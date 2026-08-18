/**
 * Verifies the PRODUCTION ZIP, not the source tree.
 *
 * WHY THIS IS SEPARATE FROM THE OTHER EXTENSION TESTS
 * ---------------------------------------------------
 * `tests/extension.spec.ts` and `tests/telemetry.spec.ts` load
 * `extension/` directly. That is the right thing for them — they test
 * behaviour, and loading the source keeps the feedback loop short.
 *
 * But it cannot catch a packaging bug, and this project had one: for
 * several commits `package.sh` did not include `telemetry.js`, which both
 * the content script and the service worker require. Every source-loaded
 * test passed. The shipped ZIP was a completely dead extension.
 *
 * So this script unpacks the real artefact into a throwaway directory,
 * loads THAT into Chrome, and drives the whole path:
 *
 *   packaged extension -> service worker -> content script -> aggregate
 *   metrics -> backend -> database
 *
 * It BUILDS that artefact itself rather than expecting one to be lying
 * around. Depending on a pre-built ZIP meant this test silently verified
 * whatever was last packaged — possibly a stale build from before the
 * change under test — and exited(1) on a fresh clone. Building here makes
 * the thing under test unambiguous: the current contents of extension/,
 * packaged by the current package.sh.
 *
 * Run (from e2e/):
 *   cd backend && AUTONOMIZE_DB_PATH=/tmp/zip.db \
 *     AUTONOMIZE_AUTH_SECRET=<any-long-random-string> \
 *     python3 -m uvicorn main:app --port 8787
 *   cd e2e && node verify-packaged-extension.mjs
 *
 * STOP THAT BACKEND AFTERWARDS. Port 8787 is the extension's DEFAULT
 * backendUrl, so a server left running there is picked up by the Playwright
 * extension suite — whose fixtures configure a different port — and two of
 * its tests fail in ways that look like product regressions and are not.
 * This script has to use 8787 because it is the only origin the packaged
 * manifest declares; the trade is that it must not be left running.
 */
import { chromium } from '@playwright/test';
import { execSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { resolveChromium } from './chromium-path.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, '..');
// 8787, NOT the ad-hoc port the other scripts use.
//
// The manifest's host_permissions list EXACT origins — `http://localhost:8787/*`
// and `http://127.0.0.1:8787/*` — never a wildcard, because a wildcard would
// let this extension make credentialed requests to every subdomain of a host.
// Chrome blocks a fetch to any unlisted origin, and the failure is silent from
// the page's side: the retry queue simply fills and nothing is ever uploaded.
//
// That restriction is the point, so this script verifies the packaged build
// against a PERMITTED origin, and separately asserts that an unlisted one is
// genuinely refused.
const API = process.env.AUTONOMIZE_TEST_API || 'http://127.0.0.1:8787';
const BLOCKED_API = 'http://127.0.0.1:8788';

let failures = 0;
const results = [];

function check(label, ok, detail) {
  if (!ok) failures++;
  results.push({ ok, label, detail });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${ok ? '' : `  <- ${detail ?? ''}`}`);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ─── Build, then unpack, the real artefact ─────────────────────────
// Built into a temp directory: this is a test fixture, and leaving a ZIP
// beside the extension/ folder is exactly the "which one is the real
// extension?" ambiguity the repository no longer has.
const buildDir = fs.mkdtempSync(path.join(os.tmpdir(), 'autonomize-build-'));
const ZIP = path.join(buildDir, 'autonomize-extension.zip');
try {
  execSync('bash package.sh', {
    cwd: path.join(REPO, 'extension'),
    env: { ...process.env, AUTONOMIZE_ZIP_OUT: ZIP },
    stdio: 'pipe',
  });
} catch (error) {
  console.error('package.sh failed:\n' + String(error.stdout || error));
  process.exit(1);
}
check('package.sh produces a ZIP', fs.existsSync(ZIP));

const unpacked = fs.mkdtempSync(path.join(os.tmpdir(), 'autonomize-zip-'));
execSync(`unzip -q -o "${ZIP}" -d "${unpacked}"`);
check('the production ZIP unpacks', fs.existsSync(path.join(unpacked, 'manifest.json')));

// The bug that motivated this file: a runtime dependency missing from the
// package. Asserted directly as well as through behaviour, so a failure
// names the cause instead of only the symptom.
for (const required of ['telemetry.js', 'site-map.js', 'content-script.js', 'background.js']) {
  check(`ZIP contains ${required}`, fs.existsSync(path.join(unpacked, required)));
}
check(
  'ZIP ships NO dashboard',
  !fs.existsSync(path.join(unpacked, 'dashboard')) &&
    !fs.existsSync(path.join(unpacked, 'dashboard-web')),
  'the extension must be a collector, not a second dashboard'
);

const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'autonomize-zip-profile-'));
const args = [
  `--disable-extensions-except=${unpacked}`,
  `--load-extension=${unpacked}`,
];
if (typeof process.getuid === 'function' && process.getuid() === 0) args.push('--no-sandbox');

const context = await chromium.launchPersistentContext(userDataDir, {
  headless: true,
  executablePath: resolveChromium(),
  args,
});

try {
  // ─── The service worker starts ───────────────────────────────────
  // Chrome refuses to load an extension whose manifest is invalid, and a
  // worker whose importScripts target is missing dies on first evaluation.
  // Either failure shows up here as "no service worker".
  let [worker] = context.serviceWorkers();
  if (!worker) worker = await context.waitForEvent('serviceworker', { timeout: 20_000 });
  check('the packaged service worker registers', !!worker);

  const extensionId = worker.url().split('/')[2];
  check('extension id looks valid', /^[a-z]{32}$/.test(extensionId), extensionId);

  // The worker's module-level code destructures AutonomizeTelemetry. If
  // telemetry.js were missing from the ZIP the worker would throw on load
  // and this returns undefined — which is exactly the packaging bug.
  const telemetry = await worker.evaluate(() => ({
    loaded: typeof globalThis.AutonomizeTelemetry,
    buckets: globalThis.AutonomizeTelemetry?.IKI_BUCKET_COUNT ?? null,
    detectors: Object.keys(globalThis.AutonomizeTelemetry?.detectors ?? {}).length,
  }));
  check('telemetry.js loaded inside the packaged worker',
    telemetry.loaded === 'object', `typeof = ${telemetry.loaded}`);
  check('histogram bucket count agrees with the backend', telemetry.buckets === 8,
    String(telemetry.buckets));
  check('the detector registry is present', telemetry.detectors >= 5,
    String(telemetry.detectors));

  // ─── Point it at the test backend ────────────────────────────────
  const page = await context.newPage();
  await page.goto(`chrome-extension://${extensionId}/popup.html`);
  await page.evaluate(
    (api) =>
      chrome.storage.local.set({
        autonomize_settings: {
          backendUrl: api,
          dashboardUrl: 'http://127.0.0.1:5599/index.html',
          tracking: { ai_assistant: true, writing: true, assessment: true },
          excludedDomains: [],
        },
      }),
    API
  );

  // ─── The content script loads on a real page ─────────────────────
  const ORIGIN = 'https://autonomize-zip-fixture.test';
  const site = await context.newPage();
  await site.route(`${ORIGIN}/**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body:
        '<!doctype html><meta charset=utf-8>' +
        '<textarea id="t" style="width:600px;height:400px"></textarea>' +
        '<iframe id="child" style="width:400px;height:200px" ' +
        'srcdoc="<div id=\'inner\' contenteditable=\'true\' style=\'width:100%;height:150px\'></div>"></iframe>',
    })
  );
  await site.goto(`${ORIGIN}/`);
  await sleep(1200);

  // ─── Typing produces aggregate metrics ───────────────────────────
  await site.locator('#t').click();
  await site.locator('#t').pressSequentially('independent writing from the packaged build', {
    delay: 8,
  });

  // And in a subframe, which is the Google-Docs-shaped case.
  const frame = site.frameLocator('#child');
  await frame.locator('#inner').click();
  await frame.locator('#inner').pressSequentially('subframe typing too', { delay: 8 });

  const framed = await page.evaluate(async () => {
    const stored = await chrome.storage.local.get('autonomize_frame_metrics');
    const map = stored.autonomize_frame_metrics ?? {};
    return Object.values(map).reduce((sum, e) => sum + (e.raw?.printable ?? 0), 0);
  });

  // Give the subframe's batch interval a chance if it has not fired yet.
  let subframeCount = framed;
  for (let i = 0; i < 20 && subframeCount === 0; i++) {
    await sleep(1000);
    subframeCount = await page.evaluate(async () => {
      const stored = await chrome.storage.local.get('autonomize_frame_metrics');
      const map = stored.autonomize_frame_metrics ?? {};
      return Object.values(map).reduce((sum, e) => sum + (e.raw?.printable ?? 0), 0);
    });
  }
  check('the packaged content script captures subframe typing', subframeCount > 0,
    `printable = ${subframeCount}`);

  // ─── The backend receives it ─────────────────────────────────────
  //
  // Backgrounding the tab, not closing it. `pagehide` does fire a final
  // flush, but closing the page can tear the frame down before
  // chrome.runtime.sendMessage is actually delivered, so the assertion
  // below would be racing teardown rather than testing the pipeline.
  // Switching away fires `visibilitychange`, which is a real flush path
  // and leaves the sender alive to deliver it.
  const other = await context.newPage();
  await other.goto('about:blank');
  await other.bringToFront();
  await sleep(2500);

  const token = await page.evaluate(async () => {
    const s = await chrome.storage.local.get('autonomize_auth_token');
    return s.autonomize_auth_token ?? null;
  });
  check('the packaged worker obtained a server-minted identity', !!token);

  let landed = null;
  // Generous: if the visibilitychange flush is missed for any reason the
  // content script's periodic flush (45s) is the backstop, and a
  // packaging check is allowed to be slow.
  for (let i = 0; i < 70 && !landed; i++) {
    const resp = await context.request.get(`${API}/api/sessions?limit=50`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (resp.ok()) {
      const body = await resp.json();
      landed = (body.sessions ?? []).find((s) => s.domain === 'autonomize-zip-fixture.test');
    }
    if (!landed) await sleep(1000);
  }

  if (!landed) {
    const diag = await page.evaluate(async () => {
      const all = await chrome.storage.local.get(null);
      return {
        keys: Object.keys(all),
        queue: (all.autonomize_queue || []).map((q) => q.path),
        settings: all.autonomize_settings,
      };
    });
    console.log('DIAG:', JSON.stringify(diag).slice(0, 600));
  }
  check('telemetry from the packaged extension reached the backend', !!landed,
    'no session for the fixture domain');

  // WHAT host_permissions ACTUALLY BUYS — asserted as measured, not as
  // assumed.
  //
  // An earlier version of this script asserted that Chrome refuses a fetch
  // to an origin outside host_permissions. It does not, and the failing
  // assertion is what surfaced that: because content_scripts matches
  // <all_urls> (which the generic detector genuinely needs), the extension
  // already holds broad host access, and a fetch to an unlisted origin
  // came back `basic` with a readable body.
  //
  // Asserting a protection that does not exist is worse than not asserting
  // one, so what is checked here is the property that IS true and does
  // matter to a store reviewer: the declared origins are exact, with no
  // subdomain wildcard.
  const manifest = JSON.parse(
    fs.readFileSync(path.join(unpacked, 'manifest.json'), 'utf8')
  );
  const wildcards = (manifest.host_permissions || []).filter((h) => h.includes('//*'));
  check('host_permissions declares exact origins, no subdomain wildcards',
    wildcards.length === 0, JSON.stringify(wildcards));
  check('host_permissions is a short, reviewable list',
    (manifest.host_permissions || []).length <= 4,
    JSON.stringify(manifest.host_permissions));

  // The permissions that DO carry weight in review.
  const perms = new Set(manifest.permissions || []);
  for (const risky of ['history', 'bookmarks', 'cookies', 'downloads',
                       'webRequest', 'debugger', 'management', 'proxy',
                       'clipboardRead', 'nativeMessaging']) {
    check(`does not request "${risky}"`, !perms.has(risky));
  }

} catch (error) {
  check('the packaged run completed without throwing', false, String(error).slice(0, 300));
} finally {
  await context.close();
  fs.rmSync(unpacked, { recursive: true, force: true });
  fs.rmSync(buildDir, { recursive: true, force: true });
  fs.rmSync(userDataDir, { recursive: true, force: true });
}

console.log('\n' + '='.repeat(64));
console.log(`${results.filter((r) => r.ok).length}/${results.length} checks passed`);
if (failures) {
  console.log('\nFAILURES:');
  results.filter((r) => !r.ok).forEach((r) => console.log(`  - ${r.label}: ${r.detail ?? ''}`));
}
process.exit(failures ? 1 : 0);
