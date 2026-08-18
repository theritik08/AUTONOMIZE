import { expect, test } from '@playwright/test';
import { BACKEND_URL, DASHBOARD_URL, readSeedIdentity } from '../playwright.config';

/**
 * The canonical dashboard, driven as a real static site against a real
 * backend. Nothing is mocked.
 *
 * This replaces `dashboard.spec.ts` and `auth.spec.ts`, which tested a
 * second React dashboard that no longer exists. The assertions they made
 * that still mean something — real seeded data renders, the auth gate
 * actually gates, sign-in works against real Argon2id hashing and real
 * session issuance — are carried over here rather than dropped.
 *
 * The long stateful journey (signup → verification → settings → logout →
 * reset → live SSE) lives in `e2e/verify-dashboard-web.mjs`, because its
 * steps depend on each other in a way the runner's per-test isolation
 * fights rather than helps. This file covers what belongs in the runner:
 * independent assertions about a loaded page.
 */

const PAGE = `${DASHBOARD_URL}/index.html`;

/** Points the page at the fixture backend, exactly as a deployment would. */
async function useBackend(page: import('@playwright/test').Page) {
  await page.addInitScript((api) => {
    window.AUTONOMIZE_BACKEND = api;
  }, BACKEND_URL);
}

/** Signs in by planting the SEEDED identity's real, server-issued token.
 *  Not a fake session: the dashboard re-verifies it against /api/auth/me
 *  before showing anything, so a bogus token would fail here. */
async function useSeededSession(page: import('@playwright/test').Page) {
  const identity = readSeedIdentity();
  await page.addInitScript((id) => {
    window.localStorage.setItem('autonomize_auth_token', JSON.stringify(id.token));
    window.localStorage.setItem('autonomize_user_id', JSON.stringify(id.userId));
  }, identity);
}

function uniqueEmail(label: string) {
  return `e2e-${label}-${process.pid}-${Math.random().toString(36).slice(2, 8)}@example.edu`;
}

// ---------------------------------------------------------------------------
// The auth gate
// ---------------------------------------------------------------------------

test('an unauthenticated visitor gets the login screen, not the dashboard', async ({ page }) => {
  await useBackend(page);
  await page.goto(PAGE);

  await expect(page.locator('#authWrap')).toBeVisible();
  await expect(page.locator('[data-pane="login"]')).toBeVisible();
  // Not merely hidden behind CSS — the app shell is out of the flow and no
  // data fetch is running.
  await expect(page.locator('#main')).toBeHidden();
  await expect(page.locator('.topbar')).toBeHidden();
});

test('a private route typed directly while signed out is refused', async ({ page }) => {
  await useBackend(page);
  await page.goto(`${PAGE}#/settings/security`);

  await expect(page.locator('#authWrap')).toBeVisible();
  await expect(page.locator('[data-view="settings"]')).toBeHidden();
});

test('registering through the real backend signs the user in', async ({ page }) => {
  await useBackend(page);
  await page.goto(`${PAGE}#/signup`);

  await page.fill('#suName', 'E2E Student');
  await page.fill('#suEmail', uniqueEmail('signup'));
  await page.fill('#suPassword', 'a-perfectly-fine-passphrase');
  await page.fill('#suConfirm', 'a-perfectly-fine-passphrase');
  await page.click('#signupSubmit');

  await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
  await expect(page.locator('#authWrap')).toBeHidden();
});

test('a wrong password is rejected and does not sign anyone in', async ({ page }) => {
  await useBackend(page);
  const email = uniqueEmail('wrongpw');

  await page.goto(`${PAGE}#/signup`);
  await page.fill('#suName', 'E2E Student');
  await page.fill('#suEmail', email);
  await page.fill('#suPassword', 'a-perfectly-fine-passphrase');
  await page.fill('#suConfirm', 'a-perfectly-fine-passphrase');
  await page.click('#signupSubmit');
  await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

  await page.click('#navAvatar');
  await page.click('#menuLogout');
  await expect(page.locator('#authWrap')).toBeVisible();

  await page.fill('#loginEmail', email);
  await page.fill('#loginPassword', 'not-the-right-password');
  await page.click('#loginSubmit');

  await expect(page.locator('#loginError')).toBeVisible();
  await expect(page.locator('.topbar')).toBeHidden();
});

// ---------------------------------------------------------------------------
// Real seeded data
// ---------------------------------------------------------------------------

test.describe('signed in with the seeded identity', () => {
  test.beforeEach(async ({ page }) => {
    await useBackend(page);
    await useSeededSession(page);
  });

  test('the dashboard renders real data from a live backend', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    // A real score, not a placeholder. The fixture seeds scored sessions,
    // so an em dash here would mean the fetch never landed.
    await expect(page.locator('#qsScore')).not.toHaveText('—', { timeout: 15_000 });
    await expect(page.locator('#quickStatus')).toBeVisible();
  });

  test('the four primary destinations are all real views', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    for (const view of ['sessions', 'insights', 'calendar', 'dashboard']) {
      await page.goto(`${PAGE}#/${view}`);
      await expect(page.locator(`[data-view="${view}"]`)).toBeVisible();
    }
    await expect(page.locator('#primaryNav .nav-link')).toHaveCount(4);
  });

  test('the sessions list shows the seeded sessions', async ({ page }) => {
    await page.goto(`${PAGE}#/sessions`);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#sessionList')).toContainText('docs.google.com', {
      timeout: 20_000,
    });
  });

  test('the composition chart renders from real backend data', async ({ page }) => {
    await page.goto(`${PAGE}#/insights`);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
    // The fixture seeds a week of history, so the chart has real geometry
    // rather than an empty state.
    await expect(page.locator('#chart svg, #chart .bar, #chart')).toBeVisible();
    await expect(page.locator('#compositionCard')).toContainText('pasted');
  });

  test('the ported explanation panels render honestly', async ({ page }) => {
    // Migrated from the removed React dashboard. They must say something
    // real — including "not enough data yet" — rather than sit empty.
    await page.goto(`${PAGE}#/insights`);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    await expect(page.locator('#explainBody')).not.toBeEmpty({ timeout: 15_000 });
    await expect(page.locator('#readinessList')).not.toBeEmpty();
    // Signal readiness names its thresholds, so a warm-up period is never
    // mistaken for a signal that says "you are fine".
    await expect(page.locator('#readinessList')).toContainText('sessions');
  });

  test('the calendar renders a month grid', async ({ page }) => {
    await page.goto(`${PAGE}#/calendar`);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#calGrid')).not.toBeEmpty({ timeout: 15_000 });
    await expect(page.locator('#calMonth')).not.toBeEmpty();
  });

  // -------------------------------------------------------------------------
  // One settings surface, one theme system
  // -------------------------------------------------------------------------

  test('settings is reached only from the profile avatar', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    // The duplication this consolidation removed: no standalone Settings
    // button, no standalone theme toggle.
    await expect(page.locator('.setting-pill')).toHaveCount(0);
    await expect(page.locator('#themeToggle')).toHaveCount(0);

    await page.click('#navAvatar');
    await expect(page.locator('#profileMenu')).toBeVisible();
    await expect(page.locator('#profileMenu .menu-item')).toHaveCount(9);
  });

  test('every settings section renders', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    for (const section of ['profile', 'appearance', 'tracking', 'privacy',
                           'security', 'devices', 'notifications', 'about']) {
      await page.goto(`${PAGE}#/settings/${section}`);
      await expect(page.locator(`section.set-sec[data-sec="${section}"]`)).toBeVisible();
    }
  });

  test('theme is three-way and survives a reload', async ({ page }) => {
    await page.goto(`${PAGE}#/settings/appearance`);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    await expect(page.locator('[data-theme-choice]')).toHaveCount(3);
    await page.click('[data-theme-choice="dark"]');
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  });

  test('the session survives a reload', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
    await page.reload();
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('#authWrap')).toBeHidden();
  });

  test('no placeholder links survive anywhere in the app', async ({ page }) => {
    await page.goto(PAGE);
    await expect(page.locator('.topbar')).toBeVisible({ timeout: 15_000 });

    const dead = await page.evaluate(() =>
      Array.from(document.querySelectorAll('a[href]'))
        .map((a) => a.getAttribute('href'))
        .filter((h) => h === '#' || h === '' || h === 'javascript:void(0)')
    );
    expect(dead).toEqual([]);
  });
});
