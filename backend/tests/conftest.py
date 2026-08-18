"""Shared fixtures for the backend test suite.

`sqlite_conn` builds a fresh in-memory SQLite database per test by running
the real migration list (see migrations.py) — the same path production
takes — rather than a hand-duplicated schema that could drift out of sync.
Tests call db.py's functions directly with this connection; they never
touch the real autonomize.db file.

`any_conn` is the same thing parameterized across BOTH backends. It yields
SQLite always, and Postgres too when TEST_DATABASE_URL is set, so the
dual-backend promise is enforced by the suite instead of only being
documented. Without that env var the Postgres case is skipped, keeping
`pytest` a zero-setup command by default.
"""
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import db  # noqa: E402
import migrations  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _fresh_sqlite():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrations.apply_migrations(conn, use_postgres=False, now_ms=int(time.time() * 1000))
    conn.commit()
    return conn


@pytest.fixture()
def sqlite_conn():
    conn = _fresh_sqlite()
    yield conn
    conn.close()


@pytest.fixture(params=["sqlite", "postgres"])
def any_conn(request):
    """A connection to each supported backend, with the real schema applied.

    Both branches yield an object with the same `.execute(sql, params)`
    interface and dict-like rows, which is exactly the contract db.py's
    functions are written against — so a test using this fixture proves
    the query strings work on both dialects, not just SQLite's.
    """
    if request.param == "sqlite":
        conn = _fresh_sqlite()
        # db.q() is a module-level function keyed off USE_POSTGRES; force
        # it to match the connection this test is actually running against.
        _with_backend(False)
        yield conn
        conn.close()
        return

    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set — skipping the Postgres half of the dual-backend suite")

    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row

    # A throwaway schema per test keeps parallel/repeat runs isolated
    # without needing a separate database per run.
    schema = f"autonomize_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row, autocommit=True)
    conn.execute(f'CREATE SCHEMA "{schema}"')
    conn.execute(f'SET search_path TO "{schema}"')
    migrations.apply_migrations(conn, use_postgres=True, now_ms=int(time.time() * 1000))

    _with_backend(True)
    try:
        yield conn
    finally:
        _with_backend(False)
        conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        conn.close()


def _with_backend(use_postgres: bool):
    """Flips db.py's dialect switches for the duration of a test.

    db.py resolves USE_POSTGRES and the placeholder style at import time
    (a deliberate choice — it means zero per-query branching in the hot
    path). Tests that exercise the Postgres dialect therefore have to flip
    them explicitly; the `any_conn` fixture always restores SQLite on the
    way out so no test leaks its dialect into the next one.
    """
    db.USE_POSTGRES = use_postgres
    db._PLACEHOLDER = "%s" if use_postgres else "?"
