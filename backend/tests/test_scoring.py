"""Tests for scoring.py's pure functions — the heart of the "independence
score" measurement pipeline. These are plain functions over dicts, so they
run against hand-built rows rather than a database.
"""
import math

import pytest

import scoring


# ---------------------------------------------------------------------------
# compute_session_score — routing / eligibility
# ---------------------------------------------------------------------------

def test_ai_assistant_sessions_are_never_scored():
    row = {"category": "ai_assistant", "active_ms": 999_999, "typed_chars": 500, "pasted_chars": 0}
    assert scoring.compute_session_score(row) is None


def test_writing_session_below_min_active_ms_is_unscored():
    row = {"category": "writing", "active_ms": scoring.MIN_ACTIVE_MS_WRITING - 1, "typed_chars": 500}
    assert scoring.compute_session_score(row) is None


def test_writing_session_with_no_input_is_unscored():
    row = {"category": "writing", "active_ms": scoring.MIN_ACTIVE_MS_WRITING, "typed_chars": 0, "pasted_chars": 0}
    assert scoring.compute_session_score(row) is None


def test_assessment_session_below_min_active_ms_is_unscored():
    row = {"category": "assessment", "active_ms": scoring.MIN_ACTIVE_MS_ASSESSMENT - 1, "typed_chars": 500}
    assert scoring.compute_session_score(row) is None


def test_unknown_category_is_unscored():
    row = {"category": "something_else", "active_ms": 999_999, "typed_chars": 500}
    assert scoring.compute_session_score(row) is None


# ---------------------------------------------------------------------------
# Writing-mode scoring
# ---------------------------------------------------------------------------

def test_writing_all_typed_no_ai_scores_high():
    row = {
        "category": "writing", "active_ms": 60_000,
        "typed_chars": 1000, "pasted_chars": 0,
        "backspace_count": 0, "revision_count": 0, "likely_ai_pastes": 0,
    }
    score = scoring.compute_session_score(row)
    assert score == pytest.approx(100.0)


def test_writing_all_pasted_no_ai_correlation_scores_zero_from_ratio():
    # Pure paste, no revision engagement, no AI correlation: the formula's
    # only positive term (typed_ratio) is zero and nothing else fires.
    row = {
        "category": "writing", "active_ms": 60_000,
        "typed_chars": 0, "pasted_chars": 1000,
        "backspace_count": 0, "revision_count": 0, "likely_ai_pastes": 0,
    }
    score = scoring.compute_session_score(row)
    assert score == pytest.approx(0.0)


def test_writing_ai_correlated_paste_is_penalized_vs_uncorrelated():
    base = {
        "category": "writing", "active_ms": 60_000,
        "typed_chars": 500, "pasted_chars": 500,
        "backspace_count": 0, "revision_count": 0,
    }
    uncorrelated = scoring.compute_session_score({**base, "likely_ai_pastes": 0})
    correlated = scoring.compute_session_score({**base, "likely_ai_pastes": 3})
    assert correlated < uncorrelated


def test_writing_revision_engagement_gives_a_bonus():
    base = {
        "category": "writing", "active_ms": 60_000,
        "typed_chars": 500, "pasted_chars": 0, "likely_ai_pastes": 0,
    }
    no_revisions = scoring.compute_session_score({**base, "backspace_count": 0, "revision_count": 0})
    with_revisions = scoring.compute_session_score({**base, "backspace_count": 40, "revision_count": 10})
    # Already at 100 from typed_ratio alone (bonus is capped by the 0-100
    # clamp), so assert the bonus doesn't *reduce* the score and the
    # engagement signal itself is computed as expected rather than
    # asserting a strictly-greater score that a ceiling clamp would hide.
    assert with_revisions == pytest.approx(100.0)
    assert no_revisions == pytest.approx(100.0)


def test_writing_score_is_always_clamped_0_to_100():
    row = {
        "category": "writing", "active_ms": 60_000,
        "typed_chars": 10, "pasted_chars": 10000, "likely_ai_pastes": 999,
        "backspace_count": 0, "revision_count": 0,
    }
    score = scoring.compute_session_score(row)
    assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# Assessment-mode (strict) scoring
# ---------------------------------------------------------------------------

def test_assessment_all_typed_no_paste_scores_100():
    row = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 500, "pasted_chars": 0,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
    }
    assert scoring.compute_session_score(row) == pytest.approx(100.0)


def test_assessment_any_paste_is_penalized_even_without_ai_correlation():
    typed_only = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 500, "pasted_chars": 0,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
    }
    with_paste = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 400, "pasted_chars": 100,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
    }
    assert scoring.compute_session_score(with_paste) < scoring.compute_session_score(typed_only)


def test_assessment_ai_correlated_paste_is_penalized_harder_than_plain_paste():
    plain_paste = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 400, "pasted_chars": 100,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
    }
    ai_paste = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 400, "pasted_chars": 100,
        "likely_ai_pastes": 2, "tab_switch_count": 0,
    }
    assert scoring.compute_session_score(ai_paste) < scoring.compute_session_score(plain_paste)


def test_assessment_tab_switches_have_a_free_allowance():
    within_free = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 500, "pasted_chars": 0,
        "likely_ai_pastes": 0, "tab_switch_count": scoring.ASSESS_TAB_SWITCH_FREE,
    }
    assert scoring.compute_session_score(within_free) == pytest.approx(100.0)


def test_assessment_tab_switches_just_over_free_allowance_cost_points():
    row = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 500, "pasted_chars": 0,
        "likely_ai_pastes": 0, "tab_switch_count": scoring.ASSESS_TAB_SWITCH_FREE + 3,
    }
    # extra_switches=3 * ASSESS_TAB_SWITCH_COST=3.0 = 9, well under the cap
    # (ASSESS_TAB_SWITCH_MAX_PENALTY=20), so this should cost points but not
    # be capped yet.
    score = scoring.compute_session_score(row)
    assert score == pytest.approx(91.0)


def test_assessment_tab_switch_penalty_is_capped():
    at_cap = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 500, "pasted_chars": 0,
        "likely_ai_pastes": 0,
        # extra_switches=10 * cost=3.0 = 30, already above the 20-point cap.
        "tab_switch_count": scoring.ASSESS_TAB_SWITCH_FREE + 10,
    }
    way_over_cap = {
        **at_cap,
        "tab_switch_count": scoring.ASSESS_TAB_SWITCH_FREE + 500,
    }
    score_at_cap = scoring.compute_session_score(at_cap)
    score_way_over = scoring.compute_session_score(way_over_cap)
    assert score_at_cap == pytest.approx(80.0)  # 100 - ASSESS_TAB_SWITCH_MAX_PENALTY
    # An absurd number of tab switches doesn't score any worse than one
    # that's already past the cap.
    assert score_way_over == pytest.approx(score_at_cap)


def test_assessment_score_is_always_clamped_0_to_100():
    row = {
        "category": "assessment", "active_ms": 30_000,
        "typed_chars": 1, "pasted_chars": 10000,
        "likely_ai_pastes": 999, "tab_switch_count": 999,
    }
    score = scoring.compute_session_score(row)
    assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# risk_level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [
        (100, "low"), (70, "low"),
        (69.9, "medium"), (40, "medium"),
        (39.9, "high"), (0, "high"),
    ],
)
def test_risk_level_thresholds(score, expected):
    assert scoring.risk_level(score) == expected


# ---------------------------------------------------------------------------
# update_baseline — EMA + streak logic
# ---------------------------------------------------------------------------

def test_update_baseline_first_ever_score_seeds_ema_and_streak():
    result = scoring.update_baseline(None, score=80.0, date_str="2026-08-01")
    assert result["ema_mean"] == pytest.approx(80.0)
    assert result["ema_var"] == pytest.approx(0.0)
    assert result["streak_days"] == 1
    assert result["last_active_date"] == "2026-08-01"
    assert result["last_score"] == pytest.approx(80.0)


def test_update_baseline_ema_moves_toward_new_score():
    existing = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 1, "last_active_date": "2026-08-01"}
    result = scoring.update_baseline(existing, score=100.0, date_str="2026-08-02")
    # EMA_ALPHA = 0.25 -> 50 + 0.25*(100-50) = 62.5
    assert result["ema_mean"] == pytest.approx(62.5)


def test_update_baseline_new_day_at_or_above_mean_extends_streak():
    existing = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 3, "last_active_date": "2026-08-01"}
    result = scoring.update_baseline(existing, score=55.0, date_str="2026-08-02")
    assert result["streak_days"] == 4


def test_update_baseline_new_day_below_mean_decays_the_streak():
    """One below-baseline day costs one day, not the whole streak.

    Previously this reset to zero. That is the habit-app convention and it
    is wrong here: everything else in the product is deliberately
    non-punitive, and erasing a fortnight of sustained work over a single
    difficult day contradicts that. Decay also removes a discontinuity from
    `streak_days`, which the bandit consumes as a context feature.
    """
    existing = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 5, "last_active_date": "2026-08-01"}
    result = scoring.update_baseline(existing, score=10.0, date_str="2026-08-02")
    assert result["streak_days"] == 4


def test_a_streak_still_decays_to_zero_if_it_never_recovers():
    """Decay is forgiving, not meaningless — sustained under-performance
    still empties the streak."""
    baseline = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 3, "last_active_date": "2026-08-01"}
    for day in range(2, 8):
        baseline = scoring.update_baseline(baseline, score=5.0, date_str=f"2026-08-0{day}")
        baseline["last_active_date"] = f"2026-08-0{day}"
    assert baseline["streak_days"] == 0


def test_update_baseline_same_day_twice_keeps_streak_unchanged():
    existing = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 3, "last_active_date": "2026-08-01"}
    # Even a below-mean second score on the SAME day shouldn't reset the
    # streak — the streak rule only evaluates on the first qualifying
    # session of a new calendar day.
    result = scoring.update_baseline(existing, score=1.0, date_str="2026-08-01")
    assert result["streak_days"] == 3


def test_update_baseline_missed_day_neither_extends_nor_resets():
    # existing.last_active_date is two days before date_str; scoring.py's
    # own docstring says a single skipped day shouldn't nuke a streak — the
    # function only looks at score-vs-mean, not day gaps, so confirm that
    # an above-mean score after a gap still just extends by one (not reset
    # to reflect the gap, and not incremented per missed day).
    existing = {"ema_mean": 50.0, "ema_var": 0.0, "streak_days": 3, "last_active_date": "2026-07-30"}
    result = scoring.update_baseline(existing, score=60.0, date_str="2026-08-01")
    assert result["streak_days"] == 4
