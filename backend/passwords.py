"""Password hashing and policy — Argon2id, with scrypt kept for verification.

WHY THE ALGORITHM CHANGED
-------------------------

This file used to be stdlib-only: `hashlib.scrypt`, no dependency, which
suited a project whose whole install was FastAPI plus pydantic. scrypt is
a perfectly respectable memory-hard KDF and nothing about it was broken.

Argon2id is the current recommendation (Password Hashing Competition
winner; OWASP's first choice; RFC 9106) and it is better in one specific
way that matters here: it is hybrid. Argon2i resists side-channel attacks
on memory access patterns but is weaker against time-memory trade-offs;
Argon2d is the reverse. Argon2id runs the first half of the first pass in
the data-independent mode and the rest data-dependent, so it gets most of
both. scrypt's access pattern is data-dependent throughout.

The cost is one C extension (`argon2-cffi`). That is a real cost and it
is paid deliberately, not casually.

THE MIGRATION IS THE INTERESTING PART
--------------------------------------

Every existing account in a deployed instance has a scrypt hash. We
cannot re-hash them — that would need the plaintexts, which is exactly
what we do not have and never want. Deleting them would lock every
existing student out of their own history.

So `verify()` reads the scheme out of the stored string and dispatches.
scrypt hashes keep verifying forever; `needs_rehash()` reports True for
every one of them; and `accounts.authenticate` already re-hashes on a
successful login, at the one moment the plaintext is legitimately in
memory. An account migrates itself the next time its owner signs in, and
one that never signs in again stays safely hashed under the old scheme.

Nothing here writes a scrypt hash any more. `_hash_scrypt` exists only
so the migration path itself has a test.

WHAT THIS FILE STILL DELIBERATELY DOES NOT DO
----------------------------------------------

  - roll its own primitive. It composes argon2-cffi, stdlib scrypt and
    `secrets.compare_digest`;
  - store anything reversible;
  - log, return, or embed a password in any error message, including
    the exception text of a policy failure.
"""
import base64
import hashlib
import re
import secrets
import unicodedata

import argon2
from argon2 import low_level

# RFC 9106's "second recommended option" (64 MiB, t=3, p=4), which is the
# profile intended for interactive logins on ordinary server hardware.
# ~50-90ms per hash here. Raising memory_cost raises attacker cost roughly
# linearly and login latency likewise; this is the knob to turn first if
# you move to bigger instances.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16

_hasher = argon2.PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LEN,
    salt_len=ARGON2_SALT_LEN,
    type=low_level.Type.ID,          # Argon2id, explicitly. Not the default in every binding.
)

# Legacy scheme. Verified, never written.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SCHEME = "scrypt"

MIN_LENGTH = 10
MAX_LENGTH = 256  # bounded: a KDF over an unbounded input is a free DoS

# Rejected outright regardless of length. Deliberately short — a serious
# deployment should check against a real breached-password corpus (e.g.
# Have I Been Pwned's k-anonymity range API), which is a network call and
# therefore a deployment decision rather than something to hard-code here.
_COMMON = {
    "password", "password1", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyuiop", "letmein123", "iloveyou1", "admin12345", "welcome123",
    "autonomize", "changeme123",
}


class PasswordPolicyError(ValueError):
    """Raised with a message safe to show the user."""


def describe() -> str:
    return (f"argon2id t={ARGON2_TIME_COST} m={ARGON2_MEMORY_KIB // 1024}MiB "
            f"p={ARGON2_PARALLELISM} (scrypt hashes still verify and self-upgrade)")


def normalize(password: str) -> str:
    """Unicode-normalises so visually identical passwords hash identically.

    Without NFKC, a password typed with a composed accent on one keyboard
    and a decomposed one on another produces two different hashes and a
    user who "can't log in on their phone".
    """
    return unicodedata.normalize("NFKC", password)


def validate(password: str, *, email: str | None = None) -> None:
    """Raises PasswordPolicyError if the password is unacceptable.

    Length is the dominant factor in real-world strength, so this leans on
    a longer minimum rather than character-class rules. Forced symbol/digit
    requirements measurably push people toward `Password1!` patterns that
    are easier to guess, not harder — NIST SP 800-63B dropped them for
    exactly this reason.
    """
    password = normalize(password)

    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"Use at most {MAX_LENGTH} characters.")
    if password.lower() in _COMMON:
        raise PasswordPolicyError("That password is too common — pick something less predictable.")
    if re.fullmatch(r"(.)\1*", password):
        raise PasswordPolicyError("That's a single repeated character.")
    if email:
        local = email.split("@")[0].lower()
        if local and len(local) >= 3 and local in password.lower():
            raise PasswordPolicyError("Don't include your email address in your password.")


def hash_password(password: str) -> str:
    """Returns a PHC-format Argon2id string (`$argon2id$v=19$m=...`).

    The format is self-describing, so the cost parameters can be raised
    later without a migration — `verify` reads them out of the stored
    string and `needs_rehash` reports which rows are behind.
    """
    if len(normalize(password)) > MAX_LENGTH:
        # Enforced here as well as in validate(), because verify() paths
        # and password resets can reach hashing without going through the
        # policy check, and an unbounded input into a 64 MiB KDF is a DoS.
        raise PasswordPolicyError(f"Use at most {MAX_LENGTH} characters.")
    return _hasher.hash(normalize(password))


def _hash_scrypt(password: str) -> str:
    """The retired scheme. Kept so the upgrade path has something to test
    against — nothing in the running system calls this."""
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(
        normalize(password).encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
        maxmem=SCRYPT_N * SCRYPT_R * 256,
    )
    b64 = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"{SCRYPT_SCHEME}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(key)}"


def _verify_scrypt(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != SCRYPT_SCHEME:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        n, r, p = int(n), int(r), int(p)
    except (ValueError, TypeError):
        return False

    candidate = hashlib.scrypt(
        normalize(password).encode("utf-8"), salt=salt,
        n=n, r=r, p=p, dklen=len(expected),
        maxmem=n * r * 256,
    )
    # compare_digest, never ==: a short-circuiting comparison leaks how
    # many leading bytes matched, which is enough to reconstruct the hash
    # byte by byte given enough attempts.
    return secrets.compare_digest(candidate, expected)


def verify(password: str, stored: str | None) -> bool:
    """True if the password matches. False for any absent or malformed hash.

    Dispatches on the stored scheme so an Argon2id deployment still
    accepts every account created before the switch.
    """
    if not stored:
        # An account with no password (OAuth-only, OTP-only, or a device
        # account) must never be loggable into with an empty string. This
        # is the single most important line in the file.
        return False

    if stored.startswith("$argon2"):
        try:
            return _hasher.verify(stored, normalize(password))
        except argon2.exceptions.VerificationError:
            return False
        except argon2.exceptions.InvalidHashError:
            return False

    if stored.startswith(SCRYPT_SCHEME + "$"):
        return _verify_scrypt(password, stored)

    return False


def dummy_verify() -> None:
    """Burns a hash's worth of CPU against a throwaway value.

    Called on the "no such account" branch of login so the response time
    does not separate an unknown email (instant) from a wrong password
    (~70ms of Argon2id). Not constant-time in the strict sense; it removes
    the trivially measurable difference, which is the practical bar.
    """
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-password")
    except Exception:  # noqa: BLE001 — the failure IS the expected path
        pass


_DUMMY_HASH = _hasher.hash("autonomize-timing-decoy-value")


def needs_rehash(stored: str | None) -> bool:
    """True if a stored hash predates the current scheme or cost profile,
    so it can be transparently upgraded on the next successful login.

    Every scrypt hash returns True — that is what drives the migration.
    """
    if not stored:
        return False
    if stored.startswith("$argon2"):
        try:
            return _hasher.check_needs_rehash(stored)
        except argon2.exceptions.InvalidHashError:
            return True
    return True
