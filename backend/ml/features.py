r"""Turning a session history into supervised training rows.

THE LABEL PROBLEM, AND THE WAY AROUND IT
----------------------------------------

The project's standing complaint about itself is that the scoring formula's
weights are hand-set, and cannot be *learned* because learning needs labels
— "did this student actually understand the work?" — and nobody has
collected any. `fit_weights.py` is the instrument for that and it is still
waiting.

But there is a second supervised problem hiding in the same table, and it
needs no human labelling at all:

    given how a student has been working, what will their NEXT session
    score?

The label is the next row. The database supervises itself. That makes this
an ordinary regression problem with thousands of training examples
available the moment anyone uses the product, and it replaces a hand-fitted
straight line (`anomaly.forecast`) with a model that learned the shape of
real behaviour instead of assuming it is linear.

WHAT THIS DOES AND DOES NOT CLAIM
---------------------------------

It predicts **behaviour**, not **understanding**. The score it forecasts is
the same constructed 0-100 quantity as ever, so a model that predicts it
perfectly still says nothing about whether the construct is valid. That
question is unchanged and still needs the study.

What it does buy is real: a forecast grounded in this population's actual
dynamics rather than in the assumption that a student's trajectory is a
straight line, and — because the features are the process signals — a
learned statement about which behaviours precede a drop.

STRICT CAUSALITY
----------------

Every feature for the row predicting session *i* is computed from sessions
strictly before *i*. This is the one property that, if broken, makes the
whole exercise worthless while making the metrics look wonderful: a model
that can see the session it is predicting will score ~1.0 R-squared and be
useless in production. There are two specific traps here and both are
avoided deliberately:

  - **the EMA is recomputed, never read from `user_baseline`.** That row
    holds the CURRENT baseline, which has already absorbed every session
    including the one being predicted. Joining it in would leak the label
    directly.
  - **streams are split per (user, category)** and never interleaved, so a
    feature never summarises a context the target session does not belong
    to.

`tests/test_features.py` asserts causality by shuffling future values and
checking no feature moves; `ml/validation.py` runs the same check inside
the training pipeline so a future edit cannot quietly reintroduce leakage.

WHAT IS DELIBERATELY NOT A FEATURE
----------------------------------

No raw text, no keystroke sequence, no document identifier, no URL. Not
because the model would not benefit — an ordered inter-keystroke interval
series is a known channel for reconstructing typed content, and it would
certainly carry signal — but because collecting it would destroy the one
property that makes this project defensible. The eight-bucket histogram in
`rhythm.py` is what survives that constraint, and `last_regularity` is the
single scalar it contributes here. `tests/test_ml_privacy.py` asserts that
every feature name and every explanation string stays inside this rule.
"""
import hashlib
import math

# Sessions needed before a stream produces its first training row. Below
# this the history features (mean-of-3, slope) are undefined or degenerate.
WARMUP_SESSIONS = 3

# The EMA is recomputed here rather than read from the database — see the
# module docstring. Kept identical to scoring.EMA_ALPHA so the feature means
# the same thing the product means by "your baseline".
EMA_ALPHA = 0.25

FEATURE_NAMES = (
    "n_prior",            # how much history exists — a confidence proxy
    "last_score",
    "prev_score",
    "mean_3",
    "mean_7",
    "std_7",              # personal volatility
    "slope_7",            # recent direction
    "ema",                # the quantity the product currently forecasts from
    "delta_last_ema",     # was the last session above or below their norm
    "mean_abs_diff_5",    # session-to-session churn
    "last_typed_ratio",
    "last_paste_ratio",
    "last_ai_paste_rate",
    "last_regularity",    # -1 when the session predates rhythm capture
    "mean_regularity_5",
    "last_active_minutes",
    "gap_hours_prev",     # time between the two most recent sessions
    "is_assessment",      # which stream this is
)
FEATURE_DIM = len(FEATURE_NAMES)

# Sentinel for "this session predates rhythm capture". A tree can split on
# -1 and isolate those rows; imputing the mean would silently tell the model
# that an unmeasured session was average, which is a different claim.
MISSING = -1.0

# Plain-language names, used by `explain.py` and shown verbatim in the UI.
# Every string here is about *how someone worked*, never about *what they
# wrote* — that is the privacy rule restated at the point it could be
# broken, since an explanation is the one place model internals become
# user-visible text.
FEATURE_LABELS = {
    "n_prior": "how many sessions you have logged",
    "last_score": "your most recent session",
    "prev_score": "the session before that",
    "mean_3": "your last three sessions",
    "mean_7": "your last seven sessions",
    "std_7": "how much you vary session to session",
    "slope_7": "which way your recent sessions are heading",
    "ema": "your running personal baseline",
    "delta_last_ema": "how far your last session sat from your baseline",
    "mean_abs_diff_5": "how much your sessions jump around",
    "last_typed_ratio": "how much of your last session you typed",
    "last_paste_ratio": "how much of your last session was pasted",
    "last_ai_paste_rate": "pastes arriving straight from an AI tab",
    "last_regularity": "how even your typing rhythm was",
    "mean_regularity_5": "your typing rhythm over recent sessions",
    "last_active_minutes": "how long you worked",
    "gap_hours_prev": "the gap since your previous session",
    "is_assessment": "whether this is graded work",
}


def feature_set_hash():
    """A short fingerprint of the feature contract.

    Written into every model manifest and checked at load time. Renaming,
    reordering or adding a feature changes the meaning of every position in
    the vector, and a model trained on the old contract would keep
    predicting confidently against the new one. The hash makes that a
    refusal instead of a silent wrong answer.
    """
    joined = "\n".join(FEATURE_NAMES).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]


def _typed_ratio(row):
    typed = row.get("typed_chars") or 0
    pasted = row.get("pasted_chars") or 0
    total = typed + pasted
    return (typed / total) if total else 0.0


def _paste_ratio(row):
    typed = row.get("typed_chars") or 0
    pasted = row.get("pasted_chars") or 0
    total = typed + pasted
    return (pasted / total) if total else 0.0


def _ai_paste_rate(row):
    typed = row.get("typed_chars") or 0
    pasted = row.get("pasted_chars") or 0
    total = max(1.0, (typed + pasted) / 500.0)
    return (row.get("likely_ai_pastes") or 0) / total


def _slope(values):
    """OLS slope over an index axis. Returns 0.0 for a degenerate window."""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    if denom == 0:
        return 0.0
    return sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values)) / denom


def _std(values):
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / n)


def build_features(history, is_assessment):
    """Features for predicting the session that comes AFTER `history`.

    `history` is that user's scored sessions in this category, oldest first,
    every one of them strictly earlier than the session being predicted.
    """
    if len(history) < WARMUP_SESSIONS:
        return None

    scores = [float(r["score"]) for r in history]
    last, prev = scores[-1], scores[-2]

    window7 = scores[-7:]
    window3 = scores[-3:]
    window5 = scores[-5:]

    # Recomputed from the stream, never read from user_baseline — that row
    # has already absorbed the target session.
    ema = scores[0]
    for value in scores[1:]:
        ema += EMA_ALPHA * (value - ema)

    diffs = [abs(b - a) for a, b in zip(window5, window5[1:])]

    recent = history[-1]
    regularity_values = [
        float(r["regularity"]) for r in history[-5:] if r.get("regularity") is not None
    ]

    gap_hours = 0.0
    if len(history) >= 2:
        a = history[-2].get("started_at") or 0
        b = history[-1].get("started_at") or 0
        gap_hours = max(0.0, (b - a) / 3_600_000.0)
    # Compressed: the difference between 1h and 6h is meaningful, between
    # 200h and 400h is not, and a raw gap lets one three-week absence
    # dominate every split threshold.
    gap_hours = math.log1p(min(gap_hours, 720.0))

    return [
        float(len(history)),
        last,
        prev,
        sum(window3) / len(window3),
        sum(window7) / len(window7),
        _std(window7),
        _slope(window7),
        ema,
        last - ema,
        (sum(diffs) / len(diffs)) if diffs else 0.0,
        _typed_ratio(recent),
        _paste_ratio(recent),
        _ai_paste_rate(recent),
        float(recent["regularity"]) if recent.get("regularity") is not None else MISSING,
        (sum(regularity_values) / len(regularity_values)) if regularity_values else MISSING,
        min((recent.get("active_ms") or 0) / 60000.0, 180.0),
        gap_hours,
        1.0 if is_assessment else 0.0,
    ]


def streams_from_rows(rows):
    """Groups scored sessions into per-(user, category) streams, oldest first.

    Splitting by category as well as user is not tidiness: a writing session
    and a graded assessment are scored by different formulas against
    separate baselines, so a feature that averaged across both would
    describe a quantity the product never computes.
    """
    streams = {}
    for row in rows:
        row = dict(row)
        if row.get("score") is None:
            continue
        if row.get("category") not in ("writing", "assessment"):
            continue
        streams.setdefault((row["user_id"], row["category"]), []).append(row)

    for key in streams:
        streams[key].sort(key=lambda r: (r.get("started_at") or 0, r["session_id"]))
    return streams


def build_dataset(rows, horizon=1):
    """Every (features, label) pair the history can supply.

    `horizon` is how many sessions ahead the label averages over:

        1   the very next session
        5   the mean of the next five — i.e. where is this student HEADING

    The second is the question the product actually asks, and it is a
    materially different problem. A single session is dominated by its own
    circumstances — one rushed evening, one easy assignment — so most of its
    variance is irreducible and a student's own running average is already
    close to optimal. Averaging over a horizon cancels that noise and leaves
    the drift, which is the part a model can actually learn and the part an
    EMA is structurally bad at, because an EMA is a summary of the past with
    no mechanism for extrapolating a trend.

    Returns parallel lists plus the metadata a time-ordered split needs.
    """
    xs, ys, groups, times = [], [], [], []

    for (user_id, category), stream in streams_from_rows(rows).items():
        is_assessment = category == "assessment"
        # Stop early enough that every label averages over a FULL horizon.
        # Letting the last few rows average over one or two sessions would
        # quietly mix two different prediction problems in one training set.
        last = len(stream) - horizon + 1
        for i in range(WARMUP_SESSIONS, last):
            vector = build_features(stream[:i], is_assessment)
            if vector is None:
                continue
            future = [float(r["score"]) for r in stream[i:i + horizon]]
            xs.append(vector)
            ys.append(sum(future) / len(future))
            groups.append(user_id)
            times.append(stream[i].get("started_at") or 0)

    return xs, ys, groups, times


SESSION_QUERY = """SELECT session_id, user_id, category, started_at, active_ms,
                          typed_chars, pasted_chars, backspace_count, revision_count,
                          likely_ai_pastes, tab_switch_count, regularity, score
                   FROM sessions
                   WHERE score IS NOT NULL
                   ORDER BY user_id, started_at"""


def load_rows(conn):
    """Every scored session, oldest first within each user."""
    import db

    return conn.execute(db.q(SESSION_QUERY)).fetchall()


def load_dataset(conn, horizon=1):
    """Reads every scored session and builds the dataset."""
    return build_dataset(load_rows(conn), horizon=horizon)
