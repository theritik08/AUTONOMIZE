const $ = (id) => document.getElementById(id);

// Same arc math as the web dashboard's gauge (dashboard-web/script.js),
// kept in sync by hand: the popup is a plain extension page and cannot
// share a module with a separately-served static site.
const CX = 120, CY = 118, R = 92, STROKE = 16;
const START_DEG = 180, SWEEP_DEG = 180;

function pointOnArc(deg) {
  const rad = (deg * Math.PI) / 180;
  return { x: CX + R * Math.cos(rad), y: CY + R * Math.sin(rad) };
}
function arcPath(startDeg, endDeg) {
  const start = pointOnArc(startDeg);
  const end = pointOnArc(endDeg);
  const largeArc = endDeg - startDeg > 180 ? 1 : 0;
  return `M ${start.x} ${start.y} A ${R} ${R} 0 ${largeArc} 1 ${end.x} ${end.y}`;
}

function renderGauge(svg, score) {
  const clamped = Math.max(0, Math.min(100, score));
  const endDeg = START_DEG + (SWEEP_DEG * clamped) / 100;
  const track = arcPath(START_DEG, START_DEG + SWEEP_DEG);
  const value = arcPath(START_DEG, Math.max(START_DEG + 0.001, endDeg));

  svg.innerHTML = `
    <defs>
      <linearGradient id="popup-gauge-grad" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="var(--signal-caution)" />
        <stop offset="100%" stop-color="var(--signal-good)" />
      </linearGradient>
    </defs>
    <path d="${track}" fill="none" stroke="var(--surface-3)" stroke-width="${STROKE}" stroke-linecap="round" />
    <path d="${value}" fill="none" stroke="url(#popup-gauge-grad)" stroke-width="${STROKE}" stroke-linecap="round" />
  `;
}

function fmtMinutes(mins) {
  if (mins < 60) return `${Math.round(mins)}m`;
  const h = Math.floor(mins / 60);
  const m = Math.round(mins % 60);
  return `${h}h ${m}m`;
}

function setStatus(state) {
  const pill = $("statusPill");
  pill.classList.remove("online", "offline");
  const label = $("statusText");
  if (state === "online") {
    pill.classList.add("online");
    label.textContent = "Connected";
  } else if (state === "offline") {
    pill.classList.add("offline");
    label.textContent = "Offline";
  } else if (state === "signin") {
    // Deliberately NOT "Offline". The backend answered; we just have no
    // usable session. Showing both at once was the confusing state.
    pill.classList.add("offline");
    label.textContent = "Sign in";
  } else {
    label.textContent = "Checking…";
  }
}

function isEmptyData(data) {
  return (
    data.baseline_mean == null &&
    data.assessment_score == null &&
    (data.trend || []).length === 0 &&
    data.independent_minutes_7d === 0 &&
    data.assisted_minutes_7d === 0
  );
}

/**
 * Opens the canonical web dashboard — REUSING an existing tab if one is
 * already open.
 *
 * The extension used to SHIP a full dashboard (a built React app at
 * `dashboard/index.html`) and open that. It no longer does, and that is the
 * point of the consolidation: two dashboards meant two settings screens,
 * two auth flows and two theme systems.
 *
 * WHY THE TAB LOOKUP, AND NOT JUST tabs.create:
 * a bare `tabs.create` opens a NEW tab on every click, so pressing "Open
 * Dashboard" three times left three copies of the same page — three live
 * SSE connections to the same account, three polling loops, and a user who
 * has to close two of them. Focusing the existing tab is what makes the
 * button idempotent.
 *
 * Matching is on the dashboard's PATH, ignoring the hash, because
 * `#/settings/tracking` and `#/dashboard` are the same page and must not
 * count as two. When a tab is found we navigate it to the requested hash
 * and focus it, which is also how the Settings deep-link works without
 * spawning a second copy.
 *
 * This function is the extension's ONLY call to tabs.create, and every one
 * of its call sites is a click handler — nothing here opens a tab in
 * response to navigation, telemetry, or a backend event.
 */
async function openDashboard(hash) {
  const { autonomize_settings } = await chrome.storage.local.get("autonomize_settings");
  const base = (autonomize_settings && autonomize_settings.dashboardUrl) ||
    "http://localhost:5599/index.html";
  const url = base + (hash || "");

  // Compare without the hash: same page, different view.
  const withoutHash = (u) => {
    const cut = u.indexOf("#");
    return cut === -1 ? u : u.slice(0, cut);
  };
  const target = withoutHash(base);

  try {
    const tabs = await chrome.tabs.query({});
    const existing = tabs.find((t) => t.url && withoutHash(t.url) === target);
    if (existing) {
      await chrome.tabs.update(existing.id, { url, active: true });
      if (existing.windowId != null) {
        await chrome.windows.update(existing.windowId, { focused: true }).catch(() => {});
      }
      return;
    }
  } catch (_) {
    // Querying tabs can fail if the permission is ever narrowed. Falling
    // through to create one is the right degradation: the user asked for
    // the dashboard and should get it.
  }

  chrome.tabs.create({ url });
}

async function main() {
  const loadingEl = $("loading"), offlineEl = $("offline"), contentEl = $("content"), emptyEl = $("empty");
  const authRequiredEl = $("authRequired");

  $("openDashboard").addEventListener("click", () => openDashboard());
  $("settingsLink").addEventListener("click", (e) => {
    e.preventDefault();
    // Deep-links into the one Settings implementation, which lives in the
    // web dashboard behind the profile avatar.
    openDashboard("#/settings/tracking");
  });
  const signInBtn = $("signInFromPopup");
  if (signInBtn) signInBtn.addEventListener("click", () => openDashboard());

  // ONE round trip to the background worker, which owns the credential and
  // knows how to refresh it. The popup deliberately does not read
  // `autonomize_auth_token` itself: access tokens live ten minutes, only
  // the worker can rotate one, and a popup that sent a raw stored token
  // reported "Offline" and "sign-in required" simultaneously while the
  // backend was reachable and the session was fine.
  let status;
  try {
    status = await ask("autonomize_popup_status");
  } catch (_) {
    status = { state: "offline" };
  }
  if (!status || status.ok === false) status = { state: "offline" };

  paintAccount(status.account || null);
  paintTracking(status.settings || null);

  // Exactly one state is shown. These were previously set together.
  if (status.state === "signed-out") {
    setStatus("signin");
    loadingEl.classList.add("hidden");
    contentEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
    offlineEl.classList.add("hidden");
    if (authRequiredEl) authRequiredEl.classList.remove("hidden");
    return;
  }

  if (status.state === "offline") {
    setStatus("offline");
    loadingEl.classList.add("hidden");
    contentEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
    if (authRequiredEl) authRequiredEl.classList.add("hidden");
    offlineEl.classList.remove("hidden");
    return;
  }

  try {
    const data = status.score;
    setStatus("online");

    loadingEl.classList.add("hidden");
    offlineEl.classList.add("hidden");
    if (authRequiredEl) authRequiredEl.classList.add("hidden");

    if (isEmptyData(data)) {
      emptyEl.classList.remove("hidden");
      contentEl.classList.add("hidden");
      return;
    }
    emptyEl.classList.add("hidden");
    contentEl.classList.remove("hidden");

    const score = data.current_score ?? 50;
    $("scoreNumber").textContent = Math.round(score);
    renderGauge($("gauge"), score);

    const delta = data.delta_vs_baseline ?? 0;
    const deltaEl = $("scoreDelta");
    if (data.baseline_mean == null) {
      deltaEl.textContent = "building your baseline";
      deltaEl.className = "delta flat";
    } else if (Math.abs(delta) < 0.5) {
      deltaEl.textContent = "steady vs. your baseline";
      deltaEl.className = "delta flat";
    } else if (delta > 0) {
      deltaEl.textContent = `+${delta.toFixed(1)} vs. your baseline`;
      deltaEl.className = "delta up";
    } else {
      deltaEl.textContent = `${delta.toFixed(1)} vs. your baseline`;
      deltaEl.className = "delta down";
    }

    const riskChip = $("riskChip");
    if (data.assessment_risk_level) {
      riskChip.textContent = `Exam risk: ${data.assessment_risk_level}`;
      riskChip.className = `riskChip ${data.assessment_risk_level}`;
      riskChip.classList.remove("hidden");
    } else {
      riskChip.classList.add("hidden");
    }

    const indep = data.independent_minutes_7d ?? 0;
    const assisted = data.assisted_minutes_7d ?? 0;
    const total = indep + assisted;
    const indepPct = total > 0 ? (indep / total) * 100 : 50;
    $("ratioIndependent").style.width = `${indepPct}%`;
    $("independentMinutes").textContent = fmtMinutes(indep);
    $("assistedMinutes").textContent = fmtMinutes(assisted);

    $("streakValue").textContent = data.streak_days ?? 0;
  } catch (err) {
    setStatus("offline");
    loadingEl.classList.add("hidden");
    contentEl.classList.add("hidden");
    emptyEl.classList.add("hidden");
    if (authRequiredEl) authRequiredEl.classList.add("hidden");
    offlineEl.classList.remove("hidden");
  }
}

main();

// ---------------------------------------------------------------------------
// Account linking
//
// The user never types a user id or a device id. They read a six-character
// code off this popup and enter it on the dashboard while signed in. That
// asymmetry is the security property: the code alone attaches nothing,
// because completing the link requires an authenticated dashboard session.
// ---------------------------------------------------------------------------

let linkCountdown = null;
let linkClaimPoll = null;

function ask(type) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type }, (response) => {
      // A disconnected worker leaves chrome.runtime.lastError set and calls
      // back with undefined. Reading it here stops Chrome logging an
      // "Unchecked runtime.lastError" for every popup open.
      void chrome.runtime.lastError;
      resolve(response || { ok: false });
    });
  });
}

function show(id, visible) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle("hidden", !visible);
}

async function paintAccount(prefetched) {
  // The popup's single status call already carries the account, so accept
  // it rather than making a second round trip for the same answer.
  const status = prefetched || (await ask("autonomize_account_status"));
  if (!status) return;
  show("accountLinked", Boolean(status.linked));
  show("accountUnlinked", !status.linked);
  show("linkCodeBox", false);
  if (status.linked) {
    document.getElementById("accountEmail").textContent = status.email || "your account";
  }
}

/** Tracking state, from the settings the worker already resolved. */
function paintTracking(settings) {
  const label = document.getElementById("trackingText");
  if (!label) return;
  if (!settings || !settings.tracking) {
    label.textContent = "Tracking —";
    return;
  }
  const on = Object.keys(settings.tracking).filter((k) => settings.tracking[k]);
  label.textContent = on.length
    ? `Tracking on · ${on.length} of 3 categories`
    : "Tracking off";
}

function startCountdown(expiresAt) {
  clearInterval(linkCountdown);
  const label = document.getElementById("linkExpiry");
  const tick = () => {
    const left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    if (left === 0) {
      clearInterval(linkCountdown);
      label.textContent = "This code has expired — generate a new one.";
      return;
    }
    const mins = Math.floor(left / 60);
    const secs = String(left % 60).padStart(2, "0");
    label.textContent = `Expires in ${mins}:${secs}`;
  };
  tick();
  linkCountdown = setInterval(tick, 1000);
}

/** Watches for the dashboard completing the link.
 *
 * Completing a link revokes this install's device token and deletes the
 * account behind it, so the extension MUST pick up the replacement
 * credential or every upload from here on returns 401 and the dashboard
 * stays empty. The worker's alarm covers the popup-closed case, but
 * Chrome floors alarms at one minute; while the popup is open the user is
 * watching, so poll properly.
 */
function startClaimPolling() {
  clearInterval(linkClaimPoll);
  const poll = async () => {
    const result = await ask("autonomize_link_claim");
    if (!result || result.status === "pending") return;
    clearInterval(linkClaimPoll);
    clearInterval(linkCountdown);
    if (result.status === "linked") {
      show("linkCodeBox", false);
      await paintAccount({ linked: true, email: result.email });
      const label = document.getElementById("accountEmail");
      if (label) label.textContent = result.email || "your account";
    } else if (result.status === "expired") {
      const label = document.getElementById("linkExpiry");
      if (label) label.textContent = "That code expired — generate a new one.";
    }
  };
  linkClaimPoll = setInterval(poll, 2000);
  poll();
}

// A link may have completed while the popup was shut. Ask once on open so
// the account shows as linked immediately rather than after the next alarm.
ask("autonomize_link_claim");

window.addEventListener("unload", () => {
  clearInterval(linkClaimPoll);
  clearInterval(linkCountdown);
});

document.getElementById("linkAccount")?.addEventListener("click", async () => {
  const result = await ask("autonomize_link_start");
  if (!result.ok) {
    document.getElementById("linkCode").textContent = "—";
    document.getElementById("linkExpiry").textContent =
      result.error === "not_registered"
        ? "Can't reach the backend right now. Try again in a moment."
        : "Couldn't get a code. Try again in a moment.";
    show("linkCodeBox", true);
    return;
  }
  document.getElementById("linkCode").textContent = result.code;
  startCountdown(result.expiresAt);
  startClaimPolling();
  show("linkCodeBox", true);
  show("accountUnlinked", false);
});

document.getElementById("cancelLink")?.addEventListener("click", () => {
  clearInterval(linkCountdown);
  clearInterval(linkClaimPoll);
  paintAccount();
});

document.getElementById("signOutDevice")?.addEventListener("click", async () => {
  await ask("autonomize_sign_out");
  // Nothing is deleted server-side — signing a device out must never
  // destroy a student's history. The next flush registers a fresh
  // anonymous device and collection continues.
  await paintAccount();
});

paintAccount();
