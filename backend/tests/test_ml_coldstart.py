"""Cold start must never manufacture confidence.

The failure this file guards against is subtle and would never crash: a new
student is shown a number computed almost entirely from a population prior,
labelled as if it were about them. Every test here is about the labelling
being honest, not about the arithmetic being clever.
"""
import random

import pytest

from ml import coldstart


def session(i, score, started_at, user="u1", category="writing"):
    return {
        "session_id": f"{user}-{i}", "user_id": user, "category": category,
        "started_at": started_at, "active_ms": 25 * 60_000,
        "typed_chars": 800, "pasted_chars": 200, "backspace_count": 30,
        "revision_count": 4, "likely_ai_pastes": 0, "tab_switch_count": 0,
        "regularity": 0.4, "score": score,
    }


def population(n_users=20, n_sessions=15, seed=5):
    rng = random.Random(seed)
    rows = []
    for u in range(n_users):
        personal = rng.uniform(50, 90)
        for i in range(n_sessions):
            rows.append(session(i, personal + rng.gauss(0, 5),
                                1_700_000_000_000 + i * 86_400_000,
                                user=f"u{u}"))
    return rows


# ---------------------------------------------------------------------------
# The prior itself
# ---------------------------------------------------------------------------

def test_a_prior_is_refused_when_there_are_too_few_users():
    """Four people's means are not a population and should not be called one."""
    assert coldstart.estimate_prior(population(n_users=4)) == {}


def test_the_prior_separates_the_two_categories():
    rows = population(n_users=15)
    rows += [session(i, 30.0, 1_700_000_000_000 + i * 86_400_000,
                     user=f"u{u}", category="assessment")
             for u in range(15) for i in range(6)]
    prior = coldstart.estimate_prior(rows)
    assert set(prior) == {"writing", "assessment"}
    # Pooling them would produce a mean that describes neither.
    assert prior["assessment"]["mean"] < prior["writing"]["mean"] - 10


def test_k_falls_out_of_the_two_variances_rather_than_being_chosen():
    """When students differ a lot from each other, personal history should
    take over quickly — a small k. When they are all alike, slowly."""
    spread_out = coldstart.estimate_prior(population(n_users=25, seed=1))

    rng = random.Random(2)
    alike = []
    for u in range(25):
        for i in range(15):
            # Same underlying mean for everyone; all the variance is noise.
            alike.append(session(i, 70 + rng.gauss(0, 12),
                                 1_700_000_000_000 + i * 86_400_000,
                                 user=f"u{u}"))
    alike_prior = coldstart.estimate_prior(alike)

    assert alike_prior["writing"]["k"] > spread_out["writing"]["k"]


# ---------------------------------------------------------------------------
# The blend
# ---------------------------------------------------------------------------

def test_with_no_history_the_answer_is_the_population_and_says_so():
    prior = {"mean": 70.0, "k": 8.0}
    out = coldstart.shrink(personal_mean=None, n_observations=0, prior=prior)
    assert out["estimate"] == 70.0
    assert out["personal_weight"] == 0.0
    assert out["source"] == "population_prior"


def test_the_personal_share_rises_monotonically_with_history():
    prior = {"mean": 70.0, "k": 8.0}
    weights = [coldstart.shrink(90.0, n, prior)["personal_weight"]
               for n in range(0, 40)]
    assert weights == sorted(weights)
    assert weights[0] == 0.0
    assert weights[-1] > 0.8


def test_the_estimate_sits_between_the_two_sources():
    prior = {"mean": 70.0, "k": 8.0}
    out = coldstart.shrink(90.0, 4, prior)
    assert 70.0 < out["estimate"] < 90.0


def test_with_no_prior_it_degrades_to_the_behaviour_that_existed_before():
    """The blend is a bonus, never a dependency."""
    out = coldstart.shrink(84.0, 6, prior=None)
    assert out["estimate"] == 84.0
    assert out["personal_weight"] == 1.0
    assert out["source"] == "personal_only"


# ---------------------------------------------------------------------------
# The honesty of the labels — the part that actually matters
# ---------------------------------------------------------------------------

def test_a_brand_new_student_is_marked_insufficient_not_confident():
    state = coldstart.readiness(0)
    assert state["insufficient_data"] is True
    assert state["confidence"] == "learning"
    assert state["reliability"] == 0.0


def test_the_warm_up_counter_is_a_progress_bar_not_a_verdict():
    state = coldstart.readiness(2)
    assert state["warm_up"] == {"have": 2, "need": coldstart.MIN_PERSONAL_OBSERVATIONS,
                                "settled_at": coldstart.SETTLED_OBSERVATIONS}


def test_confidence_only_reaches_established_with_both_history_and_weight():
    prior = {"mean": 70.0, "k": 8.0}
    # Plenty of sessions but a huge k would keep reliability low.
    heavy_prior = {"mean": 70.0, "k": 50.0}
    settled = coldstart.readiness(30, coldstart.shrink(80.0, 30, prior))
    borrowed = coldstart.readiness(30, coldstart.shrink(80.0, 30, heavy_prior))
    assert settled["confidence"] == "established"
    assert borrowed["confidence"] != "established"


def test_the_message_never_implies_another_student_was_seen():
    for n in (0, 3, 7, 40):
        message = coldstart.explain(coldstart.readiness(n)).lower()
        for phrase in ("other students", "your class", "classmates",
                       "compared to others", "your teacher", "average student"):
            assert phrase not in message


def test_the_learning_message_states_the_borrowing_out_loud():
    message = coldstart.explain(coldstart.readiness(1))
    assert "not to you" in message


def test_reliability_is_a_share_not_a_score():
    prior = {"mean": 70.0, "k": 8.0}
    for n in (0, 1, 5, 20, 100):
        state = coldstart.readiness(n, coldstart.shrink(80.0, n, prior))
        assert 0.0 <= state["reliability"] <= 1.0


def test_a_degenerate_prior_cannot_freeze_personal_history_out_forever():
    """k is clamped, so a pathological variance estimate cannot mean 'this
    student's own data never counts'."""
    rng = random.Random(9)
    # Everyone identical: between-user variance ~0, so raw k -> infinity.
    rows = [session(i, 70.0 + rng.gauss(0, 15),
                    1_700_000_000_000 + i * 86_400_000, user=f"u{u}")
            for u in range(20) for i in range(12)]
    prior = coldstart.estimate_prior(rows)["writing"]
    assert prior["k"] <= 50.0
    assert coldstart.shrink(90.0, 200, prior)["personal_weight"] > 0.75


def test_shrinkage_matches_its_closed_form():
    prior = {"mean": 60.0, "k": 4.0}
    out = coldstart.shrink(80.0, 4, prior)
    expected_weight = 4 / (4 + 4)
    assert out["personal_weight"] == pytest.approx(expected_weight)
    assert out["estimate"] == pytest.approx(
        expected_weight * 80.0 + (1 - expected_weight) * 60.0, abs=0.05)
