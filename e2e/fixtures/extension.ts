import { test as base, chromium, type BrowserContext, type Page, type Worker } from '@playwright/test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
export const EXTENSION_PATH = path.resolve(here, '../../extension');

/** Chrome only exposes extensions to a *persistent* context, so these tests
 * launch their own browser rather than using Playwright's default one —
 * which is why they live behind this fixture instead of plain `test`. */
export const test = base.extend<{
  context: BrowserContext;
  extensionId: string;
  serviceWorker: Worker;
  /** An extension-origin page. Needed because `chrome.runtime.sendMessage`
   * from the service worker doesn't dispatch to the worker's own listener —
   * messages have to originate from another extension context, exactly as
   * they do in production (content scripts and the popup). */
  extensionPage: Page;
  /** Stops the MV3 service worker and wakes a fresh one, discarding all of
   * its in-memory state — the scenario Chrome creates on its own whenever a
   * worker sits idle. Returns the new worker handle. */
  restartServiceWorker: () => Promise<Worker>;
}>({
  context: async ({}, use) => {
    const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'autonomize-ext-'));

    const args = [
      `--disable-extensions-except=${EXTENSION_PATH}`,
      `--load-extension=${EXTENSION_PATH}`,
    ];
    // Chrome's setuid sandbox can't initialise as uid 0, which is how most
    // CI images and containers run. Omitted everywhere else so a normal
    // developer machine keeps the sandbox on.
    if (typeof process.getuid === 'function' && process.getuid() === 0) {
      args.push('--no-sandbox');
    }

    const context = await chromium.launchPersistentContext(userDataDir, {
      // Extensions do load in Chrome's modern headless mode; this needs a
      // full Chrome/Chromium binary though, not the headless_shell build.
      headless: true,
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH || undefined,
      args,
    });

    await use(context);
    await context.close();
    fs.rmSync(userDataDir, { recursive: true, force: true });
  },

  serviceWorker: async ({ context }, use) => {
    await use(await waitForWorker(context));
  },

  extensionId: async ({ serviceWorker }, use) => {
    // chrome-extension://<id>/background.js
    await use(serviceWorker.url().split('/')[2]);
  },

  extensionPage: async ({ context, extensionId }, use) => {
    const page = await context.newPage();
    await page.goto(`chrome-extension://${extensionId}/popup.html`);
    await use(page);
    await page.close();
  },

  restartServiceWorker: async ({ context, extensionPage }, use) => {
    await use(async () => {
      const cdp = await context.newCDPSession(extensionPage);
      await cdp.send('ServiceWorker.enable');
      await cdp.send('ServiceWorker.stopAllWorkers');
      await cdp.detach();
      // The worker is lazy — it only comes back when something addresses
      // it, so wake it deliberately rather than racing whatever the test
      // does next.
      await extensionPage.evaluate(() => chrome.runtime.sendMessage({ type: 'autonomize_get_user_id' }));
      return waitForWorker(context);
    });
  },
});

async function waitForWorker(context: BrowserContext): Promise<Worker> {
  const [existing] = context.serviceWorkers();
  if (existing) return existing;
  return context.waitForEvent('serviceworker', { timeout: 15_000 });
}

export const expect = test.expect;

export const EMPTY_METRICS = {
  typed_chars: 0,
  pasted_chars: 0,
  backspace_count: 0,
  revision_count: 0,
  prompt_count: 0,
  likely_ai_pastes: 0,
  tab_switch_count: 0,
};

/** Sends a message to the background worker the way a content script would. */
export function sendToWorker(page: Page, message: unknown) {
  return page.evaluate((m) => chrome.runtime.sendMessage(m), message);
}

/** Points the extension at a specific backend and identity.
 *
 * Deliberately written through an extension *page* rather than the service
 * worker: the worker is free to stop the moment it has no work, and an
 * evaluate against a stopped worker fails with `chrome` undefined. A page
 * has a stable extension context for as long as it's open. */
export async function configureExtension(
  page: Page,
  { backendUrl, userId }: { backendUrl: string; userId: string }
) {
  await page.evaluate(
    async ({ backendUrl, userId }) => {
      await chrome.storage.local.set({
        autonomize_user_id: userId,
        autonomize_settings: {
          backendUrl,
          tracking: { ai_assistant: true, writing: true, assessment: true },
          excludedDomains: [],
        },
      });
    },
    { backendUrl, userId }
  );
}
