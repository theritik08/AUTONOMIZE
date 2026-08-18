/**
 * AUTONOMIZE — dashboard connector
 * ================================
 *
 * A dependency-free client that connects any HTML dashboard to the
 * Autonomize backend, which is where the Chrome extension's captured data
 * already lives.
 *
 * THE DATA PATH, AND WHY IT GOES THROUGH THE SERVER
 * -------------------------------------------------
 *
 *   content-script.js  counts events, never text
 *          |
 *   background.js      POST /api/session/upsert   (batched, retry-queued)
 *          |
 *   FastAPI + SQLite   scores, baselines, anomaly, ML   <- one place
 *          |
 *   THIS FILE          GET /api/score, /api/sessions
 *          |
 *   your dashboard
 *
 * A page served at http://localhost:3000 cannot read `chrome.storage` —
 * that API only exists inside the extension. So a web dashboard that tried
 * to read the extension's local data directly would get nothing. Going
 * through the backend is not indirection for its own sake: it is the only
 * path that works, and it is also what makes the dashboard survive an
 * extension reinstall and follow the user to a second machine.
 *
 * USAGE — three lines
 * -------------------
 *
 *     <script src="autonomize-api.js"></script>
 *     <script>
 *       Autonomize.connect().then(() => Autonomize.bind());
 *     </script>
 *
 * `bind()` fills any element carrying a `data-autonomize` attribute:
 *
 *     <span data-autonomize="current_score"></span>
 *     <span data-autonomize="streak_days"></span>
 *     <span data-autonomize="assessment_score"></span>
 *
 * See FIELDS below for every binding name. Anything more bespoke than
 * text substitution reads the data directly:
 *
 *     const score = await Autonomize.score();
 *     drawMyChart(score.composition_trend);
 *
 * WHAT THIS DELIBERATELY DOES NOT DO
 * ----------------------------------
 *
 * It does not compute anything. Every number it hands you was computed by
 * the backend, because the extension, this dashboard and any future client
 * must agree on what a score means — and they only can if exactly one
 * place decides. Re-deriving an independence score in the browser is how a
 * project ends up with two answers and no way to tell which is wrong.
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------------
  // Configuration
  // ---------------------------------------------------------------------

  const DEFAULTS = {
    // Must match the extension's `backendUrl` setting, or the two will be
    // talking to different databases and the dashboard will look empty
    // while the extension reports everything uploaded fine.
    backendUrl: 'http://localhost:8787',
    // /api/score is cheap and the extension flushes on its own schedule,
    // so this is about how fresh the page feels rather than about load.
    pollMs: 30000,
    // Where the session token is kept. localStorage rather than a cookie:
    // the API takes a bearer token, and a cookie would additionally need
    // CORS credentials and a CSRF story for no benefit here.
    tokenKey: 'autonomize_auth_token',
    userKey: 'autonomize_user_id',
  };

  const config = Object.assign({}, DEFAULTS);

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  let token = null;
  let userId = null;
  let lastScore = null;
  let lastSessions = null;
  let pollTimer = null;
  const listeners = { data: [], error: [], status: [] };

  function emit(event, payload) {
    (listeners[event] || []).forEach(function (fn) {
      // One misbehaving listener must not stop the others, and must not
      // take down the polling loop that called us.
      try { fn(payload); } catch (error) { console.error('[autonomize]', error); }
    });
  }

  // ---------------------------------------------------------------------
  // Transport
  // ---------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, message) {
      super(message);
      this.name = 'AutonomizeApiError';
      this.status = status;
    }
  }

  // fetch has no default timeout, so a backend that accepts the connection
  // and never answers would leave a request open forever and the dashboard
  // stuck on "loading" with no way to recover.
  const TIMEOUT_MS = 15000;

  /* Endpoints where a 401 is a verdict on a credential the caller just
     supplied, not on the session carrying the request. See the 401 branch
     in request() for why the distinction matters. */
  const CREDENTIAL_CHECK_PATHS = [
    '/api/auth/login',
    '/api/auth/password/change',
    '/api/auth/password/reset',
    '/api/auth/password/set',
    '/api/auth/otp/verify',
    '/api/auth/email/verify',
    '/api/me/account',
  ];

  async function request(path, options) {
    options = options || {};
    const controller = new AbortController();
    const timer = setTimeout(function () { controller.abort(); }, TIMEOUT_MS);

    const headers = Object.assign({}, options.headers);
    if (options.body) headers['Content-Type'] = 'application/json';
    if (token) headers['Authorization'] = 'Bearer ' + token;

    try {
      const response = await fetch(config.backendUrl + path, {
        method: options.method || 'GET',
        headers: headers,
        body: options.body ? JSON.stringify(options.body) : undefined,
        signal: controller.signal,
      });

      if (response.status === 401) {
        // Two very different things arrive as 401, and conflating them
        // logs people out for typing a password wrong.
        //
        //   - On an ordinary read, 401 means the SESSION is dead: expired,
        //     revoked, or signed out elsewhere. Clearing the token is
        //     right; retrying a credential that will never work again is
        //     not.
        //
        //   - On an endpoint that VERIFIES A SUPPLIED CREDENTIAL, 401
        //     means that credential was wrong. The session is perfectly
        //     valid — it is how the request was authorised in the first
        //     place. Clearing the token here destroyed a live session
        //     every time someone mistyped their current password, and the
        //     UI then reported "session expired", which is both wrong and
        //     impossible to act on.
        if (CREDENTIAL_CHECK_PATHS.some(function (p) { return path.indexOf(p) === 0; })) {
          const detail = await response.json().catch(function () { return {}; });
          throw new ApiError(401, typeof detail.detail === 'string'
            ? detail.detail : 'That did not match our records.');
        }
        clearIdentity();
        throw new ApiError(401, 'Session expired — reconnecting on next load.');
      }
      if (!response.ok) {
        throw new ApiError(response.status, 'Request failed: HTTP ' + response.status);
      }
      return await response.json();
    } catch (error) {
      if (error.name === 'AbortError') {
        throw new ApiError(0, 'The backend did not respond within 15 seconds.');
      }
      if (error instanceof ApiError) throw error;
      // A TypeError from fetch is the browser refusing to connect at all:
      // server down, wrong port, or CORS. Worth naming, because "failed to
      // fetch" sends people looking in the wrong place.
      throw new ApiError(0,
        'Cannot reach the backend at ' + config.backendUrl +
        '. Is it running, and does its CORS allow this page?');
    } finally {
      clearTimeout(timer);
    }
  }

  // ---------------------------------------------------------------------
  // Identity
  // ---------------------------------------------------------------------

  /* WHERE THE IDENTITY LIVES, AND WHY IT IS NOT ALWAYS localStorage
     ----------------------------------------------------------------
     The extension keeps its session token in `chrome.storage.local`
     (see extension/background.js). A page served over http:// cannot
     read that store at all, so it keeps its own copy in localStorage.

     Those are two different stores, and therefore two different users:
     the extension uploads sessions as one identity while the dashboard
     reads as another, and the dashboard sits empty forever while every
     upload reports success. That is the worst possible failure — it
     looks like a broken dashboard and is actually a healthy system
     wired to the wrong account.

     So when `chrome.storage` IS reachable — the dashboard opened from
     inside the extension, at a chrome-extension:// URL — it is used,
     and the two share one identity with no configuration at all. The
     localStorage path stays as the fallback for the http:// case, where
     the two must be paired once by hand (see START-HERE.md).

     The React client used to solve the same problem in its own
     chromeStorage.ts; that dashboard is gone, and this is now the only
     implementation. */
  const hasChromeStorage =
    typeof chrome !== 'undefined' && !!(chrome.storage && chrome.storage.local);

  function clearIdentity() {
    token = null;
    if (hasChromeStorage) {
      chrome.storage.local.remove([config.tokenKey, config.userKey]);
      return;
    }
    try {
      localStorage.removeItem(config.tokenKey);
      localStorage.removeItem(config.userKey);
    } catch (_) { /* private browsing */ }
  }

  function decode(raw) {
    if (raw == null) return null;
    if (typeof raw !== 'string') return raw;
    // The React dashboard and the extension both JSON-encode their
    // values. Accept a bare string too, so a token pasted in by hand
    // during pairing still works.
    try { return JSON.parse(raw); } catch (_) { return raw; }
  }

  function readStored(key) {
    try {
      return decode(localStorage.getItem(key));
    } catch (_) { return null; }
  }

  /* chrome.storage is callback-based, so identity has to be read before
     anything else runs rather than during it. `connect()` awaits this. */
  async function loadIdentity() {
    if (!hasChromeStorage) {
      return { token: readStored(config.tokenKey), userId: readStored(config.userKey) };
    }
    const stored = await chrome.storage.local.get([config.tokenKey, config.userKey]);
    return {
      token: decode(stored[config.tokenKey]),
      userId: decode(stored[config.userKey])
    };
  }

  function store(key, value) {
    if (hasChromeStorage) {
      // Written raw, not JSON-encoded: background.js reads these keys
      // directly and would otherwise get a quoted string back.
      const patch = {}; patch[key] = value;
      chrome.storage.local.set(patch);
      return;
    }
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
  }

  /**
   * Establishes an identity, reusing a stored one when it is still valid.
   *
   * The dashboard does NOT choose who it is. It either presents a token it
   * was already given, or asks the server for a device identity and is
   * told. That is what closed the IDOR this project used to have, and it
   * is why `user_id` in any request below is advisory — the server derives
   * identity from the bearer token and ignores the field.
   */
  async function connect(options) {
    Object.assign(config, options || {});

    const identity = await loadIdentity();
    token = identity.token;
    userId = identity.userId;

    if (token) {
      try {
        const me = await request('/api/auth/me');
        userId = me.user.user_id;
        store(config.userKey, userId);
        emit('status', { connected: true, user: me.user, source: 'stored' });
        return me.user;
      } catch (error) {
        // 401 already cleared the token; anything else (server down) is
        // worth surfacing rather than silently registering a NEW device,
        // which would orphan the user's existing history.
        if (error.status !== 401) {
          emit('error', error);
          throw error;
        }
      }
    }

    const device = await request('/api/auth/device', { method: 'POST' });
    token = device.access_token;
    userId = device.user.user_id;
    store(config.tokenKey, token);
    store(config.userKey, userId);
    emit('status', { connected: true, user: device.user, source: 'device' });
    return device.user;
  }

  async function signIn(email, password) {
    const body = await request('/api/auth/login', {
      method: 'POST', body: { email: email, password: password },
    });
    token = body.access_token;
    userId = body.user.user_id;
    store(config.tokenKey, token);
    store(config.userKey, userId);
    emit('status', { connected: true, user: body.user, source: 'login' });
    return body.user;
  }

  async function register(email, password, displayName) {
    const body = await request('/api/auth/register', {
      method: 'POST',
      body: { email: email, password: password, display_name: displayName },
    });
    token = body.access_token;
    userId = body.user.user_id;
    store(config.tokenKey, token);
    store(config.userKey, userId);
    emit('status', { connected: true, user: body.user, source: 'register' });
    return body.user;
  }

  async function signOut() {
    try { await request('/api/auth/logout', { method: 'POST' }); } catch (_) {}
    clearIdentity();
    stopPolling();
    emit('status', { connected: false });
  }

  // ---------------------------------------------------------------------
  // Full auth surface — every screen the dashboard needs.
  //
  // These are thin wrappers over backend/main.py routes and add NO logic.
  // That is the point: the server owns the OTP attempt caps, the purpose
  // binding, the token rotation and the reuse detection. A frontend that
  // re-implemented any of it would be a second, weaker authority, and the
  // weaker one is the one an attacker uses.
  // ---------------------------------------------------------------------

  /** Adopts a session response as the current identity. */
  function adopt(body, source) {
    token = body.access_token;
    userId = body.user.user_id;
    store(config.tokenKey, token);
    store(config.userKey, userId);
    emit('status', { connected: true, user: body.user, source: source });
    return body;
  }

  function authConfig() {
    return request('/api/auth/config');
  }

  /** Points the client at a backend WITHOUT establishing any identity.
   *
   *  Separate from connect() on purpose: connect() registers an anonymous
   *  device account when it finds no stored token, and a device account is
   *  not a signed-in person. The dashboard's auth gate must be able to say
   *  "configure yourself, then tell me whether a REAL session exists"
   *  without that question minting an identity as a side effect. */
  function configure(options) {
    Object.assign(config, options || {});
    return config;
  }

  /** Adds a first password to an OTP/Google account. Requires a freshly
   *  emailed code — see backend/main.py's set_password for why the session
   *  alone is deliberately not enough. */
  function setPassword(code, newPassword) {
    return request('/api/auth/password/set', {
      method: 'POST', body: { code: code, new_password: newPassword },
    });
  }

  /** Requests a login code. The server answers identically for a known and
   *  an unknown address — do not "improve" the UI by distinguishing them. */
  function requestOtp(email) {
    return request('/api/auth/otp/request', { method: 'POST', body: { email: email } });
  }

  async function verifyOtp(email, code) {
    const body = await request('/api/auth/otp/verify', {
      method: 'POST', body: { email: email, code: code },
    });
    return adopt(body, 'otp').user;
  }

  function sendVerificationEmail() {
    return request('/api/auth/email/send-verification', { method: 'POST' });
  }

  function verifyEmail(email, code) {
    return request('/api/auth/email/verify', {
      method: 'POST', body: { email: email, code: code },
    });
  }

  function forgotPassword(email) {
    return request('/api/auth/password/forgot', { method: 'POST', body: { email: email } });
  }

  async function resetPassword(email, code, newPassword) {
    return request('/api/auth/password/reset', {
      method: 'POST',
      body: { email: email, code: code, new_password: newPassword },
    });
  }

  async function changePassword(currentPassword, newPassword) {
    const body = await request('/api/auth/password/change', {
      method: 'POST',
      body: { current_password: currentPassword, new_password: newPassword },
    });
    // The server keeps THIS session alive and revokes every other one, so
    // it hands back a fresh access token that must replace the old one.
    if (body.access_token) adopt(body, 'password-change');
    return body;
  }

  function logoutEverywhere() {
    return request('/api/auth/logout-everywhere', { method: 'POST' });
  }

  /** Google sign-in. The browser is redirected to Google; the backend
   *  handles state, nonce, PKCE and RS256 ID-token verification and sends
   *  the browser back here with a session. The frontend never sees, and
   *  must never trust, a decoded Google token. */
  function googleStartUrl(redirectTo) {
    return config.backendUrl + '/api/auth/google/start?redirect_to=' +
           encodeURIComponent(redirectTo || window.location.origin + window.location.pathname);
  }

  // Devices / extension linking
  /** Exchanges the session for a short-lived stream ticket. See
   *  backend/events.py for why the session token itself must not travel
   *  in the EventSource URL. */
  function streamTicket() {
    return request('/api/events/ticket', { method: 'POST' });
  }

  function streamUrl(ticket, lastEventId) {
    return config.backendUrl + '/api/events?ticket=' + encodeURIComponent(ticket) +
      (lastEventId ? '&last_event_id=' + encodeURIComponent(lastEventId) : '');
  }

  /** Institution view. Admin-only server side; a student gets a 403 and
   *  the UI never links to it in the first place. */
  function fetchCohort() { return request('/api/admin/cohort'); }

  function devices() { return request('/api/devices'); }
  function renameDevice(deviceId, label) {
    return request('/api/devices/' + encodeURIComponent(deviceId),
                   { method: 'PATCH', body: { label: label } });
  }
  function revokeDevice(deviceId) {
    return request('/api/devices/' + encodeURIComponent(deviceId), { method: 'DELETE' });
  }
  /** Completes extension linking with the 6-character code the popup shows.
   *  Requires this authenticated session — the code alone is worthless. */
  function completeLink(code) {
    return request('/api/devices/link/complete', { method: 'POST', body: { code: code } });
  }

  function deleteAccount(confirmText, password) {
    return request('/api/me/account', {
      method: 'DELETE',
      body: { confirm: confirmText, password: password || null },
    });
  }

  /** True when a stored credential resolves to a REAL account rather than
   *  the anonymous device identity `connect()` mints. The gate in front of
   *  the dashboard keys off this. */
  async function currentUser() {
    if (!token) {
      const identity = await loadIdentity();
      token = identity.token;
      userId = identity.userId;
    }
    if (!token) return null;
    try {
      const me = await request('/api/auth/me');
      userId = me.user.user_id;
      store(config.userKey, userId);
      return me.user;
    } catch (error) {
      if (error.status === 401) return null;
      throw error;
    }
  }

  // ---------------------------------------------------------------------
  // Reads
  // ---------------------------------------------------------------------

  function score() {
    return request('/api/score?user_id=' + encodeURIComponent(userId || ''));
  }

  function sessions(limit) {
    return request('/api/sessions?user_id=' + encodeURIComponent(userId || '') +
                   '&limit=' + (limit || 50));
  }

  function profile() {
    return request('/api/auth/me');
  }

  // ---------------------------------------------------------------------
  // Writes — every button a settings screen or profile menu needs
  // ---------------------------------------------------------------------

  function getSettings() {
    return request('/api/me/settings');
  }

  /**
   * Saves a PARTIAL settings update.
   *
   * Returns the server's merged, normalised result, and callers should
   * render THAT rather than what they sent: the server rewrites
   * `https://Example.com/` to `example.com`, and the normalised form is
   * what the content script matches against. A UI that kept its own copy
   * would show the user an exclusion that never fires.
   */
  function saveSettings(partial) {
    return request('/api/me/settings', { method: 'PUT', body: partial });
  }

  function updateProfile(displayName) {
    return request('/api/me/profile',
                   { method: 'PATCH', body: { display_name: displayName } });
  }

  function exportData() {
    return request('/api/me/export');
  }

  function deleteAllData() {
    return request('/api/me/data', { method: 'DELETE' });
  }

  function labelSession(sessionId, understood, usedAi) {
    return request('/api/session/label', {
      method: 'POST',
      body: { session_id: sessionId, understood: understood, used_ai: !!usedAi },
    });
  }

  function health() {
    return request('/api/health');
  }

  // ---------------------------------------------------------------------
  // Polling
  // ---------------------------------------------------------------------

  async function refresh() {
    try {
      const [s, list] = await Promise.all([score(), sessions(400)]);
      lastScore = s;
      lastSessions = list.sessions || [];
      emit('data', { score: lastScore, sessions: lastSessions });
      emit('status', { connected: true, online: true });
      return lastScore;
    } catch (error) {
      // The previous data stays on screen. Blanking the dashboard because
      // one poll failed turns a momentary blip into "all your data is
      // gone", which is a much worse lie than a slightly stale number.
      emit('error', error);
      emit('status', { connected: true, online: false, message: error.message });
      return null;
    }
  }

  function startPolling(ms) {
    stopPolling();
    refresh();
    pollTimer = setInterval(refresh, ms || config.pollMs);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // ---------------------------------------------------------------------
  // Declarative binding
  // ---------------------------------------------------------------------

  /**
   * Every value a dashboard is likely to show, with the formatting each
   * one needs. Keys are what you put in `data-autonomize="..."`.
   *
   * Null is rendered as an em dash, never as 0. "No data yet" and "zero"
   * are different claims, and a new user seeing 0 concludes the tracking
   * is broken — or worse, believes the zero.
   */
  const FIELDS = {
    current_score: function (d) { return round(d.current_score); },
    baseline_mean: function (d) { return round(d.baseline_mean); },
    delta_vs_baseline: function (d) { return signed(d.delta_vs_baseline); },
    streak_days: function (d) { return d.streak_days != null ? d.streak_days : '—'; },
    independent_minutes_7d: function (d) { return duration(d.independent_minutes_7d); },
    assisted_minutes_7d: function (d) { return duration(d.assisted_minutes_7d); },
    independent_share: function (d) {
      const a = d.independent_minutes_7d || 0;
      const b = d.assisted_minutes_7d || 0;
      return (a + b) ? Math.round((a / (a + b)) * 100) + '%' : '—';
    },
    assessment_score: function (d) { return round(d.assessment_score); },
    assessment_baseline_mean: function (d) { return round(d.assessment_baseline_mean); },
    assessment_delta: function (d) { return signed(d.assessment_delta); },
    assessment_risk_level: function (d) { return d.assessment_risk_level || '—'; },
    assessment_explanation: function (d) { return d.assessment_explanation || ''; },
    forecast_direction: function (d) { return (d.forecast && d.forecast.direction) || '—'; },
    forecast_projected: function (d) {
      return d.forecast ? round(d.forecast.projected_score) : '—';
    },
    predicted_score: function (d) {
      return d.prediction ? round(d.prediction.predicted_score) : '—';
    },
    prediction_explanation: function (d) {
      return (d.prediction && d.prediction.explanation &&
              d.prediction.explanation.sentence) || '';
    },
    behavioural_explanation: function (d) { return d.behavioural_explanation || ''; },
    rhythm_progress: function (d) {
      const r = d.signals && d.signals.rhythm;
      return r ? r.observations + ' of ' + r.required : '—';
    },
    calibration_progress: function (d) {
      const c = d.signals && d.signals.calibration;
      return c ? c.observations + ' of ' + c.required : '—';
    },
    personalisation_message: function (d) {
      const p = d.signals && d.signals.personalisation;
      return (p && p.message) || '';
    },
    session_count: function (_d, sessions) { return sessions ? sessions.length : '—'; },
    site_count: function (_d, sessions) {
      if (!sessions) return '—';
      const domains = {};
      sessions.forEach(function (s) { if (s.domain) domains[s.domain] = 1; });
      return Object.keys(domains).length;
    },
  };

  function round(value) {
    return value == null ? '—' : Math.round(value);
  }

  function signed(value) {
    if (value == null) return '—';
    const rounded = Math.round(value * 10) / 10;
    return (rounded > 0 ? '+' : '') + rounded;
  }

  function duration(minutes) {
    if (minutes == null) return '—';
    const total = Math.round(minutes);
    const h = Math.floor(total / 60);
    const m = total % 60;
    return h ? h + 'h ' + m + 'm' : m + 'm';
  }

  /**
   * Fills every `[data-autonomize]` element and keeps them updated.
   *
   * Only textContent is written — never innerHTML. These values come from
   * the server, and while the server never stores free text, writing HTML
   * from a network response is the kind of habit that becomes a hole the
   * moment someone adds a field that does carry user input.
   */
  function bind(root) {
    const scope = root || document;

    function paint(payload) {
      const data = payload.score;
      const list = payload.sessions;
      if (!data) return;
      scope.querySelectorAll('[data-autonomize]').forEach(function (element) {
        const key = element.getAttribute('data-autonomize');
        const formatter = FIELDS[key];
        if (!formatter) {
          console.warn('[autonomize] unknown binding:', key);
          return;
        }
        element.textContent = formatter(data, list);
      });
    }

    on('data', paint);
    if (lastScore) paint({ score: lastScore, sessions: lastSessions });
    startPolling();
    return { refresh: refresh, stop: stopPolling };
  }

  function on(event, handler) {
    if (!listeners[event]) listeners[event] = [];
    listeners[event].push(handler);
    return function off() {
      listeners[event] = listeners[event].filter(function (h) { return h !== handler; });
    };
  }

  // ---------------------------------------------------------------------

  global.Autonomize = {
    connect: connect,
    signIn: signIn,
    register: register,
    signOut: signOut,

    authConfig: authConfig,
    configure: configure,
    setPassword: setPassword,
    currentUser: currentUser,
    requestOtp: requestOtp,
    verifyOtp: verifyOtp,
    sendVerificationEmail: sendVerificationEmail,
    verifyEmail: verifyEmail,
    forgotPassword: forgotPassword,
    resetPassword: resetPassword,
    changePassword: changePassword,
    logoutEverywhere: logoutEverywhere,
    googleStartUrl: googleStartUrl,

    fetchCohort: fetchCohort,
    streamTicket: streamTicket,
    streamUrl: streamUrl,

    devices: devices,
    renameDevice: renameDevice,
    revokeDevice: revokeDevice,
    completeLink: completeLink,
    deleteAccount: deleteAccount,

    score: score,
    sessions: sessions,
    profile: profile,
    health: health,

    getSettings: getSettings,
    saveSettings: saveSettings,
    updateProfile: updateProfile,
    exportData: exportData,
    deleteAllData: deleteAllData,
    labelSession: labelSession,

    bind: bind,
    refresh: refresh,
    startPolling: startPolling,
    stopPolling: stopPolling,
    on: on,

    FIELDS: FIELDS,
    get config() { return Object.assign({}, config); },
    get userId() { return userId; },
    get data() { return lastScore; },
    ApiError: ApiError,
  };
})(typeof window !== 'undefined' ? window : globalThis);
