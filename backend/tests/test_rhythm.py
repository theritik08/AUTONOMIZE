"""Tests for rhythm.py — the composing-vs-transcribing signal.

The claim this module makes is narrow and worth stating before testing it:
typing that *copies* existing text is more temporally regular than typing
that *composes* new text. These tests check that the measure actually
separates those two shapes, that it refuses to answer when it doesn't have
enough to go on, and — the part that matters most — that it can never cost
a student points by accident.
"""
import pytest

import rhythm
import scoring


# Two histograms standing in for the two behaviours. Composing spreads its
# intervals across the range and includes real deliberation pauses;
# transcribing piles up in one or two fast buckets with almost no pauses.
COMPOSING = dict(
    iki_buckets=[20, 60, 90, 80, 60, 50, 30, 10],
    long_pauses=30, burst_keys=80, typed_chars=400,
)
TRANSCRIBING = dict(
    iki_buckets=[10, 300, 80, 5, 3, 1, 1, 0],
    long_pauses=1, burst_keys=310, typed_chars=400,
)


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def test_transcribing_scores_far_more_regular_than_composing():
    composing = rhythm.features(**COMPOSING)["regularity_index"]
    transcribing = rhythm.features(**TRANSCRIBING)["regularity_index"]
    assert transcribing > composing
    # A wide margin, not a coin flip. If this narrows, the measure has
    # stopped separating the two things it exists to separate.
    assert transcribing - composing > 0.4


def test_regularity_is_bounded_to_the_unit_interval():
    for case in (COMPOSING, TRANSCRIBING):
        value = rhythm.features(**case)["regularity_index"]
        assert 0.0 <= value <= 1.0


def test_interval_spread_is_the_dominant_term():
    """Entropy of the histogram carries the signal; the other two adjust it.

    Stated as a test because the weights are hand-set and this is the
    assumption behind them — if a change inverts it, the module is
    measuring something other than what its docstring claims.
    """
    flat = rhythm.features(iki_buckets=[50] * 8, long_pauses=20, burst_keys=50,
                           typed_chars=400)
    spiked = rhythm.features(iki_buckets=[0, 400, 0, 0, 0, 0, 0, 0], long_pauses=20,
                             burst_keys=50, typed_chars=400)
    assert spiked["regularity_index"] > flat["regularity_index"]
    assert flat["interval_spread"] > spiked["interval_spread"]


def test_no_buckets_reports_no_data():
    result = rhythm.features(iki_buckets=None, long_pauses=0, burst_keys=0, typed_chars=500)
    assert result["status"] == "no_data"
    assert result["regularity_index"] is None


def test_wrong_bucket_count_is_refused_rather_than_guessed():
    """A client on different bucket edges must not be silently compared.

    Merging or truncating would produce a confident number whose buckets
    mean two different things — worse than no number at all.
    """
    result = rhythm.features(iki_buckets=[1, 2, 3], long_pauses=0, burst_keys=0,
                             typed_chars=500)
    assert result["status"] == "malformed"
    assert result["regularity_index"] is None


def test_too_few_keystrokes_declines_to_answer():
    result = rhythm.features(iki_buckets=[2, 3, 1, 0, 0, 0, 0, 0], long_pauses=0,
                             burst_keys=3, typed_chars=6)
    assert result["status"] == "insufficient_keystrokes"
    assert result["regularity_index"] is None


def test_median_interval_is_interpolated_from_the_histogram():
    # Everything in the fastest bucket -> the median must land there too.
    fast = rhythm.features(iki_buckets=[400, 0, 0, 0, 0, 0, 0, 0], long_pauses=0,
                           burst_keys=400, typed_chars=400)
    slow = rhythm.features(iki_buckets=[0, 0, 0, 0, 0, 0, 0, 400], long_pauses=400,
                           burst_keys=0, typed_chars=400)
    assert fast["median_interval_ms"] < slow["median_interval_ms"]


def test_negative_and_none_counts_are_tolerated():
    result = rhythm.features(iki_buckets=[100, None, -5, 100, 0, 0, 0, 0],
                             long_pauses=None, burst_keys=None, typed_chars=None)
    assert result["status"] == "ok"
    assert 0.0 <= result["regularity_index"] <= 1.0


# ---------------------------------------------------------------------------
# Per-user baseline
# ---------------------------------------------------------------------------

def test_first_observation_seeds_the_baseline():
    updated = rhythm.update_rhythm_baseline(None, 0.4)
    assert updated == {"rhythm_mean": 0.4, "rhythm_var": 0.0, "rhythm_n": 1}


def test_ema_uses_the_previous_mean_for_the_variance_term():
    """Same property scoring.update_baseline relies on.

    Computing `diff` against the updated mean systematically
    under-estimates the variance, which would inflate every later z-score.
    """
    baseline = {"rhythm_mean": 0.5, "rhythm_var": 0.0, "rhythm_n": 3}
    updated = rhythm.update_rhythm_baseline(baseline, 0.9)
    alpha = rhythm.RHYTHM_EMA_ALPHA
    diff = 0.9 - 0.5
    assert updated["rhythm_mean"] == pytest.approx(0.5 + alpha * diff)
    assert updated["rhythm_var"] == pytest.approx((1 - alpha) * (0.0 + alpha * diff * diff))
    assert updated["rhythm_n"] == 4


def test_no_regularity_leaves_the_baseline_alone():
    """A run of short sessions must not erase weeks of accumulated rhythm."""
    assert rhythm.update_rhythm_baseline({"rhythm_mean": 0.5, "rhythm_n": 9}, None) is None


# ---------------------------------------------------------------------------
# Deviation and penalty
# ---------------------------------------------------------------------------

def _baseline(mean, var, n):
    return {"rhythm_mean": mean, "rhythm_var": var, "rhythm_n": n}


def test_no_baseline_yields_no_deviation():
    assert rhythm.rhythm_deviation(0.8, None)["status"] == "no_baseline"


def test_deviation_is_gated_on_observation_count():
    result = rhythm.rhythm_deviation(0.95, _baseline(0.3, 0.01, 2))
    assert result["status"] == "insufficient_data"
    assert result["z_score"] is None


def test_becoming_much_more_regular_than_usual_is_flagged():
    result = rhythm.rhythm_deviation(0.9, _baseline(0.3, 0.01, 20))
    assert result["status"] == "ok"
    assert result["z_score"] > 0
    assert result["level"] == "high"


def test_becoming_less_regular_is_never_flagged():
    """One-sided by design: an erratic session is a hard problem or a
    distracted afternoon, not evidence of copying."""
    result = rhythm.rhythm_deviation(0.05, _baseline(0.6, 0.01, 20))
    assert result["z_score"] < 0
    assert result["level"] == "low"
    assert rhythm.penalty_weight(result) == 0.0


def test_a_very_consistent_user_is_protected_by_the_std_floor():
    """Without MIN_RHYTHM_STD a near-zero variance turns a trivial wobble
    into an enormous z — and it would fire hardest on the most consistent
    students, which is the worst possible false-positive profile."""
    result = rhythm.rhythm_deviation(0.62, _baseline(0.60, 0.0, 40))
    assert result["std_dev"] == rhythm.MIN_RHYTHM_STD
    assert result["level"] == "low"


def test_penalty_ramps_between_the_two_thresholds():
    weak = rhythm.penalty_weight({"status": "ok", "z_score": rhythm.Z_MODERATE})
    middle = rhythm.penalty_weight({"status": "ok", "z_score": 2.0})
    strong = rhythm.penalty_weight({"status": "ok", "z_score": 5.0})
    assert weak == 0.0
    assert 0.0 < middle < 1.0
    assert strong == 1.0


def test_penalty_is_zero_whenever_the_signal_is_uncertain():
    for status in ("no_baseline", "insufficient_data"):
        assert rhythm.penalty_weight({"status": status, "z_score": None}) == 0.0


def test_explain_is_silent_unless_there_is_something_to_say():
    assert rhythm.explain({"status": "no_baseline"}) is None
    assert rhythm.explain({"status": "ok", "level": "low"}) is None
    assert rhythm.explain({"status": "ok", "level": "high"})


# ---------------------------------------------------------------------------
# Integration with scoring — the property that matters most
# ---------------------------------------------------------------------------

WRITING = {"category": "writing", "active_ms": 30 * 60_000,
           "typed_chars": 1000, "pasted_chars": 0,
           "backspace_count": 40, "revision_count": 5, "likely_ai_pastes": 0}


def test_absent_rhythm_scores_exactly_as_before():
    """An older extension build, or a session too short to measure, must
    never cost a student points. A signal that isn't there is not evidence
    against anyone."""
    assert scoring.compute_session_score(WRITING) == \
        scoring.compute_session_score(WRITING, rhythm_penalty=0.0)


# WRITING alone is a perfect session (100 * typed_ratio + the full
# engagement bonus), so its raw score exceeds 100 and clamps. Measuring the
# penalty's exact size needs a case with headroom below the ceiling.
MIXED = {**WRITING, "typed_chars": 700, "pasted_chars": 300}


def test_a_transcription_signal_lowers_the_score():
    clean = scoring.compute_session_score(MIXED)
    flagged = scoring.compute_session_score(MIXED, rhythm_penalty=1.0)
    assert flagged < clean
    assert clean - flagged == pytest.approx(scoring.W_RHYTHM_PENALTY)


def test_the_penalty_is_not_swallowed_by_the_ceiling():
    """Regression test for an ordering bug found by end-to-end testing.

    The writing formula can produce a raw score above 100 (fully typed,
    plenty of redrafting -> 100 + 12). Subtracting the rhythm penalty
    before clamping meant the first 12 points disappeared into headroom
    the student never sees, so a full-strength flag cost 3 points instead
    of 15 — and it failed hardest on exactly the shape rhythm exists to
    catch: someone typing out an AI answer has typed_ratio 1.0 and no
    pastes, which is what produces that headroom in the first place.
    """
    clean = scoring.compute_session_score(WRITING)
    flagged = scoring.compute_session_score(WRITING, rhythm_penalty=1.0)
    assert clean == 100.0
    assert clean - flagged == pytest.approx(scoring.W_RHYTHM_PENALTY)


def test_assessment_mode_weights_rhythm_harder_than_writing():
    assert scoring.ASSESS_W_RHYTHM_PENALTY > scoring.W_RHYTHM_PENALTY


def test_the_typed_ai_answer_case_is_now_distinguishable():
    """The whole point of this module.

    A student who reads an AI answer and types it produces a perfect
    typed_ratio and no pastes at all — the pre-rhythm formula gives them
    100. With a confident rhythm signal they are separable from someone who
    genuinely wrote the same volume.
    """
    typed_out_the_ai_answer = scoring.compute_session_score(WRITING, rhythm_penalty=1.0)
    genuinely_composed = scoring.compute_session_score(WRITING, rhythm_penalty=0.0)
    assert genuinely_composed == 100.0
    assert typed_out_the_ai_answer < 100.0


def test_penalty_cannot_push_a_score_below_zero_or_be_over_applied():
    paste_heavy = {**WRITING, "typed_chars": 10, "pasted_chars": 1000}
    assert scoring.compute_session_score(paste_heavy, rhythm_penalty=1.0) >= 0.0
    # Out-of-range input is clamped, not trusted.
    assert scoring.compute_session_score(WRITING, rhythm_penalty=99.0) == \
        scoring.compute_session_score(WRITING, rhythm_penalty=1.0)
    assert scoring.compute_session_score(WRITING, rhythm_penalty=-5.0) == \
        scoring.compute_session_score(WRITING, rhythm_penalty=0.0)


def test_update_baseline_carries_rhythm_state_alongside_the_score():
    updated = scoring.update_baseline(None, 80.0, "2026-01-01", regularity=0.42)
    assert updated["ema_mean"] == 80.0
    assert updated["rhythm_mean"] == 0.42
    assert updated["rhythm_n"] == 1


def test_update_baseline_preserves_rhythm_when_a_session_has_none():
    existing = {"ema_mean": 70.0, "ema_var": 4.0, "streak_days": 2,
                "last_active_date": "2026-01-01", "n_observations": 6,
                "rhythm_mean": 0.55, "rhythm_var": 0.02, "rhythm_n": 6}
    updated = scoring.update_baseline(existing, 75.0, "2026-01-02", regularity=None)
    assert updated["rhythm_mean"] == 0.55
    assert updated["rhythm_n"] == 6
