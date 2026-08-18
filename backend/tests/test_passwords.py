"""Tests for password hashing and policy.

Security code, so these lean adversarial: the interesting cases are the
ones an attacker creates, not the happy path.
"""
import pytest

import passwords


def test_hash_is_not_the_password():
    stored = passwords.hash_password("correct horse battery staple")
    assert "correct horse" not in stored
    # PHC format, and specifically the `id` variant — argon2i and argon2d
    # each give up one of the two properties Argon2id is chosen for.
    assert stored.startswith("$argon2id$")


def test_same_password_hashes_differently_every_time():
    a = passwords.hash_password("correct horse battery staple")
    b = passwords.hash_password("correct horse battery staple")
    # Distinct salts. Without them, identical passwords share a hash and a
    # single leak reveals every account using that password at once.
    assert a != b
    assert passwords.verify("correct horse battery staple", a)
    assert passwords.verify("correct horse battery staple", b)


def test_verify_accepts_the_right_password_and_rejects_others():
    stored = passwords.hash_password("correct horse battery staple")
    assert passwords.verify("correct horse battery staple", stored) is True
    assert passwords.verify("Correct horse battery staple", stored) is False
    assert passwords.verify("", stored) is False


def test_empty_password_cannot_unlock_an_oauth_only_account():
    # password_hash is NULL for accounts created via Google/OTP. If verify
    # returned True for those, every OAuth account would be trivially
    # accessible with a blank password.
    assert passwords.verify("", None) is False
    assert passwords.verify("anything", None) is False


@pytest.mark.parametrize("garbage", ["", "notahash", "scrypt$bad", "scrypt$a$b$c$d$e",
                                     "argon2$1$2$3$c2FsdA==$a2V5"])
def test_malformed_stored_hash_never_verifies(garbage):
    assert passwords.verify("anything", garbage) is False


def test_unicode_normalisation_so_the_same_password_works_across_keyboards():
    composed = "café-password-123"          # U+00E9
    decomposed = "café-password-123"  # e + combining acute
    assert composed != decomposed
    stored = passwords.hash_password(composed)
    assert passwords.verify(decomposed, stored) is True


def test_policy_requires_length():
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate("short1")


def test_policy_rejects_common_passwords():
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate("password1")


def test_policy_rejects_a_single_repeated_character():
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate("aaaaaaaaaaaaaaa")


def test_policy_rejects_a_password_containing_the_email():
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate("priyanshu-2026!", email="priyanshu@example.com")


def test_policy_accepts_a_long_ordinary_passphrase():
    # Deliberately no symbol/digit requirement — forced character classes
    # push people toward Password1! patterns. Length does the work.
    passwords.validate("the quiet river runs north", email="a@b.com")


def test_policy_bounds_the_maximum_length():
    # Unbounded input into a memory-hard KDF is a free denial of service.
    with pytest.raises(passwords.PasswordPolicyError):
        passwords.validate("x" * (passwords.MAX_LENGTH + 1))


def test_needs_rehash_flags_weaker_parameters():
    weak = passwords.hash_password("a decent long password").replace("m=65536", "m=8192")
    assert passwords.needs_rehash(weak) is True
    assert passwords.needs_rehash(passwords.hash_password("a decent long password")) is False
    assert passwords.needs_rehash(None) is False


# ---------------------------------------------------------------------------
# The scrypt -> Argon2id migration
#
# Every account created before the switch has a scrypt hash and there is no
# way to convert it without the plaintext. These tests pin the only safe
# path: keep verifying the old scheme, and flag it for upgrade at the one
# moment the plaintext is legitimately in memory — a successful login.
# ---------------------------------------------------------------------------

def test_a_legacy_scrypt_hash_still_verifies():
    """If this breaks, every existing student is locked out of their own
    history by a dependency upgrade."""
    legacy = passwords._hash_scrypt("correct horse battery staple")
    assert legacy.startswith("scrypt$")
    assert passwords.verify("correct horse battery staple", legacy) is True
    assert passwords.verify("wrong password entirely", legacy) is False


def test_every_scrypt_hash_is_flagged_for_upgrade():
    """needs_rehash is what drives the migration — accounts.authenticate
    re-hashes on it. A False here would freeze the old scheme in place
    forever without anyone noticing."""
    assert passwords.needs_rehash(passwords._hash_scrypt("a decent long password")) is True


def test_nothing_writes_a_scrypt_hash_any_more():
    for _ in range(3):
        assert not passwords.hash_password("a decent long password").startswith("scrypt$")


def test_an_unknown_scheme_is_rejected_rather_than_guessed():
    """A corrupted or truncated column must fail closed. Returning True on
    a hash we cannot parse would be an authentication bypass."""
    for junk in ("", "$argon2id$truncated", "bcrypt$2b$12$whatever",
                 "scrypt$not$a$number$x$y", "plaintext-password"):
        assert passwords.verify("plaintext-password", junk) is False


def test_dummy_verify_costs_roughly_what_a_real_one_does():
    """The unknown-email branch of login calls this so response time does
    not separate 'no such account' from 'wrong password'. If it were free,
    an attacker could enumerate the user table with a stopwatch."""
    import time
    stored = passwords.hash_password("a decent long password")

    def clock(fn):
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        return best

    real = clock(lambda: passwords.verify("wrong password here", stored))
    decoy = clock(passwords.dummy_verify)
    # Same order of magnitude. A tight bound would be flaky on shared CI;
    # what must never happen is decoy being ~0 while real is ~70ms.
    assert 0.25 < decoy / real < 4.0
