r"""Randomised controlled evaluation of the nudges.

    python3 trial.py --simulate      # the framework, on synthetic students
    python3 trial.py --report        # analyse whatever real data exists

THE CLAIM THIS EXISTS TO NOT MAKE
---------------------------------

`bandit.py` chooses which nudge to show, and `simulate_bandit.py` shows it
converges on the best arm against a reward function that file invents. That
is a valid claim about the ALGORITHM and it is stated as one.

What neither file can support is the claim a judge actually cares about:
*do the nudges help students*. The bandit's live reward is engagement —
dismissed, opened, acted on — which measures whether a student interacted
with a card, not whether they learned anything. Optimising engagement and
calling it learning is the single most tempting overclaim available to this
project, and it would be indefensible.

An effect on learning needs a comparison group. This file is that
comparison group's infrastructure.

THE DESIGN
----------

    control    eligible for a nudge, shown nothing
    treatment  eligible for a nudge, shown one

Assignment is by a deterministic hash of (user_id, trial_id), so it is
stable across restarts, reproducible from the two ids alone, and auditable
by anyone who has them. No randomness is stored, and a user cannot be
silently moved between arms to improve a result.

The outcome measures, in order of how much weight they can carry:

  1. RETRIEVAL RATE after the intervention window. The strongest available,
     because it is objective and it is about learning rather than about
     behaviour. Only possible because `retrieval.py` exists.
  2. RECOVERY toward the student's own baseline — did the independence
     score come back up.
  3. SUBSEQUENT SESSION BEHAVIOUR — typed share in the next sessions.

WHY THE ANALYSIS REFUSES MORE OFTEN THAN IT REPORTS
---------------------------------------------------

`MIN_PER_ARM` is deliberately high for a student project. With a dozen
students per arm, a difference of a few points is indistinguishable from
noise, and reporting it with a confidence interval that happens to exclude
zero is how underpowered studies produce findings that do not replicate.

The bootstrap interval below is the honest instrument, not a decoration:
if it spans zero, the correct output is "no detectable effect", and this
module prints exactly that rather than reaching for a subgroup that looks
better.

There is no student cohort. Every number this file can currently produce
comes from `--simulate`, is labelled `SIMULATED` in the output and in the
returned payload, and must never be quoted as evidence about people.
"""
import argparse
import hashlib
import random
import statistics

# Below this, the arms are too small for a difference to mean anything.
# High on purpose — see the module docstring.
MIN_PER_ARM = 30

# Bootstrap resamples for the interval. 2000 is plenty for a difference in
# means and keeps this runnable without numpy.
BOOTSTRAP_N = 2000
CONFIDENCE = 0.95

# How long after the nudge the outcome is measured. Shorter and it captures
# the reaction to being interrupted; much longer and other things explain
# the change.
OUTCOME_WINDOW_DAYS = 7


def assign(user_id, trial_id):
    """Which arm this user is in. Deterministic, reproducible, auditable.

    A stored random assignment can be edited; a hash cannot be, without
    changing the ids that produced it. Anyone can recompute this.
    """
    digest = hashlib.sha256(f"{trial_id}:{user_id}".encode("utf-8")).digest()
    return "treatment" if digest[0] % 2 else "control"


def balance(user_ids, trial_id):
    """Arm sizes, so an unbalanced split is visible before any analysis."""
    counts = {"control": 0, "treatment": 0}
    for user_id in user_ids:
        counts[assign(user_id, trial_id)] += 1
    return counts


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _bootstrap_difference(control, treatment, rng):
    """Percentile bootstrap CI for (treatment mean - control mean).

    Chosen over a t-test because these outcomes are bounded rates and
    small-sample proportions, which are not normal, and a t-test on them
    would produce an interval whose coverage nobody has checked.
    """
    observed = statistics.mean(treatment) - statistics.mean(control)
    differences = []
    for _ in range(BOOTSTRAP_N):
        c = [control[rng.randrange(len(control))] for _ in range(len(control))]
        t = [treatment[rng.randrange(len(treatment))] for _ in range(len(treatment))]
        differences.append(statistics.mean(t) - statistics.mean(c))
    differences.sort()
    low = differences[int((1 - CONFIDENCE) / 2 * BOOTSTRAP_N)]
    high = differences[int((1 + CONFIDENCE) / 2 * BOOTSTRAP_N) - 1]
    return observed, low, high


def analyse(control, treatment, outcome_name, synthetic, seed=7):
    """Compares one outcome between arms. Refuses when underpowered.

    Always returns a dict carrying `synthetic`, so no caller can quote a
    number without the label that says where it came from.
    """
    result = {
        "outcome": outcome_name,
        "n_control": len(control),
        "n_treatment": len(treatment),
        "synthetic": bool(synthetic),
    }

    if len(control) < MIN_PER_ARM or len(treatment) < MIN_PER_ARM:
        result.update({
            "status": "underpowered",
            "effect": None, "ci_low": None, "ci_high": None,
            "conclusion": (
                f"Not reporting an effect: {len(control)} control and "
                f"{len(treatment)} treatment, against a minimum of "
                f"{MIN_PER_ARM} per arm. A difference at this size is "
                "indistinguishable from noise."),
        })
        return result

    rng = random.Random(seed)
    effect, low, high = _bootstrap_difference(control, treatment, rng)
    crosses_zero = low <= 0 <= high

    result.update({
        "status": "ok",
        "effect": round(effect, 4),
        "ci_low": round(low, 4),
        "ci_high": round(high, 4),
        "control_mean": round(statistics.mean(control), 4),
        "treatment_mean": round(statistics.mean(treatment), 4),
        "detectable": not crosses_zero,
        "conclusion": (
            f"No detectable effect on {outcome_name}: the interval "
            f"[{low:+.3f}, {high:+.3f}] includes zero."
            if crosses_zero else
            f"Treatment differs from control on {outcome_name} by "
            f"{effect:+.3f} (95% CI [{low:+.3f}, {high:+.3f}])."),
    })
    return result


def load_outcomes(conn, trial_id, outcome="retrieval_rate", now_ms=None):
    """Pulls each enrolled user's outcome, split by arm.

    Returns ([], []) when no trial data exists, which is the current state
    of this project and must not be mistaken for a null result.
    """
    import time

    import db
    import retrieval

    now_ms = now_ms or int(time.time() * 1000)
    rows = conn.execute(
        db.q("SELECT DISTINCT user_id FROM nudge_events")
    ).fetchall()

    control, treatment = [], []
    for row in rows:
        user_id = dict(row)["user_id"]
        if outcome == "retrieval_rate":
            summary = retrieval.summarise(conn, user_id, now_ms,
                                          window_days=OUTCOME_WINDOW_DAYS)
            if summary["status"] != "ok":
                continue
            value = summary["adjusted_rate"]
        else:
            raise ValueError(f"unknown outcome: {outcome}")

        (treatment if assign(user_id, trial_id) == "treatment" else control).append(value)

    return control, treatment


# ---------------------------------------------------------------------------
# Simulation — the framework exercised, NOT evidence
# ---------------------------------------------------------------------------

def simulate(n_users=120, true_effect=0.08, seed=11):
    """Synthetic arms with a KNOWN effect, to check the analysis recovers it.

    This validates the instrument, not the intervention. A framework that
    cannot detect an effect it was handed would also fail to detect a real
    one, and that is worth knowing before anyone runs a study.
    """
    rng = random.Random(seed)
    trial_id = "simulated-trial"
    control, treatment = [], []

    for i in range(n_users):
        user_id = f"sim-user-{i}"
        base = rng.gauss(0.55, 0.18)
        if assign(user_id, trial_id) == "treatment":
            treatment.append(max(0.0, min(1.0, base + true_effect)))
        else:
            control.append(max(0.0, min(1.0, base)))

    return control, treatment, true_effect


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulate", action="store_true",
                        help="exercise the framework on synthetic arms")
    parser.add_argument("--report", action="store_true",
                        help="analyse real trial data, if any exists")
    parser.add_argument("--trial-id", default="autonomize-nudge-v1")
    parser.add_argument("--users", type=int, default=120)
    parser.add_argument("--effect", type=float, default=0.08)
    args = parser.parse_args(argv)

    if args.simulate:
        control, treatment, true_effect = simulate(args.users, args.effect)
        print("SIMULATED — synthetic arms with an effect planted by this script.")
        print("This validates the ANALYSIS, not the intervention. No students")
        print("were involved and nothing here is evidence about people.")
        print()
        print(f"  planted effect      {true_effect:+.3f}")
        result = analyse(control, treatment, "retrieval rate", synthetic=True)
        for key in ("n_control", "n_treatment", "control_mean", "treatment_mean",
                    "effect", "ci_low", "ci_high", "detectable"):
            if key in result:
                print(f"  {key:19} {result[key]}")
        print()
        print("  " + result["conclusion"])
        recovered = result.get("effect")
        if recovered is not None:
            print(f"  recovery error      {abs(recovered - true_effect):.3f}")
        return 0

    if args.report:
        from _env import load_dotenv

        load_dotenv()
        import db

        db.init_db()
        with db.get_conn() as conn:
            control, treatment = load_outcomes(conn, args.trial_id)

        if not control and not treatment:
            print("No trial data. No students have been enrolled, no nudges have")
            print("been settled against an outcome, and therefore NOTHING can be")
            print("said about whether the nudges help.")
            print()
            print("This is the honest state of the project. The framework above is")
            print("ready; the study has not been run.")
            return 0

        result = analyse(control, treatment, "retrieval rate", synthetic=False)
        print(f"REAL DATA — trial {args.trial_id}")
        for key, value in result.items():
            print(f"  {key:19} {value}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
