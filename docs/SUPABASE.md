# Running Autonomize on Supabase

Autonomize stores everything in Postgres, and a Supabase project is a
Postgres database with a control panel on top. So "connect Supabase" is
one environment variable, plus one decision about where the tables live.

Nothing about the privacy contract changes here. The extension still
counts characters and never reads them, so what lands in Supabase is the
same aggregate counters that land in the local SQLite file: character
totals, durations, timestamps, scores. There is no column that could hold
a student's writing, on either backend.

## The short version

```bash
cd backend
cp .env.example .env
# then edit .env:
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
AUTONOMIZE_PG_SCHEMA=autonomize

uvicorn main:app --port 8787
curl localhost:8787/api/health
```

The health endpoint tells you which backend and which schema it actually
reached, so a project that came up connected-but-empty is diagnosable
without opening a SQL console:

```json
{"status":"ok","database":{"reachable":true,"backend":"Postgres (pooled, 1-10 connections, schema autonomize)"}}
```

Migrations run on startup. There is no separate migrate step and no SQL to
paste — `backend/migrations.py` is the single source of truth for the
schema on both backends, and it records what it applied in
`schema_migrations` so a restart is a no-op rather than a re-run.

## Where to get the connection string

Supabase dashboard → your project → **Project Settings → Database →
Connection string → URI**.

Two ports are offered and the difference matters:

**Port 6543, the transaction pooler.** Use this one. It is IPv4-reachable
from anywhere, and it holds the connection pool on Supabase's side, which
is what you want when the thing connecting is a web service that may run
as several instances. This is the right default for Render, Railway, Fly,
a container, or a laptop behind an IPv4-only network.

**Port 5432, the direct connection.** IPv6-only on the free tier, and it
counts every connection against the project's hard limit. Use it only if
you specifically need session-level Postgres features (Autonomize does
not), and only from a host with IPv6.

If the app starts but hangs on the first query, this is almost always the
answer: something between you and Supabase can't route IPv6, and the fix
is to switch to the 6543 pooler URL rather than to change any code.

## Why `AUTONOMIZE_PG_SCHEMA=autonomize`

Supabase serves the `public` schema over PostgREST. A table sitting in
`public` is therefore readable and writable by anyone holding the
project's anon key — which is a publishable key, meant to ship in
front-end code — unless every table individually has row-level security
turned on with policies that deny it.

A schema that isn't added to the API's exposed-schema list is not
reachable that way at all. For a product whose entire pitch is that it
never sees a student's work, "unreachable by construction" is a materially
better story than "unreachable as long as nine policies stay correct."

Two smaller benefits fall out of the same decision. A Supabase project
often already hosts another app; a dedicated schema means the two can't
collide on a table name like `sessions` or `users`, which are exactly the
names two unrelated apps both want. And removing Autonomize later is one
statement:

```sql
DROP SCHEMA autonomize CASCADE;
```

If the project belongs to Autonomize alone, leave the variable unset —
`public` is the correct answer for a dedicated database, and this document
is not arguing otherwise.

The backend creates the schema on first run if it is missing, and opens
every connection with `search_path` already pointing at it, so no query in
the codebase is schema-qualified and nothing needs rewriting to move.

## What gets created

Nine tables, all in the chosen schema:

`sessions` holds one row per tracked browsing session with its aggregate
counters. `user_baseline` holds each user's own EMA mean and variance, per
category — this is the "compared against yourself, never a cohort" part of
the design, and it is per `(user_id, category)` because a 70 in a graded
quiz and a 70 in a Google Doc are not the same claim. `nudge_events` and
`bandit_state` hold the contextual bandit's decisions and its learned
per-arm matrices. `session_labels` holds self-reported comprehension
ratings. `users`, `auth_sessions` and `audit_log` hold first-party
accounts, revocable sessions and the security audit trail.
`schema_migrations` records which migrations have run.

Every table has row-level security enabled with no policies. That is the
intended end state, not an unfinished one: the backend connects as the
owning role, which is not subject to RLS, while the `anon` and
`authenticated` roles have neither grants nor policies and so get nothing.
If a future feature ever does want to read these tables with the anon key,
it will fail loudly and need a policy written on purpose — which is the
behaviour you want from a database holding this kind of data.

## Supabase Auth

Not used by default, deliberately. Autonomize has its own accounts —
scrypt password hashing, server-side revocable sessions, an audit log,
lockout on repeated failures (`backend/accounts.py`, `backend/passwords.py`).
Running Supabase Auth alongside it would mean two identity systems in one
codebase, and "which one is authoritative" is a question you then have to
answer in every new feature.

The optional Supabase JWT path does still exist (`backend/auth.py`, plus
`VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` in the dashboard) for
anyone who wants Google sign-in and email OTP without building them. Set
`SUPABASE_JWT_SECRET` to turn it on. Left unset, none of that code runs.

## Loading demo data (and proving the connection at the same time)

```bash
cd backend
./seed_supabase.sh
```

That starts a temporary API on port 8799, waits for it to actually answer
rather than sleeping a guessed number of seconds, writes 20 weeks of
realistic history, reads the score back through the scoring path, and
shuts the server down again.

Do this before anything else, because a seed that succeeds *is* the
connection test. It exercises the connection string, the pooler port, the
schema, the migrations and the scoring pipeline in one command. If it
prints a score at the end, Supabase is genuinely wired up; nothing else
needs checking.

It writes through the HTTP API rather than straight into Postgres on
purpose. The independence score is computed in the request handler on the
way in, so a direct-to-database seed would mean a second copy of the
scoring logic — and two copies drift.

To populate your own extension's account rather than the built-in demo
user, pass its id through (`seed_demo.py` prints where to find it):

```bash
./seed_supabase.sh --user-id <your-extension-id>
```

Afterwards the rows are visible in the Supabase **Table Editor** — but
only if you switch the schema dropdown at the top of that page from
`public` to `autonomize`. An empty-looking Table Editor is nearly always
this, not a failed seed.

## Verifying it rather than assuming it

```bash
# 1. The backend can reach the database at all
curl -s localhost:8787/api/health | python3 -m json.tool

# 2. A write actually lands, and comes back scored
curl -s -X POST localhost:8787/api/session/upsert \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"check-1","session_id":"check-1","category":"writing",
       "domain":"docs.google.com","path":"/d/x",
       "started_at":'"$(date +%s000)"',"active_ms":1500000,
       "metrics":{"typed_chars":900,"pasted_chars":120,"backspace_count":60,
                  "revision_count":3,"prompt_count":0,"likely_ai_pastes":0,
                  "tab_switch_count":0},"is_final":true}'

# 3. And is readable back through the scoring path
curl -s "localhost:8787/api/score?user_id=check-1" | python3 -m json.tool
```

If you want the test suite itself to run against Postgres rather than only
SQLite, point `TEST_DATABASE_URL` at a throwaway database and run pytest —
the dual-backend tests stop skipping and execute for real:

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres pytest -q
```

Do not point that at your Supabase project. Those tests create and drop
schemas.
