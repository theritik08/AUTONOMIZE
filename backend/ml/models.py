r"""Gradient-boosted regression trees and ridge regression, written rather
than imported.

WHY NOT scikit-learn — AND WHAT ANSWER THE BENCHMARK ACTUALLY GAVE
------------------------------------------------------------------

This module used to justify itself on principle. It now justifies itself on
a measurement, which is a better kind of argument, and the measurement is
recorded in full in `docs/ML.md`. On the demo history (4,161 rows, 18
features, time-ordered split) the ranking was:

    predict their EMA (free, no model)          MAE 6.112
    ridge, hand-written (this file)             MAE 5.756
    RidgeCV, scikit-learn                       MAE 5.767
    RidgeCV on the EMA residual, scikit-learn   MAE 5.755
    boosted trees, hand-written (this file)     MAE 5.828
    HistGradientBoosting, scikit-learn          MAE 5.927
    RandomForest / ExtraTrees, scikit-learn     MAE 5.942 / 5.910

Two things follow, and they point in the same direction.

**Trees lose.** Every tree method tried — mine and scikit-learn's — is
beaten by a regularised linear model. That is not a surprise once you look
at the features: they are lagged values, rolling means and a slope of a
strongly autocorrelated series, which is very nearly a linear-response
problem by construction. So the primary model is ridge, chosen on the
held-out slice rather than on taste, and the boosted trees stay as the
control that has to be beaten each time training runs.

**scikit-learn's accuracy contribution is nil.** 5.756 against 5.755 is not
a difference; it is the same answer computed twice. So importing ~100 MB of
compiled dependencies onto the serving path would buy nothing measurable
while breaking the property that `pip install -r requirements.txt` works
from a fresh clone with six pure-Python packages.

What scikit-learn *does* earn is a place in the training and test
environment, where it is a reference implementation to check this one
against — see `tests/test_ml_against_sklearn.py`, which asserts the
hand-written ridge and this GBT agree with theirs within tolerance, and
skips cleanly when scikit-learn is not installed. A hand-written learner
nobody cross-checked is a liability. One that provably matches the
reference is a demonstration.

WHY HISTOGRAM-BASED
-------------------

The naive split search sorts every feature at every node: O(d · n log n) per
node, which in pure Python is slow enough to matter at a few thousand rows.

Instead each feature is bucketed ONCE into at most `MAX_BINS` quantile bins
before any tree is built. Finding the best split at a node then means one
O(n) pass to accumulate (sum of residuals, count) per bin, followed by an
O(bins) sweep of the cumulative sums. Total per node is O(n + d · bins)
rather than O(d · n log n), and the binning cost is paid once for the whole
forest rather than once per node.

This is the same idea LightGBM and XGBoost's `hist` method use. The
approximation it makes — splits can only fall on bin edges — costs almost
nothing on continuous features and nothing at all on the discrete ones.

WHY SQUARED LOSS
----------------

The target is a bounded continuous score and the metric that matters is how
far off the prediction is, so squared loss is the honest match. It also
makes the gradient the plain residual and the optimal leaf value the mean
residual, which keeps the implementation short enough to audit.
"""
import json
import math
import random

MAX_BINS = 32

# Bumped whenever the serialised shape changes in a way an older reader
# would misinterpret. `registry.py` refuses to load a payload from a
# different format version rather than guessing at the layout.
MODEL_FORMAT_VERSION = 2


# ---------------------------------------------------------------------------
# Binning
# ---------------------------------------------------------------------------

def compute_bin_edges(xs, max_bins=MAX_BINS):
    """Quantile bin edges per feature.

    Quantiles rather than equal width because these features are skewed:
    `n_prior` has a long tail, `gap_hours_prev` piles up near zero. Equal
    width would put almost every row in one bin and leave the model unable
    to split on it at all.
    """
    if not xs:
        return []
    dim = len(xs[0])
    edges = []
    for j in range(dim):
        column = sorted(row[j] for row in xs)
        distinct = sorted(set(column))
        if len(distinct) <= max_bins:
            # Few enough values to keep exactly — midpoints between the
            # distinct values are lossless split points.
            cuts = [(a + b) / 2.0 for a, b in zip(distinct, distinct[1:])]
        else:
            cuts = []
            for q in range(1, max_bins):
                idx = int(q * len(column) / max_bins)
                value = column[min(idx, len(column) - 1)]
                if not cuts or value > cuts[-1]:
                    cuts.append(value)
        edges.append(cuts)
    return edges


def bin_row(row, edges):
    """Bin index per feature. Linear scan — `edges` is at most 31 long."""
    out = []
    for value, cuts in zip(row, edges):
        index = 0
        for cut in cuts:
            if value <= cut:
                break
            index += 1
        out.append(index)
    return out


def bin_matrix(xs, edges):
    return [bin_row(row, edges) for row in xs]


# ---------------------------------------------------------------------------
# One regression tree over binned features
# ---------------------------------------------------------------------------

class Node:
    __slots__ = ("feature", "bin_threshold", "left", "right", "value")

    def __init__(self, value=None):
        self.feature = None
        self.bin_threshold = None
        self.left = None
        self.right = None
        self.value = value

    def to_dict(self):
        if self.feature is None:
            return {"v": round(self.value, 6)}
        return {
            "f": self.feature,
            "b": self.bin_threshold,
            "l": self.left.to_dict(),
            "r": self.right.to_dict(),
        }

    @staticmethod
    def from_dict(d):
        node = Node()
        if "v" in d:
            node.value = d["v"]
            return node
        node.feature = d["f"]
        node.bin_threshold = d["b"]
        node.left = Node.from_dict(d["l"])
        node.right = Node.from_dict(d["r"])
        return node


def _best_split(binned, residuals, indices, n_bins, min_samples_leaf, min_gain):
    """Highest variance-reduction split, or None.

    For each feature the loop accumulates the residual sum and count per bin,
    then sweeps the cumulative totals left to right. The quantity maximised
    is `sum_left^2 / n_left + sum_right^2 / n_right`, which is the reduction
    in squared error from splitting there — the standard criterion, and the
    reason it is written out rather than named is that it is two lines.
    """
    total_sum = sum(residuals[i] for i in indices)
    total_count = len(indices)
    if total_count < 2 * min_samples_leaf:
        return None

    parent = (total_sum * total_sum) / total_count
    best = None

    dim = len(binned[indices[0]])
    for feature in range(dim):
        sums = [0.0] * n_bins
        counts = [0] * n_bins
        for i in indices:
            b = binned[i][feature]
            sums[b] += residuals[i]
            counts[b] += 1

        left_sum = 0.0
        left_count = 0
        for b in range(n_bins - 1):
            left_sum += sums[b]
            left_count += counts[b]
            if left_count < min_samples_leaf:
                continue
            right_count = total_count - left_count
            if right_count < min_samples_leaf:
                break
            right_sum = total_sum - left_sum
            gain = (left_sum * left_sum) / left_count \
                 + (right_sum * right_sum) / right_count - parent
            if gain > min_gain and (best is None or gain > best[0]):
                best = (gain, feature, b)

    return best


def build_tree(binned, residuals, indices, depth, n_bins,
               min_samples_leaf, min_gain):
    if depth <= 0 or len(indices) < 2 * min_samples_leaf:
        return Node(value=sum(residuals[i] for i in indices) / max(1, len(indices)))

    split = _best_split(binned, residuals, indices, n_bins, min_samples_leaf, min_gain)
    if split is None:
        return Node(value=sum(residuals[i] for i in indices) / max(1, len(indices)))

    _, feature, threshold = split
    left_idx, right_idx = [], []
    for i in indices:
        (left_idx if binned[i][feature] <= threshold else right_idx).append(i)

    node = Node()
    node.feature = feature
    node.bin_threshold = threshold
    node.left = build_tree(binned, residuals, left_idx, depth - 1, n_bins,
                           min_samples_leaf, min_gain)
    node.right = build_tree(binned, residuals, right_idx, depth - 1, n_bins,
                            min_samples_leaf, min_gain)
    return node


def predict_tree(node, binned_row):
    while node.feature is not None:
        node = node.left if binned_row[node.feature] <= node.bin_threshold else node.right
    return node.value


# ---------------------------------------------------------------------------
# The boosted ensemble
# ---------------------------------------------------------------------------

class GradientBoostedTrees:
    """Squared-loss gradient boosting.

    Each tree fits the residual left by every tree before it, and its output
    is shrunk by `learning_rate` before being added. Shrinkage is what makes
    boosting work: without it the first few trees overfit the training set
    and later trees have nothing left to learn from. Many small corrections
    generalise better than a few large ones.
    """

    def __init__(self, n_estimators=120, learning_rate=0.06, max_depth=3,
                 min_samples_leaf=12, min_gain=1e-6, subsample=0.85, seed=7,
                 base_feature=None):
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.min_gain = min_gain
        self.subsample = subsample
        self.seed = seed
        # Index of a feature to use as the starting prediction instead of a
        # global constant — see `_initial`.
        self.base_feature = base_feature
        self.base = 0.0
        self.trees = []
        self.edges = []

    def _initial(self, row):
        """The prediction before any tree runs.

        Boosting from a global mean makes the trees spend their first dozen
        rounds rediscovering something the product already knows: a
        student's score is close to their own running average. Starting from
        that feature instead — an offset, or `base_margin` in the boosting
        literature — means every tree is spent on the part the baseline gets
        WRONG.

        This is the difference between a model that competes with the
        existing formula and one that corrects it, and on a strongly
        autocorrelated series it is the difference between adding nothing
        and adding something.
        """
        if self.base_feature is None:
            return self.base
        return row[self.base_feature]

    def fit(self, xs, ys, validation=None, patience=15):
        """Fits, with early stopping when a validation set is supplied.

        Early stopping is not a nicety here. The number of trees is the main
        capacity knob, the right value depends entirely on how much data
        exists, and this project will have anywhere from a few hundred rows
        to a few hundred thousand. Picking a fixed 120 would badly overfit
        the small case.
        """
        self.edges = compute_bin_edges(xs)
        n_bins = MAX_BINS + 1
        binned = bin_matrix(xs, self.edges)

        self.base = sum(ys) / len(ys)
        predictions = [self._initial(row) for row in xs]

        val_binned = val_pred = None
        if validation:
            val_x, val_y = validation
            val_binned = bin_matrix(val_x, self.edges)
            val_pred = [self._initial(row) for row in val_x]
            best_rmse, best_round, best_trees = float("inf"), 0, []

        rng = random.Random(self.seed)
        all_indices = list(range(len(ys)))

        for round_index in range(self.n_estimators):
            residuals = [y - p for y, p in zip(ys, predictions)]

            if self.subsample < 1.0:
                # Row subsampling decorrelates successive trees and is the
                # cheapest regularisation available. Sampled without
                # replacement so a row cannot dominate one tree's split.
                k = max(2 * self.min_samples_leaf, int(len(all_indices) * self.subsample))
                indices = rng.sample(all_indices, min(k, len(all_indices)))
            else:
                indices = all_indices

            tree = build_tree(binned, residuals, indices, self.max_depth, n_bins,
                              self.min_samples_leaf, self.min_gain)
            self.trees.append(tree)

            for i in all_indices:
                predictions[i] += self.learning_rate * predict_tree(tree, binned[i])

            if validation:
                for i in range(len(val_pred)):
                    val_pred[i] += self.learning_rate * predict_tree(tree, val_binned[i])
                rmse = math.sqrt(
                    sum((a - b) ** 2 for a, b in zip(val_y, val_pred)) / len(val_y)
                )
                if rmse < best_rmse - 1e-9:
                    best_rmse, best_round = rmse, round_index
                    best_trees = list(self.trees)
                elif round_index - best_round >= patience:
                    self.trees = best_trees
                    break
        else:
            if validation:
                self.trees = best_trees

        return self

    def predict_one(self, row):
        binned = bin_row(row, self.edges)
        total = self._initial(row)
        for tree in self.trees:
            total += self.learning_rate * predict_tree(tree, binned)
        return total

    def predict(self, xs):
        return [self.predict_one(row) for row in xs]

    def feature_importance(self, dim):
        """How often each feature is split on, weighted toward the root.

        A count alone treats a root split and a depth-3 split as equal, and
        they are not: the root partitions every row. Weighting by 2^-depth
        is crude but it is honest about being crude, and it is enough to
        answer "which behaviours does the model actually key on".

        This is a *structural* importance and it describes the model, not
        the data. `explain.permutation_importance` answers the different and
        more useful question — how much accuracy is actually lost if this
        feature is destroyed — and is what the report quotes.
        """
        scores = [0.0] * dim

        def walk(node, depth):
            if node.feature is None:
                return
            scores[node.feature] += 1.0 / (2 ** depth)
            walk(node.left, depth + 1)
            walk(node.right, depth + 1)

        for tree in self.trees:
            walk(tree, 0)
        total = sum(scores) or 1.0
        return [s / total for s in scores]

    def to_dict(self):
        return {
            "kind": "gbt",
            "base": self.base,
            "base_feature": self.base_feature,
            "learning_rate": self.learning_rate,
            "edges": self.edges,
            "trees": [t.to_dict() for t in self.trees],
        }

    @classmethod
    def from_dict(cls, d):
        model = cls()
        model.base = d["base"]
        model.base_feature = d.get("base_feature")
        model.learning_rate = d["learning_rate"]
        model.edges = d["edges"]
        model.trees = [Node.from_dict(t) for t in d["trees"]]
        return model


# ---------------------------------------------------------------------------
# Ridge regression — now the primary model, on the evidence
# ---------------------------------------------------------------------------

class RidgeRegression:
    """Normal equations with an L2 term, reusing bandit.py's linear algebra.

    This started as a control and the benchmark promoted it: on these
    features it beats every tree method tried, including scikit-learn's. The
    control role has not gone away — `training.py` still fits the boosted
    trees every run and ships them instead if they win by a margin — but the
    default expectation is now that the linear model is correct and the
    trees are the challenger.

    Why linear wins here is worth being able to say out loud: the features
    are lagged values, rolling means and a slope of a strongly
    autocorrelated series. The target is very close to a weighted sum of
    them by construction, there are few genuine interactions for a tree to
    find, and 4k rows is not enough data for a tree ensemble to recover a
    smooth response better than a linear fit can state it directly.
    """

    def __init__(self, l2=1.0):
        self.l2 = l2
        self.coefficients = []
        self.mean = []
        self.scale = []

    def fit(self, xs, ys):
        import bandit

        dim = len(xs[0])
        n = len(xs)

        # Standardised because the features span wildly different ranges
        # (a ratio in [0,1] beside a session count in the hundreds). One
        # shared L2 term across unscaled features would regularise the
        # small-magnitude ones almost out of existence.
        self.mean = [sum(row[j] for row in xs) / n for j in range(dim)]
        self.scale = []
        for j in range(dim):
            var = sum((row[j] - self.mean[j]) ** 2 for row in xs) / n
            self.scale.append(math.sqrt(var) or 1.0)

        design = [[1.0] + [(row[j] - self.mean[j]) / self.scale[j] for j in range(dim)]
                  for row in xs]
        width = dim + 1

        xtx = [[sum(r[i] * r[j] for r in design) + (self.l2 if i == j and i > 0 else 0.0)
                for j in range(width)] for i in range(width)]
        xty = [sum(r[i] * y for r, y in zip(design, ys)) for i in range(width)]
        self.coefficients = bandit.mat_vec(bandit.invert(xtx), xty)
        return self

    def predict_one(self, row):
        total = self.coefficients[0]
        for j, value in enumerate(row):
            total += self.coefficients[j + 1] * (value - self.mean[j]) / self.scale[j]
        return total

    def predict(self, xs):
        return [self.predict_one(row) for row in xs]

    def contributions(self, row):
        """Each feature's signed contribution to this one prediction.

        For a linear model the attribution is not an approximation and needs
        no sampling: the prediction *is* the sum of these terms plus the
        intercept, exactly. That is a real advantage of having ended up with
        a linear primary model, and it is why `explain.py` can give a
        student an honest per-session breakdown rather than a plausible
        story about one.
        """
        return [
            self.coefficients[j + 1] * (value - self.mean[j]) / self.scale[j]
            for j, value in enumerate(row)
        ]

    def to_dict(self):
        return {"kind": "ridge", "coefficients": self.coefficients,
                "mean": self.mean, "scale": self.scale}

    @classmethod
    def from_dict(cls, d):
        model = cls()
        model.coefficients = d["coefficients"]
        model.mean = d["mean"]
        model.scale = d["scale"]
        return model


def load_model(payload):
    """Rebuilds whichever model kind the JSON describes."""
    kind = payload.get("kind")
    if kind == "gbt":
        return GradientBoostedTrees.from_dict(payload)
    if kind == "ridge":
        return RidgeRegression.from_dict(payload)
    raise ValueError(f"unknown model kind: {kind!r}")


def dumps(model):
    return json.dumps(model.to_dict(), separators=(",", ":"))
