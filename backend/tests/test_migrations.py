"""Tests for the versioned migration runner.

The property that matters most here is the upgrade path: a database
created by the *pre-migrations* version of this app (bare CREATE TABLE,
no schema_migrations row) must come out fully migrated and with its data
intact. That's the case a released install actually hits.
"""
import sqlite3
import time

import pytest

import migrations

NOW = 1_785_000_000_000

# The exact schema the app shipped before migrations.py existed.
LEGACY_SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sessions (
        session_id       TEXT PRIMARY KEY,
        user_id          TEXT NOT NULL,
        category         TEXT NOT NULL,
        domain           TEXT,
        path             TEXT,
        started_at       INTEGER,
        active_ms        INTEGER NOT NULL DEFAULT 0,
        typed_chars      INTEGER NOT NULL DEFAULT 0,
        pasted_chars     INTEGER NOT NULL DEFAULT 0,
        backspace_count  INTEGER NOT NULL DEFAULT 0,
        revision_count   INTEGER NOT NULL DEFAULT 0,
        prompt_count     INTEGER NOT NULL DEFAULT 0,
        likely_ai_pastes INTEGER NOT NULL DEFAULT 0,
        tab_switch_count INTEGER NOT NULL DEFAULT 0,
        finalized        INTEGER NOT NULL DEFAULT 0,
        score            REAL,
        created_at       INTEGER NOT NULL,
        updated_at       INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_baseline (
        user_id          TEXT NOT NULL,
        category         TEXT NOT NULL,
        ema_mean         REAL,
        ema_var          REAL,
        streak_days      INTEGER NOT NULL DEFAULT 0,
        last_active_date TEXT,
        last_score       REAL,
        updated_at       INTEGER NOT NULL,
        PRIMARY KEY (user_id, category)
    )""",
]


def _blank():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row["name"] for row in rows}


def test_fresh_database_gets_every_migration():
    conn = _blank()
    ran = migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)
    assert ran == [m.version for m in migrations.MIGRATIONS]
    assert migrations.applied_versions(conn, False) == set(ran)


def test_all_expected_tables_exist_after_migrating():
    conn = _blank()
    migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)
    assert {"sessions", "user_baseline", "nudge_events", "bandit_state",
            "session_labels", "schema_migrations"} <= _tables(conn)


def test_migrations_are_idempotent():
    conn = _blank()
    migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)
    # Second run must be a no-op. If any migration were re-executed the
    # ALTER TABLE in version 2 would raise "duplicate column name".
    ran_again = migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)
    assert ran_again == []


def test_legacy_database_upgrades_without_losing_data():
    conn = _blank()
    for stmt in LEGACY_SQLITE_SCHEMA:
        conn.execute(stmt)
    conn.execute(
        """INSERT INTO sessions (session_id, user_id, category, started_at, active_ms,
                                 typed_chars, created_at, updated_at)
           VALUES ('legacy-1','u1','writing',1,60000,500,1,1)"""
    )
    conn.execute(
        """INSERT INTO user_baseline (user_id, category, ema_mean, ema_var, streak_days,
                                      last_active_date, last_score, updated_at)
           VALUES ('u1','writing',77.0,12.0,4,'2026-08-01',80.0,1)"""
    )

    ran = migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)

    # Everything runs, including migration 1 (harmlessly, via IF NOT EXISTS).
    assert ran == [m.version for m in migrations.MIGRATIONS]
    # The pre-existing rows survive.
    assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
    baseline = conn.execute("SELECT * FROM user_baseline").fetchone()
    assert baseline["ema_mean"] == 77.0
    # And the new column is present, defaulted, on the pre-existing row.
    assert "n_observations" in _columns(conn, "user_baseline")
    assert baseline["n_observations"] == 0


def test_partially_migrated_database_only_runs_the_remainder():
    conn = _blank()
    # Simulate an install that stopped after version 1.
    conn.execute(
        """CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)"""
    )
    for stmt in migrations.MIGRATIONS[0].statements(False):
        conn.execute(stmt)
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'initial schema', 1)")

    ran = migrations.apply_migrations(conn, use_postgres=False, now_ms=NOW)
    assert 1 not in ran
    assert ran == [m.version for m in migrations.MIGRATIONS if m.version != 1]


def test_schema_version_matches_the_highest_migration():
    assert migrations.SCHEMA_VERSION == max(m.version for m in migrations.MIGRATIONS)


def test_migration_versions_are_unique_and_contiguous():
    versions = sorted(m.version for m in migrations.MIGRATIONS)
    assert len(versions) == len(set(versions)), "duplicate migration version"
    assert versions == list(range(1, len(versions) + 1)), "migration versions must be 1..N with no gaps"


@pytest.mark.parametrize("migration", migrations.MIGRATIONS, ids=lambda m: f"v{m.version}")
def test_every_migration_defines_statements_for_both_backends(migration):
    # A migration that forgets its Postgres half would apply cleanly on
    # SQLite and silently skip on Postgres, leaving the two schemas
    # divergent with the same recorded version number.
    assert migration.statements(False), f"migration {migration.version} has no SQLite statements"
    assert migration.statements(True), f"migration {migration.version} has no Postgres statements"
