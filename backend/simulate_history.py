r"""Generate a synthetic multi-student history, for exercising the trainer.

    python3 simulate_history.py            # writes into the configured database
    python3 simulate_history.py --students 60 --days 120

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------

`train_model.py` needs history to train on, and nobody has used the product
yet. This generates some.

It is NOT a claim about students, and no number derived from it should be
presented as one. What it is good for is the thing that actually needs
checking: that the training pipeline is correct. Each simulated student
carries a latent "reliance drift" that the observable features only
partially reveal, so a model that beats the baselines here is picking up
structure rather than memorising noise — and a leak in the feature builder
would show up as an implausibly good score.

Sessions are written by replaying them through the REAL scoring path —
rhythm.features, rhythm_deviation, scoring.compute_session_score,
scoring.update_baseline — rather than by inserting scores directly. A
generator that wrote its own scores would be testing itself.
"""

import math, os, random, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or '.')

import db, scoring, rhythm

import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--students", type=int, default=60)
parser.add_argument("--days", type=int, default=120)
parser.add_argument("--seed", type=int, default=20260813)
args = parser.parse_args()

rng = random.Random(args.seed)
N_STUDENTS = args.students
DAYS = args.days

def clamp(v, lo, hi): return max(lo, min(hi, v))

def make_buckets(regularity_target):
    """A histogram whose entropy lands near the requested regularity."""
    if regularity_target > 0.55:          # transcription-like: concentrated
        base = [8, 220, 60, 6, 3, 2, 1, 0]
    elif regularity_target > 0.35:        # mixed
        base = [15, 90, 80, 40, 20, 10, 5, 2]
    else:                                  # composing: spread
        base = [20, 55, 85, 75, 60, 45, 28, 12]
    return [max(0, int(b * rng.uniform(0.7, 1.3))) for b in base]

db.init_db()
now = int(time.time() * 1000)
rows = []

for s in range(N_STUDENTS):
    user = f"sim-{s:03d}"
    # Latent trajectory: some students drift toward reliance, some improve,
    # most wander. The model can only see behaviour, never this.
    drift = rng.choice([-0.55, -0.25, 0.0, 0.0, 0.15, 0.35])
    reliance = clamp(rng.gauss(0.30, 0.12), 0.02, 0.75)
    skill = rng.uniform(0.55, 0.95)          # per-student typing consistency
    cadence = rng.uniform(0.45, 0.95)        # how often they work

    for d in range(DAYS, 0, -1):
        if rng.random() > cadence:
            continue
        # Reliance drifts, with week-to-week autocorrelation and shocks.
        reliance = clamp(reliance + drift / DAYS + rng.gauss(0, 0.035), 0.0, 0.95)
        if rng.random() < 0.03:              # deadline week
            reliance = clamp(reliance + rng.uniform(0.1, 0.3), 0.0, 0.95)

        assessment = rng.random() < 0.18
        total = int(rng.uniform(400, 2600))
        pasted = int(total * clamp(rng.gauss(reliance * 0.55, 0.08), 0.0, 0.9))
        typed = max(30, total - pasted)
        ai_pastes = 0
        if pasted > 60 and rng.random() < reliance:
            ai_pastes = rng.randint(1, 4)

        # Typing regularity tracks reliance: a student leaning on AI types
        # things out more often. Noisy, and modulated by their own skill.
        reg_target = clamp(0.20 + reliance * 0.55 + rng.gauss(0, 0.07) - (skill - 0.75) * 0.15, 0.03, 0.95)
        buckets = make_buckets(reg_target)

        started = now - d * 86400_000 + rng.randint(0, 20) * 3600_000
        row = {
            "session_id": str(uuid.uuid4()),
            "user_id": user,
            "category": "assessment" if assessment else "writing",
            "domain": "docs.google.com",
            "started_at": started,
            "active_ms": int(rng.uniform(12, 70) * 60000),
            "typed_chars": typed, "pasted_chars": pasted,
            "backspace_count": int(typed / rng.uniform(18, 60)),
            "revision_count": int(typed / rng.uniform(180, 700)),
            "prompt_count": 0,
            "likely_ai_pastes": ai_pastes,
            "tab_switch_count": rng.randint(0, 7) if assessment else rng.randint(0, 3),
            "iki_buckets": buckets,
            "long_pauses": int(sum(buckets) * clamp(0.14 - reg_target * 0.12, 0.005, 0.2)),
            "burst_keys": int(sum(buckets) * clamp(reg_target * 0.8, 0.05, 0.9)),
        }
        rows.append(row)

rows.sort(key=lambda r: r["started_at"])
print(f"generated {len(rows)} sessions for {N_STUDENTS} students over {DAYS} days")

# Replay through the REAL scoring path so scores, rhythm and baselines are
# produced exactly as production would produce them.
import json
with db.get_conn() as conn:
    baselines = {}
    for r in rows:
        feats = rhythm.features(iki_buckets=r["iki_buckets"], long_pauses=r["long_pauses"],
                               burst_keys=r["burst_keys"], typed_chars=r["typed_chars"])
        regularity = feats.get("regularity_index")
        key = (r["user_id"], r["category"])
        base = baselines.get(key)
        dev = rhythm.rhythm_deviation(regularity, base)
        penalty = rhythm.penalty_weight(dev)
        score = scoring.compute_session_score(r, rhythm_penalty=penalty)
        if score is None:
            continue
        conn.execute(db.q("""INSERT INTO sessions
            (session_id,user_id,category,domain,started_at,active_ms,typed_chars,
             pasted_chars,backspace_count,revision_count,prompt_count,likely_ai_pastes,
             tab_switch_count,finalized,score,created_at,updated_at,iki_buckets,
             long_pauses,burst_keys,regularity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""),
            (r["session_id"], r["user_id"], r["category"], r["domain"], r["started_at"],
             r["active_ms"], r["typed_chars"], r["pasted_chars"], r["backspace_count"],
             r["revision_count"], 0, r["likely_ai_pastes"], r["tab_switch_count"], 1,
             score, r["started_at"], r["started_at"], json.dumps(r["iki_buckets"]),
             r["long_pauses"], r["burst_keys"], regularity))
        updated = scoring.update_baseline(base, score, "2026-01-01", regularity=regularity)
        baselines[key] = updated
print("scored and inserted through the real pipeline")
