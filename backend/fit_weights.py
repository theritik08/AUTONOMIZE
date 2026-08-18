"""Offline analysis: are scoring.py's hand-tuned weights any good?

    python3 fit_weights.py                 # analyse whatever labels exist
    python3 fit_weights.py --min-labels 50

The README has always said, plainly, that the score formula's weights
(100 / 12 / 22) are hand-tuned for a sane 0-100 range rather than fitted
against real outcomes, and that fitting them against labelled data —
"did I actually understand this?" — is the honest next step. This script
is that step's other half: `POST /api/session/label` collects the labels,
this consumes them.

What it will NOT do is silently hand you a new set of weights. With fewer
than a few dozen labels, an ordinary least squares fit over four
correlated signals will produce confident-looking coefficients that are
mostly noise, and dropping those into scoring.py would make the product
worse while looking more scientific. So the script refuses to report a fit
below `--min-labels`, always prints the sample size next to every number,
and presents its output as evidence to weigh rather than a patch to apply.

Deliberately dependency-free (no numpy/pandas/sklearn): normal equations
over a handful of features is a few lines of the same pure-Python linear
algebra bandit.py already needs, and keeping `pip install -r
requirements.txt` to FastAPI + pydantic is a real property of this project.
"""
import argparse
import math

from _env import load_dotenv

load_dotenv()

import bandit  # reuses invert() / mat_vec() — no numpy needed
import db

# The raw process signals the score is built from. Fitting against these
# rather than against the final score answers the useful question ("which
# signals predict understanding?") instead of the circular one ("does the
# score predict the score?").
FEATURES = [
    "typed_ratio",
    "revision_rate",
    "ai_paste_rate",
    "active_minutes",
    # Typing regularity (see rhythm.py). Included here deliberately rather
    # than being grandfathered in: it is the newest and least-evidenced
    # signal in the system, so it should face the same test as the others.
    # If it turns out not to correlate with understanding, that is worth
    # knowing before its penalty weight is raised — not after.
    "regularity",
]


def _feature_row(session):
    typed = session["typed_chars"] or 0
    pasted = session["pasted_chars"] or 0
    total = typed + pasted
    if total == 0:
        return None
    if session.get("regularity") is None:
        return None
    active_min = (session["active_ms"] or 0) / 60000.0
    return [
        1.0,                                                    # intercept
        typed / total,                                          # typed_ratio
        (session["backspace_count"] or 0) / max(1.0, typed / 50),  # revision_rate
        (session["likely_ai_pastes"] or 0) / max(1.0, total / 500),  # ai_paste_rate
        min(active_min / 60.0, 2.0),                            # active_minutes (capped, hours)
        # Already 0-1 and already per-session, so it needs no scaling. NULL
        # for sessions from before rhythm capture existed, or too short to
        # measure — those rows are dropped from the fit rather than filled
        # with a zero, which would read as "perfectly irregular" and bias
        # the coefficient.
        float(session["regularity"]),
    ]


def load_dataset(conn):
    rows = conn.execute(
        """SELECT s.session_id, s.typed_chars, s.pasted_chars, s.backspace_count,
                  s.revision_count, s.likely_ai_pastes, s.active_ms, s.score,
                  s.regularity, l.understood
           FROM session_labels l
           JOIN sessions s ON s.session_id = l.session_id
           WHERE s.score IS NOT NULL"""
    ).fetchall()

    xs, ys, scores = [], [], []
    for row in rows:
        session = dict(row)
        features = _feature_row(session)
        if features is None:
            continue
        xs.append(features)
        # 1-5 self-report rescaled to 0-100 so coefficients are readable
        # on the same scale as the score they're being compared against.
        ys.append((session["understood"] - 1) / 4.0 * 100.0)
        scores.append(session["score"])
    return xs, ys, scores


def pearson(a, b):
    n = len(a)
    if n < 2:
        return None
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / math.sqrt(var_a * var_b)


def least_squares(xs, ys):
    """Normal equations with a small ridge term.

    The ridge (lambda = 1e-6) is not regularisation for its own sake — the
    features here are correlated by construction (typed_ratio and
    ai_paste_rate move together), so X^T X can be near-singular on a small
    sample and invert() would either fail or return an unstable result.
    """
    d = len(xs[0])
    xtx = [[sum(x[i] * x[j] for x in xs) + (1e-6 if i == j else 0.0) for j in range(d)] for i in range(d)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(d)]
    return bandit.mat_vec(bandit.invert(xtx), xty)


def r_squared(xs, ys, coefficients):
    predicted = [bandit.dot(coefficients, x) for x in xs]
    mean_y = sum(ys) / len(ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-labels", type=int, default=30,
                        help="refuse to report a fit below this many labelled sessions")
    args = parser.parse_args()

    db.init_db()
    with db.get_conn() as conn:
        xs, ys, scores = load_dataset(conn)

    n = len(xs)
    print(f"labelled, scored sessions available: {n}")
    if n == 0:
        print("\nNothing to analyse yet. Labels are collected via POST /api/session/label.")
        return

    correlation = pearson(scores, ys)
    if correlation is not None:
        print(f"\ncorrelation between the CURRENT score and self-reported understanding:")
        print(f"  r = {correlation:+.3f}  (n = {n})")
        print("  This is the single most important number here: it says whether the")
        print("  formula as it stands tracks the thing it claims to measure at all.")

    print("\nper-signal correlation with understanding:")
    for i, name in enumerate(FEATURES, start=1):
        column = [x[i] for x in xs]
        r = pearson(column, ys)
        print(f"  {name:<16} r = {r:+.3f}" if r is not None else f"  {name:<16} r = n/a (no variance)")

    if n < args.min_labels:
        print(f"\nNot fitting weights: {n} labels is below --min-labels={args.min_labels}.")
        print("A fit on this little data would look authoritative and mean very little.")
        return

    coefficients = least_squares(xs, ys)
    print(f"\nleast-squares fit (n = {n}, R^2 = {r_squared(xs, ys, coefficients):.3f}):")
    print(f"  {'intercept':<16} {coefficients[0]:+8.2f}")
    for name, coefficient in zip(FEATURES, coefficients[1:]):
        print(f"  {name:<16} {coefficient:+8.2f}")
    print("\nCompare against the hand-tuned constants in scoring.py")
    print("(W_TYPED_RATIO=100, W_ENGAGEMENT_BONUS=12, W_AI_CORRELATION_PENALTY=22).")
    print("Treat disagreement as a question to investigate, not a patch to apply:")
    print("self-reported understanding is itself a noisy, optimistic label.")


if __name__ == "__main__":
    main()
