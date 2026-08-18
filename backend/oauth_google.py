"""Continue with Google — authorization code flow, PKCE, verified ID token.

WHAT THE THREE ANTI-FORGERY VALUES ACTUALLY DO
-----------------------------------------------

These get implemented as a checklist and then half-checked, so each one
is written down here with the attack it stops.

    state       Stops login CSRF. Without it: an attacker starts their
                own Google login, grabs the resulting `code`, and
                navigates the victim's browser to
                `/callback?code=<attacker's>`. The victim's browser is
                now signed into the ATTACKER's account, and every
                session the victim then records — their essays, their
                retrieval checks — lands in an account the attacker can
                read. The state is minted server-side, stored, and must
                come back unconsumed and unexpired.

    nonce       Stops ID token replay. The nonce goes out in the auth
                request and must appear inside the signed ID token that
                comes back, which proves the token was minted for THIS
                request rather than lifted from another application that
                shares the same Google client.

    PKCE        Stops authorization code interception. The verifier is a
                random secret kept on the server; only its SHA-256 goes
                to Google. An attacker who intercepts the code cannot
                exchange it without the verifier. This is designed for
                public clients and is not strictly required for a
                confidential one holding a client secret — it is used
                anyway because it costs nothing and the OAuth 2.1 draft
                makes it mandatory for all clients.

VERIFYING THE ID TOKEN
----------------------

The token is verified as a signature against Google's published JWKS,
with the issuer and audience checked. It is NOT decoded unverified and
trusted, which is the failure that turns "sign in with Google" into
"sign in as anybody" — a forged token with `email: victim@school.edu`
would otherwise be accepted at face value.

`email_verified` is also required to be true before the email is used to
find an existing account. Without that check, anyone who can get Google
to issue a token for an unverified address can claim the matching local
account. Google itself only sets it for addresses it has confirmed.

ACCOUNT LINKING, AND THE TAKEOVER IT WOULD OTHERWISE ENABLE
------------------------------------------------------------

If a Google login arrives for an email that already has a local password
account, the two are linked — but only because Google asserted
`email_verified: true` for it. Linking on an unverified email is a
one-step takeover of every account whose address an attacker knows.

The provider identity is stored in `identities`, keyed by
UNIQUE(provider, subject), not by email. Google's `sub` is the stable
identifier; an email address can be changed or reassigned, and keying on
it means a reassigned address inherits the previous owner's account.

NOT IMPLEMENTED
---------------
  - Any other provider. GitHub/Microsoft would slot in beside this, but
    an untested second provider is a liability, not a feature.
  - Refresh of Google's own tokens. We take the identity once at login
    and issue our own session; we never call Google again on the user's
    behalf, and so never store their access or refresh token.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import jwt
from jwt import PyJWKClient

import db

CLIENT_ID = os.environ.get("AUTONOMIZE_GOOGLE_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("AUTONOMIZE_GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("AUTONOMIZE_GOOGLE_REDIRECT_URI", "").strip()

ENABLED = bool(CLIENT_ID and CLIENT_SECRET and REDIRECT_URI)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")

STATE_TTL_SECONDS = 10 * 60
SCOPES = "openid email profile"

# Where the browser is sent after a successful callback. Only origins in
# this list are ever used — see `safe_redirect`.
ALLOWED_REDIRECTS = [
    r.strip() for r in os.environ.get("AUTONOMIZE_OAUTH_REDIRECT_ALLOWLIST", "").split(",")
    if r.strip()
]

_jwks_client = None


class OAuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def describe() -> str:
    if not ENABLED:
        missing = [name for name, value in (
            ("AUTONOMIZE_GOOGLE_CLIENT_ID", CLIENT_ID),
            ("AUTONOMIZE_GOOGLE_CLIENT_SECRET", CLIENT_SECRET),
            ("AUTONOMIZE_GOOGLE_REDIRECT_URI", REDIRECT_URI)) if not value]
        return "off (missing " + ", ".join(missing) + ")"
    return f"on, redirect {REDIRECT_URI}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def safe_redirect(candidate: str | None) -> str | None:
    """Returns `candidate` only if it is on the allowlist.

    An open redirect on an auth callback is not cosmetic: it is the last
    hop of a phishing chain that starts on a domain the victim trusts,
    and it can be used to leak a fragment-borne token to an attacker's
    page. Exact-match against a configured list, never a prefix or
    substring test — `https://autonomize.example.com.evil.tld` passes a
    `startswith` check and is a completely different origin.
    """
    if not candidate:
        return None
    return candidate if candidate in ALLOWED_REDIRECTS else None


def begin(conn, *, redirect_to: str | None = None) -> dict:
    """Mints state + nonce + PKCE and returns the Google URL to send the
    browser to."""
    if not ENABLED:
        raise OAuthError("Google sign-in is not configured on this server.", status=501)

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    now = _now_ms()
    conn.execute(
        db.q("""INSERT INTO oauth_states (state, code_verifier, nonce, redirect_to,
                                          created_at, expires_at)
                VALUES (?,?,?,?,?,?)"""),
        (state, verifier, nonce, safe_redirect(redirect_to), now,
         now + STATE_TTL_SECONDS * 1000),
    )

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # Ask Google for a fresh account choice rather than silently
        # reusing whichever session the browser happens to hold. On a
        # shared lab machine, silent reuse signs the next student in as
        # the previous one.
        "prompt": "select_account",
    }
    return {"authorize_url": f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}",
            "state": state}


def _consume_state(conn, state: str) -> dict:
    row = conn.execute(
        db.q("SELECT * FROM oauth_states WHERE state = ?"), (state or "",)
    ).fetchone()
    if row is None:
        raise OAuthError("That sign-in link is no longer valid. Please try again.")
    row = dict(row)
    if row["consumed_at"] is not None or row["expires_at"] < _now_ms():
        raise OAuthError("That sign-in link is no longer valid. Please try again.")

    # Single use, claimed by rowcount — a replayed callback must not be
    # able to run the exchange twice.
    cursor = conn.execute(
        db.q("UPDATE oauth_states SET consumed_at = ? WHERE state = ? AND consumed_at IS NULL"),
        (_now_ms(), state),
    )
    if cursor.rowcount != 1:
        raise OAuthError("That sign-in link is no longer valid. Please try again.")
    return row


def _exchange_code(code: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request(
        TOKEN_ENDPOINT, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        # The body of a token-endpoint error can echo the client_id and
        # sometimes the redirect. Never returned to the caller.
        raise OAuthError("Google rejected that sign-in. Please try again.") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise OAuthError("Could not reach Google to complete sign-in.", status=503) from error


def _verify_id_token(id_token: str, nonce: str) -> dict:
    global _jwks_client
    if _jwks_client is None:
        # Caches keys between calls; Google rotates them and refetching per
        # login would be both slow and a dependency on their CDN in the
        # hot path.
        _jwks_client = PyJWKClient(JWKS_URI, cache_keys=True)

    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(id_token)
        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],       # never "none", never HS256 — an
                                        # algorithm the caller can choose is
                                        # how a public key becomes an HMAC
                                        # secret and every token verifies.
            audience=CLIENT_ID,
            issuer=list(ISSUERS),
            options={"require": ["exp", "iat", "aud", "iss", "sub"]},
        )
    except jwt.PyJWTError as error:
        raise OAuthError("Could not verify that Google sign-in.", status=401) from error
    except urllib.error.URLError as error:
        raise OAuthError("Could not reach Google to complete sign-in.", status=503) from error

    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OAuthError("Could not verify that Google sign-in.", status=401)
    return claims


def complete(conn, *, code: str, state: str) -> dict:
    """Finishes the callback. Returns the verified Google identity.

    Deliberately does NOT create users or sessions — that is `accounts`'
    job, and keeping it out of here means this module can be tested
    against a fake token without touching the user table.
    """
    if not ENABLED:
        raise OAuthError("Google sign-in is not configured on this server.", status=501)
    if not code:
        raise OAuthError("Google did not return an authorization code.")

    row = _consume_state(conn, state)
    tokens = _exchange_code(code, row["code_verifier"])
    id_token = tokens.get("id_token")
    if not id_token:
        raise OAuthError("Google did not return an identity token.")

    claims = _verify_id_token(id_token, row["nonce"])

    email = (claims.get("email") or "").strip().lower()
    verified = bool(claims.get("email_verified"))
    if not email:
        raise OAuthError("That Google account has no email address attached.")

    return {
        "subject": claims["sub"],
        "email": email,
        "email_verified": verified,
        "display_name": claims.get("name") or None,
        "redirect_to": row.get("redirect_to"),
    }


# ---------------------------------------------------------------------------
# Identity records
# ---------------------------------------------------------------------------

def find_identity(conn, *, provider: str, subject: str):
    row = conn.execute(
        db.q("SELECT * FROM identities WHERE provider = ? AND subject = ?"),
        (provider, subject),
    ).fetchone()
    return dict(row) if row else None


def link_identity(conn, *, user_id: str, provider: str, subject: str,
                  email: str | None) -> dict:
    existing = find_identity(conn, provider=provider, subject=subject)
    if existing:
        if existing["user_id"] != user_id:
            # One Google account, one local account. Re-pointing it would
            # be a way to move an identity between accounts without the
            # losing account's consent.
            raise OAuthError("That Google account is already linked to a different account.",
                             status=409)
        return existing

    identity_id = str(uuid.uuid4())
    conn.execute(
        db.q("""INSERT INTO identities (identity_id, user_id, provider, subject, email, created_at)
                VALUES (?,?,?,?,?,?)"""),
        (identity_id, user_id, provider, subject, email, _now_ms()),
    )
    return find_identity(conn, provider=provider, subject=subject)


def list_identities(conn, user_id: str) -> list:
    rows = conn.execute(
        db.q("SELECT provider, email, created_at FROM identities WHERE user_id = ?"),
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def unlink(conn, *, user_id: str, provider: str, has_password: bool) -> bool:
    """Removes a linked provider.

    Refuses if it is the account's only way in. Otherwise "unlink Google"
    on a password-less account is a self-inflicted permanent lockout, and
    the user finds out after it is too late to undo.
    """
    if not has_password and len(list_identities(conn, user_id)) <= 1:
        raise OAuthError(
            "Set a password before unlinking Google — otherwise you'd have no way "
            "to sign in.", status=409)
    cursor = conn.execute(
        db.q("DELETE FROM identities WHERE user_id = ? AND provider = ?"), (user_id, provider))
    return cursor.rowcount > 0
