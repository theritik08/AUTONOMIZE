r"""Distribution-free calibrated flagging.

THE PROBLEM WITH THE Z-SCORE
----------------------------

anomaly.py flags a session when it falls 1.5 or 2.5 standard deviations
below the user's own mean. Those numbers borrow the intuition of a normal
distribution, and the independence score is not normally distributed: it is
bounded on [0, 100], built from a ratio, and its variance is compressed
near the ceiling where a diligent student sits. So "2.5 sigma" does not
correspond to any particular tail probability here, and the honest position
has been that the thresholds are ordinal — they separate "a bit unusual for
you" from "very unusual for you" and nothing more.

That is defensible but weak, and it is the criticism this module answers.

WHAT CONFORMAL PREDICTION GIVES INSTEAD
---------------------------------------

Rank, not distance. Given a calibration set of that user's own past scores,
the conformal p-value of a new score is

    p = (1 + #{i : R_i >= R_new}) / (n + 1)

where R is a nonconformity measure — here simply how far below their own
running mean the score sits, so a larger R is a more unusual session. Flag
when p <= alpha.

Under exchangeability of the calibration scores and the new one, this
guarantees

    P(flag a normal session) <= alpha

for *any* underlying distribution, at *finite* sample size. No normality, no
asymptotics, no variance estimate. Setting alpha = 0.05 means at most 5% of
a student's ordinary sessions get flagged, and that is true by construction
rather than by assumption.

THE ASSUMPTION THIS STILL MAKES, STATED PLAINLY
-----------------------------------------------

Exchangeability is weaker than "normally distributed" but it is not free.
A student's scores are a time series, and this project's entire premise is
that the series drifts — that is what the EMA baseline exists to track. A
drifting series is not exchangeable, so the guarantee is approximate rather
than exact here.

Two things narrow the gap, and neither closes it:

  - the calibration window is recent and bounded (WINDOW_SIZE), so
    exchangeability only has to hold locally rather than over a whole term;
  - the nonconformity measure is a *residual* against the EMA mean rather
    than the raw score, which removes most of the slow drift before the
    ranking happens.

What is claimed, then: a flag rate calibrated to alpha under local
exchangeability, with no distributional assumption. That is strictly more
than the z-score could claim and strictly less than a clean iid guarantee.

HOW IT COMBINES WITH THE Z-SCORE
--------------------------------

They are not redundant and neither is dropped. They answer different
questions and the split is deliberate:

    z-score    magnitude   "how far below your norm is this, in your units"
    conformal  decision    "is this rare enough to say something about"

So the UI keeps reporting the z-score as an effect size and uses the
conformal p-value to decide whether to raise a level at all. A large z with
a large p means the user's history is simply wide; that combination should
not be flagged, and under the old scheme it was.
"""
import json

# How many recent scores are kept per (user, category) for calibration.
# Large enough that alpha = 0.05 is expressible (see MIN_CALIBRATION), small
# enough that the exchangeability argument is about recent behaviour rather
# than a whole semester.
WINDOW_SIZE = 60

# The finite-sample floor. With n calibration points the smallest reachable
# p-value is 1/(n+1), so alpha is unreachable until n >= 1/alpha - 1. At
# alpha = 0.05 that is 19 — below it the method cannot flag at that level
# *at all*, and pretending otherwise would silently degrade the guarantee.
ALPHA_STRONG = 0.05
ALPHA_MODERATE = 0.15
MIN_CALIBRATION = int(round(1.0 / ALPHA_STRONG)) - 1  # 19


def load_window(raw):
    """Parses the stored window. Anything unreadable becomes an empty
    window rather than raising — a corrupt calibration set should cost a
    student nothing, and an empty one simply means 'cannot flag yet'."""
    if not raw:
        return []
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out[-WINDOW_SIZE:]


def push_window(raw, score):
    """Appends a score, oldest-first, capped at WINDOW_SIZE."""
    if score is None:
        return load_window(raw)
    window = load_window(raw)
    window.append(float(score))
    return window[-WINDOW_SIZE:]


def dump_window(window):
    return json.dumps([round(v, 3) for v in (window or [])])


def _nonconformity(score, reference):
    """How unusual a score is, larger meaning more unusual.

    A residual below the reference mean, floored at zero. One-sided for the
    same reason anomaly.py is: scoring *above* your own norm is a good day,
    not an integrity signal, and letting high scores accumulate
    nonconformity would make the calibration set's upper tail dilute the
    ranking of the lower one.
    """
    return max(0.0, reference - score)


def p_value(score, window, reference):
    """The conformal p-value for `score` against this user's own history.

    Uses the standard `(1 + count) / (n + 1)` form rather than
    `count / n`. The +1 in both places is what makes the guarantee valid at
    finite n — it accounts for the new point itself being exchangeable with
    the calibration set. Dropping it produces slightly smaller p-values and
    an anti-conservative test, which is the classic implementation error
    here.
    """
    if score is None or not window:
        return None
    new = _nonconformity(score, reference)
    at_least_as_extreme = sum(1 for r in window if _nonconformity(r, reference) >= new)
    return (1.0 + at_least_as_extreme) / (len(window) + 1.0)


def assess(score, window, reference):
    """Returns a serializable verdict.

        status
            no_calibration      - nothing stored yet
            insufficient_data   - fewer than MIN_CALIBRATION points, so
                                  ALPHA_STRONG is not even reachable
            ok                  - p_value and level are meaningful
        level    low | medium | high
        p_value  the conformal p-value
        alpha    the threshold `level` was decided against
    """
    empty = {"status": "no_calibration", "p_value": None, "level": None,
             "n_calibration": 0, "alpha": None}
    if score is None or not window:
        return empty

    n = len(window)
    if n < MIN_CALIBRATION:
        return {"status": "insufficient_data", "p_value": None, "level": None,
                "n_calibration": n, "alpha": None}

    p = p_value(score, window, reference)
    if p <= ALPHA_STRONG:
        level, alpha = "high", ALPHA_STRONG
    elif p <= ALPHA_MODERATE:
        level, alpha = "medium", ALPHA_MODERATE
    else:
        level, alpha = "low", None

    return {"status": "ok", "p_value": round(p, 4), "level": level,
            "n_calibration": n, "alpha": alpha}


def explain(verdict):
    """A sentence for the student that reports the *rate*, not a sigma.

    Deliberately phrased as "rarer than X% of your own sessions" rather
    than anything resembling a significance claim — the guarantee is about
    the long-run flag rate, not about this particular session being
    evidence of anything.
    """
    if verdict.get("status") == "insufficient_data":
        return (f"Still building a picture of your usual range — "
                f"{verdict['n_calibration']} of {MIN_CALIBRATION} sessions needed before "
                f"this comparison is reliable.")
    if verdict.get("status") != "ok" or verdict.get("level") not in ("medium", "high"):
        return None
    percent = int(round(verdict["alpha"] * 100))
    return (f"This session is in the lowest {percent}% of your own recent work "
            f"for this kind of task.")
