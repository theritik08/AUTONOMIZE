"""The settings and profile endpoints, over real HTTP.

These are the endpoints a dashboard's settings panel and profile menu call,
so what is asserted here is the contract a frontend can rely on — including
the authorization boundary, which is the part that is easy to get wrong and
invisible when it is wrong.
"""
import pytest
from fastapi.testclient import TestClient

import db
import main
import ratelimit
import settings_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "settings-api-test.db")
    ratelimit.reset()
    main.reset_rate_limits()
    with TestClient(main.app) as test_client:
        yield test_client


def device(client):
    response = client.post("/api/auth/device")
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user"]["user_id"], {"Authorization": f"Bearer {body['access_token']}"}


def account(client, email="student@example.com"):
    response = client.post("/api/auth/register", json={
        "email": email, "password": "a-long-enough-password", "display_name": "Ada"})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["user"], {"Authorization": f"Bearer {body['access_token']}"}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def test_a_new_user_gets_defaults_rather_than_a_404(client):
    """A settings panel must not have to special-case "never saved"."""
    _uid, headers = device(client)
    response = client.get("/api/me/settings", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["settings"] == settings_store.DEFAULTS
    assert body["updated_at"] is None


def test_settings_require_authentication(client):
    assert client.get("/api/me/settings").status_code == 401


def test_one_users_settings_are_not_visible_to_another(client):
    """The IDOR check. `user_id` in the query string is advisory — identity
    comes from the bearer token — so asking for someone else's must return
    the caller's own, never theirs."""
    _a_id, a_headers = device(client)
    b_id, b_headers = device(client)

    client.put("/api/me/settings", json={"excludedDomains": ["b-private.com"]},
               headers=b_headers)
    leaked = client.get(f"/api/me/settings?user_id={b_id}", headers=a_headers)

    assert leaked.status_code == 200
    assert leaked.json()["settings"]["excludedDomains"] == []


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_a_partial_update_saves_and_returns_the_merged_result(client):
    _uid, headers = device(client)
    response = client.put("/api/me/settings",
                          json={"tracking": {"writing": False}}, headers=headers)
    assert response.status_code == 200
    settings = response.json()["settings"]
    assert settings["tracking"] == {"ai_assistant": True, "writing": False,
                                    "assessment": True}
    # Untouched keys survive.
    assert settings["backendUrl"] == settings_store.DEFAULTS["backendUrl"]


def test_the_response_carries_the_normalised_form_not_the_input(client):
    """A UI that kept its own copy would show the user `https://Example.com/`
    while the extension matches `example.com` — an exclusion that looks
    applied and is not."""
    _uid, headers = device(client)
    response = client.put("/api/me/settings",
                          json={"excludedDomains": ["https://Example.com/path"]},
                          headers=headers)
    assert response.json()["settings"]["excludedDomains"] == ["example.com"]


def test_an_update_is_visible_on_the_next_read(client):
    """The whole point: the extension polls this and must see the change."""
    _uid, headers = device(client)
    client.put("/api/me/settings", json={"excludedDomains": ["private.com"]},
               headers=headers)
    settings = client.get("/api/me/settings", headers=headers).json()["settings"]
    assert settings["excludedDomains"] == ["private.com"]


def test_the_timestamp_advances_so_the_extension_can_order_writes(client):
    _uid, headers = device(client)
    first = client.put("/api/me/settings", json={"tracking": {"writing": False}},
                       headers=headers).json()
    second = client.put("/api/me/settings", json={"tracking": {"writing": True}},
                        headers=headers).json()
    assert second["updated_at"] >= first["updated_at"]


@pytest.mark.parametrize("bad", [
    {"backendUrl": "ftp://example.com"},
    {"tracking": "on"},
    {"excludedDomains": "example.com"},
])
def test_invalid_settings_are_rejected_with_a_readable_message(client, bad):
    _uid, headers = device(client)
    response = client.put("/api/me/settings", json=bad, headers=headers)
    assert response.status_code in (400, 422)
    if response.status_code == 400:
        assert response.json()["detail"]


# ---------------------------------------------------------------------------
# Profile — what the avatar menu edits
# ---------------------------------------------------------------------------

def test_a_signed_in_user_can_change_their_display_name(client):
    _user, headers = account(client)
    response = client.patch("/api/me/profile", json={"display_name": "Ada Lovelace"},
                            headers=headers)
    assert response.status_code == 200
    assert response.json()["user"]["display_name"] == "Ada Lovelace"
    # And it persists.
    assert client.get("/api/auth/me",
                      headers=headers).json()["user"]["display_name"] == "Ada Lovelace"


def test_an_empty_display_name_is_refused(client):
    _user, headers = account(client)
    assert client.patch("/api/me/profile", json={"display_name": "   "},
                        headers=headers).status_code == 400


def test_an_anonymous_device_may_still_name_itself(client):
    """Deliberately permitted. A device row is a real user row, a display
    name grants nothing, and it is visible only to whoever holds that
    session — so "name this browser before you decide to register" works."""
    _uid, headers = device(client)
    response = client.patch("/api/me/profile", json={"display_name": "My laptop"},
                            headers=headers)
    assert response.status_code == 200
    assert response.json()["user"]["display_name"] == "My laptop"


def test_a_profile_edit_still_requires_a_session(client):
    """Permissive about WHICH identity, not about whether there is one."""
    assert client.patch("/api/me/profile",
                        json={"display_name": "Nobody"}).status_code == 401


def test_the_role_cannot_be_escalated_through_the_profile_endpoint(client):
    """The field is not in the model, so it is ignored rather than applied —
    asserted because "unknown fields are dropped" is a Pydantic default
    someone could change."""
    _user, headers = account(client, email="esc@example.com")
    body = client.patch("/api/me/profile",
                        json={"display_name": "Ada", "role": "admin"},
                        headers=headers).json()
    assert body["user"]["role"] != "admin"


def test_the_profile_response_never_leaks_a_password_hash(client):
    """public_user allow-lists rather than redacts; this is what keeps that
    true for the new endpoint too."""
    _user, headers = account(client)
    body = client.patch("/api/me/profile", json={"display_name": "Ada"},
                        headers=headers).json()
    # Checked against the KEYS, not a substring of the whole blob: the
    # legitimate value `provider: "password"` contains the word and is not
    # a leak, and a test that cannot tell those apart is a test that gets
    # deleted the first time it cries wolf.
    keys = set(body["user"])
    for leak in ("password", "password_hash", "hash", "salt", "token_hash",
                 "session_token", "reset_token"):
        assert leak not in keys
    assert keys == {"user_id", "email", "role", "display_name", "provider",
                    "email_verified", "has_password", "is_device_account"}
    # `has_password` is a boolean about EXISTENCE. It says nothing about
    # the password itself, and the UI genuinely needs it to choose between
    # "change password" and "set one".
    assert body["user"]["has_password"] is True


# ---------------------------------------------------------------------------
# The erase path
# ---------------------------------------------------------------------------

def test_erasing_data_also_erases_settings(client):
    _uid, headers = device(client)
    client.put("/api/me/settings", json={"excludedDomains": ["private.com"]},
               headers=headers)

    deleted = client.request("DELETE", "/api/me/data", headers=headers).json()
    assert deleted["deleted"]["user_settings"] == 1

    settings = client.get("/api/me/settings", headers=headers).json()["settings"]
    assert settings["excludedDomains"] == []


def test_exporting_data_includes_settings(client):
    _uid, headers = device(client)
    client.put("/api/me/settings", json={"excludedDomains": ["private.com"]},
               headers=headers)
    exported = client.get("/api/me/export", headers=headers).json()
    assert "user_settings" in exported["data"]
    assert "private.com" in str(exported["data"]["user_settings"])
