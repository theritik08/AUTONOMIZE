"""Guards against the migration script silently going stale again.

The original version hard-coded its table and column lists. Two schema
migrations later it was quietly copying less than it claimed to — dropping
`n_observations` and skipping three whole tables — without ever failing.
These tests fail the moment a new table is added without teaching the
script about it, which is the only way a "don't lose data" script stays
trustworthy.

No Postgres needed: everything checked here is about the script agreeing
with the schema, not about executing the copy.
"""
import db
import migrate_sqlite_to_postgres as mig


def test_every_user_scoped_table_has_a_conflict_key():
    missing = [t for t in db.USER_SCOPED_TABLES if t not in mig.CONFLICT_KEYS]
    assert missing == [], (
        f"migrate_sqlite_to_postgres.py has no ON CONFLICT key for {missing}. "
        "Without one those rows would either be skipped or duplicated on a re-run."
    )


def test_no_conflict_keys_for_tables_that_do_not_exist():
    stale = [t for t in mig.CONFLICT_KEYS if t not in db.USER_SCOPED_TABLES]
    assert stale == [], f"conflict keys defined for tables no longer in the schema: {stale}"


def test_conflict_keys_match_the_real_primary_keys(sqlite_conn):
    """A wrong conflict key breaks idempotency in the worst way — the second
    run inserts duplicates rather than erroring."""
    for table, keys in mig.CONFLICT_KEYS.items():
        pk = [
            row["name"]
            for row in sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()
            if row["pk"]
        ]
        assert sorted(keys) == sorted(pk), (
            f"{table}: script uses ON CONFLICT ({keys}) but the primary key is {pk}"
        )


def test_columns_are_read_from_the_live_schema_not_hard_coded(sqlite_conn):
    # The actual mechanism that makes this script drift-proof.
    columns = mig.sqlite_columns(sqlite_conn, "user_baseline")
    assert "n_observations" in columns, (
        "columns must come from the live schema — n_observations was exactly "
        "the column the hard-coded list silently dropped"
    )
    # Deliberately an exact match rather than a superset: this test exists
    # because a hard-coded column list silently dropped one, and a superset
    # assertion would not have caught that. The cost is that it has to be
    # updated whenever a migration adds a column — which is the point, since
    # that update is the moment to check the migrate script still copies it.
    assert set(columns) == {
        "user_id", "category", "ema_mean", "ema_var", "streak_days",
        "last_active_date", "last_score", "updated_at", "n_observations",
        "rhythm_mean", "rhythm_var", "rhythm_n", "score_window",
    }
