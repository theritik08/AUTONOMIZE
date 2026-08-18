"""The AUTONOMIZE_PG_SCHEMA setting — the one that decides WHERE the tables go.

This is a small amount of code guarding a large blast radius. Get it wrong
in the permissive direction and Autonomize's tables land in `public` on a
Supabase project, where they are reachable with the project's anon key;
get it wrong in the strict direction and a deploy comes up connected,
healthy, and writing to a schema nobody is reading from.

db.py resolves all of this at import time, so every test here reloads the
module under a patched environment rather than trying to mutate constants
after the fact — mutating them would test a state the real process can
never actually be in.
"""
import importlib
import os

import pytest

import db as db_module


def reload_db(monkeypatch, **env):
    """Re-imports db.py with a patched environment, and always restores the
    original module afterwards so a failure here can't leak a Postgres-mode
    db module into the rest of the suite."""
    for key in ("DATABASE_URL", "SUPABASE_DB_URL", "AUTONOMIZE_PG_SCHEMA"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return importlib.reload(db_module)


@pytest.fixture(autouse=True)
def restore_db_module():
    yield
    # Reload once more with the ambient (SQLite) environment so later test
    # modules see db.py exactly as they would have found it.
    for key in ("DATABASE_URL", "SUPABASE_DB_URL", "AUTONOMIZE_PG_SCHEMA"):
        os.environ.pop(key, None)
    importlib.reload(db_module)


def test_sqlite_path_ignores_the_schema_setting(monkeypatch):
    """A schema is a Postgres concept. Setting it while on SQLite should be
    inert, not an error — otherwise a shared .env file that carries the
    Postgres settings breaks every local dev run."""
    db = reload_db(monkeypatch, AUTONOMIZE_PG_SCHEMA="autonomize")
    assert db.USE_POSTGRES is False
    assert db.backend_description() == "local SQLite"


def test_defaults_to_public_when_unset(monkeypatch):
    db = reload_db(monkeypatch, DATABASE_URL="postgresql://u:p@localhost/x")
    assert db.PG_SCHEMA == "public"
    # No search_path override at all would leave the connection on whatever
    # the role's default is, which is not necessarily public.
    assert db._PG_CONNECT_KWARGS == {"options": "-c search_path=public"}


def test_named_schema_is_pushed_onto_every_connection(monkeypatch):
    db = reload_db(
        monkeypatch,
        DATABASE_URL="postgresql://u:p@localhost/x",
        AUTONOMIZE_PG_SCHEMA="autonomize",
    )
    assert db.PG_SCHEMA == "autonomize"
    # Set as a libpq connection option rather than a `SET search_path`
    # statement, because a pooled connection is handed out many times and
    # only connection-level settings survive that.
    assert db._PG_CONNECT_KWARGS == {"options": "-c search_path=autonomize"}


def test_blank_schema_falls_back_to_public(monkeypatch):
    """An empty env var is how a templated deploy config expresses
    "unset" — treating it as a schema named '' would produce a baffling
    error at first query."""
    db = reload_db(
        monkeypatch,
        DATABASE_URL="postgresql://u:p@localhost/x",
        AUTONOMIZE_PG_SCHEMA="   ",
    )
    assert db.PG_SCHEMA == "public"


@pytest.mark.parametrize(
    "bad",
    [
        "public; DROP TABLE users",   # the obvious one
        "auto-nomize",                # hyphen: valid-looking, not an identifier
        "9lives",                     # can't start with a digit
        "autonomize schema",          # space
        'auto"nomize',                # quote
        "a" * 64,                     # past Postgres's 63-byte identifier limit
    ],
)
def test_a_schema_name_that_is_not_an_identifier_is_refused_at_import(monkeypatch, bad):
    """The name is interpolated into `CREATE SCHEMA` and `search_path`,
    neither of which takes a bound parameter — so it is validated instead
    of trusted. Failing at import means a bad value can't reach the
    database at all, rather than failing later at a confusing place."""
    with pytest.raises(ValueError, match="not a plain SQL identifier"):
        reload_db(
            monkeypatch,
            DATABASE_URL="postgresql://u:p@localhost/x",
            AUTONOMIZE_PG_SCHEMA=bad,
        )


def test_health_string_names_a_non_default_schema(monkeypatch):
    db = reload_db(
        monkeypatch,
        DATABASE_URL="postgresql://u:p@localhost/x",
        AUTONOMIZE_PG_SCHEMA="autonomize",
    )
    assert "schema autonomize" in db.backend_description()


def test_health_string_stays_quiet_on_the_default_schema(monkeypatch):
    db = reload_db(monkeypatch, DATABASE_URL="postgresql://u:p@localhost/x")
    assert "schema" not in db.backend_description()
