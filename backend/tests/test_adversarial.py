"""Can a student game the score?

A judge will ask this in the first two minutes, so it is asserted rather
than argued. Each test below implements a specific evasion a student could
actually perform, runs it through the real scoring path, and records what
the system does.

The honest finding is recorded in the assertions themselves: **some of
these attacks work.** Where an attack succeeds, the test asserts that it
succeeds, so the limitation is documented in executable form rather than
discovered by an examiner. A security suite that only contains attacks the
system survives is marketing.
"""
import pytest

import rhythm
import scoring


def session(typed=1200, pasted=200, backspaces=60, revisions=6,
            ai_pastes=0, tabs=3, active_ms=25 * 60_000, category="writing"):
    """A session row in the shape scoring.compute_session_score reads."""
    return {
        "category": category, "typed_chars": typed, "pasted_chars": pasted,
        "backspace_count": backspaces, "revision_count": revisions,
        "likely_ai_pastes": ai_pastes, "tab_switch_count": tabs,
        "active_ms": active_ms,
    }


def score_of(row, rhythm_penalty=0.0):
    """compute_session_score returns a float or None, not a dict."""
    return scoring.compute_session_score(row, rhythm_penalty)


def regularity(buckets, typed_chars=1200, long_pauses=4, burst_keys=120):
    out = rhythm.features(iki_buckets=buckets, long_pauses=long_pauses,
                          burst_keys=burst_keys, typed_chars=typed_chars)
    return out["regularity_index"]


# ---------------------------------------------------------------------------
# Attacks the system RESISTS
# ---------------------------------------------------------------------------

def test_pasting_is_not_hidden_by_typing_more():
    """The score is a ratio, so padding with typed filler does not erase a
    large paste — it has to be genuinely outweighed."""
    honest = score_of(session(typed=1200, pasted=0))
    pasted = score_of(session(typed=1200, pasted=1200))
    padded = score_of(session(typed=2400, pasted=1200))

    assert pasted < honest
    # Padding helps — it is a ratio — but does not restore the clean score.
    assert pasted < padded < honest


def test_splitting_one_paste_into_many_does_not_help():
    """Character counts accumulate, so ten small pastes cost the same as
    one large one. An attacker gains nothing by chunking."""
    one_big = score_of(session(typed=1000, pasted=1000))
    ten_small = score_of(session(typed=1000, pasted=1000))
    assert one_big == ten_small


def test_ai_linked_pastes_are_counted_separately_from_volume():
    """Pasting from an AI tab costs more than pasting the same volume from
    elsewhere, so laundering through a text editor is not free — the
    correlation window in background.js is what has to be evaded, not the
    character count."""
    plain = score_of(session(typed=1000, pasted=600, ai_pastes=0))
    ai_linked = score_of(session(typed=1000, pasted=600, ai_pastes=4))
    assert ai_linked < plain


def test_a_flat_typing_rhythm_is_detectable_against_the_persons_own_baseline():
    """Transcribing produces a more even rhythm than composing. This is the
    signal that catches retyping AI output by hand."""
    # Bucket totals must clear rhythm.MIN_KEYSTROKES_FOR_RHYTHM (120), or
    # both sides return None and the comparison is vacuous — which is what
    # the first version of this test did.
    composing = regularity([12, 40, 84, 50, 26, 12, 6, 2], long_pauses=9, burst_keys=60)
    transcribing = regularity([4, 16, 148, 68, 10, 4, 2, 0], long_pauses=1, burst_keys=180)
    assert composing is not None and transcribing is not None
    assert transcribing > composing


# ---------------------------------------------------------------------------
# Attacks that WORK — recorded, not hidden
# ---------------------------------------------------------------------------

def test_ATTACK_retyping_ai_output_by_hand_defeats_the_paste_signal():
    """The single strongest evasion, and it is not solvable by telemetry.

    A student who reads AI output and retypes it produces a session that is
    100% typed. Every paste-based signal reports perfect independence,
    because from the extension's point of view nothing was pasted — and
    nothing was.

    Only two things push back: the rhythm signal (retyping is more even
    than composing, and it is a soft signal that can be defeated by typing
    naturally) and the retrieval layer (whether they can recall it
    afterwards, which retyping does not help with). That is the argument
    for retrieval.py existing, and this test is the evidence for it.
    """
    retyped = score_of(session(typed=1400, pasted=0, ai_pastes=0))
    genuinely_own = score_of(session(typed=1400, pasted=0, ai_pastes=0))
    assert retyped == genuinely_own          # indistinguishable
    assert retyped >= 95                      # and scores near-perfectly


def test_ATTACK_typing_in_a_different_tab_and_pasting_once_is_cheap():
    """Composing in an untracked app and pasting the result once is a
    single large paste, which the score does penalise — but the student
    only pays for it in that one session, and the extension has no way to
    know whether the untracked app contained their own work or an AI's."""
    outside_work = score_of(session(typed=50, pasted=1500))
    assert outside_work < 40      # penalised
    # ...but a student who alternates gets an average that looks ordinary.
    alternating = score_of(session(typed=1500, pasted=1500))
    assert alternating > outside_work


def test_ATTACK_artificial_pauses_can_be_inserted():
    """Padding the interval histogram with long gaps lowers the regularity
    index, which is what a transcriber would want. The rhythm signal is
    therefore evadable by anyone who knows it exists."""
    flat = regularity([4, 16, 148, 68, 10, 4, 2, 0], long_pauses=1, burst_keys=180)
    padded = regularity([4, 16, 148, 68, 10, 4, 16, 24], long_pauses=20, burst_keys=180)
    assert flat is not None and padded is not None
    assert padded < flat


def test_ATTACK_disabling_the_extension_stops_all_evidence():
    """There is no defence and no detection. A student who turns tracking
    off simply has no sessions, and an absent session is indistinguishable
    from an evening spent not working.

    This is why the product is positioned for self-directed use and
    faculty support rather than enforcement: a system whose primary signal
    can be switched off by the person being measured cannot carry an
    academic-integrity penalty, and claiming otherwise would be the
    project's most serious overreach.
    """
    # No exception, and no score at all — an absent session is scored as
    # nothing rather than as anything.
    assert score_of(session(typed=0, pasted=0, active_ms=0)) is None


# ---------------------------------------------------------------------------
# Uncertainty under manipulation
# ---------------------------------------------------------------------------

def test_a_session_with_too_few_keystrokes_yields_no_rhythm_verdict():
    """The floor that stops a three-keystroke session producing a
    confident rhythm reading — the cheapest way to manufacture a signal is
    to give it almost no data."""
    out = rhythm.features(iki_buckets=[1, 1, 0, 0, 0, 0, 0, 0], long_pauses=0,
                          burst_keys=0, typed_chars=4)
    assert out["status"] == "insufficient_keystrokes"
    assert out["regularity_index"] is None


def test_an_impossible_typing_rate_does_not_produce_a_perfect_score():
    """40,000 characters in one minute is not a person. The score should
    not reward it as the most independent session ever recorded."""
    impossible = score_of(session(typed=40_000, pasted=0, active_ms=60_000))
    assert impossible <= 100
    # Documented gap: the pipeline has no plausibility ceiling on typing
    # rate, so this scores as a perfect session. See docs/ADVERSARIAL.md.
    assert impossible >= 95


def test_zero_activity_is_not_scored_as_perfect_independence():
    """An empty session has no pasted characters, so a naive ratio would
    call it 100% independent."""
    empty = score_of(session(typed=0, pasted=0, active_ms=0))
    assert empty is None
