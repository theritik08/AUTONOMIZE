"""Personal-baseline anomaly detection and short-horizon forecasting.

Background — why this module exists at all.

The project's stated thesis, repeated in the README and in scoring.py, is
that every score is judged "against that user's own baseline, never a
population average". The per-session score honoured that. `risk_level()`
did not: it compared a raw score against fixed cutoffs (70 / 40) that are
the same for everyone. A meticulous student whose assessment scores sit at
95 could drop to 72 — a huge personal deviation — and be labelled "low
risk", while a student who habitually works at 45 gets labelled "medium"
on a perfectly typical day for them. That is exactly the population-norm
comparison the design set out to avoid.

Fixing it needed a spread, not just a mean. `scoring.update_baseline` has
computed an EMA variance since the first version — it was written to the
database and then never read by anything. This module is what reads it.

The absolute thresholds are deliberately kept alongside the personal ones
rather than replaced. They answer different questions:

    absolute  — "is this session, on its own terms, mostly pasted work?"
    personal  — "is this session unlike how this person normally works?"

A student who pastes everything every single time has a stable, low
baseline: nothing is anomalous, but the absolute signal is still worth
showing. Reporting both, and saying which is which, is more honest than
collapsing them into one number.
"""
import math

import conformal

# Below this many observations the EMA variance is dominated by however
# the first couple of sessions happened to go, and a "3.2 sigma deviation"
# is noise dressed up as a finding. Under it, this module reports
# 'insufficient_data' and the caller falls back to absolute thresholds
# alone. Five is a judgement call, not a derived constant.
MIN_OBSERVATIONS_FOR_ZSCORE = 5

# A floor on the standard deviation used in the denominator. Without it, a
# user with a near-perfectly consistent history (variance ~0) generates an
# enormous z-score for a trivial one-point wobble.
MIN_STD_DEV = 4.0

Z_MODERATE = 1.5   # noticeably below their own norm
Z_STRONG = 2.5     # well outside it


def personal_deviation(score, baseline):
    """How unusual `score` is for this specific user.

    `baseline` is a `user_baseline` row (or None). Returns a dict that is
    always safe to serialize, with `status` describing how much to trust it:

        no_baseline        - nothing recorded for this user/category yet
        insufficient_data  - fewer than MIN_OBSERVATIONS_FOR_ZSCORE scores
        ok                 - z_score / level are meaningful
    """
    if score is None or not baseline or baseline.get("ema_mean") is None:
        return {"status": "no_baseline", "z_score": None, "level": None,
                "mean": None, "std_dev": None, "n_observations": 0}

    n = int(baseline.get("n_observations") or 0)
    mean = float(baseline["ema_mean"])
    # EMA variance can drift very slightly negative through float error.
    variance = max(0.0, float(baseline.get("ema_var") or 0.0))
    std_dev = max(MIN_STD_DEV, math.sqrt(variance))

    if n < MIN_OBSERVATIONS_FOR_ZSCORE:
        return {"status": "insufficient_data", "z_score": None, "level": None,
                "mean": round(mean, 1), "std_dev": round(std_dev, 1), "n_observations": n}

    z = (score - mean) / std_dev

    # Only downward deviations are flagged. Scoring far *above* your own
    # baseline is not an integrity signal, it's a good day.
    if z <= -Z_STRONG:
        level = "high"
    elif z <= -Z_MODERATE:
        level = "medium"
    else:
        level = "low"

    return {
        "status": "ok",
        "z_score": round(z, 2),
        "level": level,
        "mean": round(mean, 1),
        "std_dev": round(std_dev, 1),
        "n_observations": n,
    }


def calibrated_deviation(score, baseline, window):
    """The personal signal, decided by conformal p-value rather than sigma.

    Returns the same shape `personal_deviation` does, so callers are
    unchanged, plus a `conformal` block carrying the p-value and the flag
    rate it was decided against.

    The division of labour is the point:

        z_score    magnitude  - how far below their norm, in their units
        p_value    decision   - is it rare enough to raise a level

    The z-score is still computed and still reported, because "you scored
    64 against your usual 88" is what a student can actually act on. What
    it no longer does is decide, because deciding on a sigma multiple
    requires a distribution this quantity does not have.

    A large z with a large p is the case the old scheme got wrong: the
    user's history is simply wide, so a big absolute gap is ordinary for
    them. That combination is now correctly not flagged.
    """
    deviation = personal_deviation(score, baseline)
    reference = (baseline or {}).get("ema_mean")
    verdict = conformal.assess(score, window, reference) if reference is not None \
        else {"status": "no_calibration", "p_value": None, "level": None,
              "n_calibration": 0, "alpha": None}

    deviation["conformal"] = verdict

    if verdict.get("status") == "ok":
        # Conformal is authoritative once it has enough calibration data.
        deviation["level"] = verdict["level"]
        deviation["decided_by"] = "conformal"
        if deviation.get("status") != "ok":
            # Enough history to calibrate but not enough for a variance
            # estimate. The decision is still sound — that is exactly the
            # advantage of a rank-based method — so promote the status and
            # let z_score stay None.
            deviation["status"] = "ok"
    else:
        deviation["decided_by"] = "z_score"

    return deviation


def combined_risk(absolute_level, deviation):
    """Merges the absolute and personal signals into the single label the
    UI shows, taking whichever is more serious.

    Deliberately `max`, not an average: both are real signals and either
    one being alarming is worth surfacing. Averaging would let a normal-
    for-them score mask genuinely paste-heavy work, and vice versa.
    """
    order = {None: -1, "low": 0, "medium": 1, "high": 2}
    personal_level = deviation.get("level") if deviation.get("status") == "ok" else None
    if order[personal_level] > order.get(absolute_level, -1):
        return personal_level, "personal"
    return absolute_level, "absolute"


def explain(absolute_level, deviation, score):
    """One plain sentence for the student, naming which comparison drove
    the label. Shown verbatim in the UI, so it avoids jargon like 'z-score'
    and never implies anyone else has seen this."""
    if deviation.get("status") == "ok" and deviation.get("level") in ("medium", "high"):
        mean = deviation.get("mean")
        verdict = deviation.get("conformal") or {}
        rarity = conformal.explain(verdict) if verdict.get("status") == "ok" else None
        if mean is not None:
            base = (f"This session scored {round(score)} — noticeably below your own usual "
                    f"{round(mean)} for this kind of work.")
        else:
            base = f"This session scored {round(score)} — below your usual range."
        # The rarity sentence reports a flag *rate*, which is the thing the
        # conformal guarantee actually covers. Appended rather than
        # replacing the gap, because a rate is not actionable on its own.
        return f"{base} {rarity} Visible to you only." if rarity else f"{base} Visible to you only."
    if deviation.get("status") == "insufficient_data":
        return (
            "Still learning your usual pattern for this kind of work — "
            f"{deviation['n_observations']} of {MIN_OBSERVATIONS_FOR_ZSCORE} sessions needed "
            "before personal comparisons kick in."
        )
    if absolute_level == "high":
        return f"This session scored {round(score)} — most of the work came from pasted text."
    if absolute_level == "medium":
        return f"This session scored {round(score)} — a mix of typed and pasted work."
    return "Nothing unusual in this session."


# ---------------------------------------------------------------------------
# Short-horizon trend forecast
# ---------------------------------------------------------------------------

# Below this, a straight line does not describe the data and the projection
# it produces is a confidently wrong number shown to a student — worse than
# showing nothing. The threshold lives here rather than in the dashboard
# because it is a property of the model, not of one client: the frontend
# guard stays as defence in depth, but any other consumer of /api/score was
# previously handed an unguarded projection with only `r2` to warn it.
MIN_R2_FOR_PROJECTION = 0.30


def forecast(trend, horizon_days=7):
    """Ordinary least squares over the daily trend, projected forward.

    Returns None when there's too little history to fit a line worth
    showing. Intentionally the simplest model that answers "which way is
    this heading" — with this much data per user (tens of daily points,
    noisy, non-stationary) a heavier model would produce more confident-
    looking numbers without being more correct.
    """
    points = [p for p in (trend or []) if p.get("score") is not None]
    if len(points) < 4:
        return None

    xs = list(range(len(points)))
    ys = [float(p["score"]) for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    intercept = mean_y - slope * mean_x

    predicted = [intercept + slope * x for x in xs]
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    # r2 is reported so the UI can decline to draw a projection through
    # data the line doesn't actually describe.
    #
    # ss_tot == 0 means every point is identical. The old `else 0.0` read
    # that as "no fit" and withheld the projection — but a horizontal line
    # through identical points is a *perfect* fit, and the case is not
    # exotic: it is a student whose score has been pinned at 100 all week,
    # who would have been told their trend was unclear. Report 1.0.
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0

    projected = intercept + slope * (n - 1 + horizon_days)
    projected = max(0.0, min(100.0, projected))

    if slope > 0.4:
        direction = "improving"
    elif slope < -0.4:
        direction = "declining"
    else:
        direction = "steady"

    r2 = round(max(0.0, r2), 2)
    describes_the_data = r2 >= MIN_R2_FOR_PROJECTION

    return {
        "slope_per_day": round(slope, 2),
        # Withheld, not merely flagged, when the fit is too poor to justify
        # it. `r2` is still returned so a client can say why.
        "projected_score": round(projected, 1) if describes_the_data else None,
        "horizon_days": horizon_days,
        "r2": r2,
        # A direction is a weaker claim than a projected value, but it is
        # still a claim, so it is withheld on the same condition.
        "direction": direction if describes_the_data else "unclear",
        "points_used": n,
    }
