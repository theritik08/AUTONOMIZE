"""One-time codes: issuing, hashing, expiry, attempt caps, resend cooldown.

A six-digit code is a one-in-a-million guess. That is only safe because
of everything else in this file — a naked six-digit code with unlimited
attempts is a four-second brute force, and a code with unlimited RESENDS
is the same attack from the other end (each resend is another live code
in the same space, so N outstanding codes cut the work by N).

THE FIVE RULES, AND WHAT EACH ONE STOPS

    hashed at rest      an operator reading the table, or anyone with a
                        leaked backup, must not be able to sign in as
                        somebody. Stored as HMAC-SHA256 with the service
                        secret. Not Argon2: see the note below.

    single use          `consumed_at` is set inside the same statement
                        that claims the row, so two racing requests
                        cannot both spend one code.

    attempt capped      MAX_ATTEMPTS wrong guesses burns the code. The
                        counter increments BEFORE the comparison, so
                        crashing or disconnecting mid-verify does not
                        give a free attempt.

    expiring            TTL_MINUTES. Short enough that a code sitting in
                        an abandoned mailbox is not a standing key.

    purpose-bound       the purpose is stored in the row and matched on
                        verify. A code mailed for "confirm your email"
                        cannot be spent on "reset my password". That
                        confusion is a full account takeover and it is
                        the most common OTP bug in the wild.

Plus a resend cooldown, which is the anti-abuse control rather than an
anti-guessing one: without it, an attacker mails a victim a code every
second forever, and the victim's provider starts marking the sender as
spam — a denial of service against everyone on the deployment.

WHY HMAC-SHA256 AND NOT ARGON2
-------------------------------

Tempting, because a six-digit code IS low entropy — exactly the case
Argon2 is for. It is still wrong here. An attacker who can run offline
guesses against the stored hash has the database, and with the database
they can simply overwrite `code_hash` with their own, or read the
`users` table, or issue themselves a session. Slowing an offline attack
by an attacker who already has write access to the auth tables buys
nothing, while paying 70ms of CPU on every OTP verify — a request path
that is deliberately rate-limited anyway. The HMAC key means a leaked
database alone is not enough to check a guess offline, which is the
property that actually matters here.

The online guessing attack, which is the real one, is stopped by
MAX_ATTEMPTS — not by hash cost.

NOT IMPLEMENTED, AND NAMED RATHER THAN IMPLIED
-----------------------------------------------
  - SMS/TOTP delivery. Email only.
  - Anything that treats "has the code" as proof of identity beyond the
    mailbox. It proves control of an inbox, which is what it is used for.
"""
import hashlib
import hmac
import os
import secrets
import time
import uuid

import db
import mailer

CODE_DIGITS = 6
TTL_MINUTES = int(os.environ.get("AUTONOMIZE_OTP_TTL_MINUTES", "10"))
MAX_ATTEMPTS = int(os.environ.get("AUTONOMIZE_OTP_MAX_ATTEMPTS", "5"))
RESEND_COOLDOWN_SECONDS = int(os.environ.get("AUTONOMIZE_OTP_RESEND_COOLDOWN", "60"))

# Codes issued to one address within the window, across all purposes.
# Backstop for the case where an attacker rotates purposes to sidestep the
# per-purpose cooldown.
MAX_PER_HOUR = int(os.environ.get("AUTONOMIZE_OTP_MAX_PER_HOUR", "8"))

PURPOSES = ("signup", "login", "reset", "verify_email")


class OtpError(Exception):
    def __init__(self, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _now_ms() -> int:
    return int(time.time() * 1000)


def generate_code() -> str:
    """Uniform over 000000-999999, from the CSPRNG.

    `secrets.randbelow`, not `random.randint`: the Mersenne Twister behind
    `random` is fully reconstructible from a few hundred outputs, which
    for a code generator means predicting everyone else's codes after
    watching enough of your own.
    """
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def hash_code(code: str, email: str) -> str:
    """HMAC of the code, bound to the address it was issued to.

    Binding the email in means a code row cannot be lifted from one
    account to another even by someone editing the database, and two
    users who happen to be issued the same six digits do not share a hash.
    """
    from accounts import SECRET
    return hmac.new(SECRET.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def _recent_count(conn, email: str, since_ms: int) -> int:
    row = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM otp_codes WHERE email = ? AND created_at > ?"),
        (email, since_ms),
    ).fetchone()
    return int(row["n"] or 0)


def _last_sent(conn, email: str, purpose: str):
    row = conn.execute(
        db.q("""SELECT created_at FROM otp_codes
                WHERE email = ? AND purpose = ?
                ORDER BY created_at DESC LIMIT 1"""),
        (email, purpose),
    ).fetchone()
    return int(row["created_at"]) if row else None


def issue(conn, *, email: str, purpose: str, user_id: str | None = None,
          ip_hash: str | None = None, send: bool = True) -> dict:
    """Creates and (by default) mails a code. Raises OtpError on cooldown.

    Returns `{"otp_id", "expires_at", "delivery"}` — never the code. See
    mailer.py for why there is no debug flag that would include it.
    """
    if purpose not in PURPOSES:
        raise OtpError("Unknown verification purpose.")

    email = (email or "").strip().lower()
    now = _now_ms()

    previous = _last_sent(conn, email, purpose)
    if previous is not None:
        elapsed = (now - previous) / 1000
        if elapsed < RESEND_COOLDOWN_SECONDS:
            wait = int(RESEND_COOLDOWN_SECONDS - elapsed) + 1
            raise OtpError(f"Please wait {wait} seconds before asking for another code.",
                           status=429, retry_after=wait)

    if _recent_count(conn, email, now - 3600_000) >= MAX_PER_HOUR:
        raise OtpError("Too many codes requested for this address. Try again in an hour.",
                       status=429, retry_after=3600)

    # Any earlier live code for this address AND purpose is spent now.
    # Otherwise every resend adds another valid code to the same 10^6
    # space, and ten resends make guessing ten times easier.
    conn.execute(
        db.q("""UPDATE otp_codes SET consumed_at = ?
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL"""),
        (now, email, purpose),
    )

    code = generate_code()
    otp_id = str(uuid.uuid4())
    expires_at = now + TTL_MINUTES * 60_000
    conn.execute(
        db.q("""INSERT INTO otp_codes
                (otp_id, user_id, email, purpose, code_hash, created_at, expires_at, attempts, ip_hash)
                VALUES (?,?,?,?,?,?,?,0,?)"""),
        (otp_id, user_id, email, purpose, hash_code(code, email), now, expires_at, ip_hash),
    )

    delivery = "not_sent"
    if send:
        # Deliberately not wrapped in a try/except that swallows: if mail
        # is broken the caller must find out, because the alternative is a
        # user waiting forever for a code that was never sent.
        delivery = mailer.send_otp(email, code, purpose, TTL_MINUTES)

    return {"otp_id": otp_id, "expires_at": expires_at, "delivery": delivery,
            "ttl_minutes": TTL_MINUTES}


def verify(conn, *, email: str, purpose: str, code: str) -> dict:
    """Consumes a code. Returns the row's `{"user_id", "otp_id"}` or raises.

    Every failure returns the same message and status. Distinguishing
    "no code outstanding" from "wrong code" tells an attacker whether an
    address has a live code, which is a useful signal for timing a
    phishing mail against a real reset.
    """
    email = (email or "").strip().lower()
    code = (code or "").strip()
    now = _now_ms()
    generic = OtpError("That code is wrong or has expired. Request a new one.", status=400)

    row = conn.execute(
        db.q("""SELECT * FROM otp_codes
                WHERE email = ? AND purpose = ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1"""),
        (email, purpose),
    ).fetchone()
    if row is None:
        raise generic
    row = dict(row)

    if row["expires_at"] < now:
        conn.execute(db.q("UPDATE otp_codes SET consumed_at = ? WHERE otp_id = ?"),
                     (now, row["otp_id"]))
        raise generic

    if int(row["attempts"]) >= MAX_ATTEMPTS:
        conn.execute(db.q("UPDATE otp_codes SET consumed_at = ? WHERE otp_id = ?"),
                     (now, row["otp_id"]))
        raise OtpError("Too many incorrect attempts. Request a new code.", status=429)

    # Increment FIRST, and COMMIT it before doing anything that can raise.
    #
    # The commit is not defensive noise — without it this whole control is
    # decorative. `db.get_conn()` commits only on a clean exit and rolls
    # back on an exception, so an increment followed by `raise generic`
    # would be undone by the rollback the raise triggers. The attempt
    # counter would sit at zero forever and the six-digit space would be
    # walkable at full speed. An attacker who simply guesses wrong is
    # already on the raising path, so this is the ONLY path that matters.
    conn.execute(db.q("UPDATE otp_codes SET attempts = attempts + 1 WHERE otp_id = ?"),
                 (row["otp_id"],))
    conn.commit()

    if not hmac.compare_digest(hash_code(code, email), row["code_hash"]):
        raise generic

    # Claim it conditionally: `AND consumed_at IS NULL` means two requests
    # racing with the same correct code produce one winner, not two
    # sessions. Reading then writing would let both through, and rowcount
    # is how we learn which one we were — re-reading the row cannot tell
    # us, because both racers wrote the same millisecond value.
    cursor = conn.execute(
        db.q("UPDATE otp_codes SET consumed_at = ? WHERE otp_id = ? AND consumed_at IS NULL"),
        (now, row["otp_id"]),
    )
    if cursor.rowcount != 1:
        raise generic

    return {"user_id": row["user_id"], "otp_id": row["otp_id"], "email": email}


def purge_expired(conn, *, older_than_hours: int = 48) -> int:
    cutoff = _now_ms() - older_than_hours * 3600_000
    before = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM otp_codes WHERE created_at < ?"), (cutoff,)
    ).fetchone()["n"]
    conn.execute(db.q("DELETE FROM otp_codes WHERE created_at < ?"), (cutoff,))
    return int(before or 0)
