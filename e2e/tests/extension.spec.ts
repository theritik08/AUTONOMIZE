import { BACKEND_URL } from '../playwright.config';
import {
  configureExtension,
  EMPTY_METRICS,
  expect,
  sendToWorker,
  test,
} from '../fixtures/extension';

// These tests drive the extension as Chrome actually runs it: a real
// unpacked MV3 load, a real background service worker, the real popup
// page. Everything the dashboard suite can't reach — manifest validity,
// the message handlers in background.js, popup.js's state machine, and
// service-worker lifecycle behaviour — is covered here.

// A fresh identity per test so nothing one test writes to the shared
// fixture backend can be read by another.
function uniqueUserId(label: string) {
  return `ext-${label}-${process.pid}-${Math.random().toString(36).slice(2, 8)}`;
}

test('extension loads and its MV3 service worker registers', async ({ serviceWorker, extensionId }) => {
  // This is a thinner assertion than it looks. Chrome refuses to load an
  // extension at all if the manifest has a single invalid value, and
  // reports it only to stderr — so any manifest regression (an unsupported
  // permission, a bad match pattern) shows up here as "no service worker"
  // rather than being discovered by a user with a silently dead extension.
  expect(extensionId).toMatch(/^[a-z]{32}$/);
  expect(serviceWorker.url()).toBe(`chrome-extension://${extensionId}/background.js`);
});

test('background worker obtains a SERVER-minted identity on first run', async ({
  extensionPage,
}) => {
  // This used to assert a locally generated crypto.randomUUID(). That is no
  // longer how identity works, and the change was the point: a client that
  // picks its own user_id can pick anybody's, which is the IDOR this
  // project closed. The server now mints the identity and the extension is
  // told what it is.
  await configureExtension(extensionPage, { backendUrl: BACKEND_URL });

  // Identity is provisioned LAZILY, on the first upload rather than at
  // install: a browser that installs the extension while the backend is
  // unreachable must still work once it comes back, so registration is
  // retried as part of sending data rather than being a one-shot at
  // onInstalled. Sending a flush is therefore what triggers it.
  await sendToWorker(extensionPage, {
    type: 'autonomize_flush',
    sessionId: `provision-${process.pid}`,
    category: 'writing',
    domain: 'docs.google.com',
    path: '/document/d/x',
    startedAt: Date.now() - 60_000,
    activeMs: 60_000,
    metrics: { ...EMPTY_METRICS, typed_chars: 120 },
    isFinal: true,
  });

  await expect
    .poll(
      () =>
        extensionPage.evaluate(async () => {
          const stored = await chrome.storage.local.get([
            'autonomize_user_id',
            'autonomize_auth_token',
          ]);
          return {
            userId: stored.autonomize_user_id ?? null,
            hasToken: !!stored.autonomize_auth_token,
          };
        }),
      { timeout: 10_000 }
    )
    .toMatchObject({ hasToken: true });

  const stored = await extensionPage.evaluate(() =>
    chrome.storage.local.get(['autonomize_user_id', 'autonomize_device_id'])
  );
  expect(stored.autonomize_user_id, 'the server must have supplied an identity').toBeTruthy();
  // The DEVICE id, by contrast, IS locally generated — a random UUID per
  // install, deliberately not a hardware fingerprint (see getDeviceId).
  expect(stored.autonomize_device_id).toMatch(/^[0-9a-f-]{36}$/);
});

test('popup renders live score data fetched from a real backend', async ({
  context,
  extensionPage,
  extensionId,
}) => {
  const userId = uniqueUserId('popup');
  await configureExtension(extensionPage, { backendUrl: BACKEND_URL, userId });

  // Post a finalized writing session straight through the worker, then let
  // a freshly-opened popup fetch the score the backend computed from it.
  await sendToWorker(extensionPage, {
    type: 'autonomize_flush',
    sessionId: `${userId}-w1`,
    category: 'writing',
    domain: 'docs.google.com',
    path: '/document/d/x',
    startedAt: Date.now() - 60_000,
    activeMs: 25 * 60_000,
    metrics: { ...EMPTY_METRICS, typed_chars: 900, backspace_count: 60, revision_count: 3 },
    isFinal: true,
  });

  const page = await context.newPage();
  await page.goto(`chrome-extension://${extensionId}/popup.html`);

  await expect(page.locator('#statusText')).toHaveText('Connected');
  await expect(page.locator('#content')).toBeVisible();
  // All-typed, no pastes -> a perfect independence score.
  await expect(page.locator('#scoreNumber')).toHaveText('100');
  await expect(page.locator('#independentMinutes')).toHaveText('25m');
});

test('popup shows the offline state when the backend is unreachable', async ({
  context,
  extensionPage,
  extensionId,
}) => {
  // A port nothing is listening on — a genuinely refused connection rather
  // than a mocked rejection.
  await configureExtension(extensionPage, {
    backendUrl: 'http://127.0.0.1:9',
    userId: uniqueUserId('offline'),
  });

  const page = await context.newPage();
  await page.goto(`chrome-extension://${extensionId}/popup.html`);

  await expect(page.locator('#offline')).toBeVisible();
  await expect(page.locator('#content')).toBeHidden();
  await expect(page.locator('#statusText')).toHaveText('Offline');
});

test('popup distinguishes a 401 from being offline', async ({ context, extensionPage, extensionId }) => {
  await configureExtension(extensionPage, { backendUrl: BACKEND_URL, userId: uniqueUserId('401') });

  const page = await context.newPage();
  // The backend's own 401 behaviour is covered by backend/tests/test_auth.py;
  // what's under test here is purely popup.js's handling of one, so the
  // status is injected rather than standing up a second auth-enabled backend.
  await page.route('**/api/score*', (route) =>
    route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"missing bearer token"}' })
  );
  await page.goto(`chrome-extension://${extensionId}/popup.html`);

  await expect(page.locator('#authRequired')).toBeVisible();
  await expect(page.locator('#offline')).toBeHidden();
  await expect(page.locator('#content')).toBeHidden();
});

test('sessions flushed while the backend is down are kept in the retry queue', async ({
  extensionPage,
}) => {
  const userId = uniqueUserId('queue');
  await configureExtension(extensionPage, { backendUrl: 'http://127.0.0.1:9', userId });

  await sendToWorker(extensionPage, {
    type: 'autonomize_flush',
    sessionId: `${userId}-w1`,
    category: 'writing',
    domain: 'docs.google.com',
    path: '/document/d/x',
    startedAt: Date.now() - 60_000,
    activeMs: 60_000,
    metrics: { ...EMPTY_METRICS, typed_chars: 300 },
    isFinal: true,
  });

  const queue = await extensionPage.evaluate(async () => {
    const stored = await chrome.storage.local.get('autonomize_queue');
    return stored.autonomize_queue as Array<{ path: string; body: { session_id: string } }>;
  });
  expect(queue).toHaveLength(1);
  expect(queue[0].path).toBe('/api/session/upsert');
  expect(queue[0].body.session_id).toBe(`${userId}-w1`);
});

test('a paste with no recent AI activity is not counted as AI-correlated', async ({
  extensionPage,
}) => {
  const userId = uniqueUserId('nocorr');
  await configureExtension(extensionPage, { backendUrl: BACKEND_URL, userId });

  // No preceding ai_assistant session, so this paste falls outside the
  // 10-minute correlation window and must be ignored entirely.
  await sendToWorker(extensionPage, {
    type: 'autonomize_paste_event',
    sessionId: `${userId}-s1`,
    ts: Date.now(),
  });

  const stored = await extensionPage.evaluate(() =>
    chrome.storage.local.get('autonomize_pending_correlation')
  );
  expect(stored.autonomize_pending_correlation ?? {}).toEqual({});
});

test('AI-correlated pastes survive a service-worker restart mid-session', async ({
  context,
  serviceWorker,
  extensionId,
  extensionPage,
  restartServiceWorker,
}) => {
  // The regression test for the bug this suite was built around: Chrome
  // tears the MV3 worker down between events, and a paste event and the
  // flush that consumes it are routinely minutes apart. When the pending
  // count lived in a module-level object it was silently lost across that
  // restart — the session still uploaded, just under-reporting
  // likely_ai_pastes, which quietly inflates the independence score.
  const userId = uniqueUserId('restart');
  const sessionId = `${userId}-exam`;
  await configureExtension(extensionPage, { backendUrl: BACKEND_URL, userId });

  // Visiting an AI assistant opens the 10-minute correlation window.
  await sendToWorker(extensionPage, {
    type: 'autonomize_flush',
    sessionId: `${userId}-ai`,
    category: 'ai_assistant',
    domain: 'chatgpt.com',
    path: '/',
    startedAt: Date.now() - 120_000,
    activeMs: 60_000,
    metrics: { ...EMPTY_METRICS, prompt_count: 3 },
    isFinal: true,
  });

  await sendToWorker(extensionPage, { type: 'autonomize_paste_event', sessionId, ts: Date.now() });
  await sendToWorker(extensionPage, { type: 'autonomize_paste_event', sessionId, ts: Date.now() });

  const pending = await extensionPage.evaluate(() =>
    chrome.storage.local.get('autonomize_pending_correlation')
  );
  expect(pending.autonomize_pending_correlation?.[sessionId]?.count).toBe(2);

  // Prove the restart genuinely discards worker memory, so this test can't
  // pass just because the restart quietly no-opped.
  await serviceWorker.evaluate(() => {
    (globalThis as Record<string, unknown>).__autonomizeRestartCanary = 'alive';
  });
  const restarted = await restartServiceWorker();
  const canary = await restarted.evaluate(
    () => (globalThis as Record<string, unknown>).__autonomizeRestartCanary ?? null
  );
  expect(canary).toBeNull();

  // Now finalize the session the pastes belonged to.
  await sendToWorker(extensionPage, {
    type: 'autonomize_flush',
    sessionId,
    category: 'assessment',
    domain: 'docs.google.com',
    path: '/forms/d/e/x/viewform',
    startedAt: Date.now() - 60_000,
    activeMs: 12 * 60_000,
    metrics: { ...EMPTY_METRICS, typed_chars: 200, pasted_chars: 100 },
    isFinal: true,
  });

  // The backend is the source of truth: it must have been told about both
  // pastes, even though they were recorded by a worker that no longer exists.
  await expect
    .poll(
      async () => {
        // Read AS the extension. An unauthenticated /api/score with a
        // client-chosen user_id is exactly the IDOR the backend now
        // refuses, so the fixture has to present the worker's own token
        // rather than asserting an identity it does not hold.
        const token = await extensionPage.evaluate(async () => {
          const stored = await chrome.storage.local.get('autonomize_auth_token');
          return (stored.autonomize_auth_token as string | undefined) ?? null;
        });
        if (!token) return null;
        const resp = await context.request.get(`${BACKEND_URL}/api/score`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!resp.ok()) return null;
        const body = await resp.json();
        return body.recent_assessment_sessions?.[0]?.likely_ai_pastes ?? null;
      },
      { timeout: 10_000 }
    )
    .toBe(2);

  // And the pending entry is consumed, not left to double-count on a later
  // flush of the same session.
  const afterFlush = await extensionPage.evaluate(() =>
    chrome.storage.local.get('autonomize_pending_correlation')
  );
  expect(afterFlush.autonomize_pending_correlation?.[sessionId]).toBeUndefined();
});
