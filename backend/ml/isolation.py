r"""Isolation Forest over *within-user deviation* vectors.

THE GAP THIS FILLS
------------------

Everything else in this project that judges a session judges its **score**:
`anomaly.personal_deviation` asks how far the score sits from that student's
own mean, and `conformal.assess` asks how rare that gap is in their own
history. Both are one-dimensional. Both are blind to the same failure:

    a session whose score is perfectly ordinary but whose *shape* is not.

Concretely — a student who normally types 90% of their work over 40 minutes
with an uneven rhythm, and who this time produces the same score from 30%
typed, 70% pasted, in 9 minutes, with a rhythm flat enough to be
transcription. The score formula happens to land in the same place. Nothing
in the existing pipeline notices, because nothing in the existing pipeline
looks at more than one number at a time.

An isolation forest is a good fit for exactly this question. It needs no
labels, it does not assume the features are normal or independent or even
continuous, it is cheap, and — unlike a density estimator — it works with
the handful of dimensions and few hundred rows this problem actually has.

HOW IT WORKS, IN ONE PARAGRAPH
------------------------------

Build many trees; at each node pick a feature at random and a split value at
random between that node's min and max; recurse. A point that is unusual
gets separated from the rest after very few splits, so its average path
length from the root is short. Normal points sit in the dense middle and
take many splits to isolate. The score is that average path length
normalised by the path length expected in a random binary search tree of the
same size, `c(n)`, and mapped to (0, 1) by `2^(-E[h]/c(psi))`: near 1 is
anomalous, around 0.5 is ordinary, near 0 means "more central than average".

The subtlety worth knowing is that isolation forests measure *susceptibility
to being split off*, not density, which is why they behave well in more
dimensions than a histogram or a kernel would tolerate — and also why they
are known to be weak on local anomalies inside a dense cluster. That
weakness is acceptable here because the deviation encoding below has already
moved every user to a common origin, so the anomalies that matter are global
in the transformed space.

THE ONE PLACE THIS DEPARTS FROM THE PROJECT'S THESIS — STATED PLAINLY
---------------------------------------------------------------------

Every other signal in this codebase compares a student only to themselves.
This one does not, quite, and pretending otherwise in a viva would be worse
than explaining it.

A forest fitted on one student's raw sessions needs dozens of that
student's sessions before it means anything, and almost nobody has dozens.
So the forest is fitted on **deviation vectors**: each session is first
expressed as how far each of its behavioural attributes sits from that same
student's own running mean, in units of that same student's own running
spread, computed from strictly earlier sessions only. The origin of the
space is therefore still personal — a student who always pastes 70% has a
deviation of zero on that axis, exactly as the thesis requires.

What *is* shared across users is the shape of the deviation cloud: the
answer to "how big a change is a big change". That is a population
quantity, it is assumed exchangeable across students, and that assumption
is the honest cost of getting a multivariate signal at all with this much
data. It is also the weaker of the two things one could pool, which is why
it is the one that was pooled.

`n_reference` is reported with every score so a reader can see how much
history the personal origin was actually computed from, and the signal is
withheld entirely below `MIN_REFERENCE_SESSIONS`.
"""
import math
import random

# Attributes the forest sees. Deliberately behavioural and deliberately
# few: these are the process signals the extension already sends as
# integers. No text, no identifiers, no ordered timing series — the same
# rule as everywhere else, restated where it could be broken.
ANOMALY_FEATURE_NAMES = (
    "score",
    "typed_ratio",
    "paste_ratio",
    "ai_paste_rate",
    "regularity",
    "active_minutes",
    "revision_rate",
)
ANOMALY_FEATURE_DIM = len(ANOMALY_FEATURE_NAMES)

# Sessions of personal history needed before a deviation vector means
# anything. Below three, the "running mean" is one or two numbers and the
# running spread is either zero or an artefact of which session came first.
MIN_REFERENCE_SESSIONS = 3

# Floor on the personal spread, in the same spirit as anomaly.MIN_STD_DEV:
# without it a student with a near-constant history generates enormous
# deviations from a trivial wobble, and the forest flags them every session.
MIN_SPREAD = {
    "score": 4.0,
    "typed_ratio": 0.05,
    "paste_ratio": 0.05,
    "ai_paste_rate": 0.10,
    "regularity": 0.04,
    "active_minutes": 3.0,
    "revision_rate": 0.02,
}

# Standard isolation-forest settings from Liu, Ting & Zhou (2008). 256 is
# their recommended subsample size and is not a number to tune casually:
# the method's whole efficiency argument rests on small subsamples, and
# larger ones make it *worse* by swamping anomalies with normal points.
SUBSAMPLE_SIZE = 256
N_TREES = 100

# WHERE THE THRESHOLD COMES FROM
# ------------------------------
# An absolute cut on the anomaly score is a guess, and the first version of
# this file made one: 0.62, chosen by eye. Measuring the distribution on a
# realistic multi-user fit showed why that was wrong — the 99th percentile
# came in at 0.584 and the maximum at 0.648, so 0.62 was not "the top few
# percent", it was the top fraction of one percent, and on a differently
# shaped dataset it could as easily have flagged nothing at all or half of
# everything.
#
# The score's scale depends on the data, so the threshold has to be
# calibrated from the data. It is set at fit time to a quantile of the
# training scores, which turns the knob into something that can actually be
# stated as a promise: *this signal is designed to call out roughly the most
# unusual 2% of sessions*. That is the same discipline `conformal.py` uses —
# guarantee a flag rate rather than a sigma — and for the same reason.
UNUSUAL_QUANTILE = 0.98

# Used only when a forest has no calibrated threshold (a payload written by
# an older version). Deliberately near the middle of the plausible range so
# a missing calibration degrades to something unremarkable rather than to a
# signal that fires constantly or never.
FALLBACK_THRESHOLD = 0.60


def _harmonic(n):
    """H(n), the n-th harmonic number. Euler's approximation above 10."""
    if n <= 0:
        return 0.0
    if n < 10:
        return sum(1.0 / i for i in range(1, n + 1))
    return math.log(n) + 0.5772156649015329


def average_path_length(n):
    """c(n): expected path length in a random binary search tree of n points.

    This is the normaliser that makes scores from differently sized
    subsamples comparable. Without it the score would depend on how big the
    subsample happened to be, which is a property of the fit rather than of
    the point being scored.
    """
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * _harmonic(n - 1) - (2.0 * (n - 1) / n)


# ---------------------------------------------------------------------------
# Session -> deviation vector
# ---------------------------------------------------------------------------

def _attributes(row):
    """The raw behavioural attributes of one session."""
    typed = float(row.get("typed_chars") or 0)
    pasted = float(row.get("pasted_chars") or 0)
    total = typed + pasted
    per_500 = max(1.0, total / 500.0)
    regularity = row.get("regularity")
    return {
        "score": float(row.get("score") or 0.0),
        "typed_ratio": (typed / total) if total else 0.0,
        "paste_ratio": (pasted / total) if total else 0.0,
        "ai_paste_rate": float(row.get("likely_ai_pastes") or 0) / per_500,
        # None when the session predates rhythm capture. Carried through as
        # None so `deviation_vector` can return the axis as an exact zero
        # rather than inventing a deviation for an attribute nobody
        # measured.
        "regularity": float(regularity) if regularity is not None else None,
        "active_minutes": min(float(row.get("active_ms") or 0) / 60000.0, 180.0),
        "revision_rate": (float(row.get("backspace_count") or 0)
                          + float(row.get("revision_count") or 0)) / per_500,
    }


def deviation_vector(history, row):
    """How unusual each attribute of `row` is *for this student*.

    `history` is that student's strictly earlier sessions in the same
    category. Returns None when there is not enough history for the
    reference point to mean anything — the same refusal every other signal
    in this codebase makes rather than answering from one data point.

    Causality note: the running mean and spread come from `history` only.
    `row` never contributes to the statistics it is judged against, which
    is the multivariate version of the rule `anomaly.py` follows and the
    one `validation.assert_no_leakage` checks.
    """
    if len(history) < MIN_REFERENCE_SESSIONS:
        return None

    past = [_attributes(r) for r in history]
    current = _attributes(row)

    vector = []
    for name in ANOMALY_FEATURE_NAMES:
        values = [a[name] for a in past if a[name] is not None]
        if current[name] is None or len(values) < 2:
            # Unmeasured on this session, or never measured before it.
            # Zero is "indistinguishable from their normal", which is the
            # only honest thing to say about an attribute nobody observed.
            vector.append(0.0)
            continue
        mean = sum(values) / len(values)
        variance = sum((v - mean) ** 2 for v in values) / len(values)
        spread = max(MIN_SPREAD[name], math.sqrt(variance))
        vector.append((current[name] - mean) / spread)
    return vector


def deviation_dataset(rows):
    """Every (user, session) deviation vector the history can supply.

    Returns parallel lists of vectors and the session ids they came from, so
    an evaluation can point at the sessions a fit actually flagged.
    """
    from . import features as feature_module

    vectors, session_ids = [], []
    for _key, stream in feature_module.streams_from_rows(rows).items():
        for i in range(MIN_REFERENCE_SESSIONS, len(stream)):
            vector = deviation_vector(stream[:i], stream[i])
            if vector is None:
                continue
            vectors.append(vector)
            session_ids.append(stream[i].get("session_id"))
    return vectors, session_ids


# ---------------------------------------------------------------------------
# The forest
# ---------------------------------------------------------------------------

class _INode:
    __slots__ = ("feature", "threshold", "left", "right", "size")

    def __init__(self):
        self.feature = None
        self.threshold = None
        self.left = None
        self.right = None
        self.size = 0

    def to_dict(self):
        if self.feature is None:
            return {"n": self.size}
        return {"f": self.feature, "t": self.threshold,
                "l": self.left.to_dict(), "r": self.right.to_dict()}

    @staticmethod
    def from_dict(d):
        node = _INode()
        if "n" in d:
            node.size = d["n"]
            return node
        node.feature = d["f"]
        node.threshold = d["t"]
        node.left = _INode.from_dict(d["l"])
        node.right = _INode.from_dict(d["r"])
        return node


def _build(points, depth, height_limit, rng):
    node = _INode()
    if depth >= height_limit or len(points) <= 1:
        node.size = len(points)
        return node

    dim = len(points[0])
    # Only split on axes that actually vary in this node — picking a
    # constant axis wastes a level of a tree whose whole budget is
    # `height_limit` levels deep.
    candidates = []
    for j in range(dim):
        column = [p[j] for p in points]
        low, high = min(column), max(column)
        if high > low:
            candidates.append((j, low, high))
    if not candidates:
        node.size = len(points)
        return node

    feature, low, high = rng.choice(candidates)
    threshold = rng.uniform(low, high)

    left = [p for p in points if p[feature] < threshold]
    right = [p for p in points if p[feature] >= threshold]
    if not left or not right:
        node.size = len(points)
        return node

    node.feature = feature
    node.threshold = threshold
    node.left = _build(left, depth + 1, height_limit, rng)
    node.right = _build(right, depth + 1, height_limit, rng)
    return node


def _path_length(node, point, depth=0):
    while node.feature is not None:
        node = node.left if point[node.feature] < node.threshold else node.right
        depth += 1
    # A leaf that was cut short by the height limit still holds several
    # points; charging only the depth reached would understate how hard
    # they were to isolate. Adding c(size) is the paper's correction.
    return depth + average_path_length(node.size)


class IsolationForest:
    """Liu, Ting & Zhou (2008), written out for the same reason `models.py`
    is: a `.fit()` call proves a library was installed, and this proves the
    method is understood. It is ~90 lines, it is on no hot path, and
    `tests/test_ml_against_sklearn.py` checks its rankings agree with
    scikit-learn's implementation on the same data.
    """

    def __init__(self, n_trees=N_TREES, subsample=SUBSAMPLE_SIZE, seed=11):
        self.n_trees = n_trees
        self.subsample = subsample
        self.seed = seed
        self.trees = []
        self.psi = 0
        # Calibrated in `fit` — see UNUSUAL_QUANTILE.
        self.threshold = FALLBACK_THRESHOLD

    def fit(self, points):
        if not points:
            raise ValueError("cannot fit an isolation forest on no points")
        rng = random.Random(self.seed)
        self.psi = min(self.subsample, len(points))
        height_limit = max(1, int(math.ceil(math.log2(max(2, self.psi)))))
        self.trees = []
        for _ in range(self.n_trees):
            sample = (rng.sample(points, self.psi) if self.psi < len(points)
                      else list(points))
            self.trees.append(_build(sample, 0, height_limit, rng))

        # Calibrate the cut on the fitted data itself. Every point is scored
        # by the whole forest, including the trees that saw it — which is
        # fine here and would not be for a supervised metric: the quantile is
        # describing the shape of this population's scores, not estimating
        # out-of-sample accuracy.
        scored = sorted(v for v in self.score(points) if v is not None)
        if scored:
            index = min(len(scored) - 1, int(UNUSUAL_QUANTILE * len(scored)))
            self.threshold = scored[index]
        return self

    def score_one(self, point):
        """Anomaly score in (0, 1). Near 1 anomalous, ~0.5 ordinary."""
        if not self.trees:
            return None
        mean_path = sum(_path_length(t, point) for t in self.trees) / len(self.trees)
        c = average_path_length(self.psi)
        if c <= 0:
            return None
        return 2.0 ** (-mean_path / c)

    def score(self, points):
        return [self.score_one(p) for p in points]

    def to_dict(self):
        return {
            "kind": "iforest",
            "psi": self.psi,
            "threshold": self.threshold,
            "quantile": UNUSUAL_QUANTILE,
            "feature_names": list(ANOMALY_FEATURE_NAMES),
            "trees": [t.to_dict() for t in self.trees],
        }

    @classmethod
    def from_dict(cls, d):
        forest = cls()
        forest.psi = d["psi"]
        forest.threshold = d.get("threshold", FALLBACK_THRESHOLD)
        forest.trees = [_INode.from_dict(t) for t in d["trees"]]
        return forest


# ---------------------------------------------------------------------------
# The signal as the API reports it
# ---------------------------------------------------------------------------

def assess(forest, history, row):
    """The behavioural-shape signal for one session.

    Always returns a serializable dict. `status` says how much to trust it,
    following the same vocabulary the rest of the codebase uses:

        unavailable        no forest has been trained
        insufficient_data  fewer than MIN_REFERENCE_SESSIONS earlier sessions
        ok                 `score` and `unusual` are meaningful
    """
    if forest is None or not getattr(forest, "trees", None):
        return {"status": "unavailable", "score": None, "unusual": None,
                "n_reference": len(history)}

    vector = deviation_vector(history, row)
    if vector is None:
        return {"status": "insufficient_data", "score": None, "unusual": None,
                "n_reference": len(history),
                "needed": MIN_REFERENCE_SESSIONS}

    value = forest.score_one(vector)
    if value is None:
        return {"status": "unavailable", "score": None, "unusual": None,
                "n_reference": len(history)}

    threshold = getattr(forest, "threshold", FALLBACK_THRESHOLD)
    return {
        "status": "ok",
        "score": round(value, 3),
        "unusual": value >= threshold,
        "n_reference": len(history),
        "threshold": round(threshold, 3),
        # The promise the threshold encodes, carried alongside it so a
        # reader knows this is a rate and not a probability of guilt.
        "designed_flag_rate": round(1.0 - UNUSUAL_QUANTILE, 3),
        "drivers": top_drivers(vector),
    }


def top_drivers(vector, limit=2):
    """Which attributes were furthest from this student's own norm.

    An isolation forest gives no native attribution — the score comes from
    path lengths across a hundred random trees and does not decompose. What
    is available honestly is the input: which axes of the deviation vector
    are largest. That is not the same as "which axes caused the score", and
    it is labelled as the weaker statement it is wherever it surfaces.
    """
    ranked = sorted(
        ((name, value) for name, value in zip(ANOMALY_FEATURE_NAMES, vector)),
        key=lambda kv: -abs(kv[1]),
    )
    return [
        {"feature": name, "deviation": round(value, 2),
         "direction": "above" if value > 0 else "below"}
        for name, value in ranked[:limit]
        if abs(value) >= 1.0
    ]


def explain(verdict):
    """One plain sentence, or None when there is nothing worth saying.

    Deliberately describes *how the work was done* and never *what it was
    about*, and never names another student. Asserted by
    `tests/test_ml_privacy.py`.
    """
    if verdict.get("status") != "ok" or not verdict.get("unusual"):
        return None
    drivers = verdict.get("drivers") or []
    if not drivers:
        return ("This session was put together differently from how you usually "
                "work, even though the score looks normal. Visible to you only.")
    labels = {
        "score": "the score itself",
        "typed_ratio": "how much you typed",
        "paste_ratio": "how much you pasted",
        "ai_paste_rate": "pastes coming straight from an AI tab",
        "regularity": "how even your typing rhythm was",
        "active_minutes": "how long you spent",
        "revision_rate": "how much you edited as you went",
    }
    parts = [f"{labels.get(d['feature'], d['feature'])} was well {d['direction']} "
             f"your usual" for d in drivers]
    return ("This session was put together differently from how you usually work — "
            + " and ".join(parts) + ". Visible to you only.")
