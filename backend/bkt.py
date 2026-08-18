r"""Bayesian Knowledge Tracing over retrieval checks.

WHY THIS IS HERE NOW AND WAS NOT BEFORE
---------------------------------------

The brief that asked for this said to add knowledge tracing "only if
justified", and until `retrieval.py` existed it was not. BKT models a
sequence of correct/incorrect attempts at ONE concept and estimates the
probability the learner has mastered it. Before retrieval checks there was
no such sequence anywhere in this system — only session scores, which are
behavioural, not per-concept, and not attempts at anything.

With retrieval checks there is exactly the sequence BKT was designed for,
so it earns its place. That ordering matters: adding BKT first and
inventing data to feed it is how a project ends up with an impressive
model over a meaningless input.

WHY BKT AND NOT DEEP KNOWLEDGE TRACING
--------------------------------------

DKT (an LSTM over attempt sequences) generally beats BKT on large public
datasets — tens of thousands of learners, hundreds of thousands of
attempts. This system will have a few dozen checks per student. At that
scale a four-parameter model is not a compromise, it is the correct
choice: DKT would fit noise, need a GPU nobody has, and produce a mastery
curve no student or tutor could interpret.

BKT's four parameters are also *explainable*, which matters more here than
half a point of AUC. When the dashboard says "your estimated mastery went
from 42% to 64%", every step of that is inspectable.

THE MODEL
---------

Four parameters per concept:

    p_init   probability of already knowing it before any attempt
    p_learn  probability of learning it between attempts
    p_slip   probability of getting it wrong while knowing it
    p_guess  probability of getting it right while not knowing it

After each attempt, Bayes' rule updates the belief, then the learning
transition is applied:

    posterior = P(known | observation)
    prior_next = posterior + (1 - posterior) * p_learn

The defaults below are the standard starting values from the BKT
literature (Corbett & Anderson 1995 and the follow-up work that bounded
slip and guess), NOT values fitted to this project's data — there is no
cohort to fit them on. They are named and stated as defaults so nobody
mistakes them for a result.

WHAT THIS CANNOT CLAIM
----------------------

A mastery estimate from three multiple-choice questions is a weak
posterior, and this module reports its own uncertainty rather than
implying otherwise. It is evidence about one concept under one question
bank, on the occasions the student chose to answer. It is not a grade, and
it is not proof of learning.
"""

# Standard BKT starting values. Stated as literature defaults rather than
# as anything measured here — this project has no cohort to fit them on.
DEFAULT_P_INIT = 0.25
DEFAULT_P_LEARN = 0.15

# Slip and guess are bounded well below 0.5 because above that the model
# becomes degenerate: a "known" state that produces wrong answers half the
# time explains any data at all, and the fit stops meaning anything. The
# bound is the standard one from the literature.
DEFAULT_P_SLIP = 0.10
DEFAULT_P_GUESS = 0.25   # four options, so a blind guess is 1/4
MAX_SLIP = 0.30
MAX_GUESS = 0.40

# Below this many attempts the posterior is dominated by p_init, so the
# number would describe the prior rather than the student.
MIN_ATTEMPTS_FOR_MASTERY = 3

# Where the dashboard is allowed to say "mastered". A threshold, not a
# fact — stated so it can be argued with.
MASTERY_THRESHOLD = 0.80


class Parameters:
    """One concept's four parameters, with the degenerate cases bounded."""

    __slots__ = ("p_init", "p_learn", "p_slip", "p_guess")

    def __init__(self, p_init=DEFAULT_P_INIT, p_learn=DEFAULT_P_LEARN,
                 p_slip=DEFAULT_P_SLIP, p_guess=DEFAULT_P_GUESS):
        self.p_init = _clamp(p_init, 0.01, 0.99)
        self.p_learn = _clamp(p_learn, 0.01, 0.99)
        self.p_slip = _clamp(p_slip, 0.01, MAX_SLIP)
        self.p_guess = _clamp(p_guess, 0.01, MAX_GUESS)

    def to_dict(self):
        return {"p_init": self.p_init, "p_learn": self.p_learn,
                "p_slip": self.p_slip, "p_guess": self.p_guess}


def _clamp(value, low, high):
    return max(low, min(high, float(value)))


def update(prior, correct, params):
    """One Bayesian update followed by the learning transition.

    `prior` is P(knows the concept) before this attempt. Returns the
    posterior *after* accounting for the chance of learning between
    attempts, which is what the next attempt's prior should be.
    """
    if correct:
        # P(correct | known) = 1 - slip;  P(correct | not known) = guess
        numerator = prior * (1 - params.p_slip)
        denominator = numerator + (1 - prior) * params.p_guess
    else:
        numerator = prior * params.p_slip
        denominator = numerator + (1 - prior) * (1 - params.p_guess)

    # Only reachable with degenerate parameters, which the constructor
    # bounds away — but a divide-by-zero here would take out a request.
    posterior = (numerator / denominator) if denominator > 0 else prior

    # The learning transition: even a wrong answer can be followed by
    # learning before the next attempt.
    return posterior + (1 - posterior) * params.p_learn


def trace(attempts, params=None):
    """Mastery after each attempt.

    `attempts` is a list of (n_correct, n_questions) in time order — one
    entry per retrieval check. A check of three questions is treated as
    three independent attempts, which is the standard treatment and is
    slightly optimistic: questions within one check are not fully
    independent, since a student who has just re-read the concept carries
    that into all three.
    """
    params = params or Parameters()
    prior = params.p_init
    curve = []

    for n_correct, n_questions in attempts:
        n_correct = max(0, int(n_correct or 0))
        n_questions = max(0, int(n_questions or 0))
        for i in range(n_questions):
            prior = update(prior, i < n_correct, params)
        curve.append(round(prior, 4))

    return curve


def estimate(attempts, params=None):
    """Current mastery and how much to trust it.

    Returns a dict whose `status` follows the same vocabulary as every
    other signal in this codebase: `no_data`, `warming_up`, `ok`.
    """
    params = params or Parameters()
    total_questions = sum(int(q or 0) for _c, q in attempts)

    if not attempts or total_questions == 0:
        return {"status": "no_data", "mastery": None, "n_attempts": 0,
                "needed": MIN_ATTEMPTS_FOR_MASTERY, "curve": [],
                "mastered": None,
                "message": "No retrieval checks answered for this concept yet."}

    curve = trace(attempts, params)
    mastery = curve[-1]

    if len(attempts) < MIN_ATTEMPTS_FOR_MASTERY:
        return {
            "status": "warming_up", "mastery": mastery,
            "n_attempts": len(attempts), "needed": MIN_ATTEMPTS_FOR_MASTERY,
            "curve": curve, "mastered": None,
            "message": (f"{len(attempts)} of {MIN_ATTEMPTS_FOR_MASTERY} checks — "
                        "this estimate still mostly reflects the starting "
                        "assumption rather than your answers."),
        }

    direction = "rising" if curve[-1] > curve[0] + 0.05 \
        else "falling" if curve[-1] < curve[0] - 0.05 else "flat"

    return {
        "status": "ok",
        "mastery": mastery,
        "n_attempts": len(attempts),
        "needed": MIN_ATTEMPTS_FOR_MASTERY,
        "curve": curve,
        "mastered": mastery >= MASTERY_THRESHOLD,
        "direction": direction,
        "parameters": params.to_dict(),
        "message": (f"Estimated recall for this concept is {round(mastery * 100)}% "
                    f"and {direction} across {len(attempts)} checks. This is a "
                    "model estimate from a few questions, not a grade."),
    }


def estimate_all(per_concept_checks, params=None):
    """Mastery per concept, from `retrieval.per_concept` output."""
    out = {}
    for concept_id, checks in (per_concept_checks or {}).items():
        attempts = [(c.get("n_correct"), c.get("n_questions")) for c in checks]
        out[concept_id] = estimate(attempts, params)
    return out
