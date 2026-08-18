// Shared telemetry core: the aggregate metric shape, its arithmetic, and
// the detector registry. Loaded first because the module-level
// destructuring below depends on it. See the note beside that line for why
// this is importScripts rather than an ESM import.
importScripts("telemetry.js");

// Autonomize background service worker (MV3).
// Responsibilities:
//   - own the anonymous user_id (or pick up the real one — see getUserId)
//   - relay batched session metrics from content scripts to the backend
//   - track "was an AI assistant just used" for cross-tab paste correlation
//   - retry queue for offline/backend-down resilience
//
// Auth note: this worker owns its own credentials now.
//
//   - On first run it registers a DEVICE account (POST /api/auth/device)
//     and stores the access + refresh pair. It never invents its own
//     user_id; the server mints the identity, which is what closed the
//     original IDOR.
//   - Access tokens are short (ten minutes). getValidAccessToken()
//     refreshes ahead of expiry and postJson retries once on a 401.
//   - Refresh tokens ROTATE server-side, so two concurrent refreshes
//     would present the same token twice and the server would correctly
//     read that as replay and revoke the family. refreshPromise
//     serialises them for exactly that reason.
//   - The device id is a random UUID generated at install and kept in
//     chrome.storage.local. It is NOT a hardware fingerprint — see
//     getDeviceId() for why that refusal is deliberate.
//
// The stale note that used to live here ("this worker doesn't refresh an
// expiring token itself; the token only gets refreshed when the
// dashboard/popup is open") no longer describes the code.
//
// Lifecycle note: Chrome tears this worker down aggressively between
// events, so NOTHING that has to outlive a single event may live in a
// module-level variable. Everything stateful here is in
// chrome.storage.local for that reason — the user id, the retry queue, the
// last-AI-activity timestamp, and the pending paste-correlation counters.

// NOTE ON HOST PERMISSIONS (manifest.json): each entry is an EXACT origin,
// never a wildcard like `https://*.example.com/*`. A wildcard would ask for
// far more permission than uploading counters to one API needs, and it is
// the first thing a store reviewer questions.
//
// WHAT THAT LIST DOES AND DOES NOT BUY — measured, not assumed.
//
// An earlier version of this comment claimed Chrome BLOCKS a fetch to an
// origin outside `host_permissions`. That was tested against the packaged
// build (see e2e/verify-packaged-extension.mjs) and it is NOT true here: a
// fetch from this extension to an unlisted origin returned a `basic`
// response with a fully readable body.
//
// The reason is `content_scripts.matches: ["<all_urls>"]`, which this
// extension genuinely needs — the generic detector has to work on any site
// with an editable surface, not a curated list. That broad match is what
// produces Chrome's "read and change all your data on all websites"
// warning, and it also means the narrow `host_permissions` list is not the
// network boundary it looks like.
//
// So the honest description: `host_permissions` here is a statement of
// INTENT, useful to a reviewer and to whoever reads the manifest next. It
// is not a sandbox. The controls that actually constrain what leaves this
// device are the ones in telemetry.js — aggregate counters only, no text,
// no clipboard, no ordered keystrokes, sensitive fields refused outright —
// and they hold regardless of which origins are listed.
//
// Before packaging for the Chrome Web Store, still replace
// `https://api.autonomize.example/*` with your deployed API origin and set
// `backendUrl` below to match. Not because Chrome would otherwise block
// it, but because a manifest that names an origin the extension never
// contacts — while contacting one it never declared — is exactly what gets
// a submission rejected.
const DEFAULT_SETTINGS = {
  backendUrl: "http://localhost:8787",
  // Where "Open full dashboard" goes. The extension no longer ships a
  // dashboard of its own — there is exactly one, and it is the web app.
  dashboardUrl: "http://localhost:5599/index.html",
  tracking: { ai_assistant: true, writing: true, assessment: true },
  excludedDomains: []
};

const AI_CORRELATION_WINDOW_MS = 10 * 60_000; // 10 minutes
const QUEUE_KEY = "autonomize_queue";
const MAX_QUEUE = 200;

// Per-session "pastes that landed shortly after AI-tool activity" counters,
// awaiting the flush that folds them into a session's likely_ai_pastes.
//
// These live in chrome.storage.local, NOT in a module-level object: Chrome
// can tear this service worker down between any two events, and a paste
// event and the flush that consumes it are frequently minutes apart (the
// content script batches). An in-memory map silently loses every count
// across such a restart, and the loss is invisible — the session still
// uploads, just with an under-reported likely_ai_pastes, which quietly
// inflates the independence score. See e2e/tests/extension.spec.ts for the
// regression test that reloads the extension mid-session to prove this.
const PENDING_CORRELATION_KEY = "autonomize_pending_correlation";
// Entries are consumed on flush; this only bounds sessions that never
// flush at all (tab closed mid-session, browser crash) so the map can't
// grow without limit.
const PENDING_CORRELATION_TTL_MS = 6 * 60 * 60_000; // 6 hours
const MAX_PENDING_CORRELATION = 500;

// chrome.storage has no atomic read-modify-write. Two paste events arriving
// close together (two tabs, or one tab pasting twice) would otherwise each
// read the same starting value and each write back the same +1, losing a
// count — precisely the bug this persistence exists to prevent. Every
// mutation is funnelled through one promise chain so they can't interleave.
let correlationQueue = Promise.resolve();

function pruneCorrelation(map) {
  const cutoff = Date.now() - PENDING_CORRELATION_TTL_MS;
  for (const [sessionId, entry] of Object.entries(map)) {
    if (!entry || typeof entry.count !== "number" || (entry.ts ?? 0) < cutoff) {
      delete map[sessionId];
    }
  }
  // Oldest-first eviction if something pathological still overflows it.
  const keys = Object.keys(map);
  if (keys.length > MAX_PENDING_CORRELATION) {
    keys
      .sort((a, b) => (map[a].ts ?? 0) - (map[b].ts ?? 0))
      .slice(0, keys.length - MAX_PENDING_CORRELATION)
      .forEach((k) => delete map[k]);
  }
  return map;
}

/** Runs `mutator(map)` against the persisted correlation map under the
 * serialization chain, writes the result back, and resolves to whatever the
 * mutator returned. */
function withPendingCorrelation(mutator) {
  const run = correlationQueue.then(async () => {
    const stored = await chrome.storage.local.get(PENDING_CORRELATION_KEY);
    const map = pruneCorrelation({ ...(stored[PENDING_CORRELATION_KEY] || {}) });
    const result = await mutator(map);
    await chrome.storage.local.set({ [PENDING_CORRELATION_KEY]: map });
    return result;
  });
  // A failed mutation must not wedge every later one behind a rejected
  // promise, so the chain itself only ever tracks settlement.
  correlationQueue = run.then(
    () => undefined,
    () => undefined
  );
  return run;
}

async function recordCorrelatedPaste(sessionId) {
  await withPendingCorrelation((map) => {
    const entry = map[sessionId] || { count: 0, ts: Date.now() };
    entry.count += 1;
    entry.ts = Date.now();
    map[sessionId] = entry;
  });
}

/** Removes and returns the pending count for a session (0 if none). */
async function takeCorrelatedPastes(sessionId) {
  return withPendingCorrelation((map) => {
    const entry = map[sessionId];
    if (!entry) return 0;
    delete map[sessionId];
    return entry.count;
  });
}

// ───────────────────────────────────────────────────────────────────────
// Per-tab subframe metric aggregation.
//
// See the frame-architecture comment in content-script.js for why this
// exists. Short version: Google Docs routes every keystroke into a hidden
// iframe, so the top frame — the only frame that owns a session — sees
// none of them. Subframes post raw, category-agnostic deltas here; they
// are filed per TAB and merged into that tab's next top-frame flush.
//
// Keyed by tabId because that is the only identifier BOTH roles share: a
// subframe cannot know the top frame's sessionId (it usually cannot even
// read the top frame's URL), and the worker gets the tabId for free from
// the message sender. It lives in chrome.storage for the same reason the
// paste-correlation map does — Chrome tears this worker down between any
// two events, and a subframe report and the flush that consumes it are
// routinely seconds to minutes apart.
const FRAME_METRICS_KEY = "autonomize_frame_metrics";
// Bounds tabs that report and then never flush (closed mid-typing, or a
// page whose top frame was never a tracked surface at all).
const FRAME_METRICS_TTL_MS = 30 * 60_000;
const MAX_FRAME_METRIC_TABS = 200;

// The aggregate shape and its arithmetic are defined ONCE, in telemetry.js,
// and pulled in here. Re-declaring them in the worker is how the histogram
// bucket count silently drifts out of step with the content script and the
// backend, and a histogram whose buckets mean different things in two
// places is worse than no histogram at all.
//
// `importScripts` rather than an ESM import: telemetry.js must also load as
// a classic content script (MV3 content scripts cannot be modules), so it
// exposes itself on the global rather than exporting. That in turn is why
// this worker is registered WITHOUT "type": "module" in the manifest — a
// module worker cannot importScripts, and one shared file beats two copies.
const { IKI_BUCKET_COUNT, emptyRaw, addRaw } = globalThis.AutonomizeTelemetry;

/**
 * Maps category-agnostic raw input counts onto the metric names the
 * backend scores.
 *
 * This mapping lives HERE, once, rather than in the content script,
 * because a subframe cannot classify what it is part of — it usually
 * cannot see the real page URL. The top frame supplies the category; the
 * worker applies it to the combined counts from every frame.
 */
function rawToMetrics(raw, category, tabSwitchCount) {
  const metrics = {
    typed_chars: 0,
    pasted_chars: raw.pasted_chars,
    backspace_count: 0,
    revision_count: 0,
    prompt_count: 0,
    likely_ai_pastes: 0,
    tab_switch_count: tabSwitchCount || 0,
    iki_buckets: raw.iki_buckets.slice(),
    long_pauses: raw.long_pauses,
    burst_keys: raw.burst_keys
  };

  if (category === "ai_assistant") {
    // On an assistant surface the interesting quantity is how many
    // prompts were submitted, not how much was typed — typing into a
    // chat box is not independent work and must never be counted as it.
    metrics.prompt_count = raw.enter;
    metrics.typed_chars = 0;
    metrics.pasted_chars = 0;
    metrics.iki_buckets = new Array(IKI_BUCKET_COUNT).fill(0);
    metrics.long_pauses = 0;
    metrics.burst_keys = 0;
    return metrics;
  }

  // writing / assessment
  metrics.typed_chars = raw.printable;
  metrics.backspace_count = raw.backspace;
  metrics.revision_count = raw.undo + raw.cut;
  return metrics;
}

// chrome.storage has no atomic read-modify-write, and several frames on
// one page report independently. Same serialisation reasoning as the
// correlation map above.
let frameMetricsQueue = Promise.resolve();

function withFrameMetrics(mutator) {
  const run = frameMetricsQueue.then(async () => {
    const stored = await chrome.storage.local.get(FRAME_METRICS_KEY);
    const map = { ...(stored[FRAME_METRICS_KEY] || {}) };
    const cutoff = Date.now() - FRAME_METRICS_TTL_MS;
    for (const [tabId, entry] of Object.entries(map)) {
      if (!entry || (entry.ts ?? 0) < cutoff) delete map[tabId];
    }
    const keys = Object.keys(map);
    if (keys.length > MAX_FRAME_METRIC_TABS) {
      keys
        .sort((a, b) => (map[a].ts ?? 0) - (map[b].ts ?? 0))
        .slice(0, keys.length - MAX_FRAME_METRIC_TABS)
        .forEach((k) => delete map[k]);
    }
    const result = await mutator(map);
    await chrome.storage.local.set({ [FRAME_METRICS_KEY]: map });
    return result;
  });
  frameMetricsQueue = run.then(() => undefined, () => undefined);
  return run;
}

async function recordFrameMetrics(tabId, delta) {
  if (tabId == null) return;
  await withFrameMetrics((map) => {
    const entry = map[tabId] || { raw: emptyRaw(), ts: Date.now() };
    // A stored entry round-trips through JSON, so rebuild it into a shape
    // addRaw can safely mutate rather than trusting the persisted keys.
    const merged = addRaw(addRaw(emptyRaw(), entry.raw), delta);
    map[tabId] = { raw: merged, ts: Date.now() };
  });
}

/** Removes and returns a tab's pending subframe counts. */
async function takeFrameMetrics(tabId) {
  if (tabId == null) return emptyRaw();
  return withFrameMetrics((map) => {
    const entry = map[tabId];
    if (!entry) return emptyRaw();
    delete map[tabId];
    return addRaw(emptyRaw(), entry.raw);
  });
}

/** Drops a tab's pending counts — the top frame decided this page is not
 *  a tracked surface, so nothing its subframes collected may be used. */
async function discardFrameMetrics(tabId) {
  if (tabId == null) return;
  await withFrameMetrics((map) => {
    delete map[tabId];
  });
}

// A closed tab's pending counts can never be claimed by a flush, so free
// them immediately rather than waiting out the TTL.
chrome.tabs.onRemoved.addListener((tabId) => {
  discardFrameMetrics(tabId).catch(() => {});
});

async function getUserId() {
  const { autonomize_user_id } = await chrome.storage.local.get("autonomize_user_id");
  return autonomize_user_id || null;
}

async function getAuthToken() {
  const { autonomize_auth_token } = await chrome.storage.local.get("autonomize_auth_token");
  return autonomize_auth_token || null;
}

async function getRefreshToken() {
  const { autonomize_refresh_token } = await chrome.storage.local.get("autonomize_refresh_token");
  return autonomize_refresh_token || null;
}

/**
 * This install's device id. Random, generated once, kept locally.
 *
 * NOT a hardware fingerprint, and that is a deliberate refusal rather
 * than a shortcut. A fingerprint (MAC address, CPU id, disk serial,
 * canvas hash) cannot be revoked — and revocation is the entire point of
 * a device list, because "sign this device out" has to mean something.
 * It also survives uninstall and follows a person between accounts,
 * which turns a convenience feature into a tracking identifier.
 *
 * crypto.randomUUID() is a CSPRNG. Reinstalling produces a new id, which
 * is correct: it IS a new install.
 */
async function getDeviceId() {
  const { autonomize_device_id } = await chrome.storage.local.get("autonomize_device_id");
  if (autonomize_device_id) return autonomize_device_id;
  const fresh = crypto.randomUUID();
  await chrome.storage.local.set({ autonomize_device_id: fresh });
  return fresh;
}

async function storeSession(data) {
  await chrome.storage.local.set({
    autonomize_auth_token: data.access_token,
    autonomize_refresh_token: data.refresh_token || null,
    // Refresh a minute early. Racing the expiry means the occasional
    // request goes out with a token that died in flight, and the retry
    // queue fills with 401s that look like an outage.
    autonomize_token_expires_at: (data.expires_at || 0) - 60_000,
    autonomize_user_id: data.user ? data.user.user_id : null,
    autonomize_account_email: data.user && !data.user.is_device_account
      ? data.user.email : null
  });
}

// Serialises refreshes. Several tabs can flush at once, and because the
// server ROTATES refresh tokens, two concurrent refreshes would present
// the same token twice — which the server correctly reads as replay and
// responds to by revoking the whole family. Without this lock the
// extension would repeatedly sign itself out and blame the server.
let refreshPromise = null;

async function refreshAccessToken(settings) {
  if (refreshPromise) return refreshPromise;

  refreshPromise = (async () => {
    const refreshToken = await getRefreshToken();
    if (!refreshToken) return null;
    try {
      const resp = await fetchWithTimeout(`${settings.backendUrl}/api/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken })
      });
      if (resp.status === 401) {
        // The family is gone: either it was revoked, it expired, or reuse
        // was detected. Clearing the stored pair is what turns this into
        // "sign in again" instead of an infinite retry loop.
        await chrome.storage.local.remove([
          "autonomize_auth_token", "autonomize_refresh_token",
          "autonomize_token_expires_at"
        ]);
        return null;
      }
      if (!resp.ok) return null;
      const data = await resp.json();
      if (!data.access_token) return null;
      await storeSession(data);
      return data.access_token;
    } catch (_) {
      return null;      // offline: keep the stored token and try later
    } finally {
      refreshPromise = null;
    }
  })();

  return refreshPromise;
}

/** A valid access token, refreshing it first if it is about to expire. */
async function getValidAccessToken(settings) {
  const { autonomize_token_expires_at } =
    await chrome.storage.local.get("autonomize_token_expires_at");
  const token = await getAuthToken();
  if (token && autonomize_token_expires_at && Date.now() < autonomize_token_expires_at) {
    return token;
  }
  if (await getRefreshToken()) {
    const refreshed = await refreshAccessToken(settings);
    if (refreshed) return refreshed;
  }
  return token;
}

// Serialises device registration. Several tabs can flush at once on a
// fresh install, and without this each one would register its own device
// account — the student's history would silently split across identities.
let identityPromise = null;

/**
 * Ensures this browser has a server-issued identity.
 *
 * The extension used to invent its own `crypto.randomUUID()` and send it
 * as `user_id`, which the backend trusted. That was the IDOR: naming
 * someone else's id was enough to read their data. Now the server mints
 * the identity and returns a session token, and the token is the only
 * thing that says who we are.
 *
 * Returns null if registration fails — callers treat that the same as a
 * backend outage and queue the payload, so a server that is briefly down
 * on first install delays scoring rather than losing it.
 */
async function ensureIdentity(settings) {
  const existing = await getAuthToken();
  if (existing) return existing;
  if (identityPromise) return identityPromise;

  identityPromise = (async () => {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      let resp;
      try {
        resp = await fetch(`${settings.backendUrl}/api/auth/device`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Autonomize-Device-Id": await getDeviceId()
          },
          body: "{}",
          signal: controller.signal
        });
      } finally {
        clearTimeout(timer);
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      if (!data.access_token) throw new Error("no token in response");
      await storeSession(data);
      return data.access_token;
    } catch (_) {
      return null;
    } finally {
      identityPromise = null;
    }
  })();

  return identityPromise;
}

async function getSettings() {
  const { autonomize_settings } = await chrome.storage.local.get("autonomize_settings");
  return { ...DEFAULT_SETTINGS, ...(autonomize_settings || {}) };
}

// ---------------------------------------------------------------------------
// Settings sync
// ---------------------------------------------------------------------------
//
// chrome.storage is now a CACHE, not the record. The record is the server —
// see backend/settings_store.py for why: a dashboard served as an ordinary
// web page cannot read chrome.storage at all, so a settings screen there
// would have been either inert or a second source of truth that disagreed
// with this worker from the first click.
//
// This worker keeps reading chrome.storage on the hot path and never the
// network. A flush must not wait on, or fail because of, a settings fetch —
// the settings it needs are the tracking toggles, and a stale toggle for
// two minutes is a far smaller problem than a dropped session.
//
// Conflicts are last-write-wins on the SERVER's clock. A device with a
// wrong system time could otherwise pin its stale settings permanently by
// claiming a timestamp in the future.
const SETTINGS_SYNCED_AT_KEY = "autonomize_settings_synced_at";

async function pullSettings(settings) {
  // getValidAccessToken, not getAuthToken: access tokens now live ten
  // minutes, and this alarm fires every fifteen — so a raw stored token
  // is expired essentially every time and settings sync would silently
  // never work again.
  const token = await getValidAccessToken(settings);
  if (!token) return null;
  try {
    const resp = await fetchWithTimeout(`${settings.backendUrl}/api/me/settings`, {
      headers: { Authorization: `Bearer ${token}` }
    });
    if (!resp.ok) return null;
    const body = await resp.json();
    if (!body || !body.settings) return null;

    const { [SETTINGS_SYNCED_AT_KEY]: lastSynced } =
      await chrome.storage.local.get(SETTINGS_SYNCED_AT_KEY);

    // Only adopt the server's copy when it is genuinely newer than what we
    // last wrote. Without this check, a local change made while offline is
    // overwritten by the server on the next poll — the user watches their
    // toggle flip back and has no way to tell why.
    if (body.updated_at && lastSynced && body.updated_at <= lastSynced) return null;

    // backendUrl is deliberately NOT adopted from the server. It is the
    // address used to reach the server in the first place, so letting a
    // response rewrite it means one bad value can strand this extension
    // with no way back — it would keep asking an unreachable host for the
    // address of a reachable one.
    const { backendUrl: _ignored, ...remote } = body.settings;
    const merged = { ...(await getSettings()), ...remote };
    await chrome.storage.local.set({
      autonomize_settings: merged,
      [SETTINGS_SYNCED_AT_KEY]: body.updated_at || Date.now()
    });
    return merged;
  } catch (_) {
    // Offline, or no backend. The cached settings stay in force, which is
    // the whole point of caching them.
    return null;
  }
}

async function pushSettings(next) {
  const settings = await getSettings();
  const token = await getValidAccessToken(settings);
  // Written locally first and unconditionally: the user's own toggle must
  // take effect even with no network, and the retry on the next alarm will
  // carry it up.
  await chrome.storage.local.set({ autonomize_settings: { ...settings, ...next } });
  if (!token) return false;
  try {
    const resp = await fetchWithTimeout(`${settings.backendUrl}/api/me/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(next)
    });
    if (!resp.ok) return false;
    const body = await resp.json();
    // Adopt the server's normalised form — it rewrites `https://Example.com/`
    // to `example.com`, and the stored form is what the content script
    // matches against, so keeping the raw text would silently not match.
    const { backendUrl: _ignored, ...remote } = body.settings || {};
    await chrome.storage.local.set({
      autonomize_settings: { ...settings, ...next, ...remote },
      [SETTINGS_SYNCED_AT_KEY]: body.updated_at || Date.now()
    });
    return true;
  } catch (_) {
    return false;
  }
}

async function markAiActivity(ts) {
  await chrome.storage.local.set({ autonomize_last_ai_ts: ts });
}

async function getLastAiActivity() {
  const { autonomize_last_ai_ts } = await chrome.storage.local.get("autonomize_last_ai_ts");
  return autonomize_last_ai_ts || 0;
}

// chrome.storage has no atomic read-modify-write, and the upload queue has
// exactly the hazard the correlation map above already guards against: two
// tabs failing to reach the backend at the same moment each read the same
// array and each write back their own single addition, silently dropping
// one session. Same fix, same reasoning.
let queueChain = Promise.resolve();

function withQueue(mutator) {
  const run = queueChain.then(async () => {
    const { [QUEUE_KEY]: queue = [] } = await chrome.storage.local.get(QUEUE_KEY);
    const result = await mutator(queue);
    // Oldest-first eviction: during a long outage the newest sessions are
    // the ones still worth having.
    while (queue.length > MAX_QUEUE) queue.shift();
    await chrome.storage.local.set({ [QUEUE_KEY]: queue });
    return result;
  });
  queueChain = run.then(() => undefined, () => undefined);
  return run;
}

async function enqueue(item) {
  await withQueue((queue) => { queue.push(item); });
}

// A hung backend is worse than a refused one: fetch's default has no
// timeout, so a request to a server that accepts the connection and never
// answers stays open indefinitely. With the drain alarm firing every two
// minutes those pile up, each holding a service-worker wake-up alive.
// Failing fast lands the payload in the retry queue, which is exactly where
// it should be.
const REQUEST_TIMEOUT_MS = 15_000;

/** fetch with the abort timeout above always applied.
 *
 * Extracted so the settings sync gets the same protection rather than
 * growing a second copy of the timeout dance — every request this worker
 * makes should fail fast for the reason described above, not just the ones
 * whose author remembered. */
async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function postJson(url, body) {
  const settings = await getSettings();
  const token = await getValidAccessToken(settings);
  const headers = {
    "Content-Type": "application/json",
    "X-Autonomize-Device-Id": await getDeviceId()
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  let resp = await fetchWithTimeout(url, {
    method: "POST", headers, body: JSON.stringify(body)
  });

  if (resp.status === 401 && await getRefreshToken()) {
    // The access token expired between the check above and this request,
    // or the server restarted on an ephemeral secret. One retry after a
    // forced refresh; never a loop, because a genuinely revoked family
    // would retry forever and the caller's queue would never drain.
    const refreshed = await refreshAccessToken(settings);
    if (refreshed) {
      headers["Authorization"] = `Bearer ${refreshed}`;
      resp = await fetchWithTimeout(url, {
        method: "POST", headers, body: JSON.stringify(body)
      });
    }
  }

  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json().catch(() => ({}));
}


// ---------------------------------------------------------------------------
// Account linking
//
// A fresh install collects under an anonymous device account so there is
// no signup wall. Linking moves that history onto a real account: the
// extension asks for a code, the user signs in on the dashboard and
// enters it. No user id and no device id is ever typed by anybody.
// ---------------------------------------------------------------------------

async function startAccountLink() {
  const settings = await getSettings();
  const token = await getValidAccessToken(settings);
  if (!token) return { ok: false, error: "not_registered" };

  const resp = await fetchWithTimeout(`${settings.backendUrl}/api/devices/link/start`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${token}`,
      "X-Autonomize-Device-Id": await getDeviceId()
    },
    body: "{}"
  });
  if (resp.status === 409) return { ok: false, error: "already_linked" };
  if (!resp.ok) return { ok: false, error: `http_${resp.status}` };
  const data = await resp.json();

  // The claim secret is the half of linking that used to be missing.
  // Completing a link revokes THIS install's token and deletes the
  // account behind it, so without something to exchange afterwards the
  // extension is left holding a dead credential and every upload 401s.
  // Stored, never displayed: the user types the six-character code, not
  // this.
  await chrome.storage.local.set({
    autonomize_link_claim: data.claim_secret || null,
    autonomize_link_claim_expires_at: data.expires_at || 0
  });

  return { ok: true, code: data.code, expiresAt: data.expires_at };
}

/** Collects the credential a completed link left waiting.
 *
 * Returns {status} of "idle" (nothing pending), "pending" (code not
 * entered yet), "linked" (done — a real account session is now stored),
 * or "expired" (start over).
 *
 * Called on a poll from the popup while the code is on screen, and from
 * an alarm so a link completed after the popup closed still lands.
 */
async function claimAccountLink() {
  const { autonomize_link_claim: claimSecret } =
    await chrome.storage.local.get("autonomize_link_claim");
  if (!claimSecret) return { status: "idle" };

  const settings = await getSettings();
  let resp;
  try {
    resp = await fetchWithTimeout(`${settings.backendUrl}/api/devices/link/claim`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ claim_secret: claimSecret })
    });
  } catch (_) {
    // A network failure is not an expired link. Keep the secret and let
    // the next poll try again, or the user is told to re-link over a
    // hiccup they never saw.
    return { status: "pending" };
  }

  // 404/410 are terminal: unrecognised, already claimed, or too late.
  // Anything else transient keeps the secret so the next poll retries.
  if (resp.status === 404 || resp.status === 410) {
    await chrome.storage.local.remove([
      "autonomize_link_claim", "autonomize_link_claim_expires_at"
    ]);
    return { status: "expired" };
  }
  if (!resp.ok) return { status: "pending" };

  const data = await resp.json().catch(() => ({}));
  if (data.status !== "linked") return { status: "pending" };

  // Swap the dead device credential for the real account's session
  // BEFORE clearing the secret, so a crash between the two retries the
  // claim rather than stranding the install with nothing.
  await storeSession(data);
  await chrome.storage.local.remove([
    "autonomize_link_claim", "autonomize_link_claim_expires_at"
  ]);

  // Everything that failed with 401 while the token was dead is still
  // queued. Draining now is what makes the dashboard populate the moment
  // the link lands instead of at the next two-minute alarm.
  await drainQueue();

  return { status: "linked", email: data.user ? data.user.email : null };
}

/** Whether this install is on a real account, and which one. */
async function accountStatus() {
  const { autonomize_account_email, autonomize_user_id } =
    await chrome.storage.local.get(["autonomize_account_email", "autonomize_user_id"]);
  return {
    linked: Boolean(autonomize_account_email),
    email: autonomize_account_email || null,
    userId: autonomize_user_id || null,
    deviceId: await getDeviceId()
  };
}

/** Signs this install out and starts collecting anonymously again.
 *
 * The stored history stays on the server under the account it was linked
 * to — signing out of a device must not delete a student's work. */
async function signOutDevice() {
  const settings = await getSettings();
  const token = await getAuthToken();
  const refreshToken = await getRefreshToken();
  try {
    await fetchWithTimeout(`${settings.backendUrl}/api/auth/logout`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { "Authorization": `Bearer ${token}` } : {})
      },
      body: JSON.stringify({ refresh_token: refreshToken })
    });
  } catch (_) {
    // Local state is cleared regardless. A server we cannot reach must
    // not be able to keep a user signed in on their own machine.
  }
  await chrome.storage.local.remove([
    "autonomize_auth_token", "autonomize_refresh_token",
    "autonomize_token_expires_at", "autonomize_user_id", "autonomize_account_email"
  ]);
  return { ok: true };
}

async function sendOrQueue(path, body) {
  const settings = await getSettings();
  const url = `${settings.backendUrl}${path}`;
  try {
    await postJson(url, body);
  } catch (err) {
    await enqueue({ path, body, ts: Date.now() });
  }
}

async function drainQueue() {
  const settings = await getSettings();
  const { [QUEUE_KEY]: queue = [] } = await chrome.storage.local.get(QUEUE_KEY);
  if (queue.length === 0) return;
  const remaining = [];
  for (const item of queue) {
    try {
      await postJson(`${settings.backendUrl}${item.path}`, item.body);
    } catch (_) {
      remaining.push(item);
    }
  }
  await chrome.storage.local.set({ [QUEUE_KEY]: remaining });
}

chrome.alarms.create("autonomize_drain_queue", { periodInMinutes: 2 });
// One minute is the floor Chrome enforces on a packed extension's alarms,
// so this is as fast as the background path can be. The popup polls much
// faster while it is open (see popup.js); this alarm is the fallback for
// the case where the user closes the popup, walks to the dashboard, and
// enters the code with nothing watching.
chrome.alarms.create("autonomize_link_claim", { periodInMinutes: 1 });
// Settings change rarely, so this is deliberately slower than the queue
// drain. Polling every two minutes for a value a user edits once a month
// would be traffic spent on nothing.
chrome.alarms.create("autonomize_sync_settings", { periodInMinutes: 15 });
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === "autonomize_drain_queue") drainQueue();
  if (alarm.name === "autonomize_link_claim") await claimAccountLink();
  if (alarm.name === "autonomize_sync_settings") await pullSettings(await getSettings());
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // The handler's RETURN VALUE is forwarded, not discarded. This used to
  // reply `{ok: true}` unconditionally, which was fine while every
  // message was fire-and-forget telemetry — the account-link messages
  // below actually have an answer the popup needs, and swallowing it
  // would leave the UI unable to show the code it just requested.
  handleMessage(message, sender)
    .then((result) => sendResponse(result === undefined ? { ok: true } : result))
    .catch((err) => sendResponse({ ok: false, error: String(err && err.message || err) }));
  return true; // keep the message channel open for the async response
});

async function handleMessage(message, sender) {
  if (!message || !message.type) return;
  const settings = await getSettings();

  if (message.type === "autonomize_account_status") return accountStatus();
  if (message.type === "autonomize_link_start") return startAccountLink();
  if (message.type === "autonomize_link_claim") return claimAccountLink();
  if (message.type === "autonomize_sign_out") return signOutDevice();

  // A subframe reporting raw input counts. Filed against the sender's TAB
  // and merged into that tab's next top-frame flush. Nothing is uploaded
  // from here: a subframe has no session, no category and no domain, so
  // its counts are meaningless until a top frame claims them — and a tab
  // whose top frame never claims them expires by TTL.
  if (message.type === "autonomize_frame_metrics") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    await recordFrameMetrics(tabId, message.raw);
    return;
  }

  // The top frame decided this page is not a tracked surface. Anything its
  // subframes already filed must be dropped rather than left to be picked
  // up by a later navigation in the same tab.
  if (message.type === "autonomize_frame_discard") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    await discardFrameMetrics(tabId);
    return;
  }

  if (message.type === "autonomize_paste_event") {
    const lastAi = await getLastAiActivity();
    if (message.ts - lastAi <= AI_CORRELATION_WINDOW_MS) {
      await recordCorrelatedPaste(message.sessionId);
    }
    return;
  }

  if (message.type === "autonomize_flush") {
    const tabId = sender && sender.tab ? sender.tab.id : null;

    // Gating happens BEFORE the subframe counts are claimed, and the
    // discard on the excluded/disabled path is deliberate: otherwise a
    // tab's pending counts would survive and be merged into whatever the
    // user navigated to next, which is exactly how an excluded domain's
    // typing would end up attributed to an included one.
    if (message.domain && settings.excludedDomains.includes(message.domain)) {
      await discardFrameMetrics(tabId);
      return;
    }
    if (!settings.tracking[message.category]) {
      await discardFrameMetrics(tabId);
      return;
    }

    if (message.category === "ai_assistant") {
      await markAiActivity(Date.now());
    }

    // Combine what the top frame saw with what this tab's subframes
    // reported. On Google Docs the top frame's own counts are all zero and
    // every keystroke arrives through this merge — see the frame
    // architecture note in content-script.js.
    //
    // `message.metrics` is the legacy pre-computed shape, still accepted so
    // an older content script (or a test harness posting a flush directly)
    // keeps working; `message.raw` is what the current script sends.
    let metrics;
    if (message.raw) {
      const combined = addRaw(addRaw(emptyRaw(), message.raw), await takeFrameMetrics(tabId));
      metrics = rawToMetrics(combined, message.category, message.tabSwitchCount);
    } else {
      metrics = { ...(message.metrics || {}) };
    }

    const isScorable = message.category === "writing" || message.category === "assessment";
    if (isScorable) {
      // Take-and-clear: once folded into `metrics` the count travels with
      // the payload, and sendOrQueue persists that whole payload to the
      // retry queue if the POST fails — so consuming it here can't lose it
      // to a backend outage.
      const correlated = await takeCorrelatedPastes(message.sessionId);
      if (correlated > 0) {
        metrics.likely_ai_pastes = (metrics.likely_ai_pastes || 0) + correlated;
      }
    }

    // Register with the backend if this browser has no identity yet. The
    // user_id below is now advisory only — the server derives identity
    // from the bearer token and ignores the field — but it is still sent
    // so a deployment running the legacy anonymous mode keeps working.
    await ensureIdentity(settings);
    const userId = await getUserId();
    await sendOrQueue("/api/session/upsert", {
      user_id: userId,
      session_id: message.sessionId,
      category: message.category,
      domain: message.domain,
      path: message.path,
      started_at: message.startedAt,
      active_ms: message.activeMs,
      metrics,
      // Which detector claimed this page, and whether its signals were
      // genuinely observable. A "limited" session is one where the browser
      // does not expose keystroke/paste events for the editor in use — the
      // dashboard labels it as such rather than showing a zero that reads
      // as "wrote nothing". Never resolve a limited surface by inventing
      // numbers for it.
      detector: message.detector || null,
      capability: message.capability || null,
      is_final: message.isFinal,
      client_ts: Date.now()
    });
    return;
  }

  if (message.type === "autonomize_get_user_id") {
    return getUserId();
  }

  // Lets the popup and an extension-hosted dashboard write settings through
  // the same path the server sync uses, rather than each writing
  // chrome.storage directly and none of them reaching the backend.
  if (message.type === "autonomize_set_settings") {
    return pushSettings(message.settings || {});
  }

  if (message.type === "autonomize_get_settings") {
    // Refresh opportunistically, but answer from cache if the network is
    // unavailable — a settings screen that hangs on a dead backend is worse
    // than one showing values that are a few minutes old.
    const settings = await getSettings();
    return (await pullSettings(settings)) || settings;
  }

  // Everything the popup needs, fetched HERE rather than in the popup.
  //
  // THE BUG THIS FIXES: popup.js read `autonomize_auth_token` straight out
  // of storage and sent it as a bearer token. Access tokens live ten
  // minutes. Only this worker knows how to refresh one — getValidAccessToken
  // below, whose rotations are serialised by refreshPromise so several
  // callers cannot present the same refresh token twice and trip the
  // server's reuse detection.
  //
  // So any popup opened more than ten minutes after the last refresh sent
  // a dead token, got a 401, and reported BOTH "Offline" and "sign-in
  // required" — while the backend was reachable and the session was
  // perfectly valid. Moving the fetch here is what makes the popup's
  // "Connected" mean the same thing the rest of the extension means by it,
  // and it keeps exactly one implementation of refresh.
  if (message.type === "autonomize_popup_status") {
    return popupStatus();
  }
}

/**
 * Resolves the popup's whole state in one authenticated round trip.
 *
 * Returns a discriminated `state` rather than a bag of booleans, because
 * the popup's old failure was showing two contradictory states at once.
 * Exactly one of these is true at any moment:
 *
 *   offline        the backend could not be reached at all
 *   signed-out     reached it, but no usable credential (refresh failed
 *                  too, so signing in again is genuinely required)
 *   ok             reached it and it answered
 */
async function popupStatus() {
  const settings = await getSettings();
  const account = await accountStatus();

  // Provision an identity if this install has never had one. Without this
  // a fresh profile shows "Setting up…" until the first page flush.
  await ensureIdentity(settings);

  const token = await getValidAccessToken(settings);
  if (!token) {
    return { state: "signed-out", backendUrl: settings.backendUrl, settings, account };
  }

  let resp;
  try {
    resp = await fetchWithTimeout(`${settings.backendUrl}/api/score`, {
      headers: { Authorization: `Bearer ${token}` }
    });
  } catch (_) {
    // A genuine transport failure. This is the ONLY path that may report
    // "Offline".
    return { state: "offline", backendUrl: settings.backendUrl, settings, account };
  }

  if (resp.status === 401) {
    // getValidAccessToken already tried to refresh, so a 401 here means the
    // refresh token is gone or revoked too — the session really is over.
    // Clearing it is what stops the popup asking for a sign-in it will not
    // remember.
    await chrome.storage.local.remove([
      "autonomize_auth_token", "autonomize_refresh_token",
      "autonomize_token_expires_at", "autonomize_account_email"
    ]);
    return { state: "signed-out", backendUrl: settings.backendUrl, settings, account };
  }

  if (!resp.ok) {
    return { state: "offline", backendUrl: settings.backendUrl, settings, account };
  }

  return {
    state: "ok",
    backendUrl: settings.backendUrl,
    settings,
    account,
    score: await resp.json()
  };
}

chrome.runtime.onInstalled.addListener(async () => {
  const { autonomize_settings } = await chrome.storage.local.get("autonomize_settings");
  if (!autonomize_settings) {
    await chrome.storage.local.set({ autonomize_settings: DEFAULT_SETTINGS });
  }
  // Best-effort: if the backend isn't running at install time this simply
  // fails and the first flush retries it.
  await ensureIdentity(await getSettings());
  // Adopt anything already stored server-side. This is what makes a
  // reinstall, or a second machine, pick up the settings the user already
  // chose instead of silently reverting them to the defaults.
  await pullSettings(await getSettings());
});
