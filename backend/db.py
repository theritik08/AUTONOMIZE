"""Persistence layer for Autonomize.

Two backends behind one identical API, selected at import time by the
`DATABASE_URL` environment variable:

  - unset  -> local SQLite file (`autonomize.db`). Zero extra infra beyond
              `pip install -r requirements.txt` — this is still the
              zero-setup path for local dev, a class demo, or CI.
  - set    -> Postgres via `psycopg` (v3). This is the path for a real
              deployment — a Supabase project's connection string is a
              Postgres connection string, so "connect Supabase" here just
              means setting this one environment variable.

Every row still stores only aggregate counters and timings, never raw
typed or pasted text, on either backend — the privacy contract lives in
what the extension sends, not in where the bytes are stored, so switching
backends doesn't change it.

The two backends share one set of query strings (written SQLite-style,
with `?` placeholders) and one migration list (see migrations.py, which
also documents the handful of places the two dialects genuinely differ).

Connection handling differs by backend for a real reason. SQLite opens a
local file — cheap, so a connection per request is fine. Postgres opens a
TCP connection and, against a managed instance like Supabase, counts
against a hard connection limit; a connection per request is both slow
(TLS handshake on every call) and a way to exhaust that limit under load.
So the Postgres path runs through a `psycopg_pool.ConnectionPool` opened
once at startup. If `psycopg_pool` isn't installed the code falls back to
per-request connections and says so at startup rather than refusing to
run.
"""
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import migrations

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
USE_POSTGRES = bool(DATABASE_URL)

# Which Postgres schema the tables live in. Defaults to `public`, which is
# what a dedicated database wants.
#
# Set it when the database is shared. A Supabase project often already
# hosts another app's tables in `public`, and Supabase additionally exposes
# `public` over PostgREST — so anything sitting there is reachable with the
# project's anon key unless every table is individually protected. Putting
# Autonomize in its own schema makes it unreachable that way by
# construction rather than by policy, and makes removing it one
# `DROP SCHEMA` instead of a list of tables someone has to keep current.
PG_SCHEMA = os.environ.get("AUTONOMIZE_PG_SCHEMA", "public").strip() or "public"

# The schema name is interpolated into SQL (search_path and CREATE SCHEMA
# take an identifier, not a bindable parameter), so it is validated rather
# than trusted. Environment variables are not automatically friendly input:
# this one arrives from a deploy config that someone might template.
if USE_POSTGRES and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", PG_SCHEMA):
    raise ValueError(
        f"AUTONOMIZE_PG_SCHEMA={PG_SCHEMA!r} is not a plain SQL identifier. "
        "Use letters, digits and underscores, starting with a letter or underscore."
    )

# Applied at connection time, not per query, so it holds for every
# statement on the connection including the ones psycopg issues itself —
# and, critically, survives a pooled connection being handed back out.
_PG_CONNECT_KWARGS = {"options": f"-c search_path={PG_SCHEMA}"} if USE_POSTGRES else {}

POOL_MIN_SIZE = int(os.environ.get("AUTONOMIZE_PG_POOL_MIN", "1"))
POOL_MAX_SIZE = int(os.environ.get("AUTONOMIZE_PG_POOL_MAX", "10"))

_pool = None
POOLING_AVAILABLE = False

if USE_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

    try:
        from psycopg_pool import ConnectionPool

        POOLING_AVAILABLE = True
    except ImportError:  # pragma: no cover - depends on the install
        ConnectionPool = None
else:
    import sqlite3

# AUTONOMIZE_DB_PATH lets something other than a developer's own local dev
# server point SQLite at a different file — used by the e2e suite (see
# e2e/playwright.config.ts) so a test run's fixture data never lands in the
# same autonomize.db a `uvicorn main:app --port 8787` dev session reads.
# Unset (the default for everyone else) behaves exactly as before.
DB_PATH = Path(os.environ["AUTONOMIZE_DB_PATH"]) if os.environ.get("AUTONOMIZE_DB_PATH") else Path(__file__).parent / "autonomize.db"

# Every query below is written once, SQLite-style (`?` placeholders); this
# swaps them for Postgres's `%s` when that backend is active, rather than
# hand-maintaining two copies of every query.
_PLACEHOLDER = "%s" if USE_POSTGRES else "?"


def q(sql: str) -> str:
    return sql.replace("?", _PLACEHOLDER) if USE_POSTGRES else sql


def date_expr(column: str) -> str:
    """SQL fragment returning a session's start date as 'YYYY-MM-DD' from
    a millisecond-epoch column — the one piece of SQL that isn't portable
    as plain text between SQLite's date() and Postgres's to_timestamp()."""
    if USE_POSTGRES:
        return f"to_char(to_timestamp({column} / 1000.0), 'YYYY-MM-DD')"
    return f"date({column}/1000, 'unixepoch')"


def open_pool():
    """Starts the Postgres connection pool. No-op on SQLite, or when
    psycopg_pool isn't installed. Safe to call more than once."""
    global _pool
    if not USE_POSTGRES or not POOLING_AVAILABLE or _pool is not None:
        return _pool
    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=POOL_MIN_SIZE,
        max_size=POOL_MAX_SIZE,
        kwargs={"row_factory": dict_row, **_PG_CONNECT_KWARGS},
        open=True,
    )
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def backend_description() -> str:
    if not USE_POSTGRES:
        return "local SQLite"
    # The schema is named only when it isn't the default, so /api/health
    # says something when it matters and stays quiet when it doesn't —
    # "which schema am I actually writing to" is the first question when a
    # deploy comes up connected but empty.
    where = "" if PG_SCHEMA == "public" else f", schema {PG_SCHEMA}"
    if _pool is not None:
        return f"Postgres (pooled, {POOL_MIN_SIZE}-{POOL_MAX_SIZE} connections{where})"
    if not POOLING_AVAILABLE:
        return f"Postgres (UNPOOLED — install psycopg_pool for connection reuse{where})"
    return f"Postgres (pool not yet opened{where})"


@contextmanager
def get_conn():
    if USE_POSTGRES:
        if _pool is not None:
            with _pool.connection() as conn:
                # The pool commits on clean exit and rolls back on exception.
                yield conn
            return
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row, **_PG_CONNECT_KWARGS)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        # Without this, a concurrent writer fails instantly with
        # "database is locked" instead of waiting its turn — easy to hit
        # once the extension's retry queue drains while the dashboard polls.
        conn.execute("PRAGMA busy_timeout=5000;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    """Brings the schema up to date. Idempotent, and safe against a
    database created before migrations.py existed (see its docstring)."""
    with get_conn() as conn:
        if USE_POSTGRES and PG_SCHEMA != "public":
            # search_path points at this schema, so every CREATE TABLE
            # below lands in it — but only if it exists. Without this, a
            # first deploy against a fresh database fails on the very
            # first statement with a bare "schema does not exist", which
            # reads like a config typo rather than a missing bootstrap
            # step. The name was validated as an identifier at import.
            conn.execute(f"CREATE SCHEMA IF NOT EXISTS {PG_SCHEMA}")
        return migrations.apply_migrations(conn, USE_POSTGRES, int(time.time() * 1000))


def ping() -> bool:
    """True if the database is actually reachable and answering queries —
    used by /api/health so a healthy-looking API can't mask a dead DB."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


METRIC_COLUMNS = [
    "typed_chars", "pasted_chars", "backspace_count", "revision_count",
    "prompt_count", "likely_ai_pastes", "tab_switch_count",
    # Rhythm scalars accumulate the same way every other counter does.
    # The histogram itself does not — it needs element-wise addition, which
    # SQL can't express against a JSON text column, so `merge_iki_buckets`
    # handles it in Python. See rhythm.py.
    "long_pauses", "burst_keys",
]

# Tables keyed by user_id, in the order a full purge should delete them.
# Every table keyed by user_id. Export and hard-delete both iterate this
# list rather than naming tables individually, so a new user-scoped table is
# covered by both the moment it is added here — which is the whole reason
# `user_settings` did not need its own deletion path. A settings row records
# which sites someone chose to hide from tracking; leaving it behind after
# an erase request would outlive the sessions it was protecting.
USER_SCOPED_TABLES = ["sessions", "user_baseline", "nudge_events", "bandit_state",
                      "session_labels", "user_settings"]


def get_session(conn, session_id):
    row = conn.execute(q("SELECT * FROM sessions WHERE session_id = ?"), (session_id,)).fetchone()
    return dict(row) if row else None


class SessionOwnershipError(PermissionError):
    """A write aimed at a session belonging to a different user."""


def upsert_session_row(conn, payload):
    """Accumulate delta counters into a session row (insert-or-add).

    OWNERSHIP IS CHECKED BEFORE ACCUMULATING.

    `session_id` is the primary key and it is chosen by the client, so two
    users can name the same one. Without this check the UPDATE branch below
    matched on `session_id` alone: a second user posting a session id that
    already existed had their counters ADDED INTO the first user's row.

    Found by an audit script that accidentally reused ids across two test
    users — user A's typed_chars went from 2,000 to 11,999 after user B
    posted. The row stayed owned by A, so this was never a data leak, but
    it let one account corrupt another's independence score, which for a
    tool that reports on students is arguably worse: the victim sees a
    score they cannot explain and the system has no record that anyone else
    touched it.

    Not exploitable at random — the extension generates UUIDs — but "the
    identifier is hard to guess" is not an authorization model.
    """
    now = int(time.time() * 1000)
    existing = get_session(conn, payload["session_id"])
    m = payload["metrics"]

    if existing is not None and existing["user_id"] != payload["user_id"]:
        raise SessionOwnershipError(
            "That session belongs to a different account.")

    if existing is None:
        conn.execute(
            q(f"""INSERT INTO sessions (
                session_id, user_id, category, domain, path, started_at,
                active_ms, {", ".join(METRIC_COLUMNS)}, finalized,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,{",".join(["?"] * len(METRIC_COLUMNS))},?,?,?)"""),
            (
                payload["session_id"], payload["user_id"], payload["category"],
                payload.get("domain"), payload.get("path"), payload.get("started_at"),
                payload.get("active_ms", 0),
                *[m.get(col, 0) for col in METRIC_COLUMNS],
                1 if payload.get("is_final") else 0,
                now, now,
            ),
        )
    else:
        set_clause = ", ".join(f"{col} = {col} + ?" for col in METRIC_COLUMNS)
        conn.execute(
            q(f"""UPDATE sessions SET
                domain = ?, path = ?, active_ms = active_ms + ?,
                {set_clause},
                finalized = ?, updated_at = ?
            WHERE session_id = ?"""),
            (
                payload.get("domain", existing["domain"]), payload.get("path", existing["path"]),
                payload.get("active_ms", 0),
                *[m.get(col, 0) for col in METRIC_COLUMNS],
                1 if (existing["finalized"] or payload.get("is_final")) else 0,
                now, payload["session_id"],
            ),
        )

    merge_iki_buckets(conn, payload["session_id"], m.get("iki_buckets"))
    return get_session(conn, payload["session_id"])


def merge_iki_buckets(conn, session_id, buckets):
    """Element-wise accumulation of the typing-rhythm histogram.

    A session flushes repeatedly (every 45s in the extension), so each
    payload carries the buckets for that window only and they have to be
    summed. Read-modify-write rather than SQL because the value is JSON
    text; that is safe here because a given session_id is only ever
    written by the one tab that owns it.

    A length mismatch is dropped rather than merged: it means the client
    is on a different bucket definition, and adding those counts together
    would produce a histogram whose buckets mean two different things.
    """
    if not buckets:
        return
    row = conn.execute(
        q("SELECT iki_buckets FROM sessions WHERE session_id = ?"), (session_id,)
    ).fetchone()
    if row is None:
        return

    incoming = [max(0, int(c or 0)) for c in buckets]
    existing_raw = dict(row).get("iki_buckets")
    merged = incoming
    if existing_raw:
        try:
            prior = json.loads(existing_raw)
        except (ValueError, TypeError):
            prior = None
        if isinstance(prior, list) and len(prior) == len(incoming):
            merged = [int(a or 0) + b for a, b in zip(prior, incoming)]

    conn.execute(
        q("UPDATE sessions SET iki_buckets = ? WHERE session_id = ?"),
        (json.dumps(merged), session_id),
    )


def set_session_score(conn, session_id, score, regularity=None):
    conn.execute(
        q("UPDATE sessions SET score = ?, regularity = ? WHERE session_id = ?"),
        (score, regularity, session_id),
    )


def get_baseline(conn, user_id, category):
    row = conn.execute(
        q("SELECT * FROM user_baseline WHERE user_id = ? AND category = ?"), (user_id, category)
    ).fetchone()
    return dict(row) if row else None


def save_baseline(conn, user_id, category, ema_mean, ema_var, streak_days,
                  last_active_date, last_score, n_observations=0,
                  rhythm_mean=None, rhythm_var=None, rhythm_n=0,
                  score_window=None):
    now = int(time.time() * 1000)
    conn.execute(
        q("""INSERT INTO user_baseline (user_id, category, ema_mean, ema_var, streak_days,
                                        last_active_date, last_score, n_observations,
                                        rhythm_mean, rhythm_var, rhythm_n,
                                        score_window, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id, category) DO UPDATE SET
               ema_mean=excluded.ema_mean, ema_var=excluded.ema_var,
               streak_days=excluded.streak_days, last_active_date=excluded.last_active_date,
               last_score=excluded.last_score, n_observations=excluded.n_observations,
               rhythm_mean=excluded.rhythm_mean, rhythm_var=excluded.rhythm_var,
               rhythm_n=excluded.rhythm_n, score_window=excluded.score_window,
               updated_at=excluded.updated_at"""),
        (user_id, category, ema_mean, ema_var, streak_days, last_active_date,
         last_score, n_observations, rhythm_mean, rhythm_var, rhythm_n,
         score_window, now),
    )


# ---------------------------------------------------------------------------
# Contextual bandit persistence (see bandit.py / nudge.py)
# ---------------------------------------------------------------------------

def get_bandit_arm(conn, user_id, arm):
    row = conn.execute(
        q("SELECT * FROM bandit_state WHERE user_id = ? AND arm = ?"), (user_id, arm)
    ).fetchone()
    return dict(row) if row else None


def save_bandit_arm(conn, user_id, arm, a_matrix_json, b_vector_json, n_pulls):
    now = int(time.time() * 1000)
    conn.execute(
        q("""INSERT INTO bandit_state (user_id, arm, a_matrix, b_vector, n_pulls, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id, arm) DO UPDATE SET
               a_matrix=excluded.a_matrix, b_vector=excluded.b_vector,
               n_pulls=excluded.n_pulls, updated_at=excluded.updated_at"""),
        (user_id, arm, a_matrix_json, b_vector_json, n_pulls, now),
    )


def insert_nudge_event(conn, event_id, user_id, arm, context_json, decided_at):
    conn.execute(
        q("""INSERT INTO nudge_events (event_id, user_id, arm, context, decided_at)
           VALUES (?,?,?,?,?)"""),
        (event_id, user_id, arm, context_json, decided_at),
    )


def get_nudge_event(conn, event_id):
    row = conn.execute(
        q("SELECT * FROM nudge_events WHERE event_id = ?"), (event_id,)
    ).fetchone()
    return dict(row) if row else None


def settle_nudge_event(conn, event_id, reward, settled_by):
    conn.execute(
        q("UPDATE nudge_events SET reward = ?, settled_at = ?, settled_by = ? WHERE event_id = ?"),
        (reward, int(time.time() * 1000), settled_by, event_id),
    )


def pending_nudge_events(conn, user_id, since_ms):
    """Decisions still awaiting a reward, oldest first."""
    rows = conn.execute(
        q("""SELECT * FROM nudge_events
             WHERE user_id = ? AND reward IS NULL AND decided_at >= ?
             ORDER BY decided_at ASC"""),
        (user_id, since_ms),
    ).fetchall()
    return [dict(r) for r in rows]


def count_nudges_since(conn, user_id, since_ms) -> int:
    row = conn.execute(
        q("SELECT COUNT(*) AS n FROM nudge_events WHERE user_id = ? AND decided_at >= ? AND arm <> 'none'"),
        (user_id, since_ms),
    ).fetchone()
    return int(row["n"] or 0)


# ---------------------------------------------------------------------------
# Comprehension labels (see fit_weights.py)
# ---------------------------------------------------------------------------

def upsert_session_label(conn, session_id, user_id, understood, note_present):
    conn.execute(
        q("""INSERT INTO session_labels (session_id, user_id, understood, note_present, created_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(session_id) DO UPDATE SET
               understood=excluded.understood, note_present=excluded.note_present"""),
        (session_id, user_id, understood, 1 if note_present else 0, int(time.time() * 1000)),
    )


# ---------------------------------------------------------------------------
# Export / delete — the API side of the README's privacy claims
# ---------------------------------------------------------------------------

def export_user_data(conn, user_id) -> dict:
    out = {}
    for table in USER_SCOPED_TABLES:
        rows = conn.execute(q(f"SELECT * FROM {table} WHERE user_id = ?"), (user_id,)).fetchall()
        out[table] = [dict(r) for r in rows]
    return out


def delete_user_data(conn, user_id) -> dict:
    """Hard-deletes every row this user owns. Returns per-table counts."""
    deleted = {}
    for table in USER_SCOPED_TABLES:
        before = conn.execute(
            q(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id = ?"), (user_id,)
        ).fetchone()["n"]
        conn.execute(q(f"DELETE FROM {table} WHERE user_id = ?"), (user_id,))
        deleted[table] = int(before or 0)
    return deleted
