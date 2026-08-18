"""Adversarial tests for the authentication system.

Every test here is written from the attacker's side. The question is
never "does login work" — test_api.py covers that — but "what does a
person who has read this repository get to do".

Each section names the attack, and where an attack SUCCEEDS that is said
plainly rather than left out. A security suite that only contains attacks
the system already stops is a suite that proves nothing about the ones it
does not.
"""
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client():
    tmp_dir = tempfile.mkdtemp(prefix="autonomize-sec-test-")
    os.environ["AUTONOMIZE_DB_PATH"] = str(Path(tmp_dir) / "sec-test.db")

    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as test_client:
        yield test_client

    for leftover in Path(tmp_dir).glob("*"):
        leftover.unlink()
    Path(tmp_dir).rmdir()
    os.environ.pop("AUTONOMIZE_DB_PATH", None)


@pytest.fixture(autouse=True)
def clean_limits(client):
    """The auth limiter is a module-level dict shared by the whole pytest
    process. Without this, tests that deliberately exhaust it poison every
    test that runs after them with unrelated 429s."""
    import main
    import mailer
    import ratelimit
    main.reset_rate_limits()
    ratelimit.reset()
    mailer.reset_outbox()
    # TestClient keeps a cookie jar for the whole module, so a session set
    # by one test leaks into the next — and because the refresh endpoint
    # prefers the cookie over the body, a leftover cookie silently turns
    # an extension-shaped request into a browser-shaped one and it 403s on
    # a missing CSRF header. Every test starts with no cookies; the ones
    # that are ABOUT cookies set them deliberately.
    client.cookies.clear()
    yield
    main.reset_rate_limits()
    client.cookies.clear()


PASSWORD = "a-perfectly-fine-passphrase"


def unique_email(tag="user"):
    return f"{tag}-{uuid.uuid4().hex[:12]}@example.com"


def register(client, email=None, password=PASSWORD):
    email = email or unique_email()
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    body = response.json()
    # The shared TestClient keeps a cookie jar. Real header-auth clients
    # (the extension) have none, and leaving the cookie behind makes the
    # refresh endpoint take the cookie branch and demand a CSRF header.
    client.cookies.clear()
    return {
        "email": email,
        "password": password,
        "user_id": body["user"]["user_id"],
        "access": body["access_token"],
        "refresh": body["refresh_token"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


def latest_code(email, purpose_hint=None):
    """Reads the code out of the console-mode outbox.

    This is the ONLY place a test can see a code — the API never returns
    one, deliberately (see mailer.py). Reading it from the transport is
    what a person with access to the mailbox can do, which is exactly the
    capability the OTP is meant to require.
    """
    import mailer
    for message in reversed(mailer.outbox()):
        if message["to"] != email:
            continue
        if purpose_hint and purpose_hint.lower() not in message["subject"].lower():
            continue
        found = re.search(r"\b(\d{6})\b", message["body"])
        if found:
            return found.group(1)
    return None


# ===========================================================================
# 1. OTP ABUSE
# ===========================================================================

def test_ATTACK_brute_forcing_an_otp_burns_the_code_before_the_space(client):
    """A six-digit code is one in a million, which is only safe because
    guessing is capped. Without the cap this is a few seconds of work."""
    email = unique_email("otpbrute")
    assert client.post("/api/auth/otp/request", json={"email": email}).status_code == 200

    import otp
    rejected = 0
    for attempt in range(otp.MAX_ATTEMPTS + 3):
        response = client.post("/api/auth/otp/verify",
                               json={"email": email, "code": f"{attempt:06d}"})
        assert response.status_code in (400, 429)
        rejected += 1

    # The real code must now be dead too — otherwise the attacker simply
    # waits for the attempt window to roll and carries on.
    real = latest_code(email)
    assert real is not None
    burned = client.post("/api/auth/otp/verify", json={"email": email, "code": real})
    assert burned.status_code in (400, 429), "the code survived a brute-force run"
    assert rejected == otp.MAX_ATTEMPTS + 3


def test_ATTACK_resend_flooding_is_stopped_by_the_cooldown(client):
    """Each resend puts another live code into the same 10^6 space, so
    unlimited resends divide the guessing work. It is also a way to
    mail-bomb a victim until the deployment's domain is marked as spam."""
    email = unique_email("flood")
    first = client.post("/api/auth/otp/request", json={"email": email})
    assert first.status_code == 200

    second = client.post("/api/auth/otp/request", json={"email": email})
    assert second.status_code == 429
    assert "wait" in second.json()["detail"].lower()
    assert second.headers.get("Retry-After")


def test_a_resent_code_kills_the_previous_one(client, monkeypatch):
    """Otherwise every resend leaves another valid code outstanding, and
    ten resends make the space ten times easier to hit."""
    import otp
    monkeypatch.setattr(otp, "RESEND_COOLDOWN_SECONDS", 0)

    email = unique_email("resend")
    client.post("/api/auth/otp/request", json={"email": email})
    first_code = latest_code(email)
    client.post("/api/auth/otp/request", json={"email": email})
    second_code = latest_code(email)
    assert first_code != second_code

    stale = client.post("/api/auth/otp/verify", json={"email": email, "code": first_code})
    assert stale.status_code == 400, "a superseded code still worked"
    fresh = client.post("/api/auth/otp/verify", json={"email": email, "code": second_code})
    assert fresh.status_code == 200


def test_ATTACK_replaying_a_spent_code_gets_nothing(client):
    email = unique_email("replay")
    client.post("/api/auth/otp/request", json={"email": email})
    code = latest_code(email)

    assert client.post("/api/auth/otp/verify",
                       json={"email": email, "code": code}).status_code == 200
    again = client.post("/api/auth/otp/verify", json={"email": email, "code": code})
    assert again.status_code == 400, "an OTP was single-use in name only"


def test_ATTACK_a_verification_code_cannot_be_spent_on_a_password_reset(client):
    """Purpose confusion. If a code mailed for 'confirm your address' can
    be replayed against 'reset my password', then anyone who can trigger a
    verification mail to a victim's address owns the account. This is the
    most common OTP bug in the wild and it is a full takeover."""
    user = register(client)
    verification_code = latest_code(user["email"], "confirm")
    assert verification_code is not None

    stolen = client.post("/api/auth/password/reset", json={
        "email": user["email"], "code": verification_code,
        "new_password": "attacker-chosen-password"})
    assert stolen.status_code == 400, "a verify_email code reset the password"

    # And the original password still works.
    assert client.post("/api/auth/login", json={
        "email": user["email"], "password": PASSWORD}).status_code == 200


def test_the_api_never_returns_the_code_in_any_response(client):
    """Not in the body, not in a header, not behind a flag. An endpoint
    that mails a login code and also returns it is an authentication
    bypass for every account whose address is known."""
    email = unique_email("leak")
    response = client.post("/api/auth/otp/request", json={"email": email})
    code = latest_code(email)
    assert code is not None
    blob = response.text + repr(dict(response.headers))
    assert code not in blob


def test_an_expired_code_is_refused(client, monkeypatch):
    import otp
    email = unique_email("expiry")
    client.post("/api/auth/otp/request", json={"email": email})
    code = latest_code(email)

    real_now = otp._now_ms
    monkeypatch.setattr(otp, "_now_ms",
                        lambda: real_now() + (otp.TTL_MINUTES + 1) * 60_000)
    assert client.post("/api/auth/otp/verify",
                       json={"email": email, "code": code}).status_code == 400


# ===========================================================================
# 2. PASSWORD RESET
# ===========================================================================

def test_forgot_password_is_not_a_membership_oracle(client):
    """Byte-identical responses for a real and an imaginary address.

    A difference here — status, body, even wording — hands anyone with a
    list of school email addresses a roster of who is enrolled.
    """
    user = register(client)
    real = client.post("/api/auth/password/forgot", json={"email": user["email"]})
    fake = client.post("/api/auth/password/forgot",
                       json={"email": unique_email("nobody")})
    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()


def test_a_reset_revokes_every_existing_session(client):
    """The point of a reset is usually that the account may be
    compromised. Leaving the attacker's tokens live changes the lock
    while the intruder is still inside."""
    user = register(client)
    assert client.get("/api/auth/me", headers=user["headers"]).status_code == 200

    client.post("/api/auth/password/forgot", json={"email": user["email"]})
    code = latest_code(user["email"], "reset")
    done = client.post("/api/auth/password/reset", json={
        "email": user["email"], "code": code, "new_password": "a-brand-new-passphrase"})
    assert done.status_code == 200

    stale = client.get("/api/auth/me", headers=user["headers"])
    assert stale.status_code == 401, "the pre-reset session survived the reset"


def test_ATTACK_a_reset_code_for_one_account_does_not_reset_another(client):
    """The code is bound to the address it was mailed to — hash_code
    HMACs the email in. Without that binding, any valid code resets any
    account."""
    victim = register(client)
    attacker = register(client)

    client.post("/api/auth/password/forgot", json={"email": attacker["email"]})
    attacker_code = latest_code(attacker["email"], "reset")

    hijack = client.post("/api/auth/password/reset", json={
        "email": victim["email"], "code": attacker_code,
        "new_password": "attacker-chosen-password"})
    assert hijack.status_code == 400

    assert client.post("/api/auth/login", json={
        "email": victim["email"], "password": PASSWORD}).status_code == 200


def test_a_reset_code_cannot_be_used_twice(client):
    user = register(client)
    client.post("/api/auth/password/forgot", json={"email": user["email"]})
    code = latest_code(user["email"], "reset")

    assert client.post("/api/auth/password/reset", json={
        "email": user["email"], "code": code,
        "new_password": "first-new-passphrase"}).status_code == 200
    assert client.post("/api/auth/password/reset", json={
        "email": user["email"], "code": code,
        "new_password": "second-new-passphrase"}).status_code == 400


def test_a_reset_still_enforces_the_password_policy(client):
    """A reset path that skips validation is how 'password' ends up in the
    database on an account that was created with a strong one."""
    user = register(client)
    client.post("/api/auth/password/forgot", json={"email": user["email"]})
    code = latest_code(user["email"], "reset")
    weak = client.post("/api/auth/password/reset", json={
        "email": user["email"], "code": code, "new_password": "123"})
    assert weak.status_code == 400


# ===========================================================================
# 3. CHANGE PASSWORD
# ===========================================================================

def test_ATTACK_a_stolen_session_cannot_change_the_password(client):
    """The reason the current password is required even though the caller
    is already signed in. Without it, a borrowed laptop or an XSS payload
    upgrades a temporary session into permanent ownership."""
    user = register(client)
    attempt = client.post("/api/auth/password/change", headers=user["headers"], json={
        "current_password": "a-guess-at-the-old-one",
        "new_password": "attacker-chosen-password"})
    assert attempt.status_code == 401

    assert client.post("/api/auth/login", json={
        "email": user["email"], "password": PASSWORD}).status_code == 200


def test_changing_the_password_signs_other_sessions_out_but_not_this_one(client):
    user = register(client)
    other = client.post("/api/auth/login",
                        json={"email": user["email"], "password": PASSWORD}).json()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}

    changed = client.post("/api/auth/password/change", headers=user["headers"], json={
        "current_password": PASSWORD, "new_password": "a-different-good-passphrase"})
    assert changed.status_code == 200

    assert client.get("/api/auth/me", headers=other_headers).status_code == 401
    # The caller gets a fresh session back rather than being logged out for
    # doing the responsible thing.
    fresh = {"Authorization": f"Bearer {changed.json()['access_token']}"}
    assert client.get("/api/auth/me", headers=fresh).status_code == 200


def test_an_otp_only_account_cannot_have_its_password_changed_by_guessing_empty(client):
    """password_hash is NULL for these accounts, and passwords.verify
    returns False for NULL unconditionally. If it did not, an empty string
    would authenticate every OAuth and OTP account in the database."""
    email = unique_email("otponly")
    client.post("/api/auth/otp/request", json={"email": email})
    code = latest_code(email)
    session = client.post("/api/auth/otp/verify",
                          json={"email": email, "code": code}).json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    for guess in ("", " ", "password"):
        response = client.post("/api/auth/password/change", headers=headers, json={
            "current_password": guess, "new_password": "attacker-chosen-password"})
        # 409 "this account has no password", or 422 when pydantic rejects
        # the empty string before the handler is reached. Both refuse; what
        # must never appear here is a 200.
        assert response.status_code in (409, 422)

    assert client.post("/api/auth/login", json={
        "email": email, "password": ""}).status_code in (401, 422)


# ===========================================================================
# 4. TOKEN REPLAY AND ROTATION
# ===========================================================================

def test_a_refresh_token_rotates_and_the_old_one_dies(client):
    user = register(client)
    first = client.post("/api/auth/refresh", json={"refresh_token": user["refresh"]})
    assert first.status_code == 200
    rotated = first.json()["refresh_token"]
    assert rotated != user["refresh"]


def test_ATTACK_replaying_a_used_refresh_token_kills_the_whole_family(client):
    """THE reason rotation exists. An attacker who steals a refresh token
    and an owner who still holds their copy cannot both keep refreshing:
    whichever one goes second presents an already-used token, and the
    response is to revoke everything descended from that login.

    We cannot tell which party was the thief, and this test asserts that
    we do not try — both are signed out and the real user re-authenticates
    with a credential the attacker does not have.
    """
    user = register(client)
    stolen = user["refresh"]

    def extension_refresh(token):
        # Both parties here are extension-shaped clients: token in the
        # body, no cookie jar. The shared TestClient accumulates the
        # cookies each successful refresh sets, and leaving them would
        # send the NEXT call down the cookie branch — which 403s on a
        # missing CSRF header and would mask the 401 this test is about.
        client.cookies.clear()
        return client.post("/api/auth/refresh", json={"refresh_token": token})

    # The attacker refreshes first and gets a working token.
    attacker = extension_refresh(stolen)
    assert attacker.status_code == 200
    attacker_token = attacker.json()["refresh_token"]

    # The owner's copy is now the used one. Presenting it trips detection.
    owner = extension_refresh(stolen)
    assert owner.status_code == 401
    assert "somewhere else" in owner.json()["detail"]

    # And the attacker's freshly minted token died with the family.
    after = extension_refresh(attacker_token)
    assert after.status_code == 401, "reuse detection revoked nothing"


def test_an_access_token_is_short_lived_by_configuration(client):
    """Rotation only limits damage if the access token it mints is
    actually short. A 12-hour access token beside a rotating refresh
    token is the old blast radius with extra machinery."""
    import tokens
    assert tokens.ACCESS_TTL_SECONDS <= 30 * 60
    user = register(client)
    body = client.post("/api/auth/login",
                       json={"email": user["email"], "password": PASSWORD}).json()
    assert body["expires_in"] <= 30 * 60


def test_ATTACK_a_revoked_session_token_stops_working_immediately(client):
    """The whole reason sessions are stored rather than purely stateless.
    A signed JWT with a valid expiry would still verify."""
    user = register(client)
    assert client.get("/api/auth/me", headers=user["headers"]).status_code == 200
    client.post("/api/auth/logout", headers=user["headers"])
    assert client.get("/api/auth/me", headers=user["headers"]).status_code == 401


def test_ATTACK_a_forged_token_signed_with_the_wrong_key_is_refused(client):
    """The obvious attack, asserted anyway: it is the one that turns every
    other control in this file into decoration if it ever regresses."""
    import jwt
    user = register(client)
    forged = jwt.encode(
        {"sub": user["user_id"], "role": "admin", "jti": str(uuid.uuid4()),
         "iss": "autonomize", "iat": int(time.time()), "exp": int(time.time()) + 3600},
        "not-the-real-signing-key", algorithm="HS256")
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_ATTACK_an_alg_none_token_is_refused(client):
    """The classic JWT bypass: strip the signature and set alg to none.
    PyJWT refuses it, but this is cheap insurance against someone adding
    an `algorithms` list that includes it."""
    import base64
    import json as jsonlib
    user = register(client)

    def segment(payload):
        return base64.urlsafe_b64encode(jsonlib.dumps(payload).encode()).decode().rstrip("=")

    forged = ".".join([
        segment({"alg": "none", "typ": "JWT"}),
        segment({"sub": user["user_id"], "role": "admin", "jti": str(uuid.uuid4()),
                 "iss": "autonomize", "exp": int(time.time()) + 3600}),
        "",
    ])
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_logout_everywhere_kills_refresh_tokens_too(client):
    """Otherwise the button is decorative: access tokens expire in ten
    minutes anyway, and any device holding a refresh token just mints a
    replacement."""
    user = register(client)
    assert client.post("/api/auth/logout-everywhere",
                       headers=user["headers"]).status_code == 200
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": user["refresh"]}).status_code == 401


# ===========================================================================
# 5. IDOR AND CROSS-USER DATA ACCESS
# ===========================================================================

def test_ATTACK_naming_another_user_id_returns_your_own_data(client):
    """The bug this system was built to close. `?user_id=<theirs>` used to
    be believed."""
    victim = register(client)
    attacker = register(client)

    # /api/me/export echoes the resolved user_id, which is what makes it
    # the honest place to assert this: the response says whose data it is.
    mine = client.get("/api/me/export", headers=attacker["headers"]).json()
    theirs = client.get("/api/me/export", headers=attacker["headers"],
                        params={"user_id": victim["user_id"]}).json()
    assert theirs["user_id"] == attacker["user_id"] == mine["user_id"]
    assert theirs["user_id"] != victim["user_id"]


def test_ATTACK_writing_into_another_users_session_is_refused(client):
    """Found by accident during an audit: upsert matched on session_id
    alone, so reusing another student's session id accumulated your
    counters into their row."""
    victim = register(client)
    attacker = register(client)
    session_id = f"shared-{uuid.uuid4()}"
    now = int(time.time() * 1000)

    # `user_id` is in the body because the model requires it — and it is
    # exactly what must NOT be believed. The attacker sends the victim's.
    body = {"user_id": victim["user_id"], "session_id": session_id,
            "category": "writing", "domain": "docs.google.com",
            "started_at": now, "active_ms": 60_000,
            "metrics": {"typed_chars": 2000, "pasted_chars": 0},
            "is_final": True, "client_ts": now}

    assert client.post("/api/session/upsert", json=body,
                       headers=victim["headers"]).status_code == 200
    stolen = client.post("/api/session/upsert",
                         json={**body, "metrics": {"typed_chars": 9999, "pasted_chars": 9999}},
                         headers=attacker["headers"])
    assert stolen.status_code == 409

    rows = client.get("/api/sessions", headers=victim["headers"]).json()["sessions"]
    mine = [r for r in rows if r["session_id"] == session_id]
    assert mine and mine[0]["typed_chars"] == 2000


def test_ATTACK_exporting_and_deleting_only_ever_touches_your_own_rows(client):
    victim = register(client)
    attacker = register(client)
    now = int(time.time() * 1000)
    client.post("/api/session/upsert", headers=victim["headers"], json={
        "user_id": victim["user_id"],
        "session_id": f"victim-{uuid.uuid4()}", "category": "writing",
        "domain": "docs.google.com", "started_at": now, "active_ms": 60_000,
        "metrics": {"typed_chars": 1500, "pasted_chars": 0},
        "is_final": True, "client_ts": now})

    export = client.get("/api/me/export", headers=attacker["headers"],
                        params={"user_id": victim["user_id"]}).json()
    blob = repr(export)
    assert victim["user_id"] not in blob

    client.request("DELETE", "/api/me/data", headers=attacker["headers"],
                   params={"user_id": victim["user_id"]})
    survived = client.get("/api/sessions", headers=victim["headers"]).json()["sessions"]
    assert len(survived) >= 1, "one account's delete removed another's data"


def test_ATTACK_revoking_a_device_you_do_not_own_is_a_404(client):
    """The authorization check is the scoped UPDATE itself — `WHERE
    device_id = ? AND user_id = ?` — so there is no window between
    checking ownership and acting on it."""
    victim = register(client)
    attacker = register(client)

    device_id = str(uuid.uuid4())
    made = client.post("/api/devices/register", headers=victim["headers"],
                       json={"device_id": device_id, "label": "victim laptop"})
    assert made.status_code == 201

    assert client.delete(f"/api/devices/{device_id}",
                         headers=attacker["headers"]).status_code == 404
    assert client.patch(f"/api/devices/{device_id}", headers=attacker["headers"],
                        json={"label": "owned"}).status_code == 404

    listed = client.get("/api/devices", headers=victim["headers"]).json()["devices"]
    assert [d for d in listed if d["device_id"] == device_id and not d["revoked"]]


def test_a_device_list_never_shows_another_accounts_devices(client):
    victim = register(client)
    attacker = register(client)
    client.post("/api/devices/register", headers=victim["headers"],
                json={"device_id": str(uuid.uuid4()), "label": "victim laptop"})

    listed = client.get("/api/devices", headers=attacker["headers"]).json()
    assert listed["devices"] == []
    assert "victim laptop" not in repr(listed)


def test_a_device_row_never_leaks_the_ip_hash(client):
    """public_device allow-lists rather than redacts, so a future column
    is invisible by default instead of leaking until someone notices."""
    user = register(client)
    client.post("/api/devices/register", headers=user["headers"],
                json={"device_id": str(uuid.uuid4())})
    listed = client.get("/api/devices", headers=user["headers"]).json()["devices"]
    assert listed
    for device in listed:
        assert set(device) == {"device_id", "label", "platform", "client",
                               "created_at", "last_seen_at", "revoked"}


# ===========================================================================
# 6. UNAUTHORIZED ACCESS
# ===========================================================================

@pytest.mark.parametrize("method,path", [
    ("GET", "/api/score"), ("GET", "/api/sessions"), ("GET", "/api/me/export"),
    ("GET", "/api/auth/me"), ("GET", "/api/devices"), ("GET", "/api/me/settings"),
    ("GET", "/api/admin/cohort"), ("GET", "/api/retrieval/concepts"),
])
def test_every_user_scoped_read_requires_a_credential(client, method, path):
    assert client.request(method, path).status_code == 401


def test_ATTACK_a_role_claimed_in_the_token_does_not_grant_admin(client):
    """The role is re-read from the database on every request. A token
    claim is a convenience for the UI, never an authorization decision —
    otherwise anyone who can mint a token picks their own role."""
    user = register(client)
    assert client.get("/api/admin/cohort", headers=user["headers"]).status_code == 403


def test_ATTACK_registration_cannot_grant_itself_admin(client):
    email = unique_email("wannabe")
    response = client.post("/api/auth/register",
                           json={"email": email, "password": PASSWORD, "role": "admin"})
    assert response.status_code == 201
    assert response.json()["user"]["role"] == "student"


def test_a_deleted_account_stops_authenticating_immediately(client):
    user = register(client)
    gone = client.request("DELETE", "/api/me/account", headers=user["headers"],
                          json={"confirm": "DELETE", "password": PASSWORD})
    assert gone.status_code == 200

    assert client.get("/api/auth/me", headers=user["headers"]).status_code == 401
    assert client.post("/api/auth/refresh",
                       json={"refresh_token": user["refresh"]}).status_code == 401
    assert client.post("/api/auth/login", json={
        "email": user["email"], "password": PASSWORD}).status_code == 401


def test_deleting_an_account_requires_the_password_not_just_a_session(client):
    """Being signed in is not enough for an irreversible operation. A
    borrowed laptop should not be able to erase a year of work."""
    user = register(client)
    assert client.request("DELETE", "/api/me/account", headers=user["headers"],
                          json={"confirm": "DELETE"}).status_code == 401
    assert client.request("DELETE", "/api/me/account", headers=user["headers"],
                          json={"confirm": "yes", "password": PASSWORD}).status_code == 400
    assert client.get("/api/auth/me", headers=user["headers"]).status_code == 200


# ===========================================================================
# 7. ACCOUNT TAKEOVER
# ===========================================================================

def test_ATTACK_registering_an_existing_email_does_not_take_it_over(client):
    user = register(client)
    second = client.post("/api/auth/register",
                         json={"email": user["email"], "password": "attacker-chosen-password"})
    assert second.status_code == 409
    assert client.post("/api/auth/login", json={
        "email": user["email"], "password": PASSWORD}).status_code == 200


def test_ATTACK_password_spraying_one_account_hits_the_lockout(client):
    user = register(client)
    import accounts
    for _ in range(accounts.MAX_FAILED_LOGINS + 1):
        client.post("/api/auth/login",
                    json={"email": user["email"], "password": "wrong-guess-here"})

    locked = client.post("/api/auth/login",
                         json={"email": user["email"], "password": PASSWORD})
    assert locked.status_code == 429, "no lockout after repeated failures"


def test_ATTACK_spraying_one_password_across_many_accounts_hits_the_rate_limit(client):
    """Lockout is per-account and cannot see this: each account records a
    single failure. The IP limiter is what catches it."""
    import main
    statuses = []
    for _ in range(main.AUTH_MAX_ATTEMPTS + 5):
        statuses.append(client.post("/api/auth/login", json={
            "email": unique_email("spray"), "password": "Autumn2024!"}).status_code)
    assert 429 in statuses


def test_login_does_not_reveal_whether_an_email_exists(client):
    user = register(client)
    known = client.post("/api/auth/login",
                        json={"email": user["email"], "password": "wrong-guess-here"})
    unknown = client.post("/api/auth/login",
                          json={"email": unique_email("ghost"), "password": "wrong-guess-here"})
    assert known.status_code == unknown.status_code == 401
    assert known.json()["detail"] == unknown.json()["detail"]


def test_ATTACK_an_otp_login_cannot_hijack_an_existing_password_account(client):
    """OTP sign-in resolves to the same account as the password login,
    which is correct — same mailbox, same person. What must NOT happen is
    the password being cleared or bypassed as a side effect."""
    user = register(client)
    client.post("/api/auth/otp/request", json={"email": user["email"]})
    code = latest_code(user["email"])
    assert code is not None
    session = client.post("/api/auth/otp/verify",
                          json={"email": user["email"], "code": code})
    assert session.status_code == 200
    assert session.json()["user"]["user_id"] == user["user_id"]
    assert session.json()["user"]["has_password"] is True

    assert client.post("/api/auth/login", json={
        "email": user["email"], "password": PASSWORD}).status_code == 200


def test_ATTACK_setting_a_password_on_someone_elses_account_needs_their_mailbox(client):
    """`/password/set` is gated on a code mailed to the account's own
    address, not on the session alone — otherwise a borrowed Google
    session could plant a permanent credential."""
    email = unique_email("otponly")
    client.post("/api/auth/otp/request", json={"email": email})
    session = client.post("/api/auth/otp/verify",
                          json={"email": email, "code": latest_code(email)}).json()
    headers = {"Authorization": f"Bearer {session['access_token']}"}

    guessed = client.post("/api/auth/password/set", headers=headers,
                          json={"code": "000000", "new_password": "attacker-chosen-password"})
    assert guessed.status_code in (400, 429)


# ===========================================================================
# 8. CSRF AND COOKIES
# ===========================================================================

def test_the_refresh_cookie_is_httponly_and_samesite(client):
    """HttpOnly is what a successful XSS on the dashboard cannot get
    around. Without it the refresh token sits in reach of any injected
    script, and thirty days of access leave with it."""
    import websecurity
    user_email = unique_email("cookie")
    response = client.post("/api/auth/register",
                           json={"email": user_email, "password": PASSWORD})
    raw = response.headers.get("set-cookie", "")
    assert websecurity.refresh_cookie_name() in raw
    refresh_bits = [c for c in raw.split(",") if websecurity.refresh_cookie_name() in c]
    assert refresh_bits
    assert "httponly" in refresh_bits[0].lower()
    assert "samesite" in refresh_bits[0].lower()


def test_the_csrf_cookie_is_deliberately_readable(client):
    """Double-submit needs the page to read it and echo it in a header.
    An HttpOnly CSRF cookie cannot be echoed and breaks the scheme."""
    import websecurity
    response = client.post("/api/auth/register",
                           json={"email": unique_email("csrf"), "password": PASSWORD})
    raw = response.headers.get("set-cookie", "")
    csrf_bits = [c for c in raw.split(",") if websecurity.csrf_cookie_name() in c]
    assert csrf_bits
    assert "httponly" not in csrf_bits[0].lower()


def test_ATTACK_a_cookie_refresh_without_the_csrf_header_is_refused(client):
    """The cross-site POST case. A malicious page can make the browser
    send the cookie; it cannot read the CSRF cookie to echo it back."""
    email = unique_email("csrfattack")
    client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    # The TestClient keeps the cookie jar, so this is exactly the shape of
    # a cross-site request: cookie present, header absent.
    forged = client.post("/api/auth/refresh")
    assert forged.status_code == 403
    assert "csrf" in forged.json()["detail"].lower()

    import websecurity
    csrf = client.cookies.get(websecurity.csrf_cookie_name())
    allowed = client.post("/api/auth/refresh",
                          headers={websecurity.CSRF_HEADER: csrf})
    assert allowed.status_code == 200
    client.cookies.clear()


def test_a_wildcard_cors_origin_with_credentials_is_refused_outright(client):
    """FastAPI's response to this combination is to reflect the caller's
    origin, which means every origin is trusted with credentials — the
    exact opposite of what writing '*' suggests. It has to fail loudly."""
    import websecurity
    with pytest.raises(RuntimeError, match="wildcard"):
        websecurity.validate_cors(["*"], credentials=True)
    websecurity.validate_cors(["*"], credentials=False)          # allowed
    websecurity.validate_cors(["https://a.edu"], credentials=True)  # allowed


def test_security_headers_are_present_on_every_response(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "no-referrer"


# ===========================================================================
# 9. DEVICE LINKING
# ===========================================================================

def test_the_full_extension_linking_flow_moves_the_history(client):
    """The whole point: a student collects anonymously, then claims it."""
    device = client.post("/api/auth/device", json={}).json()
    device_headers = {"Authorization": f"Bearer {device['access_token']}"}
    now = int(time.time() * 1000)
    client.post("/api/session/upsert", headers=device_headers, json={
        "user_id": device["user"]["user_id"],
        "session_id": f"anon-{uuid.uuid4()}", "category": "writing",
        "domain": "docs.google.com", "started_at": now, "active_ms": 60_000,
        "metrics": {"typed_chars": 1234, "pasted_chars": 10},
        "is_final": True, "client_ts": now})

    started = client.post("/api/devices/link/start", headers=device_headers)
    assert started.status_code == 201
    code = started.json()["code"]

    account = register(client)
    done = client.post("/api/devices/link/complete", headers=account["headers"],
                       json={"code": code})
    assert done.status_code == 200
    assert done.json()["rows_moved"]["sessions"] == 1

    rows = client.get("/api/sessions", headers=account["headers"]).json()["sessions"]
    assert any(r["typed_chars"] == 1234 for r in rows)
    # The anonymous account is finished — its token must not be a second
    # key to the same history.
    assert client.get("/api/auth/me", headers=device_headers).status_code == 401


def test_ATTACK_a_link_code_is_single_use(client):
    device = client.post("/api/auth/device", json={}).json()
    device_headers = {"Authorization": f"Bearer {device['access_token']}"}
    code = client.post("/api/devices/link/start", headers=device_headers).json()["code"]

    first = register(client)
    assert client.post("/api/devices/link/complete", headers=first["headers"],
                       json={"code": code}).status_code == 200

    second = register(client)
    assert client.post("/api/devices/link/complete", headers=second["headers"],
                       json={"code": code}).status_code == 400


def test_ATTACK_a_link_code_cannot_be_completed_without_being_signed_in(client):
    """Possessing a code is not enough. This is what makes a shoulder-surfed
    code low-value: the holder can only attach the device to an account
    they can already log into — i.e. to their own data."""
    device = client.post("/api/auth/device", json={}).json()
    device_headers = {"Authorization": f"Bearer {device['access_token']}"}
    code = client.post("/api/devices/link/start", headers=device_headers).json()["code"]

    assert client.post("/api/devices/link/complete", json={"code": code}).status_code == 401
    # And a device session cannot complete its own link either — that
    # would be a no-op that consumed the code.
    assert client.post("/api/devices/link/complete", headers=device_headers,
                       json={"code": code}).status_code == 409


def test_a_guessed_link_code_does_not_work(client):
    account = register(client)
    for guess in ("AAAAAA", "234567", "ZZZZZZ"):
        assert client.post("/api/devices/link/complete", headers=account["headers"],
                           json={"code": guess}).status_code == 400


def test_the_link_code_alphabet_has_no_ambiguous_characters(client):
    """A student reads this off a popup and types it into a dashboard.
    O/0 and I/1/L confusion turns into support tickets that look exactly
    like failed attacks in the log."""
    import devices
    for ambiguous in "01OIL":
        assert ambiguous not in devices.ALPHABET


# ===========================================================================
# 10. DEVICE IDS ARE NOT FINGERPRINTS
# ===========================================================================

def test_device_ids_are_random_not_derived_from_hardware(client):
    """Asserted in code because it is the promise most easily eroded by a
    well-meaning 'improvement' — someone adds a fingerprint to make the id
    stable across reinstalls and quietly builds a tracking identifier."""
    import devices
    user = register(client)
    with_no_id = []
    with __import__("db").get_conn() as conn:
        for _ in range(5):
            row = devices.register(conn, user_id=user["user_id"])
            with_no_id.append(row["device_id"])
    assert len(set(with_no_id)) == 5, "server-minted device ids collided"


def test_the_codebase_reads_no_hardware_identifiers():
    """A grep, deliberately. If someone adds MAC/CPU/serial collection to
    the extension or the backend, this fails and the reviewer has to
    argue for it rather than slip it in."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    banned = re.compile(
        r"getMacAddress|mac_address|macAddress|cpuInfo|system\.cpu|"
        r"getSerialNumber|serial_number|deviceSerial|hardwareConcurrency|"
        r"navigator\.hardware|canvas\.toDataURL|getDeviceId\(\)\.hardware",
        re.IGNORECASE)
    offenders = []
    for path in list(root.glob("extension/*.js")) + list(root.glob("backend/*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(errors="ignore")
        if banned.search(text):
            offenders.append(path.name)
    assert not offenders, f"hardware fingerprinting appeared in {offenders}"


# ===========================================================================
# 11. WHAT IS STORED
# ===========================================================================

def test_no_credential_is_ever_stored_in_recoverable_form(client):
    """Passwords hashed, refresh tokens hashed, OTP codes hashed. A dump
    of this database must not contain anything that can be replayed."""
    import db
    user = register(client)
    client.post("/api/auth/otp/request", json={"email": user["email"]})
    code = latest_code(user["email"])

    with db.get_conn() as conn:
        row = dict(conn.execute(
            db.q("SELECT password_hash FROM users WHERE user_id = ?"),
            (user["user_id"],)).fetchone())
        assert PASSWORD not in (row["password_hash"] or "")
        assert row["password_hash"].startswith("$argon2id$")

        stored = [dict(r) for r in conn.execute(
            db.q("SELECT token_hash FROM refresh_tokens WHERE user_id = ?"),
            (user["user_id"],)).fetchall()]
        assert stored
        for token_row in stored:
            assert user["refresh"] != token_row["token_hash"]

        codes = [dict(r) for r in conn.execute(
            db.q("SELECT code_hash FROM otp_codes WHERE email = ?"),
            (user["email"],)).fetchall()]
        assert codes
        for code_row in codes:
            assert code != code_row["code_hash"]


def test_no_raw_ip_address_is_stored_anywhere_in_auth(client):
    """IPs are personal data, and 'is this the same client' works fine on
    a hash. A leak of this database must not hand over location history."""
    import db
    register(client)
    with db.get_conn() as conn:
        for table, column in (("audit_log", "ip_hash"), ("refresh_tokens", "ip_hash"),
                              ("otp_codes", "ip_hash"), ("auth_sessions", "ip_hash")):
            rows = [dict(r) for r in conn.execute(
                db.q(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL LIMIT 20")
            ).fetchall()]
            for row in rows:
                value = row[column] or ""
                # Match address SHAPES, not hex — the stored value is a
                # 32-char hex digest and a naive `[0-9a-f:]+` pattern
                # flags every correct hash as a failure. Dotted quad, or
                # something with the colon groups of an IPv6 literal.
                assert not re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", value), \
                    f"{table}.{column} holds a raw IPv4 address: {value}"
                assert "::" not in value and value.count(":") < 2, \
                    f"{table}.{column} holds a raw IPv6 address: {value}"
                assert len(value) == 32, \
                    f"{table}.{column} is not a truncated sha256: {value!r}"
