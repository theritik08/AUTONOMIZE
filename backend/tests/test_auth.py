"""Tests for auth.py's dual-mode identity resolution.

This is the anti-spoofing boundary for the whole app once Supabase Auth is
turned on, so it's tested directly against real signed JWTs (not just code
review) — matching how this was originally verified by hand with
Playwright during development (valid / expired / wrong-secret / no-token
cases), now captured as a repeatable suite.

auth.py reads SUPABASE_JWT_SECRET at *import time* into two module-level
values (SUPABASE_JWT_SECRET, AUTH_ENABLED), so tests that need "auth on"
monkeypatch both attributes directly on the imported module rather than
the environment variable (which auth is already past reading by the time
any test runs).
"""
import time

import jwt
import pytest
from fastapi import HTTPException

import auth

TEST_SECRET = "test-secret-do-not-use-in-prod-0123456789"  # >=32 bytes, quiets PyJWT's key-length warning


def make_token(secret=TEST_SECRET, sub="user-123", audience="authenticated", exp_delta=3600, include_sub=True):
    payload = {
        "aud": audience,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if include_sub:
        payload["sub"] = sub
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", TEST_SECRET)
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)


@pytest.fixture()
def auth_off(monkeypatch):
    monkeypatch.setattr(auth, "SUPABASE_JWT_SECRET", None)
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)


# ---------------------------------------------------------------------------
# Auth off (default / zero-config behavior)
# ---------------------------------------------------------------------------

def test_no_credential_is_rejected(auth_off):
    """The IDOR regression test.

    This used to return the caller's own claim verbatim, which meant
    `?user_id=<anyone>` was a complete authorization bypass on every
    telemetry route. A request with no credential must now be refused
    outright, whatever it claims to be.
    """
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id("anonymous-uuid-123", authorization=None)
    assert exc_info.value.status_code == 401


def test_missing_user_id_and_missing_credential_is_still_401(auth_off):
    # 401 rather than the old 400: the problem is the absent credential,
    # not the absent field. Saying "user_id is required" would invite a
    # client to supply one and expect it to work.
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=None)
    assert exc_info.value.status_code == 401


def test_a_garbage_bearer_token_is_rejected_not_ignored(auth_off):
    # Previously an unparseable token was simply ignored and the
    # client-supplied id used instead — so presenting a broken credential
    # was *safer* for an attacker than presenting none.
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id("client-id", authorization="Bearer garbage")
    assert exc_info.value.status_code == 401


def test_legacy_anonymous_mode_restores_the_old_behaviour(auth_off, monkeypatch):
    """The documented upgrade path, and the reason it is opt-in.

    Anyone with data collected before device accounts existed can set
    AUTONOMIZE_ALLOW_ANONYMOUS_IDS to keep reaching it. The flag reinstates
    the IDOR, which is exactly why it defaults off and warns at startup.
    """
    monkeypatch.setattr(auth, "ALLOW_ANONYMOUS_IDS", True)
    assert auth.resolve_user_id("legacy-uuid", authorization=None) == "legacy-uuid"

    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=None)
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Auth on — the anti-spoofing path
# ---------------------------------------------------------------------------

def test_auth_on_valid_token_returns_sub_claim(auth_on):
    token = make_token(sub="real-user-uuid")
    result = auth.resolve_user_id("someone-else-claims-to-be-this", authorization=f"Bearer {token}")
    # The critical anti-spoofing property: the token's sub wins, the
    # client-claimed user_id (however it got there) is fully ignored.
    assert result == "real-user-uuid"


def test_auth_on_missing_authorization_header_401s(auth_on):
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id("someone", authorization=None)
    assert exc_info.value.status_code == 401


def test_auth_on_non_bearer_authorization_401s(auth_on):
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id("someone", authorization="Basic dXNlcjpwYXNz")
    assert exc_info.value.status_code == 401


def test_auth_on_expired_token_401s(auth_on):
    token = make_token(exp_delta=-3600)
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


def test_auth_on_wrong_secret_401s(auth_on):
    token = make_token(secret="a-completely-different-secret-0123456789")
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


def test_auth_on_wrong_audience_401s(auth_on):
    token = make_token(audience="some-other-audience")
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


def test_auth_on_token_missing_sub_claim_401s(auth_on):
    token = make_token(include_sub=False)
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization=f"Bearer {token}")
    assert exc_info.value.status_code == 401


def test_auth_on_garbage_token_401s(auth_on):
    with pytest.raises(HTTPException) as exc_info:
        auth.resolve_user_id(None, authorization="Bearer not.a.jwt")
    assert exc_info.value.status_code == 401
