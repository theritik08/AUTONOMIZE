r"""One estimate from many weak signals — and the confidence to go with it.

WHY COMBINE AT ALL
------------------

Until this module, every signal reported separately: a paste ratio here, a
rhythm z-score there, a conformal p-value, an isolation-forest score, a
retrieval rate. Each is weak on its own and each is individually gameable,
and a dashboard showing five weak numbers invites the reader to pick the
one that suits them.

Combining them does two useful things. A student who defeats one signal
has usually not defeated the others — flattening your rhythm does nothing
to your retrieval score, and avoiding pastes does nothing to your session
dynamics. And a single number can carry a single, honest confidence.

WEIGHTS ARE SET BY HOW GAMEABLE EACH SIGNAL IS
-----------------------------------------------

This is the part that matters, and it is the opposite of how these things
are usually weighted. The instinct is to weight by how *discriminative* a
signal looks. The correct move against an adversary who knows how the
system works is to weight by how *expensive it is to fake*.

    retrieval        HIGHEST — cannot be faked by changing how you type.
                     The student either recalls the concept or does not.
                     Costly to evade: you would have to actually learn it,
                     which is the outcome the product wants anyway.

    paste ratio      HIGH — a direct measurement, not an inference. Evaded
                     only by retyping, which the rhythm signal then sees,
                     or by working in an untracked app, which shows up as
                     an anomalous session shape.

    session shape    MEDIUM — the isolation forest reads several dimensions
                     at once, so imitating "normal" means imitating a
                     multivariate distribution, not one number.

    rhythm           LOW — a student who knows it exists can insert pauses
                     and defeat it. `test_adversarial.py` demonstrates
                     exactly that. It is kept because it costs nothing and
                     catches the naive case, and weighted accordingly.

    tab switching    LOWEST — trivially controlled, and confounded by
                     ordinary research. Near-zero weight; present for
                     explanation rather than for scoring.

When a signal is easy to game, the answer is to reduce its influence, not
to pretend it is reliable. That is what these weights encode.

THE OUTPUT IS AN ESTIMATE, NEVER A VERDICT
-------------------------------------------

`level` is a band, not a judgement. The payload carries an explicit
`not_proof` disclaimer, the phrase "behavioural pattern" rather than "AI
use", and a `contributors` list so a reader can see which signals drove it
and disagree with any of them.

Confidence is separate from risk on purpose. High risk with low confidence
is a common and important state — it means several weak signals agreed
while the strong one (retrieval) was missing — and collapsing the two into
one number would hide exactly that.
"""

# Weighted by how expensive each signal is to fake. See the docstring —
# this ordering is the whole design and reversing it would produce a
# system that looks better and performs worse against a real student.
WEIGHTS = {
    "retrieval": 0.40,
    "paste": 0.28,
    "session_shape": 0.16,
    "rhythm": 0.11,
    "tab_switching": 0.05,
}

# Bands. Judgements, stated as such — there is no calibrated cut, because
# calibrating one needs the labelled cohort this project does not have.
MODERATE_ABOVE = 0.45
HIGH_ABOVE = 0.68

# Below this share of the total weight actually observed, no level is
# reported at all. Two weak signals agreeing is not evidence.
MIN_EVIDENCE_WEIGHT = 0.30

# Retrieval is the only signal that cannot be faked by changing how you
# type, so its absence caps confidence no matter how many others agree.
CONFIDENCE_CAP_WITHOUT_RETRIEVAL = 0.55


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def estimate(score, baseline_mean, paste_ratio, rhythm_deviation,
             behavioural_anomaly, retrieval, tab_switch_rate=None,
             n_sessions=0):
    """A single dependency-risk estimate with its confidence and reasons.

    Every argument may be None or a not-ok status dict — that is the normal
    case, not an error path. Missing signals are dropped from both the
    numerator and the weight total, so a partial picture produces a
    correspondingly low confidence rather than a confident answer from
    whatever happened to be available.
    """
    contributors = []
    weighted_sum = 0.0
    observed_weight = 0.0

    def contribute(name, value, why):
        nonlocal weighted_sum, observed_weight
        weight = WEIGHTS[name]
        weighted_sum += weight * _clamp(value)
        observed_weight += weight
        contributors.append({
            "signal": name, "value": round(_clamp(value), 3),
            "weight": weight, "why": why,
        })

    # ---- retrieval: the one that cannot be faked by typing differently ---
    have_retrieval = (retrieval or {}).get("status") == "ok" \
        and (retrieval or {}).get("adjusted_rate") is not None
    if have_retrieval:
        rate = retrieval["adjusted_rate"]
        contribute("retrieval", 1.0 - rate,
                   f"you recalled {round(rate * 100)}% of checked concepts unaided")

    # ---- paste ratio: a measurement, not an inference ---------------------
    if paste_ratio is not None:
        contribute("paste", paste_ratio,
                   f"{round(paste_ratio * 100)}% of recent characters were pasted")

    # ---- session shape: multivariate, so harder to imitate ----------------
    anomaly = behavioural_anomaly or {}
    if anomaly.get("status") == "ok" and anomaly.get("score") is not None:
        # The forest's score sits around 0.5 for ordinary points; rescale so
        # "ordinary" contributes nothing rather than half a signal.
        shaped = _clamp((anomaly["score"] - 0.45) / 0.35)
        contribute("session_shape", shaped,
                   "this session was put together differently from your usual"
                   if anomaly.get("unusual") else
                   "session shape is close to your usual")

    # ---- rhythm: cheap, gameable, weighted low ----------------------------
    deviation = rhythm_deviation or {}
    if deviation.get("status") == "ok" and deviation.get("z_score") is not None:
        # One-sided: only typing MORE regular than their own norm is a
        # transcription signal. A z of +2.5 maps to 1.0.
        shaped = _clamp(deviation["z_score"] / 2.5)
        contribute("rhythm", shaped,
                   "your typing was more even than your usual pattern"
                   if shaped > 0.4 else "your typing rhythm looked normal for you")

    # ---- tab switching: near-zero weight, kept for explanation ------------
    if tab_switch_rate is not None:
        contribute("tab_switching", _clamp(tab_switch_rate / 20.0),
                   "more tab switching than usual" if tab_switch_rate > 12
                   else "tab switching looked ordinary")

    # ---- refuse when too little was observed -----------------------------
    if observed_weight < MIN_EVIDENCE_WEIGHT or n_sessions < 3:
        return _insufficient(contributors, observed_weight, n_sessions)

    risk = weighted_sum / observed_weight

    # Confidence is how much of the available evidence was actually
    # present, capped hard when retrieval is missing.
    confidence = observed_weight / sum(WEIGHTS.values())
    if not have_retrieval:
        confidence = min(confidence, CONFIDENCE_CAP_WITHOUT_RETRIEVAL)
    # A thin history limits confidence regardless of how many signals fired.
    confidence = min(confidence, _clamp(n_sessions / 10.0))

    level = ("high" if risk >= HIGH_ABOVE
             else "moderate" if risk >= MODERATE_ABOVE
             else "low")

    return {
        "status": "ok",
        "risk": round(risk, 3),
        "level": level,
        "confidence": round(confidence, 2),
        "evidence_weight": round(observed_weight, 2),
        "retrieval_available": have_retrieval,
        "contributors": sorted(contributors,
                               key=lambda c: -(c["weight"] * c["value"])),
        "summary": _summary(level, confidence, have_retrieval),
        # Repeated on every response, deliberately. A caller that renders
        # `level` without this is making a claim the data cannot support,
        # and putting it in the payload is the only way to make ignoring it
        # a decision rather than an oversight.
        "not_proof": (
            "A behavioural estimate, not proof of AI use or academic "
            "misconduct. It describes patterns in how work was produced, "
            "and there are ordinary explanations for every one of them."),
    }


def _insufficient(contributors, observed_weight, n_sessions):
    return {
        "status": "insufficient_evidence",
        "risk": None, "level": None,
        "confidence": 0.0,
        "evidence_weight": round(observed_weight, 2),
        "retrieval_available": False,
        "contributors": contributors,
        "summary": ("Not enough evidence for an estimate yet — "
                    f"{n_sessions} session(s) and only part of the signals "
                    "available."),
        "not_proof": (
            "A behavioural estimate, not proof of AI use or academic "
            "misconduct."),
    }


def _summary(level, confidence, have_retrieval):
    """The sentence a student reads. Never 'you are AI dependent'."""
    percent = round(confidence * 100)
    if level == "high":
        head = f"High dependency-risk behavioural pattern — confidence {percent}%."
    elif level == "moderate":
        head = f"Some dependency-risk signals present — confidence {percent}%."
    else:
        head = f"No dependency-risk pattern detected — confidence {percent}%."

    if not have_retrieval:
        return (head + " Based on behaviour only; a retrieval check would say "
                       "far more than any of these signals can.")
    return head + " Behaviour and retrieval evidence agree on this."
