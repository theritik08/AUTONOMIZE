# The provided dashboard, connected

`index.html`, `style.css` and the design are **unchanged**. `style.css` is
byte-identical to what was supplied. `index.html` differs by one line — a
`<script>` tag for the backend client.

## What changed, exactly

| File | Change |
|---|---|
| `style.css` | **none** — byte-identical |
| `index.html` | **+1 line**: `<script src="autonomize-api.js">` before `script.js` |
| `script.js` | the `DATA` literal became a live mapping from the API; the render IIFEs became callable so fresh data can redraw them. Every line that produces markup is unchanged. |
| `autonomize-api.js` | new — the backend client |

No markup was restructured, no ids were added, no classes renamed. Values
that were literals in the HTML — the hero chips, the score delta, the
weekly total, the Today ring, the exam panel, the accordion statuses, the
two "Connected" pills — are reached through the selectors the markup
already had.

## Where every element gets its data

| Element | Source |
|---|---|
| Independence gauge, `#scoreValue` | `/api/score` → `current_score` |
| `#scoreDelta`, score note | `delta_vs_baseline`, `baseline_mean` |
| Rings: Independent / AI-assisted | `independent_minutes_7d`, `assisted_minutes_7d` |
| Ring: On track | share of `trend` days at or above `baseline_mean` |
| Chips: Sessions / Streak / Sites | `/api/sessions` (7d), `streak_days`, distinct domains |
| Autonomize Coins | `/api/score` → `coins` (see below) |
| Weekly activity total + bars | `/api/sessions`, tracked minutes per weekday |
| Today ring + total | today's sessions, independent vs AI-assistant |
| Composition chart (bars + lines) | `composition_trend` → `typed_chars` / `pasted_chars` |
| Chart legend totals, "% yours" | summed from `composition_trend` |
| Chart footer trend + fit | `forecast` → `slope_per_day`, `projected_score`, `r2` |
| Exam badge / score / delta / meter | `assessment_risk_level`, `assessment_score`, `assessment_delta` |
| Recent graded sessions | `recent_assessment_sessions` |
| Activity calendar grid + day detail | `/api/sessions` indexed by local day |
| Recent activity list | `/api/sessions` |
| Accordion: backend, categories, top site | config, `/api/me/settings`, `/api/sessions` |
| Status pills (header + footer) | live connection state |
| Avatar initials | `/api/auth/me` → `display_name` |

## Autonomize Coins

The card had no backend. The rule printed on it —
*+10 for a session with nothing pasted · −1 for every 100 characters pasted* —
is now implemented in `backend/coins.py` and returned by `/api/score`.

Server-side rather than in this page for the same reason scoring is: the
moment a second client exists, a browser-side formula gets reimplemented
and the two balances drift silently.

**Note on the supplied seed data.** Its ledger comment says "every entry
follows the stated rule, so the arithmetic can be checked", but the entry
`pasted: 780` was listed as `−12` where the rule gives `−7`. The other five
were correct. The implementation follows the printed rule.

## Running it

```bash
cd backend && python3 -m uvicorn main:app --port 8787
cd dashboard-web && python3 -m http.server 5200
# http://localhost:5200/index.html
```

The backend URL defaults to `http://localhost:8787`, matching the
extension's `DEFAULT_SETTINGS.backendUrl`. Override with
`window.AUTONOMIZE_BACKEND` before `script.js` loads.

**Point the extension at the same URL.** If they differ, this page reads an
empty database while the extension reports every upload succeeding.

## Behaviour when the backend is unreachable

Verified by cutting the connection and waiting for the page's own 30-second
poll: both pills flip to **Disconnected** and every card keeps its last
known values. Blanking the dashboard would turn a blip into "all your data
is gone", which is a worse lie than a number half a minute old.
