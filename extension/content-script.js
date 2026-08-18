// Autonomize content script.
//
// This file is the ORCHESTRATOR. It owns no capture logic and knows no
// website: the aggregate shape, the privacy rules, the detectors and the
// site adapters all live in telemetry.js. What happens here is session
// lifecycle — pick a detector, decide this frame's role, accrue active
// time, and flush.
//
// ─────────────────────────────────────────────────────────────────────────
// FRAME ROLES — why this runs in every frame, and why that is not the same
// thing as "all_frames because Google Docs"
// ─────────────────────────────────────────────────────────────────────────
//
// Some editors route keyboard input into a subframe rather than handling it
// in the top document. Google Docs is the well-known case (see
// GoogleDocsAdapter in telemetry.js for the specifics) but it is not the
// only one, and the mechanism is completely generic: DOM events do not
// cross an iframe boundary, so a listener in the top document cannot
// observe input dispatched inside a child document.
//
// Running only in the top frame therefore loses input on any such editor.
// Running independently in every frame is equally wrong: each frame would
// mint its own sessionId, its own flush timer and its own active-time
// accounting, so one page would upload N overlapping sessions and the same
// wall-clock minute would be counted once per frame.
//
// So frames have ROLES:
//
//   TOP FRAME  — owns the session: the sessionId, the category (derived
//                from the real page URL, which a sandboxed subframe cannot
//                see), active-time accounting, capability, and the flush.
//
//   SUBFRAMES  — are sensors. They count input and post raw,
//                category-agnostic deltas to the background worker, which
//                files them per TAB. They never mint a session, never
//                accrue active time, and never flush.
//
// Because each event is dispatched in exactly one document, the frames
// PARTITION the input rather than duplicating it — there is no double
// counting to defend against. The `__autonomizeInjected` guard prevents a
// second listener set within any one document.
//
// A subframe stays silent unless it actually observes input, so an ordinary
// page full of ad and analytics iframes generates no messages at all.
// Category and exclusion gating happen only on the top frame's flush, so a
// subframe can never cause an excluded domain to be tracked: its deltas are
// merged solely into a flush that already passed those checks, and are
// otherwise dropped.

(function () {
  if (window.__autonomizeInjected) return;
  window.__autonomizeInjected = true;

  const T = window.AutonomizeTelemetry;
  if (!T) return; // telemetry.js failed to load; capture nothing rather than guess.

  // `window.top === window` can throw on a cross-origin top. Treating an
  // inaccessible top as "I am a subframe" is the safe answer: a subframe
  // only reports deltas and cannot mint a session.
  let isTopFrame;
  try { isTopFrame = window.top === window; } catch (_) { isTopFrame = false; }

  const FLUSH_INTERVAL_MS = 45_000;
  // Subframes report more often than the top frame flushes, so a tab closed
  // between the two loses at most this much rather than a whole window.
  const FRAME_REPORT_INTERVAL_MS = 10_000;
  const IDLE_TIMEOUT_MS = 5 * 60_000;

  const sink = T.createSink();

  function send(message) {
    try {
      // The callback form swallows "Receiving end does not exist", which is
      // the normal state during an extension reload — without it Chrome
      // logs an unhandled rejection on every batch.
      chrome.runtime.sendMessage(message, () => void chrome.runtime.lastError);
    } catch (_) {
      // Extension context invalidated (reloaded/updated). The next page
      // load re-injects a fresh script; nothing to recover here.
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // SUBFRAME ROLE — a sensor, nothing more.
  //
  // No detector selection: a subframe usually cannot see the real page URL
  // (about:blank, srcdoc, or cross-origin) so it must not try to classify
  // what it is part of. It captures and reports; the top frame's category
  // decides what those counts mean.
  // ═══════════════════════════════════════════════════════════════════
  if (!isTopFrame) {
    T.attachCapture(document, sink);

    const reportTimer = setInterval(() => {
      if (!sink.hasSignal()) return; // stay silent rather than send zeros
      send({ type: 'autonomize_frame_metrics', raw: sink.take() });
    }, FRAME_REPORT_INTERVAL_MS);

    window.addEventListener('pagehide', () => {
      clearInterval(reportTimer);
      if (sink.hasSignal()) send({ type: 'autonomize_frame_metrics', raw: sink.take() });
    });
    return;
  }

  // ═══════════════════════════════════════════════════════════════════
  // TOP FRAME ROLE — owns the session.
  // ═══════════════════════════════════════════════════════════════════

  const hostname = location.hostname;
  let category = autonomizeClassify(hostname, location.pathname);
  const knownSite = category !== 'unknown';

  const detector = T.pickDetector({
    hostname,
    pathname: location.pathname,
    document,
    knownSite
  });

  // Nothing worth tracking on this page. Tell the worker to drop anything
  // this tab's subframes may already have filed, so an unrelated embedded
  // editor cannot leak counts into whatever the user navigates to next.
  if (!detector) {
    send({ type: 'autonomize_frame_discard' });
    return;
  }

  function titleLooksLikeAssessment() {
    const haystack = `${document.title} ${location.pathname}`.toLowerCase();
    return AUTONOMIZE_ASSESSMENT_KEYWORDS.some((kw) => haystack.includes(kw));
  }

  // An unlisted host that a detector claimed. Assessment keywords are
  // checked first so an unlisted institution's quiz page gets the strict
  // treatment rather than being lumped in as ordinary writing.
  if (category === 'unknown') {
    category = titleLooksLikeAssessment() ? 'assessment' : 'writing';
  }

  const isStrict = category === 'assessment';
  // Even a small paste matters during a graded assessment; a normal writing
  // surface only flags genuinely large pastes.
  const LARGE_PASTE_CHARS = isStrict ? 15 : 80;

  const sessionId = crypto.randomUUID();
  const startedAt = Date.now();
  let tabSwitchCount = 0;

  let activeMsAccum = 0;      // since the last flush
  let activeMsTotal = 0;      // whole session; drives the capability downgrade
  let lastActiveTs = document.hasFocus() ? Date.now() : null;

  function accrueActiveTime() {
    if (lastActiveTs === null) return;
    const now = Date.now();
    const delta = Math.min(now - lastActiveTs, IDLE_TIMEOUT_MS);
    activeMsAccum += delta;
    activeMsTotal += delta;
    lastActiveTs = now;
  }

  T.attachCapture(document, sink, {
    onActivity() {
      if (document.hasFocus() && lastActiveTs === null) lastActiveTs = Date.now();
    },
    onPaste(len) {
      if ((category === 'writing' || category === 'assessment') && len >= LARGE_PASTE_CHARS) {
        send({
          type: 'autonomize_paste_event',
          sessionId, category, domain: hostname,
          pastedChars: len, ts: Date.now()
        });
      }
    }
  });

  window.addEventListener('focus', () => { lastActiveTs = Date.now(); });
  window.addEventListener('blur', () => { accrueActiveTime(); lastActiveTs = null; });

  function flush(isFinal) {
    accrueActiveTime();
    const snapshot = sink.take();
    const activeMs = activeMsAccum;
    activeMsAccum = 0;
    const tabSwitches = tabSwitchCount;
    tabSwitchCount = 0;

    // The worker merges this tab's pending SUBFRAME deltas in, so a flush
    // that looks empty from the top frame's own perspective may still carry
    // real typing — a subframe-input editor is exactly that case. Sending
    // whenever there is active time is what makes that merge possible.
    const hasSignal = isFinal || activeMs > 0 || tabSwitches > 0 || T.rawHasSignal(snapshot);
    if (!hasSignal) return;

    send({
      type: 'autonomize_flush',
      sessionId, category,
      domain: hostname,
      path: location.pathname,
      startedAt,
      activeMs,
      raw: snapshot,
      tabSwitchCount: tabSwitches,
      detector: detector.name,
      // Recomputed every flush: a surface that looked measurable at
      // injection may prove not to be, and the reverse — a slow starter
      // that eventually produces input — must not stay marked limited.
      capability: T.resolveCapability(detector.capability, sink, activeMsTotal),
      isFinal: !!isFinal
    });
  }

  const flushTimer = setInterval(() => flush(false), FLUSH_INTERVAL_MS);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      accrueActiveTime();
      // The reliable "switched away" signal — covers both another Chrome tab
      // (an AI chat, say) and another application. A soft signal on its own,
      // since checking a notification also triggers it; scoring weights it
      // lightly and only beyond a small free allowance (see scoring.py).
      if (isStrict) tabSwitchCount += 1;
      flush(false);
    } else {
      lastActiveTs = document.hasFocus() ? Date.now() : null;
    }
  });

  window.addEventListener('pagehide', () => {
    clearInterval(flushTimer);
    flush(true);
  });

  // Single-page navigation: Docs, Notion and Canvas all swap documents
  // without a page load, so the category that was right at injection can
  // stop being right. Finalise the old session rather than attributing new
  // work to it.
  let lastPath = location.pathname;
  setInterval(() => {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    const next = autonomizeClassify(hostname, location.pathname);
    if (next !== 'unknown' && next !== category) {
      flush(true);
      category = next;
    }
  }, 3000);
})();
