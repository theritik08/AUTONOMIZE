"""One-off script: copy an existing local `autonomize.db` (SQLite) into a
Postgres database — e.g. a freshly connected Supabase project — so you
don't lose data you already collected while running the zero-setup local
backend.

Idempotent: safe to run more than once (uses ON CONFLICT DO NOTHING), so
re-running after collecting more local data just copies the new rows.

Usage:
    export DATABASE_URL=postgresql://...   # your Postgres/Supabase URL
    python3 migrate_sqlite_to_postgres.py

Not run automatically by anything — this is a manual, one-time step.

A NOTE ON WHY THE COLUMN LISTS AREN'T WRITTEN OUT HERE
------------------------------------------------------
They used to be. Two schema migrations later that hand-written list had
silently gone stale: it still copied the original eight `user_baseline`
columns (dropping `n_observations`, so every migrated user's anomaly
detection would reset to "insufficient data"), and it had no idea the
`nudge_events`, `bandit_state` and `session_labels` tables existed — so a
user's entire learned nudge policy was discarded by a script whose whole
job is to not lose anything. Nothing failed loudly; the migration just
quietly copied less than it claimed to.

So the table list now comes from `db.USER_SCOPED_TABLES` and the columns
are read from the live SQLite schema at runtime. Adding a table or column
in migrations.py now carries this script along automatically, instead of
leaving a trap for whoever migrates next.
"""
import os
import sqlite3
import sys
from pathlib import Path

from _env import load_dotenv

load_dotenv()

import db

# Environment checks live in main(), not at module scope, so this file can
# be imported (by tests/test_migrate_script.py) without a bare `import`
# calling sys.exit on any machine that hasn't got DATABASE_URL set. A
# script that can't be imported is a script that can't be tested.

# Primary keys, needed for the ON CONFLICT clause that makes this rerunnable.
CONFLICT_KEYS = {
    "sessions": ["session_id"],
    "user_baseline": ["user_id", "category"],
    "nudge_events": ["event_id"],
    "bandit_state": ["user_id", "arm"],
    "session_labels": ["session_id"],
    "user_settings": ["user_id"],
}


def sqlite_columns(conn, table) -> list:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def copy_table(sqlite_conn, pg_conn, table) -> None:
    if table not in CONFLICT_KEYS:
        # Better to stop and say so than to invent a conflict key and
        # produce duplicates on the second run.
        print(f"{table}: SKIPPED — no conflict key defined in this script")
        return

    columns = sqlite_columns(sqlite_conn, table)
    if not columns:
        print(f"{table}: not present in the source database, skipping")
        return

    rows = sqlite_conn.execute(f"SELECT {', '.join(columns)} FROM {table}").fetchall()
    if not rows:
        print(f"{table}: nothing to copy")
        return

    placeholders = ", ".join(["%s"] * len(columns))
    conflict = ", ".join(CONFLICT_KEYS[table])
    sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )

    copied = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            cur.execute(sql, tuple(row[c] for c in columns))
            copied += cur.rowcount
    pg_conn.commit()
    print(
        f"{table}: copied {copied} of {len(rows)} row(s) across {len(columns)} columns "
        f"(skipped rows already present)"
    )


def main() -> None:
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not database_url:
        sys.exit(
            "DATABASE_URL is not set. Set it to your Postgres/Supabase connection string "
            "(in backend/.env or your shell) before running this migration."
        )

    sqlite_path = Path(
        os.environ.get("AUTONOMIZE_DB_PATH") or (Path(__file__).parent / "autonomize.db")
    )
    if not sqlite_path.exists():
        sys.exit(f"No local SQLite database found at {sqlite_path} — nothing to migrate.")

    if not db.USE_POSTGRES:
        # db.py resolves its backend at import time. If it came up in
        # SQLite mode, DATABASE_URL was set after the import and every
        # "copy" below would silently write back into SQLite.
        sys.exit(
            "db.py loaded in SQLite mode — set DATABASE_URL in the environment "
            "before running this script, not after."
        )

    import psycopg
    from psycopg.rows import dict_row

    # Brings the destination schema up to date before anything is copied.
    applied = db.init_db()
    if applied:
        print(f"destination schema: applied migrations {applied}")

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row
    pg_conn = psycopg.connect(database_url, row_factory=dict_row)

    try:
        for table in db.USER_SCOPED_TABLES:
            copy_table(sqlite_conn, pg_conn, table)
    finally:
        sqlite_conn.close()
        pg_conn.close()

    print("Done.")


if __name__ == "__main__":
    main()
