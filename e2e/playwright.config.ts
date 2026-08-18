import { defineConfig, devices } from '@playwright/test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname_compat = path.dirname(fileURLToPath(import.meta.url));

// Dedicated, non-default ports so this suite never collides with a real
// backend (8787) or `vite dev` (5173) instance the developer might already
// have running locally.
export const BACKEND_PORT = 8799;
export const DASHBOARD_PORT = 5199;
export const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;
export const DASHBOARD_URL = `http://localhost:${DASHBOARD_PORT}`;

/**
 * Where global-setup writes the identity the backend actually issued.
 *
 * This replaces the old `TEST_USER_ID = 'e2e-test-user'` constant. That
 * constant was a fiction: since auth landed, the server derives the owning
 * user from the bearer token and ignores any user_id in a request body, so
 * a hardcoded id names a user that was never created. Reading the real one
 * back from setup is what keeps the fixture honest.
 */
export const SEED_IDENTITY_PATH = path.join(__dirname_compat, 'seed-identity.json');

export interface SeedIdentity {
  userId: string;
  token: string;
  /** A REAL account, so tests can also sign in through the login form. */
  email: string;
  password: string;
}

export function readSeedIdentity(): SeedIdentity {
  const raw = fs.readFileSync(SEED_IDENTITY_PATH, 'utf8');
  return JSON.parse(raw) as SeedIdentity;
}

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  reporter: 'list',
  globalSetup: './global-setup.ts',
  use: {
    baseURL: DASHBOARD_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  // Playwright starts these, waits for their health URL, and reuses an
  // already-running instance on the same port (handy for iterating on a
  // spec without a full backend restart each time).
  webServer: [
    {
      // AUTONOMIZE_DB_PATH (see backend/db.py) points this instance at its
      // own SQLite file instead of the default backend/autonomize.db a
      // developer's real `uvicorn main:app --port 8787` dev session reads
      // — the two must never share a file. Deleting it before start (rather
      // than trusting cross-run upsert idempotency) is what makes every
      // fresh `npx playwright test` invocation start from a clean slate,
      // since db.upsert_session_row accumulates metrics on repeat calls
      // rather than overwriting them.
      command: `rm -f e2e-fixture.db e2e-fixture.db-wal e2e-fixture.db-shm && AUTONOMIZE_DB_PATH=./e2e-fixture.db python3 -m uvicorn main:app --port ${BACKEND_PORT}`,
      cwd: '../backend',
      url: `${BACKEND_URL}/api/health`,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      // dashboard-web is plain HTML/CSS/JS with no build step, so it is
      // served exactly as it is deployed — a static directory. This
      // replaced a `vite dev` server for a second, React dashboard that no
      // longer exists (see the consolidation commit); serving the real
      // artefact rather than a dev-server transform of it is strictly
      // closer to production.
      command: `python3 -m http.server ${DASHBOARD_PORT} --bind 127.0.0.1`,
      cwd: '../dashboard-web',
      url: DASHBOARD_URL,
      reuseExistingServer: true,
      timeout: 30_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
  projects: [
    {
      // The canonical dashboard, served as a static site exactly as it
      // ships.
      name: 'dashboard',
      testMatch: /dashboard-web\.spec\.ts/,
      use: {
        ...devices['Desktop Chrome'],
        // Some environments (this repo's cloud sandbox, most CI images)
        // ship a pre-installed Chromium instead of letting Playwright
        // manage its own download. Harmless on a normal machine: the env
        // var is simply unset and browser resolution stays default.
        launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
          ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
          : {},
      },
    },
    {
      // The real unpacked extension — manifest, background service worker,
      // popup. Launches its own persistent context (Chrome only exposes
      // extensions to one), so the browser options above don't apply; see
      // fixtures/extension.ts.
      name: 'extension',
      testMatch: /(extension|telemetry)\.spec\.ts/,
    },
  ],
});
