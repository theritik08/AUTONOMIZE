# Seeing it actually work

Three ways, fastest first. Every command below was run end-to-end from a
fresh unzip before this was written — none of it is from memory.

---

## 0. Just look at it — 10 seconds, nothing to install

Open **`docs/demo.html`** in any browser. Double-click it; that's the whole
process.

It's the real dashboard with real data from a seeded backend, inlined into
one file. Fully interactive: dark mode, chart tooltips, the calendar, the
activity feed. No Python, no Node, no extension.

One honest limit: the numbers are **frozen at build time** (a banner on
the page says so). Everything else is the genuine article — the same
`dashboard-web/` HTML, CSS and JS, inlined by `backend/build_demo_page.py`
with `fetch` stubbed out to replay a real API response.

Regenerating it takes three commands and no build step — with the backend
running: `cd backend && python3 seed_demo.py && python3 build_demo_page.py`.
`seed_demo.py` has to go first: it writes `backend/.demo-identity.json`,
the credential the builder reads the API with.

**This is also the file to put on the internet.** `.github/workflows/pages.yml`
publishes it to GitHub Pages on every push to `main` — enable it once under
*Settings → Pages → Source: GitHub Actions*, and you get a URL you can put
in a CV instead of asking someone to clone a repo.

---

## 1. Run the real backend — about 2 minutes

You need Python 3.10+. You do **not** need Node: the dashboard
(`dashboard-web/`) is plain HTML/CSS/JS with no build step and no npm
dependencies. Node is only needed for the Playwright e2e suite.

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --port 8787
```

You should see:

```
INFO  autonomize storage backend: local SQLite
INFO  autonomize applied schema migrations: [1, 2, 3, 4, 5]
INFO  autonomize auth: off (trusting client-supplied user_id)
```

Check it:

```bash
curl http://localhost:8787/api/health
# {"status":"ok","database":{"reachable":true,...}}
```

Leave it running. Open a second terminal for anything below.

Serve the dashboard from that second terminal — it's a static directory,
so any file server will do:

```bash
cd dashboard-web
python3 -m http.server 5599 --bind 127.0.0.1
```

Open <http://127.0.0.1:5599/index.html> and sign in (or sign up). Don't
open it as `file://` — a file page has a `null` origin and Chrome blocks
every API call.

---

## 2. Load the extension — about 1 minute

1. Chrome → `chrome://extensions`
2. Turn on **Developer mode** (top-right)
3. **Load unpacked** → select the **`extension/`** folder
4. Pin the Autonomize icon to your toolbar

Click the icon. It will say **"No activity tracked yet"** — that's correct,
not a bug. You haven't browsed anything yet.

---

## 3. Make data appear

### The honest way — use it for five minutes

1. Go to ChatGPT or Claude, send a couple of prompts.
2. Open a Google Doc (or Notion, or GitHub) and **type** a few sentences.
3. Copy something from the AI tab and **paste** it into the doc.
4. Leave the tab for ~20 seconds so the session finalises and uploads.
5. Click the extension icon → **Open full dashboard** (it opens the web
   dashboard you started in §1 — the `dashboardUrl` setting, default
   `http://localhost:5599/index.html`).

That paste, landing within 10 minutes of the AI tab, is the signal the
whole project is built around — you should see it counted.

### The instant way — seed two weeks of data

There's a catch worth knowing, because it's the most likely reason you'd
think this is broken:

> **The extension mints a random UUID for you on install.** Seeding the
> built-in demo user does *not* populate what your extension shows. You'd
> see an empty dashboard and reasonably assume nothing works.

So seed **your own** id:

```bash
# 1. Find your id: open the dashboard tab
#    -> DevTools (F12) -> Console -> paste:
#       JSON.parse(localStorage.autonomize_user_id)

# 2. Seed it:
cd backend
python3 seed_demo.py --user-id <the-id-you-just-copied>
```

Reload the dashboard. A fortnight of hand-tuned recent history for the
score, the composition chart and the exam-integrity panel, plus a
generated term's worth behind it so the activity heatmap has something to
show — all populated.

*(Running `python3 seed_demo.py` with no arguments seeds the built-in demo
user instead and prints instructions for viewing that. Either works.)*

---

## 4. Optional: the production-shaped setup

```bash
docker compose up --build      # backend on Postgres, with pooling
```

Same API on the same port, so the extension needs no change. This is the
configuration to deploy, not the SQLite default — see the README's
**Deploying it** section.

To use a hosted Postgres instead of a local container, set `DATABASE_URL`
(and, on a Supabase project shared with anything else,
`AUTONOMIZE_PG_SCHEMA=autonomize`) and run `backend/seed_supabase.sh` —
it loads demo data and proves the connection in one command. Details,
including which Supabase port to use and why the other one hangs:
**[docs/SUPABASE.md](docs/SUPABASE.md)**.

---

## Running the tests

```bash
cd backend && pip install -r requirements-dev.txt && python3 -m pytest -q
node --test extension/tests/*.test.js               # from the repo root
cd e2e && npm install && npx playwright install chromium && npx playwright test
```

The e2e suite starts its own backend and serves `dashboard-web/` as a
static directory, loads the real unpacked extension in Chrome, and drives
its service worker. It uses a separate database file and separate ports
(8799/5199), so it's safe to run while your dev backend is up.

There's also a full sign-up → verify → sign-in → dashboard browser journey
driven end to end against a live backend:

```bash
cd e2e && node verify-dashboard-web.mjs
```

(Its file header lists the exact backend and static-server commands it
expects to be running.)

---

## If something looks wrong

| Symptom | Cause |
|---|---|
| Popup says **"Backend not reachable"** | `uvicorn` isn't running, or it's on a different port. Check `curl localhost:8787/api/health`. |
| Popup says **"No activity tracked yet"** | Working as intended — no sessions recorded yet. Browse a bit, or seed your own id (§3). |
| Dashboard empty after seeding | You almost certainly seeded `demo-student-preview` instead of your extension's UUID. See §3. |
| Extension won't load | Select the `extension/` folder itself, not the repo root. |
| Nothing recorded on a site | `localhost` and Google search are excluded on purpose (`manifest.json` → `exclude_matches`). Try a Google Doc. |
| Changed dashboard code, no effect | There's no build step — hard-reload the dashboard tab (Ctrl/Cmd-Shift-R) to get past the browser cache. |
| **Open full dashboard** goes nowhere | Nothing is serving `dashboard-web/` on the `dashboardUrl` port (default 5599), or you changed that setting. |
