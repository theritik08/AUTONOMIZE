"""Tests for the nudge policy: featurization, persistence, and — the part
with the most design in it — where rewards come from.

The attribution tests are the important ones. A bandit that can only be
rewarded through explicit feedback structurally cannot evaluate the
'none' arm, and would therefore always drift toward over-nudging; these
confirm the outcome-attribution path actually closes that hole.
"""
import json

import pytest

import bandit
import db
import nudge


def base_context(**overrides):
    kwargs = dict(
        current_score=80.0,
        delta_vs_baseline=0.0,
        independent_minutes_7d=100.0,
        assisted_minutes_7d=20.0,
        streak_days=3,
        hour_of_day=14,
        recent_nudges=0,
    )
    kwargs.update(overrides)
    return nudge.build_context(**kwargs)


# ---------------------------------------------------------------------------
# Featurization
# ---------------------------------------------------------------------------

def test_context_has_the_declared_dimension():
    assert len(base_context()) == nudge.CONTEXT_DIM == len(nudge.CONTEXT_FEATURES)


def test_context_starts_with_a_bias_term():
    assert base_context()[0] == 1.0


def test_every_feature_is_normalized_into_range():
    extreme = nudge.build_context(
        current_score=100.0, delta_vs_baseline=9999.0,
        independent_minutes_7d=0.0, assisted_minutes_7d=100000.0,
        streak_days=9999, hour_of_day=23, recent_nudges=9999,
    )
    # Unscaled inputs would let one feature dominate both theta and the
    # uncertainty term, since LinUCB does no normalization of its own.
    for name, value in zip(nudge.CONTEXT_FEATURES, extreme):
        assert -1.0 <= value <= 1.0, f"{name} escaped its range: {value}"


def test_delta_feature_is_signed():
    below = base_context(delta_vs_baseline=-30.0)
    above = base_context(delta_vs_baseline=30.0)
    idx = nudge.CONTEXT_FEATURES.index("delta_vs_baseline")
    assert below[idx] < 0 < above[idx]


def test_assisted_share_is_a_ratio_not_a_raw_total():
    heavy = base_context(independent_minutes_7d=10.0, assisted_minutes_7d=90.0)
    light = base_context(independent_minutes_7d=900.0, assisted_minutes_7d=100.0)
    idx = nudge.CONTEXT_FEATURES.index("assisted_share_7d")
    # The light user has more assisted minutes in absolute terms but a far
    # smaller share; the share is what the policy should see.
    assert heavy[idx] > light[idx]


def test_zero_activity_does_not_divide_by_zero():
    context = base_context(independent_minutes_7d=0.0, assisted_minutes_7d=0.0)
    idx = nudge.CONTEXT_FEATURES.index("assisted_share_7d")
    assert context[idx] == 0.0


def test_missing_score_falls_back_to_a_neutral_value():
    context = nudge.build_context(
        current_score=None, delta_vs_baseline=None,
        independent_minutes_7d=None, assisted_minutes_7d=None,
        streak_days=None, hour_of_day=None, recent_nudges=None,
    )
    assert len(context) == nudge.CONTEXT_DIM
    assert all(isinstance(v, float) for v in context)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_new_user_gets_fresh_models_for_every_arm(sqlite_conn):
    models = nudge.load_models(sqlite_conn, "u1")
    assert set(models) == set(nudge.ARMS)
    for model in models.values():
        assert model.n_pulls == 0
        assert model.a_matrix == bandit.identity(nudge.CONTEXT_DIM)


def test_models_round_trip_through_the_database(sqlite_conn):
    models = nudge.load_models(sqlite_conn, "u1")
    model = models["reflect"]
    model.update(base_context(), reward=1.0)
    nudge.save_model(sqlite_conn, "u1", "reflect", model)

    reloaded = nudge.load_models(sqlite_conn, "u1")["reflect"]
    assert reloaded.n_pulls == 1
    assert reloaded.a_matrix == model.a_matrix
    assert reloaded.b_vector == model.b_vector


def test_a_stored_model_of_the_wrong_shape_is_discarded(sqlite_conn):
    # Simulates a model saved before the context vector changed shape.
    db.save_bandit_arm(sqlite_conn, "u1", "reflect", json.dumps([[1.0]]), json.dumps([0.0]), 7)
    model = nudge.load_models(sqlite_conn, "u1")["reflect"]
    # Silently mixing dimensions would corrupt the policy, so it resets.
    assert model.n_pulls == 0
    assert len(model.b_vector) == nudge.CONTEXT_DIM


def test_models_are_per_user(sqlite_conn):
    models = nudge.load_models(sqlite_conn, "u1")
    models["pause"].update(base_context(), reward=1.0)
    nudge.save_model(sqlite_conn, "u1", "pause", models["pause"])
    assert nudge.load_models(sqlite_conn, "u2")["pause"].n_pulls == 0


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------

def test_decide_records_an_event_with_the_exact_context_used(sqlite_conn):
    context = base_context()
    result = nudge.decide(sqlite_conn, "u1", context)

    event = db.get_nudge_event(sqlite_conn, result["event_id"])
    assert event is not None
    assert event["arm"] == result["arm"]
    assert event["reward"] is None
    # The stored vector must be the decision-time state, so a reward that
    # arrives later updates the model with the right context.
    assert json.loads(event["context"]) == context


def test_decide_can_return_the_none_arm(sqlite_conn):
    # 'none' is a real arm, so callers must handle "do nothing" as an answer.
    assert "none" in nudge.ARMS
    result = nudge.decide(sqlite_conn, "u1", base_context())
    assert result["arm"] in nudge.ARMS
    assert result["message"] == nudge.ARM_COPY[result["arm"]]


def test_decide_reports_every_arms_score_for_inspection(sqlite_conn):
    result = nudge.decide(sqlite_conn, "u1", base_context())
    assert set(result["scores"]) == set(nudge.ARMS)
    assert set(result["context"]) == set(nudge.CONTEXT_FEATURES)


# ---------------------------------------------------------------------------
# Explicit feedback
# ---------------------------------------------------------------------------

def test_feedback_updates_the_chosen_arms_model(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    result = nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "accepted")

    assert result["ok"] is True
    assert result["reward"] == 1.0
    assert nudge.load_models(sqlite_conn, "u1")[decision["arm"]].n_pulls == 1


def test_feedback_settles_the_event(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "dismissed")
    event = db.get_nudge_event(sqlite_conn, decision["event_id"])
    assert event["reward"] == pytest.approx(nudge.REWARD_BY_OUTCOME["dismissed"])
    assert event["settled_by"] == "feedback"


def test_feedback_cannot_be_applied_twice(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "accepted")
    second = nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "accepted")
    # Otherwise a client retry would count one outcome as many observations.
    assert second == {"ok": False, "reason": "already_settled"}


def test_feedback_for_another_users_event_is_rejected(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    result = nudge.record_feedback(sqlite_conn, "attacker", decision["event_id"], "accepted")
    # Rewards are model updates; accepting a cross-user one would let any
    # caller poison someone else's policy.
    assert result == {"ok": False, "reason": "wrong_user"}
    assert nudge.load_models(sqlite_conn, "u1")[decision["arm"]].n_pulls == 0


def test_feedback_for_an_unknown_event_is_rejected(sqlite_conn):
    assert nudge.record_feedback(sqlite_conn, "u1", "nope", "accepted")["reason"] == "unknown_event"


def test_unknown_outcome_is_rejected(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    result = nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "shrugged")
    assert result == {"ok": False, "reason": "unknown_outcome"}


# ---------------------------------------------------------------------------
# Outcome attribution — how 'none' becomes learnable
# ---------------------------------------------------------------------------

def test_beating_your_baseline_rewards_the_pending_decision(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    settled = nudge.settle_pending_outcomes(sqlite_conn, "u1", score=90.0, baseline_mean=70.0)

    assert [s["event_id"] for s in settled] == [decision["event_id"]]
    assert settled[0]["reward"] == 1.0
    event = db.get_nudge_event(sqlite_conn, decision["event_id"])
    assert event["settled_by"] == "outcome"


def test_falling_below_your_baseline_gives_zero_reward(sqlite_conn):
    nudge.decide(sqlite_conn, "u1", base_context())
    settled = nudge.settle_pending_outcomes(sqlite_conn, "u1", score=40.0, baseline_mean=70.0)
    assert settled[0]["reward"] == 0.0


def test_outcome_attribution_makes_the_none_arm_learnable(sqlite_conn):
    """The design's central claim, tested directly.

    Nothing is shown to the student for the 'none' arm, so no explicit
    feedback can ever arrive for it. If outcome attribution didn't exist,
    this arm's model would stay untouched forever and its permanent
    maximum-uncertainty bonus would make the policy over-nudge.
    """
    context = base_context()
    # Force a decision on the 'none' arm specifically.
    event_id = "evt-none"
    db.insert_nudge_event(sqlite_conn, event_id, "u1", "none", json.dumps(context), 1_000)

    assert nudge.load_models(sqlite_conn, "u1")["none"].n_pulls == 0
    nudge.settle_pending_outcomes(sqlite_conn, "u1", score=95.0, baseline_mean=70.0, now_ms=2_000)
    assert nudge.load_models(sqlite_conn, "u1")["none"].n_pulls == 1


def test_decisions_older_than_the_window_expire_neutrally(sqlite_conn):
    context = base_context()
    stale_id = "evt-stale"
    db.insert_nudge_event(sqlite_conn, stale_id, "u1", "reflect", json.dumps(context), 1_000)

    now = 1_000 + nudge.ATTRIBUTION_WINDOW_MS + 60_000
    nudge.settle_pending_outcomes(sqlite_conn, "u1", score=95.0, baseline_mean=70.0, now_ms=now)

    event = db.get_nudge_event(sqlite_conn, stale_id)
    # Neutral, not rewarded: this session says nothing about a decision
    # made hours ago, and leaving it pending forever would be worse.
    assert event["settled_by"] == "expired"
    assert event["reward"] == pytest.approx(nudge.NEUTRAL_REWARD)


def test_no_baseline_yet_yields_a_neutral_reward(sqlite_conn):
    nudge.decide(sqlite_conn, "u1", base_context())
    settled = nudge.settle_pending_outcomes(sqlite_conn, "u1", score=88.0, baseline_mean=None)
    # "Better than usual" is undefined for a user with no usual.
    assert settled[0]["reward"] == pytest.approx(nudge.NEUTRAL_REWARD)


def test_already_settled_events_are_not_double_counted(sqlite_conn):
    decision = nudge.decide(sqlite_conn, "u1", base_context())
    nudge.record_feedback(sqlite_conn, "u1", decision["event_id"], "accepted")
    settled = nudge.settle_pending_outcomes(sqlite_conn, "u1", score=95.0, baseline_mean=70.0)
    assert settled == []
    assert nudge.load_models(sqlite_conn, "u1")[decision["arm"]].n_pulls == 1


def test_attribution_is_scoped_to_the_user(sqlite_conn):
    other = nudge.decide(sqlite_conn, "u2", base_context())
    nudge.settle_pending_outcomes(sqlite_conn, "u1", score=95.0, baseline_mean=70.0)
    assert db.get_nudge_event(sqlite_conn, other["event_id"])["reward"] is None


# ---------------------------------------------------------------------------
# Fatigue
# ---------------------------------------------------------------------------

def test_recent_nudge_count_excludes_the_none_arm(sqlite_conn):
    context = json.dumps(base_context())
    now = 10_000_000_000
    db.insert_nudge_event(sqlite_conn, "a", "u1", "none", context, now - 1000)
    db.insert_nudge_event(sqlite_conn, "b", "u1", "reflect", context, now - 1000)
    db.insert_nudge_event(sqlite_conn, "c", "u1", "pause", context, now - 1000)
    # Fatigue is about interruptions actually shown; 'none' shows nothing.
    assert nudge.recent_nudge_count(sqlite_conn, "u1", now_ms=now) == 2


def test_recent_nudge_count_ignores_events_older_than_a_day(sqlite_conn):
    context = json.dumps(base_context())
    now = 10_000_000_000
    db.insert_nudge_event(sqlite_conn, "old", "u1", "reflect", context, now - 25 * 60 * 60 * 1000)
    assert nudge.recent_nudge_count(sqlite_conn, "u1", now_ms=now) == 0


def test_learned_policy_shifts_toward_the_better_arm(sqlite_conn):
    """End-to-end sanity: consistent rewards change future decisions.

    Not asserting a specific arm wins — that would just restate the tie-
    break. Asserting that after one-sided evidence the model's expected
    reward for the rewarded arm is genuinely higher than for a punished one.
    """
    context = base_context()
    models = nudge.load_models(sqlite_conn, "u1")
    for _ in range(30):
        models["reflect"].update(context, reward=1.0)
        models["pause"].update(context, reward=0.0)
    nudge.save_model(sqlite_conn, "u1", "reflect", models["reflect"])
    nudge.save_model(sqlite_conn, "u1", "pause", models["pause"])

    result = nudge.decide(sqlite_conn, "u1", context)
    assert result["scores"]["reflect"]["expected"] > result["scores"]["pause"]["expected"]
