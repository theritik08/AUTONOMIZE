/**
 * Real-browser verification of the dashboard-web controller.
 *
 * Drives the exact flow the acceptance criteria name, against a real
 * FastAPI backend and a real static server — no mocks, no stubbed fetch,
 * no injected session. Codes are read from the mail sink the backend
 * actually writes to, so the OTP paths are exercised end to end rather
 * than bypassed.
 *
 * Lives in e2e/ so `@playwright/test` resolves; it is a standalone script
 * rather than a spec because it drives one long stateful journey whose
 * steps depend on each other, which the per-test isolation of the runner
 * would fight rather than help.
 *
 * Run (from e2e/):
 *   # terminal 1 — backend with a readable mail sink
 *   cd backend && AUTONOMIZE_DB_PATH=/tmp/dw.db \
 *     AUTONOMIZE_AUTH_SECRET=<any-long-random-string> \
 *     AUTONOMIZE_ALLOWED_ORIGINS=http://127.0.0.1:5599 \
 *     AUTONOMIZE_MAIL_DIR=/tmp/dwmail \
 *     AUTONOMIZE_AUTH_RATE_LIMIT=500 \
 *     python3 -m uvicorn main:app --port 8788
 *
 *   # terminal 2 — the dashboard as a plain static site
 *   cd dashboard-web && python3 -m http.server 5599 --bind 127.0.0.1
 *
 *   # terminal 3
 *   cd e2e && node verify-dashboard-web.mjs
 *
 * AUTONOMIZE_AUTH_RATE_LIMIT is raised only because this script performs a
 * few dozen auth operations in under a minute, which a real user never
 * does. The shipped default is deliberately low and is NOT changed.
 */
import { chromium } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const WEB = 'http://127.0.0.1:5599/index.html';
const API = 'http://127.0.0.1:8788';
const MAIL = '/tmp/dwmail';

const EMAIL = `student.${Date.now()}@example.edu`;
const PASSWORD = 'a-perfectly-fine-passphrase';
const NEW_PASSWORD = 'another-entirely-fine-passphrase';
const NAME = 'Priya Sharma';

let failures = 0;
const results = [];

function check(label, condition, detail) {
  const ok = !!condition;
  if (!ok) failures++;
  results.push({ ok, label, detail: ok ? '' : detail || '' });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok ? '' : `  <- ${detail || ''}`}`);
}

/** Newest code mailed to an address, optionally filtered by subject text. */
function codeFor(email, hint) {
  const files = fs
    .readdirSync(MAIL)
    .filter((f) => f.includes(email.replace('@', '_at_')))
    .map((f) => ({ f, t: fs.statSync(path.join(MAIL, f)).mtimeMs }))
    .sort((a, b) => b.t - a.t);
  for (const { f } of files) {
    const text = fs.readFileSync(path.join(MAIL, f), 'utf8');
    if (hint && !text.toLowerCase().includes(hint.toLowerCase())) continue;
    const m = /Your code is: (\d{6})/.exec(text);
    if (m) return m[1];
  }
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  headless: true,
  executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
  args: ['--no-sandbox'],
});
const context = await browser.newContext();

// On the CONTEXT, not the page: the isolation test below opens a second
// page, and a per-page init script would leave it pointing at the default
// backend — where it would fail for a reason that has nothing to do with
// what the test is checking.
await context.addInitScript((api) => {
  window.AUTONOMIZE_BACKEND = api;
}, API);

const page = await context.newPage();

const consoleErrors = [];
page.on('pageerror', (e) => consoleErrors.push(String(e)));
page.on('console', (m) => {
  if (m.type() === 'error') consoleErrors.push(m.text());
});

try {
  // ─── 1. AUTH GATE ────────────────────────────────────────────────
  await page.goto(WEB);
  await page.waitForSelector('#authWrap:not([hidden])', { timeout: 15000 });

  check('unauthenticated visitor sees the login screen',
    await page.locator('[data-pane="login"]').isVisible());
  check('private dashboard is NOT in the document flow before auth',
    !(await page.locator('#main').isVisible()));
  check('topbar (private nav) is hidden before auth',
    !(await page.locator('.topbar').isVisible()));

  // Typing a private route while signed out must not reach it.
  await page.goto(`${WEB}#/settings/security`);
  await sleep(600);
  check('a private URL typed while signed out is refused',
    !(await page.locator('[data-view="settings"]').isVisible()) &&
    (await page.locator('#authWrap').isVisible()),
    'settings view was reachable while signed out');

  // ─── 2. SIGNUP ───────────────────────────────────────────────────
  await page.goto(`${WEB}#/signup`);
  await page.waitForSelector('[data-pane="signup"]:not([hidden])');
  await page.fill('#suName', NAME);
  await page.fill('#suEmail', EMAIL);
  await page.fill('#suPassword', PASSWORD);
  await page.fill('#suConfirm', 'does-not-match');
  await page.click('#signupSubmit');
  await sleep(400);
  check('signup rejects mismatched passwords client-side',
    (await page.locator('#signupError').textContent())?.includes('do not match'));

  await page.fill('#suConfirm', PASSWORD);
  await page.click('#signupSubmit');
  await page.waitForSelector('.topbar:not([hidden])', { timeout: 15000 });
  check('signup signs the user in and reveals the app', true);
  check('avatar shows the new account initials',
    (await page.locator('#navInitials').textContent())?.trim() === 'PS',
    `got ${await page.locator('#navInitials').textContent()}`);

  // ─── 3. EMAIL VERIFICATION ───────────────────────────────────────
  await page.goto(`${WEB}#/settings/profile`);
  await page.waitForSelector('[data-sec="profile"]:not([hidden])');
  check('a fresh account reports itself unverified',
    (await page.locator('#verifyBadge').textContent())?.includes('Not verified'));

  await page.click('#btnSendVerify');
  await sleep(1200);
  const verifyCode = codeFor(EMAIL, 'Confirm');
  check('a verification code was actually mailed', !!verifyCode, 'no code in the mail sink');

  if (verifyCode) {
    await page.fill('#verifyCode', verifyCode);
    await page.click('#btnVerifyEmail');
    await page.waitForFunction(
      () => document.querySelector('#verifyBadge')?.textContent?.includes('Verified'),
      { timeout: 10000 }
    );
    check('email verification succeeds and the badge updates', true);
  }

  // Shorten the refresh interval through the real Appearance control, so
  // the later live-update assertions are exercising the polling loop
  // rather than waiting out its default.
  await page.goto(`${WEB}#/settings/appearance`);
  await sleep(400);
  await page.selectOption('#setPoll', '10000');
  await sleep(300);
  check('refresh interval control saves',
    (await page.locator('#apprMsg').textContent())?.includes('saved'));

  // ─── 4. ROUTING ──────────────────────────────────────────────────
  for (const view of ['sessions', 'insights', 'calendar', 'dashboard']) {
    await page.goto(`${WEB}#/${view}`);
    await sleep(350);
    check(`route #/${view} shows its view`,
      await page.locator(`[data-view="${view}"]`).isVisible());
  }

  const navCount = await page.locator('#primaryNav .nav-link').count();
  check('primary nav has exactly 4 items', navCount === 4, `found ${navCount}`);
  check('no standalone Settings button in the header',
    (await page.locator('.setting-pill').count()) === 0);
  check('no standalone theme button in the header',
    (await page.locator('#themeToggle').count()) === 0);

  // ─── 5. PROFILE MENU ─────────────────────────────────────────────
  await page.click('#navAvatar');
  await sleep(300);
  check('profile menu opens from the avatar',
    await page.locator('#profileMenu').isVisible());
  check('profile menu is the single entry point (9 items)',
    (await page.locator('#profileMenu .menu-item').count()) === 9,
    `found ${await page.locator('#profileMenu .menu-item').count()}`);

  // ─── 6. SETTINGS SECTIONS ────────────────────────────────────────
  for (const sec of ['profile', 'appearance', 'tracking', 'privacy',
                     'security', 'devices', 'notifications', 'about']) {
    await page.goto(`${WEB}#/settings/${sec}`);
    await sleep(300);
    check(`settings section "${sec}" renders`,
      await page.locator(`[data-sec="${sec}"].set-sec`).isVisible());
  }

  // ─── 7. THEME: 3-WAY + PERSISTENCE ───────────────────────────────
  await page.goto(`${WEB}#/settings/appearance`);
  await sleep(300);
  const themeBtns = await page.locator('[data-theme-choice]').count();
  check('theme control offers exactly 3 choices', themeBtns === 3, `found ${themeBtns}`);

  await page.click('[data-theme-choice="dark"]');
  await sleep(250);
  check('choosing Dark applies it',
    (await page.getAttribute('html', 'data-theme')) === 'dark');

  await page.reload();
  await page.waitForSelector('.topbar:not([hidden])', { timeout: 15000 });
  check('theme PERSISTS across a reload',
    (await page.getAttribute('html', 'data-theme')) === 'dark',
    `after reload: ${await page.getAttribute('html', 'data-theme')}`);
  check('session is restored on reload (no re-login)',
    await page.locator('.topbar').isVisible());

  await page.goto(`${WEB}#/settings/appearance`);
  await sleep(300);
  await page.click('[data-theme-choice="light"]');
  await sleep(250);
  check('switching back to Light works',
    (await page.getAttribute('html', 'data-theme')) === 'light');

  // ─── 8. TRACKING SETTINGS PERSIST TO THE SERVER ──────────────────
  await page.goto(`${WEB}#/settings/tracking`);
  await sleep(500);
  await page.uncheck('#trkAi');
  await page.fill('#setExcluded', 'https://Example.com/\nprivate.test');
  await page.click('#btnSaveTracking');
  await page.waitForFunction(
    () => document.querySelector('#trackingMsg')?.textContent?.includes('Saved'),
    { timeout: 10000 }
  );
  check('tracking settings save', true);
  check('server-normalised exclusions are rendered back',
    (await page.inputValue('#setExcluded')).includes('example.com'),
    `got: ${await page.inputValue('#setExcluded')}`);

  await page.reload();
  await page.goto(`${WEB}#/settings/tracking`);
  await page.waitForSelector('[data-sec="tracking"]:not([hidden])');
  await sleep(800);
  check('tracking settings PERSIST across a reload',
    (await page.isChecked('#trkAi')) === false);

  // ─── 9. PROFILE UPDATE ───────────────────────────────────────────
  await page.goto(`${WEB}#/settings/profile`);
  await sleep(400);
  await page.fill('#setName', 'Priya S');
  await page.click('#btnSaveProfile');
  await page.waitForFunction(
    () => document.querySelector('#profileMsg')?.textContent === 'Saved.',
    { timeout: 10000 }
  );
  check('display name saves to the server', true);

  // ─── 10. SECURITY: CHANGE PASSWORD ───────────────────────────────
  await page.goto(`${WEB}#/settings/security`);
  await sleep(400);
  await page.fill('#pwCurrent', 'wrong-password-entirely');
  await page.fill('#pwNew', NEW_PASSWORD);
  await page.fill('#pwConfirm', NEW_PASSWORD);
  await page.click('#btnChangePw');
  await sleep(1500);
  check('a wrong current password is rejected by the server',
    !(await page.locator('#securityMsg').textContent())?.includes('updated'),
    `msg: ${await page.locator('#securityMsg').textContent()}`);

  await page.fill('#pwCurrent', PASSWORD);
  await page.fill('#pwNew', NEW_PASSWORD);
  await page.fill('#pwConfirm', NEW_PASSWORD);
  await page.click('#btnChangePw');
  await sleep(2500);
  const pwMsg = (await page.locator('#securityMsg').textContent()) || '';
  check('password change succeeds with the correct current password',
    pwMsg.includes('updated'), `msg: "${pwMsg}"`);

  // ─── 11. LOGOUT ──────────────────────────────────────────────────
  await page.click('#navAvatar');
  await sleep(250);
  await page.click('#menuLogout');
  await page.waitForSelector('#authWrap:not([hidden])', { timeout: 15000 });
  check('logout returns to the login screen',
    await page.locator('[data-pane="login"]').isVisible());
  check('private view is gone after logout',
    !(await page.locator('#main').isVisible()));

  await page.reload();
  await page.waitForSelector('#authWrap:not([hidden])', { timeout: 15000 });
  check('logout survives a reload (token really cleared)',
    await page.locator('#authWrap').isVisible());

  // ─── 12. LOGIN AGAIN (with the NEW password) ─────────────────────
  await page.fill('#loginEmail', EMAIL);
  await page.fill('#loginPassword', PASSWORD);
  await page.click('#loginSubmit');
  await sleep(1500);
  check('the OLD password no longer works',
    await page.locator('#authWrap').isVisible(),
    'old password still signed in');

  await page.fill('#loginPassword', NEW_PASSWORD);
  await page.click('#loginSubmit');
  await page.waitForSelector('.topbar:not([hidden])', { timeout: 15000 });
  check('login with the new password works', true);

  // ─── 13. FORGOT / RESET PASSWORD ─────────────────────────────────
  await page.click('#navAvatar');
  await sleep(200);
  await page.click('#menuLogout');
  await page.waitForSelector('#authWrap:not([hidden])');
  await page.goto(`${WEB}#/forgot`);
  await page.waitForSelector('[data-pane="forgot"]:not([hidden])');
  await page.fill('#fgEmail', EMAIL);
  await page.click('#fgSubmit');
  await sleep(1500);
  const resetCode = codeFor(EMAIL, 'Reset');
  check('a reset code was mailed', !!resetCode, 'no reset code in the sink');

  if (resetCode) {
    await page.fill('#fgCode', resetCode);
    await page.fill('#fgPassword', PASSWORD);
    await page.fill('#fgConfirm', PASSWORD);
    await page.click('#fgSubmit');
    await page.waitForFunction(
      () => !document.querySelector('#fgOk')?.hidden, { timeout: 10000 });
    check('password reset succeeds', true);

    await sleep(1600);
    await page.fill('#loginEmail', EMAIL);
    await page.fill('#loginPassword', PASSWORD);
    await page.click('#loginSubmit');
    await page.waitForSelector('.topbar:not([hidden])', { timeout: 15000 });
    check('login works with the reset password', true);
  }

  // ─── 14. QUICK STATUS SHOWS REAL DATA ────────────────────────────
  await page.goto(`${WEB}#/dashboard`);
  await sleep(1500);
  const qsScore = (await page.locator('#qsScore').textContent())?.trim();
  check('Quick Status renders (no data yet -> em dash, not a fake 0)',
    qsScore === '—' || /^\d+$/.test(qsScore || ''),
    `got "${qsScore}"`);
  check('baseline honesty note is shown while there is no baseline',
    await page.locator('#qsBaseline').isVisible());

  // ─── 15. TELEMETRY -> API -> DB -> DASHBOARD ─────────────────────
  // Post a session AS THIS USER using the token the page holds, which is
  // exactly what the linked extension does.
  const posted = await page.evaluate(async (api) => {
    const token = JSON.parse(localStorage.getItem('autonomize_auth_token'));
    const userId = JSON.parse(localStorage.getItem('autonomize_user_id'));
    const now = Date.now();
    const resp = await fetch(`${api}/api/session/upsert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        user_id: userId,
        session_id: `browser-verify-${now}`,
        category: 'writing',
        domain: 'docs.google.com',
        path: '/document/d/x/edit',
        started_at: now - 20 * 60000,
        active_ms: 20 * 60000,
        metrics: {
          typed_chars: 1400, pasted_chars: 20, backspace_count: 90,
          revision_count: 4, prompt_count: 0, likely_ai_pastes: 0,
          tab_switch_count: 0,
        },
        detector: 'google-docs',
        capability: 'full',
        is_final: true,
        client_ts: now,
      }),
    });
    return resp.status;
  }, API);
  check('telemetry upload accepted by the backend', posted === 200, `HTTP ${posted}`);

  // The dashboard polls; wait for it to reflect the new session.
  await page.waitForFunction(
    () => {
      const t = document.querySelector('#qsScore')?.textContent?.trim();
      return t && t !== '—';
    },
    { timeout: 45000 }
  ).catch(() => {});
  const liveScore = (await page.locator('#qsScore').textContent())?.trim();
  check('dashboard Quick Status updates from real telemetry',
    /^\d+$/.test(liveScore || ''), `score still "${liveScore}"`);

  // The dashboard is poll-driven, so this waits for the next poll rather
  // than assuming a fixed delay — a fixed sleep here was testing the clock,
  // not the app.
  await page.goto(`${WEB}#/sessions`);
  await page.waitForFunction(
    () => (document.querySelector('#sessionList')?.textContent || '').includes('docs.google.com'),
    { timeout: 45000 }
  ).catch(() => {});
  const sessionText = await page.locator('#sessionList').textContent();
  check('Sessions page lists the session',
    sessionText?.includes('docs.google.com'), `list: ${sessionText?.slice(0, 120)}`);
  check('typed characters reached the dashboard (the telemetry fix)',
    sessionText?.includes('1400 typed'), `list: ${sessionText?.slice(0, 160)}`);

  // Category filters must actually filter.
  await page.click('[data-filter="ai_assistant"]');
  await sleep(400);
  check('session category filter excludes non-matching rows',
    !(await page.locator('#sessionList').textContent())?.includes('docs.google.com'));
  await page.click('[data-filter="all"]');
  await sleep(400);
  check('session filter "All" restores the rows',
    (await page.locator('#sessionList').textContent())?.includes('docs.google.com'));

  // ─── 16. ACCOUNT ISOLATION ───────────────────────────────────────
  const otherEmail = `other.${Date.now()}@example.edu`;
  const isolation = await page.evaluate(async ({ api, email, password }) => {
    const reg = await fetch(`${api}/api/auth/register`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    }).then((r) => r.json());
    const resp = await fetch(`${api}/api/sessions?limit=50`, {
      headers: { Authorization: `Bearer ${reg.access_token}` },
    });
    const body = await resp.json();
    return (body.sessions || []).some((s) => s.domain === 'docs.google.com');
  }, { api: API, email: otherEmail, password: PASSWORD });
  check('another account CANNOT see this account\'s sessions', isolation === false);

  const unauth = await page.evaluate(async (api) =>
    (await fetch(`${api}/api/score`)).status, API);
  check('unauthenticated API read is refused', unauth === 401, `HTTP ${unauth}`);

  // ─── 16b. REMAINING VISIBLE CONTROLS ─────────────────────────────
  // Every control the markup still ships has to do something. These are
  // the ones not already exercised above.
  await page.goto(`${WEB}#/insights`);
  await sleep(800);
  await page.click('[data-view="lines"]');
  await sleep(400);
  check('composition chart Bars/Lines toggle switches view',
    (await page.getAttribute('[data-view="lines"]', 'aria-pressed')) === 'true');
  await page.click('[data-view="bars"]');
  await sleep(300);
  check('composition chart toggle switches back',
    (await page.getAttribute('[data-view="bars"]', 'aria-pressed')) === 'true');

  await page.goto(`${WEB}#/calendar`);
  await sleep(800);
  const monthBefore = await page.locator('#calMonth').textContent();
  await page.click('#calPrev');
  await sleep(400);
  const monthAfter = await page.locator('#calMonth').textContent();
  check('calendar previous-month arrow changes the month',
    monthBefore !== monthAfter, `${monthBefore} -> ${monthAfter}`);
  await page.click('#calNext');
  await sleep(400);
  check('calendar next-month arrow returns',
    (await page.locator('#calMonth').textContent()) === monthBefore);

  await page.goto(`${WEB}#/dashboard`);
  await sleep(600);
  await page.click('#notifBtn');
  await sleep(300);
  check('notifications menu opens', await page.locator('#notifMenu').isVisible());
  check('notifications menu has content (real or an honest empty state)',
    ((await page.locator('#notifList').textContent()) || '').trim().length > 0);
  await page.keyboard.press('Escape');
  await sleep(250);
  check('Escape closes the menu', !(await page.locator('#notifMenu').isVisible()));

  await page.goto(`${WEB}#/settings/notifications`);
  await sleep(400);
  await page.uncheck('#ntfBaseline');
  await sleep(300);
  check('notification preference persists to this browser',
    await page.evaluate(() =>
      JSON.parse(localStorage.getItem('autonomize_notifications')).baseline === false));

  await page.goto(`${WEB}#/settings/devices`);
  await sleep(900);
  check('devices section loads without error',
    await page.locator('section[data-sec="devices"]').isVisible());
  await page.click('#btnLink');
  await sleep(600);
  check('linking with an empty code reports a real error',
    ((await page.locator('#linkMsg').textContent()) || '').length > 0);


  // ─── 18. REAL-TIME (SSE) ─────────────────────────────────────────
  // The requirement is visible updates within SECONDS, without a manual
  // refresh and without leaning on the polling fallback — so the fallback
  // is turned OFF for this section. If anything below passes, it passed
  // because an event arrived.
  await page.goto(`${WEB}#/settings/appearance`);
  await sleep(400);
  await page.selectOption('#setPoll', '0');
  await sleep(300);
  await page.goto(`${WEB}#/dashboard`);

  await page.waitForFunction(
    () => document.querySelector('#livePill')?.getAttribute('data-state') === 'live',
    { timeout: 20000 }
  ).catch(() => {});
  check('dashboard reports a LIVE stream',
    (await page.getAttribute('#livePill', 'data-state')) === 'live',
    `state: ${await page.getAttribute('#livePill', 'data-state')}`);

  // Post activity from OUTSIDE the page's own polling loop and time how
  // long the UI takes to reflect it.
  const beforeSite = await page.locator('#qsSite').textContent();
  const liveStart = Date.now();
  await page.evaluate(async (api) => {
    const token = JSON.parse(localStorage.getItem('autonomize_auth_token'));
    const now = Date.now();
    await fetch(`${api}/api/session/upsert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        user_id: 'ignored', session_id: `live-${now}`, category: 'assessment',
        domain: 'canvas.instructure.test', path: '/quiz', started_at: now - 300000,
        active_ms: 300000,
        metrics: { typed_chars: 320, pasted_chars: 5, backspace_count: 12,
                   revision_count: 1, prompt_count: 0, likely_ai_pastes: 0,
                   tab_switch_count: 2 },
        detector: 'generic-input', capability: 'full', is_final: true, client_ts: now,
      }),
    });
  }, API);

  await page.waitForFunction(
    (prev) => (document.querySelector('#qsSite')?.textContent || '') !== prev &&
              (document.querySelector('#qsSite')?.textContent || '').includes('canvas'),
    beforeSite,
    { timeout: 15000 }
  ).catch(() => {});
  const liveMs = Date.now() - liveStart;
  const siteNow = await page.locator('#qsSite').textContent();
  check('dashboard updates from a LIVE event, with polling disabled',
    (siteNow || '').includes('canvas'), `site: "${siteNow}"`);
  check(`live update arrived within seconds (${liveMs} ms)`, liveMs < 8000, `${liveMs} ms`);
  check('live event switched the session indicator to tracking',
    ((await page.locator('#qsSession').textContent()) || '').includes('Tracking now'));

  // The event is a HINT; the authoritative numbers come from the reconcile
  // fetch it triggers. Prove that happened too.
  await page.waitForFunction(
    () => (document.querySelector('#sessionList')?.textContent || '').includes('canvas'),
    { timeout: 15000 }
  ).catch(() => {});
  await page.goto(`${WEB}#/sessions`);
  await sleep(500);
  check('the live event triggered a real reconcile (session list updated)',
    ((await page.locator('#sessionList').textContent()) || '').includes('canvas'));

  // ─── 19. RECONNECT ───────────────────────────────────────────────
  await page.goto(`${WEB}#/dashboard`);
  await sleep(800);
  // Drop the connection the way a network blip does — from outside the
  // app, without telling it.
  await context.setOffline(true);
  await page.waitForFunction(
    () => ['reconnecting', 'offline'].includes(
      document.querySelector('#livePill')?.getAttribute('data-state')),
    { timeout: 20000 }
  ).catch(() => {});
  check('losing the network is reported, not hidden',
    ['reconnecting', 'offline'].includes(
      await page.getAttribute('#livePill', 'data-state')),
    `state: ${await page.getAttribute('#livePill', 'data-state')}`);

  await context.setOffline(false);
  await page.waitForFunction(
    () => document.querySelector('#livePill')?.getAttribute('data-state') === 'live',
    { timeout: 60000 }
  ).catch(() => {});
  check('the stream reconnects automatically once the network returns',
    (await page.getAttribute('#livePill', 'data-state')) === 'live',
    `state: ${await page.getAttribute('#livePill', 'data-state')}`);

  // And it still delivers after recovering.
  const beforeReconnect = await page.locator('#qsSite').textContent();
  await page.evaluate(async (api) => {
    const token = JSON.parse(localStorage.getItem('autonomize_auth_token'));
    const now = Date.now();
    await fetch(`${api}/api/session/upsert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        user_id: 'ignored', session_id: `post-reconnect-${now}`, category: 'writing',
        domain: 'overleaf.test', started_at: now - 60000, active_ms: 60000,
        metrics: { typed_chars: 210, pasted_chars: 0, backspace_count: 3,
                   revision_count: 0, prompt_count: 0, likely_ai_pastes: 0,
                   tab_switch_count: 0 },
        detector: 'rich-editor', capability: 'full', is_final: true, client_ts: now,
      }),
    });
  }, API);
  await page.waitForFunction(
    () => (document.querySelector('#qsSite')?.textContent || '').includes('overleaf'),
    { timeout: 15000 }
  ).catch(() => {});
  check('events still arrive after the reconnect',
    ((await page.locator('#qsSite').textContent()) || '').includes('overleaf'),
    `site: ${await page.locator('#qsSite').textContent()}`);

  // ─── 20. STREAM ISOLATION IN A REAL BROWSER ──────────────────────
  const isoEmail = `iso.${Date.now()}@example.edu`;
  // A SEPARATE browser context, not just a second tab. Tabs share
  // localStorage, so a second page on the same origin would pick up the
  // first account's stored token and sign in as that same user — which
  // would make this test assert nothing at all.
  const otherContext = await browser.newContext();
  await otherContext.addInitScript((api) => { window.AUTONOMIZE_BACKEND = api; }, API);
  const otherPage = await otherContext.newPage();
  await otherPage.goto(`${WEB}#/signup`);
  await otherPage.waitForSelector('[data-pane="signup"]:not([hidden])');
  await otherPage.fill('#suName', 'Other Student');
  await otherPage.fill('#suEmail', isoEmail);
  await otherPage.fill('#suPassword', PASSWORD);
  await otherPage.fill('#suConfirm', PASSWORD);
  await otherPage.click('#signupSubmit');
  await otherPage.waitForSelector('.topbar:not([hidden])', { timeout: 15000 });
  await otherPage.waitForFunction(
    () => document.querySelector('#livePill')?.getAttribute('data-state') === 'live',
    { timeout: 20000 }
  ).catch(() => {});

  // The FIRST account generates activity on a distinctive domain.
  await page.evaluate(async (api) => {
    const token = JSON.parse(localStorage.getItem('autonomize_auth_token'));
    const now = Date.now();
    await fetch(`${api}/api/session/upsert`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        user_id: 'ignored', session_id: `private-${now}`, category: 'writing',
        domain: 'strictly-private.test', started_at: now - 60000, active_ms: 60000,
        metrics: { typed_chars: 4242, pasted_chars: 0, backspace_count: 0,
                   revision_count: 0, prompt_count: 0, likely_ai_pastes: 0,
                   tab_switch_count: 0 },
        is_final: true, client_ts: now,
      }),
    });
  }, API);
  await sleep(5000);

  const otherSaw = await otherPage.evaluate(() =>
    (document.body.textContent || '').includes('strictly-private') ||
    (document.body.textContent || '').includes('4242'));
  check('another account NEVER receives this account\'s live events', otherSaw === false);
  await otherContext.close();

  // ─── 21. NO DEAD CONTROLS ────────────────────────────────────────
  const deadLinks = await page.evaluate(() =>
    Array.from(document.querySelectorAll('a[href]'))
      .map((a) => a.getAttribute('href'))
      .filter((h) => h === '#' || h === '' || h === 'javascript:void(0)'));
  check('no placeholder links remain', deadLinks.length === 0, JSON.stringify(deadLinks));

  const realErrors = consoleErrors.filter(
    (e) => !/Failed to load resource|favicon|404/i.test(e));
  check('no uncaught JavaScript errors during the whole flow',
    realErrors.length === 0, realErrors.slice(0, 3).join(' | '));
} catch (error) {
  check('flow completed without throwing', false, String(error).slice(0, 400));
} finally {
  await browser.close();
}

console.log('\n' + '='.repeat(64));
console.log(`${results.filter((r) => r.ok).length}/${results.length} checks passed`);
if (failures) {
  console.log('\nFAILURES:');
  results.filter((r) => !r.ok).forEach((r) => console.log(`  - ${r.label}: ${r.detail}`));
}
process.exit(failures ? 1 : 0);
