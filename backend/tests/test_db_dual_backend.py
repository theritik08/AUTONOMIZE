"""Runs db.py's query layer against BOTH backends.

Until now the Postgres path was verified by hand once and then described
in the README. That's the kind of claim that quietly stops being true: a
query written with SQLite-only syntax, a migration whose Postgres half was
forgotten, an integer column that overflows only on Postgres — none of
those show up in a SQLite-only suite.

Every test here uses the `any_conn` fixture, which yields SQLite always
and Postgres when TEST_DATABASE_URL is set (see conftest.py). Without that
env var the Postgres half reports as skipped rather than silently not
existing, so `pytest` stays a zero-setup command while still being honest
about what it did and didn't cover.

    TEST_DATABASE_URL=postgresql://user:pass@localhost/postgres pytest
"""
import json
import time

import pytest

import db
import settings_store
import scoring


def payload(session_id="s1", user_id="u1", category="writing", is_final=True, **metrics):
    base = {
        "typed_chars": 0, "pasted_chars": 0, "backspace_count": 0,
        "revision_count": 0, "prompt_count": 0, "likely_ai_pastes": 0,
        "tab_switch_count": 0,
    }
    base.update(metrics)
    return {
        "session_id": session_id, "user_id": user_id, "category": category,
        "domain": "docs.google.com", "path": "/d/x",
        # A 13-digit millisecond epoch. On Postgres this overflows a plain
        # 32-bit INTEGER column, which is exactly why the schema uses
        # BIGINT there — a value that only fails on one backend.
        "started_at": 1_785_000_000_000,
        "active_ms": 25 * 60_000,
        "metrics": base, "is_final": is_final,
    }


def test_session_round_trip(any_conn):
    row = db.upsert_session_row(any_conn, payload(typed_chars=500))
    assert row["session_id"] == "s1"
    assert row["typed_chars"] == 500
    assert row["finalized"] == 1


def test_millisecond_epoch_survives_the_round_trip(any_conn):
    row = db.upsert_session_row(any_conn, payload())
    assert row["started_at"] == 1_785_000_000_000


def test_metric_accumulation(any_conn):
    db.upsert_session_row(any_conn, payload(typed_chars=100))
    row = db.upsert_session_row(any_conn, payload(typed_chars=50))
    assert row["typed_chars"] == 150
    assert row["active_ms"] == 2 * 25 * 60_000


def test_set_and_read_score(any_conn):
    db.upsert_session_row(any_conn, payload(typed_chars=500))
    db.set_session_score(any_conn, "s1", 87.5)
    assert db.get_session(any_conn, "s1")["score"] == pytest.approx(87.5)


def test_baseline_upsert_on_conflict(any_conn):
    db.save_baseline(any_conn, "u1", "writing", 50.0, 0.0, 1, "2026-08-01", 50.0, 1)
    db.save_baseline(any_conn, "u1", "writing", 60.0, 5.0, 2, "2026-08-02", 70.0, 2)
    row = db.get_baseline(any_conn, "u1", "writing")
    assert row["ema_mean"] == pytest.approx(60.0)
    assert row["n_observations"] == 2


def test_date_expr_produces_the_same_date_on_both_backends(any_conn):
    """The one genuinely non-portable fragment in the codebase.

    SQLite uses date(x/1000,'unixepoch'); Postgres needs to_char(
    to_timestamp(...)). If those ever disagree the trend chart's day
    buckets silently shift by a day on one backend only.
    """
    db.upsert_session_row(any_conn, payload(typed_chars=500))
    db.set_session_score(any_conn, "s1", 90.0)
    row = any_conn.execute(
        db.q(f"SELECT {db.date_expr('started_at')} AS day FROM sessions WHERE session_id = ?"),
        ("s1",),
    ).fetchone()
    # 1785000000000 ms -> 2026-07-25 UTC
    assert row["day"] == "2026-07-25"


def test_aggregate_rollup_query(any_conn):
    db.upsert_session_row(any_conn, payload(session_id="a", typed_chars=100))
    db.upsert_session_row(any_conn, payload(session_id="b", typed_chars=100))
    row = any_conn.execute(
        db.q("""SELECT COALESCE(SUM(active_ms), 0) AS s FROM sessions
                WHERE user_id = ? AND category = 'writing' AND started_at >= ?"""),
        ("u1", 0),
    ).fetchone()
    assert row["s"] == 2 * 25 * 60_000


def test_bandit_state_round_trip(any_conn):
    a_matrix = json.dumps([[1.0, 0.0], [0.0, 1.0]])
    b_vector = json.dumps([0.5, 0.25])
    db.save_bandit_arm(any_conn, "u1", "reflect", a_matrix, b_vector, 3)
    row = db.get_bandit_arm(any_conn, "u1", "reflect")
    assert json.loads(row["a_matrix"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert row["n_pulls"] == 3

    db.save_bandit_arm(any_conn, "u1", "reflect", a_matrix, json.dumps([9.0, 9.0]), 4)
    assert db.get_bandit_arm(any_conn, "u1", "reflect")["n_pulls"] == 4


def test_nudge_event_lifecycle(any_conn):
    now = int(time.time() * 1000)
    db.insert_nudge_event(any_conn, "e1", "u1", "reflect", json.dumps([1.0, 0.0]), now)
    assert db.get_nudge_event(any_conn, "e1")["reward"] is None

    pending = db.pending_nudge_events(any_conn, "u1", now - 1000)
    assert [e["event_id"] for e in pending] == ["e1"]

    db.settle_nudge_event(any_conn, "e1", 1.0, "feedback")
    assert db.get_nudge_event(any_conn, "e1")["reward"] == pytest.approx(1.0)
    assert db.pending_nudge_events(any_conn, "u1", now - 1000) == []


def test_count_nudges_excludes_the_none_arm(any_conn):
    now = int(time.time() * 1000)
    db.insert_nudge_event(any_conn, "e1", "u1", "none", "[]", now)
    db.insert_nudge_event(any_conn, "e2", "u1", "reflect", "[]", now)
    assert db.count_nudges_since(any_conn, "u1", now - 1000) == 1


def test_session_label_round_trip_and_upsert(any_conn):
    db.upsert_session_row(any_conn, payload(typed_chars=500))
    db.upsert_session_label(any_conn, "s1", "u1", 4, False)
    db.upsert_session_label(any_conn, "s1", "u1", 2, True)
    rows = any_conn.execute(db.q("SELECT * FROM session_labels WHERE session_id = ?"), ("s1",)).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["understood"] == 2


def test_export_covers_every_user_scoped_table(any_conn):
    db.upsert_session_row(any_conn, payload(typed_chars=500))
    db.save_baseline(any_conn, "u1", "writing", 80.0, 4.0, 1, "2026-08-01", 80.0, 1)
    db.insert_nudge_event(any_conn, "e1", "u1", "reflect", "[]", 1)
    db.save_bandit_arm(any_conn, "u1", "reflect", "[[1.0]]", "[0.0]", 1)
    db.upsert_session_label(any_conn, "s1", "u1", 5, False)
    settings_store.save(any_conn, "u1", {"excludedDomains": ["private.com"]})

    exported = db.export_user_data(any_conn, "u1")
    assert set(exported) == set(db.USER_SCOPED_TABLES)
    # One row in EVERY table, not just in the ones the setup remembered:
    # a table that exports as an empty list looks identical to a table
    # nobody wrote to, and the whole point here is proving coverage.
    assert all(len(rows) == 1 for rows in exported.values()), {
        table: len(rows) for table, rows in exported.items()}


def test_delete_removes_every_table_and_is_scoped_to_one_user(any_conn):
    db.upsert_session_row(any_conn, payload(session_id="mine", user_id="u1", typed_chars=500))
    db.upsert_session_row(any_conn, payload(session_id="theirs", user_id="u2", typed_chars=500))
    db.save_baseline(any_conn, "u1", "writing", 80.0, 4.0, 1, "2026-08-01", 80.0, 1)
    db.insert_nudge_event(any_conn, "e1", "u1", "reflect", "[]", 1)

    deleted = db.delete_user_data(any_conn, "u1")
    assert deleted["sessions"] == 1
    assert deleted["user_baseline"] == 1
    assert deleted["nudge_events"] == 1

    assert db.export_user_data(any_conn, "u1") == {t: [] for t in db.USER_SCOPED_TABLES}
    assert db.get_session(any_conn, "theirs") is not None


def test_full_scoring_flow_behaves_identically_on_both_backends(any_conn):
    """A miniature end-to-end run of the real pipeline.

    Any dialect-specific bug in upsert, score persistence, or baseline
    upsert shows up as a different final baseline on one backend.
    """
    for i in range(3):
        row = db.upsert_session_row(
            any_conn, payload(session_id=f"s{i}", typed_chars=400, pasted_chars=100)
        )
        score = scoring.compute_session_score(row)
        db.set_session_score(any_conn, f"s{i}", score)
        baseline = db.get_baseline(any_conn, "u1", "writing")
        updated = scoring.update_baseline(baseline, score, f"2026-08-0{i + 1}")
        db.save_baseline(any_conn, "u1", "writing", **updated)

    final = db.get_baseline(any_conn, "u1", "writing")
    assert final["n_observations"] == 3
    assert final["ema_mean"] == pytest.approx(80.0, abs=0.01)


def test_composition_rollup_query_works_on_both_dialects(any_conn):
    """The /api/score composition query, exercised directly.

    It combines a date_expr GROUP BY with SUM aggregates and an IN clause —
    the sort of statement that's easy to write in a SQLite-only way.
    """
    db.upsert_session_row(any_conn, payload(session_id="w1", typed_chars=300, pasted_chars=100))
    db.upsert_session_row(
        any_conn, payload(session_id="e1", category="assessment", typed_chars=50, pasted_chars=400)
    )
    db.upsert_session_row(
        any_conn, payload(session_id="ai1", category="ai_assistant", typed_chars=9999)
    )

    rows = any_conn.execute(
        db.q(f"""SELECT {db.date_expr('started_at')} AS day,
                        SUM(typed_chars) AS typed,
                        SUM(pasted_chars) AS pasted,
                        SUM(likely_ai_pastes) AS ai_pastes
                 FROM sessions
                 WHERE user_id = ? AND category IN ('writing', 'assessment')
                   AND started_at >= ?
                 GROUP BY day ORDER BY day ASC"""),
        ("u1", 0),
    ).fetchall()

    assert len(rows) == 1
    row = dict(rows[0])
    assert row["day"] == "2026-07-25"
    # writing + assessment summed; the ai_assistant session's 9999 excluded.
    assert int(row["typed"]) == 350
    assert int(row["pasted"]) == 500
