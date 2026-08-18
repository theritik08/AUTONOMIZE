r"""Metrics, free baselines and calibration checks.

THE BASELINE IS THE RESULT
--------------------------

An MAE of 5.8 means nothing on its own. It means something the moment it
sits next to the MAE of predicting the student's own running average, which
costs nothing and requires no model at all. On a strongly autocorrelated
series like this one, "assume tomorrow looks like today" is a genuinely
strong predictor, and a learned model that fails to beat it has not earned
the complexity it adds — that is a real possible outcome here rather than a
formality, and `training.py` says so in those words when it happens.

So the baselines are computed first, printed first, and stored in the model
file alongside the model's own metrics. Anyone reading the artefact later
can see what the model was actually worth.

WHY MAE LEADS
-------------

RMSE and R-squared are both reported, but MAE is the one decisions are made
on. The prediction becomes a sentence shown to a student — "you look to be
heading for around 74" — and the error a student experiences is the
absolute one. RMSE's extra weight on large errors is answering a different
question, and R-squared on a time series with a strong trend flatters
everything.
"""
import math


def metrics(actual, predicted):
    """MAE, RMSE, R-squared and n. Safe on empty input."""
    n = len(actual)
    if n == 0:
        return {"mae": None, "rmse": None, "r2": None, "n": 0}
    mae = sum(abs(a - p) for a, p in zip(actual, predicted)) / n
    rmse = math.sqrt(sum((a - p) ** 2 for a, p in zip(actual, predicted)) / n)
    mean = sum(actual) / n
    ss_tot = sum((a - mean) ** 2 for a in actual)
    ss_res = sum((a - p) ** 2 for a, p in zip(actual, predicted))
    # ss_tot == 0 means every label is identical, so a constant prediction
    # is perfect and R-squared is undefined rather than zero. Reported as
    # None so nobody quotes a 0.0 that means "undefined".
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    return {"mae": mae, "rmse": rmse, "r2": r2, "n": n}


def baseline_predictions(xs, feature_names):
    """Each free baseline's predictions on the same rows.

    All three read straight out of the feature vector, which is the point:
    they are information the product already has, so beating them is the
    minimum bar for the model being worth shipping.
    """
    i_last = feature_names.index("last_score")
    i_ema = feature_names.index("ema")
    i_mean7 = feature_names.index("mean_7")
    return {
        "predict last score": [row[i_last] for row in xs],
        "predict their EMA (current behaviour)": [row[i_ema] for row in xs],
        "predict 7-session mean": [row[i_mean7] for row in xs],
    }


def conformal_radius(residuals, coverage=0.9):
    """The (1-alpha) quantile of absolute residuals, or None if too few.

    This is the split-conformal construction: fit on one slice, take
    absolute residuals on a slice the model never saw, and use their
    quantile as the interval half-width. The resulting interval has
    distribution-free finite-sample coverage — no assumption that the errors
    are normal, symmetric, or homoscedastic, and the guarantee holds at this
    sample size rather than asymptotically.

    The index is `ceil((n+1)(1-alpha)) - 1`, not `n(1-alpha)`. The `+1` is
    what makes the guarantee exact rather than approximate, and dropping it
    is the standard way this construction is quietly implemented wrong.
    """
    import conformal

    ordered = sorted(abs(r) for r in residuals)
    if len(ordered) < conformal.MIN_CALIBRATION:
        return None
    index = min(len(ordered) - 1,
                math.ceil((len(ordered) + 1) * coverage) - 1)
    return ordered[index]


def empirical_coverage(actual, predicted, radius):
    """What fraction of held-out points the interval actually contained.

    A conformal interval carries a theoretical guarantee, and checking it
    empirically is not redundant: the guarantee rests on exchangeability,
    the data here is a time series with drift, and exchangeability is
    exactly the assumption a drifting series breaks. If this number comes
    back well under the nominal coverage, the assumption is failing and the
    interval is narrower than it claims. Reporting it is how that becomes
    visible instead of theoretical.
    """
    if radius is None or not actual:
        return None
    inside = sum(1 for a, p in zip(actual, predicted) if abs(a - p) <= radius)
    return inside / len(actual)


def compare(actual, model_predictions, baselines):
    """Model against every baseline, with the honest verdict attached."""
    model_metrics = metrics(actual, model_predictions)
    baseline_metrics = {name: metrics(actual, preds)
                        for name, preds in baselines.items()}
    best_name, best = min(baseline_metrics.items(), key=lambda kv: kv[1]["mae"])
    improvement = 100.0 * (1.0 - model_metrics["mae"] / best["mae"]) if best["mae"] else 0.0
    return {
        "model": model_metrics,
        "baselines": baseline_metrics,
        "best_baseline": best_name,
        "improvement_percent": improvement,
        "beats_baseline": improvement > 0,
    }


def score_distribution(values, buckets=10):
    """A coarse histogram, used to sanity-check the isolation-forest output.

    A threshold picked without looking at the distribution it cuts is a
    guess. Printing this beside the forest's calibrated threshold makes
    the calibration checkable — and makes it obvious if a retrain moves the
    whole distribution, which would silently change what the signal means.
    """
    clean = [v for v in values if v is not None]
    if not clean:
        return []
    low, high = min(clean), max(clean)
    if high <= low:
        return [{"from": low, "to": high, "count": len(clean)}]
    width = (high - low) / buckets
    counts = [0] * buckets
    for v in clean:
        index = min(buckets - 1, int((v - low) / width))
        counts[index] += 1
    return [
        {"from": round(low + i * width, 3),
         "to": round(low + (i + 1) * width, 3),
         "count": counts[i]}
        for i in range(buckets)
    ]
