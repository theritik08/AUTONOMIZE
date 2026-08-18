"""Tests for personal-baseline anomaly detection and forecasting.

The headline behaviour under test is the one that motivated the module:
identical scores must produce different risk levels for different people,
because the comparison is to each person's own history. An absolute
threshold cannot do that, and the old implementation didn't.
"""
import pytest

import anomaly


def baseline(mean, var, n, last_score=None):
    return {
        "ema_mean": mean,
        "ema_var": var,
        "n_observations": n,
        "last_score": last_score if last_score is not None else mean,
    }


# ---------------------------------------------------------------------------
# personal_deviation
# ---------------------------------------------------------------------------

def test_no_baseline_is_reported_as_such_not_as_low_risk():
    result = anomaly.personal_deviation(50.0, None)
    assert result["status"] == "no_baseline"
    assert result["level"] is None
    assert result["z_score"] is None


def test_baseline_with_too_few_observations_is_not_used_for_zscores():
    result = anomaly.personal_deviation(20.0, baseline(90.0, 25.0, n=2))
    # A dramatic-looking drop, but on two observations there is no
    # trustworthy notion of "usual" yet.
    assert result["status"] == "insufficient_data"
    assert result["level"] is None
    assert result["n_observations"] == 2


def test_score_at_the_users_own_mean_is_low_risk():
    result = anomaly.personal_deviation(90.0, baseline(90.0, 25.0, n=20))
    assert result["status"] == "ok"
    assert result["z_score"] == pytest.approx(0.0)
    assert result["level"] == "low"


def test_large_drop_below_a_consistent_baseline_is_high_risk():
    # mean 90, sd 5 -> a 70 is four standard deviations down.
    result = anomaly.personal_deviation(70.0, baseline(90.0, 25.0, n=20))
    assert result["z_score"] == pytest.approx(-4.0)
    assert result["level"] == "high"


def test_moderate_drop_is_medium_risk():
    # mean 90, sd 5 -> 81 is -1.8 sigma, between the moderate and strong cutoffs.
    result = anomaly.personal_deviation(81.0, baseline(90.0, 25.0, n=20))
    assert result["level"] == "medium"


def test_scoring_far_above_your_own_baseline_is_never_flagged():
    result = anomaly.personal_deviation(100.0, baseline(40.0, 25.0, n=20))
    assert result["z_score"] > 0
    # A great day is not an integrity signal.
    assert result["level"] == "low"


def test_identical_scores_get_different_levels_for_different_people():
    """The whole point of the module, in one assertion.

    Two students both score 70. For the one who consistently works at 95
    that is a large personal deviation; for the one who consistently works
    at 68 it is a completely ordinary day.
    """
    high_achiever = anomaly.personal_deviation(70.0, baseline(95.0, 16.0, n=30))
    steady_worker = anomaly.personal_deviation(70.0, baseline(68.0, 16.0, n=30))
    assert high_achiever["level"] == "high"
    assert steady_worker["level"] == "low"


def test_near_zero_variance_does_not_produce_an_explosive_zscore():
    # Without a floor on the standard deviation, a one-point wobble against
    # a perfectly consistent history would read as dozens of sigma.
    result = anomaly.personal_deviation(99.0, baseline(100.0, 0.0, n=50))
    assert result["std_dev"] == anomaly.MIN_STD_DEV
    assert result["level"] == "low"
    assert abs(result["z_score"]) < 1.0


def test_negative_variance_from_float_drift_is_clamped():
    result = anomaly.personal_deviation(90.0, baseline(90.0, -1e-12, n=20))
    assert result["status"] == "ok"
    assert result["std_dev"] >= anomaly.MIN_STD_DEV


def test_missing_score_is_handled():
    assert anomaly.personal_deviation(None, baseline(90.0, 25.0, n=20))["status"] == "no_baseline"


# ---------------------------------------------------------------------------
# combined_risk
# ---------------------------------------------------------------------------

def test_combined_risk_takes_the_more_serious_of_the_two_signals():
    deviation = anomaly.personal_deviation(70.0, baseline(95.0, 16.0, n=30))  # personal: high
    level, driver = anomaly.combined_risk("low", deviation)                    # absolute: low
    assert (level, driver) == ("high", "personal")


def test_combined_risk_keeps_the_absolute_signal_when_it_is_worse():
    # Someone who always pastes everything has a stable low baseline, so
    # nothing is personally anomalous — but the absolute signal still says
    # this session was mostly pasted, and that shouldn't be hidden.
    deviation = anomaly.personal_deviation(20.0, baseline(20.0, 16.0, n=30))  # personal: low
    level, driver = anomaly.combined_risk("high", deviation)
    assert (level, driver) == ("high", "absolute")


def test_combined_risk_falls_back_to_absolute_without_enough_observations():
    deviation = anomaly.personal_deviation(20.0, baseline(90.0, 16.0, n=1))
    level, driver = anomaly.combined_risk("high", deviation)
    assert (level, driver) == ("high", "absolute")


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------

def test_explanation_mentions_the_personal_comparison_when_that_drove_it():
    deviation = anomaly.personal_deviation(70.0, baseline(95.0, 16.0, n=30))
    text = anomaly.explain("low", deviation, 70.0)
    assert "your own usual" in text
    assert "Visible to you only" in text


def test_explanation_says_it_is_still_learning_when_data_is_thin():
    deviation = anomaly.personal_deviation(70.0, baseline(95.0, 16.0, n=2))
    assert "Still learning" in anomaly.explain("low", deviation, 70.0)


def test_explanation_avoids_jargon():
    deviation = anomaly.personal_deviation(70.0, baseline(95.0, 16.0, n=30))
    text = anomaly.explain("low", deviation, 70.0)
    for jargon in ("z-score", "z_score", "sigma", "standard deviation"):
        assert jargon not in text.lower()


# ---------------------------------------------------------------------------
# forecast
# ---------------------------------------------------------------------------

def trend(scores):
    return [{"date": f"2026-08-{i + 1:02d}", "score": s} for i, s in enumerate(scores)]


def test_forecast_needs_a_minimum_of_history():
    assert anomaly.forecast(trend([50, 60, 70])) is None
    assert anomaly.forecast([]) is None
    assert anomaly.forecast(None) is None


def test_forecast_detects_an_improving_trend():
    result = anomaly.forecast(trend([50, 55, 60, 65, 70, 75]))
    assert result["direction"] == "improving"
    assert result["slope_per_day"] == pytest.approx(5.0)
    assert result["r2"] == pytest.approx(1.0)


def test_forecast_detects_a_declining_trend():
    result = anomaly.forecast(trend([90, 85, 80, 75, 70]))
    assert result["direction"] == "declining"
    assert result["slope_per_day"] < 0


def test_forecast_withholds_a_projection_the_line_does_not_describe():
    """A noisy flat series has a near-zero r-squared, so there is no trend
    to project. The projection is now withheld server-side rather than
    returned with a low r-squared and a note to the client — any consumer
    other than the dashboard was previously handed an unguarded number.
    """
    result = anomaly.forecast(trend([70, 71, 70, 69, 70, 70]))
    assert result["r2"] < anomaly.MIN_R2_FOR_PROJECTION
    assert result["projected_score"] is None
    assert result["direction"] == "unclear"


def test_a_perfectly_flat_series_is_a_perfect_fit_not_a_failed_one():
    """Distinguishes "no trend" from "no fit".

    Every point identical means zero total variance, which the r-squared
    formula cannot divide by. Reporting 0.0 there treated a horizontal line
    through identical points as a failed fit and withheld the projection —
    so a student pinned at 100 all week was told their trend was unclear.
    A horizontal line through identical points fits perfectly.
    """
    result = anomaly.forecast(trend([70, 70, 70, 70, 70, 70]))
    assert result["slope_per_day"] == 0.0
    assert result["r2"] == 1.0
    assert result["direction"] == "steady"
    assert result["projected_score"] == 70.0


def test_forecast_is_clamped_to_the_score_range():
    # A steep climb projected 7 days out would exceed 100 unclamped.
    result = anomaly.forecast(trend([60, 70, 80, 90, 100]), horizon_days=7)
    assert 0.0 <= result["projected_score"] <= 100.0

    crash = anomaly.forecast(trend([40, 30, 20, 10, 0]), horizon_days=7)
    assert crash["projected_score"] >= 0.0


def test_forecast_reports_low_r2_for_noisy_data():
    noisy = anomaly.forecast(trend([10, 95, 20, 88, 15, 91, 25]))
    # The caller uses r2 to decide whether drawing a projection is honest.
    assert noisy["r2"] < 0.3


def test_forecast_ignores_null_scores():
    points = trend([50, 55, 60, 65, 70])
    points.append({"date": "2026-08-06", "score": None})
    result = anomaly.forecast(points)
    assert result["points_used"] == 5
