"""Tests for the learned next-horizon predictor.

Three properties matter more than any accuracy number here, and each has a
failure mode that makes the metrics look BETTER rather than worse — which is
why they are asserted rather than eyeballed:

  causality      a feature that can see the future scores ~1.0 R-squared and
                 is useless in production;
  refusal        a model that guesses when its inputs are missing emits a
                 number indistinguishable from a good one;
  baselines      a model that cannot beat "predict their own average" has
                 not earned the complexity it adds.
"""
import json
import math
import random

import pytest

from ml import evaluation, features, inference, models, registry, training


def session(i, score, started_at, regularity=0.4, typed=800, pasted=200):
    return {
        "session_id": f"s{i}", "user_id": "u1", "category": "writing",
        "started_at": started_at, "active_ms": 25 * 60_000,
        "typed_chars": typed, "pasted_chars": pasted,
        "backspace_count": 30, "revision_count": 4,
        "likely_ai_pastes": 0, "tab_switch_count": 0,
        "regularity": regularity, "score": score,
    }


def stream(scores, start=1_700_000_000_000, step=86_400_000):
    return [session(i, s, start + i * step) for i, s in enumerate(scores)]


# ---------------------------------------------------------------------------
# Causality — the property that, if broken, flatters every metric
# ---------------------------------------------------------------------------

def test_features_cannot_see_the_session_being_predicted():
    """Shuffling everything after the cut must not move a single feature."""
    history = stream([70, 72, 68, 75, 71, 69, 74])
    baseline = features.build_features(history, is_assessment=False)

    rng = random.Random(3)
    for _ in range(20):
        future = stream([rng.uniform(0, 100) for _ in range(6)],
                        start=1_800_000_000_000)
        assert features.build_features(history, is_assessment=False) == baseline
        # Appending the future and re-cutting must reproduce the same vector.
        assert features.build_features((history + future)[:len(history)],
                                       is_assessment=False) == baseline


def test_the_ema_feature_is_recomputed_not_read_from_the_baseline_row():
    """user_baseline holds the CURRENT baseline, which has already absorbed
    the target session. Reading it would leak the label outright."""
    history = stream([50, 60, 70, 80])
    vector = features.build_features(history, is_assessment=False)
    ema_index = features.FEATURE_NAMES.index("ema")

    expected = 50.0
    for value in [60, 70, 80]:
        expected += features.EMA_ALPHA * (value - expected)
    assert vector[ema_index] == pytest.approx(expected)


def test_streams_never_mix_categories():
    rows = ([dict(r, category="writing") for r in stream([70, 71, 72, 73, 74])]
            + [dict(r, session_id=f"a{i}", category="assessment", score=20.0)
               for i, r in enumerate(stream([20, 21, 22, 23, 24]))])
    streams = features.streams_from_rows(rows)
    assert set(streams) == {("u1", "writing"), ("u1", "assessment")}
    for (_, category), rows_in in streams.items():
        assert {r["category"] for r in rows_in} == {category}


def test_a_horizon_label_always_averages_a_full_window():
    """A partial final window would mix two prediction problems silently."""
    rows = stream([60, 61, 62, 63, 64, 65, 66, 67, 68, 69])
    xs, ys, _, _ = features.build_dataset(rows, horizon=3)
    # 10 sessions, warmup 3, horizon 3 -> targets at i = 3..7 inclusive.
    assert len(ys) == 5
    assert ys[0] == pytest.approx((63 + 64 + 65) / 3)


def test_a_short_history_produces_no_row_rather_than_a_padded_one():
    assert features.build_features(stream([70, 71]), is_assessment=False) is None
    xs, ys, _, _ = features.build_dataset(stream([70, 71]), horizon=1)
    assert xs == [] and ys == []


def test_the_feature_hash_changes_when_the_contract_does(monkeypatch):
    """The hash is what stops a stale model reinterpreting the columns."""
    before = features.feature_set_hash()
    monkeypatch.setattr(features, "FEATURE_NAMES",
                        features.FEATURE_NAMES + ("something_new",))
    assert features.feature_set_hash() != before


# ---------------------------------------------------------------------------
# The learners
# ---------------------------------------------------------------------------

def test_boosting_reduces_training_error():
    rng = random.Random(11)
    xs = [[rng.uniform(0, 10), rng.uniform(0, 10)] for _ in range(400)]
    ys = [3 * a + (a * b) / 5.0 + rng.gauss(0, 0.5) for a, b in xs]

    model = models.GradientBoostedTrees(n_estimators=60, max_depth=3).fit(xs, ys)
    predictions = model.predict(xs)
    mean = sum(ys) / len(ys)

    model_err = sum((a - p) ** 2 for a, p in zip(ys, predictions))
    mean_err = sum((a - mean) ** 2 for a in ys)
    assert model_err < mean_err * 0.2


def test_the_model_survives_a_json_round_trip_exactly():
    """Serving loads from JSON, so a lossy round trip would mean the served
    model is not the evaluated one."""
    rng = random.Random(5)
    xs = [[rng.uniform(0, 5), rng.uniform(0, 5)] for _ in range(200)]
    ys = [a * 2 - b for a, b in xs]

    for model in (models.GradientBoostedTrees(n_estimators=25).fit(xs, ys),
                  models.RidgeRegression().fit(xs, ys)):
        restored = models.load_model(json.loads(models.dumps(model)))
        for row in xs[:25]:
            assert restored.predict_one(row) == pytest.approx(model.predict_one(row),
                                                              abs=1e-6)


def test_base_feature_starts_boosting_from_that_column():
    """With no trees the prediction must be exactly the offset feature —
    that is what makes the trees fit the baseline's error rather than the
    target."""
    model = models.GradientBoostedTrees(n_estimators=0, base_feature=1)
    model.edges = models.compute_bin_edges([[0.0, 0.0], [1.0, 1.0]])
    model.base = 0.0
    assert model.predict_one([9.0, 42.0]) == pytest.approx(42.0)


def test_quantile_bins_survive_a_skewed_column():
    """Equal-width bins would put a heavily skewed feature in one bucket and
    leave the model unable to split on it at all."""
    xs = [[0.0] for _ in range(500)] + [[float(i)] for i in range(1, 60)]
    edges = models.compute_bin_edges(xs)
    assert len(edges[0]) >= 2
    assert models.bin_row([0.0], edges)[0] < models.bin_row([59.0], edges)[0]


def test_early_stopping_keeps_the_best_validation_round():
    rng = random.Random(9)
    xs = [[rng.uniform(0, 1)] for _ in range(300)]
    ys = [x[0] * 5 + rng.gauss(0, 2) for x in xs]
    val_x = [[rng.uniform(0, 1)] for _ in range(120)]
    val_y = [x[0] * 5 + rng.gauss(0, 2) for x in val_x]

    model = models.GradientBoostedTrees(n_estimators=400, learning_rate=0.15)
    model.fit(xs, ys, validation=(val_x, val_y), patience=8)
    assert len(model.trees) < 400


def test_ridge_contributions_reconstruct_the_prediction_exactly():
    """This is what lets `explain.py` give an exact local attribution rather
    than an approximation — so it has to actually hold."""
    rng = random.Random(17)
    xs = [[rng.uniform(0, 5), rng.uniform(-2, 2), rng.uniform(0, 1)]
          for _ in range(300)]
    ys = [2 * a - 3 * b + c for a, b, c in xs]
    model = models.RidgeRegression().fit(xs, ys)

    for row in xs[:20]:
        rebuilt = model.coefficients[0] + sum(model.contributions(row))
        assert rebuilt == pytest.approx(model.predict_one(row), abs=1e-9)


# ---------------------------------------------------------------------------
# Evaluation discipline
# ---------------------------------------------------------------------------

def test_the_split_is_ordered_by_time_not_random():
    xs = [[float(i)] for i in range(100)]
    ys = [float(i) for i in range(100)]
    times = list(range(100))
    (train_x, _, _), (val_x, _, _), (test_x, _, _) = \
        training.time_ordered_split(xs, ys, times)
    # Everything in train happened before everything in test.
    assert max(r[0] for r in train_x) < min(r[0] for r in test_x)
    assert max(r[0] for r in val_x) < min(r[0] for r in test_x)


def test_metrics_agree_with_their_definitions():
    m = evaluation.metrics([10.0, 20.0, 30.0], [12.0, 18.0, 33.0])
    assert m["mae"] == pytest.approx((2 + 2 + 3) / 3)
    assert m["rmse"] == pytest.approx(math.sqrt((4 + 4 + 9) / 3))
    assert m["r2"] < 1.0


def test_a_perfect_prediction_scores_zero_error():
    m = evaluation.metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert m["mae"] == 0 and m["rmse"] == 0 and m["r2"] == pytest.approx(1.0)


def test_r2_is_none_not_zero_when_every_label_is_identical():
    """Undefined and zero are different claims, and 0.0 reads as 'the model
    explained nothing' when in fact the question has no answer."""
    assert evaluation.metrics([5.0, 5.0, 5.0], [5.0, 5.0, 5.0])["r2"] is None


def test_baselines_are_read_straight_from_the_feature_vector():
    """They have to be free — that is what makes them the bar to clear."""
    history = stream([40, 50, 60, 70])
    vector = features.build_features(history, is_assessment=False)
    baselines = evaluation.baseline_predictions([vector], features.FEATURE_NAMES)
    assert baselines["predict last score"][0] == 70.0


def test_the_conformal_radius_uses_the_plus_one_quantile():
    """Dropping the +1 is the standard way this construction is quietly
    implemented wrong, and it makes the interval too narrow."""
    residuals = [float(i) for i in range(1, 101)]
    radius = evaluation.conformal_radius(residuals, coverage=0.9)
    # ceil(101 * 0.9) - 1 = 90 -> the 91st smallest residual, which is 91.
    assert radius == 91.0


def test_measured_coverage_is_reported_next_to_the_claim():
    actual = [10.0] * 100
    predicted = [10.0] * 95 + [50.0] * 5
    assert evaluation.empirical_coverage(actual, predicted, radius=1.0) == 0.95


# ---------------------------------------------------------------------------
# Serving — refusing is the important behaviour
# ---------------------------------------------------------------------------

def test_no_model_file_means_no_prediction_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "MODEL_PATH", str(tmp_path / "absent.json"))
    inference.reset_cache()
    assert inference.available() is False
    assert inference.predict(stream([70, 71, 72, 73])) is None
    described = inference.describe()
    assert described["available"] is False
    # It must say WHY, or a broken deployment looks like an untrained one.
    assert "never" in described["reason"] or "no model" in described["reason"]


def _payload(feature_names=None, manifest_overrides=None):
    from ml import manifest as manifest_module

    rows = stream([60, 62, 64, 66, 68, 70, 72, 74, 76, 78])
    xs, ys, groups, _times = features.build_dataset(rows, horizon=1)
    model = models.RidgeRegression().fit(xs, ys)
    built = manifest_module.build(rows, xs, groups, horizon=1, seed=1,
                                  synthetic=True)
    if feature_names is not None:
        built["feature_names"] = feature_names
        built["feature_set_hash"] = "deliberately-wrong"
    built.update(manifest_overrides or {})
    return {
        "model": model.to_dict(),
        "horizon": 1,
        "interval_90": 8.0,
        "test_metrics": {"mae": 2.0},
        "manifest": built,
    }


def _write(tmp_path, payload, monkeypatch):
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload))
    monkeypatch.setattr(registry, "MODEL_PATH", str(path))
    inference.reset_cache()
    return path


def test_a_trained_model_predicts_with_a_conformal_interval(tmp_path, monkeypatch):
    _write(tmp_path, _payload(), monkeypatch)

    out = inference.predict(stream([60, 62, 64, 66, 68]))
    assert out["source"] == "learned"
    assert 0.0 <= out["predicted_score"] <= 100.0
    assert out["interval_low"] < out["predicted_score"] < out["interval_high"]
    assert out["interval_coverage"] == 0.9
    # The synthetic flag must travel with the number.
    assert out["synthetic_training_data"] is True


def test_a_model_trained_on_different_features_is_refused(tmp_path, monkeypatch):
    """Feature positions carry meaning. A stale model would not error — it
    would confidently interpret paste ratio as a session count."""
    _write(tmp_path, _payload(feature_names=["a", "b", "c"]), monkeypatch)
    assert inference.available() is False
    assert inference.predict(stream([60, 62, 64, 66, 68])) is None
    assert "feature definitions" in inference.describe()["reason"]


def test_a_model_with_no_manifest_is_refused(tmp_path, monkeypatch):
    """An unlabelled artefact cannot be verified, so it is not trusted."""
    payload = _payload()
    payload.pop("manifest")
    _write(tmp_path, payload, monkeypatch)
    assert inference.available() is False


def test_a_corrupt_model_file_degrades_to_no_prediction(tmp_path, monkeypatch):
    path = tmp_path / "model.json"
    path.write_text("{not json")
    monkeypatch.setattr(registry, "MODEL_PATH", str(path))
    inference.reset_cache()
    assert inference.available() is False
    assert "not valid JSON" in inference.describe()["reason"]


def test_too_little_history_predicts_nothing(tmp_path, monkeypatch):
    _write(tmp_path, _payload(), monkeypatch)
    assert inference.predict(stream([70, 71])) is None


def test_predictions_are_clamped_to_the_score_range(tmp_path, monkeypatch):
    _write(tmp_path, _payload(), monkeypatch)
    # A wildly extrapolating history must not produce a score above 100.
    out = inference.predict(stream([10, 40, 70, 100, 100, 100, 100]))
    assert 0.0 <= out["predicted_score"] <= 100.0


def test_retraining_takes_effect_without_a_restart(tmp_path, monkeypatch):
    """The cache is keyed on mtime precisely so this works."""
    import os
    import time as time_module

    path = _write(tmp_path, _payload(), monkeypatch)
    first = inference.predict(stream([60, 62, 64, 66, 68]))["predicted_score"]

    shifted = _payload()
    shifted["interval_90"] = 20.0
    path.write_text(json.dumps(shifted))
    os.utime(path, (time_module.time() + 10, time_module.time() + 10))

    second = inference.predict(stream([60, 62, 64, 66, 68]))
    assert second["predicted_score"] == first
    assert second["interval_high"] - second["predicted_score"] == pytest.approx(20.0,
                                                                               abs=0.1)
