# Autonomize

Measures whether your independent thinking is growing or eroding from AI
over-reliance — scored against **your own historical baseline**, not a
population average. A Chrome extension for capture, a FastAPI service for
scoring and policy, and a full web dashboard — plain HTML/CSS/JS, no build
step — for the analysis surface.

**No text you type or paste is ever read, stored, or transmitted** — only
counts, lengths and timings. That constraint shapes everything below.

### ▶ [See the dashboard running](https://theritik08.github.io/AUTONOMIZE/demo.html)

Real data from a seeded backend, frozen at build time — no install, no
sign-up, no backend to start. (Enable it once at **Settings → Pages →
Source: GitHub Actions**; the workflow in `.github/workflows/pages.yml`
publishes it on every push to `main`.)

![The Autonomize dashboard](docs/screenshots/dashboard-light.png)

<table>
<tr>
<td width="50%"><img src="docs/screenshots/dashboard-dark.png" alt="Dashboard in dark mode" /></td>
<td width="50%"><img src="docs/screenshots/popup.png" alt="The extension popup" /></td>
</tr>
<tr>
<td align="center"><em>Dark mode — one token set, no second stylesheet</em></td>
<td align="center"><em>The 320px popup: a glance, not a dashboard</em></td>
</tr>
</table>

| | |
|---|---|
| **Stack** | Chrome MV3 extension (vanilla JS) · FastAPI + Python 3.12 · dashboard in plain HTML/CSS/JS (no bundler, no npm dependencies) · SQLite / PostgreSQL |
| **Tests** | 347 backend (pytest, run against SQLite *and* real Postgres) · extension unit tests (`node --test`) · end-to-end (Playwright, incl. the real unpacked extension) |
| **ML** | Gradient-boosted trees + ridge trained on the session history · per-user EMA baselines · typing-rhythm composing-vs-transcribing signal · conformal (distribution-free) flagging and prediction intervals · LinUCB contextual bandit — all pure Python, no numpy, no scikit-learn |
| **Evaluated** | The bandit is measured against a known optimum: 48.8 cumulative regret vs 600.6 for random over 2,000 decisions (`backend/simulate_bandit.py`). The *scoring* model is **not** validated — see "Are the scoring weights actually right?" |
| **Run it** | **[QUICKSTART.md](QUICKSTART.md)** — or open `docs/demo.html` in a browser to see the whole dashboard with zero install |
| **Design** | **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — the diagram, and why each boundary sits where it does |
| **Machine learning** | **[docs/ML.md](docs/ML.md)** — what is learned, the scikit-learn benchmark, and every caveat |

## Architecture at a glance

Three tiers, each doing one job:

```
dashboard-web/         THE dashboard — plain HTML/CSS/JS, no build step, no npm install, no bundler.
                       index.html · style.css · autonomize-api.js (transport + auth client) ·
                       script.js (charts, gauge, calendar, theme) · app.js (auth gate, routing,
                       settings, SSE). Loaded in that script order.
extension/             the Chrome MV3 extension — content script, background worker, popup.
                       A telemetry collector only: it ships no dashboard and has no options page.
backend/               FastAPI scoring + policy service (SQLite by default, Postgres/Supabase optional)
e2e/                   Playwright suites driving the real dashboard and the real unpacked extension
```

Inside `backend/`, one module per concern:

```
main.py         HTTP routes and request/response shapes — no business logic
db.py           queries + connection handling (SQLite file, or a pooled Postgres)
migrations.py   versioned, idempotent schema changes for both backends
scoring.py      per-session independence score + each user's own EMA baseline
anomaly.py      is this session unusual FOR THIS PERSON? plus trend forecasting
bandit.py       LinUCB contextual bandit — pure linear algebra, no dependencies
nudge.py        what the bandit's arms mean, and where its reward signal comes from
accounts.py     first-party accounts, roles, revocable sessions, audit log
passwords.py    scrypt hashing + password policy (stdlib only, no deps)
cohort.py       aggregate-only institution view — enforces the privacy floor
auth.py         optional Supabase JWT verification
ratelimit.py    write-endpoint token bucket
fit_weights.py  offline: do the hand-tuned scoring weights match real outcomes?
```

**Why a popup *and* a full dashboard, not just one?** A Manifest V3 popup is
capped at roughly 800×600 and disappears the instant it loses focus — it's
built for a glance, not for a day-by-day chart, a risk table, and a
settings form with a live connection test. Cramming all of that into 320px
would mean either a cramped UI or an oversized popup that fights Chrome's
own chrome. So the popup stays a genuinely minimal "pulse check" (score,
this-week ratio, streak, an exam-risk chip) with one clear action — **Open
full dashboard** — which opens the web dashboard in an ordinary browser
tab with no size constraint, backed by the same API. The popup's CSS
carries a literal, hand-synced subset of the dashboard's tokens rather
than loading a stylesheet built for a full page into a 320px surface — see
the comment at the top of `extension/popup.css`.

**There is exactly one dashboard**, and the extension does not contain it.
It used to: a built React app was committed at `extension/dashboard/`,
generated from a `dashboard-app/` source tree, with a third stale copy at
`extension/dashboard-web/`. Three copies meant two settings screens, two
auth flows and two theme systems, and the embedded copy had neither the
live stream nor most of the account features. All three are deleted;
`dashboard-web/` is the only one, and CI fails if any of those paths comes
back.

Because it no longer ships a dashboard, the extension has **no
`options_page`**. The popup's **Settings** link deep-links into the one
settings UI in the web dashboard (`#/settings/tracking`), and **Open full
dashboard** opens the `dashboardUrl` setting — default
`http://localhost:5599/index.html`, stored server-side alongside
`backendUrl`, so a self-hosted deployment points it at its own origin
without touching the extension.

What that one dashboard carries:

- **Accounts** — login, signup, email OTP, email verification, password
  reset, and Google OAuth when it's configured.
- **Views** — Dashboard, Sessions, Insights, Calendar, a notifications
  menu, the profile menu, and an admin-only **Cohort** view at `#/cohort`
  that renders only for `role=admin`.
- **Settings**, in nine sections: Profile, Preferences & Appearance,
  Tracking, Privacy & Data, Security, Connected Devices, Notifications,
  About, Logout.
- **Analysis panels migrated from the old React dashboard**: the score
  explanation, signal readiness (how far the rhythm and calibration
  signals are through their warm-up), and the model/prediction insight.
- **Light / Dark / System** theme, and **real-time updates over SSE**.

## What it does

- Watches (locally, in your browser) three kinds of pages:
  - **AI assistant / answer sites** — ChatGPT, Claude, Gemini, Copilot,
    Perplexity, Chegg, CourseHero, Brainly, Mathway, Symbolab, Quizlet, etc.
  - **Writing/work surfaces** — Google Docs, Notion, GitHub, code editors,
    and any other page with a substantial text field.
  - **Assessment surfaces (strict mode)** — Google Forms quizzes, Google
    Classroom, Canvas, Blackboard, Moodle, Turnitin, Gradescope, and any
    unlisted page whose title/URL reads like a quiz/exam/assignment attempt.
    This is where college cheating actually happens most, so it's scored
    much harder than ordinary writing — see below.
- Counts (never reads) typed characters, pasted characters, backspaces,
  undo/redo, tab-switch-aways (assessment pages only), and — the key signal —
  pastes that happen within 10 minutes of using an AI/answer site
  ("likely AI-sourced paste").
- Sends only those aggregate counts to a small local backend, which computes
  a 0–100 **independence score** per session and updates your personal
  rolling baseline (EMA mean + variance) and a daily streak — kept
  **separately for "writing" and "assessment"**, since a 70 means something
  very different in a quiz than in a Google Doc.
- The dashboard also charts, day by day, **what you wrote yourself versus
  what was pasted in** — one stacked column per day on a single character
  axis (typed on the baseline, pasted stacked above), drawn straight from
  the counts the independence score is computed from. It's the same
  evidence the score uses, shown unaggregated, so you can see the shape of
  a fortnight rather than only its summary number. Deliberately *not*
  overlaid with the 0–100 score: that would be a dual-axis chart, where the
  crossing point is an artefact of scaling rather than a fact about the
  data. The score keeps its own home in the hero number and the forecast
  line beneath the chart.
- The popup shows: current score vs. your baseline, this
  week's independent-vs-assisted minutes, your streak, and — when any exam/
  quiz/assignment activity has been tracked — a dedicated **Exam &
  Assignment Integrity** panel with a low/medium/high risk badge and a
  breakdown of your last 5 graded sessions (paste volume, AI-linked pastes,
  tab switches, per-session score). Nothing here is sent anywhere beyond
  your own configured backend — see Privacy model below.

## Privacy model (important, also a selling point)

The extension **never reads, stores, or transmits the text you type or
paste** — only counts, lengths, and timings. Pasted text is measured with
`.length` and discarded in the same tick; the string never leaves the page.
Everything is sent only to the backend URL you configure (localhost by
default) — no third party, no cloud, no analytics SDK.

## Run it

> **Just want to see it?** Open **`docs/demo.html`** — the real dashboard,
> real data, one self-contained file, nothing to install. For the working
> system, **[QUICKSTART.md](QUICKSTART.md)** has the tested step-by-step
> (including the one gotcha that makes people think it's broken: the
> extension mints its own user id, so seeding the demo user won't populate
> *your* dashboard).

### 1. Start the backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
uvicorn main:app --port 8787 --reload
```

Health check: `curl http://localhost:8787/api/health` →

```json
{"status":"ok","database":{"reachable":true,"backend":"local SQLite"},"time":...}
```

That endpoint actually issues a query rather than just confirming the
process is up — a service that can't reach its storage reporting `ok` is
how an outage gets past every monitor pointed at it. It answers `degraded`
with `reachable: false` instead.

On startup the service logs exactly how it resolved every piece of
configuration, so a misconfigured `DATABASE_URL` or a rate limit you
thought was on is visible immediately rather than after the first write:

```
2026-08-04 16:19:41,686 INFO     autonomize storage backend: local SQLite
2026-08-04 16:19:41,686 INFO     autonomize applied schema migrations: [1, 2, 3, 4, 5]
2026-08-04 16:19:41,686 INFO     autonomize auth: off (trusting client-supplied user_id)
2026-08-04 16:19:41,686 INFO     autonomize rate limit: off
2026-08-04 16:19:41,686 INFO     autonomize cors allowed origins: ['*']
```

(Real `logging`, not `print()` — severity and timestamps are what make
this readable in a log aggregator rather than a wall of unattributed
stdout. Level via `AUTONOMIZE_LOG_LEVEL`.)

Schema changes go through `backend/migrations.py` (versioned, idempotent,
recorded in a `schema_migrations` table) rather than
`CREATE TABLE IF NOT EXISTS`, so upgrading an existing install — including
one created before that module existed — never needs the database deleted.

Optional — populate a few days of realistic demo data (writing, AI-assistant,
and assessment sessions) so the dashboard isn't staring at an empty state
the first time you look at it: `python3 seed_demo.py` (dev tool only, not
loaded by the extension).

### 2. Serve the dashboard

`dashboard-web/` is a static directory — no build, no npm install, no
bundler — so any file server will do:

```bash
cd dashboard-web
python3 -m http.server 5599 --bind 127.0.0.1
```

Then open <http://127.0.0.1:5599/index.html> and sign in or sign up. (Not
as `file://` — a file page has a `null` origin and Chrome blocks every API
call from it.) You do **not** need Node to run any of this.

### 3. Load the extension

1. Open `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the Autonomize icon to your toolbar

### 4. Try it

- Visit ChatGPT/Claude/Gemini and send a couple of prompts.
- Visit Google Docs / Notion / any page with a big text box and type for a
  bit, then paste something you copied from the AI chat tab.
- Click the extension icon for the quick pulse check, or **Open full
  dashboard** for the trend chart, exam-integrity panel, and activity feed.
  If it says the backend is unreachable, make sure `uvicorn` is still
  running on port 8787.
- A fresh install collects under an anonymous device account. To put that
  history on the account you signed in with, click **Link an account** in
  the popup and type the six-character code it shows into **Settings →
  Connected Devices** on the dashboard.

### 5. Working on the dashboard itself

There is no dev server and nothing to build: edit the file, hard-reload
the tab. The static server above serves exactly the artefact that ships,
which is the point — a dev-server transform of the source is one more
thing that can differ from what a user runs.

## Automated tests

Each suite covers a different layer — unit tests on each side of the
stack, plus end-to-end layers that drive real software against a real
backend rather than mocks: the dashboard as the static page it actually
ships as, and the extension as Chrome actually loads it.

```bash
# Backend — scoring formulas, JWT verification, the migration runner, the
# LinUCB maths, personal-baseline anomaly detection, nudge reward
# attribution, the HTTP layer end-to-end, and a round-trip of every db.py
# function. No network required.
cd backend
pip install -r requirements-dev.txt
python3 -m pytest -q

# ...and the same suite with the Postgres half switched on, so the
# dual-backend support is enforced rather than assumed. Any Postgres will
# do; the tests create and drop their own throwaway schemas.
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/postgres python3 -m pytest -q

# Extension unit tests — the telemetry layer as pure functions, run by
# Node's own test runner. No npm install, no test framework dependency.
cd ..                                    # back to the repo root
node --test extension/tests/*.test.js

# End-to-end — Playwright starts the real FastAPI backend, serves
# dashboard-web/ as a plain static directory (the artefact as it ships,
# not a dev-server transform of it), seeds sessions over HTTP (not
# mocked), and drives real software against it. Two projects:
#   dashboard  — the served dashboard: live score data, the recent-activity
#     feed, the exam-integrity panel, the typed-vs-pasted composition
#     chart, Settings navigation, the theme control.
#   extension  — the extension as Chrome loads it, unpacked: manifest
#     validity, the MV3 background service worker, popup.js's connected /
#     offline / sign-in-required states, the offline retry queue, and the
#     paste-correlation-survives-a-worker-restart regression test.
cd e2e
npm install
npx playwright install chromium   # skip this if Chromium is already
                                   # available on your PATH/managed elsewhere
npx playwright test               # or: npx playwright test --project=extension

# The full browser journey — sign-up, email verification, OTP, sign-in,
# password reset, the dashboard itself — driven end to end against a live
# backend and a live static server. See the file header for the exact
# commands it expects to already be running.
node verify-dashboard-web.mjs     # still in e2e/
```

A few things worth knowing about the e2e suites specifically. They run the
backend against its own SQLite file (`backend/e2e-fixture.db`, via a
`AUTONOMIZE_DB_PATH` env var — see `backend/db.py`), wiped before every
run, so they never touch or pollute the `autonomize.db` a real local dev
session reads. Seeded sessions upsert by a fixed `session_id`, matching how
the real extension reports incremental deltas — this is *not* what keeps
re-runs clean, the fresh-database wipe is; re-running without that wipe
would double-count the seeded metrics rather than leave them unchanged.
Backend and dashboard servers run on non-default ports (8799, 5199)
specifically so the suites are safe to run alongside a normal local
`uvicorn --port 8787` / `http.server 5599` session without port collisions.
The extension project launches its own persistent browser context — Chrome
only exposes extensions to one — which is why it lives behind the fixture
in `e2e/fixtures/extension.ts` rather than using Playwright's default
browser.

**These tests were checked against the bugs they claim to catch, not just
watched to go green.** Two were verified by deliberately reintroducing the
defect and confirming the relevant test fails:

- The service-worker restart test was re-run against the original
  in-memory implementation (fails: the count never reaches the backend),
  and against a subtler variant that persists the counter but still reads
  it back from memory (also fails, at the backend assertion rather than
  the storage one) — so both layers of that test are load-bearing, not
  just the shallow one. The test additionally plants a canary global on
  the worker and asserts it's gone after the restart, so it can't pass
  because the restart quietly no-opped.
- The "extension loads" test was re-run with the invalid manifest value
  that this suite originally uncovered (see "A bug this suite caught"
  below) and fails as it should.

This is real, if not exhaustive, coverage — not a committed guarantee
against every regression. The heuristic scoring weights and the Supabase
Postgres path still aren't covered by an automated suite; see "Known MVP
limitations" below for the honest list of what's still manual or missing.

### A bug this suite caught

Building the extension suite immediately surfaced a genuine shipping bug
that code review and manual QA had both missed: `manifest.json` listed
`"chrome://*/*"` under `content_scripts[0].exclude_matches`, and Chrome
rejects the `chrome://` scheme there. The consequence was not a warning —
Chrome refused to load **the entire extension**, reporting only to stderr:

```
Failed to load extension from: .../extension.
Invalid value for 'content_scripts[0].exclude_matches[3]': Invalid scheme.
```

The entry was redundant anyway: Chrome never injects content scripts into
`chrome://` pages regardless of what a manifest asks for. Removing it is
the whole fix, and behaviour is unchanged. Worth stating plainly because
it's the exact failure mode an extension test suite exists for — a
manifest is validated by the browser at load time, silently, so no amount
of reading the JSON catches it.

## Connecting Supabase (optional — swaps the storage backend)

By default the backend uses a local SQLite file (`backend/autonomize.db`)
— zero setup, nothing to sign up for, good for local dev and demos. You
can swap that for a hosted Postgres database (Supabase's free tier is
Postgres) without touching any other part of the system: the extension,
the dashboard, and `seed_demo.py` all only ever talk to the FastAPI
backend over HTTP, never to the database directly, so this is purely a
backend-internal change.

1. Create a project at [supabase.com](https://supabase.com) (free tier is
   plenty for this).
2. In your project: **Project Settings → Database → Connection string →
   URI**. Use the **pooler** connection string (port `6543`, "Transaction"
   mode) unless you have a specific reason to use the direct connection
   (port `5432`) — the pooler handles the many short-lived connections a
   web backend opens much better.
3. Copy `backend/.env.example` to `backend/.env` and paste your connection
   string in as `DATABASE_URL`:
   ```
   DATABASE_URL=postgresql://postgres.xxxx:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:6543/postgres
   AUTONOMIZE_PG_SCHEMA=autonomize
   ```
   That second line is the one worth understanding. Supabase serves the
   `public` schema over PostgREST, so a table left there is reachable with
   the project's anon key — a key designed to ship in front-end code —
   unless every table individually carries a row-level-security policy
   that says no. A schema that isn't exposed to the API isn't reachable
   that way at all. For a product whose whole claim is that it never sees
   a student's work, "unreachable by construction" beats "unreachable as
   long as nine policies stay right". It also stops a name like `sessions`
   or `users` colliding with whatever else lives in that project, and
   makes uninstalling one `DROP SCHEMA autonomize CASCADE`. Leave it unset
   if the database is Autonomize's alone; `public` is correct then.
4. `pip install -r requirements.txt` (now also installs `psycopg`, the
   Postgres driver, and `psycopg_pool` — both only imported at runtime if
   `DATABASE_URL` is actually set, so they cost nothing on the SQLite path).
5. `uvicorn main:app --port 8787 --reload` as usual. On startup you'll see
   `[autonomize] storage backend: Postgres (pooled, 1-10 connections, schema autonomize)`
   in the logs instead of `local SQLite`, followed by the list of schema
   migrations applied. The schema is created if missing and all nine
   tables (`sessions`, `user_baseline`, `nudge_events`, `bandit_state`,
   `session_labels`, `users`, `auth_sessions`, `audit_log`,
   `schema_migrations`) come up on first run. `/api/health` reports the
   schema it actually reached, so a deploy that comes up connected but
   writing somewhere unexpected is visible without a SQL console.

   Full detail, including which port to use and why an IPv6-only direct
   connection is the usual cause of a hang: **[docs/SUPABASE.md](docs/SUPABASE.md)**.
6. Already had local demo/seeded data you want to keep? Run
   `python3 migrate_sqlite_to_postgres.py` once (with `DATABASE_URL` set)
   to copy it over — it's idempotent, so re-running it after collecting
   more local data only copies the new rows.

**Why one `db.py` instead of two backends living in separate files:**
every query is written once, SQLite-style (`?` placeholders), and
`db.q()` swaps them for Postgres's `%s` only when `DATABASE_URL` is set —
duplicating ~150 lines of near-identical query logic across two files
would be a worse trade than one file with one small translation helper.
The one place the two backends' SQL genuinely differs is date extraction
from a millisecond-epoch column (SQLite's `date()` vs. Postgres's
`to_timestamp()`), isolated in `db.date_expr()` rather than inlined at
each call site. The one place their *schema* differs is that ms-epoch
timestamp columns are `BIGINT` on Postgres — plain `INTEGER` there is
32-bit and overflows on a 13-digit epoch value, unlike SQLite's
dynamically-sized `INTEGER`.

**Connection pooling.** SQLite opens a local file, so a connection per
request is fine. Postgres opens a TCP+TLS connection and, on a managed
instance like Supabase, counts against a hard connection limit — a
connection per request is both slow (a handshake on every call) and a way
to exhaust that limit under load. The Postgres path therefore runs through
a `psycopg_pool.ConnectionPool` opened once at startup and closed on
shutdown; bounds are configurable via `AUTONOMIZE_PG_POOL_MIN` /
`AUTONOMIZE_PG_POOL_MAX` (defaults 1 and 10). If `psycopg_pool` isn't
installed the code degrades to per-request connections and says so
explicitly at startup and in `/api/health` rather than refusing to run.

**This is enforced by tests, not just described here.** Every database
test runs against SQLite always and against Postgres whenever
`TEST_DATABASE_URL` is set, including the one genuinely non-portable
fragment (date extraction from a ms-epoch column, which must produce the
same day string on both) and a 13-digit timestamp round-trip that would
overflow a 32-bit `INTEGER` on Postgres only. Migrations, pooling, and the
full endpoint surface were additionally exercised against a real
PostgreSQL 16 instance — including restarting the service against an
already-migrated database to confirm nothing re-runs.

## Adding Supabase Auth (optional — real per-student sign-in)

Independent of the storage swap above: by default every `user_id` is a
random UUID the extension generates on install and every backend request
is trusted to say who it is — fine for a single person on their own
laptop, not fine for a real multi-student deployment (anyone could claim
to be anyone). Turning this on replaces that with real Supabase-issued
identity, verified by the backend on every request, using the *same*
Supabase project as the database connection above (Supabase bundles auth
with the Postgres database — you don't sign up for a second thing).

**What it changes, concretely:**
- The dashboard is gated behind a sign-in screen either way — the auth
  gate in `dashboard-web/app.js` runs before any view renders, and the
  first-party flow (password, email OTP, verification, reset, and Google
  when it's configured) is what a fresh clone gets.
- The extension is unaffected by which identity provider you choose: it
  holds a device account and is attached to a person by the linking code
  flow (popup → **Link an account**, dashboard → **Settings → Connected
  Devices**), never by typing a user id anywhere.
- The backend (`backend/auth.py`) verifies every request's bearer token
  against your project's JWT secret and uses the token's identity, not
  whatever `user_id` the request claims — once this is on, a request
  cannot write into (or read) another student's data by naming their id,
  because the id is no longer taken from the request's word for it.

**Turning it on:**
1. In your Supabase project (the same one from "Connecting Supabase"
   above): **Authentication → Providers**, make sure **Email** is enabled.
   **Authentication → Settings**, uncheck "Confirm email" or leave it on —
   either works, since sign-in here is by code, not a confirmation click.
2. **Project Settings → API → JWT Settings** — copy the **JWT Secret**.
   Add it to `backend/.env` as `SUPABASE_JWT_SECRET` and restart the
   backend. The startup log will now say
   `[autonomize] auth: ON (Supabase Auth — bearer token required)`.

There is nothing to configure on the dashboard side and nothing to
rebuild: `dashboard-web/` ships no Supabase client and talks only to this
backend, so a Supabase access token is simply one more credential
`backend/auth.py` knows how to verify (alongside first-party session
tokens).

Leave either env var unset and that half stays off — you can turn on the
Postgres backend without turning on auth, or (less usefully) the reverse.
Both were built as independent, gracefully-degrading toggles on purpose,
matching the pattern already established for the storage backend.

**Verified, not just written:** the backend's token verification was
tested against real signed JWTs — a valid token is accepted, an expired
one rejected, one signed with the wrong secret rejected, a request with
no token rejected once auth is on, and — the check that actually matters
— a session written while the request body *claimed* a different user_id
than the token's `sub` landed under the token's identity, not the claimed
one, confirming a request can't spoof someone else's data once auth is
on. The dashboard's own gate is covered separately by
`e2e/verify-dashboard-web.mjs`, which drives sign-up, email verification,
OTP, sign-in and password reset in a real browser against a real backend,
reading the codes out of the mail sink the backend actually writes to
rather than stubbing them.

**Worth knowing:** `background.js` is hand-written vanilla JS with no
bundler (see its file header for why), so it runs no identity SDK. It
refreshes its own first-party access token instead — short access tokens,
rotating refresh tokens, refreshes serialised through a single promise so
two concurrent flushes can't present the same rotated token and trip the
server's replay defence, and one retry on a 401. It does not depend on any
page being open to stay authenticated.

## Design system & why it looks the way it does

The dashboard's visual language was deliberately built to mirror the
*structure* of a specific reference admin-dashboard layout (large
rounded cards, a pill nav, small ring stats in the header, a "featured"
dark panel, an accordion, a calendar) while every element inside
that structure carries genuine Autonomize data — no stock photos, no
invented names, no meetings or line items that don't exist for this
product. A few decisions worth defending explicitly:

- **Light-first, not dark-first.** The palette is a warm cream/ink/amber
  system (`--canvas: #F3EFE6`, `--ink: #17181A`, `--amber: #D69A3B`)
  rather than the near-black system this project started with. Dark mode
  is a full, faithful inversion — the same token names re-declared under
  `[data-theme="dark"]`, not a second stylesheet — but light is the
  designed-for first impression now, matching the admin-dashboard
  reference this UI's structure is built on. It's a **three-way choice**,
  not a toggle: Light, Dark, and System, with System following the OS
  only until an explicit choice is made. (`dashboard-web/style.css`, and
  the theme block at the top of `dashboard-web/script.js`, which exposes
  `Autonomize.theme` so the settings screen drives the one implementation
  rather than growing a second.)
- **Color is semantic, not decorative.** Amber is the single brand accent
  (active nav, primary fills, the score ring) and never used to encode
  data. Green/amber/red are reserved for the actual independence signal —
  the gauge, the forecast line, risk badges — and never appear as generic
  UI chrome.
- **Elevation is shadow-based in light mode, luminance-based in dark
  mode** — not one rule forced onto both themes. Soft shadows read clean
  on the cream background the reference uses; on a near-black background
  they'd read muddy, so dark mode keeps the layered-surface + hairline-
  border approach instead (`--shadow-rest` / `--shadow-hover` are
  re-declared per theme).
- **The "featured panel" pattern (`--dark-panel`) always inverts against
  its siblings.** The reference's dark onboarding-checklist card is echoed
  in the exam-integrity panel's "Recent graded sessions" list — an
  intentionally higher-contrast surface for the one panel that carries the
  strict-mode integrity signal.
- **Nothing in the layout is decoration standing in for missing content.**
  The reference's circular time-tracker had a play/pause transport
  control; tracking here is passive and automatic, so a fake start/stop
  button would be dead UI — the Today card shows today's real tracked time
  instead, no controls. The reference's meetings calendar is replaced by
  an activity calendar built from the same session data already on the
  page: click a day and it lists that day's sites with their typed and
  pasted counts. It replaced a deterministic pseudo-random day generator,
  which was the right call for a static mock and exactly wrong once real
  data existed — it would have kept drawing convincing activity for days
  the student never worked. The reference's benefits/compensation list is
  replaced by an accordion of things that are actually true right now:
  backend connection status, which categories are tracked, this week's
  most-active site, and a plain-language privacy summary.
- **Motion is either explanatory or off.** The score count-up and the
  gauge's arc reveal exist to confirm "the app noticed a change" — they're
  not there to look busy. Cards enter with a scroll-triggered reveal (an
  `IntersectionObserver`, so a card below the fold doesn't finish
  animating unseen). Every animation respects `prefers-reduced-motion` —
  in CSS *and* in the script that drives the count-up and the gauge, which
  reads the media query itself rather than trusting CSS alone.
- **No CSS framework, and no build step.** One hand-written stylesheet
  over Tailwind/MUI/Bootstrap was a deliberate call, not an oversight —
  utility-class soup is exactly what gives AI-generated and template
  dashboards their recognizable fingerprint, and a design-token system
  gives the same consistency guarantees with full control over the visual
  language. It also means the file a reviewer opens is the file the
  browser runs: no bundler, no source maps, nothing to regenerate.

## How the score is computed (backend/scoring.py)

**Writing mode** (Docs, Notion, code editors, generic text fields):

```
typed_ratio      = typed_chars / (typed_chars + pasted_chars)
engagement       = min(1, (backspaces + revisions) / (typed_chars / 50))   # capped
ai_correlation   = min(1, sqrt(likely_ai_pastes) / 3)

score = 100*typed_ratio + 12*engagement - 22*ai_correlation   (clamped 0–100)
```

**Assessment mode — strict** (quizzes, exams, graded assignment portals):

```
typed_ratio      = typed_chars / (typed_chars + pasted_chars)
paste_ratio      = pasted_chars / (typed_chars + pasted_chars)
ai_correlation   = min(1, sqrt(likely_ai_pastes) / 2)       # saturates faster than writing mode
tab_penalty      = min(20, max(0, tab_switch_count - 2) * 3) # first 2 switches are free

score = 100*typed_ratio - 40*paste_ratio - 45*ai_correlation - tab_penalty   (clamped 0–100)
```

The differences are deliberate: in a graded context, *any* paste counts
against you (not just AI-correlated ones), the AI-correlation penalty is
roughly double, and repeatedly leaving the tab during the attempt costs
points too. A session scoring <40 is labelled "high risk", 40–70 "medium",
70+ "low" (`scoring.risk_level`).

Your personal baseline is an EMA (`alpha = 0.25`) of your own past scores,
**kept separately per category** — a writing baseline and an assessment
baseline never mix — so "70" means something different for two different
people (or for the same person in two different contexts); what matters is
the delta against *your own* mean, and whether that mean is trending up or
down over weeks.

Writing sessions shorter than 20 active seconds, or assessment sessions
shorter than 10 active seconds, or any session with zero typed+pasted
characters, aren't scored (too noisy to mean anything). AI-assistant/
answer-site sessions are never scored — they only count toward the weekly
"assisted minutes" total and the AI-activity timestamp used for paste
correlation.

## The composition chart: what you wrote vs. what was pasted

`/api/score` returns a `composition_trend` — per day, the characters typed
and the characters pasted across all work sessions — which the dashboard
draws as two lines.

Three decisions worth naming, because each could reasonably have gone the
other way:

**One shared y-axis, always.** Giving each series its own scale is the
default in a lot of charting libraries and it would quietly destroy the
entire point: a day of 50 typed and 5000 pasted characters would render as
two similar-looking lines. The renderer derives one peak from the maximum
across *both* series and scales both against it — in the stacked-bar view
and the two-line view alike.

**Writing *and* assessment sessions, but never AI-assistant ones.** The
score `trend` above is writing-only, because writing and assessment use
different formulas and averaging their scores would be meaningless. Raw
character counts have no such problem — a typed character is a typed
character — and excluding assessment would blank out exam days, which are
exactly the days worth looking at. AI-assistant sessions are excluded on
the opposite reasoning: characters typed into ChatGPT are prompts, not
work you produced, and counting them as "what I wrote myself" would be
actively misleading.

**Two views of the same numbers, both labelled.** Stacked bars answer "how
much of that day was mine?" at a glance; the two-line view answers "which
way is this going?" over the fortnight. Both carry a legend with the
running totals and the percentage that was yours, and both label the
series in the tooltip rather than relying on colour alone to say which
line is which.

## Accounts, roles, and the institution view

Two ways in, converging on one identity:

- **First-party email + password** (`backend/accounts.py`) — works from a
  fresh clone with no external service. scrypt hashing via the standard
  library, so no new dependency.
- **Supabase** (optional) — adds Google and email-OTP without running an
  identity provider. See "Adding Supabase Auth" below.

```bash
python3 make_admin.py dean@university.edu     # promote to institution
python3 make_admin.py dean@university.edu --demote
```

Promotion is a **server-side CLI, never an HTTP route**. Any endpoint that
can grant admin is an endpoint worth attacking, and whoever can run this
already has the database. The sign-in panel's Student/Institution toggle
changes copy and routing only — there is a test asserting that
`POST /api/auth/register` with `"role": "admin"` in the body still returns
a student.

### What the institution account can and cannot see

It sees a cohort. It cannot see a student. `backend/cohort.py` enforces
two rules, both in SQL rather than in the UI:

1. **A minimum cohort size (5).** Below that, every statistic is withheld
   — with four students, "the average is 62" plus three people comparing
   notes identifies the fourth. The floor counts students who actually
   *contributed data*, not students who have accounts, so creating four
   empty accounts can't unlock the fifth student's numbers. Individual
   days are suppressed on the same rule, and the number of suppressed days
   is reported rather than silently dropped.
2. **No caller-supplied filtering.** This is the one people miss. An
   aggregate view that lets the caller narrow the population — by class,
   by date, by score band — is not aggregate-only: narrow far enough and
   the aggregate *is* one student. `/api/admin/cohort` takes no
   parameters at all, and a test asserts its signature stays that way.

There is no student list, no search, and no drill-down — not deferred,
but absent by construction: no route exists that would return them. A test
asserts no user id or email appears anywhere in the response, and another
pins the response shape to an allow-list so a future per-student field
fails CI rather than shipping.

**What this still doesn't defend against, stated plainly:** an admin who
queries repeatedly over time can learn things by differencing — if the
mean moves the day after one student joins, that student's score is
recoverable. Defending against that properly needs differential privacy
(calibrated noise and a privacy budget), which is real work and is not
implemented. A fixed unfilterable population, a size floor, and coarse
buckets raise the cost without eliminating the attack.

### Security posture

Implemented: scrypt hashing; identical error *and* status for "wrong
password" and "no such account" (any difference is a user-enumeration
oracle, and a test asserts they match); server-side revocable sessions, so
signing out kills a token that is still cryptographically valid;
per-account lockout with exponential backoff, deliberately temporary
because a permanent lock turns an attack on your account into a denial of
service against you; auth endpoints rate-limited unconditionally, even
when the general limiter is off; IP addresses hashed, never stored raw; an
append-only audit log including every cohort view; roles re-read from the
database on every request, so a demotion takes effect immediately rather
than at token expiry.

Not implemented, and named rather than implied: email-verification
delivery (no mail transport is configured — the flag exists and is
honoured, but nothing sends the mail), MFA/TOTP, device binding, and
breached-password corpus checks (a network call, so a deployment decision).

## Typing rhythm: the case a paste counter cannot catch

`typed_ratio` carries most of the score, and it has one large hole: **a student
who reads an AI answer and types it out scores 100.** Every character genuinely
came from their keyboard, and because there is no paste, the AI-correlation
signal never fires either.

What that student cannot easily fake is the *shape* of their typing. Composing is
irregular — bursts of a phrase, a pause while the next clause is worked out,
backtracking to fix the last one. Transcribing is metronomic: the text already
exists and the hands are a buffer.

**The privacy constraint shapes the whole design.** A raw sequence of
inter-keystroke intervals is *not* safe to collect — keystroke-timing inference is
a well-established side channel, and an ordered series leaks information about
the characters that produced it. So the extension ships an eight-bucket
**histogram** and never a series: bucketing destroys the ordering, and the
ordering is what carries the content. Two sessions with identical bucket counts
are indistinguishable regardless of what was typed.

The cost is named rather than hidden in `backend/rhythm.py`: order-dependent
features — whether pauses land at clause boundaries, whether revision follows
bursts — are unavailable, and they are probably the strongest signals. They are
also the ones that leak.

`regularity_index` is then never used as an absolute threshold. It is compared to
an EMA of *that user's own* regularity in that category, with the same guards as
the score comparison (5-observation gate, standard-deviation floor, one-sided).
Some people simply type evenly; the question asked is not "is this regular?" but
"is this far more regular than how this person normally writes?"

Measured end-to-end, two sessions with identical character counts:

```
composed      : regularity 0.091   score 100.0
typed-out-AI  : regularity 0.742   score  85.0
```

The weight (15 writing, 25 assessment) is provisional in the same sense as every
other constant in `scoring.py`, and `regularity` is in `fit_weights.py`'s feature
set so it faces the same validation as the rest.

## Conformal prediction: a flag rate you can actually promise

Anomaly detection used to flag at 1.5 and 2.5 standard deviations below a user's
own mean. Those numbers borrow the intuition of a normal distribution, and the
independence score is not normally distributed — bounded on [0, 100], built from
a ratio, with its variance compressed near the ceiling where a diligent student
sits. The only defensible claim was that the thresholds were *ordinal*.

`backend/conformal.py` ranks instead of measuring distance. Against a window of
the user's own past scores, the conformal p-value is

```
p = (1 + #{i : Rᵢ ≥ R_new}) / (n + 1)
```

Under exchangeability this bounds the false-flag rate at α for **any**
distribution at **finite** sample size. α = 0.05 means at most 5% of a student's
ordinary sessions are flagged — by construction, not by assumption. The suite
checks that empirically across a normal, a heavy-tailed and a bounded-skewed
distribution.

The assumption that remains is stated rather than hidden: a score series drifts,
and a drifting series is not exchangeable. Two things narrow the gap and neither
closes it — the window is recent and bounded, and nonconformity is a residual
against the EMA mean rather than the raw score. The claim is *a calibrated flag
rate under local exchangeability*, not a clean i.i.d. guarantee.

The z-score is kept, because the two answer different questions:

| | role | question |
|---|---|---|
| z-score | magnitude | "how far below your norm, in your units" |
| conformal | decision | "is this rare enough to say something about" |

This also fixes a case the old scheme got wrong: a user whose scores genuinely
range over 40 points has a large σ, so a 20-point drop is unremarkable *for them*
— but it is also a large absolute gap. Ranking against their own history
correctly leaves it alone.

## Anomaly detection: fixing a contradiction in the original design
### (backend/anomaly.py)

This project's stated premise, repeated throughout this README, is that
every judgement is made **against your own baseline, never a population
average**. The per-session score honoured that. `risk_level()` did not: it
compared a raw score against fixed cutoffs (70 / 40) identical for
everybody. A meticulous student who normally scores 95 could drop to 72 —
a large personal deviation — and be labelled "low risk", while someone who
habitually works at 45 gets labelled "medium" on a completely ordinary day
for them. That is precisely the population-norm comparison the design set
out to avoid, sitting in the middle of the codebase.

Fixing it needed a measure of spread, not just a mean. It turned out one
was already there: `scoring.update_baseline` has computed an EMA variance
since the first version, written it to the database, and **nothing had ever
read it**. `anomaly.py` is what reads it.

```
std_dev  = max(4.0, sqrt(ema_var))          # floor: a near-constant history
                                            # shouldn't make a 1-point wobble
                                            # look like 30 sigma
z        = (score - ema_mean) / std_dev

z <= -2.5  -> high      (well outside your own norm)
z <= -1.5  -> medium
otherwise  -> low
```

Three deliberate constraints on that:

- **Only downward deviations are flagged.** Scoring far above your own
  baseline is a good day, not an integrity signal.
- **Nothing is flagged below 5 observations.** A variance over two sessions
  is whatever those two sessions happened to do; "3.2 sigma" computed from
  it is noise wearing a lab coat. Under the threshold the API reports
  `status: "insufficient_data"` and falls back to the absolute thresholds.
- **The absolute signal is kept, not replaced.** The two answer different
  questions — *"was this session mostly pasted?"* versus *"is this unlike
  how this person normally works?"* — and a student who pastes everything
  every time has a stable low baseline where nothing is ever anomalous. The
  API returns both, reports which one produced the shown level
  (`risk_driver`), and takes whichever is more serious.

`/api/score` also now returns a `forecast`: ordinary least squares over the
daily trend with the projection, direction, and an `r2` so the UI can
decline to draw a line through data it doesn't describe. Deliberately the
simplest model that answers "which way is this heading" — at tens of noisy
daily points per user, a heavier model buys more confident-looking numbers
rather than more correct ones.

## The contextual bandit: when to nudge
### (backend/bandit.py, backend/nudge.py)

The original blueprint's second half — deferred in the first release,
implemented here. `bandit.py` is the algorithm; `nudge.py` is everything
domain-shaped.

**Algorithm.** LinUCB with disjoint linear models. Each arm keeps
`A = I + Σxxᵀ` and `b = Σrx`, and scores a context as
`θ·x + α·sqrt(xᵀA⁻¹x)` where `θ = A⁻¹b` — predicted reward plus an
uncertainty bonus that shrinks as the arm accumulates evidence *in that
direction of feature space*. Arms: `none`, `reflect`, `pause`, `contrast`.
Context: 7 normalised features (score, delta vs. own baseline, AI-assisted
share of the week, time of day, streak, nudge fatigue, bias).

**Why LinUCB rather than something bigger.** One user produces a handful of
decisions a day over a 7-feature context. That is a small-data regime.
LinUCB is closed-form (no training loop, no hyperparameter search), updates
in O(d²), and — the part that matters most here — is fully inspectable:
`/api/nudge/decide` returns every arm's expected reward and exploration
bonus separately, so any decision can be explained rather than asserted. A
neural policy would be worse on all three counts at this scale. There is no
numpy: `d = 7`, and a 7×7 Gauss-Jordan inversion is microseconds of pure
Python, which keeps `pip install -r requirements.txt` at FastAPI + pydantic.

**Where the reward comes from — the part that took the most thought.** The
tempting design is "reward = 1 if the student tapped Accept". Optimising
that yields a model that learns to generate *agreeable pop-ups*, which is
not the goal. Worse, the most important arm is `none` — not interrupting —
and an explicit-feedback-only design can never learn about it, because
there's no pop-up to accept. A bandit that structurally cannot evaluate
"leave them alone" will always over-nudge.

So rewards arrive two ways, and every arm can earn them both ways:

1. **Explicit** — the client reports what happened
   (`POST /api/nudge/feedback`). Immediate but sparse, and impossible for
   `none`.
2. **Outcome attribution** — when the student's next session is scored
   within 2 hours of an unsettled decision, that score decides the reward:
   did the work that followed beat their own baseline? This makes `none`
   learnable on the same footing as every other arm, and it measures what
   the product actually cares about instead of what's easy to collect.

Explicit feedback wins when both are available (a direct observation beats
an inference). Decisions that get neither inside the window settle as
`expired` with a neutral reward, so an abandoned tab doesn't quietly bias
the model toward whichever arm happened to be playing. Attribution compares
against the baseline as it stood *before* the session folded into it —
otherwise a session is being judged against a mean it already moved.

**Not yet wired to the student.** The extension does not call these
endpoints; the popup stays display-only, exactly as documented before. The
measurement and policy machinery is built and tested; the client surface is
the deliberate next step, and pretending otherwise would be the easiest way
to mislead someone reading this.

## Learning from the database instead of a formula

The scoring weights are hand-set, and they cannot be learned, because
learning needs labels — "did this student actually understand the work?" —
and nobody has collected any.

But there is a second supervised problem in the same table that needs **no
human labelling at all**:

> given how a student has been working, where is their independence heading?

**The label is the next row.** The database supervises itself. That makes it an
ordinary regression problem with thousands of training examples available the
moment anyone uses the product, and it replaces `anomaly.forecast` — a straight
line fitted through the last few daily averages — with a model that learned the
shape of real behaviour instead of assuming it is linear.

### The pipeline

Everything lives in the `backend/ml/` package. Full write-up, including the
head-to-head benchmark against scikit-learn and every caveat, is in
[docs/ML.md](docs/ML.md).

| File | Role |
|---|---|
| `ml/features.py` | 18 strictly-causal features per row, from that user's prior sessions only |
| `ml/models.py` | Histogram-based gradient-boosted trees + ridge, pure Python |
| `ml/isolation.py` | Isolation forest over within-user deviation vectors — the only multivariate signal |
| `ml/coldstart.py` | Empirical-Bayes population prior, with the borrowed share stated out loud |
| `ml/explain.py` | Permutation importance (global) + exact linear attribution (local) |
| `ml/validation.py` | Six leakage guards that raise, run before any model is fitted |
| `ml/evaluation.py` | Metrics, free baselines, conformal radius and measured coverage |
| `ml/training.py` | Time-ordered split, baselines first, refuses below `--min-rows` |
| `ml/inference.py` | Loads the model and serves; returns `None` rather than guessing |
| `ml/registry.py` | Versioned model files; refuses an incompatible one |
| `ml/manifest.py` | Reproducibility record — seed, fingerprints, versions, `synthetic` flag |
| `simulate_history.py` | Synthetic multi-student history so the pipeline can be exercised |

Training is offline and serving reads a JSON file, so the request path never
imports the learner and **no new dependency was added**. The gradient-boosting
loop is thirty lines and the tree builder a hundred; `scikit-learn` plus numpy
and scipy is roughly 100 MB to fit a model on a few thousand rows of eighteen
features, and a `.fit()` call demonstrates a library was installed rather than
that the method is understood.

### What the evaluation actually says

Measured on simulated history (60 students, 120 days, 4,161 training rows),
held out by **time** — the last 20% chronologically, never a random split, since
a random split of a time series lets the model train on February and be tested
on January:

```
baselines on the held-out slice (no model, free):
  predict last score                     MAE   8.73   R² +0.664
  predict their EMA (current behaviour)  MAE   6.11   R² +0.828
  predict 7-session mean                 MAE   6.35   R² +0.811

learned models:
  ridge regression                       MAE   5.76   R² +0.861
  boosted trees (from the mean)          MAE   5.93   R² +0.857
  boosted trees (correcting the EMA)     MAE   5.83   R² +0.855

best model: ridge regression -> 5.8% lower MAE than the best free baseline
conformal prediction interval: ±11.9 points at 90% coverage
```

Three things in there are worth more than the headline number.

**The horizon changes the problem.** Predicting the *very next* session, the
learned model beats the EMA by under 1% — a single session is dominated by its
own circumstances, so most of its variance is irreducible and a student's own
running average is already near-optimal. Averaging over the next five cancels
that noise and leaves the drift, which is both what the product actually asks
and the part an EMA is structurally bad at, having no mechanism for
extrapolating a trend. The default horizon is 5 for that reason.

**The simpler model won, and it ships.** Gradient-boosted trees are implemented,
evaluated and beaten by ridge regression on these features. The trainer picks on
the held-out slice and takes the simpler model on a tie, because capacity that
buys nothing is a liability rather than a neutral. Reporting that the fancier
method lost is the point of running both.

**Boosting from the right starting point matters.** Starting the trees at a
global mean makes them spend their first rounds rediscovering that a student's
score is near their own average. Starting from the EMA feature instead — an
offset, `base_margin` in the boosting literature — means every tree is spent on
what the baseline gets *wrong*. On a strongly autocorrelated series that is the
difference between competing with the existing formula and correcting it.

### What it predicts, and what it does not

It predicts **behaviour**, not **understanding**. The forecast target is the same
constructed 0–100 score as ever, so a model that predicted it perfectly would
still say nothing about whether the construct is valid. That question is
unchanged and still needs the study.

`GET /api/score` returns `prediction` alongside the existing `forecast`, with a
`source` field, and `prediction` is `null` whenever there is no trained model or
too little history — the client keeps using the straight line, which is what the
model exists to improve on. A learned model that silently degrades to guessing
when its inputs are missing is worse than no model, because the number it emits
is indistinguishable from a good one.

```bash
cd backend
python3 seed_question_bank.py   # the retrieval question bank (demo concepts)
python3 simulate_history.py     # synthetic history, clearly labelled as such
python3 train_model.py          # trains, evaluates, writes model.json
python3 train_model.py --dry-run   # evaluate without writing anything
```

On the demo data this chooses ridge regression at **MAE 5.76** against the
best free baseline's **6.11** — a 5.8% improvement, reported rather than
inflated. If the model had failed to beat the baseline, the pipeline would
have written nothing and said so: "the free baseline wins" is a result, not
a bug.

`scikit-learn` is not a runtime dependency. It is in `requirements-ml.txt`,
dev and CI only, where it serves as the oracle the hand-written learners are
checked against — `RidgeRegression` matches `sklearn.linear_model.Ridge` to
1e-6 on both predictions and coefficients. The benchmark that decided this
is in [docs/ML.md](docs/ML.md); the short version is that on this data the
hand-written ridge (5.756) ties scikit-learn's best (5.755) and every tree
method loses, so ~100 MB of compiled dependencies on the serving path would
buy 0.001 MAE.

`model.json` is gitignored: it is a build artefact, specific to the data it was
trained on, and a model fitted on simulated history must never be mistaken for
one fitted on real students.

## Evaluating the bandit without a single student

The scoring model cannot be validated without labels — that is the honest gap,
and `fit_weights.py` below is the instrument waiting on it.

The bandit is different, and `backend/simulate_bandit.py` exploits the
difference. A contextual bandit can be evaluated with **no human subjects at
all**, because what is being measured is the algorithm's ability to find a good
policy, not a claim about students. Define a ground-truth reward, run the policy
against it, and measure how much reward it left on the table versus an oracle
that knew the truth from the start.

```
$ python3 simulate_bandit.py --rounds 2000 --runs 20 --ablate-none --sweep-alpha

  LinUCB (a=1.0)    final regret     48.8   per round 0.0244
  LinUCB (a=0.25)   final regret     12.7   per round 0.0064
  Thompson (v=1.0)  final regret    142.6   per round 0.0713
  e-greedy (e=0.1)  final regret     88.0   per round 0.0440
  Random            final regret    600.6   per round 0.3003
```

Two findings matter more than the headline.

**Removing the `none` arm costs 15.4× more regret** (752 vs 49). Making
"don't interrupt" a first-class arm was previously defended on theory — that a
policy which can only learn from pop-up feedback can never reward staying quiet.
It now has a number.

**α = 1.0 is not optimal in simulation** — α = 0.1 gives 10.0 against 48.8,
monotonically worsening as α rises. It is **deliberately not changed**. The
simulated reward is exactly linear and exactly stationary, which is the regime
needing least exploration; the real reward is neither, since outcome attribution
compares against an EMA that the policy's own successes move. Tuning a production
constant to a reward function I invented would be fitting to my own assumptions.
The finding is recorded in `bandit.py` and left for real interaction data.

What this does **not** establish is anything about real students. The reward
function is a plausible shape, not a measured one, and the script says so in its
own output.

A bug the harness's own test caught, which is worth repeating because it is the
easy mistake here: the first ablation removed `none` from the policy *and* from
the oracle, so a crippled policy was scored against a handicapped optimum and
appeared to *improve*. `run_once` now takes `oracle_arms` separately.

## Are the scoring weights actually right?
### (backend/fit_weights.py)

They're hand-tuned, and this README has always said so. The honest next
step was to fit them against labelled outcomes; both halves of that now
exist. `POST /api/session/label` records a self-reported comprehension
rating (1–5: *could you explain what you just produced, without looking at
it?*), and `python3 fit_weights.py` analyses whatever has been collected:

```
correlation between the CURRENT score and self-reported understanding:
  r = +0.765  (n = 40)

per-signal correlation with understanding:
  typed_ratio      r = +0.793
  revision_rate    r = +0.453
  ai_paste_rate    r = +0.066
  active_minutes   r = +0.005
```

(That sample is synthetic, generated to exercise the script — it is not a
finding about real students, and this README will not present one until
there is real data behind it.)

The script **refuses to report a fitted model below `--min-labels`**
(default 30). With a couple of dozen labels, least squares over four
correlated signals produces confident-looking coefficients that are mostly
noise, and dropping those into `scoring.py` would make the product worse
while looking more scientific. It prints the sample size beside every
number and frames its output as evidence to weigh, not a patch to apply —
self-reported understanding is itself a noisy and optimistic label.

## Your data: export and erasure

Two endpoints back the privacy claims above, which until now had no API to
exercise them (the only route was deleting the SQLite file by hand — not an
option once the backend is Supabase):

```bash
curl "localhost:8787/api/me/export?user_id=YOUR_ID"          # everything stored about you
curl -X DELETE "localhost:8787/api/me/data?user_id=YOUR_ID"  # hard-delete, all tables
```

Delete covers every user-scoped table (`sessions`, `user_baseline`,
`nudge_events`, `bandit_state`, `session_labels`), is scoped strictly to
one user, and is idempotent. Both are covered by tests that assert a second
user's rows survive.

## Deploying it

```bash
docker compose up --build      # backend + Postgres, production-shaped
curl localhost:8787/api/health
```

`docker-compose.yml` runs the backend against Postgres with pooling rather
than the default single-file SQLite, so you can reproduce a deployed setup
locally before deploying one. The extension points at
`http://localhost:8787` by default, so it works against this unchanged.

For a real host (Render, Railway, Fly.io, Cloud Run — anything that takes
a Dockerfile), `backend/Dockerfile` is the whole story. It runs as a
non-root user, binds `$PORT` (assigned at runtime by most platforms),
and its `HEALTHCHECK` calls `/api/health`, which issues a real query — so
the platform restarts the container when the *database* is unreachable,
not only when the process has died.

Environment variables that matter in a public deployment:

| Variable | Why you want it |
|---|---|
| `DATABASE_URL` | Postgres/Supabase. Without it the container falls back to a SQLite file in `/data`, which most PaaS filesystems will not persist. |
| `AUTONOMIZE_ALLOWED_ORIGINS` | Comma-separated. The default is `*`, which is correct on localhost (a `chrome-extension://<id>` origin isn't known until install) and too loose in public. |
| `AUTONOMIZE_RATE_LIMIT` | e.g. `300/60`. `POST /api/session/upsert` is an unauthenticated write by default — see the honesty note below. |
| `SUPABASE_JWT_SECRET` | Turns on real per-student auth. Strongly recommended for anything multi-user. |
| `AUTONOMIZE_PG_POOL_MAX` | Keep comfortably under your Postgres instance's connection limit if you run more than one replica. |

**What the rate limiter is and isn't.** It's a token bucket in the memory
of one process. It does not coordinate across replicas (two instances each
enforce the limit separately), it resets on restart, and it is not a
defence against a distributed flood — that belongs at the edge, in
Cloudflare or your ingress, not in application code. It is still worth
having, because it stops one buggy client or one casual `curl` loop from
filling a free-tier database, and it costs nothing. Claiming more than
that for a dict in one process would be a lie, so `ratelimit.py` says the
same thing in its own docstring.

**The Chrome Web Store is a separate matter.** Nothing here is submitted
or reviewed. `<all_urls>` host permissions and a content script on every
page mean a real submission would need a privacy-policy URL, a justified
permissions rationale, and probably a narrower match list. That's product
work, not code, and it hasn't been done.

## Integrating this into your own web app's backend

This backend is intentionally self-contained but easy to fold into a larger
system:

- **Reuse the schema, not just the API.** `backend/db.py` and
  `backend/scoring.py` have zero FastAPI-specific code — you can import
  `scoring.compute_session_score()` / `scoring.risk_level()` directly from
  your own backend, or point your own service at the same database.
  `db.py` already supports Postgres (including Supabase) as a drop-in
  swap for the default SQLite file — see "Connecting Supabase" above —
  so this isn't hypothetical portability, it's a tested second backend
  behind the same two tables (`sessions`, `user_baseline`).
- **Or call it as a service.** Keep `main.py` running as-is and have your
  web app's backend call `GET /api/score?user_id=...` server-to-server
  (same contract the popup uses) instead of talking to it from the browser.
- **`user_id` is already the join key.** By default the extension generates
  a random UUID per install; turn on "Adding Supabase Auth" above and it
  becomes a real authenticated Supabase user id instead, verified
  server-side on every request — either way, every row in
  `sessions`/`user_baseline` slots straight into your existing user model
  once `user_id` means something real to you.
- This MVP deliberately does **not** add an instructor/admin-facing report —
  everything stays student-facing. If a future version of your web app needs
  that, it's a read-only view over the same `sessions` table
  (`category='assessment'`) rather than a new pipeline.

## Honest self-review

`docs/REVIEW.md` is a senior-engineer review of this repo, written against
it rather than for it: what's genuinely strong, a table of everything found
and fixed in a hardening pass (including a migration script that had gone
stale and was silently discarding data), and the questions a sharp
interviewer will still ask — with the honest answers, starting with the
biggest one: **nobody has used this yet.**

## Known MVP limitations / what's next

- **Automated test suite exists now, but isn't exhaustive.** See
  "Automated tests" above: 207 pytest tests (scoring formulas, JWT
  verification, the migration runner including the legacy-upgrade path,
  the LinUCB maths, anomaly detection, nudge reward attribution, the HTTP
  layer end-to-end, and the query layer against *both* SQLite and
  Postgres), the extension's `node --test` unit tests, and Playwright
  suites across the served dashboard and the real unpacked extension —
  plus `e2e/verify-dashboard-web.mjs` for the full account journey in a
  real browser. What's still
  uncovered: `content-script.js` in a real page — the extension suite
  drives `background.js` by sending it the same messages a content script
  would, which exercises every handler but not the DOM instrumentation
  that produces those messages — and any claim that the scoring weights
  are *correct* rather than merely behaving as written, which needs real
  labelled data rather than more tests (see `fit_weights.py`).
- **Service worker lifecycle** — *fixed, and regression-tested.* Chrome
  kills and restarts the MV3 background worker between events. Both the
  cross-tab "AI activity" timestamp and the per-session paste-correlation
  counter now live in `chrome.storage.local`, so neither is lost across a
  restart; the counter was previously an in-memory object, which silently
  under-reported `likely_ai_pastes` (and so quietly *inflated* the
  independence score) whenever a worker restart landed between a paste and
  the flush that consumed it. Because `chrome.storage` has no atomic
  read-modify-write, mutations are serialized through a single promise
  chain so two near-simultaneous pastes can't clobber each other, and
  entries are pruned after 6 hours so sessions that never flush (tab
  closed, browser crash) can't grow the map without bound. See
  `e2e/tests/extension.spec.ts` for the test that stops the real worker
  mid-session and asserts the count still reaches the backend.
- **The contextual bandit is built and tested, but not yet shown to
  students.** `/api/nudge/decide` and `/api/nudge/feedback` exist, backed
  by a real LinUCB policy with two reward paths (see "The contextual
  bandit" above). What does *not* exist is the client side: the extension
  never calls those endpoints and the popup remains display-only, so no
  student has ever seen a nudge. Until it runs against real users the
  policy is unvalidated in the only way that counts — the maths is
  correct, the reward design is defensible, and neither of those is
  evidence that it helps anybody.
- **Supabase-issued identity is supported but off by default.** Supabase
  Auth support (see "Adding Supabase Auth" above) is implemented and
  verified, not a stub — but it only activates if you set
  `SUPABASE_JWT_SECRET`, so a fresh checkout runs on first-party accounts
  and anonymous device accounts instead.
- **Heuristic weights are still heuristic.** The score formula's weights
  (100 / 12 / 22) remain hand-tuned for a sane 0–100 range, not learned.
  What changed is that the machinery to interrogate them now exists —
  `POST /api/session/label` collects self-reported comprehension and
  `fit_weights.py` reports per-signal correlations and a fitted model (see
  "Are the scoring weights actually right?" above). What's missing is
  data: nobody has collected real labels yet, and the script deliberately
  refuses to output a fit below 30 of them. So this stays on the
  limitations list, one step less hypothetical than before.
- **The reward signal is a proxy, and worth naming as one.** Outcome
  attribution rewards a decision when the next scored session beats the
  user's own baseline. That is a better target than "did they tap
  Accept", but it is still a proxy for learning, it can be confounded (an
  easy assignment scores well regardless of any nudge), and a 2-hour
  attribution window is a judgement call rather than a measured one.

## Uninstall / reset

Remove the extension from `chrome://extensions`. To wipe collected data:
stop the backend, then delete `backend/autonomize.db` (SQLite backend) —
or, if you connected Supabase, drop/truncate the `sessions` and
`user_baseline` tables from the Supabase Table Editor or SQL editor.
