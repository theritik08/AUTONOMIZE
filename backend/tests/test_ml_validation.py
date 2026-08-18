"""Every leakage guard must actually fire.

A guard nobody has seen fail is a guard nobody knows works. Each test here
constructs the specific broken input the guard exists to catch and asserts
it raises — and one test asserts the guards pass on a clean dataset, so a
guard that raises on everything would also be caught.
"""
import pytest

from ml import features, validation


def session(i, score, started_at, regularity=0.4):
    return {
        "session_id": f"s{i}", "user_id": "u1", "category": "writing",
        "started_at": started_at, "active_ms": 25 * 60_000,
        "typed_chars": 800, "pasted_chars": 200, "backspace_count": 30,
        "revision_count": 4, "likely_ai_pastes": 0, "tab_switch_count": 0,
        "regularity": regularity, "score": score,
    }


def stream(scores):
    return [session(i, s, 1_700_000_000_000 + i * 86_400_000)
            for i, s in enumerate(scores)]


LONG = [70, 72, 68, 75, 71, 69, 74, 73, 66, 78, 71, 70]


def test_the_real_pipeline_passes_the_causality_check():
    assert validation.assert_causal(features.build_dataset, stream(LONG),
                                    horizon=1,
                                    warmup=features.WARMUP_SESSIONS)


def test_a_pipeline_that_hands_over_the_future_is_caught():
    """The mistake the guard exists for: the feature window including the
    session being predicted, rather than stopping strictly before it."""
    def leaky_build_dataset(rows, horizon=1):
        xs, ys, groups, times = [], [], [], []
        ordered = sorted(rows, key=lambda r: r["started_at"])
        for i in range(features.WARMUP_SESSIONS, len(ordered) - horizon + 1):
            # `i + 1` instead of `i` — one row of the future, which is all
            # it takes.
            vector = features.build_features(ordered[:i + 1], False)
            if vector is None:
                continue
            xs.append(vector)
            ys.append(float(ordered[i]["score"]))
            groups.append(ordered[i]["user_id"])
            times.append(ordered[i]["started_at"])
        return xs, ys, groups, times

    with pytest.raises(validation.LeakageError):
        validation.assert_causal(leaky_build_dataset, stream(LONG), horizon=1,
                                 warmup=features.WARMUP_SESSIONS)


def test_a_feature_that_joins_an_already_updated_table_is_caught():
    """Reading `user_baseline` — which has absorbed the target session —
    is the specific real-world version of the same bug."""
    def leaky_build_dataset(rows, horizon=1):
        ordered = sorted(rows, key=lambda r: r["started_at"])
        # Stands in for the current baseline row: computed over EVERYTHING,
        # including sessions after the one each training row describes.
        current_baseline = sum(r["score"] for r in ordered) / len(ordered)
        xs, ys, groups, times = [], [], [], []
        for i in range(features.WARMUP_SESSIONS, len(ordered) - horizon + 1):
            vector = features.build_features(ordered[:i], False)
            if vector is None:
                continue
            xs.append(vector + [current_baseline])
            ys.append(float(ordered[i]["score"]))
            groups.append(ordered[i]["user_id"])
            times.append(ordered[i]["started_at"])
        return xs, ys, groups, times

    with pytest.raises(validation.LeakageError):
        validation.assert_causal(leaky_build_dataset, stream(LONG), horizon=1,
                                 warmup=features.WARMUP_SESSIONS)


def test_an_unordered_split_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_time_ordered([100, 500], [200, 300])


def test_a_properly_ordered_split_passes():
    assert validation.assert_time_ordered([100, 200], [300, 400])


def test_a_renamed_feature_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_feature_contract(["a", "b"], ["a", "c"])


def test_a_constant_label_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_no_constant_label([7.0, 7.0, 7.0])


def test_a_dataset_that_is_mostly_duplicates_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_no_duplicate_rows([[1.0, 2.0]] * 100, [1.0] * 100)


def test_a_nan_in_the_design_matrix_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_finite([[1.0, float("nan")]])


def test_an_infinity_in_the_design_matrix_is_caught():
    with pytest.raises(validation.LeakageError):
        validation.assert_finite([[1.0, float("inf")]])


def test_run_all_reports_which_checks_passed_on_a_clean_dataset():
    rows = stream([60, 63, 61, 66, 64, 69, 67, 72, 70, 75, 73, 78])
    xs, ys, _groups, times = features.build_dataset(rows, horizon=1)
    cut = len(times) - 2
    passed = validation.run_all(
        xs, ys, times[:cut], times[cut:],
        features.FEATURE_NAMES, features.FEATURE_NAMES,
        build_dataset=features.build_dataset, sample_stream=rows, horizon=1,
        warmup=features.WARMUP_SESSIONS,
    )
    assert "strict causality" in passed
    assert "time-ordered split" in passed
    assert "feature contract" in passed
