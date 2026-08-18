"""Tests for the LinUCB implementation — pure maths, no database.

Split into three concerns: the linear algebra is correct, the UCB score
behaves the way the algorithm requires (uncertainty shrinks with
evidence), and selection actually learns from rewards.
"""
import math

import pytest

import bandit

D = 4


# ---------------------------------------------------------------------------
# Linear algebra
# ---------------------------------------------------------------------------

def _matmul(a, b):
    n, m, p = len(a), len(b), len(b[0])
    return [[sum(a[i][k] * b[k][j] for k in range(m)) for j in range(p)] for i in range(n)]


def test_identity_is_its_own_inverse():
    inverse = bandit.invert(bandit.identity(D))
    for i in range(D):
        for j in range(D):
            assert inverse[i][j] == pytest.approx(1.0 if i == j else 0.0, abs=1e-12)


def test_invert_produces_a_true_inverse():
    matrix = [
        [4.0, 1.0, 0.0, 0.0],
        [1.0, 3.0, 1.0, 0.0],
        [0.0, 1.0, 5.0, 2.0],
        [0.0, 0.0, 2.0, 6.0],
    ]
    product = _matmul(matrix, bandit.invert(matrix))
    for i in range(D):
        for j in range(D):
            assert product[i][j] == pytest.approx(1.0 if i == j else 0.0, abs=1e-9)


def test_invert_raises_on_a_singular_matrix():
    singular = [[1.0, 2.0], [2.0, 4.0]]  # second row is 2x the first
    with pytest.raises(ValueError):
        bandit.invert(singular)


def test_invert_handles_a_matrix_needing_pivoting():
    # A zero in the leading position forces a row swap; without partial
    # pivoting this divides by zero.
    matrix = [[0.0, 1.0], [1.0, 0.0]]
    product = _matmul(matrix, bandit.invert(matrix))
    assert product[0][0] == pytest.approx(1.0)
    assert product[1][1] == pytest.approx(1.0)


def test_outer_add_adds_the_outer_product():
    base = [[0.0, 0.0], [0.0, 0.0]]
    result = bandit.outer_add(base, [2.0, 3.0])
    assert result == [[4.0, 6.0], [6.0, 9.0]]


def test_dot_and_mat_vec():
    assert bandit.dot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == pytest.approx(32.0)
    assert bandit.mat_vec([[1.0, 0.0], [0.0, 2.0]], [3.0, 4.0]) == [3.0, 8.0]


# ---------------------------------------------------------------------------
# Arm behaviour
# ---------------------------------------------------------------------------

def test_fresh_arm_predicts_zero_reward_but_has_an_exploration_bonus():
    arm = bandit.ArmModel.fresh(D)
    context = [1.0, 0.5, 0.0, 0.0]
    scored = arm.score(context)
    # theta is zero, so no evidence-based expectation yet...
    assert scored["expected"] == pytest.approx(0.0)
    # ...but the arm is maximally uncertain, which is what makes it worth trying.
    assert scored["bonus"] > 0
    assert scored["ucb"] == pytest.approx(scored["bonus"])


def test_uncertainty_bonus_shrinks_as_an_arm_is_played_in_the_same_context():
    arm = bandit.ArmModel.fresh(D)
    context = [1.0, 0.5, 0.2, 0.1]
    before = arm.score(context)["bonus"]
    for _ in range(10):
        arm.update(context, reward=1.0)
    after = arm.score(context)["bonus"]
    assert after < before


def test_uncertainty_stays_high_for_a_context_the_arm_has_not_seen():
    arm = bandit.ArmModel.fresh(D)
    seen = [1.0, 1.0, 0.0, 0.0]
    unseen = [0.0, 0.0, 0.0, 1.0]
    unseen_before = arm.score(unseen)["bonus"]
    for _ in range(25):
        arm.update(seen, reward=1.0)
    # Evidence in one direction of feature space must not be mistaken for
    # evidence in an orthogonal one — this is the whole point of a
    # *contextual* bandit over a plain multi-armed one.
    assert arm.score(unseen)["bonus"] == pytest.approx(unseen_before, rel=0.05)
    assert arm.score(seen)["bonus"] < unseen_before


def test_expected_reward_moves_toward_observed_rewards():
    arm = bandit.ArmModel.fresh(D)
    context = [1.0, 0.0, 0.0, 0.0]
    for _ in range(30):
        arm.update(context, reward=1.0)
    assert arm.score(context)["expected"] > 0.8

    punished = bandit.ArmModel.fresh(D)
    for _ in range(30):
        punished.update(context, reward=0.0)
    assert punished.score(context)["expected"] == pytest.approx(0.0, abs=1e-9)


def test_update_increments_pull_count():
    arm = bandit.ArmModel.fresh(D)
    arm.update([1.0, 0.0, 0.0, 0.0], 1.0)
    arm.update([1.0, 0.0, 0.0, 0.0], 0.0)
    assert arm.n_pulls == 2


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_select_arm_is_deterministic_for_identical_untried_arms():
    models = {name: bandit.ArmModel.fresh(D) for name in ("b_arm", "a_arm", "c_arm")}
    context = [1.0, 0.3, 0.3, 0.3]
    picks = {bandit.select_arm(models, context)["arm"] for _ in range(10)}
    # All arms are identical, so the tie-break decides — and it must be
    # stable, or the same state would produce different decisions run to run.
    assert picks == {"a_arm"}


def test_select_arm_prefers_the_arm_with_better_observed_reward():
    context = [1.0, 0.5, 0.5, 0.5]
    good = bandit.ArmModel.fresh(D)
    bad = bandit.ArmModel.fresh(D)
    for _ in range(40):
        good.update(context, reward=1.0)
        bad.update(context, reward=0.0)
    assert bandit.select_arm({"good": good, "bad": bad}, context)["arm"] == "good"


def test_select_arm_explores_an_untried_arm_over_a_mediocre_known_one():
    context = [1.0, 0.5, 0.5, 0.5]
    known = bandit.ArmModel.fresh(D)
    for _ in range(15):
        known.update(context, reward=0.2)  # consistently mediocre
    untried = bandit.ArmModel.fresh(D)
    assert bandit.select_arm({"known": known, "untried": untried}, context)["arm"] == "untried"


def test_alpha_zero_reduces_to_pure_exploitation():
    context = [1.0, 0.5, 0.5, 0.5]
    known = bandit.ArmModel.fresh(D)
    for _ in range(15):
        known.update(context, reward=0.2)
    untried = bandit.ArmModel.fresh(D)
    # With no exploration bonus the mediocre-but-positive arm wins, which
    # confirms the previous test's result came from the bonus term and not
    # from something incidental.
    result = bandit.select_arm({"known": known, "untried": untried}, context, alpha=0.0)
    assert result["arm"] == "known"


def test_select_arm_reports_every_arms_score():
    models = {"a": bandit.ArmModel.fresh(D), "b": bandit.ArmModel.fresh(D)}
    result = bandit.select_arm(models, [1.0, 0.0, 0.0, 0.0])
    assert set(result["scores"]) == {"a", "b"}
    for scored in result["scores"].values():
        assert {"ucb", "expected", "bonus"} == set(scored)
        assert scored["ucb"] == pytest.approx(scored["expected"] + scored["bonus"])


def test_ucb_never_returns_nan_for_a_zero_context():
    # A degenerate all-zero context must not produce sqrt(-epsilon).
    arm = bandit.ArmModel.fresh(D)
    scored = arm.score([0.0] * D)
    assert not math.isnan(scored["ucb"])
    assert scored["bonus"] == pytest.approx(0.0)
