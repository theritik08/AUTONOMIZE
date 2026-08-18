r"""The four interpretable states, from behaviour AND retrieval together.

WHY A STATE AND NOT JUST THE SCORE
----------------------------------

The independence score is a number, and a number invites a comparison it
cannot support — "I got 71, is that good?" A state answers the question a
student is actually asking: *is the way I am working now a problem?*

More importantly, a state can express something the score cannot. A score
of 55 is one number with at least two very different meanings:

  - assisted work, and the student can still do it unaided  -> fine
  - assisted work, and the student can no longer do it      -> the problem

Behaviour alone cannot separate those. Retrieval alone cannot either — a
student who never uses AI and simply has not learned the material looks
identical to one who has forgotten it. Only the two axes together
distinguish them, which is the whole argument for `retrieval.py` existing.

    retrieval
      high  |  Independent          Assisted but Engaged
            |
      low   |  Struggling*          High Dependency Risk
            +-----------------------------------------
               independent            assisted
                        behaviour

    * `Struggling` is deliberately NOT one of the four states the brief
      asked for. Low retrieval with independent behaviour means the
      student is doing their own work and finding it hard, which is not a
      dependency problem and must not be labelled as one. It maps to
      `Independent` with a note, because the intervention it calls for is
      teaching, not a nudge about AI use.

WHAT THESE STATES ARE NOT
-------------------------

Not psychological claims. Not diagnoses. Not predictions about a person.
They are labels on a two-axis measurement over a recent window, and every
one of them is reported with the evidence that produced it and with the
confidence of that evidence.

No state is ever assigned from a single feature, and no state is assigned
at all while either axis is still warming up — `insufficient_evidence` is
a state, and it is the honest one for a new user.
"""

# Where "assisted" begins. The independence score is 0-100 and its weights
# are hand-set (see scoring.py), so these are presentation thresholds on an
# already-uncalibrated quantity — stated as judgements, not derived.
BEHAVIOUR_ASSISTED_BELOW = 60.0
BEHAVIOUR_INDEPENDENT_ABOVE = 75.0

# Retrieval is guess-corrected before it reaches here (see
# retrieval.summarise), so 0.5 means half the concepts recalled above
# chance rather than half the questions answered.
RETRIEVAL_LOW_BELOW = 0.45
RETRIEVAL_HIGH_ABOVE = 0.65

# A single session cannot move a state. Dependency is a trend, and a
# label that flips on one bad evening is a label nobody trusts.
MIN_SESSIONS = 5

STATES = (
    "insufficient_evidence",
    "independent",
    "assisted_but_engaged",
    "increasing_dependency",
    "high_dependency_risk",
)

LABELS = {
    "insufficient_evidence": "Still learning your pattern",
    "independent": "Independent",
    "assisted_but_engaged": "Assisted but engaged",
    "increasing_dependency": "Increasing dependency",
    "high_dependency_risk": "High dependency risk",
}


def classify(behaviour_score, behaviour_trend, retrieval, n_sessions,
             baseline_mean=None):
    """The state, the evidence behind it, and how much to trust it.

    `retrieval` is `retrieval.summarise()` output — or None when the
    student has never answered a check, which is the normal case and must
    degrade gracefully rather than blocking a state entirely.
    """
    evidence = []

    if n_sessions < MIN_SESSIONS or behaviour_score is None:
        return _state("insufficient_evidence", "low", [
            f"{n_sessions} of {MIN_SESSIONS} sessions recorded",
        ], "A state needs a few sessions before it means anything.")

    retrieval_status = (retrieval or {}).get("status")
    retrieval_rate = (retrieval or {}).get("adjusted_rate")
    have_retrieval = retrieval_status == "ok" and retrieval_rate is not None

    # ---- the behaviour axis ------------------------------------------
    if behaviour_score >= BEHAVIOUR_INDEPENDENT_ABOVE:
        behaviour = "independent"
        evidence.append(f"independence score {round(behaviour_score)} — most work was your own")
    elif behaviour_score < BEHAVIOUR_ASSISTED_BELOW:
        behaviour = "assisted"
        evidence.append(f"independence score {round(behaviour_score)} — a large share was pasted")
    else:
        behaviour = "mixed"
        evidence.append(f"independence score {round(behaviour_score)} — a mix of typed and pasted work")

    declining = behaviour_trend == "declining"
    if declining:
        evidence.append("your recent sessions are trending downward")
    if baseline_mean is not None and behaviour_score < baseline_mean - 10:
        evidence.append(f"well below your own baseline of {round(baseline_mean)}")

    # ---- without retrieval, say so and stop at behaviour --------------
    if not have_retrieval:
        note = (retrieval or {}).get("message") or "No retrieval checks answered yet."
        evidence.append(note)
        if behaviour == "assisted" and declining:
            return _state("increasing_dependency", "low", evidence,
                          "Based on behaviour alone. A few retrieval checks would "
                          "show whether this is affecting what you can recall.")
        if behaviour == "assisted":
            return _state("assisted_but_engaged", "low", evidence,
                          "Assisted work is not itself a problem. Retrieval checks "
                          "are what would show whether you can still do it unaided.")
        return _state("independent", "low", evidence,
                      "Based on behaviour alone — no retrieval evidence yet.")

    # ---- both axes present -------------------------------------------
    if retrieval_rate >= RETRIEVAL_HIGH_ABOVE:
        recall = "high"
        evidence.append(f"you recall {round(retrieval_rate * 100)}% of checked concepts unaided")
    elif retrieval_rate < RETRIEVAL_LOW_BELOW:
        recall = "low"
        evidence.append(f"you recall {round(retrieval_rate * 100)}% of checked concepts unaided")
    else:
        recall = "mixed"
        evidence.append(f"you recall {round(retrieval_rate * 100)}% of checked concepts unaided")

    if (retrieval or {}).get("trend") == "declining":
        evidence.append("your retrieval has fallen compared with earlier checks")

    confidence = "high" if (retrieval or {}).get("n_checks", 0) >= 6 else "medium"

    # The quadrant. Note that low recall with INDEPENDENT behaviour is not
    # a dependency state — see the module docstring.
    if behaviour == "assisted" and recall == "low":
        return _state("high_dependency_risk", confidence, evidence,
                      "Most of this work was assisted, and the concepts are not "
                      "coming back to you unaided. This is the combination worth "
                      "acting on — not the AI use by itself.")

    if behaviour == "assisted" and recall == "mixed" and \
            (declining or (retrieval or {}).get("trend") == "declining"):
        return _state("increasing_dependency", confidence, evidence,
                      "Assisted work with recall slipping. Worth trying a session "
                      "unaided before the next deadline.")

    if behaviour in ("assisted", "mixed") and recall == "high":
        return _state("assisted_but_engaged", confidence, evidence,
                      "You are using help and still retrieving the concepts "
                      "yourself. That is what good use of a tool looks like.")

    if behaviour == "mixed" and recall == "low":
        return _state("increasing_dependency", confidence, evidence,
                      "A mixed working pattern with weak recall. More of the "
                      "next session done unaided would show which way this goes.")

    if behaviour == "independent" and recall == "low":
        # Deliberately not a dependency label. See the docstring.
        return _state("independent", confidence, evidence,
                      "You are doing the work yourself and finding the material "
                      "hard. That is a teaching question, not a dependency one.")

    return _state("independent", confidence, evidence,
                  "You are working independently and the concepts are staying "
                  "with you.")


def _state(name, confidence, evidence, message):
    assert name in STATES, name
    return {
        "state": name,
        "label": LABELS[name],
        "confidence": confidence,
        # Every state ships the reasons that produced it. A label without
        # its evidence is exactly the kind of opaque judgement this project
        # exists to argue against.
        "evidence": evidence,
        "message": message,
        # Restated on every response so no client can render this as a
        # verdict about a person.
        "disclaimer": ("A description of recent measurements, not a judgement "
                       "about you. Visible to you only."),
    }
