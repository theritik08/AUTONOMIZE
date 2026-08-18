"""Short-lived access tokens and rotating refresh tokens with reuse detection.

THE PROBLEM WITH WHAT WAS HERE BEFORE
--------------------------------------

`accounts.issue_session` minted a 12-hour JWT and the client sent it on
every request. It was revocable — the `jti` is checked against a stored
row, which is already better than a naked stateless JWT — but a token
stolen at minute 1 worked for the next 719 minutes unless somebody
noticed and revoked it. Nobody notices.

WHAT REPLACES IT

Two credentials with different jobs:

    ACCESS TOKEN    10 minutes, sent on every request, checked by
                    signature and by the session row behind it. Short
                    enough that a leaked one is a narrow window rather
                    than a day.

    REFRESH TOKEN   30 days, sent ONLY to /api/auth/refresh, never on an
                    ordinary API call. Stored hashed. Rotates on every
                    use: presenting it returns a new one and marks this
                    one used.

ROTATION IS ONLY USEFUL IF REUSE IS DETECTED
---------------------------------------------

Rotation on its own gains little — if an attacker steals a refresh token
and uses it first, they get the new one and the victim gets an error they
will interpret as "the app logged me out again".

The fix is that every token descended from a single login shares a
`family_id`, and a token that has ALREADY been used, if presented a
second time, revokes the whole family. Consider the two cases:

    the attacker refreshes first   the victim's next refresh presents a
                                   token the attacker already burned ->
                                   reuse -> family dies -> the attacker's
                                   stolen token dies with it.

    the victim refreshes first     the attacker's stolen token is now the
                                   used one -> reuse -> family dies.

Either way the family is revoked and the legitimate user re-authenticates
with a credential the attacker does not have. This is the mechanism from
the OAuth 2.0 Security BCP (draft-ietf-oauth-security-topics), and the
property worth stating plainly is that it does not require knowing WHICH
party was the thief — it cannot, and it does not need to.

WHY SHA-256 AND NOT ARGON2 FOR THE TOKEN HASH
----------------------------------------------

These are 128 bits of `secrets.token_urlsafe` output, not passwords.
There is no dictionary to slow an attacker down and no low-entropy guess
to make expensive. Argon2 here would add ~70ms of CPU to every token
refresh — a self-inflicted denial of service — and buy nothing. Password
hashing is slow on purpose because passwords are guessable; random tokens
are not.

WHAT THIS DOES NOT DEFEND AGAINST
----------------------------------

An attacker with live access to the client (a malicious extension in the
same browser, malware on the machine) can steal the refresh token and
keep rotating it silently, and reuse detection never fires because the
victim's copy is being overwritten too. Binding a token to a device id
narrows this — a stolen token used from a different device is rejected —
but a thief who is already inside the browser has the device id too.
This is defence in depth, not a guarantee.
"""
import hashlib
import hmac
import os
import secrets
import time
import uuid

import db

# Short. This is the number that limits the damage of a leaked access
# token, and every increase is a direct trade against that.
ACCESS_TTL_SECONDS = int(os.environ.get("AUTONOMIZE_ACCESS_TTL", 10 * 60))

# Long, because rotation means a live client keeps renewing it and only an
# ABANDONED one ever reaches this. It doubles as "log out after 30 days of
# not opening the app".
REFRESH_TTL_SECONDS = int(os.environ.get("AUTONOMIZE_REFRESH_TTL", 30 * 24 * 60 * 60))

# 32 bytes of urlsafe base64 -> 256 bits. Guessing is not a threat model
# at this size; the storage hash is about database leaks, not brute force.
TOKEN_BYTES = 32

REUSE_DETECTED = "reuse_detected"


class TokenError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.message = message
        self.status = status


def _now_ms() -> int:
    return int(time.time() * 1000)


def hash_token(token: str) -> str:
    """SHA-256 of the raw token. See the module docstring for why not Argon2.

    Keyed with the service secret so that a leaked database alone is not
    enough to confirm a guessed token offline — an attacker would need the
    secret too. Cheap to add, occasionally useful.
    """
    from accounts import SECRET
    return hmac.new(SECRET.encode(), token.encode(), hashlib.sha256).hexdigest()


def issue_refresh(conn, *, user_id: str, family_id: str | None = None,
                  device_id: str | None = None, session_jti: str | None = None,
                  ip_hash: str | None = None, user_agent: str | None = None) -> dict:
    """Mints a refresh token. Pass `family_id` to continue a chain, omit it
    to start one (i.e. on a fresh login)."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now_ms()
    row = {
        "token_id": str(uuid.uuid4()),
        "family_id": family_id or str(uuid.uuid4()),
        "user_id": user_id,
        "token_hash": hash_token(raw),
        "device_id": device_id,
        "session_jti": session_jti,
        "issued_at": now,
        "expires_at": now + REFRESH_TTL_SECONDS * 1000,
    }
    conn.execute(
        db.q("""INSERT INTO refresh_tokens
                (token_id, family_id, user_id, token_hash, device_id, session_jti,
                 issued_at, expires_at, user_agent, ip_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?)"""),
        (row["token_id"], row["family_id"], user_id, row["token_hash"], device_id,
         session_jti, now, row["expires_at"], (user_agent or "")[:200], ip_hash),
    )
    return {"token": raw, **row}


def _revoke_family(conn, family_id: str, reason: str) -> int:
    now = _now_ms()
    live = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM refresh_tokens WHERE family_id = ? AND revoked_at IS NULL"),
        (family_id,),
    ).fetchone()["n"]
    conn.execute(
        db.q("""UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = ?
                WHERE family_id = ? AND revoked_at IS NULL"""),
        (now, reason, family_id),
    )
    return int(live or 0)


def revoke_family(conn, family_id: str, reason: str = "logout") -> int:
    return _revoke_family(conn, family_id, reason)


def revoke_all_for_user(conn, user_id: str, reason: str = "logout_all") -> int:
    now = _now_ms()
    live = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM refresh_tokens WHERE user_id = ? AND revoked_at IS NULL"),
        (user_id,),
    ).fetchone()["n"]
    conn.execute(
        db.q("""UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = ?
                WHERE user_id = ? AND revoked_at IS NULL"""),
        (now, reason, user_id),
    )
    return int(live or 0)


def revoke_for_device(conn, device_id: str, user_id: str, reason: str = "device_revoked") -> int:
    """Kills every refresh token issued to one device.

    Scoped by user_id as well as device_id on purpose: the caller is an
    authenticated user revoking one of THEIR devices, and a device id is
    guessable enough that accepting it alone would let one account log
    another's device out.
    """
    now = _now_ms()
    live = conn.execute(
        db.q("""SELECT COUNT(*) AS n FROM refresh_tokens
                WHERE device_id = ? AND user_id = ? AND revoked_at IS NULL"""),
        (device_id, user_id),
    ).fetchone()["n"]
    conn.execute(
        db.q("""UPDATE refresh_tokens SET revoked_at = ?, revoked_reason = ?
                WHERE device_id = ? AND user_id = ? AND revoked_at IS NULL"""),
        (now, reason, device_id, user_id),
    )
    return int(live or 0)


def rotate(conn, raw_token: str, *, ip_hash: str | None = None,
           user_agent: str | None = None) -> dict:
    """Exchanges a refresh token for a new one. Raises TokenError.

    Returns `{"refresh": <new row incl. raw token>, "user_id", "device_id",
    "family_id"}`. The caller mints the access token, because that needs
    the user row and this module deliberately does not read `users`.
    """
    row = conn.execute(
        db.q("SELECT * FROM refresh_tokens WHERE token_hash = ?"), (hash_token(raw_token),)
    ).fetchone()

    if row is None:
        # Unknown token. Could be garbage, could be one from a family that
        # was already deleted. Nothing to revoke, nothing to learn.
        raise TokenError("That session has expired. Please sign in again.")
    row = dict(row)

    if row["used_at"] is not None:
        # THE detection. Someone is presenting a token that was already
        # exchanged — either a replay or a theft, and there is no way to
        # tell which from here. Both answers are "kill the family".
        revoked = _revoke_family(conn, row["family_id"], REUSE_DETECTED)
        # Commit before raising. `db.get_conn()` rolls back on an
        # exception, so revoking the family and then raising would undo
        # the revocation — the detector would fire, log, tell the user we
        # had signed everything out, and leave the attacker's token live.
        conn.commit()
        raise _reuse_error(revoked)

    if row["revoked_at"] is not None:
        raise TokenError("That session has been signed out. Please sign in again.")

    if row["expires_at"] < _now_ms():
        raise TokenError("That session has expired. Please sign in again.")

    conn.execute(
        db.q("UPDATE refresh_tokens SET used_at = ? WHERE token_id = ?"),
        (_now_ms(), row["token_id"]),
    )
    fresh = issue_refresh(
        conn, user_id=row["user_id"], family_id=row["family_id"],
        device_id=row["device_id"], session_jti=row["session_jti"],
        ip_hash=ip_hash, user_agent=user_agent,
    )
    return {
        "refresh": fresh,
        "user_id": row["user_id"],
        "device_id": row["device_id"],
        "family_id": row["family_id"],
    }


def _reuse_error(revoked: int) -> TokenError:
    error = TokenError(
        "This sign-in was used from somewhere else, so every session in it has "
        "been signed out as a precaution. Please sign in again.")
    error.reuse_detected = True
    error.sessions_revoked = revoked
    return error


def list_devices_with_sessions(conn, user_id: str) -> list:
    """Live refresh families for one user, newest first — the data behind a
    "where you're signed in" screen."""
    rows = conn.execute(
        db.q("""SELECT family_id, device_id, MAX(issued_at) AS last_used,
                       MIN(issued_at) AS started, COUNT(*) AS rotations
                FROM refresh_tokens
                WHERE user_id = ? AND revoked_at IS NULL
                GROUP BY family_id, device_id
                ORDER BY last_used DESC"""),
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def purge_expired(conn, *, older_than_days: int = 60) -> int:
    """Housekeeping. Rows must outlive their expiry by a margin, because a
    reuse attempt against an expired-but-deleted token would look like an
    unknown token and the family would never be revoked."""
    cutoff = _now_ms() - older_than_days * 24 * 60 * 60 * 1000
    before = conn.execute(
        db.q("SELECT COUNT(*) AS n FROM refresh_tokens WHERE expires_at < ?"), (cutoff,)
    ).fetchone()["n"]
    conn.execute(db.q("DELETE FROM refresh_tokens WHERE expires_at < ?"), (cutoff,))
    return int(before or 0)
