r"""The guards that make the metrics worth quoting.

A machine-learning result is a claim about data the model has not seen. Most
of the ways that claim goes wrong are not modelling mistakes at all — they
are plumbing mistakes that make the number *better*, which is why they
survive review: nobody investigates a good result.

Everything in this file exists to make one of those failures loud. Each
function raises rather than returning False, because a leak discovered
during training must stop the run, not add a line to a log nobody reads.
`training.py` calls all of them on every run, and `tests/test_ml_validation.py`
checks that each one actually fires on a deliberately broken input — a guard
nobody has seen fail is a guard nobody knows works.
"""


class LeakageError(AssertionError):
    """Raised when a check finds the future leaking into the past."""


def _poison(row):
    """Corrupt a session beyond recognition, in every direction at once."""
    return dict(
        row,
        score=0.0,
        typed_chars=0,
        pasted_chars=999_999,
        backspace_count=0,
        revision_count=0,
        likely_ai_pastes=999,
        regularity=1.0,
        active_ms=1,
    )


def assert_causal(build_dataset, rows, horizon=1, warmup=3):
    """Corrupt the future and check that the past does not move.

    The check operates on the whole dataset builder rather than on one
    feature vector, and that is deliberate — an earlier version of this
    guard compared `build(prefix)` against `build(copy_of_prefix)`, which
    are identical inputs, so it could never have failed. The bug it needs to
    catch does not live inside the feature function; it lives in what the
    feature function is *handed*.

    So: build the dataset, then destroy every session from index `k`
    onwards and build it again. Any training row whose history window ends
    before `k` must come out byte-identical. If one moves, something in the
    pipeline is reading sessions that happen after the row it describes —
    which is the mistake that produces a wonderful R-squared and a useless
    model.

    Labels are deliberately not compared: a label legitimately averages
    sessions from the future, that is what a label *is*, and comparing them
    would make the guard fail on correct code.
    """
    if len(rows) < warmup + 4:
        raise ValueError(f"need at least {warmup + 4} sessions to check causality")

    clean_xs, _ys, _groups, _times = build_dataset(rows, horizon=horizon)
    if not clean_xs:
        raise ValueError("dataset builder produced no rows to check")

    # Poison the last third of the history, leaving enough clean prefix to
    # produce training rows that must be unaffected.
    k = max(warmup + 1, int(len(rows) * 0.66))
    poisoned = [dict(r) for r in rows[:k]] + [_poison(r) for r in rows[k:]]
    dirty_xs, _dy, _dg, _dt = build_dataset(poisoned, horizon=horizon)

    # Training row i is built from rows[:warmup + i] — indices 0 to
    # warmup+i-1 — so it is unaffected by the poisoning exactly when
    # warmup + i <= k. The bound is INCLUSIVE, and that matters: the row
    # where warmup + i == k is the one that catches an off-by-one window
    # reaching one session into the future, which is the single most likely
    # form of this bug. Stopping one row earlier would let it through.
    unaffected = min(k - warmup + 1, len(clean_xs), len(dirty_xs))
    if unaffected <= 0:
        raise ValueError("no training rows fall entirely before the poisoned cut")

    for i in range(unaffected):
        for j, (a, b) in enumerate(zip(clean_xs[i], dirty_xs[i])):
            if abs(a - b) > 1e-9:
                raise LeakageError(
                    f"training row {i}, feature {j} changed ({a} -> {b}) when "
                    f"sessions from index {k} onward were corrupted: the "
                    "pipeline is reading data from after the session it predicts"
                )
    return True


def assert_time_ordered(train_times, test_times):
    """Every training row must precede every test row.

    A random split of a time series lets the model train on a student's
    February and be tested on their January. That situation never occurs in
    production and it inflates every metric, because the model has seen the
    regime it is being asked to predict.
    """
    if not train_times or not test_times:
        return True
    if max(train_times) > min(test_times):
        raise LeakageError(
            f"a training row (t={max(train_times)}) is newer than a test row "
            f"(t={min(test_times)}): the split is not time-ordered"
        )
    return True


def assert_feature_contract(feature_names, expected_names):
    """The vector positions must mean what the model learned they meant.

    Renaming or reordering a feature silently changes every coefficient's
    subject. This is checked at training time here and again at load time
    in `registry.py`, because the two events can be months apart.
    """
    if list(feature_names) != list(expected_names):
        raise LeakageError(
            "feature contract mismatch — the model would be interpreting "
            "columns it was not trained on"
        )
    return True


def assert_no_constant_label(ys):
    """A constant target makes R-squared undefined and every model perfect."""
    if len(set(round(y, 6) for y in ys)) < 2:
        raise LeakageError("the label is constant across every row")
    return True


def assert_no_duplicate_rows(xs, ys, limit=0.5):
    """Too many identical rows means the split is not really holding out.

    If half the dataset is duplicates, a row in the test set is likely to
    have a twin in training, and the test metric is measuring memorisation.
    The threshold is loose on purpose: some duplication is natural here,
    since a student with a flat history genuinely produces near-identical
    vectors.
    """
    if not xs:
        return True
    seen = {tuple(round(v, 6) for v in row) for row in xs}
    unique_share = len(seen) / len(xs)
    if unique_share < (1.0 - limit):
        raise LeakageError(
            f"only {unique_share:.0%} of feature rows are distinct — the "
            "held-out slice is largely a copy of the training set"
        )
    return True


def assert_finite(xs):
    """No NaN or infinity anywhere in the design matrix.

    A single NaN propagates through the normal equations and produces a
    model that predicts NaN for everything, which then serialises to JSON
    as `NaN` — invalid JSON that the loader will reject at some later date
    with an error pointing nowhere near the cause.
    """
    for i, row in enumerate(xs):
        for j, value in enumerate(row):
            if value != value or value in (float("inf"), float("-inf")):
                raise LeakageError(f"non-finite value at row {i}, feature {j}")
    return True


def run_all(xs, ys, train_times, test_times, feature_names, expected_names,
            build_dataset=None, sample_stream=None, horizon=1, warmup=3):
    """Every guard, in one call. Returns the list of checks that passed."""
    passed = []

    assert_finite(xs)
    passed.append("finite")

    assert_no_constant_label(ys)
    passed.append("label varies")

    assert_no_duplicate_rows(xs, ys)
    passed.append("rows distinct")

    assert_feature_contract(feature_names, expected_names)
    passed.append("feature contract")

    assert_time_ordered(train_times, test_times)
    passed.append("time-ordered split")

    if build_dataset and sample_stream and len(sample_stream) >= warmup + 4:
        assert_causal(build_dataset, sample_stream, horizon=horizon, warmup=warmup)
        passed.append("strict causality")

    return passed
