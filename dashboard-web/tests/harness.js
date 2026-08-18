/**
 * A DOM/browser harness small enough to reason about, faithful enough to
 * run `app.js` unmodified.
 *
 * WHY NOT jsdom
 * -------------
 * `dashboard-web/` has zero npm dependencies, and that is a property worth
 * keeping: it is why the dashboard has no build step, no lockfile and no
 * dependency-audit surface. Adding jsdom to unit-test it would trade that
 * away. The extension's telemetry suite already sets the precedent of a
 * hand-rolled fake document for the same reason.
 *
 * WHY THIS IS NOT A TOY
 * ---------------------
 * The element registry is built by scanning the REAL `index.html` for every
 * `id`, `data-view`, `data-sec`, `data-pane`, `data-route`, `data-filter`,
 * `data-theme-choice` and `href` it declares. So if `app.js` reaches for an
 * id the markup does not have, the lookup returns null here exactly as it
 * would in a browser — which means these tests catch markup/controller
 * drift, not just logic bugs.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------
 * No layout, no CSS, no real event capture/bubbling beyond what app.js
 * uses (delegated `click` with `closest`, `submit`, `hashchange`,
 * `visibilitychange`, `online`/`offline`). Anything needing real rendering
 * belongs in the Playwright suite, which drives the same file in a real
 * browser. This covers the decision logic; that covers the pixels.
 */
const fs = require('node:fs');
const path = require('node:path');

const INDEX = path.join(__dirname, '..', 'index.html');

// ───────────────────────────────────────────────────────────────────────
// Element
// ───────────────────────────────────────────────────────────────────────

class El {
  constructor(tag = 'div', attrs = {}) {
    this.tagName = tag.toUpperCase();
    this._attrs = { ...attrs };
    this.children = [];
    this.parent = null;
    this._listeners = {};
    this._text = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.checked = false;
    this.style = {};
    this.files = [];
    this.classList = new ClassList(this);
  }

  get id() { return this._attrs.id || ''; }
  get className() { return this._attrs.class || ''; }
  set className(v) { this._attrs.class = v; }

  getAttribute(name) {
    return name in this._attrs ? this._attrs[name] : null;
  }
  setAttribute(name, value) { this._attrs[name] = String(value); }
  removeAttribute(name) { delete this._attrs[name]; }

  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join('');
    return this._text;
  }
  set textContent(v) {
    this._text = v == null ? '' : String(v);
    this.children = [];
  }

  get innerHTML() { return this._html || ''; }
  set innerHTML(v) {
    // app.js only ever assigns '' to clear a container. Anything else would
    // need a parser, and app.js deliberately builds nodes instead of
    // writing HTML from network data — so an assignment of real markup here
    // is a regression worth failing on rather than emulating.
    if (v !== '') {
      throw new Error(
        'innerHTML was assigned non-empty markup. app.js builds nodes with ' +
        'createElement/textContent precisely so network data is never ' +
        'written as HTML; if that changed, the change is the bug.'
      );
    }
    this._html = '';
    this.children = [];
    this._text = '';
  }

  appendChild(child) {
    child.parent = this;
    this.children.push(child);
    return child;
  }

  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  removeEventListener(type, fn) {
    this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
  }

  /** Fires listeners on this node, then walks up for delegated handlers. */
  dispatch(type, event = {}) {
    const e = {
      type,
      target: this,
      preventDefault() { e.defaultPrevented = true; },
      stopPropagation() { e.propagationStopped = true; },
      ...event,
    };
    let node = this;
    while (node) {
      for (const fn of node._listeners[type] || []) fn.call(node, e);
      if (e.propagationStopped) break;
      node = node.parent;
    }
    return e;
  }

  /** Nearest ancestor-or-self matching a simple selector. */
  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parent;
    }
    return null;
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matches(child, sel)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

class ClassList {
  constructor(el) { this.el = el; }
  _set() {
    return new Set((this.el._attrs.class || '').split(/\s+/).filter(Boolean));
  }
  _write(set) { this.el._attrs.class = [...set].join(' '); }
  contains(c) { return this._set().has(c); }
  add(c) { const s = this._set(); s.add(c); this._write(s); }
  remove(c) { const s = this._set(); s.delete(c); this._write(s); }
  toggle(c, force) {
    const s = this._set();
    const on = force === undefined ? !s.has(c) : !!force;
    if (on) s.add(c); else s.delete(c);
    this._write(s);
    return on;
  }
}

/** Supports only the selector shapes app.js actually uses. */
function matches(node, selector) {
  return selector.split(',').map((s) => s.trim()).filter(Boolean).some((sel) => {
    // a[href="..."] / section[data-sec="..."] / tag[attr]
    let m = /^([a-z]+)?\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(sel);
    if (m) {
      const [, tag, attr, value] = m;
      if (tag && node.tagName !== tag.toUpperCase()) return false;
      const actual = node.getAttribute(attr);
      if (actual === null) return false;
      return value === undefined || actual === value;
    }
    if (sel.startsWith('#')) return node.id === sel.slice(1);
    if (sel.startsWith('.')) return node.classList.contains(sel.slice(1));
    return node.tagName === sel.toUpperCase();
  });
}

// ───────────────────────────────────────────────────────────────────────
// Building the tree from the real index.html
// ───────────────────────────────────────────────────────────────────────

/**
 * Scans index.html for every element that carries an id or one of the
 * data-attributes app.js routes on, and registers a stub for it.
 *
 * A flat tree under <body> rather than the real nesting: app.js looks
 * elements up by id or by a single-class/attribute selector, never by
 * descendant relationship — with two exceptions that are reconstructed
 * explicitly below (the settings sections and the menus, which app.js
 * queries within a parent).
 */
function buildFromIndex() {
  const html = fs.readFileSync(INDEX, 'utf8');
  const body = new El('body');
  const byId = new Map();

  const TAG_RE = /<(\w+)([^>]*)>/g;
  let m;
  while ((m = TAG_RE.exec(html))) {
    const [, tag, rawAttrs] = m;
    const attrs = {};
    const ATTR_RE = /([a-zA-Z-]+)(?:="([^"]*)")?/g;
    let a;
    while ((a = ATTR_RE.exec(rawAttrs))) attrs[a[1]] = a[2] === undefined ? '' : a[2];

    const interesting =
      attrs.id ||
      'data-view' in attrs || 'data-sec' in attrs || 'data-pane' in attrs ||
      'data-route' in attrs || 'data-filter' in attrs ||
      'data-theme-choice' in attrs || 'data-google-only' in attrs ||
      (attrs.class || '').split(/\s+/).some((c) =>
        ['view', 'set-sec', 'set-nav-link', 'nav-link', 'auth-pane', 'topbar',
         'menu-item', 'set-msg'].includes(c)) ||
      (tag === 'a' && (attrs.href || '').startsWith('#/'));

    if (!interesting) continue;

    const el = new El(tag, attrs);
    // The markup's own `hidden` attribute is the initial state, exactly as
    // the browser would treat it.
    el.hidden = 'hidden' in attrs;
    if (attrs.id) byId.set(attrs.id, el);
    body.appendChild(el);
  }

  return { body, byId };
}

// ───────────────────────────────────────────────────────────────────────
// Harness
// ───────────────────────────────────────────────────────────────────────

/**
 * Boots `app.js` against a fake browser.
 *
 * `overrides.api` replaces the Autonomize client wholesale, so each test
 * states exactly what the server does — including failing.
 */
function createHarness(overrides = {}) {
  const { body, byId } = buildFromIndex();
  const htmlEl = new El('html');
  const storage = new Map(Object.entries(overrides.storage || {}));

  const documentEl = {
    body,
    documentElement: htmlEl,
    readyState: 'complete',
    visibilityState: 'visible',
    activeElement: null,
    _listeners: {},
    getElementById: (id) => byId.get(id) || null,
    querySelector: (sel) => body.querySelector(sel),
    querySelectorAll: (sel) => body.querySelectorAll(sel),
    createElement: (tag) => new El(tag),
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      this._listeners[type] = (this._listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event = {}) {
      const e = { type, target: documentEl, preventDefault() {}, stopPropagation() {}, ...event };
      for (const fn of this._listeners[type] || []) fn(e);
      return e;
    },
  };

  // ── EventSource double ────────────────────────────────────────────
  const streams = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this._listeners = {};
      this.onopen = null;
      this.onerror = null;
      this.closed = false;
      streams.push(this);
    }
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }
    close() { this.closed = true; this.readyState = 2; }
    /** Deliver a named server event. */
    emit(type, payload) {
      const e = { type, data: JSON.stringify(payload) };
      for (const fn of this._listeners[type] || []) fn(e);
    }
    open() { this.readyState = 1; if (this.onopen) this.onopen({}); }
    fail() { if (this.onerror) this.onerror({}); }
  }

  const timers = [];
  const win = {
    location: { hash: overrides.hash || '' },
    localStorage: {
      getItem: (k) => (storage.has(k) ? storage.get(k) : null),
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
    document: documentEl,
    EventSource: FakeEventSource,
    _listeners: {},
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type, event = {}) {
      for (const fn of this._listeners[type] || []) fn({ type, ...event });
    },
    scrollTo() {},
    confirm: overrides.confirm || (() => true),
    prompt: overrides.prompt || (() => null),
    AUTONOMIZE_BACKEND: 'http://backend.test',
    AutonomizeTheme: overrides.theme,
    AutonomizeRender: overrides.render || (() => {}),
  };

  const sandbox = {
    window: win,
    // In a browser `window` IS the global object, so app.js's bare
    // `new EventSource(...)` and `navigator.onLine` resolve through it.
    // A vm context has a separate global, so the same names have to be
    // present here too or the stream code silently never runs.
    EventSource: FakeEventSource,
    localStorage: win.localStorage,
    location: win.location,
    document: documentEl,
    navigator: { onLine: overrides.onLine !== false },
    Autonomize: overrides.api,
    setTimeout: (fn, ms) => { const id = timers.push({ fn, ms }) - 1; return id; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    Blob: class {},
    URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    console,
    Promise,
    JSON,
    Math,
    Date,
    Object,
    Array,
    String,
    Number,
    parseInt,
    isNaN,
  };

  return {
    body, byId, documentEl, win, sandbox, streams, timers, storage,
    el: (id) => byId.get(id) || null,
    /** Runs every timer callback scheduled so far (debounces, backoff). */
    flushTimers() {
      const pending = timers.splice(0, timers.length);
      pending.forEach((t) => t.fn());
    },
  };
}

/** Loads app.js into the harness and returns it, once boot() has settled. */
async function boot(overrides = {}) {
  const h = createHarness(overrides);
  const vm = require('node:vm');
  const src = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  const context = vm.createContext(h.sandbox);
  // app.js is an IIFE that self-boots on load, exactly as in the browser.
  vm.runInContext(src, context, { filename: 'app.js' });
  // Let boot()'s awaits settle.
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  return h;
}

/** A signed-in, fully-working backend. Tests override what they care about. */
function fakeApi(over = {}) {
  const calls = [];
  const record = (name) => (...args) => { calls.push({ name, args }); };
  const user = over.user === undefined
    ? {
        user_id: 'u-1', email: 'student@example.edu', role: 'student',
        display_name: 'Test Student', provider: 'password',
        email_verified: true, has_password: true, is_device_account: false,
      }
    : over.user;

  const api = {
    calls,
    configure: record('configure'),
    authConfig: async () => over.authConfig ?? {
      password: true, google: false, otp: true,
      email_verification: true, password_reset: true,
    },
    currentUser: async () => {
      calls.push({ name: 'currentUser', args: [] });
      if (over.currentUserThrows) throw over.currentUserThrows;
      return user;
    },
    score: async () => {
      calls.push({ name: 'score', args: [] });
      if (over.scoreThrows) throw over.scoreThrows;
      return over.score ?? { current_score: 82, baseline_mean: 80, signals: {} };
    },
    sessions: async () => {
      calls.push({ name: 'sessions', args: [] });
      if (over.sessionsThrows) throw over.sessionsThrows;
      return { sessions: over.sessions ?? [] };
    },
    getSettings: async () => {
      calls.push({ name: 'getSettings', args: [] });
      if (over.getSettingsThrows) throw over.getSettingsThrows;
      return { settings: over.settings ?? {
        tracking: { writing: true, assessment: true, ai_assistant: false },
        excludedDomains: ['example.com'],
      } };
    },
    saveSettings: async (patch) => {
      calls.push({ name: 'saveSettings', args: [patch] });
      if (over.saveSettingsThrows) throw over.saveSettingsThrows;
      return { settings: over.savedSettings ?? {
        tracking: patch.tracking,
        excludedDomains: (patch.excludedDomains || []).map((d) =>
          d.replace(/^https?:\/\//, '').replace(/\/$/, '').toLowerCase()),
      } };
    },
    signIn: async (email, password) => {
      calls.push({ name: 'signIn', args: [email, password] });
      if (over.signInThrows) throw over.signInThrows;
      return user;
    },
    register: async (...a) => { calls.push({ name: 'register', args: a }); return user; },
    signOut: async () => {
      calls.push({ name: 'signOut', args: [] });
      if (over.signOutThrows) throw over.signOutThrows;
    },
    streamTicket: async () => {
      calls.push({ name: 'streamTicket', args: [] });
      if (over.ticketThrows) throw over.ticketThrows;
      return { ticket: 'tkt-abc', expires_in: 60, heartbeat_seconds: 20 };
    },
    streamUrl: (t, last) => `http://backend.test/api/events?ticket=${t}&last=${last || 0}`,
    devices: async () => ({ devices: [], sessions: [] }),
    fetchCohort: async () => ({ available: false, reason: 'too small', students_needed: 3 }),
    health: async () => ({ database: { backend: 'sqlite', reachable: true } }),
    updateProfile: async (n) => { calls.push({ name: 'updateProfile', args: [n] }); return { user }; },
    sendVerificationEmail: async () => ({ ok: true }),
    verifyEmail: async () => ({ user }),
    requestOtp: async (e) => { calls.push({ name: 'requestOtp', args: [e] }); },
    verifyOtp: async () => user,
    forgotPassword: async (e) => { calls.push({ name: 'forgotPassword', args: [e] }); },
    resetPassword: async (...a) => { calls.push({ name: 'resetPassword', args: a }); },
    changePassword: async (...a) => { calls.push({ name: 'changePassword', args: a }); },
    setPassword: async (...a) => { calls.push({ name: 'setPassword', args: a }); },
    logoutEverywhere: async () => { calls.push({ name: 'logoutEverywhere', args: [] }); },
    completeLink: async (c) => { calls.push({ name: 'completeLink', args: [c] }); return { rows_moved: {} }; },
    renameDevice: async () => {},
    revokeDevice: async () => ({ sessions_revoked: 1 }),
    deleteAccount: async (...a) => { calls.push({ name: 'deleteAccount', args: a }); },
    deleteAllData: async () => ({ deleted: { sessions: 3 } }),
    exportData: async () => ({ user_id: 'u-1' }),
    googleStartUrl: () => 'http://backend.test/api/auth/google/start',
  };
  return api;
}

/** An ApiError-shaped rejection, matching autonomize-api.js. */
function apiError(status, message = 'boom') {
  const e = new Error(message);
  e.status = status;
  return e;
}

module.exports = { boot, fakeApi, apiError, El, createHarness };
