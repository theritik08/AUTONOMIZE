"""Tests for conformal.py — distribution-free calibrated flagging.

The property worth testing is not "does it flag the obvious case" but the
guarantee itself: under exchangeable data, the flag rate is at most alpha,
whatever the underlying distribution. That is checked empirically against
three distributions with very different shapes, including two the z-score
would handle badly.
"""
import random

import pytest

import anomaly
import conformal


def test_empty_window_cannot_flag():
    assert conformal.assess(50.0, [], 80.0)["status"] == "no_calibration"


def test_below_minimum_calibration_declines_to_answer():
    window = [80.0] * (conformal.MIN_CALIBRATION - 1)
    verdict = conformal.assess(10.0, window, 80.0)
    assert verdict["status"] == "insufficient_data"
    assert verdict["level"] is None


def test_minimum_calibration_is_the_smallest_n_that_can_reach_alpha():
    """With n points the smallest reachable p-value is 1/(n+1). Below
    MIN_CALIBRATION, ALPHA_STRONG is unreachable and flagging at that level
    would be a lie about the guarantee."""
    n = conformal.MIN_CALIBRATION
    assert 1.0 / (n + 1) <= conformal.ALPHA_STRONG
    assert 1.0 / n > conformal.ALPHA_STRONG


def test_a_much_worse_session_gets_the_smallest_possible_p():
    window = [float(80 + i % 5) for i in range(40)]
    verdict = conformal.assess(5.0, window, 82.0)
    assert verdict["status"] == "ok"
    assert verdict["level"] == "high"
    # assess() rounds to 4dp for serialisation; compare at that precision.
    assert verdict["p_value"] == pytest.approx(1.0 / (len(window) + 1), abs=1e-4)


def test_a_typical_session_is_not_flagged():
    window = [float(80 + i % 5) for i in range(40)]
    assert conformal.assess(82.0, window, 82.0)["level"] == "low"


def test_scoring_above_your_norm_is_never_flagged():
    """One-sided: nonconformity floors at zero, so a great session ties with
    every other great session and lands at p = 1."""
    window = [float(70 + i % 7) for i in range(40)]
    verdict = conformal.assess(99.0, window, 73.0)
    assert verdict["level"] == "low"
    assert verdict["p_value"] == pytest.approx(1.0)


@pytest.mark.parametrize("draw,label", [
    (lambda rng: rng.gauss(75, 8), "normal"),
    (lambda rng: 100 - rng.expovariate(1 / 12.0), "heavy left tail"),
    (lambda rng: 100 * rng.betavariate(9, 2), "bounded and skewed"),
])
def test_flag_rate_is_bounded_by_alpha_whatever_the_distribution(draw, label):
    """The guarantee, checked empirically.

    This is the whole reason the module exists: the same alpha holds across
    a normal, a heavy-tailed and a bounded-skewed distribution, none of
    which the z-score's sigma multiples would treat consistently.
    """
    rng = random.Random(4242)
    trials, flags = 3000, 0

    for _ in range(trials):
        sample = [max(0.0, min(100.0, draw(rng))) for _ in range(41)]
        window, new = sample[:-1], sample[-1]
        reference = sum(window) / len(window)
        verdict = conformal.assess(new, window, reference)
        if verdict["status"] == "ok" and verdict["level"] == "high":
            flags += 1

    rate = flags / trials
    # The guarantee is an upper bound; a little slack for Monte-Carlo error.
    assert rate <= conformal.ALPHA_STRONG + 0.02, f"{label}: flag rate {rate:.3f}"


def test_window_is_capped_and_ordered_oldest_first():
    window = []
    raw = None
    for i in range(conformal.WINDOW_SIZE + 25):
        window = conformal.push_window(raw, float(i))
        raw = conformal.dump_window(window)
    assert len(window) == conformal.WINDOW_SIZE
    assert window[-1] == float(conformal.WINDOW_SIZE + 24)
    assert window == sorted(window)


def test_corrupt_window_degrades_to_empty_rather_than_raising():
    for bad in ("not json", "{}", '["a","b"]', None, ""):
        assert conformal.load_window(bad) == []


def test_push_ignores_a_none_score():
    raw = conformal.dump_window([1.0, 2.0])
    assert conformal.push_window(raw, None) == [1.0, 2.0]


# ---------------------------------------------------------------------------
# Integration with anomaly.py
# ---------------------------------------------------------------------------

def _baseline(mean, var, n):
    return {"ema_mean": mean, "ema_var": var, "n_observations": n}


def test_conformal_decides_once_it_has_calibration_data():
    window = [float(80 + i % 5) for i in range(40)]
    deviation = anomaly.calibrated_deviation(20.0, _baseline(82.0, 9.0, 30), window)
    assert deviation["decided_by"] == "conformal"
    assert deviation["level"] == "high"
    # The z-score is still reported — it is the effect size, not the decision.
    assert deviation["z_score"] is not None


def test_z_score_still_decides_before_calibration_exists():
    deviation = anomaly.calibrated_deviation(40.0, _baseline(80.0, 25.0, 30), [])
    assert deviation["decided_by"] == "z_score"
    assert deviation["conformal"]["status"] == "no_calibration"


def test_a_wide_history_no_longer_flags_a_large_absolute_gap():
    """The case the z-score got wrong.

    A user whose scores genuinely range over 40 points has a large sigma,
    so a 20-point drop is unremarkable for them — but it is also a large
    absolute gap. Conformal ranks it against their own history and
    correctly leaves it alone.
    """
    rng = random.Random(11)
    window = [float(max(0, min(100, rng.gauss(60, 20)))) for _ in range(50)]
    deviation = anomaly.calibrated_deviation(45.0, _baseline(60.0, 400.0, 40), window)
    assert deviation["conformal"]["status"] == "ok"
    assert deviation["level"] == "low"


def test_explanation_reports_a_rate_not_a_sigma():
    window = [float(80 + i % 5) for i in range(40)]
    deviation = anomaly.calibrated_deviation(15.0, _baseline(82.0, 9.0, 30), window)
    text = anomaly.explain("low", deviation, 15.0)
    assert "lowest" in text and "%" in text
    assert "sigma" not in text.lower() and "z-score" not in text.lower()
