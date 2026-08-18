r"""Typing-rhythm analysis: telling composition apart from transcription.

WHY THIS EXISTS
---------------

The scoring formula in scoring.py rests on `typed_ratio` — how much of the
work arrived through the keyboard rather than the clipboard. That has one
large, known hole, and it is the first thing anyone attacks: a student who
reads an AI answer on a phone or a second monitor and *types it in* scores
100. Every character is genuinely theirs, by the only measure the formula
has. Nothing about the paste-correlation signal helps either — there is no
paste.

What that student cannot easily fake is the *shape* of their typing.
Composing and transcribing produce different temporal signatures, and the
difference is large enough to see in aggregate:

  composing      irregular. Bursts of a phrase, then a pause while the next
                 clause is worked out, then backtracking to fix the last
                 one. Inter-keystroke intervals spread across a wide range.

  transcribing   metronomic. The text already exists; the hands are a
                 buffer. Intervals cluster tightly, pauses land at line
                 breaks rather than at clause boundaries, and there is far
                 less mid-sentence revision.

So this module measures *regularity*, not speed. Speed would be useless —
it says more about whose hands they are than about what they are doing.

PRIVACY: WHY HISTOGRAMS, NOT A TIMING SERIES
--------------------------------------------

This is the part that constrains the whole design. A raw sequence of
inter-keystroke intervals is not privacy-safe: keystroke-timing inference
is a well-established side channel, and an ordered series of intervals
leaks information about the characters that produced it. Collecting one
would quietly undo the property the entire project is built on.

A histogram does not. Bucketing each interval and keeping only the counts
destroys the ordering, and it is the ordering that carries the content.
Two sessions with identical bucket counts are indistinguishable here no
matter what was typed. So the extension ships eight integers and two
counters, never a series, and this module is written to work from exactly
that (see `extension/content-script.js`).

The cost of that choice is real and worth naming: order-dependent features
— whether long pauses fall at clause boundaries, whether revision clusters
follow bursts — are unavailable. Those would likely be the strongest
signals. They are also the ones that leak. The histogram is the honest
trade.

THE PER-USER PART
-----------------

`regularity_index` on its own is a population measure, and a population
measure is exactly what this project exists to avoid. Some people simply
type evenly. A touch-typist composing freely can look more regular than a
hunt-and-peck typist copying.

So the index is never used as an absolute threshold. It is compared to an
EMA of *that user's own* regularity in *that category* — the same
machinery, and the same argument, as scoring.update_baseline and
anomaly.personal_deviation. The question asked is not "is this typing
regular?" but "is this far more regular than how this person normally
writes?" That is answerable without knowing anything about anyone else.

WHAT THIS IS NOT
----------------

It is not a detector and the weight reflects that. `W_RHYTHM_PENALTY` is
provisional — hand-set like the rest of scoring.py's constants, and for
the same reason: there are no labels yet. `regularity_index` is exported
into fit_weights.py's feature set so that when labels do exist, this
signal is validated on the same footing as the others rather than being
grandfathered in because it was harder to build.
"""
import math

# Bucket edges in milliseconds, log-spaced. The extension owns the same
# list; if either side changes it, `IKI_BUCKET_COUNT` mismatches and
# `features()` refuses the row rather than silently comparing histograms
# with different meanings.
IKI_BUCKET_EDGES_MS = (60, 120, 200, 320, 500, 900, 2000)
IKI_BUCKET_COUNT = len(IKI_BUCKET_EDGES_MS) + 1  # 8

# Representative interval for each bucket, used to interpolate a median.
# The open-ended top bucket gets 3000 rather than infinity so a session of
# nothing but long pauses produces a finite number.
_BUCKET_CENTRES_MS = (30, 90, 160, 260, 410, 700, 1450, 3000)

# Below this many timed keystrokes the histogram is too sparse to describe
# a rhythm — a dozen intervals will look "irregular" or "regular" mostly by
# chance. Sessions under it report status 'insufficient_keystrokes' and the
# caller leaves the score alone.
MIN_KEYSTROKES_FOR_RHYTHM = 120

# A pause this long or longer is read as deliberation rather than typing
# cadence. Matches the top bucket edge.
LONG_PAUSE_MS = IKI_BUCKET_EDGES_MS[-1]

# Same shape as anomaly.py, and for the same reasons: a floor on the
# denominator so a user with a very consistent rhythm doesn't generate a
# huge z from a trivial wobble, and an observation gate so the first few
# sessions can't be flagged against a variance estimated from one point.
MIN_RHYTHM_STD = 0.06
MIN_OBSERVATIONS_FOR_RHYTHM = 5

# Only upward deviations matter. Becoming *less* regular than usual is not
# a transcription signal — it is a harder piece of work, or a distracted
# afternoon.
Z_MODERATE = 1.5
Z_STRONG = 2.5

RHYTHM_EMA_ALPHA = 0.25  # deliberately the same as scoring.EMA_ALPHA


def _normalised_entropy(counts, total):
    """Shannon entropy of the bucket distribution, scaled to [0, 1].

    1.0 means intervals are spread evenly across every bucket; 0.0 means
    every interval landed in one. This is the core of the measure: a
    transcriber's intervals concentrate, a composer's do not.
    """
    if total <= 0:
        return 0.0
    entropy = 0.0
    for c in counts:
        if c <= 0:
            continue
        p = c / total
        entropy -= p * math.log(p)
    max_entropy = math.log(len(counts))
    if max_entropy <= 0:
        return 0.0
    return max(0.0, min(1.0, entropy / max_entropy))


def _median_interval_ms(counts, total):
    """Median interval, interpolated from bucket centres."""
    if total <= 0:
        return None
    half = total / 2.0
    cumulative = 0
    for centre, c in zip(_BUCKET_CENTRES_MS, counts):
        cumulative += c
        if cumulative >= half:
            return float(centre)
    return float(_BUCKET_CENTRES_MS[-1])


def features(*, iki_buckets, long_pauses, burst_keys, typed_chars):
    """Derives rhythm features from what the extension actually sends.

    Returns a dict with a `status` field, always safe to serialize:

        no_data                  - nothing sent (an older extension build)
        malformed                - bucket count doesn't match this version
        insufficient_keystrokes  - too few intervals to describe a rhythm
        ok                       - `regularity_index` is meaningful
    """
    if not iki_buckets:
        return {"status": "no_data", "regularity_index": None}
    if len(iki_buckets) != IKI_BUCKET_COUNT:
        # A build mismatch between extension and backend. Refusing is the
        # only safe answer — comparing histograms with different edges
        # would produce a confident number that means nothing.
        return {"status": "malformed", "regularity_index": None}

    counts = [max(0, int(c or 0)) for c in iki_buckets]
    total = sum(counts)
    if total < MIN_KEYSTROKES_FOR_RHYTHM:
        return {"status": "insufficient_keystrokes", "regularity_index": None,
                "intervals": total}

    typed = max(1, int(typed_chars or 0))
    long_pauses = max(0, int(long_pauses or 0))
    burst_keys = max(0, int(burst_keys or 0))

    spread = _normalised_entropy(counts, total)

    # Deliberation pauses per 100 characters. Composing generates them at
    # clause boundaries; transcription mostly doesn't, because the decision
    # about what comes next was already made elsewhere.
    pause_rate = min(1.0, (long_pauses / (typed / 100.0)) / 8.0)

    # Share of keystrokes in fast runs. High on its own is ambiguous — a
    # fast typist composing also bursts — which is why it is the smallest
    # of the three terms and never used alone.
    burst_share = min(1.0, burst_keys / total)

    # Regularity: high = metronomic. Spread is inverted because a wide
    # spread of intervals is the composing signature. Weights are
    # provisional, in the same sense as scoring.py's, and are set so that
    # interval spread dominates — it is the term with the clearest
    # mechanism behind it.
    regularity = (
        0.60 * (1.0 - spread)
        + 0.25 * (1.0 - pause_rate)
        + 0.15 * burst_share
    )

    return {
        "status": "ok",
        "regularity_index": round(max(0.0, min(1.0, regularity)), 4),
        "interval_spread": round(spread, 4),
        "pause_rate": round(pause_rate, 4),
        "burst_share": round(burst_share, 4),
        "median_interval_ms": _median_interval_ms(counts, total),
        "intervals": total,
    }


def update_rhythm_baseline(existing, regularity):
    """EMA mean/variance of a user's own regularity, per category.

    Same form as scoring.update_baseline — `diff` is taken against the
    previous mean, before the update, because using the updated one
    systematically under-estimates the variance and would inflate every
    subsequent z-score.

    Returns None when there is nothing to record, so the caller can leave
    the stored baseline untouched rather than writing nulls over it.
    """
    if regularity is None:
        return None

    if existing is None or existing.get("rhythm_mean") is None:
        return {"rhythm_mean": float(regularity), "rhythm_var": 0.0, "rhythm_n": 1}

    prev_mean = float(existing["rhythm_mean"])
    prev_var = float(existing.get("rhythm_var") or 0.0)
    prev_n = int(existing.get("rhythm_n") or 0)

    diff = regularity - prev_mean
    mean = prev_mean + RHYTHM_EMA_ALPHA * diff
    var = (1 - RHYTHM_EMA_ALPHA) * (prev_var + RHYTHM_EMA_ALPHA * diff * diff)

    return {"rhythm_mean": mean, "rhythm_var": max(0.0, var), "rhythm_n": prev_n + 1}


def rhythm_deviation(regularity, baseline):
    """How unusual this session's rhythm is *for this user*.

    One-sided by design: only typing that is markedly MORE regular than
    their own norm is a transcription signal.
    """
    empty = {"status": "no_baseline", "z_score": None, "level": None,
             "mean": None, "std_dev": None, "n_observations": 0}
    if regularity is None or not baseline or baseline.get("rhythm_mean") is None:
        return empty

    n = int(baseline.get("rhythm_n") or 0)
    mean = float(baseline["rhythm_mean"])
    std_dev = max(MIN_RHYTHM_STD, math.sqrt(max(0.0, float(baseline.get("rhythm_var") or 0.0))))

    if n < MIN_OBSERVATIONS_FOR_RHYTHM:
        return {"status": "insufficient_data", "z_score": None, "level": None,
                "mean": round(mean, 4), "std_dev": round(std_dev, 4), "n_observations": n}

    z = (regularity - mean) / std_dev

    if z >= Z_STRONG:
        level = "high"
    elif z >= Z_MODERATE:
        level = "medium"
    else:
        level = "low"

    return {
        "status": "ok",
        "z_score": round(z, 2),
        "level": level,
        "mean": round(mean, 4),
        "std_dev": round(std_dev, 4),
        "n_observations": n,
    }


def penalty_weight(deviation):
    """Scales the score penalty from the deviation, in [0, 1].

    Ramps from 0 at Z_MODERATE to 1 at Z_STRONG rather than stepping, so a
    session sitting just over a threshold is not treated the same as one
    far past it. Returns 0.0 for anything the deviation isn't confident
    about, which means an unknown rhythm never costs a student points.
    """
    if deviation.get("status") != "ok" or deviation.get("z_score") is None:
        return 0.0
    z = deviation["z_score"]
    if z <= Z_MODERATE:
        return 0.0
    return min(1.0, (z - Z_MODERATE) / (Z_STRONG - Z_MODERATE))


def explain(deviation):
    """One plain sentence for the student. No jargon, and phrased as an
    observation rather than an accusation — the signal is not strong
    enough to justify anything harder, and saying so is the honest
    framing."""
    if deviation.get("status") != "ok":
        return None
    if deviation.get("level") == "high":
        return ("Your typing in this session was unusually even compared with how you "
                "normally write — closer to copying something out than working it out.")
    if deviation.get("level") == "medium":
        return ("Your typing was somewhat steadier than your usual pattern for this kind "
                "of work.")
    return None
