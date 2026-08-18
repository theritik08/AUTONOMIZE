# Autonomize — Authentication

Complete reference for the auth system: schema, environment, endpoints,
local setup, deployment, and the Chrome-extension linking flow.

**This is defence in depth, not a guarantee.** The "What this does not
defend against" section near the end is not boilerplate — read it before
deciding this is safe for a given deployment.

---

## 1. What is implemented

| Capability | Status | Where |
|---|---|---|
| Email + password signup/login | done | `accounts.py`, `/api/auth/register`, `/api/auth/login` |
| Email + OTP signup/login (one button) | done | `otp.py`, `/api/auth/otp/*` |
| Continue with Google (OAuth 2.0 + PKCE) | done | `oauth_google.py`, `/api/auth/google/*` |
| Email verification | done | `/api/auth/email/*` |
| Forgot / reset password by emailed code | done | `/api/auth/password/forgot`, `/reset` |
| Change password (requires current) | done | `/api/auth/password/change` |
| Set a first password (OTP/Google accounts) | done | `/api/auth/password/set` |
| Logout / logout all devices | done | `/api/auth/logout`, `/logout-everywhere` |
| Multiple-device support + per-device revoke | done | `devices.py`, `/api/devices/*` |
| Extension ↔ account linking by short code | done | `/api/devices/link/*` |
| Argon2id hashing (scrypt still verifies) | done | `passwords.py` |
| Short access + rotating refresh, reuse detection | done | `tokens.py` |
| HttpOnly/Secure/SameSite cookies + CSRF | done | `websecurity.py` |
| Rate limiting / brute-force protection | done | `main.py`, `ratelimit.py` |
| IDOR protection, server-side authorization | done | `auth.py`, `db.py` |
| Account deletion | done | `/api/me/account` |

**Not implemented, named rather than implied:** TOTP/MFA as a second
factor (OTP here is a login *method*, not a second factor on top of a
password — calling it 2FA would be a lie); SMS delivery; providers other
than Google; WebAuthn/passkeys; breached-password corpus checks (needs a
network call, so it is a deployment decision — see `passwords.py`).

---

## 2. Database schema

Migration 12. `SCHEMA_VERSION = 12`. Both SQLite and Postgres; `BIGINT`
for millisecond-epoch columns on Postgres because its `INTEGER` is 32-bit
and overflows in 1970 + 24 days.

### `users` (extended)

```
user_id             TEXT PRIMARY KEY
email               TEXT NOT NULL UNIQUE
password_hash       TEXT            -- NULL for OAuth-only / OTP-only / device
role                TEXT NOT NULL DEFAULT 'student'
display_name        TEXT
provider            TEXT NOT NULL DEFAULT 'password'  -- password|otp|google|device
email_verified      INTEGER NOT NULL DEFAULT 0
failed_logins       INTEGER NOT NULL DEFAULT 0
locked_until        BIGINT
created_at          BIGINT NOT NULL
last_login_at       BIGINT
deleted_at          BIGINT          -- migration 12: tombstone
password_changed_at BIGINT          -- migration 12
email_verified_at   BIGINT          -- migration 12
```

`password_hash` being NULL is load-bearing: `passwords.verify` returns
False for NULL unconditionally, so an OAuth or OTP account can never be
logged into with an empty string.

### `refresh_tokens`

```
token_id       TEXT PRIMARY KEY
family_id      TEXT NOT NULL        -- all tokens from one login
user_id        TEXT NOT NULL
token_hash     TEXT NOT NULL UNIQUE -- HMAC-SHA256, never the token
device_id      TEXT
session_jti    TEXT
issued_at      BIGINT NOT NULL
expires_at     BIGINT NOT NULL
used_at        BIGINT               -- set on rotation; reuse trips on this
revoked_at     BIGINT
revoked_reason TEXT                 -- logout | logout_all | reuse_detected | device_revoked
user_agent     TEXT
ip_hash        TEXT                 -- hashed, never the raw address
```

`family_id` is what makes rotation a *detector* rather than bookkeeping —
see §7.

### `otp_codes`

```
otp_id      TEXT PRIMARY KEY
user_id     TEXT                 -- NULL for a signup code (no user row yet)
email       TEXT NOT NULL
purpose     TEXT NOT NULL        -- signup | login | reset | verify_email
code_hash   TEXT NOT NULL        -- HMAC-SHA256(secret, "email:code")
created_at  BIGINT NOT NULL
expires_at  BIGINT NOT NULL
consumed_at BIGINT
attempts    INTEGER NOT NULL DEFAULT 0
ip_hash     TEXT
```

`purpose` is stored in the row and matched on verify. A code mailed for
"confirm your email" cannot be spent on "reset my password" — that
confusion is a full account takeover and the most common OTP bug there is.

### `oauth_states`

```
state         TEXT PRIMARY KEY
code_verifier TEXT NOT NULL      -- PKCE secret, never sent to the browser
nonce         TEXT NOT NULL
redirect_to   TEXT               -- already allowlist-filtered on write
created_at    BIGINT NOT NULL
expires_at    BIGINT NOT NULL
consumed_at   BIGINT             -- single use
```

### `identities`

```
identity_id TEXT PRIMARY KEY
user_id     TEXT NOT NULL
provider    TEXT NOT NULL
subject     TEXT NOT NULL        -- Google's `sub`
email       TEXT
created_at  BIGINT NOT NULL
UNIQUE (provider, subject)
```

Separate from `users` so one account can hold several login methods.
Keyed on `sub`, not email: an address can be renamed or reassigned, and
keying on it means a reassigned address inherits the previous owner's
account.

### `devices`

```
device_id    TEXT PRIMARY KEY    -- random UUID from the client, NOT a fingerprint
user_id      TEXT NOT NULL
label        TEXT
platform     TEXT
client       TEXT
created_at   BIGINT NOT NULL
last_seen_at BIGINT
revoked_at   BIGINT
ip_hash      TEXT
```

### `device_link_codes`

```
code_hash      TEXT PRIMARY KEY   -- HMAC, never the code
device_id      TEXT NOT NULL
device_user_id TEXT NOT NULL      -- the anonymous account being claimed
created_at     BIGINT NOT NULL
expires_at     BIGINT NOT NULL    -- 10 minutes
consumed_at    BIGINT             -- single use
attempts       INTEGER NOT NULL DEFAULT 0
```

### Indexes

```sql
CREATE INDEX idx_refresh_family ON refresh_tokens(family_id);
CREATE INDEX idx_refresh_user   ON refresh_tokens(user_id, revoked_at);
CREATE INDEX idx_otp_lookup     ON otp_codes(email, purpose, created_at);
CREATE INDEX idx_identities_user ON identities(user_id);
CREATE INDEX idx_devices_user   ON devices(user_id);
```

---

## 3. Environment variables

`backend/.env.example` is the authoritative annotated copy. Summary:

### Required in production

| Variable | Why |
|---|---|
| `AUTONOMIZE_AUTH_SECRET` | Signs sessions and keys every token/OTP HMAC. Unset ⇒ random per process ⇒ every restart signs everyone out and two replicas reject each other. `openssl rand -base64 48` |
| `AUTONOMIZE_ALLOWED_ORIGINS` | Exact origins. Defaults to `*`; cookie auth **refuses to start** with `*`. |
| `AUTONOMIZE_SECURE_COOKIES=1` | Secure + `__Host-` prefix. |
| `DATABASE_URL` | Postgres. Omit for local SQLite. |

### Required for OTP / verification / reset

| Variable | Default |
|---|---|
| `AUTONOMIZE_SMTP_HOST` | *(unset ⇒ console mode, **nothing is mailed**)* |
| `AUTONOMIZE_SMTP_PORT` | `587` (`465` switches to implicit TLS) |
| `AUTONOMIZE_SMTP_USER` / `_PASSWORD` | — |
| `AUTONOMIZE_SMTP_STARTTLS` | `1` |
| `AUTONOMIZE_MAIL_FROM` | `Autonomize <no-reply@autonomize.local>` |

### Required for Google (all three together)

`AUTONOMIZE_GOOGLE_CLIENT_ID`, `AUTONOMIZE_GOOGLE_CLIENT_SECRET`,
`AUTONOMIZE_GOOGLE_REDIRECT_URI`, plus
`AUTONOMIZE_OAUTH_REDIRECT_ALLOWLIST` for post-login redirects.

### Tuning

`AUTONOMIZE_ACCESS_TTL` (600) · `AUTONOMIZE_REFRESH_TTL` (2592000) ·
`AUTONOMIZE_COOKIE_SAMESITE` (lax) · `AUTONOMIZE_OTP_TTL_MINUTES` (10) ·
`AUTONOMIZE_OTP_MAX_ATTEMPTS` (5) · `AUTONOMIZE_OTP_RESEND_COOLDOWN` (60) ·
`AUTONOMIZE_OTP_MAX_PER_HOUR` (8) · `AUTONOMIZE_AUTH_RATE_LIMIT` (20 per
5 min) · `AUTONOMIZE_DEVICE_RATE_LIMIT` (60) · `AUTONOMIZE_RATE_LIMIT` (off).

**No secret is ever read by the extension or the dashboard.** The
extension ships `backendUrl` and nothing else; the dashboard reads
`/api/auth/config`, which reports *which* methods are on and never any
key. `.env` is gitignored; `.env.example` contains no real values.

---

## 4. API endpoints

### Sessions

| Method | Path | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/auth/register` | — | 201. Mails a verification code. Sets cookies. |
| `POST` | `/api/auth/login` | — | Password. |
| `POST` | `/api/auth/refresh` | refresh token | Cookie (CSRF required) or body. Rotates. |
| `POST` | `/api/auth/logout` | access | Revokes this session **and its refresh family**. |
| `POST` | `/api/auth/logout-everywhere` | access | Every session and every refresh token. |
| `GET` | `/api/auth/me` | access | User + linked identities. |
| `GET` | `/api/auth/config` | — | What is actually enabled. |
| `POST` | `/api/auth/device` | — | Anonymous install identity. |

### OTP and email

| Method | Path | Auth |
|---|---|---|
| `POST` | `/api/auth/otp/request` | — |
| `POST` | `/api/auth/otp/verify` | — |
| `POST` | `/api/auth/email/send-verification` | access |
| `POST` | `/api/auth/email/verify` | access |

### Passwords

| Method | Path | Auth | Requires |
|---|---|---|---|
| `POST` | `/api/auth/password/forgot` | — | — (always the same response) |
| `POST` | `/api/auth/password/reset` | — | emailed code |
| `POST` | `/api/auth/password/change` | access | **current password** |
| `POST` | `/api/auth/password/set` | access | emailed code (no password exists yet) |

### Google

| Method | Path |
|---|---|
| `GET` | `/api/auth/google/start` → `{authorize_url, state}` |
| `GET` | `/api/auth/google/callback?code&state` |
| `DELETE` | `/api/auth/google/link` (refuses if it is the only way in) |

### Devices

| Method | Path | Auth |
|---|---|---|
| `POST` | `/api/devices/register` | access |
| `GET` | `/api/devices` | access |
| `PATCH` | `/api/devices/{device_id}` | access |
| `DELETE` | `/api/devices/{device_id}` | access |
| `POST` | `/api/devices/link/start` | **device** session |
| `POST` | `/api/devices/link/complete` | **account** session |

### Account

| Method | Path | Requires |
|---|---|---|
| `DELETE` | `/api/me/account` | `confirm: "DELETE"` + password |
| `GET` | `/api/me/export` | access |
| `DELETE` | `/api/me/data` | access |

---

## 5. Local setup

```bash
git clone <repo> && cd autonomize/backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --port 8787 --reload
```

That is the whole thing — SQLite, migrations on boot, no external service.
The startup banner tells you the posture:

```
storage backend: local SQLite
passwords: argon2id t=3 m=64MiB p=4 (scrypt hashes still verify and self-upgrade)
tokens: access 600s, refresh 30d rotating with reuse detection
web security: cookies INSECURE (http, dev only), SameSite=lax, CSRF double-submit
google oauth: off (missing AUTONOMIZE_GOOGLE_CLIENT_ID, ...)
mail: console — NO MAIL IS SENT. OTP codes go to the server log
WARNING  NO MAIL TRANSPORT CONFIGURED...
WARNING  AUTONOMIZE_AUTH_SECRET is unset — a random key was generated...
```

**Reading a code in development:** codes are never in an API response.
They are in the server log, and in `AUTONOMIZE_MAIL_DIR` if you set it:

```bash
AUTONOMIZE_MAIL_DIR=./.mail uvicorn main:app --port 8787
```

**Extension:** `chrome://extensions` → Developer mode → Load unpacked →
`extension/`. It registers a device on first run and starts collecting;
no signup.

**Tests:**

```bash
cd backend && python -m pytest -q          # 694 passed, 15 skipped
python -m pytest tests/test_security_auth.py -q   # 60 adversarial tests
```

---

## 6. Production deployment

### 6.1 Secrets

```bash
openssl rand -base64 48        # -> AUTONOMIZE_AUTH_SECRET
```

Put it in your platform's secret store. Never in the repo, never in the
extension, never in a `VITE_`/`REACT_APP_` variable (those are compiled
into the bundle and are public).

### 6.2 Database

```bash
export DATABASE_URL='postgresql://user:pass@host:5432/autonomize'
export AUTONOMIZE_PG_SCHEMA=autonomize
python -c "import db; print(db.init_db())"     # -> [1, 2, ..., 12]
python verify_schema_target.py                 # confirms it landed where you think
```

### 6.3 Environment

```bash
AUTONOMIZE_AUTH_SECRET=<from 6.1>
AUTONOMIZE_ALLOWED_ORIGINS=https://dashboard.your-college.edu,chrome-extension://YOUR_ID
AUTONOMIZE_SECURE_COOKIES=1
AUTONOMIZE_COOKIE_SAMESITE=lax
AUTONOMIZE_RATE_LIMIT=300/60
AUTONOMIZE_AUTH_RATE_LIMIT=20
AUTONOMIZE_SMTP_HOST=smtp.your-provider.com
AUTONOMIZE_SMTP_USER=apikey
AUTONOMIZE_SMTP_PASSWORD=<secret store>
AUTONOMIZE_MAIL_FROM=Autonomize <no-reply@your-college.edu>
AUTONOMIZE_GOOGLE_CLIENT_ID=xxxx.apps.googleusercontent.com
AUTONOMIZE_GOOGLE_CLIENT_SECRET=<secret store>
AUTONOMIZE_GOOGLE_REDIRECT_URI=https://api.your-college.edu/api/auth/google/callback
AUTONOMIZE_OAUTH_REDIRECT_ALLOWLIST=https://dashboard.your-college.edu/
```

### 6.4 Google Cloud Console

1. APIs & Services → Credentials → Create → OAuth client ID → Web application
2. Authorized redirect URI: exactly your `AUTONOMIZE_GOOGLE_REDIRECT_URI`
   — Google string-compares it, so scheme, port and trailing path all matter
3. OAuth consent screen → scopes `openid`, `email`, `profile`

### 6.5 Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8787 \
  --workers 4 --proxy-headers --forwarded-allow-ips='*'
```

`--proxy-headers` matters: without it every client looks like your load
balancer's address and the per-IP rate limits collapse into one shared
bucket. Note that `_client_ip` deliberately does **not** read
`X-Forwarded-For` itself — unproxied, that header is attacker-controlled
and trusting it would let anyone evade every limit by setting a header.
Strip and re-set it at the edge.

### 6.6 Reverse proxy

```nginx
location / {
    proxy_pass http://127.0.0.1:8787;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;   # SET, not append
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

TLS is not optional: `Secure` cookies are not sent over http, so the
dashboard silently cannot stay signed in without it.

### 6.7 Verify the deployment

```bash
curl -s https://api.your-college.edu/api/auth/config | python -m json.tool
```

Check: `"ephemeral_secret": false`, `"mail_mode": "smtp"`,
`"secure_cookies": true`, `"google": true`.

### 6.8 Package the extension

```bash
cd extension && ./package.sh https://api.your-college.edu
```

Rewrites the origin in **both** the manifest's `host_permissions` and
`DEFAULT_SETTINGS.backendUrl`. Getting only one is the failure where
Chrome silently blocks every fetch and the retry queue fills forever.

### 6.9 Housekeeping

```python
tokens.purge_expired(conn, older_than_days=60)   # NOT sooner — see below
otp.purge_expired(conn, older_than_hours=48)
```

Refresh rows must outlive their expiry by a margin: a deleted token reads
as an *unknown* token, and an unknown token revokes nothing, so purging
eagerly turns reuse detection off for exactly the oldest — most likely
stolen — tokens.

---

## 7. How the token design works

```
LOGIN
  └─ access  (10 min, every request)
  └─ refresh (30 days, only /api/auth/refresh)   family: F

REFRESH
  present refresh R  →  mark R used  →  issue R' in family F
```

Reuse of an already-used token revokes **the entire family**:

```
attacker refreshes first   →  victim presents a used token  →  family dies
victim refreshes first     →  attacker presents a used token →  family dies
```

Either way the thief loses the only credential they had, and the real
user re-authenticates with one the thief does not have. The system cannot
tell which party was the thief and does not try — that is the design, not
a limitation.

---

## 8. Chrome-extension account linking

**No user_id and no device_id is ever typed by anybody.**

```
1. Install                 extension generates a random UUID device id
                           (chrome.storage.local, NOT a fingerprint)
2. First run               POST /api/auth/device -> anonymous account
                           + access/refresh pair. Collection starts.
3. Student clicks          POST /api/devices/link/start  (device session)
   "Link an account"       -> 6-char code, 10 min, single use
4. Popup shows the code    e.g.  QEQAJN
5. Dashboard, SIGNED IN    POST /api/devices/link/complete {code}
6. Server                  moves sessions/baselines/checks/settings to the
                           account, revokes and tombstones the anonymous one
```

**Why the code alone is not dangerous:** step 5 requires an authenticated
account session, and the target account comes from *that session*, never
from the request body. Someone who shoulder-surfs a code can only attach
the device to an account they can already log into — i.e. to their own
data. It is single-use, attempt-capped, expires in ten minutes, and is
stored as an HMAC.

**Why the alphabet excludes `0 O 1 I L`:** a student reads it off a popup
and types it elsewhere. Ambiguity there becomes support tickets that look
exactly like failed attacks in the log.

**Why the device id is random and not a fingerprint:**

1. A fingerprint cannot be revoked, and revocation is the whole point.
2. It follows a person across accounts — two students sharing a lab
   machine would be silently correlated, and someone who deleted their
   account and started again would be re-identified.
3. It survives uninstall. "Remove the extension" should mean something.
4. It is the wrong tool: a random id is a *perfect* identifier for an
   install; a fingerprint is a mediocre one for a machine.

`tests/test_security_auth.py::test_the_codebase_reads_no_hardware_identifiers`
greps the extension and backend for MAC/CPU/serial/canvas access, so
adding one means arguing for it in review rather than slipping it in.

---

## 9. What is never collected

Unchanged by this work, and re-asserted by the existing privacy tests:

- no raw document text
- no raw keystroke sequences — inter-key intervals are stored as an
  **8-bucket histogram**, never an ordered series (an ordered series leaks
  content; a histogram does not)
- no clipboard contents
- no screenshots
- no browsing history beyond the domain/path of a tracked session
- no raw IP addresses anywhere — hashed with the service secret

Auth adds: email address, a password *hash*, hashed tokens, hashed codes,
a random device id, a user-agent string (truncated to 200 chars), and
hashed IPs. Nothing else.

---

## 10. What this does NOT defend against

Stated plainly, because a security document that only lists wins is not
a security document.

- **XSS on the dashboard origin.** HttpOnly protects the refresh token
  from being *read*, and the CSRF token does not help at all here — an
  attacker running script on our origin can read the CSRF cookie and
  forge the header. CSRF tokens have never defended against XSS. The
  defences are the CSP and not putting untrusted HTML on the page.
- **A compromised client.** Malware or a malicious extension in the same
  browser holds the refresh token *and* the device id, and can keep
  rotating silently — reuse detection never fires because the victim's
  copy is being overwritten too.
- **A compromised mailbox.** Every OTP flow proves control of an inbox.
  Someone who owns the mailbox owns the account. That is inherent to
  email-based recovery, not a bug here.
- **A distributed attack.** The rate limiter is a dict in one process
  (`ratelimit.py` says so). Two replicas enforce it separately. Real
  protection belongs at the edge.
- **Timing analysis with enough samples.** `dummy_verify` removes the
  *trivially measurable* difference between "no such account" and "wrong
  password". It is not constant-time in the strict sense.
- **A malicious operator.** Anyone with database write access can insert
  their own `code_hash` or `password_hash`. Hashing protects against a
  *leak*, not against write access.
- **Phishing.** Nothing here stops a student typing a real code into a
  fake page. The mail says we never ask for the code, which is the most
  a plain-text mail can do.

**No claim is made that this is unhackable.** It is a layered
implementation of current practice, with the tests to show which layers
actually hold and the honesty to name the ones that do not exist.

---

## 11. Security test coverage

`backend/tests/test_security_auth.py` — 60 tests, written from the
attacker's side:

| Area | Attacks covered |
|---|---|
| OTP abuse | brute force, resend flooding, superseded codes, replay, **purpose confusion**, code-in-response leak, expiry |
| Password reset | membership oracle, session revocation, cross-account code, double use, policy bypass |
| Change password | stolen-session upgrade, other-session revocation, empty-password on NULL hash |
| Token replay | rotation, **family revocation on reuse**, TTL, revoked session, forged signature, `alg: none`, logout-all |
| IDOR | `?user_id=`, cross-user write, cross-user export/delete, device revoke/rename, device list |
| Unauthorized access | 8 endpoints unauthenticated, role claim ≠ admin, self-granted admin, deleted account |
| Account takeover | duplicate registration, per-account lockout, cross-account spraying, login enumeration, OTP→password hijack |
| CSRF/cookies | HttpOnly, readable CSRF cookie, cross-site refresh, wildcard-CORS refusal, security headers |
| Device linking | full flow, single use, unauthenticated completion, guessed codes, alphabet |
| Fingerprinting | random ids, **repo-wide grep for hardware identifiers** |
| Storage | no recoverable credential, no raw IPs |

Plus `tests/test_tokens_otp_devices.py` (32 unit tests) underneath.

---

## 12. Two bugs this work found

Recorded because they were real and neither was visible from reading the
code.

**1. Per-account lockout had never worked.** `accounts.authenticate`
incremented `failed_logins` and then raised. `db.get_conn()` rolls back on
an exception, so the increment was undone by the raise it preceded —
`failed_logins` sat at 0 after any number of wrong guesses,
`MAX_FAILED_LOGINS` was never reached, and the audit log recorded no
failed logins at all. Both controls read as implemented and neither was.
Fixed with an explicit `conn.commit()` before each raise; the same
pattern was needed in `otp.verify` (attempt cap) and `tokens.rotate`
(family revocation), where it would have been the identical silent
failure.

**2. Returning a `Response` object silently discarded the decorator's
status code.** `@app.post(..., status_code=201)` became a 200 the moment
the handler started setting cookies. Caught by an existing test.

---

*Schema version 12 · 694 backend tests passing · verified end-to-end
against a live server, not only through TestClient.*

---

## Real-time activity stream

The dashboard does not wait for a polling cycle to show new activity.
Updates arrive over **Server-Sent Events**, typically within tens of
milliseconds of the database write.

### Why SSE rather than WebSockets

The traffic is strictly one-directional — the server tells a dashboard that
new activity landed. Nothing the dashboard says needs to travel back over
the same channel; it already has a REST API with authentication, rate
limiting and CSRF built. SSE is plain HTTP, needs no protocol upgrade,
survives proxies that mangle WebSocket handshakes, and `EventSource`
reconnects on its own with `Last-Event-ID` semantics defined by the spec. A
WebSocket would buy bidirectionality this feature does not use, at the cost
of a second authentication path and a hand-written reconnect loop.

### Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/events/ticket` | Exchange a session for a 60-second stream ticket |
| `GET` | `/api/events?ticket=…` | `text/event-stream` for one account |

### Why a ticket instead of the session token

`EventSource` cannot send an `Authorization` header — the API has nowhere
to put one. The alternatives are a cookie (which drags CORS credentials and
a CSRF story into a read-only stream) or a credential in the query string.

A query string is the pragmatic choice, but the **session token must never
go there**: URLs land in access logs, proxy logs and `Referer` headers, and
a leaked session token is a full account compromise. The ticket is a
distinct, single-purpose credential — HMAC-signed with the app's auth
secret, expiring in 60 seconds, read-only, and useless for anything but
opening a stream for the user it names.

### Account isolation

The stream is keyed by the `user_id` inside the *verified* ticket. There is
no `user_id` parameter to tamper with, so one account receiving another's
events is not a check that could be forgotten — it is a thing the code has
no way to express. Forged, tampered, expired and malformed tickets are all
refused with a 401 (`backend/tests/test_events.py`).

### What travels over it

Only what the privacy model already permits to leave the device: aggregate
counters, a domain, a category, a timestamp. `events._public_event`
allow-lists the fields rather than redacting, so a field added to the
upload payload later is invisible here by default instead of leaking until
someone remembers to exclude it. Tests assert that typed text, clipboard
contents and keystroke sequences cannot appear in a serialised frame.

### Durability is separate from delivery

The database write is the durable record and happens first. Publishing is
best-effort notification on top of it, and an event is a **hint that
something changed**, not the thing that changed — the dashboard reconciles
by fetching from the REST API when an event arrives. So a dropped or
duplicated event costs a few seconds of freshness, never data, and a slow
or disconnected listener can never apply backpressure to a write.

### Client behaviour

- **States:** `Live`, `Reconnecting…`, `Offline`, plus a "last updated"
  stamp. Four states because "not live" is several different problems, and
  collapsing them leaves the user unable to tell a quiet afternoon from a
  dead connection.
- **Duplicate suppression:** events carry a monotonic per-user id; anything
  at or below the last seen id is ignored, which makes the spec's
  `Last-Event-ID` replay safe.
- **Reconnect:** exponential backoff capped at 30s, reset on a real
  `online` event.
- **Dead-socket detection:** a socket can die silently, with no error event
  and `EventSource` still reporting itself open. The server heartbeats
  every 20s and the client treats silence past two intervals as death.
  `online`/`offline` events cover the common case immediately.
- **Polling** still exists but is demoted to a slow safety net (2 minutes
  by default), covering only the cases where a stream cannot be
  established at all.

### Deployment limit — read before scaling out

The bus is **in-process**. With one uvicorn worker (the default, and what
`docker-compose.yml` runs) this is correct.

Run two or more workers and a dashboard connected to worker A will not see
events published on worker B. It does not break — the client still
reconciles on its polling fallback — but it stops being real-time for some
users, silently. Fixing it properly means an out-of-process broker (Redis
pub/sub, Postgres `LISTEN`/`NOTIFY`); `events.publish()` is deliberately
the single seam where that would be swapped in. The startup log prints the
active configuration, and warns explicitly when `WEB_CONCURRENCY` is set
above 1, so this cannot be discovered by accident in production.
