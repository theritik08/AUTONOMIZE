"""Builds `docs/demo.html` — the whole dashboard as one self-contained file.

    python3 build_demo_page.py          # against a running local backend
    python3 build_demo_page.py --backend http://localhost:8787

Why this exists: the fastest honest way to show someone this project is a
link they can click, not a five-step local setup they won't do. This
inlines the dashboard's CSS, JS and images into a single HTML file and
replaces `fetch` with a stub that returns a real API response captured
from a seeded backend — so the page is fully interactive (navigation,
theme, chart tooltips, the calendar) with no server, no extension, and no
install.

It is a SNAPSHOT, not a live view. The numbers are frozen at build time.
The page says so on screen, because a demo that silently pretends to be
live is worse than no demo.

SOURCE: `dashboard-web/`, which is the only dashboard.

This used to read the Vite build output of a second, React dashboard
(`extension/dashboard/`, built from `dashboard-app/`). That dashboard is
gone — see the consolidation commit. The rewrite is a simplification
rather than a port: dashboard-web has no build step, no hashed asset
names, and no lazily-imported chunks, so there is nothing to resolve and
nothing to remove from the nav to keep the demo honest.

Regenerate it whenever the dashboard changes:
    1. cd backend && uvicorn main:app --port 8787 &
    2. python3 seed_demo.py
    3. python3 build_demo_page.py
"""
import argparse
import base64
import json
import mimetypes
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC = ROOT / "dashboard-web"
OUT = ROOT / "docs" / "demo.html"

# Load order matters and mirrors index.html: the transport first, then the
# renderer, then the controller that drives both.
SCRIPTS = ("autonomize-api.js", "script.js", "app.js")
IMAGES = ("logo.png", "coin.png", "favicon.png")

# Written by seed_demo.py. Reads are authenticated and the server derives
# identity from the bearer token, so the demo builder cannot simply name a
# user — it has to present the credential the seeder was issued.
IDENTITY = Path(__file__).parent / ".demo-identity.json"

parser = argparse.ArgumentParser()
parser.add_argument("--backend", default="http://127.0.0.1:8787")
args = parser.parse_args()

if not IDENTITY.exists():
    raise SystemExit(
        f"No seeded identity at {IDENTITY}. Run `python3 seed_demo.py` first — "
        "it registers the demo account and writes the token this script reads with."
    )
_identity = json.loads(IDENTITY.read_text())
USER_ID = _identity["user_id"]
TOKEN = _identity["token"]


def api(path: str):
    req = urllib.request.Request(
        f"{args.backend}{path}", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def main() -> None:
    if not (SRC / "index.html").exists():
        raise SystemExit(f"No dashboard source at {SRC}.")

    score = api("/api/score")
    # Matches what the live dashboard asks for: the heatmap needs the full
    # 20-week window, not just the recent-activity feed's worth of rows.
    sessions = api("/api/sessions?limit=400")
    health = api("/api/health")

    if not score.get("trend"):
        raise SystemExit(
            "The backend returned no data for that user — run `python3 seed_demo.py` first, "
            "otherwise the demo page would just show an empty state."
        )

    html = (SRC / "index.html").read_text(encoding="utf-8")
    css = (SRC / "style.css").read_text(encoding="utf-8")
    js = "\n;\n".join((SRC / name).read_text(encoding="utf-8") for name in SCRIPTS)
    # `autonomize-api.js` documents its own usage with a literal
    # `</script>` in a comment. Inlined verbatim, the HTML parser sees that
    # as the END of the script element and treats everything after it as
    # markup — which split the app into three fragments, two of them
    # syntactically invalid, and left the demo rendering an empty shell.
    # Escaping the sequence is the standard remedy and is invisible to JS.
    js = js.replace("</", "<\\/")

    # Inline the images. The demo is one file by definition, and a broken
    # logo is the first thing anyone notices.
    for name in IMAGES:
        path = SRC / name
        if path.exists():
            html = html.replace(f'"{name}"', f'"{data_uri(path)}"')

    # Replace the external stylesheet, the Google Fonts links and the
    # script tags with inlined equivalents. Fonts are dropped rather than
    # inlined: the page falls back to the stack already declared in
    # style.css, which is a cosmetic difference, whereas fetching from
    # fonts.googleapis.com would make a "no server needed" demo silently
    # depend on the network.
    html = html.replace('<link rel="stylesheet" href="style.css">',
                        f"<style>\n{css}\n</style>")
    for line in list(html.splitlines()):
        if "fonts.googleapis.com" in line or "fonts.gstatic.com" in line:
            html = html.replace(line + "\n", "")

    for name in SCRIPTS:
        html = html.replace(f'<script src="{name}"></script>', "")

    stub = f"""
<script>
(function () {{
  // A stored session, so the demo lands on the dashboard rather than the
  // login screen. There is no reachable backend to sign in against, and a
  // login form that cannot succeed is a worse first impression than a
  // frozen dashboard that says it is frozen.
  try {{
    localStorage.setItem('autonomize_auth_token', JSON.stringify('demo-token'));
    localStorage.setItem('autonomize_user_id', JSON.stringify({json.dumps(USER_ID)}));
    localStorage.setItem('autonomize_theme', 'light');
    // No live stream in a static file; the dashboard reports "Offline"
    // honestly rather than claiming to be live.
    localStorage.setItem('autonomize_poll', '0');
  }} catch (e) {{}}

  window.AUTONOMIZE_BACKEND = 'https://demo.invalid';

  var SCORE = {json.dumps(score)};
  var SESSIONS = {json.dumps(sessions)};
  var HEALTH = {json.dumps(health)};
  var USER = {{
    user_id: {json.dumps(USER_ID)},
    email: 'demo@example.edu',
    role: 'student',
    display_name: 'Demo Student',
    provider: 'password',
    email_verified: true,
    has_password: true,
    is_device_account: false
  }};
  var SETTINGS = {{
    settings: {{
      backendUrl: 'https://demo.invalid',
      dashboardUrl: 'https://demo.invalid',
      tracking: {{ ai_assistant: true, writing: true, assessment: true }},
      excludedDomains: []
    }},
    updated_at: null
  }};

  function ok(body) {{
    return Promise.resolve(new Response(JSON.stringify(body), {{
      status: 200, headers: {{ 'Content-Type': 'application/json' }}
    }}));
  }}

  window.fetch = function (input) {{
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.indexOf('/api/health') !== -1) return ok(HEALTH);
    if (url.indexOf('/api/sessions') !== -1) return ok(SESSIONS);
    if (url.indexOf('/api/score') !== -1) return ok(SCORE);
    if (url.indexOf('/api/auth/me') !== -1) return ok({{ user: USER }});
    if (url.indexOf('/api/me/settings') !== -1) return ok(SETTINGS);
    if (url.indexOf('/api/devices') !== -1) return ok({{ devices: [], sessions: [] }});
    if (url.indexOf('/api/auth/config') !== -1) {{
      return ok({{ password: true, google: false, otp: false,
                  email_verification: false, password_reset: false,
                  mail_mode: 'console' }});
    }}
    // Anything else — every write, and the event stream — has no
    // meaningful offline answer. Failing is correct: the UI already
    // handles an unreachable backend, and inventing a success would make
    // buttons look like they worked.
    return Promise.reject(new TypeError('demo: no backend'));
  }};

  // EventSource has no stub either, for the same reason. Removing it
  // makes the dashboard take its "no live stream" path rather than
  // retrying a connection that cannot exist.
  try {{ delete window.EventSource; }} catch (e) {{ window.EventSource = undefined; }}
}})();
</script>
"""

    banner = """
<div style="position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:9999;
            background:rgba(23,21,15,.92);color:#f4f1ea;font:500 12px/1.4 system-ui,sans-serif;
            padding:8px 14px;border-radius:999px;box-shadow:0 8px 24px rgba(0,0,0,.3);
            pointer-events:none;white-space:nowrap">
  Static demo — real data from a seeded backend, frozen at build time.
</div>
"""

    # The stub must run BEFORE the app scripts (it installs the fetch
    # replacement they immediately use), and the app scripts must run after
    # the DOM they bind to exists.
    html = html.replace("</head>", stub + "</head>")
    html = html.replace("</body>", f"<script>\n{js}\n</script>\n{banner}\n</body>")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT} ({len(html) / 1024:.0f} KB) — open it in any browser, no server needed")


if __name__ == "__main__":
    main()
