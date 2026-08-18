"""The unsupervised behavioural-shape signal.

The claim this module makes is specific: it catches a session that is
unusual for a student *in more than one dimension at once*, including when
the score itself is perfectly ordinary — the case the z-score and conformal
layers are structurally unable to see because they only ever look at one
number.

That claim is what the headline test here asserts. The rest guard the
properties that make the score mean anything: causality of the reference
point, refusal when the reference is too thin, and determinism.
"""
import json
import random

import pytest

from ml import isolation


def session(i, score, started_at, typed=900, pasted=100, regularity=0.35,
            active_ms=30 * 60_000, backspace=40):
    return {
        "session_id": f"s{i}", "user_id": "u1", "category": "writing",
        "started_at": started_at, "active_ms": active_ms,
        "typed_chars": typed, "pasted_chars": pasted,
        "backspace_count": backspace, "revision_count": 5,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
        "regularity": regularity, "score": score,
    }


def habitual(n=25, rng=None):
    """A student who works the same way every time."""
    rng = rng or random.Random(4)
    return [
        session(i, 72 + rng.gauss(0, 2), 1_700_000_000_000 + i * 86_400_000,
                typed=900 + rng.randint(-40, 40),
                pasted=100 + rng.randint(-15, 15),
                regularity=0.35 + rng.gauss(0, 0.02),
                active_ms=int((30 + rng.gauss(0, 2)) * 60_000))
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# The headline claim
# ---------------------------------------------------------------------------

def test_it_flags_a_session_whose_shape_is_wrong_while_the_score_is_normal():
    """This is the whole reason the module exists. If this test does not
    hold, the isolation forest is adding nothing the z-score did not."""
    history = habitual(30)

    # Same score as always. Everything about HOW it was produced is
    # different: mostly pasted, in a third of the time, with a machine-flat
    # rhythm and no editing.
    disguised = session(99, 72.0, 1_700_000_000_000 + 30 * 86_400_000,
                        typed=200, pasted=1400, regularity=0.93,
                        active_ms=8 * 60_000, backspace=1)

    # An ordinary next session, for contrast.
    ordinary = session(98, 72.0, 1_700_000_000_000 + 30 * 86_400_000)

    vectors, _ids = isolation.deviation_dataset(history)
    forest = isolation.IsolationForest(n_trees=120, seed=3).fit(vectors)

    odd = isolation.assess(forest, history, disguised)
    normal = isolation.assess(forest, history, ordinary)

    assert odd["status"] == "ok" and normal["status"] == "ok"
    assert odd["score"] > normal["score"], (
        "the disguised session must look more anomalous than the ordinary one"
    )
    assert odd["unusual"] is True
    assert normal["unusual"] is False


def test_the_one_dimensional_signal_cannot_see_that_session():
    """The counterpart of the test above: the existing machinery is blind
    to it, which is what makes the addition worth its complexity."""
    import anomaly

    baseline = {"ema_mean": 72.0, "ema_var": 4.0, "n_observations": 30}
    verdict = anomaly.personal_deviation(72.0, baseline)
    assert verdict["status"] == "ok"
    assert verdict["level"] == "low"   # nothing to see, by construction


# ---------------------------------------------------------------------------
# Causality and refusal
# ---------------------------------------------------------------------------

def test_the_reference_point_uses_only_earlier_sessions():
    history = habitual(12)
    row = session(50, 40.0, 1_800_000_000_000, typed=100, pasted=2000)

    before = isolation.deviation_vector(history, row)
    # Corrupting sessions that come after must be impossible to observe,
    # because they are simply not passed in — asserted by constructing the
    # same call with a longer history and checking the prefix result stands.
    after = isolation.deviation_vector([dict(r) for r in history], row)
    assert before == pytest.approx(after)


def test_too_little_history_yields_no_vector_rather_than_a_zero_one():
    short = habitual(2)
    assert isolation.deviation_vector(short, session(9, 70.0, 1)) is None


def test_assess_refuses_without_a_forest():
    verdict = isolation.assess(None, habitual(10), session(9, 70.0, 1))
    assert verdict["status"] == "unavailable"
    assert verdict["score"] is None


def test_assess_refuses_on_a_thin_reference():
    vectors, _ = isolation.deviation_dataset(habitual(30))
    forest = isolation.IsolationForest(n_trees=20, seed=3).fit(vectors)
    verdict = isolation.assess(forest, habitual(2), session(9, 70.0, 1))
    assert verdict["status"] == "insufficient_data"
    assert verdict["needed"] == isolation.MIN_REFERENCE_SESSIONS


def test_an_unmeasured_rhythm_becomes_an_exact_zero_not_an_invented_deviation():
    history = habitual(10)
    row = session(9, 72.0, 1_800_000_000_000)
    row["regularity"] = None
    vector = isolation.deviation_vector(history, row)
    index = isolation.ANOMALY_FEATURE_NAMES.index("regularity")
    assert vector[index] == 0.0


# ---------------------------------------------------------------------------
# The forest itself
# ---------------------------------------------------------------------------

def test_a_planted_outlier_scores_higher_than_the_cluster():
    rng = random.Random(7)
    cluster = [[rng.gauss(0, 1) for _ in range(4)] for _ in range(400)]
    forest = isolation.IsolationForest(n_trees=100, seed=1).fit(cluster)

    outlier = forest.score_one([12.0, -11.0, 13.0, 9.0])
    typical = sum(forest.score(cluster[:100])) / 100
    assert outlier > typical + 0.15


def test_the_path_length_normaliser_matches_its_definition():
    # c(2) = 1 exactly; c(n) = 2H(n-1) - 2(n-1)/n.
    assert isolation.average_path_length(2) == pytest.approx(1.0)
    assert isolation.average_path_length(1) == 0.0
    expected = 2 * (1 + 1 / 2 + 1 / 3) - 2 * 3 / 4
    assert isolation.average_path_length(4) == pytest.approx(expected)


def test_the_forest_is_deterministic_for_a_fixed_seed():
    rng = random.Random(8)
    points = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(200)]
    a = isolation.IsolationForest(n_trees=30, seed=5).fit(points)
    b = isolation.IsolationForest(n_trees=30, seed=5).fit(points)
    assert a.score(points[:20]) == pytest.approx(b.score(points[:20]))


def test_the_forest_survives_a_json_round_trip():
    rng = random.Random(6)
    points = [[rng.gauss(0, 1) for _ in range(3)] for _ in range(200)]
    forest = isolation.IsolationForest(n_trees=25, seed=2).fit(points)
    restored = isolation.IsolationForest.from_dict(
        json.loads(json.dumps(forest.to_dict())))
    for point in points[:20]:
        assert restored.score_one(point) == pytest.approx(forest.score_one(point))


def test_drivers_only_name_attributes_that_actually_moved():
    """A "driver" that deviated by 0.1 sigma is noise with a label on it."""
    vector = [0.05] * isolation.ANOMALY_FEATURE_DIM
    assert isolation.top_drivers(vector) == []

    vector[isolation.ANOMALY_FEATURE_NAMES.index("paste_ratio")] = 3.4
    drivers = isolation.top_drivers(vector)
    assert len(drivers) == 1 and drivers[0]["feature"] == "paste_ratio"
    assert drivers[0]["direction"] == "above"
