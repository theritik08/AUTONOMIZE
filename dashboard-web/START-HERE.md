# Autonomize dashboard — run it

## 1. Keep all 7 files in ONE folder

```
index.html          your dashboard          (+1 script tag)
style.css           your styles             (UNCHANGED, byte-identical)
script.js           your behaviour          (data layer connected)
autonomize-api.js   backend client          (new)
logo.png            your logo
coin.png            your coin
favicon.png         your favicon
```

**If `style.css` is missing the page renders unstyled.** That is the single
most common cause of "the CSS stopped working" — `index.html` links it with
a relative path (`href="style.css"`), so it must sit beside `index.html`.

## 2. Start the backend

```bash
cd backend
pip install -r requirements.txt      # first time only
python3 -m uvicorn main:app --port 8787
```

## 3. Serve the dashboard

It must be served over http, **not** opened as `file://`. A page opened
from disk has a `null` origin, so the browser blocks its API calls and you
get a permanently "Disconnected" pill with a CORS error in the console.

```bash
cd dashboard-web
python3 -m http.server 5200
```

Open <http://localhost:5200/index.html>.

## 4. Point the extension at the same backend

Load `extension/` at `chrome://extensions` (Developer mode → Load
unpacked). Its default backend is `http://localhost:8787`, which matches
the dashboard's default.

**If the two URLs differ, the dashboard reads an empty database while the
extension reports every upload succeeding** — which looks like a broken
dashboard and is not.

To point the dashboard elsewhere, set the global before `script.js` runs:

```html
<script>window.AUTONOMIZE_BACKEND = 'http://127.0.0.1:8787';</script>
<script src="autonomize-api.js"></script>
<script src="script.js"></script>
```

## Checks when something looks wrong

| Symptom | Cause |
|---|---|
| Page renders unstyled | `style.css` not in the same folder |
| Broken image icons | `logo.png` / `coin.png` / `favicon.png` missing |
| Pill says Disconnected | backend not running, or opened as `file://` |
| Everything reads `—` or `0` | connected, but no sessions uploaded yet |
| Fonts look wrong offline | Google Fonts is a CDN link; the layout still works |

Open the browser console — the client logs `[autonomize]` with the exact
reason on any failure.
