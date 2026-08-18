"""Seeds a term's worth of realistic demo data so the dashboard isn't an
empty state the first time you look at it.

Two different windows, because two different cards need different things:

* The last **14 days** are hand-tuned. They drive the score, the personal
  baseline, and the typed-vs-pasted chart, so each day's numbers were
  chosen to tell a legible story rather than generated.
* The **18 weeks** before that are generated, and exist for one card: the
  activity heatmap. A contribution grid seeded with a fortnight is 85%
  empty, which reads as "this product has no data" rather than "this
  student took a fortnight's worth of notes". The generator is a fixed-seed
  LCG, not `random`, so two runs produce byte-identical history and the
  committed demo page doesn't churn on every rebuild.

    python3 seed_demo.py                       # seeds the built-in demo user
    python3 seed_demo.py --user-id <your-id>   # seeds YOUR extension's user
    python3 seed_demo.py --backend http://localhost:9000

WHY --user-id EXISTS
--------------------
The extension mints a random UUID per install (see background.js
`getUserId`). Seeding the default demo user therefore does NOT populate
what the extension shows you — you'd load the extension, see an empty
dashboard, and reasonably conclude the thing is broken. Pass your own id
(the script prints where to find it) to seed the account you're actually
looking at.

Dev tooling — nothing in the extension or backend imports this.
"""
import argparse
import json
import pathlib
import time
import urllib.request

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--user-id", default="demo-student-preview",
                    help="who to seed for. Use your extension's id to populate your own dashboard.")
parser.add_argument("--backend", default="http://127.0.0.1:8787", help="backend base URL")
args = parser.parse_args()

BASE = args.backend
NOW = int(time.time() * 1000)
DAY = 86_400_000

# Writes are authenticated, and the server derives the owning user from the
# bearer token — the user_id in a request body is advisory and ignored.
# That is what closed this project's IDOR, so the seeder has to hold a real
# identity rather than assert one.
#
# A DEVICE account is the right kind here: it is exactly what a
# freshly-installed extension registers, so the demo data arrives by the
# same path a real user's would, and it needs no password or mailbox.
_TOKEN = None


def _authenticate():
    """Reuses the previously seeded identity when one is still valid.

    Re-seeding has to land in the SAME account, because the session ids
    below are fixed and `upsert` refuses a session id that belongs to
    someone else — so minting a fresh account on every run turns the second
    run into a wall of 409s.
    """
    global _TOKEN, USER

    if IDENTITY_PATH.exists():
        try:
            saved = json.loads(IDENTITY_PATH.read_text())
            req = urllib.request.Request(
                f"{BASE}/api/auth/me",
                headers={"Authorization": f"Bearer {saved['token']}"},
            )
            urllib.request.urlopen(req).read()
            _TOKEN, USER = saved["token"], saved["user_id"]
            return
        except Exception:
            # Expired, revoked, or a different backend. Fall through and
            # register a fresh one rather than failing the whole seed.
            pass

    req = urllib.request.Request(
        f"{BASE}/api/auth/device", data=b"{}",
        headers={"Content-Type": "application/json"}, method="POST",
    )
    body = json.loads(urllib.request.urlopen(req).read())
    _TOKEN = body["access_token"]
    # The SERVER decides the id. args.user_id survives only as a label in
    # the messages printed at the end.
    USER = body["user"]["user_id"]


def post(path, body):
    headers = {"Content-Type": "application/json"}
    if _TOKEN:
        headers["Authorization"] = f"Bearer {_TOKEN}"
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    urllib.request.urlopen(req).read()


# Handed to build_demo_page.py, which reads this same user's data back out
# through an authenticated API. The server mints both the id and the token,
# so neither can be a constant either script hardcodes.
IDENTITY_PATH = pathlib.Path(__file__).parent / ".demo-identity.json"

USER = args.user_id
_authenticate()

# Session ids are namespaced by the account that owns them. They stay
# stable for a given account (so re-seeding accumulates rather than
# duplicating, matching how the extension reports deltas) while a second
# demo account cannot collide with the first — `upsert` correctly refuses
# a session id that belongs to someone else, and globally-fixed ids turned
# that correct refusal into a wall of 409s.
_SID = USER[:8]


def writing_session(i, days_ago, typed, pasted, backspaces, revisions, ai_pastes, active_min, domain="docs.google.com"):
    post(
        "/api/session/upsert",
        {
            "user_id": USER,
            "session_id": f"{_SID}-w-{i}",
            "category": "writing",
            "domain": domain,
            "path": "/document/d/abc",
            "started_at": NOW - days_ago * DAY - 3600_000,
            "active_ms": active_min * 60_000,
            "metrics": {
                "typed_chars": typed, "pasted_chars": pasted, "backspace_count": backspaces,
                "revision_count": revisions, "prompt_count": 0, "likely_ai_pastes": ai_pastes,
                "tab_switch_count": 0,
            },
            "is_final": True,
        },
    )


def ai_session(i, days_ago, prompts, active_min, domain="chatgpt.com"):
    post(
        "/api/session/upsert",
        {
            "user_id": USER,
            "session_id": f"{_SID}-ai-{i}",
            "category": "ai_assistant",
            "domain": domain,
            "path": "/",
            "started_at": NOW - days_ago * DAY - 1800_000,
            "active_ms": active_min * 60_000,
            "metrics": {
                "typed_chars": 0, "pasted_chars": 0, "backspace_count": 0,
                "revision_count": 0, "prompt_count": prompts, "likely_ai_pastes": 0,
                "tab_switch_count": 0,
            },
            "is_final": True,
        },
    )


def assessment_session(i, days_ago, typed, pasted, ai_pastes, tab_switches, active_min, domain="docs.google.com"):
    post(
        "/api/session/upsert",
        {
            "user_id": USER,
            "session_id": f"{_SID}-exam-{i}",
            "category": "assessment",
            "domain": domain,
            "path": "/forms/d/e/xyz/viewform",
            "started_at": NOW - days_ago * DAY - 2000_000,
            "active_ms": active_min * 60_000,
            "metrics": {
                "typed_chars": typed, "pasted_chars": pasted, "backspace_count": 5,
                "revision_count": 0, "prompt_count": 0, "likely_ai_pastes": ai_pastes,
                "tab_switch_count": tab_switches,
            },
            "is_final": True,
        },
    )


# ---------------------------------------------------------------------------
# Older history (days 14-97). Heatmap fodder: enough shape that the grid has
# a rhythm — busier midweek, quiet weekends, the occasional week off.
# ---------------------------------------------------------------------------
_seed = 20260805  # fixed: reproducible builds, no `random` import


def rnd(n: int) -> int:
    """Tiny LCG. Deterministic across runs and Python versions, which
    `random.seed()` does not actually promise."""
    global _seed
    _seed = (_seed * 1103515245 + 12345) % (1 << 31)
    return _seed % n


HISTORY_DAYS = 140  # 20 weeks — matches the heatmap window exactly
day_of_week_now = time.gmtime(NOW / 1000).tm_wday

for d in range(HISTORY_DAYS - 1, 13, -1):
    # tm_wday counts Monday=0; d days ago moves backwards through the week.
    weekday = (day_of_week_now - d) % 7
    weekend = weekday >= 5

    # A student who worked literally every day for three months would be
    # the least believable thing on the page.
    if rnd(100) < (68 if weekend else 22):
        continue

    # Roughly one day in eight is a long push — an essay deadline, a lab
    # writeup. Without them the heatmap's darkest step never occurs, and a
    # legend advertising a level the data never reaches is a small lie
    # about the scale.
    deep = rnd(100) < 13

    typed = (90 if weekend else 200) + rnd(320) + (700 if deep else 0)
    pasted = rnd(60 if weekend else 130)
    ai = 1 if rnd(100) < 18 else 0
    writing_session(
        1000 + d, d, typed, pasted,
        backspaces=int(typed * 0.07), revisions=1 + rnd(3), ai_pastes=ai,
        active_min=(95 + rnd(70)) if deep else (8 + rnd(30)),
        domain=["docs.google.com", "notion.so", "github.com"][rnd(3)],
    )
    if rnd(100) < 30:
        ai_session(1000 + d, d, prompts=1 + rnd(6), active_min=6 + rnd(20))

# ---------------------------------------------------------------------------
# The last 14 days, hand-tuned: a gentle upward drift plus realistic
# day-to-day noise (a perfectly monotonic line looks synthetic on a chart).
# ---------------------------------------------------------------------------
#
# These are queued rather than posted inline, then flushed oldest-first.
# Order matters for real: the EMA baseline folds each score in as it
# arrives, and `current_score` is the last score the baseline saw. Posting
# a 12-days-ago session after a today session would make the dashboard's
# headline number be a session from last week — an artefact of the seed
# script, not of the product, but indistinguishable from one when you're
# looking at the demo.
events: list[tuple[int, object]] = []

# The last entry is deliberately modest rather than the largest: it lands
# today's session just above the running baseline, which is what keeps the
# streak alive. A demo whose newest day dips below the user's own average
# shows a 0-day streak next to an "improving" forecast, and the two read as
# a contradiction even though both are computed correctly.
noise = [40, -15, 60, 10, -30, 80, 20, -10, 50, 90, -20, 70, 30, 42]
scores_days = [13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
for i, d in enumerate(scores_days):
    typed = 260 + i * 14
    pasted = max(30, 140 - i * 6 + noise[i])
    events.append((d, lambda i=i, d=d, typed=typed, pasted=pasted: writing_session(
        i, d, typed, pasted, backspaces=int(typed * 0.08), revisions=2, ai_pastes=0, active_min=22)))

# AI assistant usage within the last 7 days (for the weekly ratio bar)
events.append((5, lambda: ai_session(1, 5, prompts=8, active_min=35)))
events.append((3, lambda: ai_session(2, 3, prompts=4, active_min=18)))
events.append((1, lambda: ai_session(3, 1, prompts=6, active_min=27)))

# Extra writing sessions across categories on other domains, last 7 days
events.append((4, lambda: writing_session(90, 4, 480, 20, 40, 3, 0, active_min=30, domain="notion.so")))
events.append((2, lambda: writing_session(91, 2, 260, 15, 22, 1, 0, active_min=17, domain="github.com")))

# Assessment sessions: one clean, one risky
events.append((6, lambda: assessment_session(1, 6, typed=520, pasted=0, ai_pastes=0, tab_switches=1, active_min=14)))
events.append((1, lambda: assessment_session(2, 1, typed=15, pasted=380, ai_pastes=3, tab_switches=5, active_min=9)))

for _, send in sorted(events, key=lambda e: -e[0]):
    send()

IDENTITY_PATH.write_text(json.dumps({"user_id": USER, "token": _TOKEN}, indent=2))

print(f"\nSeeded 20 weeks of demo data for user_id = {USER}")
print(f"Identity written to {IDENTITY_PATH.name} for build_demo_page.py\n")

if USER == "demo-student-preview":
    print("To SEE it, you have two options:\n")
    print("  A) Open the dashboard directly as the demo user (no extension needed):")
    print("     1. Open dashboard-web/index.html (serve it, e.g. python3 -m http.server 5599)")
    print("     2. Open DevTools -> Console and paste:")
    print("        localStorage.setItem('autonomize_user_id', JSON.stringify('demo-student-preview'));")
    print(f"        localStorage.setItem('autonomize_settings', JSON.stringify({{backendUrl:'{BASE}',tracking:{{ai_assistant:true,writing:true,assessment:true}},excludedDomains:[]}}));")
    print("     3. Reload the page.\n")
    print("  B) Seed YOUR extension's account instead, so the extension itself shows data:")
    print("     1. Load the extension (chrome://extensions -> Load unpacked -> extension/)")
    print("     2. Click the icon -> Open full dashboard -> DevTools Console:")
    print("        JSON.parse(localStorage.autonomize_user_id || 'null')")
    print("        ...or in the extension's service worker console:")
    print("        chrome.storage.local.get('autonomize_user_id').then(console.log)")
    print("     3. Re-run:  python3 seed_demo.py --user-id <that-id>")
else:
    print("Now run: python3 build_demo_page.py   (or serve dashboard-web/ and sign in)")
