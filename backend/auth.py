"""Identity resolution for every user-scoped route.

HOW THIS USED TO WORK, AND WHY IT WAS WRONG
-------------------------------------------

There were two identity systems. `accounts.py` — scrypt, revocable
server-side sessions, roles, audit log — guarded `/api/auth/*` and the
institution cohort view. Everything else, meaning every telemetry route,
went through a separate function here that verified a Supabase JWT if one
was configured and otherwise **trusted whatever `user_id` the request
sent**.

That default existed to preserve the zero-configuration first run: install
the extension, it invents a UUID, everything scores, no signup. The cost
was an IDOR. `GET /api/me/export?user_id=<someone else's uuid>` returned
their data and `DELETE /api/me/data` would have deleted it. It was logged
at startup rather than hidden, but a documented data-exposure bug is still
a data-exposure bug, and it made the service unsafe to host anywhere.

WHAT REPLACES IT
----------------

One resolver, three credential types, no client-asserted identity:

  1. a first-party session token (`accounts.verify_session`) — covers both
     real password accounts and the anonymous *device* accounts minted by
     `POST /api/auth/device`;
  2. a Supabase access token, when `SUPABASE_JWT_SECRET` is set;
  3. nothing — which is now a 401 rather than a free pass.

Zero-config survives because of the device account. The extension no
longer invents its own id; it asks the server for one once, gets a
long-lived session token back, and sends that. The client never chooses
who it is. That is the entire fix — everything else here is bookkeeping.

`AUTONOMIZE_ALLOW_ANONYMOUS_IDS` restores the old permissive behaviour for
anyone with existing data collected under it. It is off unless explicitly
set, it warns loudly at startup, and it exists so upgrading doesn't
silently orphan a student's history — not because it is a reasonable way
to run this.
"""
import os
from typing import Optional

import jwt
from fastapi import Header, HTTPException

import accounts
import db

SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
AUTH_ENABLED = bool(SUPABASE_JWT_SECRET)

# Escape hatch for data collected before device accounts existed. Off by
# default; see the module docstring.
ALLOW_ANONYMOUS_IDS = os.environ.get("AUTONOMIZE_ALLOW_ANONYMOUS_IDS", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def describe() -> str:
    """One line for the startup log, so the running posture is never a guess."""
    modes = ["first-party sessions"]
    if AUTH_ENABLED:
        modes.append("Supabase JWT")
    if ALLOW_ANONYMOUS_IDS:
        modes.append("UNAUTHENTICATED client-supplied user_id (INSECURE)")
    return " + ".join(modes)


def _bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[len("Bearer "):].strip() or None


def _verify_supabase(token: str) -> Optional[str]:
    """Returns the Supabase user's uuid, or None if this isn't one of ours.

    Returning None rather than raising matters: a first-party session token
    and a Supabase token arrive in the same header, so failing to verify as
    one is not an error until *both* have been tried.
    """
    if not SUPABASE_JWT_SECRET:
        return None
    try:
        payload = jwt.decode(
            token, SUPABASE_JWT_SECRET, algorithms=["HS256"], audience="authenticated"
        )
    except jwt.PyJWTError:
        return None
    return payload.get("sub") or None


def resolve_identity(authorization: Optional[str] = None,
                     user_id_param: Optional[str] = None) -> dict:
    """Returns `{"user_id", "role", "source"}` for the caller, or raises 401.

    `user_id_param` is whatever the request claimed. It is ignored entirely
    unless the legacy anonymous mode is on — a request can no longer write
    into, read from, or delete another user's rows by naming them.
    """
    token = _bearer(authorization)

    if token:
        # First-party first: it is the system that owns roles and
        # revocation, so it should win when a token could be read as either.
        try:
            with db.get_conn() as conn:
                user = accounts.verify_session(conn, token)
            return {"user_id": user["user_id"], "role": user.get("role", accounts.DEFAULT_ROLE),
                    "source": "session"}
        except accounts.AuthError:
            pass

        supabase_sub = _verify_supabase(token)
        if supabase_sub:
            # Supabase owns the identity but not the role — roles live in
            # our `users` table, and a token claim is not authority over
            # our own authorization decisions.
            role = accounts.DEFAULT_ROLE
            with db.get_conn() as conn:
                local = accounts.get_user(conn, supabase_sub)
                if local:
                    role = local.get("role", accounts.DEFAULT_ROLE)
            return {"user_id": supabase_sub, "role": role, "source": "supabase"}

        raise HTTPException(401, "Session is invalid or has expired.")

    if ALLOW_ANONYMOUS_IDS:
        if not user_id_param:
            raise HTTPException(400, "user_id is required")
        return {"user_id": user_id_param, "role": accounts.DEFAULT_ROLE, "source": "anonymous"}

    raise HTTPException(
        401,
        "Not signed in. The extension registers a device automatically on first run "
        "(POST /api/auth/device); the dashboard signs in at /api/auth/login.",
    )


def resolve_user_id(user_id_param: Optional[str] = None,
                    authorization: Optional[str] = Header(None)) -> str:
    """Back-compatible wrapper — the identity only, for call sites that
    don't need the role. Argument order is preserved from the previous
    version so existing callers keep working unchanged."""
    return resolve_identity(authorization, user_id_param)["user_id"]
