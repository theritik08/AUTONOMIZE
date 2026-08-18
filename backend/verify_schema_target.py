"""Proves AUTONOMIZE_PG_SCHEMA actually puts the tables where it says.

    DATABASE_URL=postgresql://... AUTONOMIZE_PG_SCHEMA=autonomize \
        python3 verify_schema_target.py

The unit tests in tests/test_pg_schema.py check that db.py builds the
right connection options. That is worth checking, but it is a string
assertion — it cannot tell you whether libpq honours the option, whether a
pooled connection keeps it when it is handed back out, or whether
`CREATE TABLE sessions` under that search_path lands where you expect.
Those are the ways this feature would actually fail, and all three need a
real server to observe.

So this connects for real, runs the real migrations, and then asks the
catalog where the tables ended up — including asserting that `public` did
NOT quietly receive them, which is the failure mode that matters: it is
silent, it looks like success, and on a Supabase project it is the
difference between "not exposed to the API" and "exposed to the API".

Exits non-zero on any mismatch, so CI can depend on it.
"""
import os
import sys

EXPECTED_TABLES = {
    "schema_migrations", "sessions", "user_baseline", "nudge_events",
    "bandit_state", "session_labels", "users", "auth_sessions", "audit_log",
}


def main() -> int:
    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set — nothing to verify.", file=sys.stderr)
        return 2

    # Imported here, not at module scope: db.py resolves DATABASE_URL and
    # the schema at import time, so the env has to be settled first.
    import db

    if not db.USE_POSTGRES:
        print("db is not in Postgres mode.", file=sys.stderr)
        return 2

    schema = db.PG_SCHEMA
    # Open the pool, because pooled connections are what production
    # actually uses and are the path where a connection-level setting
    # could plausibly be lost between checkouts. Verifying only the
    # unpooled path would be verifying the easy case.
    db.open_pool()
    print(f"target schema: {schema}")
    print(f"backend      : {db.backend_description()}")

    applied = db.init_db()
    print(f"migrations applied this run: {applied or 'none (already current)'}")

    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
        ).fetchall()
        found = {r["tablename"] for r in rows}

        # The connection's own view. If search_path were being dropped —
        # by the pool, by a proxy, by a role default — an unqualified query
        # would still "work" while reading a different schema's table.
        path = conn.execute("SHOW search_path").fetchone()
        print(f"connection search_path: {list(path.values())[0]}")

        leaked = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(%s)",
            (sorted(EXPECTED_TABLES),),
        ).fetchall()

    missing = EXPECTED_TABLES - found
    ok = True

    if missing:
        print(f"MISSING from {schema}: {sorted(missing)}", file=sys.stderr)
        ok = False
    else:
        print(f"all {len(EXPECTED_TABLES)} tables present in {schema}")

    if schema != "public" and leaked:
        # The whole point of the setting. Tables in `public` on a Supabase
        # project are served over PostgREST to the anon key.
        print(
            f"LEAKED into public: {sorted(r['tablename'] for r in leaked)}",
            file=sys.stderr,
        )
        ok = False
    elif schema != "public":
        print("public schema is clean — nothing leaked out of the target schema")

    # An unqualified write has to reach the target schema, not just exist
    # somewhere. This is the actual application code path.
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, user_id, category, active_ms,"
            " created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
            ("schema-probe", "schema-probe", "writing", 0, 0, 0),
        )
    with db.get_conn() as conn:
        where = conn.execute(
            "SELECT schemaname FROM pg_tables t WHERE t.tablename = 'sessions'"
            " AND EXISTS (SELECT 1 FROM sessions s WHERE s.session_id = 'schema-probe')"
        ).fetchall()
        landed = {r["schemaname"] for r in where}
        conn.execute("DELETE FROM sessions WHERE session_id = 'schema-probe'")

    if landed and schema in landed:
        print(f"unqualified INSERT landed in {schema}")
    else:
        print(f"unqualified INSERT did NOT land in {schema} (saw {landed})", file=sys.stderr)
        ok = False

    db.close_pool()
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
