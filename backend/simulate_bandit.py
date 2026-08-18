r"""Offline evaluation of the nudge policy against a known optimum.

    python3 simulate_bandit.py                    # default run
    python3 simulate_bandit.py --rounds 4000 --runs 30 --csv regret.csv

WHY THIS EXISTS
---------------

The project's honest weak point is evaluation. There is no pilot cohort, no
labelled data, and therefore no way to claim the *scoring* model measures
what it says it measures — fit_weights.py exists for that and is waiting on
labels.

The bandit is different, and this script exploits the difference. A
contextual bandit can be evaluated without any human subjects at all,
because the thing being evaluated is the *algorithm's ability to find a
good policy*, not a claim about students. Define a ground-truth reward
function, run the policy against it, and measure how much reward it left on
the table compared with an oracle that knew the truth from the start. That
quantity is cumulative regret, and it is a real, reportable result.

What this DOES establish:
  - LinUCB converges on this problem, and roughly how fast;
  - how it compares against Thompson Sampling, epsilon-greedy and random;
  - how sensitive it is to alpha, which is otherwise an unjustified
    constant in nudge.py;
  - that `none` being a first-class arm is load-bearing rather than
    decorative (see --ablate-none).

What this does NOT establish, and must not be presented as:
  - anything about real students. The reward function here is invented. It
    is a plausible shape — nudges help more when someone is drifting below
    their baseline and help less when they have already been nudged four
    times today — but plausible is not measured.

So the claim to make from this output is "the policy is sound and converges
on a problem of this shape", not "nudging works".

DESIGN OF THE SIMULATED WORLD
-----------------------------

Contexts are drawn from the same 7-feature space nudge.build_context
produces, with roughly realistic marginals. Each arm has a hidden theta;
the reward for playing arm a in context x is Bernoulli with probability
theta_a . x, clipped to [0, 1]. The oracle plays argmax_a theta_a . x every
round, which is the best any policy could do knowing the truth.

The hidden thetas encode three deliberate structures worth naming, because
they are what make the problem non-trivial:

  1. `none` is genuinely the best arm in most contexts. A policy that
     cannot learn to stay quiet will accumulate regret steadily.
  2. `reflect` beats `none` only when the student is well below their own
     baseline — so the optimal policy is context-dependent, which is the
     whole reason for a *contextual* bandit rather than a plain one.
  3. Every intervention's value decays with nudge fatigue, so a policy that
     over-nudges early poisons its own later rounds.
"""
import argparse
import random

import bandit
import nudge

# Hidden ground truth: one theta per arm over nudge.CONTEXT_FEATURES, which
# are, in order:
#   bias, current_score, delta_vs_baseline, assisted_share_7d,
#   time_of_day, streak, nudge_fatigue
#
# Chosen by hand to encode the three structures in the module docstring, not
# fitted to anything. They are the *simulation's* truth, not a claim about
# the world.
TRUE_THETA = {
    #        bias  score  delta  assisted  hour  streak  fatigue
    "none":    [0.62, 0.10, 0.18, -0.05, 0.00, 0.06, 0.00],
    "reflect": [0.34, -0.06, -0.42, 0.22, 0.04, 0.00, -0.30],
    "pause":   [0.28, -0.04, -0.20, 0.34, 0.10, -0.02, -0.34],
    "contrast": [0.30, 0.02, -0.16, 0.18, 0.02, 0.02, -0.26],
}


def sample_context(rng):
    """A plausible moment, in the same shape build_context emits."""
    current_score = rng.betavariate(5, 2)                 # skewed high, like real scores
    delta = max(-1.0, min(1.0, rng.gauss(0.0, 0.35)))     # signed, mostly small
    assisted_share = rng.betavariate(2, 5)                # most weeks are mostly independent
    hour = rng.random()
    streak = rng.betavariate(2, 3)
    fatigue = rng.betavariate(1.5, 4)                     # usually low, occasionally high
    return [1.0, current_score, delta, assisted_share, hour, streak, fatigue]


def true_reward_prob(arm, context):
    return max(0.0, min(1.0, bandit.dot(TRUE_THETA[arm], context)))


def best_arm(context, arms):
    return max(arms, key=lambda a: true_reward_prob(a, context))


# ---------------------------------------------------------------------------
# Policies. Each takes (models, context, rng) and returns an arm name.
# ---------------------------------------------------------------------------

def policy_linucb(alpha):
    def choose(models, context, rng):
        return bandit.select_arm(models, context, alpha=alpha)["arm"]
    return choose


def policy_thompson(v):
    def choose(models, context, rng):
        return bandit.select_arm_thompson(models, context, rng, v=v)["arm"]
    return choose


def policy_epsilon(epsilon):
    def choose(models, context, rng):
        if rng.random() < epsilon:
            return rng.choice(sorted(models))
        # Greedy on the point estimate — no uncertainty term at all, which
        # is exactly what makes it the informative baseline: it isolates
        # how much the confidence bonus is actually worth.
        best, best_value = None, None
        for name in sorted(models):
            value = bandit.dot(models[name].theta(), context)
            if best_value is None or value > best_value:
                best, best_value = name, value
        return best
    return choose


def policy_random(models, context, rng):
    return rng.choice(sorted(models))


def run_once(policy, arms, rounds, seed, oracle_arms=None):
    """One trajectory. Returns the cumulative-regret curve.

    `oracle_arms` defaults to `arms` but can be set wider. That distinction
    is what makes the ablation meaningful: when the `none` arm is taken away
    from the *policy*, the oracle must still be allowed to play it, or the
    comparison silently lowers the bar it is measured against and a
    crippled policy scores better. Getting this wrong made the first run of
    this script report that removing `none` *reduced* regret.
    """
    rng = random.Random(seed)
    models = {a: bandit.ArmModel.fresh(nudge.CONTEXT_DIM) for a in arms}
    oracle_arms = oracle_arms or arms

    cumulative = 0.0
    curve = []
    for _ in range(rounds):
        context = sample_context(rng)
        chosen = policy(models, context, rng)

        # Regret is measured against the *expected* reward of the best arm,
        # not the sampled one. Using the realised draw would make the curve
        # mostly Bernoulli noise and hide the thing being measured.
        cumulative += true_reward_prob(best_arm(context, oracle_arms), context) \
            - true_reward_prob(chosen, context)
        curve.append(cumulative)

        reward = 1.0 if rng.random() < true_reward_prob(chosen, context) else 0.0
        models[chosen].update(context, reward)

    return curve


def average_curves(policy, arms, rounds, runs, base_seed, oracle_arms=None):
    """Mean regret curve across independent runs.

    Averaging matters: a single trajectory of a stochastic policy is not
    evidence, and Thompson Sampling in particular has high run-to-run
    variance early on. Seeds are derived from a base so the whole comparison
    is reproducible.
    """
    totals = [0.0] * rounds
    for i in range(runs):
        curve = run_once(policy, arms, rounds, base_seed + i * 1013, oracle_arms)
        for t, value in enumerate(curve):
            totals[t] += value
    return [v / runs for v in totals]


def sparkline(curve, width=48, height=8):
    """A regret curve in the terminal, so the script is useful without a
    plotting dependency. The CSV is the artefact for a real chart."""
    if not curve:
        return ""
    step = max(1, len(curve) // width)
    sampled = curve[::step][:width]
    top = max(sampled) or 1.0
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(len(blocks) - 1, int(v / top * (len(blocks) - 1)))]
                   for v in sampled)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3000)
    parser.add_argument("--runs", type=int, default=20,
                        help="independent trajectories to average over")
    parser.add_argument("--seed", type=int, default=20260813)
    parser.add_argument("--csv", help="write the averaged curves here")
    parser.add_argument("--ablate-none", action="store_true",
                        help="rerun without the 'none' arm to show what it is worth")
    parser.add_argument("--sweep-alpha", action="store_true",
                        help="sweep LinUCB's exploration weight")
    args = parser.parse_args()

    arms = list(nudge.ARMS)
    policies = {
        "LinUCB (a=1.0)": policy_linucb(1.0),
        "LinUCB (a=0.25)": policy_linucb(0.25),
        "LinUCB (a=2.0)": policy_linucb(2.0),
        "Thompson (v=1.0)": policy_thompson(1.0),
        "e-greedy (e=0.1)": policy_epsilon(0.1),
        "Random": policy_random,
    }

    print(f"rounds={args.rounds}  runs={args.runs}  arms={arms}")
    print("Regret is measured against an oracle that knows the true reward function.")
    print("Lower is better. Random is the do-nothing baseline; the gap to it is what")
    print("learning bought.\n")

    results = {}
    width = max(len(n) for n in policies)
    for name, policy in policies.items():
        curve = average_curves(policy, arms, args.rounds, args.runs, args.seed)
        results[name] = curve
        final = curve[-1]
        per_round = final / args.rounds
        print(f"  {name:<{width}}  final regret {final:8.1f}   "
              f"per round {per_round:.4f}   {sparkline(curve)}")

    baseline = results["Random"][-1]
    best_name = min(results, key=lambda n: results[n][-1])
    print(f"\n  best policy: {best_name} — "
          f"{100 * (1 - results[best_name][-1] / baseline):.1f}% less regret than random")

    if args.ablate_none:
        print("\nAblation: the 'none' arm removed.")
        print("If a nudge policy cannot choose to stay quiet, it must play an")
        print("intervention every round — so this measures what that costs.")
        reduced = [a for a in arms if a != "none"]
        # oracle_arms stays the FULL set — the question is what the policy
        # loses by being unable to stay quiet, measured against what was
        # actually achievable, not against a handicapped optimum.
        curve = average_curves(policy_linucb(1.0), reduced, args.rounds, args.runs,
                               args.seed, oracle_arms=arms)
        full = results["LinUCB (a=1.0)"][-1]
        print(f"  LinUCB without 'none': final regret {curve[-1]:8.1f}  "
              f"(vs {full:.1f} with it) — "
              f"{curve[-1] / full:.1f}x worse")

    if args.sweep_alpha:
        print("\nLinUCB exploration weight. bandit.DEFAULT_ALPHA is currently "
              f"{bandit.DEFAULT_ALPHA}.")
        print("This is the constant that had no justification behind it.")
        for alpha in (0.1, 0.25, 0.5, 1.0, 1.5, 2.0):
            curve = average_curves(policy_linucb(alpha), arms, args.rounds,
                                   args.runs, args.seed)
            marker = "  <- current default" if alpha == bandit.DEFAULT_ALPHA else ""
            print(f"  alpha={alpha:<5} final regret {curve[-1]:8.1f}{marker}")

    if args.csv:
        with open(args.csv, "w") as handle:
            names = list(results)
            handle.write("round," + ",".join(n.replace(",", "") for n in names) + "\n")
            for t in range(args.rounds):
                handle.write(str(t + 1) + "," +
                             ",".join(f"{results[n][t]:.4f}" for n in names) + "\n")
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()
