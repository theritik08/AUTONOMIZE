r"""Answering "why did this change?" without inventing a reason.

TWO QUESTIONS, TWO METHODS
--------------------------

    global   which behaviours does this model rely on at all?
    local    why is THIS student's forecast where it is?

They need different instruments and conflating them is the usual mistake.
A global importance ranking cannot explain one prediction — a feature the
model leans on heavily across the population may be perfectly ordinary for
this particular student and contribute nothing to their number.

GLOBAL: PERMUTATION IMPORTANCE, NOT SPLIT COUNTS
------------------------------------------------

`models.GradientBoostedTrees.feature_importance` counts splits. That
describes the model's *structure* and is biased toward high-cardinality
features, which get more opportunities to be split on regardless of whether
they carry signal.

Permutation importance asks the question that actually matters: shuffle
this one column in the held-out set and measure how much worse the
predictions get. It is model-agnostic, it is measured on data the model
never trained on, and its units are the units of the metric — "destroying
this feature costs 0.8 MAE points" is a sentence with meaning.

Its known weakness is correlated features, and these features are heavily
correlated by construction (`ema`, `mean_7` and `last_score` are three
views of the same series). When one is shuffled the model recovers most of
the signal from its neighbours, so each individually looks unimportant and
the group's true importance is understated. That is stated here rather than
worked around, because the workaround — grouped permutation — needs a
grouping someone has to justify, and an honest caveat beats an
unjustifiable grouping.

LOCAL: EXACT FOR THE LINEAR MODEL
---------------------------------

The primary model is ridge (see `models.py` for the benchmark that decided
that), and for a linear model the local attribution is not an approximation
at all. The prediction *is* the intercept plus the sum of per-feature terms,
exactly, so a student's breakdown can be reported without any of the
sampling error, baseline-choice arbitrariness or runtime cost that SHAP
would bring.

That is worth being explicit about, because "we used SHAP" is the expected
answer and the better answer here is "SHAP's whole job is to approximate a
decomposition that this model has in closed form". For the boosted-tree
challenger, where no such decomposition exists, the local explanation falls
back to naming which features are furthest from the population's typical
value — clearly labelled as the weaker statement it is, rather than dressed
up as an attribution.

THE PRIVACY RULE, RESTATED AT THE PLACE IT COULD BREAK
------------------------------------------------------

An explanation is the one point where model internals become user-visible
prose. Every string this module can emit comes from `features.FEATURE_LABELS`,
which is a fixed dictionary of phrases about *how someone worked*. There is
no path by which document text, a URL, a keystroke sequence or another
user's data can reach an explanation, because none of those are features.
`tests/test_ml_privacy.py` asserts it against the emitted strings rather
than trusting this paragraph.
"""
import random

from . import features as feature_module
from . import models as model_module


# ---------------------------------------------------------------------------
# Global
# ---------------------------------------------------------------------------

def permutation_importance(model, xs, ys, feature_names=None, repeats=5, seed=13):
    """Increase in MAE when each feature is shuffled, on held-out rows.

    Returns a list of dicts sorted by importance, largest first. Values are
    in MAE points and can legitimately be negative — a feature the model
    would be better off without. Negatives are reported rather than clipped,
    because clipping them hides the finding.
    """
    from .evaluation import metrics

    names = list(feature_names or feature_module.FEATURE_NAMES)
    if not xs:
        return []

    rng = random.Random(seed)
    base = metrics(ys, model.predict(xs))["mae"]

    results = []
    for j in range(len(xs[0])):
        deltas = []
        for _ in range(repeats):
            column = [row[j] for row in xs]
            rng.shuffle(column)
            shuffled = [list(row) for row in xs]
            for i, value in enumerate(column):
                shuffled[i][j] = value
            deltas.append(metrics(ys, model.predict(shuffled))["mae"] - base)
        mean_delta = sum(deltas) / len(deltas)
        spread = max(deltas) - min(deltas)
        results.append({
            "feature": names[j] if j < len(names) else f"feature_{j}",
            "mae_increase": round(mean_delta, 4),
            # The spread across repeats is the honest error bar. A feature
            # whose "importance" is smaller than the variation between
            # shuffles has not been shown to matter at all.
            "spread": round(spread, 4),
            "significant": mean_delta > spread,
        })

    results.sort(key=lambda r: -r["mae_increase"])
    return results


# ---------------------------------------------------------------------------
# Local
# ---------------------------------------------------------------------------

# Below this many MAE-points of contribution, a term is noise dressed as a
# reason. Showing a student that "how long you worked" moved their forecast
# by 0.03 points invites them to act on nothing.
MIN_CONTRIBUTION = 0.5

# Features that describe the CONTEXT a session happened in rather than
# anything the student did, and which are therefore excluded from the
# student-facing attribution — though not from the model, where they carry
# real signal.
#
# This is not cosmetic. `is_assessment` is constant within a stream, and
# because the coefficients are fitted on standardised features, a constant
# that sits far from the pooled mean produces a large contribution in every
# single prediction. On a writing session it came out as the single biggest
# term, rendered as "the biggest factor was whether this is graded work" —
# which is true of the arithmetic and useless to the person reading it,
# since it says the same thing on every writing session they will ever
# have. `n_prior` is excluded for the same reason: it only ever goes up,
# so naming it as a factor tells a student to have logged fewer sessions.
#
# The rule being applied: a local explanation should name things that could
# have been otherwise. Everything else belongs in the global ranking, where
# it is labelled as a property of the model rather than of their session.
CONTEXT_FEATURES = frozenset({"is_assessment", "n_prior"})


def attribute(model, row, feature_names=None, limit=3):
    """Why this one prediction landed where it did.

    Returns a dict with `method` naming which of the two regimes produced
    it, because they are not equally strong claims and a caller that shows
    them identically is misleading someone.
    """
    names = list(feature_names or feature_module.FEATURE_NAMES)

    if isinstance(model, model_module.RidgeRegression):
        contributions = model.contributions(row)
        ranked = sorted(
            (t for t in zip(names, contributions, row)
             if t[0] not in CONTEXT_FEATURES),
            key=lambda t: -abs(t[1]),
        )
        terms = [
            {
                "feature": name,
                "label": feature_module.FEATURE_LABELS.get(name, name),
                "contribution": round(value, 2),
                "direction": "raises" if value > 0 else "lowers",
                "value": round(raw, 2),
            }
            for name, value, raw in ranked[:limit]
            if abs(value) >= MIN_CONTRIBUTION
        ]
        return {
            "method": "exact",
            "note": "the forecast is the sum of these terms plus a constant",
            "terms": terms,
        }

    # Tree ensemble: no closed-form decomposition. Say the weaker thing
    # accurately rather than the stronger thing loosely.
    importance = model.feature_importance(len(row)) \
        if hasattr(model, "feature_importance") else [0.0] * len(row)
    ranked = sorted((t for t in zip(names, importance, row)
                     if t[0] not in CONTEXT_FEATURES),
                    key=lambda t: -t[1])
    return {
        "method": "model_wide",
        "note": ("the model relies on these overall; this is not a "
                 "decomposition of this particular forecast"),
        "terms": [
            {"feature": name, "label": feature_module.FEATURE_LABELS.get(name, name),
             "weight": round(weight, 3), "value": round(raw, 2)}
            for name, weight, raw in ranked[:limit] if weight > 0.01
        ],
    }


def sentence(attribution):
    """One plain-language line, or None when there is nothing solid to say.

    Never asserts causation — "because you pasted more" is a claim the model
    cannot support, and "the biggest factor was how much you pasted" is one
    it can.
    """
    terms = (attribution or {}).get("terms") or []
    if not terms:
        return None
    if attribution.get("method") != "exact":
        labels = [t["label"] for t in terms[:2]]
        return ("This forecast leans mostly on " + " and ".join(labels)
                + ", across everyone the model was trained on.")
    head = terms[0]
    line = f"The biggest factor was {head['label']}, which {head['direction']} the forecast."
    if len(terms) > 1:
        line += f" Next was {terms[1]['label']}."
    return line
