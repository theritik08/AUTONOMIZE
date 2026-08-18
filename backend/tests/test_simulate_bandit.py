"""Tests for simulate_bandit.py.

A simulation that silently produces a flattering number is worse than no
simulation, so these check the harness itself rather than the headline
result: that it is reproducible, that regret is non-negative and
non-decreasing, that a learning policy actually beats random, and — the one
that caught a real bug — that the ablation measures against the right
oracle.
"""
import pytest

import nudge
import simulate_bandit as sim


ARMS = list(nudge.ARMS)


def test_the_simulation_is_reproducible_from_a_seed():
    """A bandit comparison that moves between runs is not evidence."""
    a = sim.run_once(sim.policy_linucb(1.0), ARMS, 200, seed=99)
    b = sim.run_once(sim.policy_linucb(1.0), ARMS, 200, seed=99)
    assert a == b


def test_different_seeds_give_different_trajectories():
    a = sim.run_once(sim.policy_linucb(1.0), ARMS, 200, seed=1)
    b = sim.run_once(sim.policy_linucb(1.0), ARMS, 200, seed=2)
    assert a != b


def test_thompson_sampling_is_reproducible_too():
    """It draws from the RNG, so it is the policy most likely to leak
    nondeterminism — via dict ordering, if arms were not sorted."""
    a = sim.run_once(sim.policy_thompson(1.0), ARMS, 200, seed=5)
    b = sim.run_once(sim.policy_thompson(1.0), ARMS, 200, seed=5)
    assert a == b


def test_regret_is_non_negative_and_non_decreasing():
    """Cumulative regret against an oracle cannot go down. If it does, the
    oracle is not actually optimal and every number here is meaningless."""
    curve = sim.run_once(sim.policy_linucb(1.0), ARMS, 400, seed=7)
    assert curve[0] >= 0.0
    assert all(b >= a - 1e-9 for a, b in zip(curve, curve[1:]))


def test_the_oracle_has_exactly_zero_regret():
    """Sanity check on the measurement itself."""
    def oracle(models, context, rng):
        return sim.best_arm(context, ARMS)
    curve = sim.run_once(oracle, ARMS, 300, seed=3)
    assert curve[-1] == pytest.approx(0.0, abs=1e-9)


def test_learning_beats_random_by_a_wide_margin():
    rounds, runs = 600, 4
    learned = sim.average_curves(sim.policy_linucb(1.0), ARMS, rounds, runs, 21)
    random_policy = sim.average_curves(sim.policy_random, ARMS, rounds, runs, 21)
    assert learned[-1] < random_policy[-1] / 2


def test_linucb_beats_pure_greedy_exploration_free():
    """epsilon=0 is greedy on the point estimate with no uncertainty term,
    which isolates what the confidence bonus is worth."""
    rounds, runs = 800, 4
    ucb = sim.average_curves(sim.policy_linucb(1.0), ARMS, rounds, runs, 33)
    greedy = sim.average_curves(sim.policy_epsilon(0.0), ARMS, rounds, runs, 33)
    assert ucb[-1] < greedy[-1]


def test_the_none_ablation_measures_against_the_full_oracle():
    """Regression test for a real bug in this harness.

    The first version removed `none` from the policy *and* from the oracle,
    so the crippled policy was scored against a handicapped optimum and
    appeared to improve. The oracle must keep every arm — the question is
    what the policy loses by being unable to stay quiet.
    """
    rounds, runs = 500, 3
    full = sim.average_curves(sim.policy_linucb(1.0), ARMS, rounds, runs, 44)
    reduced = [a for a in ARMS if a != "none"]
    without = sim.average_curves(sim.policy_linucb(1.0), reduced, rounds, runs, 44,
                                 oracle_arms=ARMS)
    assert without[-1] > full[-1], "removing the 'none' arm must cost regret"


def test_none_is_the_best_arm_in_most_contexts():
    """The simulated world has to have the structure the docstring claims,
    or the ablation above is testing nothing."""
    import random
    rng = random.Random(2)
    wins = sum(1 for _ in range(2000)
               if sim.best_arm(sim.sample_context(rng), ARMS) == "none")
    assert wins / 2000 > 0.5


def test_the_optimal_arm_actually_depends_on_context():
    """Otherwise a non-contextual bandit would do just as well and the
    whole LinUCB apparatus is unjustified."""
    import random
    rng = random.Random(8)
    chosen = {sim.best_arm(sim.sample_context(rng), ARMS) for _ in range(2000)}
    assert len(chosen) >= 2


def test_reward_probabilities_stay_in_range():
    import random
    rng = random.Random(13)
    for _ in range(500):
        context = sim.sample_context(rng)
        for arm in ARMS:
            assert 0.0 <= sim.true_reward_prob(arm, context) <= 1.0
