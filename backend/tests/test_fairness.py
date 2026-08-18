"""The score must react to what a student did, not to who they are.

The property under test is the project's central design claim: because
every judgement is made against a student's OWN baseline, a trait that
shifts their whole distribution — slower typing, a phone keyboard,
composing in a second language — should shift the baseline with it and
leave the score alone.

That claim was asserted in the README for a long time before anything
measured it. This is the measurement.
"""
import random

import pytest

import fairness


@pytest.mark.parametrize("trait", sorted(fairness.TRAITS))
def test_a_learner_trait_does_not_move_the_score_the_way_reliance_does(trait):
    rng = random.Random(20260816)
    table, levels, _rhythm_ok, penalties = fairness.sweep(trait, rng)
    trait_effect, reliance_effect, ratio = fairness.analyse(table, levels)

    # The sweep has to have exercised the rhythm penalty, or a 0.0 trait
    # effect means only that the trait-sensitive part of the pipeline was
    # switched off. Two earlier versions of the harness failed exactly
    # this way and looked like a clean result.
    assert any(p > 0 for p in penalties), (
        f"{trait}: the rhythm penalty never fired, so this sweep proves nothing")

    assert reliance_effect > 20, "reliance should dominate — check the generators"
    assert ratio < fairness.CONFOUND_RATIO, (
        f"{trait} moves the score by {trait_effect:.1f} points at fixed "
        f"reliance ({ratio:.0%} of the reliance effect) — the score is "
        "measuring the person rather than the behaviour")


def test_reliance_itself_still_moves_the_score_a_lot():
    """The control. If reliance stopped mattering, every fairness result
    above would pass trivially."""
    rng = random.Random(1)
    table, levels, _ok, _p = fairness.sweep("speed", rng)
    clean = table[(levels[0], 0.0)]
    heavy = table[(levels[0], 0.75)]
    assert clean - heavy > 40


def test_an_unmeasurable_rhythm_costs_nothing():
    """A very short session produces too few intervals for a rhythm
    reading. An absent signal must never be scored as a bad one — that is
    what would penalise anyone whose input method the histogram cannot
    describe."""
    rng = random.Random(2)
    short = fairness.session_length("very_short", 0.0, rng)
    score, features, penalty = fairness._score_with_history(
        fairness.session_length, "very_short", 0.0, rng)
    assert penalty == 0.0
    assert score is None or score > 0
