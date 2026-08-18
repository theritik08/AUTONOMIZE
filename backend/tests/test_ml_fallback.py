"""The system must work identically when the ML layer is not there.

This is the property that makes adding machine learning to a working
product safe rather than reckless. Every one of these tests removes or
breaks part of the ML layer and asserts that `/api/score` still answers,
still returns the deterministic signals, and never emits a fabricated
number in place of a missing one.

The distinction being defended: a *missing* answer and a *guessed* answer
look the same to a caller unless the code is careful, and only one of them
is honest.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

import db
import main
import ratelimit
from ml import inference, registry


@pytest.fixture
def client(tmp_path, monkeypatch):
    # db.py resolves DB_PATH at import time, so the module attribute is
    # what has to move — setting the env var here would be too late and the
    # tests would quietly run against the developer's real autonomize.db.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fallback-test.db")
    inference.reset_cache()
    # Device registration has its own limiter (60 per 5 minutes). A whole
    # test session shares one process, so without this the last few tests
    # to run get a 429 that has nothing to do with what they assert.
    ratelimit.reset()
    main.reset_rate_limits()
    # As a context manager so the lifespan hook runs: that is what applies
    # migrations and opens the pool.
    with TestClient(main.app) as test_client:
        yield test_client


def register(client):
    """Mints a server-issued device identity, the way the extension does."""
    response = client.post("/api/auth/device")
    assert response.status_code == 201, response.text
    body = response.json()
    return (body["user"]["user_id"],
            {"Authorization": f"Bearer {body['access_token']}"})


def post_sessions(client, identity, n=8):
    user_id, headers = identity
    # Relative to now: /api/score's rollups are windowed, so a fixed epoch
    # silently falls out of range and the assertions start reading zeros.
    now = int(time.time() * 1000)
    for i in range(n):
        response = client.post("/api/session/upsert", json={
            # Present because the schema still carries it; the server
            # ignores it and uses the bearer token instead. That is the
            # IDOR fix, and passing a real id here keeps the test honest
            # about what is actually being authenticated.
            "user_id": user_id,
            "session_id": f"s{i}",
            "started_at": now - (n - i) * 86_400_000,
            "category": "writing",
            # `is_final` and `active_ms`, at the TOP level — which is where
            # SessionUpsertRequest declares them.
            #
            # This previously sent `finalized` (not a field on the schema)
            # and nested `active_ms` inside `metrics` (not a field on
            # Metrics). Pydantic dropped both silently, so every session
            # posted here was unfinalised and zero-length, nothing was ever
            # scored, and `current_score` came back as the hardcoded 50.0
            # fallback. The assertion below — "the deterministic pipeline is
            # untouched" — was therefore testing a constant, not a pipeline.
            # Removing that fallback is what exposed it.
            "is_final": True,
            "active_ms": 25 * 60_000,
            "metrics": {
                "typed_chars": 900, "pasted_chars": 100,
                "backspace_count": 40, "revision_count": 5,
                "likely_ai_pastes": 0, "tab_switch_count": 2,
                "iki_buckets": [10, 40, 60, 30, 15, 8, 4, 2],
            },
        }, headers=headers)
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# No model at all — the normal state of a fresh deployment
# ---------------------------------------------------------------------------

def test_the_score_endpoint_works_with_no_model_file(client, tmp_path,
                                                     monkeypatch):
    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()

    identity = register(client)
    headers = identity[1]
    post_sessions(client, identity)
    body = client.get("/api/score", headers=headers).json()

    # The deterministic pipeline is untouched.
    assert body["current_score"] is not None
    assert "trend" in body and "forecast" in body
    assert body["signals"]["rhythm"]["required"] > 0

    # And the learned layer is absent rather than invented.
    assert body["prediction"] is None
    assert body["behavioural_anomaly"]["status"] in ("unavailable",
                                                     "insufficient_data")
    assert body["behavioural_explanation"] is None
    assert body["signals"]["model"]["available"] is False
    assert body["signals"]["model"]["reason"]


def test_cold_start_still_answers_with_no_model(client, tmp_path, monkeypatch):
    """The empirical-Bayes layer degrades to the personal mean rather than
    disappearing — the prior is a bonus, not a dependency."""
    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()

    identity = register(client)
    headers = identity[1]
    post_sessions(client, identity, n=6)
    personalisation = client.get("/api/score",
                                 headers=headers).json()["signals"]["personalisation"]

    assert personalisation["source"] == "personal_only"
    assert personalisation["confidence"] in ("learning", "provisional",
                                             "established")
    assert personalisation["message"]


# ---------------------------------------------------------------------------
# A broken model — the dangerous state, because it looks like a working one
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("contents,expected", [
    ("{not json at all", "not valid JSON"),
    ("{}", "no manifest"),
    (json.dumps({"model": {"kind": "unknown-kind"},
                 "manifest": {"model_format_version": 2,
                              "feature_set_hash": "wrong"}}),
     "feature definitions"),
])
def test_a_broken_model_file_falls_back_rather_than_erroring(
        client, tmp_path, monkeypatch, contents, expected):
    path = tmp_path / "model.json"
    path.write_text(contents)
    monkeypatch.setattr(registry, "MODEL_PATH", str(path))
    inference.reset_cache()

    identity = register(client)
    headers = identity[1]
    post_sessions(client, identity)
    response = client.get("/api/score", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["current_score"] is not None
    assert body["prediction"] is None
    assert expected in body["signals"]["model"]["reason"]


def test_a_model_whose_format_version_moved_on_is_refused(tmp_path, monkeypatch):
    from ml import manifest as manifest_module

    payload = {"model": {"kind": "ridge", "coefficients": [1.0], "mean": [0.0],
                         "scale": [1.0]},
               "manifest": {"model_format_version": 999,
                            "feature_set_hash": "x"}}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(registry, "MODEL_PATH", str(path))
    inference.reset_cache()

    assert inference.available() is False
    assert "format" in inference.describe()["reason"]
    ok, reason = manifest_module.compatible(payload["manifest"])
    assert ok is False and "format" in reason


# ---------------------------------------------------------------------------
# The layer's own refusals
# ---------------------------------------------------------------------------

def test_global_importance_is_empty_rather_than_fabricated(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()
    assert inference.global_importance() == []


def test_behavioural_anomaly_refuses_without_a_forest(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()
    verdict = inference.behavioural_anomaly([], {})
    assert verdict["status"] in ("unavailable", "insufficient_data")
    assert verdict["score"] is None


def test_the_registry_reports_no_model_distinctly_from_a_broken_one(
        tmp_path, monkeypatch):
    """A deployment that never trained and one whose file is corrupt need
    different fixes, so they must not produce the same message."""
    missing = registry.reason_unavailable(str(tmp_path / "absent.json"))
    broken = tmp_path / "broken.json"
    broken.write_text("{{{")
    corrupt = registry.reason_unavailable(str(broken))
    assert missing != corrupt
    assert "never" in missing or "no model" in missing
