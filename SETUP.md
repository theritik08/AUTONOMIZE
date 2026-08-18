# Running Autonomize on your PC

Written from a clean unzip and verified command by command. Nothing below
is from memory.

**What you need:** Python 3.10 or newer, and Google Chrome. No Node.js.

---

## Step 1 — Unzip

Put the folder anywhere. Every path below is relative to it.

```
autonomize/
├── backend/            FastAPI + SQLite — computes every score
├── extension/          the Chrome extension (load this folder in Chrome) —
│                       a telemetry collector and a small popup, nothing else
├── dashboard-web/      the dashboard: plain HTML/CSS/JS, served over http.
│                       No build step, no npm install, no bundler.
└── docs/
```

**Nothing needs editing.** The extension's default backend is
`http://localhost:8787` and so is the dashboard's, so the defaults already
agree.

---

## Step 2 — Install the backend

**Folder:** `backend/`

```bash
cd backend
pip install -r requirements.txt
```

On macOS/Linux, if pip refuses with "externally-managed-environment", use a
virtual environment (recommended) or add `--break-system-packages`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

---

## Step 3 — Start the backend

**Folder:** `backend/` — leave this terminal running.

```bash
python3 -m uvicorn main:app --port 8787
```

You should see exactly this:

```
INFO  autonomize storage backend: local SQLite
INFO  autonomize applied schema migrations: [1, 2, 3, 4, 5, 6, 7, 8, 9]
INFO  autonomize auth: first-party sessions
INFO  autonomize next-horizon predictor: none (no model has been trained yet)
INFO  autonomize rate limit: off
INFO  autonomize cors allowed origins: ['*']
WARNING autonomize CORS is open to all origins while auth is enabled
INFO:     Uvicorn running on http://127.0.0.1:8787
```

The migration list must end at **9**. If it stops at 8, you are running an
older copy of `backend/` and the dashboard's coins card and calendar detail
will be empty.

The CORS warning is expected for local use. It matters only if you put this
on a public URL.

Check it from a second terminal:

```bash
curl http://localhost:8787/api/health
# {"status":"ok","database":{"reachable":true,"backend":"local SQLite"},...}
```

A file `backend/autonomize.db` appears on first run. That is your data.

---

## Step 4 — Load the extension in Chrome

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the **`extension/`** folder — the one containing `manifest.json`

**Permissions Chrome will show, and why each is needed** — all declared in
`extension/manifest.json`, nothing to enable by hand:

| Permission | Why |
|---|---|
| `storage` | keeps the retry queue and session token across service-worker restarts |
| `alarms` | drains the upload queue every 2 minutes, syncs settings every 15 |
| `tabs` | reads the tab's hostname to classify the site |
| Host access to `localhost:8787` / `127.0.0.1:8787` | uploading sessions to your backend |
| "Read and change all your data on all websites" | the content script counts keystroke and paste **events** on any page. It reads `.length` of a paste and drops the string in the same statement — no text, no titles, no URLs beyond the hostname. |

`localhost` and `127.0.0.1` are in the manifest's `exclude_matches`, so
the dashboard itself is never tracked.

---

## Step 5 — Open the dashboard

The extension does not contain a dashboard. There is exactly one, and it
is served over http from `dashboard-web/` — a static directory, so any
file server works.

**Folder:** `dashboard-web/` — a third terminal.

```bash
cd dashboard-web
python3 -m http.server 5599 --bind 127.0.0.1
```

Open <http://127.0.0.1:5599/index.html> and create an account (or sign in).
Port 5599 is what the extension popup's **Open full dashboard** button
opens by default — it uses the `dashboardUrl` setting, stored server-side
alongside `backendUrl`, so change it there if you serve on another port.

Do not open the dashboard as `file://`. A file page has a `null` origin, so
Chrome blocks every API call and the pill sticks on "Disconnected".

### Pair the extension with your account

A fresh install collects under an anonymous device account, so uploads
start before you have signed in anywhere. To make those uploads land on
the account you just created:

1. Click the extension icon → **Link an account**. It shows a
   six-character code.
2. On the dashboard, signed in, go to **Settings → Connected Devices** and
   type the code into **Link a Chrome extension**.

Until you do this, the extension and the dashboard are two different
identities — the extension uploads perfectly happily while the dashboard
looks empty.

---

## Step 6 — Make real data flow

The extension only tracks sites it recognises. From
`extension/site-map.js`, these count as **writing**:

```
docs.google.com   notion.so   overleaf.com   github.com
```

`chatgpt.com` counts as **ai_assistant**, and `docs.google.com/forms/…`
counts as **assessment**.

1. Open <https://docs.google.com> and start a new document
2. **Type** a few sentences — real typing, not pasting
3. Paste a paragraph from somewhere, so there is something to compare
4. Wait about a minute, or close the tab (that forces a flush)
5. Reload the dashboard

The extension batches and uploads every ~60 seconds, and retries on a
2-minute alarm if the backend was down.

---

## Step 7 — Confirm the data is really flowing

**A. Did the backend receive it?** In the terminal running uvicorn you will
see `POST /api/session/upsert 200`.

**B. Ask the API directly.** In the dashboard tab's console:

```js
await (await fetch('http://localhost:8787/api/sessions?limit=5', {
  headers: { Authorization: 'Bearer ' + JSON.parse(localStorage.autonomize_auth_token) }
})).json()
```

You should get your session rows, each with `typed_chars` and
`pasted_chars`.

**C. Watch the dashboard change.** These move as soon as a session lands:

| Panel | What changes |
|---|---|
| Independence score | 50 → your real score |
| Autonomize Coins | +10 for a session with nothing pasted, −1 per 100 pasted characters |
| Sessions / Sites chips | count up |
| What you wrote vs. pasted | a column appears per day |
| Activity calendar | click today — it lists the site, typed and pasted counts |
| Recent activity | the site appears with its category and duration |

---

## Errors you may hit

| What you see | Cause | Fix |
|---|---|---|
| Page renders unstyled | `style.css` not beside `index.html` | keep every file of `dashboard-web/` in one folder — it loads `style.css`, then `autonomize-api.js`, `script.js`, `app.js`, in that order |
| Broken image icons | `logo.png` / `coin.png` / `favicon.png` missing | same folder as `index.html` |
| Pill says **Disconnected** | backend not running, wrong port, or page opened as `file://` | start uvicorn; serve over http |
| Dashboard empty but extension works | the extension is still on its anonymous device account — two identities | link it to your account (Step 5) |
| Everything reads `—` or `0` | connected, but no sessions uploaded yet | do Step 6 |
| Migrations stop at `[… 8]` | old copy of `backend/` | use the `backend/` from this zip |
| `address already in use` | port 8787 or 5599 taken | `--port 8788`, and set `window.AUTONOMIZE_BACKEND` to match |
| `externally-managed-environment` | system Python protects itself | use a venv (Step 2) |
| Coins card empty | backend missing `coins.py` | use the `backend/` from this zip |
| Nothing tracked on a site | site not in `site-map.js` | use docs.google.com, notion.so, overleaf.com or github.com |

The dashboard logs `[autonomize]` with the exact reason to the browser
console on any failure. Check there first.

**One thing that is not a bug:** a brand-new account shows **50/100** with
no sessions. That is the backend's neutral starting value, and the line
beneath it reads "No baseline yet — still learning how you work". It moves
to a real score as soon as your first session lands.

---

## FINAL CHECKLIST

- [ ] `curl http://localhost:8787/api/health` returns `"status":"ok"`
- [ ] Uvicorn startup shows migrations `[1 … 9]`
- [ ] `chrome://extensions` lists Autonomize with no errors
- [ ] Clicking the extension icon shows a popup, not a blank box
- [ ] Dashboard opens and the pill reads **Connected**
- [ ] Dashboard logo and coin images render
- [ ] Typed on docs.google.com, waited a minute
- [ ] Uvicorn logged `POST /api/session/upsert 200`
- [ ] Independence score is no longer 50
- [ ] Coins balance changed
- [ ] Sessions / Sites chips are non-zero
- [ ] "What you wrote vs. what was pasted" has at least one column
- [ ] Clicking today in the calendar lists the site with typed/pasted counts
- [ ] Recent activity lists the session
- [ ] Bars ↔ Lines both draw; month arrows work; theme toggle works
- [ ] Stopping uvicorn flips both pills to **Disconnected** and the numbers stay
