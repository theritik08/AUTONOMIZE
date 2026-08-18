"""The combined estimate, and whether an informed attacker defeats it.

The threat model is a student who has read this repository. Every test in
the second section defeats one signal deliberately and checks what the
combined estimate does — the useful property is not that the risk stays
high (it often should not), but that CONFIDENCE falls when the signals
that remain are the cheap ones.
"""
import pytest

import dependency_risk as dr


def ok_retrieval(rate, n=8):
    return {"status": "ok", "adjusted_rate": rate, "n_checks": n, "trend": "steady"}


def anomaly(score, unusual=False):
    return {"status": "ok", "score": score, "unusual": unusual}


def rhythm_z(z):
    return {"status": "ok", "z_score": z, "level": "medium"}


FULL = dict(score=45.0, baseline_mean=70.0, paste_ratio=0.6,
            rhythm_deviation=rhythm_z(2.0), behavioural_anomaly=anomaly(0.7, True),
            retrieval=ok_retrieval(0.15), tab_switch_rate=14, n_sessions=20)


# ---------------------------------------------------------------------------
# The weighting is the design
# ---------------------------------------------------------------------------

def test_retrieval_carries_more_weight_than_every_typing_signal_combined():
    """Weighted by cost to fake, not by how discriminative it looks.
    Retrieval cannot be defeated by changing how you type."""
    typing = dr.WEIGHTS["rhythm"] + dr.WEIGHTS["tab_switching"] + dr.WEIGHTS["session_shape"]
    assert dr.WEIGHTS["retrieval"] > typing


def test_the_most_gameable_signal_has_the_least_influence():
    """test_adversarial.py shows rhythm is defeated by inserting pauses.
    A signal that cheap must not drive the estimate."""
    assert dr.WEIGHTS["rhythm"] < dr.WEIGHTS["paste"]
    assert dr.WEIGHTS["tab_switching"] == min(dr.WEIGHTS.values())


def test_weights_sum_to_one_so_confidence_is_a_real_fraction():
    assert sum(dr.WEIGHTS.values()) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# An informed attacker
# ---------------------------------------------------------------------------

def test_ATTACK_defeating_rhythm_alone_barely_moves_the_estimate():
    """A student who inserts artificial pauses defeats the rhythm signal.
    Because it carries 11%, the estimate should barely notice."""
    with_rhythm = dr.estimate(**FULL)
    beaten = dr.estimate(**{**FULL, "rhythm_deviation": rhythm_z(-0.5)})
    assert abs(with_rhythm["risk"] - beaten["risk"]) < 0.12
    assert beaten["level"] in ("moderate", "high")


def test_ATTACK_imitating_normal_composing_across_every_typing_signal():
    """The strongest realistic attack: a student who has read this repo and
    controls rhythm, tab switching AND session shape to look ordinary —
    while still pasting and still unable to recall the material.

    Retrieval and paste ratio are what remain, and they are the two most
    expensive signals to fake. The estimate must survive."""
    imitating = dr.estimate(
        score=45.0, baseline_mean=70.0,
        paste_ratio=0.6,                       # still pasting
        rhythm_deviation=rhythm_z(0.0),        # imitated
        behavioural_anomaly=anomaly(0.45),     # imitated
        retrieval=ok_retrieval(0.15),          # cannot imitate
        tab_switch_rate=3, n_sessions=20)
    assert imitating["level"] in ("moderate", "high")
    assert imitating["retrieval_available"] is True


def test_ATTACK_avoiding_pastes_by_retyping_is_caught_by_retrieval():
    """Retyping AI output defeats every paste signal — test_adversarial.py
    asserts that. Retrieval is what is left, and it is unaffected."""
    retyped = dr.estimate(
        score=95.0, baseline_mean=70.0,
        paste_ratio=0.0,                       # nothing pasted
        rhythm_deviation=rhythm_z(0.3),
        behavioural_anomaly=anomaly(0.5),
        retrieval=ok_retrieval(0.10),          # cannot recall any of it
        tab_switch_rate=2, n_sessions=20)
    assert retyped["risk"] > 0.35
    top = retyped["contributors"][0]
    assert top["signal"] == "retrieval"


def test_the_same_attacker_with_good_recall_is_NOT_flagged():
    """The control that stops the test above being vacuous. Identical
    behaviour, opposite recall — and using AI while still understanding the
    work is explicitly not the thing being measured."""
    engaged = dr.estimate(**{**FULL, "retrieval": ok_retrieval(0.9)})
    dependent = dr.estimate(**{**FULL, "retrieval": ok_retrieval(0.1)})
    assert engaged["risk"] < dependent["risk"]
    assert engaged["level"] != "high"


# ---------------------------------------------------------------------------
# Uncertainty — the part that must never be optimistic
# ---------------------------------------------------------------------------

def test_missing_retrieval_caps_confidence_however_many_signals_agree():
    """Every behavioural signal can be gamed by an informed student, so
    their agreement cannot produce high confidence on its own."""
    behaviour_only = dr.estimate(**{**FULL, "retrieval": None})
    assert behaviour_only["confidence"] <= dr.CONFIDENCE_CAP_WITHOUT_RETRIEVAL
    assert "retrieval check would say far more" in behaviour_only["summary"]


def test_a_thin_history_limits_confidence_regardless_of_signals():
    thin = dr.estimate(**{**FULL, "n_sessions": 4})
    thick = dr.estimate(**{**FULL, "n_sessions": 30})
    assert thin["confidence"] < thick["confidence"]


def test_too_few_signals_produces_insufficient_evidence_not_a_low_risk():
    """A missing signal is not a clean signal. Reporting 'low risk' from
    almost no evidence is the failure that would let this be used
    punitively in reverse."""
    sparse = dr.estimate(score=None, baseline_mean=None, paste_ratio=None,
                         rhythm_deviation=None, behavioural_anomaly=None,
                         retrieval=None, n_sessions=1)
    assert sparse["status"] == "insufficient_evidence"
    assert sparse["level"] is None and sparse["risk"] is None


def test_a_missing_signal_is_dropped_from_the_denominator():
    """Otherwise an absent signal reads as a zero — i.e. as evidence of
    innocence — and the estimate drifts down for students the system
    simply cannot see."""
    full = dr.estimate(**FULL)
    without_tabs = dr.estimate(**{**FULL, "tab_switch_rate": None})
    assert without_tabs["evidence_weight"] < full["evidence_weight"]
    assert without_tabs["risk"] > 0.3


# ---------------------------------------------------------------------------
# The ethical boundary, asserted in code
# ---------------------------------------------------------------------------

def test_every_response_carries_the_not_proof_disclaimer():
    for payload in (dr.estimate(**FULL),
                    dr.estimate(**{**FULL, "n_sessions": 1})):
        assert "not proof" in payload["not_proof"].lower()
        assert "misconduct" in payload["not_proof"].lower()


def test_no_output_ever_asserts_that_a_student_used_ai():
    """The language rule. 'Dependency-risk behavioural pattern' is a claim
    the data supports; 'this student used AI' is not."""
    for rate in (0.05, 0.5, 0.95):
        payload = dr.estimate(**{**FULL, "retrieval": ok_retrieval(rate)})
        text = (payload["summary"] + " " + payload["not_proof"]).lower()
        for forbidden in ("used ai", "cheated", "cheating", "misconduct detected",
                          "is ai dependent", "plagiar"):
            assert forbidden not in text, forbidden
        assert "behavioural" in text or "behaviour" in text


def test_the_estimate_names_its_contributors_so_it_can_be_argued_with():
    payload = dr.estimate(**FULL)
    assert len(payload["contributors"]) >= 4
    for c in payload["contributors"]:
        assert c["why"] and c["signal"] in dr.WEIGHTS


def test_risk_and_confidence_are_reported_separately():
    """High risk with low confidence is a real and important state: several
    weak signals agreed while the strong one was missing. Collapsing them
    into one number would hide exactly that."""
    payload = dr.estimate(**{**FULL, "retrieval": None})
    assert payload["risk"] is not None
    assert payload["confidence"] < 0.6
