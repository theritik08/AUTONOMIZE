"""Tests for the aggregate-only institution view.

These are mostly privacy tests. The functional part (does it compute a
mean) is easy; the part worth protecting is that no individual is ever
identifiable, and that the size floor can't be worked around.
"""
import time

import accounts
import cohort
import db


def make_student(conn, n: int):
    return accounts.create_user(
        conn, email=f"s{n}@uni.edu", password="the quiet river runs north", role="student"
    )


def add_scored_session(conn, user_id: str, score: float, *, sid: str | None = None,
                       days_ago: int = 1, category: str = "writing"):
    sid = sid or f"{user_id}-{score}-{days_ago}"
    db.upsert_session_row(conn, {
        "session_id": sid, "user_id": user_id, "category": category,
        "domain": "docs.google.com", "path": "/x",
        "started_at": int((time.time() - days_ago * 86400) * 1000),
        "active_ms": 25 * 60_000,
        "metrics": {"typed_chars": 400, "pasted_chars": 100, "backspace_count": 30,
                    "revision_count": 2, "prompt_count": 0, "likely_ai_pastes": 0,
                    "tab_switch_count": 0},
        "is_final": True,
    })
    db.set_session_score(conn, sid, score)


def build_cohort(conn, scores: list, days_ago: int = 1):
    for i, score in enumerate(scores):
        user = make_student(conn, i)
        add_scored_session(conn, user["user_id"], score, days_ago=days_ago)


# ---------------------------------------------------------------------------
# The size floor
# ---------------------------------------------------------------------------

def test_no_students_at_all_is_withheld_not_an_error(sqlite_conn):
    result = cohort.summary(sqlite_conn)
    assert result["available"] is False
    assert result["contributing_students"] == 0


def test_below_the_floor_everything_is_withheld(sqlite_conn):
    build_cohort(sqlite_conn, [60, 70, 80, 90])  # 4 < MIN_COHORT_SIZE
    result = cohort.summary(sqlite_conn)
    assert result["available"] is False
    assert result["reason"] == "cohort_too_small"
    assert result["students_needed"] == cohort.MIN_COHORT_SIZE - 4
    # Crucially: no statistic leaks alongside the refusal.
    for leaky in ("mean_score", "median_score", "distribution", "trend"):
        assert leaky not in result


def test_at_the_floor_statistics_become_available(sqlite_conn):
    build_cohort(sqlite_conn, [60, 70, 80, 90, 100])
    result = cohort.summary(sqlite_conn)
    assert result["available"] is True
    assert result["contributing_students"] == 5
    assert result["mean_score"] == 80.0
    assert result["median_score"] == 80.0


def test_empty_accounts_cannot_unlock_a_single_students_data(sqlite_conn):
    """The attack the floor exists to stop.

    Create four accounts that never submit anything, and the fifth
    student's average would become "the cohort average" if the floor
    counted enrolment rather than contribution.
    """
    real = make_student(sqlite_conn, 0)
    add_scored_session(sqlite_conn, real["user_id"], 42.0)
    for i in range(1, 5):
        make_student(sqlite_conn, i)  # no sessions

    result = cohort.summary(sqlite_conn)
    assert result["available"] is False
    assert result["contributing_students"] == 1
    # And the refusal itself must not leak the padded enrolment number,
    # which would tell the admin their four dummy accounts registered.
    assert "enrolled_students" not in result


def test_admin_accounts_are_not_counted_as_students(sqlite_conn):
    build_cohort(sqlite_conn, [60, 70, 80, 90])
    admin = accounts.create_user(
        sqlite_conn, email="dean@uni.edu", password="the quiet river runs north", role="admin"
    )
    add_scored_session(sqlite_conn, admin["user_id"], 100.0)
    # An admin padding the cohort with their own account must not push it
    # over the floor.
    assert cohort.summary(sqlite_conn)["available"] is False


# ---------------------------------------------------------------------------
# No individual ever leaks
# ---------------------------------------------------------------------------

def test_no_user_identifier_appears_anywhere_in_the_output(sqlite_conn):
    build_cohort(sqlite_conn, [55, 65, 75, 85, 95, 45])
    result = cohort.summary(sqlite_conn)
    assert result["available"] is True

    blob = repr(result)
    ids = [r["user_id"] for r in sqlite_conn.execute("SELECT user_id FROM users").fetchall()]
    emails = [r["email"] for r in sqlite_conn.execute("SELECT email FROM users").fetchall()]
    for identifier in ids + emails:
        assert identifier not in blob, f"{identifier} leaked into the cohort summary"


def test_no_individual_score_is_recoverable_from_the_distribution(sqlite_conn):
    build_cohort(sqlite_conn, [55, 65, 75, 85, 95])
    result = cohort.summary(sqlite_conn)
    # Bands are counts, never lists of values.
    for band in result["distribution"]:
        assert set(band) == {"band", "count", "share"}
        assert isinstance(band["count"], int)


def test_summary_shape_is_only_aggregates(sqlite_conn):
    build_cohort(sqlite_conn, [55, 65, 75, 85, 95])
    result = cohort.summary(sqlite_conn)
    # An allow-list, so a future field that happens to be per-student
    # fails this test rather than silently shipping.
    assert set(result) == {
        "available", "min_cohort_size", "contributing_students", "enrolled_students",
        "mean_score", "median_score", "distribution", "trend", "suppressed_days",
        "independent_hours_7d", "assisted_hours_7d", "generated_at",
    }


# ---------------------------------------------------------------------------
# Per-day suppression
# ---------------------------------------------------------------------------

def test_a_day_with_too_few_students_is_suppressed_from_the_trend(sqlite_conn):
    # Five students all active 3 days ago -> that day qualifies.
    build_cohort(sqlite_conn, [60, 70, 80, 90, 100], days_ago=3)
    # ...but only one of them worked yesterday.
    first = sqlite_conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()["user_id"]
    add_scored_session(sqlite_conn, first, 10.0, sid="lonely-day", days_ago=1)

    result = cohort.summary(sqlite_conn)
    assert result["available"] is True
    # Publishing that day's mean would publish one student's score.
    assert all(point["students"] >= cohort.MIN_COHORT_SIZE for point in result["trend"])
    assert result["suppressed_days"] >= 1


def test_suppressed_day_count_is_reported_not_hidden(sqlite_conn):
    build_cohort(sqlite_conn, [60, 70, 80, 90, 100], days_ago=3)
    first = sqlite_conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()["user_id"]
    add_scored_session(sqlite_conn, first, 10.0, sid="lonely", days_ago=1)
    # Silently dropping data would make the chart look complete when it
    # isn't; the count lets the UI say so.
    assert cohort.summary(sqlite_conn)["suppressed_days"] == 1


# ---------------------------------------------------------------------------
# The numbers themselves
# ---------------------------------------------------------------------------

def test_distribution_counts_add_up_to_the_cohort(sqlite_conn):
    build_cohort(sqlite_conn, [10, 30, 50, 65, 80, 95])
    result = cohort.summary(sqlite_conn)
    assert sum(b["count"] for b in result["distribution"]) == result["contributing_students"]
    assert abs(sum(b["share"] for b in result["distribution"]) - 1.0) < 0.01


def test_a_student_with_many_sessions_counts_once(sqlite_conn):
    # Otherwise a prolific student would dominate the cohort mean, and the
    # "cohort average" would really be "the most active person's average".
    build_cohort(sqlite_conn, [50, 50, 50, 50])
    loud = make_student(sqlite_conn, 99)
    for i in range(20):
        add_scored_session(sqlite_conn, loud["user_id"], 100.0, sid=f"loud-{i}", days_ago=2)

    result = cohort.summary(sqlite_conn)
    assert result["contributing_students"] == 5
    # Mean of per-student means: (50*4 + 100) / 5 = 60, not ~91.
    assert result["mean_score"] == 60.0


def test_hours_are_split_between_independent_and_assisted(sqlite_conn):
    build_cohort(sqlite_conn, [60, 70, 80, 90, 100])
    first = sqlite_conn.execute("SELECT user_id FROM users LIMIT 1").fetchone()["user_id"]
    add_scored_session(sqlite_conn, first, 0.0, sid="ai-1", days_ago=1, category="ai_assistant")

    result = cohort.summary(sqlite_conn)
    assert result["independent_hours_7d"] > 0
    assert result["assisted_hours_7d"] > 0
