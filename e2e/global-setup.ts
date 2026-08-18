import fs from 'node:fs';
import { BACKEND_URL, SEED_IDENTITY_PATH } from './playwright.config';

// Seeds a small, fixed set of sessions for TEST_USER_ID against the real
// backend (already up by the time global setup runs — see playwright.config's
// webServer). Session ids are stable and readable for debugging, but NOT
// meant to make re-seeding idempotent: db.upsert_session_row accumulates
// metrics (typed_chars, active_ms, ...) on repeat calls for an existing
// session_id rather than overwriting them, matching how the real extension
// reports incremental deltas. Isolation instead comes from
// playwright.config.ts's webServer wiping its dedicated SQLite file
// (AUTONOMIZE_DB_PATH) before every backend start, so this always runs
// against an empty database.
const NOW = Date.now();
const DAY = 86_400_000;

interface Metrics {
  typed_chars?: number;
  pasted_chars?: number;
  backspace_count?: number;
  revision_count?: number;
  prompt_count?: number;
  likely_ai_pastes?: number;
  tab_switch_count?: number;
}

/**
 * The seeder's own credential.
 *
 * Writes to /api/session/upsert are authenticated, and the server derives
 * the owning user from the bearer token — the `user_id` in the body is
 * advisory and ignored. That is what closed this project's original IDOR,
 * so the fixture must obtain a real identity rather than asserting one.
 *
 * A REAL PASSWORD ACCOUNT, not a device account. This used to register via
 * /api/auth/device, which is what a freshly-installed extension does — but
 * the dashboard deliberately refuses to treat an anonymous device identity
 * as a signed-in person (doing so would show the private dashboard to
 * anyone who opened the page). So a device token could seed data and then
 * could not be used to view it, and every dashboard test failed on a
 * correct security decision.
 */
const SEED_EMAIL = `e2e-seed-${process.pid}-${Date.now()}@example.edu`;
const SEED_PASSWORD = 'a-perfectly-fine-passphrase';

let seedToken: string | null = null;
let seedUserId: string | null = null;

async function authenticate() {
  const resp = await fetch(`${BACKEND_URL}/api/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      email: SEED_EMAIL,
      password: SEED_PASSWORD,
      display_name: 'E2E Seed Student',
    }),
  });
  if (!resp.ok) {
    throw new Error(`seed registration failed: HTTP ${resp.status} ${await resp.text()}`);
  }
  const body = await resp.json();
  seedToken = body.access_token;
  seedUserId = body.user.user_id;
}

async function post(path: string, body: unknown) {
  const resp = await fetch(`${BACKEND_URL}${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(seedToken ? { Authorization: `Bearer ${seedToken}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    throw new Error(`seed request to ${path} failed: HTTP ${resp.status} ${await resp.text()}`);
  }
}

function session(
  sessionId: string,
  category: 'writing' | 'assessment' | 'ai_assistant',
  domain: string,
  daysAgo: number,
  activeMin: number,
  metrics: Metrics
) {
  return post('/api/session/upsert', {
    // Advisory only — the server reads identity from the bearer token and
    // ignores this field. Sent anyway so the payload matches what the real
    // extension posts.
    user_id: seedUserId,
    session_id: sessionId,
    category,
    domain,
    path: '/e2e-fixture',
    started_at: NOW - daysAgo * DAY,
    active_ms: activeMin * 60_000,
    metrics: {
      typed_chars: 0,
      pasted_chars: 0,
      backspace_count: 0,
      revision_count: 0,
      prompt_count: 0,
      likely_ai_pastes: 0,
      tab_switch_count: 0,
      ...metrics,
    },
    is_final: true,
  });
}

async function waitForHealth() {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${BACKEND_URL}/api/health`);
      if (resp.ok) return;
    } catch {
      // backend not up yet — retry
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Backend at ${BACKEND_URL} never became healthy`);
}

export default async function globalSetup() {
  await waitForHealth();
  await authenticate();

  // A short history so the 14-day trend chart has more than one point.
  for (let d = 6; d >= 1; d--) {
    await session(`e2e-writing-history-${d}`, 'writing', 'docs.google.com', d, 22, {
      typed_chars: 400,
      pasted_chars: 20,
      backspace_count: 30,
      revision_count: 2,
    });
  }

  // Clean, all-typed session — posted before the risky one so the risky
  // session (posted later, more recently updated) is what /api/score's
  // "most recent writing score" picks up as current_score.
  await session('e2e-writing-clean', 'writing', 'github.com', 0, 20, {
    typed_chars: 800,
    pasted_chars: 0,
  });

  // Paste-heavy, AI-correlated session — deliberately low-scoring so the
  // dashboard has a real "below baseline" signal to render, not just a
  // flat 100 everywhere.
  await session('e2e-writing-risky', 'writing', 'notion.so', 0, 20, {
    typed_chars: 100,
    pasted_chars: 400,
    likely_ai_pastes: 4,
  });

  // AI-assistant usage in the last 7 days, for the weekly independent/
  // assisted ratio.
  await session('e2e-ai-1', 'ai_assistant', 'chatgpt.com', 1, 25, { prompt_count: 6 });

  // One flagged assessment session — exercises the exam-integrity panel.
  await session('e2e-assessment-1', 'assessment', 'docs.google.com', 0, 12, {
    typed_chars: 20,
    pasted_chars: 300,
    likely_ai_pastes: 3,
    tab_switch_count: 6,
  });

  // Hand the seeded identity to the test files. The server mints the user
  // id, so it cannot be a compile-time constant — a hardcoded one is how
  // this suite ended up asserting an identity the backend had never issued.
  fs.writeFileSync(
    SEED_IDENTITY_PATH,
    JSON.stringify(
      { userId: seedUserId, token: seedToken, email: SEED_EMAIL, password: SEED_PASSWORD },
      null,
      2
    )
  );
}
