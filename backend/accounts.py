"""First-party accounts, roles, and revocable sessions.

This exists alongside the optional Supabase integration rather than
replacing it. Supabase gives you Google/OTP without running an identity
provider; this gives you a login that works from a fresh `git clone` with
no external service, no API keys, and no signup — which is what makes the
login panel demonstrable rather than a screenshot of a form that can't be
submitted.

Both paths converge on the same thing: a `user_id` and a `role`, resolved
by `auth.resolve_identity`.

SECURITY POSTURE — what is and isn't defended here
--------------------------------------------------
Implemented:
  - Argon2id password hashing (see passwords.py), never reversible
    storage, with scrypt hashes from before the switch still verifying
    and self-upgrading on next login
  - constant-time verification and constant-time-ish login regardless of
    whether the email exists (no user enumeration via timing or message)
  - per-account lockout with exponential backoff after repeated failures
  - short-lived access tokens with rotating refresh tokens and family
    reuse detection (see tokens.py)
  - server-side sessions keyed by `jti`, so logout and "revoke everything"
    actually terminate access instead of waiting for a JWT to expire
  - email verification and OTP login/reset over a real mail transport
    (see otp.py, mailer.py) — and an explicit console mode that says so
    rather than pretending mail was sent
  - Google OAuth with state, nonce and PKCE (see oauth_google.py)
  - an append-only audit log of security events
  - IP addresses hashed, never stored raw
  - role changes only ever server-side; the client cannot assert a role
  - soft-deleted accounts refuse authentication and identity resolution

Not implemented, and deliberately named rather than implied:
  - MFA/TOTP as a second factor. OTP here is a login METHOD, not a second
    factor on top of a password, and calling it 2FA would be a lie.
  - device fingerprinting or anomaly-based session binding. The device
    id is random per install by design — see devices.py.
  - password-breach corpus checks (needs a network call — see passwords.py)
  - any provider beyond Google
"""
import hashlib
import os
import re
import secrets
import time
import uuid

import jwt

import db
import passwords
import tokens

# Signing key for our own session tokens. Distinct from SUPABASE_JWT_SECRET
# — these are different issuers and must not be interchangeable.
SECRET = os.environ.get("AUTONOMIZE_AUTH_SECRET")

# Generated per-process when unset so local development works with zero
# configuration. The cost is that a restart invalidates every session,
# which is the correct trade for a dev default: the alternative is a
# hard-coded fallback key, and a hard-coded signing key in a public repo
# is a total authentication bypass for anyone who deploys it as-is.
EPHEMERAL_SECRET = False
if not SECRET:
    SECRET = secrets.token_urlsafe(48)
    EPHEMERAL_SECRET = True

ALGORITHM = "HS256"
ISSUER = "autonomize"
SESSION_TTL_SECONDS = int(os.environ.get("AUTONOMIZE_SESSION_TTL", 60 * 60 * 12))  # 12h

ROLES = ("student", "admin")
DEFAULT_ROLE = "student"

MAX_FAILED_LOGINS = 5
LOCKOUT_BASE_SECONDS = 60  # doubles per failure past the threshold
LOCKOUT_MAX_SECONDS = 60 * 60

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


class AuthError(Exception):
    """Carries a message safe to return to the client verbatim."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def hash_ip(ip: str | None) -> str | None:
    """One-way, salted with the signing key.

    Raw IPs are personal data under GDPR and are rarely what you actually
    need — "is this the same client as before" and "how many distinct
    clients" both work fine on a hash. Storing the hash means a database
    leak doesn't hand over everyone's location history.
    """
    if not ip:
        return None
    return hashlib.sha256(f"{SECRET}:{ip}".encode()).hexdigest()[:32]


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def audit(conn, event: str, *, actor_id: str | None = None, detail: str | None = None,
          ip: str | None = None) -> None:
    conn.execute(
        db.q("INSERT INTO audit_log (at, actor_id, event, detail, ip_hash) VALUES (?,?,?,?,?)"),
        (int(time.time() * 1000), actor_id, event, detail, hash_ip(ip)),
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def get_user_by_email(conn, email: str, *, include_deleted: bool = False):
    row = conn.execute(
        db.q("SELECT * FROM users WHERE email = ?"), (normalize_email(email),)
    ).fetchone()
    if row is None:
        return None
    row = dict(row)
    if not include_deleted and row.get("deleted_at") is not None:
        return None
    return row


def get_user(conn, user_id: str, *, include_deleted: bool = False):
    row = conn.execute(db.q("SELECT * FROM users WHERE user_id = ?"), (user_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    if not include_deleted and row.get("deleted_at") is not None:
        # A deleted account resolving as a live identity would keep every
        # issued token working after "delete my account" — the one
        # operation where a stale read is unambiguously a bug.
        return None
    return row


def create_user(conn, *, email: str, password: str | None, role: str = DEFAULT_ROLE,
                display_name: str | None = None, provider: str = "password",
                ip: str | None = None) -> dict:
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("That doesn't look like a valid email address.")
    if role not in ROLES:
        raise AuthError("Unknown role.")

    if get_user_by_email(conn, email):
        # Registration is the one place enumeration is hard to avoid
        # entirely — you cannot both create the account and pretend you
        # didn't. Kept generic; the real mitigation is the rate limit on
        # this endpoint.
        raise AuthError("That email is already registered.", status=409)

    password_hash = None
    if password is not None:
        passwords.validate(password, email=email)
        password_hash = passwords.hash_password(password)
    elif provider == "password":
        raise AuthError("A password is required.")

    user_id = str(uuid.uuid4())
    conn.execute(
        db.q("""INSERT INTO users (user_id, email, password_hash, role, display_name,
                                   provider, email_verified, created_at)
                VALUES (?,?,?,?,?,?,?,?)"""),
        (user_id, email, password_hash, role, display_name, provider, 0, int(time.time() * 1000)),
    )
    audit(conn, "user.created", actor_id=user_id, detail=f"role={role} provider={provider}", ip=ip)
    return get_user(conn, user_id)


DEVICE_EMAIL_DOMAIN = "device.autonomize.local"
DEVICE_SESSION_TTL_SECONDS = int(
    os.environ.get("AUTONOMIZE_DEVICE_SESSION_TTL", 60 * 60 * 24 * 365)
)


def create_device_user(conn, *, ip: str | None = None, user_agent: str | None = None) -> dict:
    """Creates an anonymous, password-less account bound to one browser.

    WHY THIS EXISTS
    ---------------
    The telemetry endpoints used to accept whatever `user_id` the client
    sent. That made the zero-configuration first run work — install the
    extension, it invents a UUID, everything scores — at the cost of an
    IDOR: anyone who knew (or guessed) another user's id could read or
    delete their data through /api/me/export and /api/me/data.

    The obvious fix, requiring a real account, would have cost the property
    that made the project demonstrable: no signup, no email, no external
    identity provider, works from a fresh clone.

    A device account keeps both. The extension asks the server once for an
    identity; the server generates it, stores it, and returns a session
    token. From then on the token is the only thing that identifies the
    caller, and a `user_id` in a request body is ignored. The client never
    chooses who it is, which is the whole of the fix.

    The email is synthetic and unroutable by construction — the `users`
    table requires one and it is unique-indexed. `provider='device'` marks
    these rows so they can be told apart from real accounts, and
    `password_hash` is NULL so none of them can ever be logged into.

    These sessions get a much longer TTL than a password login (a year by
    default): there is no credential to re-enter, so expiring one would
    silently orphan a student's history rather than prompt a re-login.
    """
    user_id = str(uuid.uuid4())
    email = f"device+{user_id}@{DEVICE_EMAIL_DOMAIN}"

    conn.execute(
        db.q("""INSERT INTO users (user_id, email, password_hash, role, display_name,
                                   provider, email_verified, created_at)
                VALUES (?,?,?,?,?,?,?,?)"""),
        (user_id, email, None, DEFAULT_ROLE, None, "device", 0, int(time.time() * 1000)),
    )
    audit(conn, "user.created", actor_id=user_id, detail="provider=device", ip=ip)

    user = get_user(conn, user_id)
    session = issue_session(conn, user, ip=ip, user_agent=user_agent,
                            ttl_seconds=DEVICE_SESSION_TTL_SECONDS)
    return {"user": user, "session": session}


def _lockout_remaining(user: dict, now_ms: int) -> int:
    locked_until = user.get("locked_until") or 0
    return max(0, int((locked_until - now_ms) / 1000))


def _register_failure(conn, user: dict, now_ms: int) -> None:
    failures = int(user.get("failed_logins") or 0) + 1
    locked_until = None
    if failures >= MAX_FAILED_LOGINS:
        # Exponential backoff past the threshold, capped. Lockout is not
        # permanent on purpose: a permanent lock turns a guessing attempt
        # against someone else's account into a denial of service against
        # its owner.
        overshoot = failures - MAX_FAILED_LOGINS
        delay = min(LOCKOUT_BASE_SECONDS * (2 ** overshoot), LOCKOUT_MAX_SECONDS)
        locked_until = now_ms + delay * 1000
    conn.execute(
        db.q("UPDATE users SET failed_logins = ?, locked_until = ? WHERE user_id = ?"),
        (failures, locked_until, user["user_id"]),
    )


def authenticate(conn, *, email: str, password: str, ip: str | None = None,
                 user_agent: str | None = None, with_refresh: bool = False,
                 device_id: str | None = None) -> dict:
    """Verifies credentials and returns an issued session. Raises AuthError."""
    now_ms = int(time.time() * 1000)
    email = normalize_email(email)
    user = get_user_by_email(conn, email)

    if user is None:
        # Burn comparable CPU to a real verification so response time
        # doesn't reveal whether the address exists. Not perfectly
        # constant-time, but it removes the trivially measurable
        # difference between "no such user" (instant) and "wrong password"
        # (~70ms of Argon2id).
        #
        # This used to hash a fresh decoy and then verify it — i.e. TWO
        # KDF runs against the real branch's one, which made the missing
        # account measurably SLOWER and leaked the same bit in reverse.
        passwords.dummy_verify()
        audit(conn, "login.failed", detail="unknown_email", ip=ip)
        conn.commit()   # see _commit_before_raising below
        raise AuthError("Email or password is incorrect.", status=401)

    remaining = _lockout_remaining(user, now_ms)
    if remaining > 0:
        audit(conn, "login.blocked", actor_id=user["user_id"], detail="locked_out", ip=ip)
        conn.commit()
        raise AuthError(
            f"Too many failed attempts. Try again in {max(1, remaining // 60)} minute(s).",
            status=429,
        )

    if not passwords.verify(password, user.get("password_hash")):
        _register_failure(conn, user, now_ms)
        audit(conn, "login.failed", actor_id=user["user_id"], detail="bad_password", ip=ip)
        # ------------------------------------------------------------------
        # _commit_before_raising
        #
        # This line is the difference between a working lockout and a
        # decorative one, and its absence was a real bug in this file for
        # as long as lockout has been documented in the module docstring.
        #
        # `db.get_conn()` commits on a clean exit and rolls back on an
        # exception. Every failure branch here writes something that must
        # SURVIVE the failure — the incremented counter, the audit row —
        # and then raises. Without an explicit commit the raise rolls the
        # write straight back: `failed_logins` sat at 0 after any number
        # of wrong guesses, MAX_FAILED_LOGINS was never reached, and the
        # audit log recorded no failed logins at all. Both controls read
        # as implemented and neither was.
        #
        # A test caught it (test_security_auth.py::test_ATTACK_password_
        # spraying_one_account_hits_the_lockout) precisely because it
        # asserted the OUTCOME — sixth attempt is refused — rather than
        # that the code path ran.
        # ------------------------------------------------------------------
        conn.commit()
        # Same message and status as the unknown-email branch above.
        raise AuthError("Email or password is incorrect.", status=401)

    # Transparently upgrade a hash made under weaker parameters, now that
    # we have the plaintext in hand and know it's correct.
    if passwords.needs_rehash(user.get("password_hash")):
        conn.execute(
            db.q("UPDATE users SET password_hash = ? WHERE user_id = ?"),
            (passwords.hash_password(password), user["user_id"]),
        )

    conn.execute(
        db.q("UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE user_id = ?"),
        (now_ms, user["user_id"]),
    )
    audit(conn, "login.succeeded", actor_id=user["user_id"], ip=ip)
    return issue_session(conn, user, ip=ip, user_agent=user_agent,
                         with_refresh=with_refresh, device_id=device_id)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def issue_session(conn, user: dict, *, ip: str | None = None,
                  user_agent: str | None = None, ttl_seconds: int | None = None,
                  with_refresh: bool = False, device_id: str | None = None) -> dict:
    """Mints an access token, and optionally the refresh token beside it.

    `with_refresh` defaults to False so that every existing caller keeps
    the shape it had. New callers — every real login path — pass True and
    get the short access token plus a rotating refresh token, which is
    the pair that makes a 10-minute access TTL usable.
    """
    now = int(time.time())
    jti = str(uuid.uuid4())
    if ttl_seconds is None and with_refresh:
        # A refresh token exists precisely so the access token can be
        # short. Leaving this at 12 hours would keep the old blast radius
        # while adding the machinery to avoid it.
        ttl_seconds = tokens.ACCESS_TTL_SECONDS
    # Device accounts pass a much longer TTL — there is no credential to
    # re-enter, so an expiry would orphan history rather than prompt a login.
    expires = now + (ttl_seconds or SESSION_TTL_SECONDS)

    conn.execute(
        db.q("""INSERT INTO auth_sessions (jti, user_id, issued_at, expires_at, user_agent, ip_hash)
                VALUES (?,?,?,?,?,?)"""),
        (jti, user["user_id"], now * 1000, expires * 1000, (user_agent or "")[:200], hash_ip(ip)),
    )

    token = jwt.encode(
        {
            "sub": user["user_id"],
            "email": user["email"],
            # The role is a claim for convenience only. Every protected
            # route re-reads it from the database (see auth.resolve_identity)
            # so a role revoked mid-session takes effect immediately rather
            # than at token expiry.
            "role": user["role"],
            "jti": jti,
            "iss": ISSUER,
            "iat": now,
            "exp": expires,
        },
        SECRET,
        algorithm=ALGORITHM,
    )
    issued = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_at": expires * 1000,
        "expires_in": expires - now,
        "user": public_user(user),
    }

    if with_refresh:
        refresh = tokens.issue_refresh(
            conn, user_id=user["user_id"], device_id=device_id, session_jti=jti,
            ip_hash=hash_ip(ip), user_agent=user_agent,
        )
        issued["refresh_token"] = refresh["token"]
        issued["refresh_expires_at"] = refresh["expires_at"]
        issued["family_id"] = refresh["family_id"]

    return issued


def verify_session(conn, token: str) -> dict:
    """Decodes and checks a session token against the session table."""
    try:
        payload = jwt.decode(token, SECRET, algorithms=[ALGORITHM], issuer=ISSUER)
    except jwt.PyJWTError:
        raise AuthError("Session is invalid or has expired.", status=401)

    jti = payload.get("jti")
    if not jti:
        raise AuthError("Session is invalid or has expired.", status=401)

    row = conn.execute(db.q("SELECT * FROM auth_sessions WHERE jti = ?"), (jti,)).fetchone()
    if row is None:
        raise AuthError("Session is invalid or has expired.", status=401)
    row = dict(row)
    if row["revoked_at"] is not None:
        # The whole reason sessions are stored rather than purely stateless.
        raise AuthError("Session has been signed out.", status=401)
    if row["expires_at"] < int(time.time() * 1000):
        raise AuthError("Session is invalid or has expired.", status=401)

    user = get_user(conn, payload["sub"])
    if user is None:
        raise AuthError("Account no longer exists.", status=401)
    return user


def revoke_session(conn, token: str, *, ip: str | None = None) -> None:
    try:
        payload = jwt.decode(
            token, SECRET, algorithms=[ALGORITHM], issuer=ISSUER,
            options={"verify_exp": False},  # logging out an expired token is fine
        )
    except jwt.PyJWTError:
        return  # nothing to revoke; not an error worth surfacing
    conn.execute(
        db.q("UPDATE auth_sessions SET revoked_at = ? WHERE jti = ? AND revoked_at IS NULL"),
        (int(time.time() * 1000), payload.get("jti")),
    )
    audit(conn, "logout", actor_id=payload.get("sub"), ip=ip)


def revoke_all_sessions(conn, user_id: str, *, ip: str | None = None) -> int:
    now = int(time.time() * 1000)
    before = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM auth_sessions WHERE user_id = ? AND revoked_at IS NULL"),
        (user_id,),
    ).fetchone()["n"]
    conn.execute(
        db.q("UPDATE auth_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL"),
        (now, user_id),
    )
    # Refresh tokens too, or "sign out everywhere" signs out nothing that
    # matters: the access tokens die in ten minutes anyway, and any device
    # still holding a live refresh token would mint a replacement and
    # carry on. This is the line that makes the button mean something.
    refresh_killed = tokens.revoke_all_for_user(conn, user_id, reason="logout_all")
    audit(conn, "sessions.revoked_all", actor_id=user_id,
          detail=f"count={before} refresh={refresh_killed}", ip=ip)
    return int(before or 0)


def set_display_name(conn, user_id: str, display_name: str) -> dict:
    """Updates a user's display name and returns the fresh row.

    Returns the row rather than nothing so the caller serialises what was
    actually stored. A client that optimistically rendered the string it
    sent would be showing unstripped whitespace, and — more to the point —
    would keep showing it after a failed write.
    """
    display_name = display_name.strip()[:80]
    if not display_name:
        raise AuthError("Display name cannot be empty.")
    conn.execute(
        db.q("UPDATE users SET display_name = ? WHERE user_id = ?"),
        (display_name, user_id),
    )
    updated = get_user(conn, user_id)
    if not updated:
        raise AuthError("That account no longer exists.")
    return updated


# ---------------------------------------------------------------------------
# Email verification, OTP sign-in, password reset, deletion
#
# Everything below shares one rule: an endpoint that takes an email
# address must behave identically whether or not that address has an
# account. Any difference — a different message, a different status, a
# measurably different latency — turns "forgot password" into a free
# membership oracle for the whole institution.
# ---------------------------------------------------------------------------

def mark_email_verified(conn, user_id: str) -> dict:
    now = int(time.time() * 1000)
    conn.execute(
        db.q("UPDATE users SET email_verified = 1, email_verified_at = ? WHERE user_id = ?"),
        (now, user_id),
    )
    audit(conn, "email.verified", actor_id=user_id)
    return get_user(conn, user_id)


def create_or_get_otp_user(conn, *, email: str, ip: str | None = None) -> tuple:
    """For OTP signup/login, where 'sign up' and 'sign in' are one button.

    Returns `(user, created)`. The account is created with NO password —
    `password_hash` stays NULL, and `passwords.verify` returns False for
    NULL unconditionally, so an OTP-only account can never be logged into
    with a guessed password. Adding one later goes through
    `set_password`.
    """
    email = normalize_email(email)
    if not EMAIL_RE.match(email):
        raise AuthError("That doesn't look like a valid email address.")

    existing = get_user_by_email(conn, email)
    if existing:
        return existing, False

    user_id = str(uuid.uuid4())
    conn.execute(
        db.q("""INSERT INTO users (user_id, email, password_hash, role, display_name,
                                   provider, email_verified, created_at)
                VALUES (?,?,?,?,?,?,?,?)"""),
        (user_id, email, None, DEFAULT_ROLE, None, "otp", 0, int(time.time() * 1000)),
    )
    audit(conn, "user.created", actor_id=user_id, detail="provider=otp", ip=ip)
    return get_user(conn, user_id), True


def set_password(conn, user_id: str, password: str, *, ip: str | None = None,
                 reason: str = "password.changed") -> dict:
    """Sets or replaces a password and stamps `password_changed_at`.

    Does NOT revoke sessions — the caller decides, because the right
    answer differs. A reset (the account may be compromised) must revoke
    everything. A deliberate change from inside a live session should
    revoke every OTHER session but keep the one the user is sitting in,
    or they get logged out for doing the responsible thing.
    """
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("That account no longer exists.", status=404)

    passwords.validate(password, email=user["email"])
    conn.execute(
        db.q("""UPDATE users SET password_hash = ?, password_changed_at = ?,
                                 failed_logins = 0, locked_until = NULL
                WHERE user_id = ?"""),
        (passwords.hash_password(password), int(time.time() * 1000), user_id),
    )
    audit(conn, reason, actor_id=user_id, ip=ip)
    return get_user(conn, user_id)


def change_password(conn, user_id: str, *, current_password: str, new_password: str,
                    ip: str | None = None) -> dict:
    """Requires the current password even though the caller is already
    authenticated.

    That is not redundant. It is what stops a stolen session — a borrowed
    laptop, an XSS payload, a shared machine someone forgot to sign out
    of — from being upgraded into permanent ownership of the account by
    changing the password out from under its owner.
    """
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("That account no longer exists.", status=404)

    if not user.get("password_hash"):
        raise AuthError(
            "This account signs in with Google or an email code. Use 'set a password' "
            "instead.", status=409)

    if not passwords.verify(current_password, user["password_hash"]):
        audit(conn, "password.change_failed", actor_id=user_id, ip=ip)
        raise AuthError("Your current password is incorrect.", status=401)

    if passwords.verify(new_password, user["password_hash"]):
        raise AuthError("That's the password you already have.")

    return set_password(conn, user_id, new_password, ip=ip)


def soft_delete(conn, user_id: str, *, ip: str | None = None) -> dict:
    """Marks an account deleted and severs every way back into it.

    Soft, not a DELETE, and the reason is narrow: `sessions`,
    `retrieval_checks` and friends carry this user_id, and a hard delete
    of the row would leave those orphaned rather than removed. So the
    account row is tombstoned here and the telemetry is deleted by the
    caller (`/api/me/data`), which already knows every table.

    The email is released — rewritten to a tombstone address — so the
    person can sign up again with the same address later. Keeping it
    would mean "delete my account" silently burns their email forever.
    """
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("That account no longer exists.", status=404)

    now = int(time.time() * 1000)
    tombstone = f"deleted+{user_id}@{DEVICE_EMAIL_DOMAIN}"
    conn.execute(
        db.q("""UPDATE users SET deleted_at = ?, email = ?, password_hash = NULL,
                                 display_name = NULL, email_verified = 0
                WHERE user_id = ?"""),
        (now, tombstone, user_id),
    )
    conn.execute(db.q("DELETE FROM identities WHERE user_id = ?"), (user_id,))
    revoked = revoke_all_sessions(conn, user_id, ip=ip)
    audit(conn, "account.deleted", actor_id=user_id, detail=f"sessions={revoked}", ip=ip)
    return {"deleted_at": now, "sessions_revoked": revoked}


def public_user(user: dict) -> dict:
    """The only shape of a user that ever leaves the backend.

    Explicitly allow-listed rather than deleting sensitive keys from the
    row — a future column is then invisible by default instead of leaking
    until someone remembers to redact it.
    """
    return {
        "user_id": user["user_id"],
        "email": user["email"],
        "role": user["role"],
        "display_name": user.get("display_name"),
        "provider": user.get("provider"),
        "email_verified": bool(user.get("email_verified")),
        # Whether a password EXISTS, never anything about it. The UI needs
        # this to decide between "change password" and "set a password",
        # and to warn before unlinking the only sign-in method.
        "has_password": bool(user.get("password_hash")),
        "is_device_account": user.get("provider") == "device",
    }
