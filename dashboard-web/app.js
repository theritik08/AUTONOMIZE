/**
 * AUTONOMIZE — dashboard-web application controller
 * ==================================================
 *
 * Owns everything the markup in index.html declares but cannot do by
 * itself: the authentication gate, hash routing between the five views,
 * every auth flow, all nine Settings sections, the Quick Status strip, and
 * the notification menu.
 *
 * WHAT THIS FILE IS NOT
 * ---------------------
 * It is not an authentication system. Every credential decision is made by
 * the FastAPI backend (backend/accounts.py, tokens.py, otp.py,
 * oauth_google.py). This file holds an access token the server issued and
 * nothing else — no password is ever stored, no session is ever minted
 * client-side, and there is no frontend flag that can substitute for a
 * valid token. `showApp()` is only ever reached after /api/auth/me has
 * confirmed the token against the server.
 *
 * It also computes no metrics. Every number on screen was calculated by
 * the backend, because the extension, this dashboard and any future client
 * must agree on what a score means — and they only can if exactly one
 * place decides.
 *
 * RELATIONSHIP TO script.js
 * -------------------------
 * script.js renders the dashboard's charts, gauge, calendar and session
 * list, and owns the three-way theme. It is untouched by this file beyond
 * being told when data arrives. This file owns navigation, identity and
 * configuration. The split is deliberate: rendering and routing failing
 * independently is much easier to diagnose than one file that does both.
 */
(function () {
  'use strict';

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(sel));
  };

  // Same default the extension ships with (background.js DEFAULT_SETTINGS).
  // Override by setting window.AUTONOMIZE_BACKEND before this script loads.
  var BACKEND = window.AUTONOMIZE_BACKEND || 'http://localhost:8787';

  var state = {
    user: null,
    authConfig: null,
    settings: null,
    score: null,
    sessions: [],
    connected: false,
    lastUpdated: null,
    pollTimer: null,
    sessionFilter: 'all',
    // Live stream
    source: null,
    liveState: 'connecting',   // connecting | live | reconnecting | offline
    lastEventId: 0,
    reconnectAttempt: 0,
    reconnectTimer: null,
    watchdogTimer: null,
    heartbeatSeconds: 20,
    refreshQueued: false,
    stoppingStream: false
  };

  // ─────────────────────────────────────────────────────────────────────
  // Small UI helpers
  // ─────────────────────────────────────────────────────────────────────

  function show(el) { if (el) el.hidden = false; }
  function hide(el) { if (el) el.hidden = true; }

  function setText(id, value) {
    var el = document.getElementById(id);
    // textContent, never innerHTML: these values come off the network, and
    // writing HTML from a network response is the habit that becomes a hole
    // the moment a field carries user input.
    if (el) el.textContent = value == null || value === '' ? '—' : String(value);
  }

  function setMsg(id, text, kind) {
    var el = document.getElementById(id);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'set-msg' + (kind ? ' is-' + kind : '');
  }

  function setError(id, message) {
    var el = document.getElementById(id);
    if (!el) return;
    if (!message) { el.hidden = true; el.textContent = ''; return; }
    el.textContent = message;
    el.hidden = false;
  }

  /** Disables a button and shows a working label, returning a restore fn.
   *  Without this a slow network invites a second submit, and a second
   *  submit on an OTP endpoint burns an attempt against the cap. */
  function busy(button, label) {
    if (!button) return function () {};
    var original = button.textContent;
    button.disabled = true;
    button.textContent = label || 'Working…';
    return function () { button.disabled = false; button.textContent = original; };
  }

  function friendly(error) {
    if (!error) return 'Something went wrong.';
    if (error.status === 0) {
      return 'Cannot reach the server. Is the backend running at ' + BACKEND + '?';
    }
    return error.message || 'Something went wrong.';
  }

  function minutes(value) {
    if (value == null) return '—';
    var total = Math.round(value);
    var h = Math.floor(total / 60);
    var m = total % 60;
    return h ? h + 'h ' + m + 'm' : m + 'm';
  }

  function initials(user) {
    if (!user) return '—';
    var source = (user.display_name || user.email || '').trim();
    if (!source) return '—';
    var parts = source.split(/[\s@._-]+/).filter(Boolean);
    if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
    return source.slice(0, 2).toUpperCase();
  }

  function relativeTime(ts) {
    if (!ts) return '—';
    var secs = Math.round((Date.now() - ts) / 1000);
    if (secs < 10) return 'just now';
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.round(secs / 60) + 'm ago';
    return Math.round(secs / 3600) + 'h ago';
  }

  // ─────────────────────────────────────────────────────────────────────
  // Routing
  //
  // Two route spaces sharing one hash, gated by authentication:
  //   #/login #/signup #/otp #/forgot   — only when signed out
  //   #/dashboard … #/settings/<sec>    — only when signed in
  //
  // The gate is enforced in route() itself rather than by hiding links, so
  // typing a private URL while signed out cannot reach a private view.
  // ─────────────────────────────────────────────────────────────────────

  var APP_VIEWS = ['dashboard', 'sessions', 'insights', 'calendar', 'settings', 'cohort'];
  var AUTH_PANES = ['login', 'signup', 'otp', 'forgot'];
  var SETTINGS_SECTIONS = [
    'profile', 'appearance', 'tracking', 'privacy',
    'security', 'devices', 'notifications', 'about'
  ];

  function parseHash() {
    var raw = (window.location.hash || '').replace(/^#\/?/, '');
    var parts = raw.split('/').filter(Boolean);
    return { head: parts[0] || '', tail: parts[1] || '' };
  }

  function route() {
    var parsed = parseHash();
    var signedIn = !!state.user;

    if (!signedIn) {
      var pane = AUTH_PANES.indexOf(parsed.head) >= 0 ? parsed.head : 'login';
      showAuth(pane);
      return;
    }

    // A signed-in user landing on an auth route is sent to the dashboard —
    // otherwise "log in" leaves them staring at the login form they just
    // completed.
    if (AUTH_PANES.indexOf(parsed.head) >= 0 || !parsed.head) {
      window.location.hash = '#/dashboard';
      return;
    }

    var view = APP_VIEWS.indexOf(parsed.head) >= 0 ? parsed.head : 'dashboard';
    showView(view);
    if (view === 'settings') {
      showSettingsSection(SETTINGS_SECTIONS.indexOf(parsed.tail) >= 0 ? parsed.tail : 'profile');
    }
    if (view === 'cohort') loadCohort();
  }

  function showView(view) {
    $$('.view').forEach(function (el) {
      el.hidden = el.getAttribute('data-view') !== view;
    });
    $$('.nav-link').forEach(function (link) {
      var on = link.getAttribute('data-route') === view;
      link.classList.toggle('is-active', on);
      if (on) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    closeMenus();
    window.scrollTo({ top: 0, behavior: 'auto' });
  }

  function showSettingsSection(section) {
    $$('.set-sec').forEach(function (el) {
      el.hidden = el.getAttribute('data-sec') !== section;
    });
    $$('.set-nav-link').forEach(function (link) {
      link.classList.toggle('is-active', link.getAttribute('data-sec') === section);
    });
    // Loaded on demand: the device list and active-session list are extra
    // round trips that most Settings visits never need.
    if (section === 'devices' || section === 'security') loadDevices();
    if (section === 'about') paintAbout();
  }

  // ─────────────────────────────────────────────────────────────────────
  // Auth gate
  // ─────────────────────────────────────────────────────────────────────

  function showAuth(pane) {
    hide($('#bootGate'));
    show($('#authWrap'));
    // The private views are not merely hidden — the whole app shell is
    // taken out of the document flow, and no data fetch is running,
    // because polling is only started by showApp().
    $$('.view').forEach(hide);
    var topbar = $('.topbar');
    if (topbar) topbar.hidden = true;
    var main = $('#main');
    if (main) main.hidden = true;

    $$('.auth-pane').forEach(function (el) {
      el.hidden = el.getAttribute('data-pane') !== pane;
    });
    if (pane === 'login') setError('loginError', null);
    if (pane === 'signup') setError('signupError', null);
  }

  function showApp() {
    hide($('#bootGate'));
    hide($('#authWrap'));
    var topbar = $('.topbar');
    if (topbar) topbar.hidden = false;
    var main = $('#main');
    if (main) main.hidden = false;
    paintIdentity();
    applyRole();
    startPolling();
    openStream();
  }

  /** Adopts a server-issued session and enters the app. */
  function onSignedIn(user) {
    state.user = user;
    window.location.hash = '#/dashboard';
    showApp();
    route();
    loadSettings();
  }

  async function signOut() {
    stopPolling();
    closeStream();
    state.lastEventId = 0;
    try {
      await Autonomize.signOut();
    } catch (_) {
      // The server-side revoke is what actually kills the session, but a
      // network failure must not strand someone in a UI they cannot leave.
    }
    state.user = null;
    state.score = null;
    state.sessions = [];
    window.location.hash = '#/login';
    showAuth('login');
  }

  /**
   * Handles a token the server has stopped accepting.
   *
   * Expiry is not an error to report — it is a state to return to. Anything
   * else leaves a signed-out user looking at stale private data.
   */
  function onUnauthorized() {
    if (!state.user) return;
    stopPolling();
    closeStream();
    state.lastEventId = 0;
    state.user = null;
    window.location.hash = '#/login';
    showAuth('login');
    setError('loginError', 'Your session expired. Please sign in again.');
  }

  // ─────────────────────────────────────────────────────────────────────
  // Identity painting
  // ─────────────────────────────────────────────────────────────────────

  function paintIdentity() {
    var user = state.user;
    if (!user) return;
    var name = user.display_name || (user.email || '').split('@')[0] || 'Signed in';

    setText('navInitials', initials(user));
    setText('menuInitials', initials(user));
    setText('menuName', name);
    setText('menuEmail', user.email || '');
    setText('setAvatar', initials(user));
    setText('setAvatarName', name);
    setText('setAvatarMail', user.email || '');

    var nameInput = $('#setName');
    if (nameInput && document.activeElement !== nameInput) {
      nameInput.value = user.display_name || '';
    }
    var emailInput = $('#setEmail');
    if (emailInput) emailInput.value = user.email || '';

    paintVerification();
    paintPasswordSection();
  }

  function paintVerification() {
    var user = state.user;
    var badge = $('#verifyBadge');
    if (!badge || !user) return;
    if (user.email_verified) {
      badge.textContent = 'Verified';
      badge.className = 'badge badge-ok';
      hide($('#btnSendVerify'));
      hide($('#verifyCodeRow'));
    } else {
      badge.textContent = 'Not verified';
      badge.className = 'badge badge-warn';
      // Only offer this at all if mail actually works. A button that
      // silently does nothing is worse than not offering the option.
      if (state.authConfig && state.authConfig.email_verification) {
        show($('#btnSendVerify'));
        // The code entry is shown IMMEDIATELY, not behind the button.
        // Registration already mails a verification code, so an unverified
        // account always has one outstanding — hiding the input until the
        // user presses "send" meant the only way to reach it was to
        // request a second code, which the server's resend cooldown
        // correctly refuses. The button is therefore a resend, not a
        // prerequisite.
        show($('#verifyCodeRow'));
        var send = $('#btnSendVerify');
        if (send) send.textContent = 'Resend code';
      } else {
        hide($('#btnSendVerify'));
        hide($('#verifyCodeRow'));
      }
    }
  }

  /** An OTP-only account has no password, so it needs "set" rather than
   *  "change" — and asking for a current password it does not have would be
   *  an unanswerable form. */
  function paintPasswordSection() {
    var user = state.user;
    if (!user) return;
    var heading = $('#pwHeading');
    var currentField = $('#curPwField');
    var button = $('#btnChangePw');
    var codeField = $('#setPwCodeField');
    if (user.has_password) {
      if (heading) heading.textContent = 'Change password';
      show(currentField);
      hide(codeField);
      if (button) button.textContent = 'Update password';
    } else {
      if (heading) heading.textContent = 'Set a password';
      hide(currentField);
      show(codeField);
      if (button) button.textContent = 'Set password';
    }
  }


  // ─────────────────────────────────────────────────────────────────────
  // Real-time activity stream
  //
  // Server-sent events (see backend/events.py for why SSE and not
  // WebSockets). The stream is a HINT that something changed, never the
  // data itself: on an event the dashboard reconciles by fetching from the
  // REST API, so a dropped or duplicated event costs nothing. That is what
  // makes reconnection safe without an event log to replay.
  //
  // Polling still exists but is demoted to a slow safety net, not the
  // delivery mechanism — it covers the case where the stream cannot be
  // established at all (a proxy that buffers text/event-stream, a
  // multi-worker deployment without a shared broker).
  // ─────────────────────────────────────────────────────────────────────

  var LIVE_LABELS = {
    connecting: 'Connecting…',
    live: 'Live',
    reconnecting: 'Reconnecting…',
    offline: 'Offline'
  };

  function setLiveState(next) {
    if (state.liveState === next) return;
    state.liveState = next;
    var pill = $('#livePill');
    if (pill) pill.setAttribute('data-state', next);
    setText('liveLabel', LIVE_LABELS[next] || next);
    paintStatusBar();
  }

  /** Coalesces bursts of events into one reconcile.
   *
   *  A tab flushing several sessions at once would otherwise fire a fetch
   *  per event. The delay is short enough to stay well inside "within
   *  seconds" and long enough that a burst costs one round trip. */
  function queueRefresh() {
    if (state.refreshQueued) return;
    state.refreshQueued = true;
    setTimeout(function () {
      state.refreshQueued = false;
      refresh();
    }, 250);
  }

  function clearWatchdog() {
    if (state.watchdogTimer) { clearTimeout(state.watchdogTimer); state.watchdogTimer = null; }
  }

  /** Distinguishes a live-but-quiet connection from a dead socket.
   *
   *  A TCP connection can die without either side noticing — no error
   *  event ever fires, and EventSource happily reports itself open
   *  forever. The server heartbeats on a known cadence, so silence past
   *  two intervals means the connection is gone regardless of what the
   *  browser claims. */
  function armWatchdog() {
    clearWatchdog();
    state.watchdogTimer = setTimeout(function () {
      if (!state.user) return;
      setLiveState('reconnecting');
      restartStream();
    }, state.heartbeatSeconds * 2 * 1000 + 5000);
  }

  function closeStream() {
    clearWatchdog();
    if (state.reconnectTimer) { clearTimeout(state.reconnectTimer); state.reconnectTimer = null; }
    if (state.source) {
      state.stoppingStream = true;
      try { state.source.close(); } catch (_) {}
      state.source = null;
      state.stoppingStream = false;
    }
  }

  function restartStream() {
    closeStream();
    if (state.user) scheduleReconnect();
  }

  /** Exponential backoff, capped. A backend that is down must not be
   *  hammered by every open dashboard, and a user watching the page should
   *  still see it recover on its own within a reasonable time. */
  function scheduleReconnect() {
    if (state.reconnectTimer) return;
    state.reconnectAttempt += 1;
    var delay = Math.min(30000, 1000 * Math.pow(2, Math.min(state.reconnectAttempt, 5)));
    state.reconnectTimer = setTimeout(function () {
      state.reconnectTimer = null;
      openStream();
    }, delay);
  }

  async function openStream() {
    if (!state.user || state.source) return;
    if (navigator.onLine === false) {
      setLiveState('offline');
      return;   // the 'online' listener re-opens it
    }
    if (typeof window.EventSource !== 'function') {
      // No SSE support. The polling fallback is the whole story here, and
      // saying "Live" would be a lie.
      setLiveState('offline');
      return;
    }

    var ticket;
    try {
      var body = await Autonomize.streamTicket();
      ticket = body.ticket;
      if (body.heartbeat_seconds) state.heartbeatSeconds = body.heartbeat_seconds;
    } catch (error) {
      if (error && error.status === 401) { onUnauthorized(); return; }
      setLiveState(state.reconnectAttempt > 2 ? 'offline' : 'reconnecting');
      scheduleReconnect();
      return;
    }

    var source;
    try {
      source = new EventSource(Autonomize.streamUrl(ticket, state.lastEventId));
    } catch (_) {
      setLiveState('offline');
      scheduleReconnect();
      return;
    }
    state.source = source;

    source.addEventListener('ready', function (e) {
      state.reconnectAttempt = 0;
      setLiveState('live');
      armWatchdog();
      var payload = parseEvent(e);
      // The server tells us whether anything happened while we were away.
      // We reconcile through the API rather than replaying, because there
      // is no durable event log to replay from — and inventing one would
      // be a durability claim this bus deliberately does not make.
      if (payload && payload.data && payload.data.missed_events) queueRefresh();
      if (payload && typeof payload.id === 'number') state.lastEventId = payload.id;
    });

    source.addEventListener('activity', function (e) {
      armWatchdog();
      var payload = parseEvent(e);
      if (!payload) return;
      // Duplicate suppression. The browser replays from Last-Event-ID on
      // reconnect, so the same event can legitimately arrive twice.
      if (typeof payload.id === 'number') {
        if (payload.id <= state.lastEventId) return;
        state.lastEventId = payload.id;
      }
      applyLiveActivity(payload.data || {});
      queueRefresh();
    });

    source.addEventListener('desync', function () {
      // The server dropped events from our backlog. Everything on screen
      // is suspect; reload it wholesale rather than patching.
      armWatchdog();
      queueRefresh();
    });

    source.onopen = function () {
      state.reconnectAttempt = 0;
      setLiveState('live');
      armWatchdog();
    };

    source.onerror = function () {
      if (state.stoppingStream) return;
      // EventSource retries on its own, but our ticket is single-use and
      // short-lived, so its retry would present an expired credential.
      // Take the connection over rather than letting it loop on 401s.
      setLiveState(state.reconnectAttempt > 2 ? 'offline' : 'reconnecting');
      closeStream();
      scheduleReconnect();
    };
  }

  function parseEvent(e) {
    try { return JSON.parse(e.data); } catch (_) { return null; }
  }

  /**
   * Immediate, optimistic paint from the event itself.
   *
   * The authoritative numbers arrive a moment later from the reconcile
   * fetch. This exists so the visible state — which site, which category,
   * "tracking now" — changes the instant activity arrives rather than one
   * round trip later. Only fields the privacy model already permits are
   * present; the server allow-lists them before they are sent.
   */
  function applyLiveActivity(data) {
    state.lastUpdated = Date.now();
    setText('qsSession', 'Tracking now');
    var dot = $('#qsDot');
    if (dot) dot.className = 'status-dot is-live';
    if (data.domain) {
      setText('qsSite', data.domain +
        (data.category ? ' · ' + String(data.category).replace('_', ' ') : ''));
    }
    setText('qsUpdated', 'Updated just now');
  }

  // ─────────────────────────────────────────────────────────────────────
  // Data loading + Quick Status
  // ─────────────────────────────────────────────────────────────────────

  /* Polling is a SAFETY NET, not the delivery mechanism.
     Live updates arrive over SSE within a second or two; this only covers
     the cases where a stream cannot be established at all — a proxy that
     buffers text/event-stream, a browser without EventSource, or a
     multi-worker deployment with no shared broker (see events.py). Hence a
     slow default: polling faster would be using bandwidth to paper over a
     stream that is either working or genuinely unavailable. */
  var DEFAULT_POLL_MS = 120000;

  function pollInterval() {
    var stored = parseInt(
      window.localStorage.getItem('autonomize_poll') || String(DEFAULT_POLL_MS), 10);
    return isNaN(stored) ? DEFAULT_POLL_MS : stored;
  }

  function startPolling() {
    stopPolling();
    refresh();
    var ms = pollInterval();
    if (ms > 0) state.pollTimer = setInterval(refresh, ms);
  }

  /** The browser suspends timers and often drops sockets in a background
   *  tab. Coming back to a stale dashboard that says "Live" would be the
   *  worst of both, so returning to the tab forces a reconcile and, if the
   *  stream died while away, a reconnect. */
  function wireVisibility() {
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible' || !state.user) return;
      refresh();
      if (!state.source) restartStream();
    });

    /* Connectivity events, because the watchdog alone is too slow here.
     *
     * A socket that dies when the network drops usually dies SILENTLY —
     * no error event fires, and EventSource keeps reporting itself open.
     * The heartbeat watchdog does eventually catch that, but it has to
     * wait out two heartbeat intervals before it can distinguish a dead
     * connection from a quiet one, and showing "Live" for the better part
     * of a minute after connectivity is gone is exactly the stale-but-
     * confident state this indicator exists to prevent.
     *
     * `offline`/`online` fire immediately and are unambiguous, so they
     * cover the common case; the watchdog remains for the one they miss —
     * the network is fine but the connection is dead anyway (a proxy or
     * load balancer dropped it). */
    window.addEventListener('offline', function () {
      if (!state.user) return;
      setLiveState('offline');
      state.connected = false;
      paintStatusBar();
      closeStream();
    });

    window.addEventListener('online', function () {
      if (!state.user) return;
      setLiveState('reconnecting');
      // Reset the backoff: this is a known-good moment to retry, not the
      // next step of a failing sequence.
      state.reconnectAttempt = 0;
      closeStream();
      openStream();
      refresh();
    });
  }

  function stopPolling() {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  }

  async function refresh() {
    if (!state.user) return;

    // ONLY the fetch is guarded here.
    //
    // Wrapping the render in the same try/catch conflates two unrelated
    // failures: a backend that is unreachable, and a bug in our own
    // painting code. That is not a theoretical tidiness point — it hid a
    // real one. A TypeError thrown while painting was caught by this
    // handler, reported as "disconnected", and left every later paint step
    // unrun, so the Sessions page stayed empty while the network was
    // perfectly healthy and the console stayed silent.
    var score;
    var list;
    try {
      score = await Autonomize.score();
      list = await Autonomize.sessions(400);
    } catch (error) {
      if (error && error.status === 401) { onUnauthorized(); return; }
      // The last good data stays on screen behind a Disconnected pill.
      // Blanking every card because one poll failed turns a momentary blip
      // into "all your data is gone", a far worse lie than a stale number.
      state.connected = false;
      paintStatusBar();
      return;
    }

    state.score = score;
    state.sessions = list.sessions || [];
    state.connected = true;
    state.lastUpdated = Date.now();

    // Deliberately unguarded: a render bug should reach the console and
    // the test suite, not be silently downgraded to "offline".
    paintQuickStatus();
    paintStatusBar();
    paintSessions();
    paintInsights();
    paintExplanation();
    paintReadiness();
    paintNotifications();
    // Hand the same payload to the renderer that owns the charts, so there
    // is one fetch and one source of truth rather than two clients polling
    // the same endpoints on different schedules.
    if (window.AutonomizeRender) window.AutonomizeRender(score, state.sessions);
  }

  /**
   * Quick Status — the whole point is that "how am I doing right now" is
   * readable in two seconds.
   *
   * Nulls render as an em dash, never as 0. "No data yet" and "zero" are
   * different claims, and a new user reading 0 concludes either that the
   * tracking is broken or — worse — believes it.
   */
  function paintQuickStatus() {
    var d = state.score;
    if (!d) return;

    setText('qsScore', d.current_score == null ? '—' : Math.round(d.current_score));

    var delta = d.delta_vs_baseline;
    var deltaEl = $('#qsDelta');
    if (deltaEl) {
      if (delta == null) {
        deltaEl.textContent = 'No personal baseline yet';
        deltaEl.className = 'qs-delta';
      } else {
        var rounded = Math.round(delta * 10) / 10;
        deltaEl.textContent = (rounded > 0 ? '+' : '') + rounded + ' vs. your baseline';
        deltaEl.className = 'qs-delta ' + (rounded >= 0 ? 'is-up' : 'is-down');
      }
    }

    var ind = d.independent_minutes_7d || 0;
    var ast = d.assisted_minutes_7d || 0;
    var total = ind + ast;
    setText('qsIndependent', total ? Math.round((ind / total) * 100) + '%' : '—');
    setText('qsAssisted', total ? Math.round((ast / total) * 100) + '%' : '—');

    // "On track" is the share of recent scored sessions at or above the
    // personal baseline — an honest, derivable quantity. With no baseline
    // there is nothing to be on track against, and it says so.
    var onTrack = '—';
    if (d.baseline_mean != null && state.sessions.length) {
      var scored = state.sessions.filter(function (s) {
        return s.category === 'writing' && s.score != null;
      });
      if (scored.length) {
        var good = scored.filter(function (s) { return s.score >= d.baseline_mean; }).length;
        onTrack = Math.round((good / scored.length) * 100) + '%';
      }
    }
    setText('qsOnTrack', onTrack);

    var todayStr = new Date().toISOString().slice(0, 10);
    var todayMs = state.sessions.reduce(function (sum, s) {
      var day = s.date || (s.started_at
        ? new Date(s.started_at).toISOString().slice(0, 10) : null);
      return day === todayStr ? sum + (s.active_ms || 0) : sum;
    }, 0);
    setText('qsToday', todayMs ? minutes(todayMs / 60000) : '0m');

    paintLiveSession();

    // Honest about a baseline that is still forming, while still showing
    // whatever score does exist — see the ML/scoring note in Settings.
    var signals = d.signals || {};
    var personal = signals.personalisation || {};
    var baselineEl = $('#qsBaseline');
    if (baselineEl) {
      if (d.baseline_mean == null) {
        baselineEl.textContent = personal.message ||
          'Building your baseline — a few more sessions are needed before a ' +
          'personal trend means anything.';
        baselineEl.hidden = false;
      } else {
        baselineEl.hidden = true;
      }
    }
  }

  function paintLiveSession() {
    var latest = state.sessions[0];
    var dot = $('#qsDot');
    // "Active" means a session was updated within the last five minutes —
    // one flush interval plus slack. Anything older is a past session, and
    // calling it current would be a claim the data does not support.
    var fresh = latest && latest.updated_at && (Date.now() - latest.updated_at) < 5 * 60_000;

    setText('qsSession', fresh ? 'Tracking now' : 'No active session');
    setText('qsSite', latest ? (latest.domain || '—') +
      (latest.category ? ' · ' + latest.category.replace('_', ' ') : '') : '—');
    setText('qsUpdated', state.lastUpdated
      ? 'Updated ' + relativeTime(state.lastUpdated) : '—');
    if (dot) dot.className = 'status-dot' + (fresh ? ' is-live' : '');
  }

  function paintStatusBar() {
    var connected = state.connected;
    var live = state.liveState;
    setText('sbConnection', connected
      ? (live === 'live'
          ? 'Live — updates arrive as they happen'
          : live === 'reconnecting'
            ? 'Reconnecting to the live stream…'
            : 'Connected to ' + BACKEND + ' (periodic refresh only)')
      : 'Disconnected — showing the last known data');
    var dot = $('#sbDot');
    if (dot) dot.className = 'status-dot' + (connected ? '' : ' is-off');

    var footDot = $('#footDot');
    if (footDot) footDot.className = 'status-dot' + (connected ? '' : ' is-off');
    setText('footStatus', connected ? 'Connected' : 'Disconnected');

    var tracking = state.settings && state.settings.tracking;
    if (tracking) {
      var on = Object.keys(tracking).filter(function (k) { return tracking[k]; });
      setText('sbTracking', 'Tracking ' + on.length + ' of 3 categories');
    }

    var hasDevice = state.sessions.some(function (s) { return s.domain; });
    setText('sbExtension', hasDevice ? 'Extension reporting' : 'No extension data yet');
  }

  function paintInsights() {
    var d = state.score;
    if (!d) return;

    var forecast = d.forecast || {};
    var note = $('#trendNote');
    if (note) {
      note.textContent = forecast.explanation || forecast.direction
        ? (forecast.explanation ||
           ('Trend: ' + forecast.direction +
            (forecast.projected_score != null
              ? ' — projected ' + Math.round(forecast.projected_score) + ' in 7 days' : '')))
        : 'Not enough sessions yet to project a trend.';
    }
    setText('trendFit', forecast.fit == null ? '' : 'fit ' + Math.round(forecast.fit * 100) + '%');

    var riskNote = $('#riskNote');
    if (riskNote) {
      riskNote.textContent = d.assessment_delta == null
        ? 'No graded sessions recorded yet.'
        : (d.assessment_delta > 0 ? '+' : '') +
          (Math.round(d.assessment_delta * 10) / 10) + ' vs. your exam baseline';
    }
    var meter = $('#riskMeter');
    if (meter) {
      meter.style.width =
        (d.assessment_score == null ? 0 : Math.round(d.assessment_score)) + '%';
    }
    var badge = $('#riskBadge');
    if (badge && d.assessment_risk_level) {
      badge.className = 'badge badge-' +
        (d.assessment_risk_level === 'low' ? 'ok'
          : d.assessment_risk_level === 'medium' ? 'warn' : 'risk');
    }
  }


  // ───────────────────────────────────────────────────────────────────
  // Score explanation and signal readiness.
  //
  // Ported from the React dashboard when the two were consolidated. The
  // rule they exist to serve: the score must be able to explain itself,
  // and a signal that is not measuring yet must say so rather than look
  // like a signal that says "you are fine".
  // ───────────────────────────────────────────────────────────────────

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function paintExplanation() {
    var host = $('#explainBody');
    if (!host) return;
    var d = state.score || {};
    host.innerHTML = '';

    var said = false;

    if (d.behavioural_explanation) {
      host.appendChild(el('p', 'explain-line', d.behavioural_explanation));
      said = true;
    }

    var personal = (d.signals || {}).personalisation;
    if (personal && personal.message) {
      host.appendChild(el('p', 'explain-line explain-muted', personal.message));
      said = true;
    }

    var risk = d.dependency_risk;
    if (risk && risk.summary) {
      host.appendChild(el('p', 'explain-line', risk.summary));
      // Carried verbatim from the backend and never paraphrased: this is
      // the sentence that keeps a behavioural estimate from being read as
      // an accusation.
      if (risk.not_proof) {
        host.appendChild(el('p', 'explain-caveat', risk.not_proof));
      }
      said = true;
    }

    var model = (d.signals || {}).model;
    if (model && model.available === false && model.reason) {
      host.appendChild(el('p', 'explain-line explain-muted',
        'No trained model is in use — ' + model.reason + '. The score comes from your ' +
        'own baseline only.'));
      said = true;
    }

    if (d.prediction && d.prediction.predicted_score != null) {
      var p = d.prediction;
      var line = 'Next-session estimate: ' + Math.round(p.predicted_score);
      if (p.interval_low != null && p.interval_high != null) {
        line += ' (typically ' + Math.round(p.interval_low) + '–' +
                Math.round(p.interval_high) + ')';
      }
      host.appendChild(el('p', 'explain-line', line));
      if (p.interval_coverage_measured != null) {
        // The MEASURED coverage, not the nominal target. Reporting the
        // target as though it were an observed property would be exactly
        // the invented-accuracy claim this project refuses to make.
        host.appendChild(el('p', 'explain-caveat',
          'That range has actually contained the outcome ' +
          Math.round(p.interval_coverage_measured * 100) + '% of the time so far on ' +
          'your own sessions.'));
      }
      said = true;
    }

    if (!said) {
      host.appendChild(el('p', 'explain-line explain-muted',
        'Not enough sessions yet to explain a change. Your first few sessions build ' +
        'the baseline everything else is measured against.'));
    }
  }

  function paintReadiness() {
    var list = $('#readinessList');
    if (!list) return;
    var signals = (state.score || {}).signals || {};
    list.innerHTML = '';

    [
      {
        key: 'rhythm', name: 'Typing rhythm', signal: signals.rhythm,
        ready: 'Compared against your usual pace — never against anyone else.',
        waiting: 'Learning how you normally type. Until then this signal does not ' +
                 'affect your score at all.'
      },
      {
        key: 'calibration', name: 'Score calibration', signal: signals.calibration,
        ready: 'Flags only work that is rarer than your own recent range.',
        waiting: 'Building a picture of your usual range. Nothing is flagged this ' +
                 'way yet.'
      }
    ].forEach(function (row) {
      var signal = row.signal;
      if (!signal) return;
      var have = signal.observations || 0;
      var need = signal.required || 0;
      var ready = !!signal.ready;
      var pct = need ? Math.min(100, Math.round((have / need) * 100)) : 0;

      var li = el('li', 'readiness-row');
      var head = el('div', 'readiness-head');
      head.appendChild(el('strong', null, row.name));
      head.appendChild(el('span', 'readiness-count',
        ready ? 'Active' : have + ' of ' + need + ' sessions'));
      li.appendChild(head);

      var track = el('div', 'readiness-track');
      var fill = el('span', 'readiness-fill' + (ready ? ' is-ready' : ''));
      fill.style.width = (ready ? 100 : pct) + '%';
      track.appendChild(fill);
      li.appendChild(track);

      li.appendChild(el('p', 'readiness-note', ready ? row.ready : row.waiting));
      list.appendChild(li);
    });

    if (!list.children.length) {
      list.appendChild(el('li', 'readiness-note', 'No signal information available yet.'));
    }
  }

  // ───────────────────────────────────────────────────────────────────
  // Cohort (institution view) — admin only.
  // ───────────────────────────────────────────────────────────────────

  async function loadCohort() {
    var host = $('#cohortBody');
    if (!host) return;
    host.innerHTML = '';
    host.appendChild(el('p', 'explain-line explain-muted', 'Loading…'));
    try {
      var data = await Autonomize.fetchCohort();
      host.innerHTML = '';
      if (!data.available) {
        // Withheld, not empty. Saying so — and saying how many more
        // students are needed — is the difference between "no data" and
        // "we are protecting the people who are in it".
        host.appendChild(el('p', 'explain-line', data.reason ||
          'Withheld: the cohort is too small to report anonymously.'));
        host.appendChild(el('p', 'explain-caveat',
          (data.students_needed || 0) + ' more contributing student(s) needed ' +
          '(minimum cohort size ' + (data.min_cohort_size || 0) + ').'));
        return;
      }
      var dl = el('dl', 'set-dl');
      [
        ['Contributing students', data.contributing_students],
        ['Enrolled students', data.enrolled_students],
        ['Mean score', Math.round(data.mean_score)],
        ['Median score', Math.round(data.median_score)],
        ['Independent hours (7d)', Math.round(data.independent_hours_7d)],
        ['AI-assisted hours (7d)', Math.round(data.assisted_hours_7d)],
        ['Days suppressed for size', data.suppressed_days]
      ].forEach(function (pair) {
        dl.appendChild(el('dt', null, pair[0]));
        dl.appendChild(el('dd', null, String(pair[1])));
      });
      host.appendChild(dl);

      (data.distribution || []).forEach(function (band) {
        var li = el('div', 'readiness-row');
        var head = el('div', 'readiness-head');
        head.appendChild(el('strong', null, band.band + ' independence'));
        head.appendChild(el('span', 'readiness-count',
          band.count + ' (' + Math.round(band.share * 100) + '%)'));
        li.appendChild(head);
        var track = el('div', 'readiness-track');
        var fill = el('span', 'readiness-fill');
        fill.style.width = Math.round(band.share * 100) + '%';
        track.appendChild(fill);
        li.appendChild(track);
        host.appendChild(li);
      });
    } catch (error) {
      if (error && error.status === 401) { onUnauthorized(); return; }
      host.innerHTML = '';
      host.appendChild(el('p', 'explain-line', error.status === 403
        ? 'This view is available to institution accounts only.'
        : friendly(error)));
    }
  }

  /** Adds the Cohort nav entry for admins only, once. */
  function applyRole() {
    if (!state.user || state.user.role !== 'admin') return;
    var nav = $('#primaryNav');
    if (!nav || nav.querySelector('[data-route="cohort"]')) return;
    var link = el('a', 'nav-link', 'Cohort');
    link.setAttribute('href', '#/cohort');
    link.setAttribute('data-route', 'cohort');
    nav.appendChild(link);
  }

  // ─────────────────────────────────────────────────────────────────────
  // Sessions view
  // ─────────────────────────────────────────────────────────────────────

  function paintSessions() {
    var list = $('#sessionList');
    if (!list) return;
    var rows = state.sessionFilter === 'all'
      ? state.sessions
      : state.sessions.filter(function (s) { return s.category === state.sessionFilter; });

    list.innerHTML = '';
    if (!rows.length) {
      show($('#sessionsEmpty'));
      return;
    }
    hide($('#sessionsEmpty'));

    rows.slice(0, 100).forEach(function (s) {
      var li = document.createElement('li');
      li.className = 'session-row';

      var main = document.createElement('div');
      main.className = 'session-main';
      var domain = document.createElement('strong');
      domain.textContent = s.domain || 'unknown site';
      var meta = document.createElement('small');
      var bits = [(s.category || '').replace('_', ' '), minutes((s.active_ms || 0) / 60000)];
      // A session captured on a surface the browser cannot instrument is
      // labelled as such, rather than showing 0 typed characters — which
      // would be indistinguishable from "wrote nothing".
      if (s.capability === 'limited') bits.push('limited tracking');
      meta.textContent = bits.filter(Boolean).join(' · ');
      main.appendChild(domain);
      main.appendChild(meta);

      var stats = document.createElement('div');
      stats.className = 'session-stats';
      if (s.capability === 'limited') {
        stats.innerHTML = '';
        var lim = document.createElement('span');
        lim.className = 'session-limited';
        lim.textContent = 'activity only';
        stats.appendChild(lim);
      } else {
        var typed = document.createElement('span');
        typed.textContent = (s.typed_chars || 0) + ' typed';
        var pasted = document.createElement('span');
        pasted.className = 'session-pasted';
        pasted.textContent = (s.pasted_chars || 0) + ' pasted';
        stats.appendChild(typed);
        stats.appendChild(pasted);
      }

      li.appendChild(main);
      li.appendChild(stats);
      list.appendChild(li);
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // Settings — all nine sections
  // ─────────────────────────────────────────────────────────────────────

  async function loadSettings() {
    try {
      var body = await Autonomize.getSettings();
      state.settings = body.settings;
      paintSettings();
      paintStatusBar();
    } catch (error) {
      if (error && error.status === 401) onUnauthorized();
    }
  }

  function paintSettings() {
    var s = state.settings;
    if (!s) return;
    var tracking = s.tracking || {};
    var writing = $('#trkWriting');
    var assessment = $('#trkAssessment');
    var ai = $('#trkAi');
    if (writing) writing.checked = !!tracking.writing;
    if (assessment) assessment.checked = !!tracking.assessment;
    if (ai) ai.checked = !!tracking.ai_assistant;

    var excluded = $('#setExcluded');
    if (excluded && document.activeElement !== excluded) {
      excluded.value = (s.excludedDomains || []).join('\n');
    }
  }

  function paintAbout() {
    setText('aboutBackend', BACKEND);
    setText('aboutConn', state.connected ? 'Connected' : 'Disconnected');
    setText('aboutUser', state.user ? (state.user.email || state.user.user_id) : '—');
    var hasData = state.sessions.length > 0;
    setText('aboutExt', hasData
      ? state.sessions.length + ' sessions received'
      : 'No extension data yet');

    Autonomize.health().then(function (h) {
      var db = h && h.database;
      setText('aboutDb', db
        ? (db.backend || 'unknown') + (db.reachable ? ' · reachable' : ' · unreachable')
        : '—');
    }).catch(function () { setText('aboutDb', 'Unavailable'); });
  }

  // ─────────────────────────────────────────────────────────────────────
  // Devices + active sessions
  // ─────────────────────────────────────────────────────────────────────

  async function loadDevices() {
    try {
      var body = await Autonomize.devices();
      paintDevices(body.devices || []);
      paintActiveSessions(body.sessions || []);
    } catch (error) {
      if (error && error.status === 401) { onUnauthorized(); return; }
      setMsg('linkMsg', friendly(error), 'error');
    }
  }

  function paintDevices(devices) {
    var list = $('#deviceList');
    if (!list) return;
    list.innerHTML = '';
    if (!devices.length) { show($('#devicesEmpty')); return; }
    hide($('#devicesEmpty'));

    devices.forEach(function (device) {
      var li = document.createElement('li');
      li.className = 'dev-row';

      var text = document.createElement('div');
      text.className = 'dev-text';
      var label = document.createElement('strong');
      label.textContent = device.label || 'Unnamed device';
      var meta = document.createElement('small');
      meta.textContent = device.last_seen_at
        ? 'Last seen ' + relativeTime(device.last_seen_at)
        : 'Never used';
      text.appendChild(label);
      text.appendChild(meta);

      var actions = document.createElement('div');
      actions.className = 'dev-actions';

      var rename = document.createElement('button');
      rename.type = 'button';
      rename.className = 'btn btn-ghost btn-sm';
      rename.textContent = 'Rename';
      rename.addEventListener('click', function () { renameDevice(device); });

      var revoke = document.createElement('button');
      revoke.type = 'button';
      revoke.className = 'btn btn-danger btn-sm';
      revoke.textContent = 'Sign out';
      revoke.addEventListener('click', function () { revokeDevice(device); });

      actions.appendChild(rename);
      actions.appendChild(revoke);
      li.appendChild(text);
      li.appendChild(actions);
      list.appendChild(li);
    });
  }

  function paintActiveSessions(sessions) {
    var list = $('#sessionsList');
    if (!list) return;
    list.innerHTML = '';
    if (!sessions.length) {
      var li = document.createElement('li');
      li.className = 'dev-row';
      li.textContent = 'This browser is the only signed-in session.';
      list.appendChild(li);
      return;
    }
    sessions.forEach(function (item) {
      var li = document.createElement('li');
      li.className = 'dev-row';
      var text = document.createElement('div');
      text.className = 'dev-text';
      var label = document.createElement('strong');
      label.textContent = item.label || item.device_id || 'Browser session';
      var meta = document.createElement('small');
      meta.textContent = item.last_used_at
        ? 'Active ' + relativeTime(item.last_used_at) : 'Active';
      text.appendChild(label);
      text.appendChild(meta);
      li.appendChild(text);
      list.appendChild(li);
    });
  }

  async function renameDevice(device) {
    var next = window.prompt('Name this device', device.label || '');
    if (next == null) return;
    try {
      await Autonomize.renameDevice(device.device_id, next.trim());
      await loadDevices();
      setMsg('linkMsg', 'Device renamed.', 'ok');
    } catch (error) {
      setMsg('linkMsg', friendly(error), 'error');
    }
  }

  async function revokeDevice(device) {
    if (!window.confirm('Sign out "' + (device.label || 'this device') +
        '"? It will stop sending data until it is linked again.')) return;
    try {
      var body = await Autonomize.revokeDevice(device.device_id);
      await loadDevices();
      setMsg('linkMsg',
        'Device signed out. ' + (body.sessions_revoked || 0) + ' session(s) revoked.', 'ok');
    } catch (error) {
      setMsg('linkMsg', friendly(error), 'error');
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Notifications
  //
  // Derived from the user's own data on this page. Nothing is emailed or
  // pushed, and the preferences are browser-local because the backend has
  // no notification-preference store — the UI says exactly that rather
  // than implying a server round trip that does not happen.
  // ─────────────────────────────────────────────────────────────────────

  var NOTIF_KEY = 'autonomize_notifications';

  function notifPrefs() {
    try {
      var stored = JSON.parse(window.localStorage.getItem(NOTIF_KEY) || 'null');
      if (stored) return stored;
    } catch (_) { /* corrupt or private browsing */ }
    return { baseline: true, drop: true, graded: true };
  }

  function saveNotifPrefs(prefs) {
    try { window.localStorage.setItem(NOTIF_KEY, JSON.stringify(prefs)); } catch (_) {}
    paintNotifications();
  }

  function buildNotifications() {
    var prefs = notifPrefs();
    var d = state.score;
    var items = [];
    if (!d) return items;

    if (prefs.baseline && d.baseline_mean == null) {
      var signals = (d.signals || {}).personalisation || {};
      items.push({
        title: 'Building your baseline',
        body: signals.message ||
          'A few more sessions are needed before a personal trend is reliable.'
      });
    }
    if (prefs.drop && d.delta_vs_baseline != null && d.delta_vs_baseline <= -10) {
      items.push({
        title: 'Score below your baseline',
        body: 'Your latest sessions are ' + Math.abs(Math.round(d.delta_vs_baseline)) +
              ' points under your own average.'
      });
    }
    if (prefs.graded && d.assessment_risk_level === 'high') {
      items.push({
        title: 'Graded session flagged',
        body: 'A recent assessment session scored as high risk. Open Insights for the detail.'
      });
    }
    return items;
  }

  function paintNotifications() {
    var prefs = notifPrefs();
    var baseline = $('#ntfBaseline');
    var drop = $('#ntfDrop');
    var graded = $('#ntfGraded');
    if (baseline) baseline.checked = !!prefs.baseline;
    if (drop) drop.checked = !!prefs.drop;
    if (graded) graded.checked = !!prefs.graded;

    var items = buildNotifications();
    var list = $('#notifList');
    if (list) {
      list.innerHTML = '';
      if (!items.length) {
        var li = document.createElement('li');
        li.className = 'notif-empty';
        li.textContent = 'Nothing needs your attention.';
        list.appendChild(li);
      } else {
        items.forEach(function (item) {
          var row = document.createElement('li');
          row.className = 'notif-item';
          var title = document.createElement('strong');
          title.textContent = item.title;
          var body = document.createElement('span');
          body.textContent = item.body;
          row.appendChild(title);
          row.appendChild(body);
          list.appendChild(row);
        });
      }
    }
    var dot = $('#notifDot');
    if (dot) dot.hidden = items.length === 0;
  }

  // ─────────────────────────────────────────────────────────────────────
  // Menus
  // ─────────────────────────────────────────────────────────────────────

  function closeMenus() {
    hide($('#profileMenu'));
    hide($('#notifMenu'));
    var avatar = $('#navAvatar');
    var notif = $('#notifBtn');
    if (avatar) avatar.setAttribute('aria-expanded', 'false');
    if (notif) notif.setAttribute('aria-expanded', 'false');
  }

  function toggleMenu(menu, button) {
    var opening = menu.hidden;
    closeMenus();
    if (opening) {
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
    }
  }

  // ─────────────────────────────────────────────────────────────────────
  // Wiring
  // ─────────────────────────────────────────────────────────────────────

  function wireMenus() {
    var avatar = $('#navAvatar');
    var profileMenu = $('#profileMenu');
    if (avatar && profileMenu) {
      avatar.addEventListener('click', function (e) {
        e.stopPropagation();
        toggleMenu(profileMenu, avatar);
      });
    }

    var notifBtn = $('#notifBtn');
    var notifMenu = $('#notifMenu');
    if (notifBtn && notifMenu) {
      notifBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        paintNotifications();
        toggleMenu(notifMenu, notifBtn);
      });
    }

    document.addEventListener('click', function (e) {
      if (e.target.closest && e.target.closest('.menu')) return;
      closeMenus();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeMenus();
    });

    var logout = $('#menuLogout');
    if (logout) logout.addEventListener('click', function () { signOut(); });

    // Mobile nav toggle — the markup ships the button, so it must work.
    var navToggle = $('#navToggle');
    var nav = $('#primaryNav');
    if (navToggle && nav) {
      navToggle.addEventListener('click', function () {
        var open = nav.classList.toggle('is-open');
        navToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      nav.addEventListener('click', function (e) {
        if (e.target.closest('.nav-link')) {
          nav.classList.remove('is-open');
          navToggle.setAttribute('aria-expanded', 'false');
        }
      });
    }
  }

  function wireAuthForms() {
    // ---- Login ----
    var loginForm = $('#formLogin');
    if (loginForm) {
      loginForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        setError('loginError', null);
        var restore = busy($('#loginSubmit'), 'Signing in…');
        try {
          var user = await Autonomize.signIn(
            $('#loginEmail').value.trim(), $('#loginPassword').value);
          $('#loginPassword').value = '';   // never keep a password around
          onSignedIn(user);
        } catch (error) {
          setError('loginError', friendly(error));
        } finally {
          restore();
        }
      });
    }

    // ---- Signup ----
    var signupForm = $('#formSignup');
    if (signupForm) {
      var pw = $('#suPassword');
      if (pw) {
        pw.addEventListener('input', function () {
          var hint = $('#suStrength');
          if (!hint) return;
          var value = pw.value;
          if (!value) { hint.textContent = 'At least 10 characters.'; return; }
          if (value.length < 10) {
            hint.textContent = (10 - value.length) + ' more character' +
              (10 - value.length === 1 ? '' : 's') + ' needed';
          } else if (value.length >= 20) hint.textContent = 'Strong';
          else if (value.length >= 14) hint.textContent = 'Good';
          else hint.textContent = 'Acceptable';
        });
      }

      signupForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        setError('signupError', null);
        var password = $('#suPassword').value;
        if (password !== $('#suConfirm').value) {
          setError('signupError', 'Those passwords do not match.');
          return;
        }
        // The server's policy is the authority; this only avoids a
        // round trip that ends in an error we can already predict.
        if (password.length < 10) {
          setError('signupError', 'Please use at least 10 characters.');
          return;
        }
        var restore = busy($('#signupSubmit'), 'Creating account…');
        try {
          var user = await Autonomize.register(
            $('#suEmail').value.trim(), password, $('#suName').value.trim());
          $('#suPassword').value = '';
          $('#suConfirm').value = '';
          onSignedIn(user);
          // A fresh account is unverified; send them to the section that
          // says so rather than leaving it to be discovered.
          if (!user.email_verified && state.authConfig && state.authConfig.email_verification) {
            window.location.hash = '#/settings/profile';
          }
        } catch (error) {
          setError('signupError', friendly(error));
        } finally {
          restore();
        }
      });
    }

    // ---- OTP sign-in ----
    var otpForm = $('#formOtp');
    if (otpForm) {
      var otpSent = false;
      otpForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        setError('otpError', null);
        var email = $('#otpEmail').value.trim();

        if (!otpSent) {
          var restoreSend = busy($('#otpSubmit'), 'Sending…');
          try {
            await Autonomize.requestOtp(email);
            otpSent = true;
            show($('#otpCodeField'));
            hide($('#otpEmailField'));
            show($('#otpResend'));
            setText('otpSub', 'Enter the six-digit code sent to ' + email + '.');
            $('#otpSubmit').textContent = 'Verify';
            $('#otpCode').focus();
            startResendCooldown($('#otpResend'));
          } catch (error) {
            setError('otpError', friendly(error));
          } finally {
            restoreSend();
            if (otpSent) $('#otpSubmit').textContent = 'Verify';
          }
          return;
        }

        var restore = busy($('#otpSubmit'), 'Verifying…');
        try {
          var user = await Autonomize.verifyOtp(email, $('#otpCode').value.trim());
          onSignedIn(user);
        } catch (error) {
          setError('otpError', friendly(error));
        } finally {
          restore();
        }
      });

      var resend = $('#otpResend');
      if (resend) {
        resend.addEventListener('click', async function () {
          setError('otpError', null);
          try {
            await Autonomize.requestOtp($('#otpEmail').value.trim());
            startResendCooldown(resend);
          } catch (error) {
            setError('otpError', friendly(error));
          }
        });
      }
    }

    // ---- Forgot / reset ----
    var forgotForm = $('#formForgot');
    if (forgotForm) {
      var codeSent = false;
      forgotForm.addEventListener('submit', async function (e) {
        e.preventDefault();
        setError('fgError', null);
        hide($('#fgOk'));
        var email = $('#fgEmail').value.trim();

        if (!codeSent) {
          var restoreSend = busy($('#fgSubmit'), 'Sending…');
          try {
            await Autonomize.forgotPassword(email);
            codeSent = true;
            hide($('#fgEmailField'));
            show($('#fgResetFields'));
            show($('#fgResend'));
            // The server answers identically for a known and an unknown
            // address, and so does this UI — distinguishing them here would
            // hand back the account-enumeration oracle the backend refuses
            // to be.
            setText('fgSub', 'If that address has an account, a reset code is on its way.');
            $('#fgSubmit').textContent = 'Reset password';
            startResendCooldown($('#fgResend'));
          } catch (error) {
            setError('fgError', friendly(error));
          } finally {
            restoreSend();
            if (codeSent) $('#fgSubmit').textContent = 'Reset password';
          }
          return;
        }

        var password = $('#fgPassword').value;
        if (password !== $('#fgConfirm').value) {
          setError('fgError', 'Those passwords do not match.');
          return;
        }
        if (password.length < 10) {
          setError('fgError', 'Please use at least 10 characters.');
          return;
        }
        var restore = busy($('#fgSubmit'), 'Resetting…');
        try {
          await Autonomize.resetPassword(email, $('#fgCode').value.trim(), password);
          $('#fgPassword').value = '';
          $('#fgConfirm').value = '';
          var ok = $('#fgOk');
          if (ok) {
            ok.textContent = 'Password reset. You can sign in now.';
            ok.hidden = false;
          }
          setTimeout(function () { window.location.hash = '#/login'; }, 1200);
        } catch (error) {
          setError('fgError', friendly(error));
        } finally {
          restore();
        }
      });

      var fgResend = $('#fgResend');
      if (fgResend) {
        fgResend.addEventListener('click', async function () {
          setError('fgError', null);
          try {
            await Autonomize.forgotPassword($('#fgEmail').value.trim());
            startResendCooldown(fgResend);
          } catch (error) {
            setError('fgError', friendly(error));
          }
        });
      }
    }

    // ---- Google ----
    // Both buttons stay hidden unless the server says Google is configured;
    // a button that 500s is worse than no button.
    ['#loginGoogle', '#signupGoogle'].forEach(function (sel) {
      var button = $(sel);
      if (!button) return;
      button.addEventListener('click', function () {
        window.location.href = Autonomize.googleStartUrl(
          window.location.origin + window.location.pathname + '#/dashboard');
      });
    });
  }

  /** Mirrors the server's resend cooldown so the button cannot be mashed
   *  into a rate-limit rejection the user did not expect. */
  function startResendCooldown(button) {
    if (!button) return;
    var remaining = 60;
    button.disabled = true;
    var label = 'Resend code';
    button.textContent = label + ' (' + remaining + 's)';
    var timer = setInterval(function () {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(timer);
        button.disabled = false;
        button.textContent = label;
        return;
      }
      button.textContent = label + ' (' + remaining + 's)';
    }, 1000);
  }

  function wireSettings() {
    // ---- Profile ----
    var saveProfile = $('#btnSaveProfile');
    if (saveProfile) {
      saveProfile.addEventListener('click', async function () {
        var name = $('#setName').value.trim();
        if (!name) { setMsg('profileMsg', 'Display name cannot be empty.', 'error'); return; }
        var restore = busy(saveProfile, 'Saving…');
        try {
          var body = await Autonomize.updateProfile(name);
          state.user = body.user;
          paintIdentity();
          setMsg('profileMsg', 'Saved.', 'ok');
        } catch (error) {
          setMsg('profileMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var sendVerify = $('#btnSendVerify');
    if (sendVerify) {
      sendVerify.addEventListener('click', async function () {
        var restore = busy(sendVerify, 'Sending…');
        try {
          await Autonomize.sendVerificationEmail();
          setMsg('profileMsg', 'Verification code sent.', 'ok');
        } catch (error) {
          setMsg('profileMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var verifyEmail = $('#btnVerifyEmail');
    if (verifyEmail) {
      verifyEmail.addEventListener('click', async function () {
        var restore = busy(verifyEmail, 'Verifying…');
        try {
          var body = await Autonomize.verifyEmail(
            state.user.email, $('#verifyCode').value.trim());
          state.user = body.user || state.user;
          paintIdentity();
          setMsg('profileMsg', 'Email verified.', 'ok');
        } catch (error) {
          setMsg('profileMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    // ---- Appearance ----
    // Theme itself is owned by script.js (window.AutonomizeTheme) so there
    // is exactly one implementation; this only reflects the stored choice
    // into the segmented control on first paint.
    if (window.AutonomizeTheme) window.AutonomizeTheme.set(window.AutonomizeTheme.get());

    var poll = $('#setPoll');
    if (poll) {
      poll.value = String(pollInterval());
      poll.addEventListener('change', function () {
        try { window.localStorage.setItem('autonomize_poll', poll.value); } catch (_) {}
        startPolling();
        setMsg('apprMsg', 'Fallback refresh interval saved.', 'ok');
      });
    }

    // ---- Tracking ----
    var saveTracking = $('#btnSaveTracking');
    if (saveTracking) {
      saveTracking.addEventListener('click', async function () {
        var restore = busy(saveTracking, 'Saving…');
        try {
          var body = await Autonomize.saveSettings({
            tracking: {
              writing: $('#trkWriting').checked,
              assessment: $('#trkAssessment').checked,
              ai_assistant: $('#trkAi').checked
            },
            excludedDomains: $('#setExcluded').value
              .split('\n').map(function (v) { return v.trim(); }).filter(Boolean)
          });
          // Render what the SERVER decided, not what we sent: it normalises
          // `https://Example.com/` to `example.com`, and that normalised
          // form is what the extension matches against. A UI keeping its own
          // copy would show an exclusion that never fires.
          state.settings = body.settings;
          paintSettings();
          paintStatusBar();
          setMsg('trackingMsg', 'Saved. The extension picks this up within 15 minutes, ' +
            'or immediately when you reopen its popup.', 'ok');
        } catch (error) {
          setMsg('trackingMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    // ---- Privacy & data ----
    var exportBtn = $('#btnExport');
    if (exportBtn) {
      exportBtn.addEventListener('click', async function () {
        var restore = busy(exportBtn, 'Preparing…');
        try {
          var data = await Autonomize.exportData();
          var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url;
          a.download = 'autonomize-export.json';
          a.click();
          URL.revokeObjectURL(url);
          setMsg('privacyMsg', 'Download started.', 'ok');
        } catch (error) {
          setMsg('privacyMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var deleteData = $('#btnDeleteData');
    if (deleteData) {
      deleteData.addEventListener('click', async function () {
        if (!window.confirm(
          'Delete all tracked data? Your account stays, but every session, ' +
          'score and baseline is removed. This cannot be undone.')) return;
        var restore = busy(deleteData, 'Deleting…');
        try {
          var body = await Autonomize.deleteAllData();
          var removed = Object.keys(body.deleted || {}).reduce(function (sum, k) {
            return sum + body.deleted[k];
          }, 0);
          setMsg('privacyMsg', removed + ' row(s) deleted.', 'ok');
          await refresh();
        } catch (error) {
          setMsg('privacyMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var deleteAccount = $('#btnDeleteAccount');
    if (deleteAccount) {
      deleteAccount.addEventListener('click', async function () {
        var confirmText = $('#delConfirm').value.trim();
        if (confirmText !== 'DELETE') {
          setMsg('privacyMsg', 'Type DELETE to confirm.', 'error');
          return;
        }
        var restore = busy(deleteAccount, 'Deleting…');
        try {
          await Autonomize.deleteAccount(confirmText, $('#delPassword').value || null);
          $('#delPassword').value = '';
          stopPolling();
          closeStream();
          state.user = null;
          window.location.hash = '#/login';
          showAuth('login');
          setError('loginError', 'Your account has been deleted.');
        } catch (error) {
          setMsg('privacyMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    // ---- Security ----
    var changePw = $('#btnChangePw');
    if (changePw) {
      changePw.addEventListener('click', async function () {
        var next = $('#pwNew').value;
        if (next !== $('#pwConfirm').value) {
          setMsg('securityMsg', 'Those passwords do not match.', 'error');
          return;
        }
        if (next.length < 10) {
          setMsg('securityMsg', 'Please use at least 10 characters.', 'error');
          return;
        }
        var restore = busy(changePw, 'Saving…');
        try {
          if (state.user.has_password) {
            await Autonomize.changePassword($('#pwCurrent').value, next);
            setMsg('securityMsg',
              'Password updated. Every other signed-in device was signed out.', 'ok');
          } else {
            // First password on an OTP/Google account. A different endpoint,
            // gated on an emailed code rather than a current password.
            var code = $('#pwCode').value.trim();
            if (!code) {
              setMsg('securityMsg', 'Enter the code we emailed you.', 'error');
              restore();
              return;
            }
            await Autonomize.setPassword(code, next);
            $('#pwCode').value = '';
            setMsg('securityMsg', 'Password set. You can now sign in with it.', 'ok');
          }
          $('#pwCurrent').value = '';
          $('#pwNew').value = '';
          $('#pwConfirm').value = '';
          state.user.has_password = true;
          paintPasswordSection();
        } catch (error) {
          setMsg('securityMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var sendPwCode = $('#btnSendPwCode');
    if (sendPwCode) {
      sendPwCode.addEventListener('click', async function () {
        var restore = busy(sendPwCode, 'Sending…');
        try {
          // Reuses the verification-code transport: set_password verifies
          // against the same 'verify_email' purpose, so this is the code it
          // will accept rather than a second, parallel code type.
          await Autonomize.sendVerificationEmail();
          setMsg('securityMsg', 'Code sent to ' + state.user.email + '.', 'ok');
        } catch (error) {
          setMsg('securityMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    var logoutOthers = $('#btnLogoutOthers');
    if (logoutOthers) {
      logoutOthers.addEventListener('click', async function () {
        if (!window.confirm('Sign out every other device? This browser stays signed in.')) return;
        var restore = busy(logoutOthers, 'Signing out…');
        try {
          await Autonomize.logoutEverywhere();
          await loadDevices();
          setMsg('sessionsMsg', 'All other devices signed out.', 'ok');
        } catch (error) {
          setMsg('sessionsMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    // ---- Connected devices ----
    var link = $('#btnLink');
    if (link) {
      link.addEventListener('click', async function () {
        var code = $('#linkCode').value.trim().toUpperCase();
        if (!code) { setMsg('linkMsg', 'Enter the code from the extension popup.', 'error'); return; }
        var restore = busy(link, 'Linking…');
        try {
          var body = await Autonomize.completeLink(code);
          $('#linkCode').value = '';
          var moved = body.rows_moved || {};
          var total = Object.keys(moved).reduce(function (s, k) { return s + moved[k]; }, 0);
          setMsg('linkMsg', total
            ? 'Extension linked. ' + total + ' existing record(s) moved to this account.'
            : 'Extension linked.', 'ok');
          await loadDevices();
          await refresh();
        } catch (error) {
          setMsg('linkMsg', friendly(error), 'error');
        } finally { restore(); }
      });
    }

    // ---- Notifications ----
    [['#ntfBaseline', 'baseline'], ['#ntfDrop', 'drop'], ['#ntfGraded', 'graded']]
      .forEach(function (pair) {
        var box = $(pair[0]);
        if (!box) return;
        box.addEventListener('change', function () {
          var prefs = notifPrefs();
          prefs[pair[1]] = box.checked;
          saveNotifPrefs(prefs);
          setMsg('notifMsg', 'Saved to this browser.', 'ok');
        });
      });
  }

  function wireSessionFilters() {
    var card = $('#activityCard');
    if (!card) return;
    card.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-filter]');
      if (!btn) return;
      state.sessionFilter = btn.getAttribute('data-filter');
      $$('[data-filter]', card).forEach(function (b) {
        var on = b === btn;
        b.classList.toggle('is-on', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      paintSessions();
    });
  }

  // ─────────────────────────────────────────────────────────────────────
  // Boot
  // ─────────────────────────────────────────────────────────────────────

  async function boot() {
    show($('#bootGate'));
    wireMenus();
    wireAuthForms();
    wireSettings();
    wireSessionFilters();
    wireVisibility();
    window.addEventListener('hashchange', route);

    // Point the client at the backend WITHOUT letting it mint an identity.
    // connect() would register an anonymous device account, and a device
    // account is not a signed-in person — using it here would hand the
    // private dashboard to anyone who opened the page.
    Autonomize.configure({ backendUrl: BACKEND });

    try {
      state.authConfig = await Autonomize.authConfig();
    } catch (_) {
      // Backend unreachable. The login form still renders and will report
      // the real reason on submit.
      state.authConfig = null;
    }
    applyAuthConfig();

    try {
      var user = await Autonomize.currentUser();
      // A device account is an anonymous extension identity, not a person.
      // Treating it as signed in would show the private dashboard to
      // anyone who opened the page.
      if (user && !user.is_device_account) {
        state.user = user;
        showApp();
        route();
        loadSettings();
        return;
      }
    } catch (_) {
      // Network failure during session restore — fall through to the
      // login screen rather than guessing.
    }

    hide($('#bootGate'));
    route();
  }

  function applyAuthConfig() {
    var cfg = state.authConfig;
    var googleOn = !!(cfg && cfg.google);
    ['#loginGoogle', '#signupGoogle'].forEach(function (sel) {
      var el = $(sel);
      if (el) el.hidden = !googleOn;
    });
    $$('[data-google-only]').forEach(function (el) { el.hidden = !googleOn; });

    // OTP and password reset both need a mail transport. Without one the
    // links are removed rather than left to fail.
    var otpOn = !!(cfg && cfg.otp);
    var resetOn = !!(cfg && cfg.password_reset);
    $$('a[href="#/otp"]').forEach(function (a) {
      var li = a.closest('.auth-switch') || a;
      li.hidden = !otpOn;
    });
    $$('a[href="#/forgot"]').forEach(function (a) { a.hidden = !resetOn; });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
