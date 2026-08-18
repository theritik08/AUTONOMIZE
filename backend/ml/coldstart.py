r"""What to say about a student the system has barely met.

THE PROBLEM
-----------

Every judgement this project makes is relative to a personal baseline, and
on session one there is no personal baseline. The options are:

  1. show nothing until enough history exists,
  2. quietly use the population average and call it personal,
  3. blend, and say exactly how much of the answer is still borrowed.

Option 2 is the one that would look best in a demo and it is dishonest, so
it is not available. Option 1 is what the codebase did before this module,
and it is honest but wasteful: `anomaly.personal_deviation` returns
`insufficient_data` for the first four sessions and the student sees an
empty panel during precisely the window when a new habit is forming.

This module implements option 3, with the borrowing made explicit in the
output so a client can render "still learning you" rather than a confident
number.

EMPIRICAL BAYES, AND WHY THE SHRINKAGE CONSTANT IS NOT A GUESS
--------------------------------------------------------------

Treat each student's true mean as drawn from a population distribution.
Then the posterior mean of a student with `n` observations is a weighted
average of their own mean and the population mean, and the weight is not a
knob to tune — it falls out of the two variances:

    k        = within-student variance / between-student variance
    weight   = n / (n + k)
    estimate = weight * personal_mean + (1 - weight) * population_mean

`k` is the number of the student's own sessions needed before their own
mean is worth as much as the population's. If students differ a lot from
each other (large between-user variance) `k` is small and personal history
takes over quickly. If everyone is much the same and the noise is in the
sessions rather than the people, `k` is large and borrowing is worth more
for longer. Both variances are measured from the database in
`estimate_prior`, so the crossover is a measurement, not a preference.

This is the standard James-Stein / hierarchical-model result, and the
reason to reach for it here rather than a hand-set "use the population for
the first 3 sessions" rule is that the hand-set rule is a guess about the
same quantity, made without looking.

WHAT IS AND IS NOT SHARED
-------------------------

This is the second place in the codebase where a cross-user quantity
appears (the first is `isolation.py`, for the same kind of reason) and, as
there, the honest framing matters more than the defence. What is borrowed
is a *prior*: two numbers describing the population, which decay out of the
answer as the student's own history accumulates and are gone by the time
anyone is being judged on anything consequential. What is never borrowed is
the decision: a student is still only ever compared to their own estimated
mean, and `reliability` says out loud how much of that estimate is still
somebody else's.
"""

# Under this many of their own observations, no personal claim is made at
# all — only the shrunken estimate, labelled as provisional. Matches
# `anomaly.MIN_OBSERVATIONS_FOR_ZSCORE` so the two modules agree about when
# a student stops being new.
MIN_PERSONAL_OBSERVATIONS = 5

# Where the personal estimate is considered to stand on its own. At this
# point the borrowed component is small and shrinking; the number is a
# presentation threshold, not a statistical one, and it is only used to
# choose which sentence to show.
SETTLED_OBSERVATIONS = 12

# Fallback k when the prior cannot be estimated (a brand-new deployment
# with too few users to measure a between-user variance). Deliberately
# large: not knowing how different students are from each other is a reason
# to lean on the population *more* cautiously, not less, and a big k makes
# the personal component ramp in slowly rather than jumping around on two
# sessions of evidence.
DEFAULT_K = 8.0

# Below this many distinct users, a "between-user variance" is really a
# statement about four people and should not be dignified with a number.
MIN_USERS_FOR_PRIOR = 8


def estimate_prior(rows):
    """Measures the population prior from scored sessions.

    Returns one prior per category, because a writing session and a graded
    assessment are scored by different formulas and pooling them would
    produce a mean that describes neither.

    The between-user variance is the variance of the per-user means, and
    the within-user variance is the mean of the per-user variances. That
    decomposition is the one-way ANOVA split, and it is deliberately the
    naive version: with a few dozen users the unbiased correction (which
    subtracts within/n from the between term) can go negative, and a
    negative variance estimate is a worse thing to ship than a slightly
    conservative one.
    """
    from . import features as feature_module

    per_category = {}
    for (user_id, category), stream in feature_module.streams_from_rows(rows).items():
        scores = [float(r["score"]) for r in stream]
        if len(scores) < 2:
            continue
        mean = sum(scores) / len(scores)
        variance = sum((s - mean) ** 2 for s in scores) / len(scores)
        per_category.setdefault(category, []).append((user_id, mean, variance))

    priors = {}
    for category, entries in per_category.items():
        if len(entries) < MIN_USERS_FOR_PRIOR:
            continue
        means = [m for _u, m, _v in entries]
        grand = sum(means) / len(means)
        between = sum((m - grand) ** 2 for m in means) / len(means)
        within = sum(v for _u, _m, v in entries) / len(entries)
        k = (within / between) if between > 1e-9 else DEFAULT_K
        # A k of 300 would mean nobody's own history ever matters, which is
        # a sign the estimate has gone wrong rather than a finding.
        k = min(max(k, 0.5), 50.0)
        priors[category] = {
            "mean": round(grand, 2),
            "between_user_var": round(between, 2),
            "within_user_var": round(within, 2),
            "k": round(k, 2),
            "n_users": len(entries),
        }
    return priors


def shrink(personal_mean, n_observations, prior):
    """Empirical-Bayes estimate of this student's mean, plus its provenance.

    Always returns a dict. When there is no prior to borrow from, the
    personal mean is returned unchanged with `weight` 1.0 — the module
    degrades to exactly the behaviour that existed before it, which is the
    fallback rule the whole codebase follows.
    """
    n = max(0, int(n_observations or 0))

    if not prior or prior.get("mean") is None:
        return {
            "estimate": personal_mean,
            "personal_weight": 1.0 if personal_mean is not None else 0.0,
            "population_mean": None,
            "k": None,
            "source": "personal_only",
        }

    population_mean = float(prior["mean"])
    k = float(prior.get("k") or DEFAULT_K)

    if personal_mean is None or n == 0:
        return {
            "estimate": round(population_mean, 1),
            "personal_weight": 0.0,
            "population_mean": round(population_mean, 1),
            "k": k,
            "source": "population_prior",
        }

    weight = n / (n + k)
    estimate = weight * float(personal_mean) + (1.0 - weight) * population_mean
    return {
        "estimate": round(estimate, 1),
        "personal_weight": round(weight, 3),
        "population_mean": round(population_mean, 1),
        "k": k,
        "source": "blended" if weight < 0.99 else "personal_only",
    }


def readiness(n_observations, blended=None):
    """How much to trust anything said about this student, in one dict.

    The four fields the brief asks for, kept deliberately separate because
    they answer different questions and collapsing them into a single
    percentage is how a warm-up state ends up looking like a confident one:

        insufficient_data  boolean — is a personal claim being withheld
        warm_up            {have, need} — a progress bar, not a judgement
        reliability        0-1 — how much of the estimate is their own data
        confidence         a word, for choosing which sentence to show
    """
    n = max(0, int(n_observations or 0))
    reliability = (blended or {}).get("personal_weight")
    if reliability is None:
        reliability = min(1.0, n / float(SETTLED_OBSERVATIONS))

    if n >= SETTLED_OBSERVATIONS and reliability >= 0.6:
        confidence = "established"
    elif n >= MIN_PERSONAL_OBSERVATIONS:
        confidence = "provisional"
    else:
        confidence = "learning"

    return {
        "insufficient_data": n < MIN_PERSONAL_OBSERVATIONS,
        "warm_up": {"have": n, "need": MIN_PERSONAL_OBSERVATIONS,
                    "settled_at": SETTLED_OBSERVATIONS},
        "reliability": round(float(reliability), 3),
        "confidence": confidence,
    }


def explain(state):
    """One sentence for the student. Never implies anyone else saw their work.

    The population is referred to as "how sessions like this usually go",
    which is what a prior mean actually is, rather than "other students",
    which would be both alarming and a misdescription — the prior is an
    aggregate with no individual in it.
    """
    warm = state.get("warm_up") or {}
    have, need = warm.get("have", 0), warm.get("need", MIN_PERSONAL_OBSERVATIONS)
    confidence = state.get("confidence")

    if confidence == "learning":
        return (f"Still learning your pattern — {have} of {need} sessions. "
                "Until then this is anchored to how sessions like this usually "
                "go, not to you.")
    if confidence == "provisional":
        share = round(100 * float(state.get("reliability") or 0))
        return (f"Mostly yours now — about {share}% of this comparison comes from "
                "your own history, and that share grows every session.")
    return "This is now based on your own history."
