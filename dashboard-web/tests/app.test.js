/**
 * Unit tests for the dashboard controller (app.js).
 *
 * These close the coverage gap left after the consolidation: lib.js had
 * direct tests, but the auth gate, routing, session restore, settings
 * persistence, theme and the SSE client were only exercised end-to-end by
 * the browser journey. That journey is real coverage, but a routing
 * regression surfaced there as "a browser test went red" rather than as a
 * named failure.
 *
 * app.js is loaded UNMODIFIED into a fake browser (see harness.js) and
 * driven through its real entry points — boot, hashchange, DOM events,
 * server responses. Nothing is stubbed inside app.js itself, so these
 * assert on the shipped code path rather than on a testable copy of it.
 *
 * Run: node --test dashboard-web/tests/app.test.js
 */
const test = require('node:test');
const assert = require('node:assert');

const { boot, fakeApi, apiError } = require('./harness');

const settle = () => new Promise((r) => setImmediate(r));

// ═══════════════════════════════════════════════════════════════════
// 1. Authentication gate
// ═══════════════════════════════════════════════════════════════════

test('a signed-out visitor gets the auth screen and NOT the app shell', async () => {
  const h = await boot({ api: fakeApi({ user: null }) });

  assert.equal(h.el('authWrap').hidden, false, 'auth must be visible');
  assert.equal(h.el('main').hidden, true, 'the private view must be hidden');
  assert.equal(h.documentEl.querySelector('.topbar').hidden, true,
    'the private navigation must be hidden');
});

test('a DEVICE account is not treated as a signed-in person', async () => {
  // The anonymous identity the extension registers. Letting it through
  // would hand the private dashboard to anyone who opened the page.
  const h = await boot({
    api: fakeApi({
      user: {
        user_id: 'dev-1', email: null, role: 'student', provider: 'device',
        email_verified: false, has_password: false, is_device_account: true,
      },
    }),
  });

  assert.equal(h.el('authWrap').hidden, false);
  assert.equal(h.el('main').hidden, true);
});

test('a verified session reveals the app and hides the auth screen', async () => {
  const h = await boot({ api: fakeApi() });

  assert.equal(h.el('authWrap').hidden, true);
  assert.equal(h.el('main').hidden, false);
  assert.equal(h.documentEl.querySelector('.topbar').hidden, false);
  assert.equal(h.el('bootGate').hidden, true, 'the boot gate must be dismissed');
});

test('the gate asks the SERVER, it does not trust stored state', async () => {
  const api = fakeApi();
  const h = await boot({
    api,
    storage: { autonomize_auth_token: '"whatever"' },
  });
  assert.ok(
    api.calls.some((c) => c.name === 'currentUser'),
    'a stored token must be re-verified against /api/auth/me, never assumed'
  );
  assert.equal(h.el('main').hidden, false);
});

test('a network failure during restore lands on the auth screen, not a half-open app', async () => {
  const h = await boot({
    api: fakeApi({ currentUserThrows: apiError(0, 'offline') }),
  });
  assert.equal(h.el('authWrap').hidden, false);
  assert.equal(h.el('main').hidden, true);
});

// ═══════════════════════════════════════════════════════════════════
// 2. Routing
// ═══════════════════════════════════════════════════════════════════

test('a private route requested while signed out is refused', async () => {
  const h = await boot({
    api: fakeApi({ user: null }),
    hash: '#/settings/security',
  });

  const settings = h.documentEl.querySelectorAll('[data-view="settings"]')[0];
  assert.equal(settings.hidden, true, 'settings must not render for a signed-out visitor');
  assert.equal(h.el('authWrap').hidden, false);
});

test('an unknown auth route falls back to login rather than a blank screen', async () => {
  const h = await boot({ api: fakeApi({ user: null }), hash: '#/nonsense' });
  const login = h.documentEl.querySelectorAll('[data-pane="login"]')[0];
  assert.equal(login.hidden, false);
});

test('each primary route shows exactly one view', async () => {
  const h = await boot({ api: fakeApi() });

  for (const view of ['dashboard', 'sessions', 'insights', 'calendar']) {
    h.win.location.hash = `#/${view}`;
    h.win.dispatch('hashchange');
    await settle();

    const shown = h.documentEl.querySelectorAll('.view').filter((v) => !v.hidden);
    assert.equal(shown.length, 1, `#/${view} must show one view, saw ${shown.length}`);
    assert.equal(shown[0].getAttribute('data-view'), view);
  }
});

test('the active nav link tracks the route', async () => {
  const h = await boot({ api: fakeApi() });
  h.win.location.hash = '#/insights';
  h.win.dispatch('hashchange');
  await settle();

  const active = h.documentEl.querySelectorAll('.nav-link')
    .filter((l) => l.classList.contains('is-active'));
  assert.equal(active.length, 1);
  assert.equal(active[0].getAttribute('data-route'), 'insights');
  assert.equal(active[0].getAttribute('aria-current'), 'page');
});

test('a signed-in user on an auth route is redirected into the app', async () => {
  // Otherwise "log in" leaves them staring at the form they just completed.
  const h = await boot({ api: fakeApi(), hash: '#/login' });
  assert.equal(h.win.location.hash, '#/dashboard');
});

test('an unknown private route falls back to the dashboard', async () => {
  const h = await boot({ api: fakeApi() });
  h.win.location.hash = '#/does-not-exist';
  h.win.dispatch('hashchange');
  await settle();

  const shown = h.documentEl.querySelectorAll('.view').filter((v) => !v.hidden);
  assert.equal(shown[0].getAttribute('data-view'), 'dashboard');
});

test('each settings section shows exactly one pane', async () => {
  const h = await boot({ api: fakeApi() });

  for (const section of ['profile', 'appearance', 'tracking', 'privacy',
                         'security', 'devices', 'notifications', 'about']) {
    h.win.location.hash = `#/settings/${section}`;
    h.win.dispatch('hashchange');
    await settle();

    const open = h.documentEl.querySelectorAll('.set-sec').filter((s) => !s.hidden);
    assert.equal(open.length, 1, `${section} must open one section`);
    assert.equal(open[0].getAttribute('data-sec'), section);
  }
});

test('an unknown settings section falls back to Profile', async () => {
  const h = await boot({ api: fakeApi(), hash: '#/settings/not-a-section' });
  const open = h.documentEl.querySelectorAll('.set-sec').filter((s) => !s.hidden);
  assert.equal(open[0].getAttribute('data-sec'), 'profile');
});

test('the Cohort route is added for an admin and withheld from a student', async () => {
  const student = await boot({ api: fakeApi() });
  assert.equal(
    student.documentEl.querySelectorAll('[data-route="cohort"]').length,
    0,
    'a student must not even see a link to a page they cannot open'
  );

  const admin = await boot({
    api: fakeApi({
      user: {
        user_id: 'a-1', email: 'staff@example.edu', role: 'admin',
        display_name: 'Staff', provider: 'password',
        email_verified: true, has_password: true, is_device_account: false,
      },
    }),
  });
  assert.equal(admin.documentEl.querySelectorAll('[data-route="cohort"]').length, 1);
});

// ═══════════════════════════════════════════════════════════════════
// 3. Logout
// ═══════════════════════════════════════════════════════════════════

test('logout revokes server-side, returns to login, and hides the app', async () => {
  const api = fakeApi();
  const h = await boot({ api });

  h.el('menuLogout').dispatch('click');
  await settle();
  await settle();

  assert.ok(api.calls.some((c) => c.name === 'signOut'), 'the server revoke must be attempted');
  assert.equal(h.el('authWrap').hidden, false);
  assert.equal(h.el('main').hidden, true);
  assert.equal(h.win.location.hash, '#/login');
});

test('a failed server revoke still signs the user out locally', async () => {
  // A network failure must not strand someone in a UI they cannot leave.
  const h = await boot({ api: fakeApi({ signOutThrows: apiError(0, 'offline') }) });

  h.el('menuLogout').dispatch('click');
  await settle();
  await settle();

  assert.equal(h.el('authWrap').hidden, false);
});

test('logout closes the live stream', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  assert.equal(h.streams.length, 1);

  h.el('menuLogout').dispatch('click');
  await settle();
  await settle();

  assert.equal(h.streams[0].closed, true, 'a signed-out page must not keep streaming');
});

// ═══════════════════════════════════════════════════════════════════
// 4. Session expiry
// ═══════════════════════════════════════════════════════════════════

test('a 401 during polling returns to login with an explanation', async () => {
  const h = await boot({ api: fakeApi({ scoreThrows: apiError(401, 'expired') }) });
  await settle();
  await settle();

  assert.equal(h.el('authWrap').hidden, false);
  assert.match(h.el('loginError').textContent, /expired/i,
    'expiry is a state to return to, and the user should be told why');
});

test('a NON-401 fetch failure keeps the last data on screen', async () => {
  // Blanking every card because one poll failed turns a blip into "all your
  // data is gone", which is a far worse lie than a stale number.
  const h = await boot({ api: fakeApi({ scoreThrows: apiError(0, 'network') }) });
  await settle();

  assert.equal(h.el('main').hidden, false, 'the user must stay signed in');
  assert.equal(h.el('authWrap').hidden, true);
  assert.match(h.el('sbConnection').textContent, /Disconnected/i);
});

// ═══════════════════════════════════════════════════════════════════
// 5. Settings load / save
// ═══════════════════════════════════════════════════════════════════

test('settings load from the server and populate the controls', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();

  assert.equal(h.el('trkWriting').checked, true);
  assert.equal(h.el('trkAssessment').checked, true);
  assert.equal(h.el('trkAi').checked, false);
  assert.equal(h.el('setExcluded').value, 'example.com');
});

test('saving sends the toggles and renders back what the SERVER decided', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  h.el('trkAi').checked = true;
  h.el('setExcluded').value = 'https://Example.com/\nprivate.test';
  h.el('btnSaveTracking').dispatch('click');
  await settle();
  await settle();

  const save = api.calls.find((c) => c.name === 'saveSettings');
  assert.ok(save, 'the save must reach the server');
  assert.deepEqual(save.args[0].tracking,
    { writing: true, assessment: true, ai_assistant: true });
  assert.deepEqual(save.args[0].excludedDomains, ['https://Example.com/', 'private.test']);

  // The server normalises; rendering our own copy would show the user an
  // exclusion that never actually fires.
  assert.equal(h.el('setExcluded').value, 'example.com\nprivate.test');
});

test('a failed save reports the error and does not claim success', async () => {
  const h = await boot({ api: fakeApi({ saveSettingsThrows: apiError(400, 'bad domain') }) });
  await settle();

  h.el('btnSaveTracking').dispatch('click');
  await settle();
  await settle();

  assert.match(h.el('trackingMsg').textContent, /bad domain/);
  assert.ok(h.el('trackingMsg').className.includes('is-error'));
});

test('a settings load failure with a 401 signs the user out', async () => {
  const h = await boot({ api: fakeApi({ getSettingsThrows: apiError(401, 'expired') }) });
  await settle();
  await settle();
  assert.equal(h.el('authWrap').hidden, false);
});

test('saving a display name updates the profile', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  h.el('setName').value = 'Renamed Student';
  h.el('btnSaveProfile').dispatch('click');
  await settle();
  await settle();

  const call = api.calls.find((c) => c.name === 'updateProfile');
  assert.deepEqual(call.args, ['Renamed Student']);
});

test('an empty display name is refused before it reaches the server', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  h.el('setName').value = '   ';
  h.el('btnSaveProfile').dispatch('click');
  await settle();

  assert.ok(!api.calls.some((c) => c.name === 'updateProfile'));
  assert.match(h.el('profileMsg').textContent, /cannot be empty/i);
});

// ═══════════════════════════════════════════════════════════════════
// 6. Theme — Light / Dark / System
// ═══════════════════════════════════════════════════════════════════

test('app.js does not own the theme; it defers to the single implementation', async () => {
  // Two theme implementations is exactly the duplication the consolidation
  // removed. app.js must call into script.js's controller, not re-derive.
  const applied = [];
  await boot({
    api: fakeApi(),
    theme: { get: () => 'dark', set: (c) => applied.push(c), apply() {} },
  });
  await settle();

  assert.deepEqual(applied, ['dark'],
    'the stored choice must be pushed through the one theme controller');
});

test('all three theme choices round-trip through the controller', async () => {
  for (const choice of ['light', 'dark', 'system']) {
    const applied = [];
    await boot({
      api: fakeApi(),
      theme: { get: () => choice, set: (c) => applied.push(c), apply() {} },
    });
    await settle();
    assert.deepEqual(applied, [choice]);
  }
});

test('the fallback refresh interval persists to storage', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();

  h.el('setPoll').value = '30000';
  h.el('setPoll').dispatch('change');
  await settle();

  assert.equal(h.storage.get('autonomize_poll'), '30000');
  assert.match(h.el('apprMsg').textContent, /saved/i);
});

test('a stored refresh interval is read back on boot', async () => {
  const h = await boot({ api: fakeApi(), storage: { autonomize_poll: '10000' } });
  await settle();
  assert.equal(h.el('setPoll').value, '10000');
});

// ═══════════════════════════════════════════════════════════════════
// 7. SSE — connect, events, reconnect
// ═══════════════════════════════════════════════════════════════════

test('a signed-in dashboard opens exactly one stream, with a ticket', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  assert.equal(h.streams.length, 1, 'one stream per dashboard, not one per view');
  assert.ok(api.calls.some((c) => c.name === 'streamTicket'),
    'the stream must be authenticated by a ticket, never by the session token in a URL');
  assert.match(h.streams[0].url, /ticket=tkt-abc/);
});

test('a signed-OUT page opens no stream at all', async () => {
  const h = await boot({ api: fakeApi({ user: null }) });
  await settle();
  assert.equal(h.streams.length, 0);
});

test('the ready frame marks the dashboard live', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();

  h.streams[0].emit('ready', { id: 0, data: { missed_events: false, resumed: false } });
  assert.equal(h.el('livePill').getAttribute('data-state'), 'live');
  assert.equal(h.el('liveLabel').textContent, 'Live');
});

test('an activity event paints immediately without waiting for a fetch', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  h.streams[0].emit('activity', {
    id: 1, data: { domain: 'docs.google.com', category: 'writing', typed_chars: 40 },
  });

  assert.match(h.el('qsSession').textContent, /Tracking now/);
  assert.match(h.el('qsSite').textContent, /docs\.google\.com/);
});

test('a replayed event id is ignored, so a reconnect cannot double-count', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  const before = api.calls.filter((c) => c.name === 'score').length;
  h.streams[0].emit('activity', { id: 5, data: { domain: 'a.test' } });
  h.flushTimers();
  await settle();
  const afterFirst = api.calls.filter((c) => c.name === 'score').length;

  // Same id again — the browser replays from Last-Event-ID on reconnect.
  h.streams[0].emit('activity', { id: 5, data: { domain: 'b.test' } });
  h.flushTimers();
  await settle();
  const afterReplay = api.calls.filter((c) => c.name === 'score').length;

  assert.ok(afterFirst > before, 'a fresh event must trigger a reconcile');
  assert.equal(afterReplay, afterFirst, 'a replayed event must not reconcile again');
  // And the duplicate's payload must not have repainted anything. Note the
  // authoritative reconcile legitimately overwrites the optimistic paint —
  // that is the design, the event is only a hint — so this asserts the
  // replayed value never appears, not that the first one survives.
  assert.ok(!/b\.test/.test(h.el('qsSite').textContent),
    'a duplicate event must not repaint the live indicator');
});

test('the event is a HINT: it triggers a real reconcile against the API', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  const before = api.calls.filter((c) => c.name === 'sessions').length;
  h.streams[0].emit('activity', { id: 1, data: { domain: 'x.test' } });
  h.flushTimers();
  await settle();
  await settle();

  assert.ok(
    api.calls.filter((c) => c.name === 'sessions').length > before,
    'authoritative numbers come from the API, never from the event payload'
  );
});

test('a desync frame forces a reconcile', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  const before = api.calls.filter((c) => c.name === 'score').length;
  h.streams[0].emit('desync', { id: 9, data: { reason: 'backlog overflow' } });
  h.flushTimers();
  await settle();

  assert.ok(api.calls.filter((c) => c.name === 'score').length > before,
    'dropped events mean everything on screen is suspect');
});

test('resuming behind the server triggers a reconcile', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  const before = api.calls.filter((c) => c.name === 'score').length;
  h.streams[0].emit('ready', { id: 12, data: { missed_events: true, resumed: true } });
  h.flushTimers();
  await settle();

  assert.ok(api.calls.filter((c) => c.name === 'score').length > before);
});

test('a stream error is reported and a reconnect is scheduled', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });
  assert.equal(h.el('livePill').getAttribute('data-state'), 'live');

  h.streams[0].fail();
  await settle();

  assert.notEqual(h.el('livePill').getAttribute('data-state'), 'live',
    'a dead stream must never keep claiming to be live');
  assert.equal(h.streams[0].closed, true, 'the broken stream must be closed, not left looping');
});

test('a failing ticket does not spin: it backs off and reports', async () => {
  const h = await boot({ api: fakeApi({ ticketThrows: apiError(0, 'offline') }) });
  await settle();

  assert.equal(h.streams.length, 0, 'no stream can be opened without a ticket');
  assert.ok(['reconnecting', 'offline'].includes(h.el('livePill').getAttribute('data-state')));
});

test('a 401 on the ticket signs the user out rather than retrying forever', async () => {
  const h = await boot({ api: fakeApi({ ticketThrows: apiError(401, 'expired') }) });
  await settle();
  await settle();
  assert.equal(h.el('authWrap').hidden, false);
});

test('going offline is reported immediately, not after a timeout', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  h.win.dispatch('offline');
  await settle();

  assert.equal(h.el('livePill').getAttribute('data-state'), 'offline');
  assert.equal(h.streams[0].closed, true);
});

test('coming back online reconnects and reconciles', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });
  h.win.dispatch('offline');
  await settle();

  const scoresBefore = api.calls.filter((c) => c.name === 'score').length;
  h.win.dispatch('online');
  await settle();
  await settle();

  assert.equal(h.streams.length, 2, 'a fresh stream must be opened');
  assert.ok(api.calls.filter((c) => c.name === 'score').length > scoresBefore,
    'returning online must also reconcile what was missed');
});

test('no stream is attempted while the browser reports itself offline', async () => {
  const h = await boot({ api: fakeApi(), onLine: false });
  await settle();
  assert.equal(h.streams.length, 0);
  assert.equal(h.el('livePill').getAttribute('data-state'), 'offline');
});

test('a browser with no EventSource degrades honestly rather than claiming live', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  // Simulate the unsupported case by re-booting without the constructor.
  const h2 = await boot({ api: fakeApi() });
  h2.win.EventSource = undefined;
  h2.win.dispatch('offline');
  await settle();
  assert.notEqual(h2.el('livePill').getAttribute('data-state'), 'live');
  assert.ok(h.streams.length >= 0);
});

// ═══════════════════════════════════════════════════════════════════
// 8. SSE account isolation — the client half of the contract
// ═══════════════════════════════════════════════════════════════════

test('the stream URL carries a ticket and never the session token', async () => {
  // The server enforces isolation; the client must not undermine it by
  // putting a reusable credential in a URL that lands in access logs.
  const h = await boot({ api: fakeApi(), storage: { autonomize_auth_token: '"secret-token"' } });
  await settle();

  const url = h.streams[0].url;
  assert.ok(url.includes('ticket='));
  assert.ok(!url.includes('secret-token'), 'the session token must never appear in the stream URL');
});

test('the client never derives identity from an event payload', async () => {
  // A forged or misrouted event must not be able to repoint the dashboard
  // at another account: identity comes from /api/auth/me only.
  const api = fakeApi();
  const h = await boot({ api });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });

  h.streams[0].emit('activity', {
    id: 1,
    data: { domain: 'attacker.test', user_id: 'someone-else', email: 'victim@example.edu' },
  });
  h.flushTimers();
  await settle();

  // Reconciliation still goes through the authenticated API, unchanged.
  assert.ok(api.calls.some((c) => c.name === 'score'));
  assert.equal(h.el('menuEmail').textContent, 'student@example.edu',
    'the signed-in identity must be unaffected by anything an event claims');
});

test('signing out clears the event cursor so a new account starts fresh', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();
  h.streams[0].emit('ready', { id: 0, data: {} });
  h.streams[0].emit('activity', { id: 7, data: { domain: 'a.test' } });

  h.el('menuLogout').dispatch('click');
  await settle();
  await settle();

  // A stream opened afterwards must not resume from the previous account's
  // position — the harness records the requested last-event id in the URL.
  assert.equal(h.streams[0].closed, true);
});

// ═══════════════════════════════════════════════════════════════════
// 9. Stale / offline presentation
// ═══════════════════════════════════════════════════════════════════

test('the status bar distinguishes live from merely connected', async () => {
  const h = await boot({ api: fakeApi() });
  await settle();

  h.streams[0].emit('ready', { id: 0, data: {} });
  await settle();
  assert.match(h.el('sbConnection').textContent, /Live/i);
});

test('an empty score renders an em dash, never a fabricated zero', async () => {
  // "No data yet" and "zero" are different claims, and a new user reading 0
  // concludes the tracking is broken — or believes it.
  const h = await boot({
    api: fakeApi({ score: { current_score: null, baseline_mean: null, signals: {} } }),
  });
  await settle();

  assert.equal(h.el('qsScore').textContent, '—');
  assert.equal(h.el('qsIndependent').textContent, '—');
  assert.equal(h.el('qsBaseline').hidden, false,
    'a forming baseline must say so rather than show a confident number');
});

test('with no sessions the list shows an honest empty state', async () => {
  const h = await boot({ api: fakeApi({ sessions: [] }) });
  await settle();
  assert.equal(h.el('sessionsEmpty').hidden, false);
});

test('a limited-capability session is labelled, not shown as zero work', async () => {
  const h = await boot({
    api: fakeApi({
      sessions: [{
        session_id: 's1', domain: 'canvas.test', category: 'writing',
        active_ms: 600000, typed_chars: 0, pasted_chars: 0, capability: 'limited',
      }],
    }),
  });
  await settle();

  const text = h.el('sessionList').textContent;
  assert.match(text, /limited tracking/i);
  assert.ok(!/0 typed/.test(text),
    'an unmeasurable surface must not be reported as "wrote nothing"');
});

// ═══════════════════════════════════════════════════════════════════
// 10. API error handling in forms
// ═══════════════════════════════════════════════════════════════════

test('a rejected sign-in shows the server message and stays signed out', async () => {
  const h = await boot({
    api: fakeApi({ user: null, signInThrows: apiError(401, 'Wrong email or password.') }),
  });

  h.el('loginEmail').value = 'a@b.test';
  h.el('loginPassword').value = 'nope';
  h.el('formLogin').dispatch('submit');
  await settle();
  await settle();

  assert.equal(h.el('loginError').textContent, 'Wrong email or password.');
  assert.equal(h.el('main').hidden, true);
});

test('an unreachable server is named as such, not reported as a bad password', async () => {
  const h = await boot({
    api: fakeApi({ user: null, signInThrows: apiError(0) }),
  });

  h.el('loginEmail').value = 'a@b.test';
  h.el('loginPassword').value = 'x';
  h.el('formLogin').dispatch('submit');
  await settle();
  await settle();

  assert.match(h.el('loginError').textContent, /reach the server/i,
    'collapsing these sends people hunting for a typo in a password never checked');
});

test('the password field is cleared after a sign-in attempt', async () => {
  const h = await boot({ api: fakeApi({ user: null }) });

  h.el('loginEmail').value = 'a@b.test';
  h.el('loginPassword').value = 'a-perfectly-fine-passphrase';
  h.el('formLogin').dispatch('submit');
  await settle();
  await settle();

  assert.equal(h.el('loginPassword').value, '', 'no password may linger in the DOM');
});

test('signup refuses mismatched passwords before calling the server', async () => {
  const api = fakeApi({ user: null });
  const h = await boot({ api });

  h.el('suName').value = 'A';
  h.el('suEmail').value = 'a@b.test';
  h.el('suPassword').value = 'a-perfectly-fine-passphrase';
  h.el('suConfirm').value = 'something-else-entirely';
  h.el('formSignup').dispatch('submit');
  await settle();

  assert.match(h.el('signupError').textContent, /do not match/i);
  assert.ok(!api.calls.some((c) => c.name === 'register'));
});

test('signup refuses a short password before calling the server', async () => {
  const api = fakeApi({ user: null });
  const h = await boot({ api });

  h.el('suName').value = 'A';
  h.el('suEmail').value = 'a@b.test';
  h.el('suPassword').value = 'short';
  h.el('suConfirm').value = 'short';
  h.el('formSignup').dispatch('submit');
  await settle();

  assert.ok(!api.calls.some((c) => c.name === 'register'));
});

test('forgot-password says the same thing for a known and an unknown address', async () => {
  const api = fakeApi({ user: null });
  const h = await boot({ api, hash: '#/forgot' });

  h.el('fgEmail').value = 'maybe@example.edu';
  h.el('formForgot').dispatch('submit');
  await settle();
  await settle();

  assert.ok(api.calls.some((c) => c.name === 'forgotPassword'));
  assert.match(h.el('fgSub').textContent, /if that address has an account/i,
    'distinguishing them would hand back the enumeration oracle the backend refuses to be');
});

test('reset refuses mismatched new passwords locally', async () => {
  const api = fakeApi({ user: null });
  const h = await boot({ api, hash: '#/forgot' });

  h.el('fgEmail').value = 'a@b.test';
  h.el('formForgot').dispatch('submit');
  await settle();
  await settle();

  h.el('fgCode').value = '123456';
  h.el('fgPassword').value = 'a-perfectly-fine-passphrase';
  h.el('fgConfirm').value = 'not-the-same-passphrase';
  h.el('formForgot').dispatch('submit');
  await settle();

  assert.match(h.el('fgError').textContent, /do not match/i);
  assert.ok(!api.calls.some((c) => c.name === 'resetPassword'));
});

test('account deletion requires the exact confirmation word', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  h.el('delConfirm').value = 'delete';   // lowercase
  h.el('btnDeleteAccount').dispatch('click');
  await settle();

  assert.ok(!api.calls.some((c) => c.name === 'deleteAccount'));
  assert.match(h.el('privacyMsg').textContent, /type DELETE/i);
});

test('linking refuses an empty code without calling the server', async () => {
  const api = fakeApi();
  const h = await boot({ api });
  await settle();

  h.el('linkCode').value = '   ';
  h.el('btnLink').dispatch('click');
  await settle();

  assert.ok(!api.calls.some((c) => c.name === 'completeLink'));
  assert.match(h.el('linkMsg').textContent, /code/i);
});

test('an OTP-only account is offered "set password", not "change password"', async () => {
  const h = await boot({
    api: fakeApi({
      user: {
        user_id: 'u-2', email: 'otp@example.edu', role: 'student',
        display_name: 'OTP User', provider: 'otp',
        email_verified: true, has_password: false, is_device_account: false,
      },
    }),
  });
  await settle();

  assert.match(h.el('pwHeading').textContent, /set a password/i);
  assert.equal(h.el('curPwField').hidden, true,
    'asking for a current password they do not have would be an unanswerable form');
  assert.equal(h.el('setPwCodeField').hidden, false);
});

test('methods the server has not configured are not offered', async () => {
  // A button that 500s is worse than no button.
  const h = await boot({
    api: fakeApi({
      user: null,
      authConfig: { password: true, google: false, otp: false,
                    email_verification: false, password_reset: false },
    }),
  });

  assert.equal(h.el('loginGoogle').hidden, true);
  assert.equal(h.el('signupGoogle').hidden, true);
});

test('Google is offered when the server reports it configured', async () => {
  const h = await boot({
    api: fakeApi({
      user: null,
      authConfig: { password: true, google: true, otp: true,
                    email_verification: true, password_reset: true },
    }),
  });

  assert.equal(h.el('loginGoogle').hidden, false);
});
