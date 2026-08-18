#!/usr/bin/env bash
#
# Loads 20 weeks of demo data into whatever database DATABASE_URL points
# at — normally a Supabase project.
#
#   cd backend
#   ./seed_supabase.sh                      # reads backend/.env
#   ./seed_supabase.sh --user-id <your-id>  # seed your own extension's id
#
# Why a script rather than three commands in the README: the seed talks to
# the HTTP API, not to the database, because the independence score is
# computed in the request handler (main.py) on the way in. Writing rows
# straight into Postgres would mean reimplementing the scoring pipeline in
# a second place, which is how the seed and the product drift apart. So a
# server has to be running, and that means two terminals — unless
# something starts one, waits for it to actually answer, seeds, and cleans
# up after itself. That is all this does.
#
# Seeding through the real connection is also the honest connection test.
# It exercises the connection string, the pooler port, the schema, the
# migrations and the scoring path in one go; if it prints a score at the
# end, Supabase is genuinely wired up.
set -euo pipefail

cd "$(dirname "$0")"

PORT="${AUTONOMIZE_SEED_PORT:-8799}"   # not 8787: don't collide with a dev server
LOG="$(mktemp -t autonomize-seed-XXXXXX.log)"
SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# .env is loaded by the app itself (_env.py), but this script needs to see
# DATABASE_URL too so it can refuse early with a useful message rather than
# silently seeding a local SQLite file and reporting success.
if [[ -f .env ]]; then
  set -a; . ./.env; set +a
fi

if [[ -z "${DATABASE_URL:-}${SUPABASE_DB_URL:-}" ]]; then
  cat >&2 <<'ERR'
DATABASE_URL is not set, so this would seed the local SQLite file instead
of Supabase — and would look like it worked.

Copy backend/.env.example to backend/.env and fill in:

  DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
  AUTONOMIZE_PG_SCHEMA=autonomize

Get the URI from: Supabase dashboard -> Project Settings -> Database ->
Connection string -> URI. Use the pooler (port 6543); the direct
connection on 5432 is IPv6-only on the free tier and will simply hang from
most networks.
ERR
  exit 2
fi

echo "starting a temporary API on port $PORT ..."
python3 -m uvicorn main:app --port "$PORT" --log-level warning > "$LOG" 2>&1 &
SERVER_PID=$!

# Poll for readiness instead of sleeping a guessed number of seconds: the
# first connection to a cold Supabase project can take a while, and a
# fixed sleep either wastes time or fails intermittently.
echo -n "waiting for the database"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
    echo " — up"
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo
    echo "the API exited before it was ready:" >&2
    cat "$LOG" >&2
    exit 1
  fi
  echo -n "."
  sleep 1
done

if ! curl -fsS "http://127.0.0.1:$PORT/api/health" > /dev/null 2>&1; then
  echo
  echo "gave up waiting. Last output from the server:" >&2
  cat "$LOG" >&2
  echo >&2
  echo "A hang here is almost always the connection string: the 5432 direct" >&2
  echo "connection is IPv6-only on Supabase's free tier. Use the 6543 pooler URI." >&2
  exit 1
fi

echo "connected to: $(curl -fsS "http://127.0.0.1:$PORT/api/health" | python3 -c 'import json,sys; print(json.load(sys.stdin)["database"]["backend"])')"

python3 seed_demo.py --backend "http://127.0.0.1:$PORT" "$@"

# Mirror seed_demo.py's default so the read-back below checks the same
# user that was just written, whether or not --user-id was passed.
USER_ID="demo-student-preview"
prev=""
for arg in "$@"; do
  [[ "$prev" == "--user-id" ]] && USER_ID="$arg"
  [[ "$arg" == --user-id=* ]] && USER_ID="${arg#--user-id=}"
  prev="$arg"
done

echo
echo "reading it back through the scoring path:"
# Written without f-strings: this is inside single quotes inside a shell
# script, and an f-string expression cannot contain a backslash-escaped
# quote, so the obvious version is a syntax error rather than a style
# choice.
curl -fsS "http://127.0.0.1:$PORT/api/score?user_id=$USER_ID" \
  | python3 -c 'import json, sys
d = json.load(sys.stdin)
print("  score", d["current_score"], " baseline", d["baseline_mean"],
      "", len(d["trend"]), "days of trend,", d["streak_days"], "day streak")'

echo
echo "Done. Supabase now holds this data — check the Table Editor, and"
echo "remember to switch the schema selector from 'public' to 'autonomize'."
