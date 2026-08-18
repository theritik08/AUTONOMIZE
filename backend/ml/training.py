r"""The offline training pipeline.

    python3 train_model.py                  # train, evaluate, save
    python3 train_model.py --dry-run        # evaluate only, write nothing
    python3 train_model.py --horizon 1      # predict the next session

WHAT IS BEING LEARNED
---------------------

Given how a student has been working, predict the mean score of their next
`horizon` sessions. The label is the next few rows, so the database
supervises itself and no human labelling is needed — see `features.py` for
why that is available when "did they understand it" is not.

This replaces `anomaly.forecast`, which fits a straight line through the
last few daily averages. A line is a strong assumption about how behaviour
moves; this learns the shape instead.

HOW IT IS EVALUATED, AND WHY THAT MATTERS MORE THAN THE MODEL
-------------------------------------------------------------

Four decisions here do more for the credibility of the number than any
amount of tuning:

1. **The split is by TIME, not random.** Rows are ordered by when the
   predicted session happened and the last slice is held out. A random
   split of a time series lets the model train on a student's February and
   be tested on their January — a situation that never occurs in production
   and that inflates every metric. `validation.assert_time_ordered` fails
   the run if this is ever broken.

2. **Baselines are reported first.** Predicting the previous score, or the
   student's EMA, is free and requires no model at all. A learned model
   that cannot beat "assume tomorrow looks like today" has not earned its
   complexity, and on a strongly autocorrelated series that is a real
   possibility rather than a formality.

3. **Every leakage guard runs before any model is fitted.** See
   `validation.py`. A leak makes the result *better*, which is why it
   survives review unless something is actively looking for it.

4. **It refuses to report below `--min-rows`.** Same rule as
   `fit_weights.py`: a model fitted on a hundred rows produces a confident-
   looking R-squared that is mostly noise, and shipping it because it
   printed a number is how projects end up worse than the formula they
   replaced.

The conformal calibration residuals are computed on the held-out slice, so
the prediction interval that ships is calibrated on data the model never
trained on — and the empirical coverage is measured and recorded beside the
claimed one.

REAL VERSUS DEMO
----------------

`--synthetic` marks the run as trained on simulated histories. It defaults
to on, because that is the truth of this project's current state: there is
no pilot cohort, `simulate_history.py` generates the data, and every
accuracy number the pipeline can currently produce demonstrates that the
machinery works rather than that the construct is valid. Passing
`--real-data` is a deliberate act, and the flag travels inside the model
file so a metric can never be quoted without it.
"""
import argparse
import os
import time

import conformal

from . import (
    coldstart,
    evaluation,
    explain,
    features,
    isolation,
    manifest as manifest_module,
    models,
    registry,
    validation,
)

# Below this the fit is noise wearing a metric. Chosen the same way
# fit_weights.py's threshold was: enough rows that an 18-feature model is
# not simply memorising, and stated as a judgement rather than derived.
DEFAULT_MIN_ROWS = 400

TEST_FRACTION = 0.20
VALIDATION_FRACTION = 0.15

# Fixed so a re-run reproduces the model. Recorded in the manifest.
SEED = 20260813


def time_ordered_split(xs, ys, times):
    """Oldest rows train, newest rows test. See the module docstring."""
    order = sorted(range(len(ys)), key=lambda i: times[i])
    n = len(order)
    n_test = max(1, int(n * TEST_FRACTION))
    n_val = max(1, int(n * VALIDATION_FRACTION))
    n_train = n - n_test - n_val
    if n_train <= 0:
        return None

    def take(idx):
        return [xs[i] for i in idx], [ys[i] for i in idx], [times[i] for i in idx]

    return (take(order[:n_train]),
            take(order[n_train:n_train + n_val]),
            take(order[n_train + n_val:]))


def _fit_candidates(train_x, train_y, val_x, val_y):
    """Every model in the bake-off, fitted on the same rows.

    Ridge is first because the benchmark in `docs/ML.md` made it the
    primary; the boosted trees remain as the challenger that has to beat it
    by a margin, every run, on the held-out slice.
    """
    candidates = []

    candidates.append((
        "ridge regression",
        models.RidgeRegression(l2=1.0).fit(train_x, train_y),
    ))

    candidates.append((
        "boosted trees (from the mean)",
        models.GradientBoostedTrees(seed=SEED).fit(
            train_x, train_y, validation=(val_x, val_y)),
    ))

    # Boosting from the student's own EMA rather than a global mean: every
    # tree is then spent on what the baseline gets wrong. See
    # models.GradientBoostedTrees._initial.
    ema_index = features.FEATURE_NAMES.index("ema")
    candidates.append((
        "boosted trees (correcting the EMA)",
        models.GradientBoostedTrees(base_feature=ema_index, seed=SEED).fit(
            train_x, train_y, validation=(val_x, val_y)),
    ))

    return candidates


# The margin a challenger must beat the primary by, in MAE points. Not zero:
# a 0.004 win on one held-out slice is noise, and swapping a linear model
# for a 34-tree ensemble on that basis is how a codebase accumulates
# complexity nobody can justify later.
CHALLENGER_MARGIN = 0.05


def train(conn, horizon=5, min_rows=DEFAULT_MIN_ROWS, synthetic=True,
          verbose=True, seed=SEED):
    """Runs the whole pipeline and returns (payload, report).

    `payload` is None when nothing should be saved — too little data, or a
    model that failed to beat the free baselines. `report` is always a dict
    describing what happened, so a caller can print it or assert on it.
    """
    def say(line=""):
        if verbose:
            print(line)

    rows = features.load_rows(conn)
    xs, ys, groups, times = features.build_dataset(rows, horizon=horizon)

    report = {
        "rows": len(xs),
        "users": len(set(groups)),
        "horizon": horizon,
        "synthetic": synthetic,
        "saved": False,
    }

    say(f"training rows: {len(xs)}   distinct users: {report['users']}   "
        f"features: {features.FEATURE_DIM}   horizon: {horizon} session(s)")
    if synthetic:
        say("SOURCE: simulated histories — these metrics show the pipeline "
            "works, not that the construct is valid.")

    if len(xs) < 20:
        report["refused"] = f"{len(xs)} rows is too few to evaluate at all"
        say(f"\nNot training: {report['refused']}.")
        return None, report

    split = time_ordered_split(xs, ys, times)
    if split is None:
        report["refused"] = "not enough rows to hold out a test slice"
        say("\n" + report["refused"])
        return None, report

    (train_x, train_y, train_t), (val_x, val_y, _vt), (test_x, test_y, test_t) = split
    say(f"split by time — train {len(train_y)} · validation {len(val_y)} · "
        f"test {len(test_y)}")

    # ---- guards, before any model is fitted -------------------------------
    sample_stream = None
    for _key, stream in features.streams_from_rows(rows).items():
        if len(stream) >= features.WARMUP_SESSIONS + horizon + 6:
            sample_stream = stream
            break

    passed = validation.run_all(
        xs, ys, train_t, test_t,
        features.FEATURE_NAMES, features.FEATURE_NAMES,
        build_dataset=features.build_dataset, sample_stream=sample_stream,
        horizon=horizon, warmup=features.WARMUP_SESSIONS,
    )
    report["validation_passed"] = passed
    say("\nleakage guards: " + ", ".join(passed))

    # ---- free baselines ---------------------------------------------------
    say("\nbaselines on the held-out slice (no model, free):")
    baselines = evaluation.baseline_predictions(test_x, features.FEATURE_NAMES)
    baseline_metrics = {}
    for name, predictions in baselines.items():
        m = evaluation.metrics(test_y, predictions)
        baseline_metrics[name] = m
        say(f"  {name:<38} MAE {m['mae']:6.2f}   RMSE {m['rmse']:6.2f}"
            + (f"   R² {m['r2']:+.3f}" if m["r2"] is not None else ""))

    # ---- the bake-off -----------------------------------------------------
    say("\nlearned models:")
    started = time.time()
    results = []
    for name, model in _fit_candidates(train_x, train_y, val_x, val_y):
        m = evaluation.metrics(test_y, model.predict(test_x))
        results.append((name, model, m))
        extra = f"   ({len(model.trees)} trees)" if hasattr(model, "trees") else ""
        say(f"  {name:<38} MAE {m['mae']:6.2f}   RMSE {m['rmse']:6.2f}"
            + (f"   R² {m['r2']:+.3f}" if m["r2"] is not None else "") + extra)
    say(f"  ({time.time() - started:.1f}s total)")

    # Primary is the first candidate; a challenger must win by a margin.
    best_name, best_model, best_metrics = results[0]
    for name, model, m in results[1:]:
        if m["mae"] < best_metrics["mae"] - CHALLENGER_MARGIN:
            best_name, best_model, best_metrics = name, model, m

    comparison = evaluation.compare(test_y, best_model.predict(test_x), baselines)
    report["chosen"] = best_name
    report["test_metrics"] = best_metrics
    report["baseline_metrics"] = baseline_metrics
    report["improvement_percent"] = comparison["improvement_percent"]

    say(f"\nchosen: {best_name}")
    say(f"best free baseline: {comparison['best_baseline']} "
        f"(MAE {baseline_metrics[comparison['best_baseline']]['mae']:.2f})")
    if comparison["beats_baseline"]:
        say(f"  -> {comparison['improvement_percent']:.1f}% lower MAE than the "
            "best free baseline")
    else:
        say(f"  -> NO improvement over the free baseline "
            f"({comparison['improvement_percent']:.1f}%).")
        say("     On a strongly autocorrelated series this is a real outcome,")
        say("     not a bug. Nothing will be written: shipping the model anyway")
        say("     would add complexity for nothing.")

    # ---- explainability ---------------------------------------------------
    importance = explain.permutation_importance(
        best_model, test_x, test_y, features.FEATURE_NAMES, seed=seed)
    report["permutation_importance"] = importance
    say("\nwhat the model actually relies on (MAE cost of destroying each "
        "feature, on held-out rows):")
    for entry in importance[:8]:
        mark = "" if entry["significant"] else "   (within shuffle noise)"
        bar = "█" * max(0, int(entry["mae_increase"] * 12))
        say(f"  {entry['feature']:<22} {entry['mae_increase']:+6.3f}  {bar}{mark}")

    # ---- conformal interval, calibrated on held-out data ------------------
    predictions = best_model.predict(test_x)
    residuals = [a - p for a, p in zip(test_y, predictions)]
    radius = evaluation.conformal_radius(residuals, coverage=0.9)
    coverage = evaluation.empirical_coverage(test_y, predictions, radius)
    report["interval_90"] = radius
    report["empirical_coverage"] = coverage

    if radius is not None:
        say(f"\nconformal prediction interval: ±{radius:.1f} points at 90% nominal "
            "coverage")
        say(f"  measured coverage on the held-out slice: {coverage:.1%}")
        say("  Distribution-free: no assumption about the shape of the errors.")
        if coverage is not None and coverage < 0.82:
            say("  WARNING: measured coverage is well below nominal. The "
                "exchangeability")
            say("  assumption is strained by drift — treat the interval as "
                "optimistic.")
    else:
        say(f"\nNo prediction interval: {len(residuals)} calibration residuals is "
            f"below {conformal.MIN_CALIBRATION}.")

    # ---- the unsupervised anomaly signal ----------------------------------
    forest = None
    vectors, _ids = isolation.deviation_dataset(rows)
    if len(vectors) >= 100:
        forest = isolation.IsolationForest(seed=seed).fit(vectors)
        scores = forest.score(vectors)
        flagged = sum(1 for s in scores if s is not None
                      and s >= forest.threshold)
        report["isolation"] = {
            "n_vectors": len(vectors),
            "flag_rate": flagged / len(vectors),
            "distribution": evaluation.score_distribution(scores),
        }
        say(f"\nisolation forest: {len(vectors)} deviation vectors, "
            f"{flagged} ({flagged / len(vectors):.1%}) above "
            f"{forest.threshold:.3f} "
            f"(calibrated to the top {1 - isolation.UNUSUAL_QUANTILE:.0%})")
        say("  This flags sessions whose SHAPE is unusual for that student even "
            "when")
        say("  the score is ordinary — the case the one-dimensional signals "
            "cannot see.")
    else:
        say(f"\nNo isolation forest: {len(vectors)} deviation vectors is too few "
            "to fit one.")

    # ---- the population prior for cold start ------------------------------
    prior = coldstart.estimate_prior(rows)
    report["population_prior"] = prior
    if prior:
        say("\npopulation prior (used only until a student has their own history):")
        for category, values in sorted(prior.items()):
            say(f"  {category:<12} mean {values['mean']:5.1f}   k {values['k']:5.2f}"
                f"   from {values['n_users']} users")
            say(f"               -> a student's own history outweighs the prior "
                f"after ~{values['k']:.0f} sessions")
    else:
        say(f"\nNo population prior: fewer than {coldstart.MIN_USERS_FOR_PRIOR} "
            "users with enough history.")

    # ---- decide whether to write ------------------------------------------
    if len(xs) < min_rows:
        report["refused"] = (f"{len(xs)} rows is below --min-rows={min_rows}")
        say(f"\nBelow --min-rows={min_rows}, so nothing will be written. A model "
            "fitted on")
        say("this little data produces a confident-looking R-squared that is "
            "mostly noise.")
        return None, report

    if not comparison["beats_baseline"]:
        report["refused"] = "the model does not beat a free baseline"
        return None, report

    payload = {
        "model": best_model.to_dict(),
        "model_name": best_name,
        "horizon": horizon,
        "interval_90": radius,
        "empirical_coverage": round(coverage, 3) if coverage is not None else None,
        "test_metrics": best_metrics,
        "baseline_metrics": baseline_metrics,
        "beat_baseline_by_percent": comparison["improvement_percent"],
        "permutation_importance": importance,
        "population_prior": prior,
        "isolation_forest": forest.to_dict() if forest else None,
        "manifest": manifest_module.build(
            rows, xs, groups, horizon=horizon, seed=seed, synthetic=synthetic),
    }
    return payload, report


def main(argv=None):
    from _env import load_dotenv

    load_dotenv()
    import db

    parser = argparse.ArgumentParser(
        description="Train the next-horizon predictor on the session history.")
    parser.add_argument("--min-rows", type=int, default=DEFAULT_MIN_ROWS)
    parser.add_argument("--dry-run", action="store_true",
                        help="evaluate without writing a model file")
    parser.add_argument("--out", default=None,
                        help="override the current-model path")
    parser.add_argument("--horizon", type=int, default=5,
                        help="sessions ahead the label averages over "
                             "(1 = next session, 5 = where they are heading)")
    parser.add_argument("--real-data", action="store_true",
                        help="mark this run as trained on real collected "
                             "sessions rather than simulated ones")
    args = parser.parse_args(argv)

    db.init_db()
    with db.get_conn() as conn:
        payload, report = train(conn, horizon=args.horizon, min_rows=args.min_rows,
                                synthetic=not args.real_data)

    if payload is None:
        print(f"\nNothing written — {report.get('refused', 'refused')}.")
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    archived = registry.save(payload, model_path=args.out)
    print(f"\nwrote {archived}")
    print(f"  and pointed {args.out or registry.MODEL_PATH} at it")
    versions = registry.list_versions()
    if len(versions) > 1:
        print(f"  {len(versions)} versions archived in "
              f"{os.path.relpath(registry.MODEL_DIR)}/")
    return 0
