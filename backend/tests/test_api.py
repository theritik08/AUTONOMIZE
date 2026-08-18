"""HTTP-level tests: the FastAPI app driven through real requests.

These run against a temporary SQLite file (via AUTONOMIZE_DB_PATH) rather
than mocking the database, so routing, pydantic validation, the lifespan
hook, migrations, and the query layer are all exercised together — the
combination is where integration bugs actually live.
"""
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def client():
    # db.py resolves DB_PATH at import time, so the env var has to be set
    # before the app (and therefore db) is first imported.
    tmp_dir = tempfile.mkdtemp(prefix="autonomize-api-test-")
    os.environ["AUTONOMIZE_DB_PATH"] = str(Path(tmp_dir) / "api-test.db")

    from fastapi.testclient import TestClient

    import db
    import main

    # TestClient as a context manager is what runs the lifespan hook —
    # without it the pool is never opened and migrations never applied.
    with TestClient(main.app) as test_client:
        yield test_client

    for leftover in Path(tmp_dir).glob("*"):
        leftover.unlink()
    Path(tmp_dir).rmdir()
    os.environ.pop("AUTONOMIZE_DB_PATH", None)


class Identity(str):
    """A registered user id that also carries its own credentials.

    Subclasses `str` so every existing `{"user_id": user}` body and
    `params={"user_id": user}` query keeps working unchanged, while
    `user.headers` supplies the bearer token the API now requires.

    The tests deliberately still send `user_id` even though the server
    ignores it — that is what proves it is ignored rather than merely
    unused.
    """

    headers: dict

    def __new__(cls, user_id, token):
        obj = super().__new__(cls, user_id)
        obj.headers = {"Authorization": f"Bearer {token}"}
        return obj


def new_user(client=None):
    """Registers a real device account and returns its identity.

    Telemetry endpoints no longer accept a client-invented id, so a test
    user has to be minted by the server the same way the extension mints
    one on first install. `client` is optional only so the signature stays
    compatible with call sites that predate this.
    """
    if client is None:
        raise TypeError("new_user(client) now needs the client fixture to register a device")
    resp = client.post("/api/auth/device")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return Identity(body["user"]["user_id"], body["access_token"])


def upsert(client, user_id, session_id, category="writing", started_at=None, **metrics):
    # Relative to now, not a fixed epoch: /api/score's rollups are windowed
    # (7 and 14 days), so a hardcoded timestamp silently falls out of range
    # as the calendar moves and the test starts asserting zeros.
    if started_at is None:
        started_at = int(time.time() * 1000) - 60 * 60 * 1000  # an hour ago
    body = {
        "user_id": user_id,
        "session_id": session_id,
        "category": category,
        "domain": "docs.google.com",
        "path": "/d/x",
        "started_at": started_at,
        "active_ms": 25 * 60_000,
        "metrics": {
            "typed_chars": 0, "pasted_chars": 0, "backspace_count": 0,
            "revision_count": 0, "prompt_count": 0, "likely_ai_pastes": 0,
            "tab_switch_count": 0, **metrics,
        },
        "is_final": True,
    }
    # `user_id` is an Identity, so it carries the credential the endpoint
    # now requires. Kept in the body as well: the server ignores it, and a
    # test that stopped sending it would no longer prove that.
    headers = getattr(user_id, "headers", {})
    resp = client.post("/api/session/upsert", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health_reports_database_reachability(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    # A process that is up but can't reach storage is not healthy; the
    # endpoint has to actually check rather than always answering "ok".
    assert body["database"]["reachable"] is True
    assert "SQLite" in body["database"]["backend"]


# ---------------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------------

def test_score_for_an_unknown_user_is_an_empty_shape_not_an_error(client):
    user = new_user(client)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()
    assert body["trend"] == []
    assert body["baseline_mean"] is None
    assert body["forecast"] is None
    assert body["recent_assessment_sessions"] == []


def test_score_reflects_an_upserted_session(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=900, backspace_count=60, revision_count=3)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()
    assert body["current_score"] == 100.0
    assert body["independent_minutes_7d"] == pytest.approx(25.0)


def test_assessment_session_surfaces_the_new_anomaly_fields(client):
    user = new_user(client)
    upsert(client, user, f"{user}-a1", category="assessment",
           typed_chars=20, pasted_chars=300, likely_ai_pastes=3, tab_switch_count=6)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()

    assert body["assessment_score"] is not None
    assert body["assessment_risk_driver"] in ("absolute", "personal")
    assert body["assessment_deviation"]["status"] in ("no_baseline", "insufficient_data", "ok")
    assert isinstance(body["assessment_explanation"], str)

    session = body["recent_assessment_sessions"][0]
    assert {"risk_level", "risk_driver", "absolute_risk_level", "personal_z_score"} <= set(session)


def test_a_single_assessment_falls_back_to_absolute_risk(client):
    user = new_user(client)
    upsert(client, user, f"{user}-a1", category="assessment", typed_chars=20, pasted_chars=300)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()
    # One observation is nowhere near enough to call anything anomalous
    # for this person specifically.
    assert body["assessment_deviation"]["status"] == "insufficient_data"
    assert body["assessment_risk_driver"] == "absolute"


# ---------------------------------------------------------------------------
# Nudge
# ---------------------------------------------------------------------------

def test_nudge_decide_returns_an_arm_and_an_event(client):
    user = new_user(client)
    body = client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers).json()
    assert body["arm"] in ("none", "reflect", "pause", "contrast")
    assert body["event_id"]
    assert set(body["scores"]) == {"none", "reflect", "pause", "contrast"}


def test_nudge_decide_accepts_a_client_supplied_local_hour(client):
    user = new_user(client)
    body = client.post("/api/nudge/decide", json={"user_id": user, "hour_of_day": 23}, headers=user.headers).json()
    assert body["context"]["time_of_day"] == pytest.approx(23 / 24, abs=1e-4)


def test_nudge_decide_rejects_an_impossible_hour(client):
    user = new_user(client)
    resp = client.post("/api/nudge/decide", json={"user_id": user, "hour_of_day": 99}, headers=user.headers)
    assert resp.status_code == 422


def test_nudge_feedback_round_trip(client):
    user = new_user(client)
    decision = client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers).json()
    resp = client.post("/api/nudge/feedback", json={
        "user_id": user, "event_id": decision["event_id"], "outcome": "accepted",
    }, headers=user.headers)
    assert resp.status_code == 200
    assert resp.json()["reward"] == 1.0


def test_nudge_feedback_for_an_unknown_event_is_404(client):
    user = new_user(client)
    resp = client.post("/api/nudge/feedback", json={
        "user_id": user, "event_id": "does-not-exist", "outcome": "accepted",
    }, headers=user.headers)
    assert resp.status_code == 404


def test_nudge_feedback_twice_is_409(client):
    user = new_user(client)
    decision = client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers).json()
    payload = {"user_id": user, "event_id": decision["event_id"], "outcome": "accepted"}
    assert client.post("/api/nudge/feedback", json=payload,
                       headers=user.headers).status_code == 200
    assert client.post("/api/nudge/feedback", json=payload,
                       headers=user.headers).status_code == 409


def test_nudge_feedback_rejects_an_unknown_outcome_at_validation(client):
    user = new_user(client)
    decision = client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers).json()
    resp = client.post("/api/nudge/feedback", json={
        "user_id": user, "event_id": decision["event_id"], "outcome": "shrugged",
    }, headers=user.headers)
    assert resp.status_code == 422


def test_another_users_event_cannot_be_rewarded(client):
    victim, attacker = new_user(client), new_user(client)
    decision = client.post("/api/nudge/decide", json={"user_id": victim}, headers=victim.headers).json()
    resp = client.post("/api/nudge/feedback", json={
        "user_id": attacker, "event_id": decision["event_id"], "outcome": "accepted",
    }, headers=attacker.headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Sessions feed
# ---------------------------------------------------------------------------

def test_sessions_feed_returns_only_the_requested_user(client):
    mine, theirs = new_user(client), new_user(client)
    upsert(client, mine, f"{mine}-w1", typed_chars=400)
    upsert(client, theirs, f"{theirs}-w1", typed_chars=400)

    body = client.get("/api/sessions", params={"user_id": mine}, headers=mine.headers).json()
    ids = [s["session_id"] for s in body["sessions"]]
    assert ids == [f"{mine}-w1"]


def test_sessions_feed_is_most_recent_first(client):
    user = new_user(client)
    for n in range(3):
        upsert(client, user, f"{user}-w{n}", typed_chars=400)

    body = client.get("/api/sessions", params={"user_id": user}, headers=user.headers).json()
    assert [s["session_id"] for s in body["sessions"]] == [
        f"{user}-w2", f"{user}-w1", f"{user}-w0",
    ]


def test_sessions_feed_clamps_the_limit_at_both_ends(client):
    """The ceiling exists because the dashboard's 20-week activity heatmap
    asks for hundreds of rows: cap it too low and the grid silently
    renders older days as 'no activity' rather than 'not fetched', which
    is a false claim rather than a missing one. The floor exists because
    limit=0 and limit=-1 are otherwise a valid way to ask SQLite for
    nothing, or for everything."""
    user = new_user(client)
    for n in range(4):
        upsert(client, user, f"{user}-w{n}", typed_chars=400)

    assert len(client.get("/api/sessions", params={"user_id": user, "limit": 2}, headers=user.headers).json()["sessions"]) == 2
    # Below 1 is clamped up to 1, not passed through as "no rows".
    assert len(client.get("/api/sessions", params={"user_id": user, "limit": 0}, headers=user.headers).json()["sessions"]) == 1
    assert len(client.get("/api/sessions", params={"user_id": user, "limit": -5}, headers=user.headers).json()["sessions"]) == 1
    # Above the ceiling is clamped down rather than rejected — an
    # over-eager client gets data, not a 422.
    resp = client.get("/api/sessions", params={"user_id": user, "limit": 99_999}, headers=user.headers)
    assert resp.status_code == 200
    assert len(resp.json()["sessions"]) == 4


def test_sessions_feed_covers_the_dashboards_heatmap_window(client):
    """Guards the specific coupling that broke once already: App.tsx asks
    for 400 rows, and if the server's cap drops below that the heatmap
    loses its older columns without any error anywhere."""
    user = new_user(client)
    for n in range(120):
        upsert(client, user, f"{user}-w{n}", typed_chars=100)

    body = client.get("/api/sessions", params={"user_id": user, "limit": 400}, headers=user.headers).json()
    assert len(body["sessions"]) == 120


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def test_label_a_session(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=500)
    resp = client.post("/api/session/label", json={
        "user_id": user, "session_id": f"{user}-w1", "understood": 4,
    }, headers=user.headers)
    assert resp.status_code == 200


def test_label_rejects_an_out_of_range_rating(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=500)
    resp = client.post("/api/session/label", json={
        "user_id": user, "session_id": f"{user}-w1", "understood": 9,
    }, headers=user.headers)
    assert resp.status_code == 422


def test_label_for_another_users_session_is_404(client):
    owner, attacker = new_user(client), new_user(client)
    upsert(client, owner, f"{owner}-w1", typed_chars=500)
    resp = client.post("/api/session/label", json={
        "user_id": attacker, "session_id": f"{owner}-w1", "understood": 5,
    }, headers=attacker.headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Export / delete
# ---------------------------------------------------------------------------

def test_export_returns_every_user_scoped_table(client):
    # Imported here, not at module scope: db.py resolves DB_PATH at import
    # time and the `client` fixture is what sets AUTONOMIZE_DB_PATH first.
    import db

    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=500)
    client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers)
    client.post("/api/session/label", json={
        "user_id": user, "session_id": f"{user}-w1", "understood": 3,
    }, headers=user.headers)

    data = client.get("/api/me/export", params={"user_id": user}, headers=user.headers).json()["data"]
    # Compared against db.USER_SCOPED_TABLES rather than a literal list:
    # the point of this test is that export covers EVERY user-scoped table,
    # and a hard-coded set turns that into "covers the five I thought of in
    # 2026" — which is how a new table quietly escapes both export and
    # erase.
    assert set(data) == set(db.USER_SCOPED_TABLES)
    assert len(data["sessions"]) == 1
    assert len(data["nudge_events"]) == 1
    assert len(data["session_labels"]) == 1


def test_export_only_returns_the_requesting_user(client):
    mine, theirs = new_user(client), new_user(client)
    upsert(client, mine, f"{mine}-w1", typed_chars=500)
    upsert(client, theirs, f"{theirs}-w1", typed_chars=500)

    data = client.get("/api/me/export", params={"user_id": mine}, headers=mine.headers).json()["data"]
    assert [s["session_id"] for s in data["sessions"]] == [f"{mine}-w1"]


def test_delete_removes_everything_for_that_user(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=500)
    client.post("/api/nudge/decide", json={"user_id": user}, headers=user.headers)
    client.post("/api/session/label", json={
        "user_id": user, "session_id": f"{user}-w1", "understood": 3,
    }, headers=user.headers)

    deleted = client.request("DELETE", "/api/me/data", params={"user_id": user}, headers=user.headers).json()["deleted"]
    assert deleted["sessions"] == 1
    assert deleted["user_baseline"] == 1
    assert deleted["nudge_events"] == 1
    assert deleted["session_labels"] == 1
    assert deleted["bandit_state"] >= 0

    after = client.get("/api/me/export", params={"user_id": user}, headers=user.headers).json()["data"]
    assert all(rows == [] for rows in after.values())


def test_delete_leaves_other_users_untouched(client):
    mine, theirs = new_user(client), new_user(client)
    upsert(client, mine, f"{mine}-w1", typed_chars=500)
    upsert(client, theirs, f"{theirs}-w1", typed_chars=500)

    client.request("DELETE", "/api/me/data", params={"user_id": mine}, headers=mine.headers)

    survivors = client.get("/api/me/export", params={"user_id": theirs}, headers=theirs.headers).json()["data"]
    assert len(survivors["sessions"]) == 1


def test_delete_is_idempotent(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=500)
    client.request("DELETE", "/api/me/data", params={"user_id": user}, headers=user.headers)
    second = client.request("DELETE", "/api/me/data", params={"user_id": user}, headers=user.headers)
    assert second.status_code == 200
    assert second.json()["deleted"]["sessions"] == 0


# ---------------------------------------------------------------------------
# Composition trend (typed vs pasted per day)
# ---------------------------------------------------------------------------

def test_composition_trend_is_empty_for_a_new_user(client):
    user = new_user(client)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()
    assert body["composition_trend"] == []


def test_composition_trend_reports_typed_and_pasted_per_day(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=600, pasted_chars=150)
    body = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()

    assert len(body["composition_trend"]) == 1
    point = body["composition_trend"][0]
    assert point["typed_chars"] == 600
    assert point["pasted_chars"] == 150
    assert "date" in point


def test_composition_trend_sums_multiple_sessions_on_the_same_day(client):
    user = new_user(client)
    same_day = int(time.time() * 1000) - 60 * 60 * 1000
    upsert(client, user, f"{user}-a", started_at=same_day, typed_chars=100, pasted_chars=10)
    upsert(client, user, f"{user}-b", started_at=same_day, typed_chars=200, pasted_chars=20)

    points = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()["composition_trend"]
    assert len(points) == 1
    assert points[0]["typed_chars"] == 300
    assert points[0]["pasted_chars"] == 30


def test_composition_trend_includes_assessment_sessions(client):
    # Unlike `trend` (writing-only, because the two categories use
    # different scoring formulas), raw character counts are comparable
    # across categories — and blanking out exam days would hide exactly
    # the days worth looking at.
    user = new_user(client)
    upsert(client, user, f"{user}-exam", category="assessment", typed_chars=50, pasted_chars=400)
    points = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()["composition_trend"]
    assert points[0]["typed_chars"] == 50
    assert points[0]["pasted_chars"] == 400


def test_composition_trend_excludes_ai_assistant_sessions(client):
    # Characters typed on ChatGPT are prompts, not work produced — counting
    # them as "what I wrote myself" would be actively misleading.
    user = new_user(client)
    upsert(client, user, f"{user}-w", typed_chars=100, pasted_chars=0)
    upsert(client, user, f"{user}-ai", category="ai_assistant", typed_chars=9999, pasted_chars=0)
    points = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()["composition_trend"]
    assert len(points) == 1
    assert points[0]["typed_chars"] == 100


def test_composition_trend_carries_the_ai_linked_paste_count(client):
    user = new_user(client)
    upsert(client, user, f"{user}-w1", typed_chars=100, pasted_chars=400, likely_ai_pastes=3)
    point = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()["composition_trend"][0]
    assert point["ai_linked_pastes"] == 3


def test_composition_trend_separates_distinct_days(client):
    user = new_user(client)
    now = int(time.time() * 1000)
    upsert(client, user, f"{user}-d1", started_at=now - 3 * 86400_000, typed_chars=100)
    upsert(client, user, f"{user}-d2", started_at=now - 1 * 86400_000, typed_chars=200)

    points = client.get("/api/score", params={"user_id": user}, headers=user.headers).json()["composition_trend"]
    assert len(points) == 2
    # Ordered oldest-first, so the chart draws left-to-right in time.
    assert points[0]["date"] < points[1]["date"]
    assert [p["typed_chars"] for p in points] == [100, 200]


# ---------------------------------------------------------------------------
# Rate limiting (write endpoints only)
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429_over_real_http(monkeypatch):
    """Drives the actual middleware, not just ratelimit.check().

    A limiter that works in isolation but isn't wired into the request
    path is worse than none — it reads as protection in code review while
    doing nothing at runtime.
    """
    import tempfile as _tempfile
    from pathlib import Path as _Path

    tmp_dir = _tempfile.mkdtemp(prefix="autonomize-rl-test-")
    os.environ["AUTONOMIZE_DB_PATH"] = str(_Path(tmp_dir) / "rl.db")

    from fastapi.testclient import TestClient

    import main
    import ratelimit

    monkeypatch.setattr(ratelimit, "ENABLED", True)
    monkeypatch.setattr(ratelimit, "MAX_REQUESTS", 3)
    monkeypatch.setattr(ratelimit, "WINDOW_SECONDS", 60.0)
    ratelimit.reset()

    try:
        with TestClient(main.app) as c:
            # Registered against this client, not the module-scoped one —
            # it has its own database file.
            user = new_user(c)
            statuses = [
                c.post("/api/nudge/decide", json={"user_id": user},
                       headers=user.headers).status_code
                for _ in range(5)
            ]
            assert statuses[:3] == [200, 200, 200]
            assert statuses[3:] == [429, 429]

            blocked = c.post("/api/nudge/decide", json={"user_id": user},
                             headers=user.headers)
            assert blocked.headers.get("Retry-After") == "60"

            # Reads stay available — the limit is on writes, and locking a
            # user out of viewing their own dashboard would be a worse bug
            # than the one being prevented.
            assert c.get("/api/score", params={"user_id": user},
                         headers=user.headers).status_code == 200
            assert c.get("/api/health").status_code == 200
    finally:
        ratelimit.reset()
        for leftover in _Path(tmp_dir).glob("*"):
            leftover.unlink()
        _Path(tmp_dir).rmdir()
        os.environ.pop("AUTONOMIZE_DB_PATH", None)


# ---------------------------------------------------------------------------
# Auth over real HTTP
# ---------------------------------------------------------------------------

GOOD_PASSWORD = "the quiet river runs north"


def new_email():
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def test_auth_config_tells_the_ui_what_to_offer(client):
    body = client.get("/api/auth/config").json()
    # Password always works; the rest depend on Supabase being configured.
    # The UI uses this so it never shows a Google button that 500s.
    assert body["password"] is True
    assert "google" in body and "otp" in body


def test_register_then_use_the_session(client):
    email = new_email()
    resp = client.post("/api/auth/register", json={"email": email, "password": GOOD_PASSWORD})
    assert resp.status_code == 201
    token = resp.json()["access_token"]

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email
    assert me.json()["user"]["role"] == "student"


def test_registration_never_grants_admin(client):
    # The role must not be settable from the request body under any name.
    resp = client.post("/api/auth/register", json={
        "email": new_email(), "password": GOOD_PASSWORD, "role": "admin",
    })
    assert resp.status_code == 201
    assert resp.json()["user"]["role"] == "student"


def test_weak_password_is_rejected_with_a_useful_message(client):
    resp = client.post("/api/auth/register", json={"email": new_email(), "password": "pass"})
    assert resp.status_code == 400
    assert "characters" in resp.json()["detail"].lower()


def test_login_with_correct_and_incorrect_credentials(client):
    email = new_email()
    client.post("/api/auth/register", json={"email": email, "password": GOOD_PASSWORD})

    ok = client.post("/api/auth/login", json={"email": email, "password": GOOD_PASSWORD})
    assert ok.status_code == 200

    bad = client.post("/api/auth/login", json={"email": email, "password": "wrong password here"})
    assert bad.status_code == 401


def test_me_requires_a_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_logout_invalidates_the_token_for_real(client):
    email = new_email()
    token = client.post("/api/auth/register",
                        json={"email": email, "password": GOOD_PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    # Still a cryptographically valid, unexpired JWT — and still dead.
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_logout_is_safe_without_a_session(client):
    # A confused client logging out twice shouldn't get an error it can't
    # act on.
    assert client.post("/api/auth/logout").status_code == 200


def test_logout_everywhere_kills_other_devices(client):
    email = new_email()
    client.post("/api/auth/register", json={"email": email, "password": GOOD_PASSWORD})
    a = client.post("/api/auth/login", json={"email": email, "password": GOOD_PASSWORD}).json()
    b = client.post("/api/auth/login", json={"email": email, "password": GOOD_PASSWORD}).json()

    out = client.post("/api/auth/logout-everywhere",
                      headers={"Authorization": f"Bearer {a['access_token']}"})
    assert out.status_code == 200
    for session in (a, b):
        assert client.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {session['access_token']}"}).status_code == 401


# ---------------------------------------------------------------------------
# Institution (admin) view — authorization
# ---------------------------------------------------------------------------

def test_cohort_requires_a_session(client):
    assert client.get("/api/admin/cohort").status_code == 401


def test_cohort_is_403_for_a_student_account(client):
    email = new_email()
    token = client.post("/api/auth/register",
                        json={"email": email, "password": GOOD_PASSWORD}).json()["access_token"]
    resp = client.get("/api/admin/cohort", headers={"Authorization": f"Bearer {token}"})
    # 403 not 404: they're authenticated, just not permitted. And the role
    # comes from the database — nothing in the request can assert it.
    assert resp.status_code == 403


def test_cohort_is_reachable_once_promoted_server_side(client):
    import accounts as acct
    import db as database

    email = new_email()
    token = client.post("/api/auth/register",
                        json={"email": email, "password": GOOD_PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/admin/cohort", headers=headers).status_code == 403

    # Promotion happens server-side only (make_admin.py does exactly this).
    with database.get_conn() as conn:
        user = acct.get_user_by_email(conn, email)
        conn.execute(database.q("UPDATE users SET role = 'admin' WHERE user_id = ?"),
                     (user["user_id"],))

    resp = client.get("/api/admin/cohort", headers=headers)
    # The SAME token now works — proving the role is re-read per request
    # rather than baked into the token at issue time.
    assert resp.status_code == 200
    body = resp.json()
    assert "available" in body


def test_cohort_withholds_everything_for_a_small_population(client):
    import accounts as acct
    import db as database

    email = new_email()
    token = client.post("/api/auth/register",
                        json={"email": email, "password": GOOD_PASSWORD}).json()["access_token"]
    with database.get_conn() as conn:
        user = acct.get_user_by_email(conn, email)
        conn.execute(database.q("UPDATE users SET role = 'admin' WHERE user_id = ?"),
                     (user["user_id"],))

    body = client.get("/api/admin/cohort", headers={"Authorization": f"Bearer {token}"}).json()
    if not body["available"]:
        assert body["reason"] == "cohort_too_small"
        assert "mean_score" not in body


def test_cohort_route_rejects_filter_parameters(client):
    """A filterable 'aggregate' view can be narrowed until the aggregate is
    one student. FastAPI ignores unknown query params, so this asserts the
    handler takes none — the protection is the absence of the parameter,
    and this test fails if someone adds one without re-checking the floor."""
    import inspect

    import main as app_module

    sig = inspect.signature(app_module.admin_cohort)
    assert set(sig.parameters) == {"request", "authorization"}


# ---------------------------------------------------------------------------
# Identity — the IDOR regression suite
# ---------------------------------------------------------------------------
# These are the tests that would have caught the original defect: telemetry
# endpoints accepted whatever `user_id` the caller supplied, so naming
# someone else's id was enough to read or delete their data. Every case
# below sends a *valid* credential for one user while claiming to be
# another — the shape an attacker would actually use.

USER_SCOPED_READS = ("/api/sessions", "/api/score", "/api/me/export")


@pytest.mark.parametrize("path", USER_SCOPED_READS)
def test_user_scoped_reads_reject_an_unauthenticated_request(client, path):
    victim = new_user(client)
    upsert(client, victim, f"{victim}-w1", typed_chars=500)

    resp = client.get(path, params={"user_id": victim})
    assert resp.status_code == 401, f"{path} served an unauthenticated caller"


@pytest.mark.parametrize("path", USER_SCOPED_READS)
def test_claiming_another_users_id_returns_your_own_data_not_theirs(client, path):
    """The core property: the token decides identity, the body does not."""
    victim = new_user(client)
    upsert(client, victim, f"{victim}-w1", typed_chars=900, pasted_chars=10)

    attacker = new_user(client)
    resp = client.get(path, params={"user_id": victim}, headers=attacker.headers)

    assert resp.status_code == 200
    # Whatever comes back must belong to the attacker, who has no sessions.
    assert victim not in resp.text


def test_upsert_writes_into_the_tokens_user_not_the_claimed_one(client):
    victim = new_user(client)
    attacker = new_user(client)

    body = {
        "user_id": victim,                    # the lie
        "session_id": f"{attacker}-planted",
        "category": "writing",
        "domain": "docs.google.com",
        "started_at": int(time.time() * 1000) - 3600_000,
        "active_ms": 25 * 60_000,
        "metrics": {"typed_chars": 100},
        "is_final": True,
    }
    assert client.post("/api/session/upsert", json=body,
                       headers=attacker.headers).status_code == 200

    # The row landed on the attacker, so the victim's feed is untouched.
    victim_feed = client.get("/api/sessions", params={"user_id": victim},
                             headers=victim.headers).json()["sessions"]
    assert victim_feed == []

    attacker_feed = client.get("/api/sessions", params={"user_id": attacker},
                               headers=attacker.headers).json()["sessions"]
    assert len(attacker_feed) == 1


def test_delete_cannot_be_aimed_at_another_user(client):
    victim = new_user(client)
    upsert(client, victim, f"{victim}-w1", typed_chars=500)
    attacker = new_user(client)

    resp = client.request("DELETE", "/api/me/data", params={"user_id": victim},
                          headers=attacker.headers)
    assert resp.status_code == 200
    # The attacker deleted their own (empty) data, not the victim's.
    assert resp.json()["deleted"]["sessions"] == 0

    still_there = client.get("/api/sessions", params={"user_id": victim},
                             headers=victim.headers).json()["sessions"]
    assert len(still_there) == 1


def test_a_forged_bearer_token_is_rejected(client):
    user = new_user(client)
    resp = client.get("/api/score", params={"user_id": user},
                      headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_device_registration_issues_a_usable_identity(client):
    """Zero-config first run still works — the server mints the id."""
    resp = client.post("/api/auth/device")
    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"]
    assert body["user"]["user_id"]

    headers = {"Authorization": f"Bearer {body['access_token']}"}
    assert client.get("/api/score", params={"user_id": body["user"]["user_id"]},
                      headers=headers).status_code == 200


def test_two_device_registrations_are_separate_identities(client):
    a = new_user(client)
    b = new_user(client)
    assert a != b
    upsert(client, a, f"{a}-w1", typed_chars=500)
    assert client.get("/api/sessions", params={"user_id": b},
                      headers=b.headers).json()["sessions"] == []


def test_a_device_account_cannot_reach_the_cohort_view(client):
    """Device accounts are students. Anonymity must not imply privilege."""
    user = new_user(client)
    assert client.get("/api/admin/cohort", headers=user.headers).status_code == 403


def test_every_response_carries_the_api_version(client):
    """A client cannot detect a contract it does not understand without
    one. There is no /v1/ prefix — see the middleware for why — so the
    header is the only signal available."""
    for resp in (client.get("/api/health"),
                 client.get("/api/score", params={"user_id": "x"}),
                 client.post("/api/auth/device")):
        assert resp.headers.get("X-Autonomize-API-Version") == "1"


def test_a_user_cannot_write_into_another_users_session(client):
    """session_id is the primary key and the client chooses it, so two
    users can name the same one.

    Before the ownership check, the UPDATE branch matched on session_id
    alone and the second user's counters were ACCUMULATED into the first
    user's row — found when an audit script reused ids across two test
    users and A's typed_chars went from 2,000 to 11,999.

    Never a data leak (the row stays owned by A), but one account could
    corrupt another's independence score, and the victim would see a number
    they could not explain with nothing in the record to say why.
    """
    a = new_user(client)
    b = new_user(client)
    # Unique per run: this module's `client` fixture is module-scoped and
    # one test repoints AUTONOMIZE_DB_PATH partway through, so a fixed id
    # is a latent collision with whatever ran before.
    shared = f"shared-{uuid.uuid4()}"

    upsert(client, a, shared, typed_chars=2000)
    response = client.post("/api/session/upsert", json={
        "user_id": str(b), "session_id": shared, "category": "writing",
        "started_at": int(time.time() * 1000) - 3600_000, "active_ms": 60_000,
        "metrics": {"typed_chars": 9999, "pasted_chars": 0, "backspace_count": 0,
                    "revision_count": 0, "likely_ai_pastes": 0, "tab_switch_count": 0},
        "is_final": True,
    }, headers=b.headers)

    assert response.status_code == 409, response.text

    # And A's row is untouched.
    sessions = client.get("/api/sessions", params={"user_id": a},
                          headers=a.headers).json()["sessions"]
    mine = [s for s in sessions if s["session_id"] == shared]
    assert len(mine) == 1
    assert mine[0]["typed_chars"] == 2000


def test_the_rejection_does_not_reveal_who_owns_the_session(client):
    """Otherwise the endpoint becomes an oracle for enumerating other
    users' session ids."""
    a = new_user(client)
    b = new_user(client)
    secret = f"secret-{uuid.uuid4()}"
    upsert(client, a, secret, typed_chars=500)

    response = client.post("/api/session/upsert", json={
        "user_id": str(b), "session_id": secret, "category": "writing",
        "started_at": int(time.time() * 1000) - 3600_000, "active_ms": 60_000,
        "metrics": {"typed_chars": 1, "pasted_chars": 0, "backspace_count": 0,
                    "revision_count": 0, "likely_ai_pastes": 0, "tab_switch_count": 0},
        "is_final": True,
    }, headers=b.headers)
    assert str(a) not in response.text
