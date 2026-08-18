"""Nudge policy: what the bandit's arms and context actually mean.

`bandit.py` is pure LinUCB. This module supplies everything domain-shaped:
the set of interventions, how a moment gets turned into a feature vector,
how the model is loaded and saved, and — the part that took the most
thought — where the reward signal comes from.

THE REWARD PROBLEM, STATED HONESTLY

A nudge bandit is easy to build badly. The tempting design is "reward = 1
if the student tapped Accept". Optimising that produces a model that
learns to generate agreeable pop-ups, which is not the goal; the goal is
that the student's *next stretch of work is more independent*. Those two
things come apart quickly.

Worse, the most important arm is `none` — not interrupting. An explicit-
feedback-only design can never learn about `none`, because there is no
pop-up to accept or dismiss, so the arm that should often win is the one
arm that never gets a reward. A bandit that structurally cannot evaluate
"leave them alone" will always over-nudge.

So rewards arrive two ways, and every arm can earn them both ways:

  1. Explicit — the client reports what the student did with the nudge
     (POST /api/nudge/feedback). Immediate, sparse, and only available
     for arms that showed something.

  2. Outcome attribution — when the student's next session is scored
     within ATTRIBUTION_WINDOW_MS of a still-unsettled decision, that
     score decides the reward: did the work that followed beat their own
     baseline? This is what makes `none` learnable on the same footing as
     every other arm, and it measures the thing the product actually
     cares about rather than the thing that's easy to collect.

Explicit feedback wins when both are available, because it's a direct
observation rather than an inference. Decisions that get neither inside
the window are settled as `expired` with a neutral reward, so an abandoned
browser tab doesn't quietly bias the model toward whatever arm happened to
be playing.

None of this is switched on for the student yet: the extension does not
call these endpoints (the popup remains display-only, as documented). The
measurement and policy machinery is here and tested; the client-side
surface is the deliberate next step.
"""
import json
import time
import uuid

import bandit
import db

# Arms. `none` is a first-class arm, not the absence of a decision — see
# the module docstring.
ARMS = ("none", "reflect", "pause", "contrast")

ARM_COPY = {
    "none": None,
    "reflect": "Before you move on — can you explain this in your own words?",
    "pause": "You've been leaning on AI tools for a while. Worth a short break from them?",
    "contrast": "Today so far: here's your independent vs. AI-assisted split.",
}

# How long after a decision an outcome can still be attributed to it. Long
# enough to cover finishing the piece of work in progress, short enough
# that tomorrow morning's session isn't credited to yesterday's nudge.
ATTRIBUTION_WINDOW_MS = 2 * 60 * 60 * 1000  # 2 hours

# Rewards, all in [0, 1] to match bandit.DEFAULT_ALPHA's assumption.
REWARD_BY_OUTCOME = {
    "accepted": 1.0,
    "engaged": 0.7,     # interacted without completing (e.g. opened, then left)
    "dismissed": 0.1,   # actively closed — mildly negative, not zero-information
    "ignored": 0.0,
}
NEUTRAL_REWARD = 0.5

CONTEXT_FEATURES = (
    "bias",
    "current_score",
    "delta_vs_baseline",
    "assisted_share_7d",
    "time_of_day",
    "streak",
    "nudge_fatigue",
)
CONTEXT_DIM = len(CONTEXT_FEATURES)


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def build_context(*, current_score, delta_vs_baseline, independent_minutes_7d,
                  assisted_minutes_7d, streak_days, hour_of_day, recent_nudges):
    """Turns the state the API already computes into a feature vector.

    Every feature is scaled into roughly [0, 1] (or [-1, 1] for the signed
    one). LinUCB has no feature normalisation of its own, so unscaled
    inputs would let whichever feature happens to have the largest raw
    magnitude dominate both theta and the uncertainty term.
    """
    total = (independent_minutes_7d or 0) + (assisted_minutes_7d or 0)
    assisted_share = (assisted_minutes_7d / total) if total > 0 else 0.0

    return [
        1.0,                                                    # bias
        _clamp((current_score or 50.0) / 100.0),                 # where they are
        _clamp((delta_vs_baseline or 0.0) / 50.0, -1.0, 1.0),    # vs. their own norm
        _clamp(assisted_share),                                  # how AI-heavy the week is
        _clamp((hour_of_day or 0) / 24.0),                       # late-night work differs
        _clamp((streak_days or 0) / 14.0),                       # momentum
        _clamp((recent_nudges or 0) / 5.0),                      # fatigue: stop pestering
    ]


def explain_context(context):
    return dict(zip(CONTEXT_FEATURES, [round(v, 4) for v in context]))


# ---------------------------------------------------------------------------
# Model persistence
# ---------------------------------------------------------------------------

def load_models(conn, user_id) -> dict:
    """Every arm's model for this user, seeding any arm not yet stored.

    A brand-new user gets identity/zero models for all arms, which is
    exactly the maximum-uncertainty state LinUCB should start from — the
    first few decisions are then genuine exploration rather than a
    hard-coded default.
    """
    models = {}
    for arm in ARMS:
        row = db.get_bandit_arm(conn, user_id, arm)
        if row is None:
            models[arm] = bandit.ArmModel.fresh(CONTEXT_DIM)
            continue
        a_matrix = json.loads(row["a_matrix"])
        b_vector = json.loads(row["b_vector"])
        # Guard against a stored model from an older, differently-shaped
        # context: silently mixing dimensions would corrupt the policy.
        if len(b_vector) != CONTEXT_DIM or len(a_matrix) != CONTEXT_DIM:
            models[arm] = bandit.ArmModel.fresh(CONTEXT_DIM)
            continue
        models[arm] = bandit.ArmModel(a_matrix, b_vector, int(row["n_pulls"] or 0))
    return models


def save_model(conn, user_id, arm, model) -> None:
    db.save_bandit_arm(
        conn, user_id, arm,
        json.dumps(model.a_matrix), json.dumps(model.b_vector), model.n_pulls,
    )


# ---------------------------------------------------------------------------
# Decide / settle
# ---------------------------------------------------------------------------

def decide(conn, user_id, context, now_ms=None) -> dict:
    """Chooses an arm for this moment and records the decision.

    The decision row stores the exact context vector that produced it, so
    when a reward arrives later it updates the model with the state the
    world was in at decision time, not at reward time.
    """
    now_ms = now_ms or int(time.time() * 1000)
    models = load_models(conn, user_id)
    selection = bandit.select_arm(models, context)
    arm = selection["arm"]

    event_id = str(uuid.uuid4())
    db.insert_nudge_event(conn, event_id, user_id, arm, json.dumps(context), now_ms)

    return {
        "event_id": event_id,
        "arm": arm,
        "message": ARM_COPY[arm],
        "scores": {
            name: {k: round(v, 4) for k, v in vals.items()}
            for name, vals in selection["scores"].items()
        },
        "context": explain_context(context),
        "n_pulls": {name: models[name].n_pulls for name in sorted(models)},
    }


def _apply_reward(conn, user_id, event, reward, settled_by) -> None:
    models = load_models(conn, user_id)
    arm = event["arm"]
    model = models.get(arm)
    if model is None:
        return
    model.update(json.loads(event["context"]), reward)
    save_model(conn, user_id, arm, model)
    db.settle_nudge_event(conn, event["event_id"], reward, settled_by)


def record_feedback(conn, user_id, event_id, outcome) -> dict:
    """Explicit client-reported outcome for a specific decision."""
    event = db.get_nudge_event(conn, event_id)
    if event is None:
        return {"ok": False, "reason": "unknown_event"}
    if event["user_id"] != user_id:
        # Rewards are per-user model updates; accepting one for someone
        # else's decision would let any caller poison another user's policy.
        return {"ok": False, "reason": "wrong_user"}
    if event["reward"] is not None:
        return {"ok": False, "reason": "already_settled"}

    reward = REWARD_BY_OUTCOME.get(outcome)
    if reward is None:
        return {"ok": False, "reason": "unknown_outcome"}

    _apply_reward(conn, user_id, event, reward, "feedback")
    return {"ok": True, "arm": event["arm"], "reward": reward}


def settle_pending_outcomes(conn, user_id, score, baseline_mean, now_ms=None) -> list:
    """Attributes a freshly scored session to any decisions still awaiting
    a reward, and expires anything now outside the window.

    Called from the session-upsert path (see main.py) — that is the moment
    the outcome the policy actually cares about becomes known.
    """
    now_ms = now_ms or int(time.time() * 1000)
    window_start = now_ms - ATTRIBUTION_WINDOW_MS

    # Anything older than the window can no longer be judged by this
    # session; retire it neutrally rather than leaving it pending forever.
    for event in db.pending_nudge_events(conn, user_id, 0):
        if event["decided_at"] < window_start:
            _apply_reward(conn, user_id, event, NEUTRAL_REWARD, "expired")

    if score is None:
        return []

    # Beat your own baseline -> the stretch of work after that decision
    # went well. No baseline yet -> neutral, since "better than usual" is
    # undefined for a user with no usual.
    if baseline_mean is None:
        reward = NEUTRAL_REWARD
    else:
        reward = 1.0 if score >= baseline_mean else 0.0

    settled = []
    for event in db.pending_nudge_events(conn, user_id, window_start):
        _apply_reward(conn, user_id, event, reward, "outcome")
        settled.append({"event_id": event["event_id"], "arm": event["arm"], "reward": reward})
    return settled


def recent_nudge_count(conn, user_id, now_ms=None) -> int:
    """Nudges actually shown in the last 24h — the fatigue feature."""
    now_ms = now_ms or int(time.time() * 1000)
    return db.count_nudges_since(conn, user_id, now_ms - 24 * 60 * 60 * 1000)
