"""Round-trip tests for db.py's SQLite path (the default, zero-config
backend). Uses an in-memory SQLite database built from db.py's own
SQLITE_SCHEMA (see conftest.sqlite_conn) so these tests exercise the exact
schema and query strings the real app runs, without ever touching
autonomize.db or requiring Postgres/DATABASE_URL.
"""
import db


def make_payload(session_id="s1", user_id="u1", category="writing", is_final=False, **metric_overrides):
    metrics = {
        "typed_chars": 100, "pasted_chars": 0, "backspace_count": 0,
        "revision_count": 0, "prompt_count": 0, "likely_ai_pastes": 0,
        "tab_switch_count": 0,
    }
    metrics.update(metric_overrides)
    return {
        "session_id": session_id,
        "user_id": user_id,
        "category": category,
        "domain": "docs.google.com",
        "path": "/document/d/abc",
        "started_at": 1_700_000_000_000,
        "active_ms": 60_000,
        "metrics": metrics,
        "is_final": is_final,
    }


def test_get_session_returns_none_when_absent(sqlite_conn):
    assert db.get_session(sqlite_conn, "does-not-exist") is None


def test_upsert_session_row_inserts_new_session(sqlite_conn):
    row = db.upsert_session_row(sqlite_conn, make_payload(typed_chars=250))
    assert row["session_id"] == "s1"
    assert row["user_id"] == "u1"
    assert row["typed_chars"] == 250
    assert row["active_ms"] == 60_000
    assert row["finalized"] == 0


def test_upsert_session_row_accumulates_metrics_on_repeat_calls(sqlite_conn):
    db.upsert_session_row(sqlite_conn, make_payload(typed_chars=100))
    row = db.upsert_session_row(sqlite_conn, make_payload(typed_chars=50))
    # Metrics accumulate (delta-style), not overwrite.
    assert row["typed_chars"] == 150


def test_upsert_session_row_accumulates_active_ms(sqlite_conn):
    db.upsert_session_row(sqlite_conn, make_payload())
    row = db.upsert_session_row(sqlite_conn, make_payload())
    assert row["active_ms"] == 120_000


def test_upsert_session_row_finalized_flag_sticks_once_set(sqlite_conn):
    db.upsert_session_row(sqlite_conn, make_payload(is_final=False))
    row = db.upsert_session_row(sqlite_conn, make_payload(is_final=True))
    assert row["finalized"] == 1
    # A later non-final upsert (e.g. a stray retry) shouldn't un-finalize it.
    row = db.upsert_session_row(sqlite_conn, make_payload(is_final=False))
    assert row["finalized"] == 1


def test_set_session_score(sqlite_conn):
    db.upsert_session_row(sqlite_conn, make_payload())
    db.set_session_score(sqlite_conn, "s1", 87.5)
    row = db.get_session(sqlite_conn, "s1")
    assert row["score"] == 87.5


def test_get_baseline_returns_none_when_absent(sqlite_conn):
    assert db.get_baseline(sqlite_conn, "u1", "writing") is None


def test_save_and_get_baseline_round_trip(sqlite_conn):
    db.save_baseline(sqlite_conn, "u1", "writing", ema_mean=72.5, ema_var=10.0,
                      streak_days=3, last_active_date="2026-08-01", last_score=80.0)
    row = db.get_baseline(sqlite_conn, "u1", "writing")
    assert row["ema_mean"] == 72.5
    assert row["ema_var"] == 10.0
    assert row["streak_days"] == 3
    assert row["last_active_date"] == "2026-08-01"
    assert row["last_score"] == 80.0


def test_save_baseline_upserts_on_conflict(sqlite_conn):
    db.save_baseline(sqlite_conn, "u1", "writing", ema_mean=50.0, ema_var=0.0,
                      streak_days=1, last_active_date="2026-08-01", last_score=50.0)
    db.save_baseline(sqlite_conn, "u1", "writing", ema_mean=60.0, ema_var=5.0,
                      streak_days=2, last_active_date="2026-08-02", last_score=70.0)
    row = db.get_baseline(sqlite_conn, "u1", "writing")
    assert row["ema_mean"] == 60.0
    assert row["streak_days"] == 2


def test_baselines_are_isolated_per_category(sqlite_conn):
    # A user's writing and assessment baselines must never mix — this is
    # the whole point of keying by (user_id, category), called out
    # explicitly in scoring.py's module docstring.
    db.save_baseline(sqlite_conn, "u1", "writing", 90.0, 0.0, 5, "2026-08-01", 90.0)
    db.save_baseline(sqlite_conn, "u1", "assessment", 40.0, 0.0, 0, "2026-08-01", 40.0)
    writing = db.get_baseline(sqlite_conn, "u1", "writing")
    assessment = db.get_baseline(sqlite_conn, "u1", "assessment")
    assert writing["ema_mean"] == 90.0
    assert assessment["ema_mean"] == 40.0


def test_q_is_identity_under_sqlite_backend():
    # This whole test suite runs with DATABASE_URL unset, so db.py should
    # have loaded in SQLite mode — q() is a no-op passthrough there.
    assert db.USE_POSTGRES is False
    assert db.q("SELECT * FROM sessions WHERE user_id = ?") == "SELECT * FROM sessions WHERE user_id = ?"


def test_date_expr_sqlite_form():
    assert db.date_expr("started_at") == "date(started_at/1000, 'unixepoch')"


def test_autonomize_db_path_env_override():
    # DB_PATH is computed at module import time, so this exercises it in a
    # fresh subprocess rather than monkeypatching the already-imported `db`
    # module — the thing actually under test (see db.py's DB_PATH comment)
    # is what a *fresh* process resolves given the env var, which is
    # exactly what e2e/playwright.config.ts relies on to keep its fixture
    # database separate from a developer's real backend/autonomize.db.
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_dir = Path(__file__).parent.parent
    custom_path = "/tmp/autonomize-test-db-path-override.db"

    env = dict(os.environ)
    env["AUTONOMIZE_DB_PATH"] = custom_path
    result = subprocess.run(
        [sys.executable, "-c", "import db; print(db.DB_PATH)"],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == custom_path


def test_db_path_defaults_to_backend_dir_when_unset():
    import os
    import subprocess
    import sys
    from pathlib import Path

    backend_dir = Path(__file__).parent.parent
    env = dict(os.environ)
    env.pop("AUTONOMIZE_DB_PATH", None)
    result = subprocess.run(
        [sys.executable, "-c", "import db; print(db.DB_PATH)"],
        cwd=str(backend_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(backend_dir / "autonomize.db")


def test_the_connection_block_is_a_real_transaction(tmp_path, monkeypatch):
    """Pins atomicity of the scoring path.

    /api/session/upsert writes the session, scores it, updates the baseline
    and settles bandit rewards inside one `with db.get_conn()` block. That
    block already commits on clean exit and rolls back on exception — but
    nothing asserted it, so a future refactor that committed early (or
    swapped in an autocommit connection) would silently allow a half-written
    scoring pass: a session recorded as scored whose baseline never saw it,
    with no repair path because the EMA is not reconstructible.
    """
    import importlib
    monkeypatch.setenv("AUTONOMIZE_DB_PATH", str(tmp_path / "tx.db"))
    import db as db_module
    importlib.reload(db_module)
    db_module.init_db()

    class Boom(Exception):
        pass

    try:
        with db_module.get_conn() as conn:
            conn.execute(
                db_module.q("""INSERT INTO sessions
                    (session_id, user_id, category, active_ms, started_at,
                     finalized, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?)"""),
                ("s-tx", "u-tx", "writing", 1000, 1, 1, 1, 1),
            )
            raise Boom("crash between the session write and the baseline update")
    except Boom:
        pass

    with db_module.get_conn() as conn:
        n = dict(conn.execute(
            db_module.q("SELECT COUNT(*) AS n FROM sessions WHERE session_id = ?"),
            ("s-tx",),
        ).fetchone())["n"]

    assert n == 0, "a partial scoring pass survived — the block is not atomic"
    importlib.reload(db_module)
