r"""LinUCB contextual bandit — the linear algebra half.

This is the "when to nudge" decision the original blueprint called for and
the first release deliberately deferred. `nudge.py` owns the domain
(what the arms mean, how a context vector is built, how rewards arrive);
this file owns only the algorithm, so it can be unit tested as pure maths.

Algorithm: LinUCB with disjoint linear models (Li et al., 2010). Each arm
`a` keeps

    A_a = I_d + sum(x x^T)   over the contexts that arm was played in
    b_a = sum(r * x)         over the rewards it earned

and, for a context x, scores itself

    theta_a = A_a^-1 b_a
    p_a     = theta_a . x  +  alpha * sqrt(x^T A_a^-1 x)
              \_________/     \___________________________/
               expected            uncertainty bonus
                reward

then the highest p_a is played. The second term is what makes it a bandit
rather than a regression: an arm that has rarely been tried in contexts
like this one gets a large bonus and is explored; as A_a accumulates
observations in that direction the bonus shrinks and the arm has to earn
its plays on predicted reward alone.

Why LinUCB and not something heavier: the context here is ~7 features and
a single user generates a handful of decisions per day. That is a small-
data regime. LinUCB is closed-form (no training loop, no hyperparameter
search), updates in O(d^2) per observation, needs no gradient framework,
and — the part that matters for this project — is fully inspectable: you
can read theta and say which feature is driving a decision. A neural
policy would be strictly worse on every one of those axes at this scale.

Why no numpy: d is 7. A 7x7 Gauss-Jordan inversion is microseconds of pure
Python, and it keeps the backend's install to FastAPI + pydantic, which is
a real feature of this project rather than an accident.
"""
import math

# Exploration weight. Higher explores more aggressively. 1.0 is the common
# default for LinUCB with rewards scaled into [0, 1]; the rewards in
# nudge.py are, deliberately, in that range.
#
# simulate_bandit.py --sweep-alpha says this is too high *for that
# simulation*: over 2000 rounds, alpha=0.1 accumulates 8.3 regret against
# 47.6 at alpha=1.0, monotonically worsening as alpha rises.
#
# It is deliberately NOT lowered on that evidence. The simulated world has a
# reward that is exactly linear in the context and exactly stationary, which
# is the regime least exploration is needed in. The real reward is neither:
# nudge.py's outcome attribution compares against an EMA baseline that the
# policy's own successes move, so the reward is non-stationary by
# construction, and non-stationarity is precisely the condition under which
# too little exploration locks a policy onto a stale estimate. Tuning a
# production constant to a reward function I invented would be fitting to
# my own assumptions.
#
# The finding is recorded rather than acted on. Revisit once there is real
# interaction data — that is what would make a lower alpha justifiable.
DEFAULT_ALPHA = 1.0


def identity(d: int) -> list:
    return [[1.0 if i == j else 0.0 for j in range(d)] for i in range(d)]


def zeros(d: int) -> list:
    return [0.0] * d


def invert(matrix: list) -> list:
    """Gauss-Jordan inversion with partial pivoting.

    Raises ValueError on a singular matrix. In this application that
    shouldn't happen — A starts at the identity and only ever has positive
    semi-definite outer products added to it, so it stays invertible — but
    failing loudly beats silently returning garbage that would then be
    written back to the database as a policy.
    """
    n = len(matrix)
    # Work on a copy augmented with the identity.
    aug = [list(row) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-12:
            raise ValueError("matrix is singular and cannot be inverted")
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]

        pivot = aug[col][col]
        aug[col] = [v / pivot for v in aug[col]]

        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor == 0.0:
                continue
            aug[row] = [v - factor * p for v, p in zip(aug[row], aug[col])]

    return [row[n:] for row in aug]


def mat_vec(matrix: list, vector: list) -> list:
    return [sum(m * v for m, v in zip(row, vector)) for row in matrix]


def dot(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def outer_add(matrix: list, vector: list) -> list:
    """matrix + vector vector^T"""
    return [
        [m + vector[i] * vector[j] for j, m in enumerate(row)]
        for i, row in enumerate(matrix)
    ]


class ArmModel:
    """One arm's A and b, plus how many times it's been played."""

    __slots__ = ("a_matrix", "b_vector", "n_pulls")

    def __init__(self, a_matrix, b_vector, n_pulls=0):
        self.a_matrix = a_matrix
        self.b_vector = b_vector
        self.n_pulls = n_pulls

    @classmethod
    def fresh(cls, d: int):
        return cls(identity(d), zeros(d), 0)

    def theta(self) -> list:
        return mat_vec(invert(self.a_matrix), self.b_vector)

    def score(self, context: list, alpha: float = DEFAULT_ALPHA) -> dict:
        """Returns the UCB score and its two components, so a decision can
        be explained rather than just emitted."""
        a_inv = invert(self.a_matrix)
        theta = mat_vec(a_inv, self.b_vector)
        expected = dot(theta, context)
        # x^T A^-1 x is guaranteed non-negative for a positive-definite A;
        # clamp anyway so float error near zero can't reach sqrt().
        variance = max(0.0, dot(context, mat_vec(a_inv, context)))
        bonus = alpha * math.sqrt(variance)
        return {"ucb": expected + bonus, "expected": expected, "bonus": bonus}

    def update(self, context: list, reward: float) -> None:
        self.a_matrix = outer_add(self.a_matrix, context)
        self.b_vector = [b + reward * x for b, x in zip(self.b_vector, context)]
        self.n_pulls += 1


def select_arm(models: dict, context: list, alpha: float = DEFAULT_ALPHA) -> dict:
    """Picks the arm with the highest UCB.

    Ties break on the arm name rather than dict order so the same inputs
    always produce the same decision — a bandit that is non-deterministic
    for reasons unrelated to its own exploration term is untestable.
    """
    scored = {}
    for name in sorted(models):
        scored[name] = models[name].score(context, alpha)

    best = max(sorted(scored), key=lambda name: scored[name]["ucb"])
    return {"arm": best, "scores": scored}


# ---------------------------------------------------------------------------
# Linear Thompson Sampling — the alternative policy
# ---------------------------------------------------------------------------
# LinUCB and Thompson Sampling solve the same problem with opposite
# philosophies. LinUCB is deterministic and optimistic: it adds a confidence
# bonus and always plays the highest upper bound. Thompson Sampling is
# stochastic and Bayesian: it draws a plausible theta from the posterior and
# plays greedily against that draw, so exploration comes from the sampling
# rather than from a bonus term.
#
# Both are here because the project had no way to justify choosing one. With
# simulate_bandit.py they can be compared on cumulative regret against a
# known optimum, which turns "LinUCB, because the paper is well known" into
# a claim with a number behind it.
#
# The posterior for the disjoint linear model is theta ~ N(A^-1 b, v^2 A^-1),
# so sampling needs a factorisation of A^-1. Cholesky is the right one: A is
# symmetric positive definite by construction (it starts at the identity and
# only accumulates outer products), it is O(d^3/3) rather than a full
# eigendecomposition, and at d = 7 that is a few hundred operations.

def cholesky(matrix: list) -> list:
    """Lower-triangular L with L L^T = matrix.

    Raises ValueError if the matrix is not positive definite, for the same
    reason invert() raises on a singular one: silently returning a
    factorisation of something else would corrupt every sample drawn from
    it, and the corruption would look like ordinary randomness.
    """
    n = len(matrix)
    lower = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            total = sum(lower[i][k] * lower[j][k] for k in range(j))
            if i == j:
                radicand = matrix[i][i] - total
                if radicand <= 1e-12:
                    raise ValueError("matrix is not positive definite")
                lower[i][j] = math.sqrt(radicand)
            else:
                lower[i][j] = (matrix[i][j] - total) / lower[j][j]
    return lower


def sample_theta(model: "ArmModel", rng, v: float = 1.0) -> list:
    """One draw from this arm's posterior over theta.

    `rng` is a random.Random instance rather than the module-level RNG so a
    simulation can be seeded and reproduced exactly — a bandit comparison
    that moves between runs is not evidence of anything.
    """
    a_inv = invert(model.a_matrix)
    mean = mat_vec(a_inv, model.b_vector)
    lower = cholesky(a_inv)
    standard = [rng.gauss(0.0, 1.0) for _ in range(len(mean))]
    # mean + v * L z  gives a draw from N(mean, v^2 A^-1).
    noise = mat_vec(lower, standard)
    return [m + v * e for m, e in zip(mean, noise)]


def select_arm_thompson(models: dict, context: list, rng, v: float = 1.0) -> dict:
    """Picks the arm with the highest sampled reward.

    Arms are iterated in sorted order so that, for a given seed, the
    sequence of random draws is fixed — otherwise dict ordering would make
    a seeded run irreproducible.
    """
    scored = {}
    for name in sorted(models):
        theta = sample_theta(models[name], rng, v)
        scored[name] = {"sampled": dot(theta, context)}
    best = max(sorted(scored), key=lambda name: scored[name]["sampled"])
    return {"arm": best, "scores": scored}
