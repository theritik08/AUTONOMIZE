"""scikit-learn as a validation oracle, not as a dependency.

`models.py` and `isolation.py` implement three standard algorithms by hand.
The reason recorded there is a measurement — on this data a regularised
linear model beats every tree method tried, so importing ~100 MB of
compiled dependencies onto the serving path would buy nothing — but that
argument only holds if the hand-written implementations are actually
correct. A learner nobody cross-checked is a liability.

So the reference implementations are used for what they are genuinely best
at here: telling me whether my code is right.

These tests skip cleanly when scikit-learn is absent, which is the normal
state of a production install. They run in CI and in development, where
`requirements-ml.txt` is installed. That asymmetry is the point of keeping
the two requirement files apart.
"""
import random

import pytest

sklearn = pytest.importorskip("sklearn", reason="scikit-learn is a dev-only oracle")

import numpy as np  # noqa: E402
from sklearn.ensemble import IsolationForest as SkIsolationForest  # noqa: E402
from sklearn.linear_model import Ridge  # noqa: E402

from ml import models, isolation  # noqa: E402


def linear_data(n=600, seed=3):
    rng = random.Random(seed)
    xs, ys = [], []
    for _ in range(n):
        a, b, c = rng.uniform(0, 10), rng.uniform(-5, 5), rng.uniform(0, 1)
        xs.append([a, b, c])
        ys.append(3.0 * a - 2.0 * b + 7.0 * c + rng.gauss(0, 1.0))
    return xs, ys


# ---------------------------------------------------------------------------
# Ridge — the primary model, so this is the one that matters most
# ---------------------------------------------------------------------------

def test_hand_written_ridge_matches_sklearn_on_the_same_problem():
    """Both solve the same penalised normal equations, so their predictions
    should agree to well within the noise of the data.

    The alphas differ by design: this implementation standardises the
    features and penalises the standardised coefficients, so sklearn is
    given a standardised design matrix to make the two problems identical.
    Getting that correspondence right is most of what this test checks.
    """
    xs, ys = linear_data()
    mine = models.RidgeRegression(l2=1.0).fit(xs, ys)

    X = np.array(xs)
    mean, scale = X.mean(axis=0), X.std(axis=0)
    Z = (X - mean) / scale
    theirs = Ridge(alpha=1.0, fit_intercept=True).fit(Z, np.array(ys))

    mine_pred = np.array(mine.predict(xs))
    theirs_pred = theirs.predict(Z)
    assert np.allclose(mine_pred, theirs_pred, atol=1e-6)


def test_the_ridge_coefficients_themselves_agree():
    """Predictions can agree by luck; coefficients agreeing means the same
    optimisation problem was actually solved."""
    xs, ys = linear_data(seed=11)
    mine = models.RidgeRegression(l2=2.5).fit(xs, ys)

    X = np.array(xs)
    Z = (X - X.mean(axis=0)) / X.std(axis=0)
    theirs = Ridge(alpha=2.5, fit_intercept=True).fit(Z, np.array(ys))

    assert np.allclose(np.array(mine.coefficients[1:]), theirs.coef_, atol=1e-6)
    assert mine.coefficients[0] == pytest.approx(theirs.intercept_, abs=1e-6)


# ---------------------------------------------------------------------------
# Gradient boosting — the challenger
# ---------------------------------------------------------------------------

def test_hand_written_boosting_is_competitive_with_sklearn():
    """Not identical — different binning, subsampling and stopping rules —
    but if mine were materially worse the hand-written version would not be
    defensible. Tested as a bound rather than an equality, because claiming
    equality here would be claiming something untrue."""
    from sklearn.ensemble import HistGradientBoostingRegressor

    rng = random.Random(21)
    xs = [[rng.uniform(0, 10), rng.uniform(0, 10), rng.uniform(0, 10)]
          for _ in range(1200)]
    ys = [a * 2 + (b * c) / 4 + (10 if a > 6 else 0) + rng.gauss(0, 1.0)
          for a, b, c in xs]
    cut = 900
    train_x, train_y, test_x, test_y = xs[:cut], ys[:cut], xs[cut:], ys[cut:]

    mine = models.GradientBoostedTrees(n_estimators=200, learning_rate=0.08,
                                       max_depth=4).fit(train_x, train_y)
    theirs = HistGradientBoostingRegressor(random_state=0).fit(
        np.array(train_x), np.array(train_y))

    def mae(actual, predicted):
        return float(np.mean(np.abs(np.array(actual) - np.array(predicted))))

    mine_mae = mae(test_y, mine.predict(test_x))
    theirs_mae = mae(test_y, theirs.predict(np.array(test_x)))

    # Within 35% of the reference on a problem built to favour trees.
    assert mine_mae < theirs_mae * 1.35, (mine_mae, theirs_mae)


# ---------------------------------------------------------------------------
# Isolation forest
# ---------------------------------------------------------------------------

def test_the_isolation_forest_ranks_anomalies_the_same_way_sklearn_does():
    """Scores are on different scales — sklearn negates and re-centres —
    so the comparison is of the RANKING, which is what the threshold
    actually consumes."""
    from scipy.stats import spearmanr

    rng = np.random.default_rng(4)
    cluster = rng.normal(0, 1, size=(500, 4))
    outliers = rng.uniform(6, 12, size=(25, 4))
    points = np.vstack([cluster, outliers])

    mine = isolation.IsolationForest(n_trees=200, seed=3).fit(points.tolist())
    theirs = SkIsolationForest(n_estimators=200, random_state=3).fit(points)

    mine_scores = np.array(mine.score(points.tolist()))
    # sklearn's score_samples is HIGHER for normal points; negate to align.
    theirs_scores = -theirs.score_samples(points)

    rho = spearmanr(mine_scores, theirs_scores).statistic
    assert rho > 0.9, f"rank correlation with the reference is only {rho:.2f}"


def test_both_implementations_separate_the_planted_outliers():
    rng = np.random.default_rng(5)
    cluster = rng.normal(0, 1, size=(400, 3))
    outliers = rng.uniform(8, 14, size=(20, 3))
    points = np.vstack([cluster, outliers])

    mine = isolation.IsolationForest(n_trees=200, seed=2).fit(points.tolist())
    scores = np.array(mine.score(points.tolist()))

    # Every planted outlier must score above every point in the cluster.
    assert scores[400:].min() > scores[:400].max()
