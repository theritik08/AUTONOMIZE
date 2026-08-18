"""Autonomize scoring.

Two ideas from the project blueprint, implemented as a lightweight,
fully-explainable MVP (the contextual bandit / nudge-timing model is future
work — see README):

1. Per-session "independence score" (0-100) built from PROCESS signals
   (how the work was produced) rather than an after-the-fact judgement of
   the output. We never see the output at all — only typed vs. pasted
   character volume, revision behaviour, and whether a paste is likely to
   have come straight from an AI chat tab / homework-answer site (via the
   cross-tab correlation signal computed in the extension's background
   worker).

2. A per-user, per-CATEGORY baseline: every score is compared to THAT
   USER'S OWN exponential moving average for that context, not a
   population norm and not mixed across contexts. A 70 on ordinary writing
   and a 70 during a graded quiz mean very different things, so they get
   independent baselines (see db.get_baseline / db.save_baseline, keyed by
   (user_id, category)).

STRICT MODE — "assessment" sessions (quizzes/exams/graded assignment
portals, see extension/site-map.js) use a harsher formula than plain
"writing" sessions: any paste is inherently more suspicious in a graded
context (not just AI-correlated ones), AI-correlated pastes are penalized
much more heavily, and switching away from the tab during the session
(possible AI-tool lookup) costs points beyond a small free allowance.
"""
import math

import rhythm

# Sessions shorter than this are too noisy to score. Assessments are allowed
# a lower bar since a quiz attempt can legitimately be short.
MIN_ACTIVE_MS_WRITING = 20_000
MIN_ACTIVE_MS_ASSESSMENT = 10_000

EMA_ALPHA = 0.25

# ---------------------------------------------------------------------------
# Writing-mode weights
# ---------------------------------------------------------------------------
W_TYPED_RATIO = 100.0
W_ENGAGEMENT_BONUS = 12.0
W_AI_CORRELATION_PENALTY = 22.0

# ---------------------------------------------------------------------------
# Assessment-mode weights (strict) — hand-tuned to punish paste-heavy and
# AI-correlated behaviour far harder than the writing formula does, and to
# add a real (if small) cost for leaving the tab during the attempt.
# ---------------------------------------------------------------------------
ASSESS_W_TYPED_RATIO = 100.0
ASSESS_W_PASTE_PENALTY = 40.0          # any paste at all counts against you here
ASSESS_W_AI_CORRELATION_PENALTY = 45.0  # correlated pastes hit much harder than in writing mode
ASSESS_TAB_SWITCH_FREE = 2              # first couple of tab switches aren't penalized
ASSESS_TAB_SWITCH_COST = 3.0
ASSESS_TAB_SWITCH_MAX_PENALTY = 20.0

# ---------------------------------------------------------------------------
# Typing-rhythm penalty (see rhythm.py)
# ---------------------------------------------------------------------------
# Closes the largest hole in the formulas above: a student who reads an AI
# answer and TYPES it scores 100, because every character genuinely came
# from their keyboard and no paste ever happened.
#
# Deliberately small relative to the paste signals. Rhythm is a weaker,
# noisier signal than a paste correlated with an AI tab, and it is measured
# against the user's own history rather than any absolute standard, so it
# should nudge a score rather than decide it. It is also strictly one-sided
# and gated: it only ever applies when the deviation module is confident
# (>= 5 prior rhythm observations for that user and category), so a new
# user is never penalised by it.
#
# Assessment mode weights it higher for the same reason it weights
# everything higher — during a graded attempt the base rate of legitimate
# transcription is much lower.
W_RHYTHM_PENALTY = 15.0
ASSESS_W_RHYTHM_PENALTY = 25.0


def _apply_rhythm(base: float, weight: float, penalty: float) -> float:
    """Clamps the base score first, THEN subtracts the rhythm penalty.

    Order matters here, and getting it wrong makes the signal nearly
    useless in the one case it was built for. The writing formula can
    produce a raw score above 100 — a session that is entirely typed with
    plenty of redrafting scores 100 + 12 = 112 — and a student typing out
    an AI answer produces exactly that shape: typed_ratio 1.0, no pastes.
    Subtracting inside the clamp meant the first 12 points of penalty
    vanished into headroom the student never saw, so a full-strength
    rhythm flag cost 3 points instead of 15.

    Clamping first makes the penalty mean the same thing everywhere: a
    confident transcription signal always costs its full weight off the
    score the student is actually shown.
    """
    return max(0.0, min(100.0, base) - weight * penalty)


def _writing_score(row: dict, rhythm_penalty: float = 0.0) -> float:
    typed = row.get("typed_chars", 0) or 0
    pasted = row.get("pasted_chars", 0) or 0
    backspaces = row.get("backspace_count", 0) or 0
    revisions = row.get("revision_count", 0) or 0
    ai_pastes = row.get("likely_ai_pastes", 0) or 0

    total_input = typed + pasted
    typed_ratio = typed / total_input

    # Editing your own words (backspacing, undoing, redrafting) is a sign of
    # genuine independent composition, not a penalty — but we cap the bonus
    # so someone can't game the score by mashing backspace.
    revision_signal = (backspaces + revisions) / max(1, typed / 50)
    engagement = min(1.0, revision_signal)

    # Each large paste that happened within 10 minutes of visiting an AI
    # chat tool / answer site is a strong "this was probably lifted from
    # elsewhere" signal, diminishing in marginal impact via sqrt.
    ai_correlation = min(1.0, math.sqrt(ai_pastes) / 3.0) if ai_pastes else 0.0

    score = (
        W_TYPED_RATIO * typed_ratio
        + W_ENGAGEMENT_BONUS * engagement
        - W_AI_CORRELATION_PENALTY * ai_correlation
    )
    return _apply_rhythm(score, W_RHYTHM_PENALTY, rhythm_penalty)


def _assessment_score(row: dict, rhythm_penalty: float = 0.0) -> float:
    typed = row.get("typed_chars", 0) or 0
    pasted = row.get("pasted_chars", 0) or 0
    ai_pastes = row.get("likely_ai_pastes", 0) or 0
    tab_switches = row.get("tab_switch_count", 0) or 0

    total_input = typed + pasted
    typed_ratio = typed / total_input
    paste_ratio = pasted / total_input

    # Saturates faster than writing mode (2 instead of 3): even 1-2
    # AI-correlated pastes during a graded attempt is already a strong signal.
    ai_correlation = min(1.0, math.sqrt(ai_pastes) / 2.0) if ai_pastes else 0.0

    extra_switches = max(0, tab_switches - ASSESS_TAB_SWITCH_FREE)
    tab_penalty = min(ASSESS_TAB_SWITCH_MAX_PENALTY, extra_switches * ASSESS_TAB_SWITCH_COST)

    score = (
        ASSESS_W_TYPED_RATIO * typed_ratio
        - ASSESS_W_PASTE_PENALTY * paste_ratio
        - ASSESS_W_AI_CORRELATION_PENALTY * ai_correlation
        - tab_penalty
    )
    return _apply_rhythm(score, ASSESS_W_RHYTHM_PENALTY, rhythm_penalty)


def risk_level(score: float) -> str:
    """Student-facing label for an assessment-session score."""
    if score >= 70:
        return "low"
    if score >= 40:
        return "medium"
    return "high"


def compute_session_score(session_row: dict, rhythm_penalty: float = 0.0):
    """Returns a 0-100 float, or None if the session is too short/empty or
    the category (e.g. ai_assistant) isn't scored at all.

    `rhythm_penalty` is a 0-1 scalar from rhythm.penalty_weight(). It
    defaults to 0 so every existing caller — and every session from an
    extension build that predates rhythm capture — scores exactly as it
    did before. A signal that is absent must never cost a student points.
    """
    rhythm_penalty = max(0.0, min(1.0, rhythm_penalty or 0.0))
    category = session_row.get("category")
    active_ms = session_row.get("active_ms", 0) or 0
    typed = session_row.get("typed_chars", 0) or 0
    pasted = session_row.get("pasted_chars", 0) or 0
    total_input = typed + pasted

    if category == "writing":
        if active_ms < MIN_ACTIVE_MS_WRITING or total_input == 0:
            return None
        return _writing_score(session_row, rhythm_penalty)

    if category == "assessment":
        if active_ms < MIN_ACTIVE_MS_ASSESSMENT or total_input == 0:
            return None
        return _assessment_score(session_row, rhythm_penalty)

    return None  # ai_assistant sessions are never scored — no "independence" axis there


def update_baseline(existing: dict | None, score: float, date_str: str,
                    regularity: float | None = None):
    """EMA update of a user's personal (per-category) baseline + a simple
    daily streak.

    Returns a dict ready for db.save_baseline(**dict).
    Streak rule (intentionally simple for the MVP): the first qualifying
    session on a NEW calendar day extends the streak if that session's score
    is at/above the baseline mean *going into* that update; otherwise the
    streak resets to 0. A day with no qualifying session neither extends nor
    resets it (so a single skipped day doesn't nuke a streak — only a
    below-baseline day does).
    """
    if existing is None or existing.get("ema_mean") is None:
        ema_mean = score
        ema_var = 0.0
        streak_days = 1
        last_active_date = date_str
        n_observations = 1
    else:
        prev_mean = existing["ema_mean"]
        prev_var = existing.get("ema_var") or 0.0
        prev_streak = existing.get("streak_days") or 0
        prev_date = existing.get("last_active_date")

        diff = score - prev_mean
        ema_mean = prev_mean + EMA_ALPHA * diff
        ema_var = (1 - EMA_ALPHA) * (prev_var + EMA_ALPHA * diff * diff)

        if prev_date == date_str:
            # already updated today — keep streak as-is, just refresh EMA
            streak_days = prev_streak
        elif score >= prev_mean:
            streak_days = prev_streak + 1
        else:
            # Decay by one, not a reset to zero.
            #
            # A hard reset is the habit-app convention and it is wrong for
            # this product. Everything else here is deliberately
            # non-punitive — nudges are prompts, nothing about an
            # individual reaches an instructor, the explanation text says
            # "visible to you only" — and then a single below-baseline day
            # erased a fortnight of sustained work. That is a punishment,
            # and it lands hardest on someone having one difficult week in
            # an otherwise good term.
            #
            # Decay keeps the streak meaning "sustained effort recently"
            # while making one bad day cost one day. It also makes the
            # feature safer as a bandit input: `streak_days` is a context
            # feature, and a variable that periodically slams to zero is a
            # discontinuity the linear model has to absorb as noise.
            streak_days = max(0, prev_streak - 1)
        last_active_date = date_str
        # How many scores this baseline has actually seen. The EMA itself
        # is memoryless, but anomaly.py needs to know whether "the user's
        # usual pattern" is built from three sessions or three hundred
        # before it flags anything as unusual.
        n_observations = (existing.get("n_observations") or 0) + 1

    out = {
        "ema_mean": ema_mean,
        "ema_var": ema_var,
        "streak_days": streak_days,
        "last_active_date": last_active_date,
        "last_score": score,
        "n_observations": n_observations,
    }

    # Rhythm state rides along in the same row, but on its own counter and
    # its own update. Carried forward unchanged when this session produced
    # no usable rhythm, so a run of short sessions cannot quietly erase a
    # baseline that took weeks to build.
    updated_rhythm = rhythm.update_rhythm_baseline(existing, regularity)
    if updated_rhythm is not None:
        out.update(updated_rhythm)
    elif existing:
        out.update({
            "rhythm_mean": existing.get("rhythm_mean"),
            "rhythm_var": existing.get("rhythm_var"),
            "rhythm_n": existing.get("rhythm_n") or 0,
        })
    else:
        out.update({"rhythm_mean": None, "rhythm_var": None, "rhythm_n": 0})

    return out
