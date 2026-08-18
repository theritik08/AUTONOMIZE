// Autonomize — telemetry core.
//
// This file owns the COMMON TELEMETRY INTERFACE. Every detector and every
// site adapter produces the same aggregate shape, so the background worker,
// the API payload, the database schema and the dashboard never learn the
// name of any particular website. Adding an adapter for a new proprietary
// editor is a change to this file's registry and nothing else.
//
// ─────────────────────────────────────────────────────────────────────────
// PRIVACY CONTRACT — binding on every detector below, without exception
// ─────────────────────────────────────────────────────────────────────────
//   - The actual text a user types is NEVER read, stored, or transmitted.
//   - Pasted content is measured with `.length` and the string is discarded
//     in the same expression that reads it.
//   - Keystrokes are counted into buckets (printable / backspace / undo /
//     enter) by classifying `event.key`. The key identity is never kept.
//   - ORDERED keystroke sequences are never retained. Rhythm is a HISTOGRAM
//     of inter-keystroke intervals, because it is the ORDERING that could
//     carry content — destroying it is the entire safety argument. Two
//     sessions with identical bucket counts are indistinguishable here no
//     matter what was typed in them.
//   - Password fields and other sensitive inputs are excluded from counting
//     altogether — see isSensitiveTarget().
//   - No hardware fingerprints, no page content, no form values, no
//     screenshots, no browsing history.
//
// ─────────────────────────────────────────────────────────────────────────
// THE COMMON AGGREGATE SHAPE
// ─────────────────────────────────────────────────────────────────────────
// Every detector accumulates into one `raw` object. It is deliberately
// CATEGORY-AGNOSTIC: it records what the browser did, not what it meant. A
// subframe usually cannot see the real page URL, so it cannot classify what
// it is part of; the top frame supplies the category and the background
// worker applies it once, in rawToMetrics(). That separation is what lets a
// sensor run anywhere without knowing anything about the site.
//
//   printable      count of printable-character keydowns
//   backspace      count of Backspace/Delete
//   undo           count of Ctrl/Cmd+Z
//   cut            count of cut events (a bulk revision)
//   enter          count of bare Enter (a prompt submission, on AI surfaces)
//   pasted_chars   SUM OF LENGTHS of pasted text — never the text
//   iki_buckets    histogram of gaps between printable keys
//   long_pauses    gaps at or beyond the top bucket edge
//   burst_keys     keys arriving faster than BURST_MS
//
// ─────────────────────────────────────────────────────────────────────────
// CAPABILITY — the honesty mechanism
// ─────────────────────────────────────────────────────────────────────────
// Some editors cannot be measured. A canvas-rendered editor that swallows
// key events, a cross-origin editing frame this extension may not inject
// into, a Flash/WASM surface — the browser genuinely does not expose the
// signal, and no amount of code changes that.
//
// For those the honest answer is "limited tracking", not a fabricated zero
// that is indistinguishable from "wrote nothing". Each detector DECLARES a
// capability, and the session can be DOWNGRADED at runtime when the page is
// focused and accruing active time while producing no input events at all.
// The capability travels with the session to the dashboard, which labels it.
//
//   "full"     keystroke, paste, revision and active time are all observable
//   "limited"  only active time, site and category can be observed here
//
// Never resolve a limited surface by inventing numbers for it.

(function (global) {
  'use strict';

  // Inter-keystroke-interval buckets, in ms. Log-spaced because the
  // interesting structure is at the short end — the difference between 90ms
  // and 160ms says far more about what someone is doing than the difference
  // between 2s and 4s. backend/rhythm.py owns the identical list; if the two
  // ever disagree the backend refuses the row rather than comparing
  // histograms whose buckets mean different things.
  const IKI_BUCKET_EDGES_MS = [60, 120, 200, 320, 500, 900, 2000];
  const IKI_BUCKET_COUNT = IKI_BUCKET_EDGES_MS.length + 1;

  // A gap longer than this is someone leaving, not someone thinking.
  // Counting it would let a lunch break masquerade as deliberation.
  const MAX_MEANINGFUL_GAP_MS = 60_000;
  const BURST_MS = 120;
  const LONG_PAUSE_MS = 2000;

  // How long a surface may be focused and accruing active time with zero
  // observed input before its capability is downgraded to "limited". Long
  // enough that reading a document for a minute doesn't trip it.
  const CAPABILITY_DOWNGRADE_MS = 90_000;

  function emptyRaw() {
    return {
      printable: 0,
      backspace: 0,
      undo: 0,
      cut: 0,
      enter: 0,
      pasted_chars: 0,
      iki_buckets: new Array(IKI_BUCKET_COUNT).fill(0),
      long_pauses: 0,
      burst_keys: 0
    };
  }

  function rawHasSignal(r) {
    if (!r) return false;
    return (
      r.printable > 0 || r.backspace > 0 || r.undo > 0 || r.cut > 0 ||
      r.enter > 0 || r.pasted_chars > 0 ||
      (Array.isArray(r.iki_buckets) && r.iki_buckets.some((c) => c > 0))
    );
  }

  function addRaw(into, delta) {
    if (!delta) return into;
    into.printable += delta.printable || 0;
    into.backspace += delta.backspace || 0;
    into.undo += delta.undo || 0;
    into.cut += delta.cut || 0;
    into.enter += delta.enter || 0;
    into.pasted_chars += delta.pasted_chars || 0;
    into.long_pauses += delta.long_pauses || 0;
    into.burst_keys += delta.burst_keys || 0;
    const buckets = Array.isArray(delta.iki_buckets) ? delta.iki_buckets : [];
    for (let i = 0; i < IKI_BUCKET_COUNT; i++) {
      into.iki_buckets[i] += buckets[i] || 0;
    }
    return into;
  }

  /**
   * Sensitive-target refusal.
   *
   * A password manager's field, a card number, a one-time code — counting
   * keystrokes in these is both useless (they are not "writing") and
   * exactly the kind of thing that makes a keystroke counter indefensible.
   * The check is on the element TYPE and autocomplete hint only; the value
   * is never read.
   */
  const SENSITIVE_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-|"?card)/i;

  function isSensitiveTarget(target) {
    if (!target || typeof target !== 'object') return false;
    const tag = (target.tagName || '').toLowerCase();
    if (tag !== 'input' && tag !== 'textarea') {
      // A contenteditable can still be marked sensitive by the page.
      return target.getAttribute && target.getAttribute('data-sensitive') != null;
    }
    const type = (target.type || '').toLowerCase();
    if (type === 'password' || type === 'hidden') return true;
    const ac = target.getAttribute && target.getAttribute('autocomplete');
    if (ac && SENSITIVE_AUTOCOMPLETE.test(ac)) return true;
    const name = ((target.name || '') + ' ' + (target.id || '')).toLowerCase();
    return /pass|passwd|pwd|otp|cvv|cvc|ssn|card.?number/.test(name);
  }

  // ───────────────────────────────────────────────────────────────────
  // Sink — the shared accumulator every detector writes into.
  // ───────────────────────────────────────────────────────────────────

  function createSink() {
    let raw = emptyRaw();
    let lastTypedTs = null;
    let sawAnyInput = false;

    function recordInterval(now) {
      if (lastTypedTs === null) { lastTypedTs = now; return; }
      const dt = now - lastTypedTs;
      lastTypedTs = now;
      if (dt < 0 || dt > MAX_MEANINGFUL_GAP_MS) return;

      let bucket = IKI_BUCKET_EDGES_MS.length;
      for (let i = 0; i < IKI_BUCKET_EDGES_MS.length; i++) {
        if (dt < IKI_BUCKET_EDGES_MS[i]) { bucket = i; break; }
      }
      raw.iki_buckets[bucket] += 1;
      if (dt >= LONG_PAUSE_MS) raw.long_pauses += 1;
      if (dt < BURST_MS) raw.burst_keys += 1;
    }

    return {
      /** Classifies one keydown. Never inspects the key beyond its bucket. */
      key(event, now) {
        if (isSensitiveTarget(event.target)) return;
        const key = event.key;
        if (typeof key !== 'string') return;
        sawAnyInput = true;

        if (key === 'Backspace' || key === 'Delete') {
          raw.backspace += 1;
        } else if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === 'z') {
          raw.undo += 1;
        } else if (key === 'Enter' && !event.shiftKey) {
          raw.enter += 1;
        } else if (key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) {
          raw.printable += 1;
          // Measured between printable keys only. Including backspaces
          // would fold revision behaviour into the cadence measure, and
          // revision is already counted separately.
          recordInterval(now);
        }
      },

      /** Records a paste by LENGTH ONLY. */
      paste(length, target) {
        if (isSensitiveTarget(target)) return;
        sawAnyInput = true;
        raw.pasted_chars += length;
      },

      cut(target) {
        if (isSensitiveTarget(target)) return;
        sawAnyInput = true;
        raw.cut += 1;
      },

      merge(delta) { addRaw(raw, delta); },
      hasSignal() { return rawHasSignal(raw); },
      sawInput() { return sawAnyInput; },

      /** Drains the accumulator. The caller owns the returned object. */
      take() {
        const snapshot = raw;
        raw = emptyRaw();
        lastTypedTs = null;
        return snapshot;
      }
    };
  }

  // ───────────────────────────────────────────────────────────────────
  // Detectors.
  //
  // Every detector implements the same interface:
  //
  //   name         stable identifier, used in tests and diagnostics
  //   capability   "full" | "limited" — what this surface can honestly report
  //   matches(ctx) does this detector claim the page?
  //
  // They share ONE capture core (attachCapture below) because the events
  // are genuinely the same everywhere — document-level keydown/paste/cut in
  // the capture phase. Pretending otherwise would be fake polymorphism.
  // What actually differs between them is DETECTION and CAPABILITY, and
  // that is exactly what each one declares.
  // ───────────────────────────────────────────────────────────────────

  function hasSubstantialEditable(doc) {
    const editables = doc.querySelectorAll(
      "textarea, [contenteditable=''], [contenteditable='true']"
    );
    for (const el of editables) {
      if (el.offsetWidth > 200 && el.offsetHeight > 60) return true;
    }
    return false;
  }

  function hasAnyTextInput(doc) {
    return !!doc.querySelector(
      "textarea, input[type='text'], input[type='search'], input[type='email'], " +
      "input[type='url'], input:not([type]), [contenteditable=''], [contenteditable='true']"
    );
  }

  /**
   * GoogleDocsAdapter — the reference example of a SITE ADAPTER.
   *
   * WHY IT EXISTS (and this is the root cause of the reported bug):
   * Google Docs does not put a contenteditable in the top document. The
   * visible page is a canvas/SVG rendering with no editable surface at all,
   * and keyboard input is routed into a hidden ~1x1px iframe
   * (`.docs-texteventtarget-iframe`) whose document is the real event
   * target. DOM events do not cross an iframe boundary, so a keydown
   * dispatched inside that iframe never reaches the top document.
   *
   * The old build injected only into the top frame, so its document-level
   * keydown listener was watching a document that receives no keystrokes.
   * Active time accrued (that is measured on the top frame's focus), the
   * domain and category were correct, and the upload returned 200 — which
   * is precisely why it looked like a backend problem rather than a
   * capture problem.
   *
   * The adapter does NOT contain Google-specific event handling. It only
   * declares that this surface's input arrives in a subframe, which the
   * generic frame-relay in content-script.js already handles for every
   * site. Nothing about the product is built around this one website.
   */
  const GoogleDocsAdapter = {
    name: 'google-docs',
    capability: 'full',
    inputInSubframe: true,
    matches(ctx) {
      return ctx.hostname === 'docs.google.com' &&
             /^\/(document|spreadsheets|presentation)\//.test(ctx.pathname);
    }
  };

  /**
   * Office / Notion / Overleaf class editors: rich contenteditable in the
   * top document, occasionally with editing panes in same-origin frames.
   * Treated as full capability with relay enabled, because the relay is
   * harmless when there are no subframes reporting.
   */
  const RichEditorAdapter = {
    name: 'rich-editor',
    capability: 'full',
    inputInSubframe: true,
    matches(ctx) {
      return /(^|\.)(notion\.so|overleaf\.com|office\.com|officeapps\.live\.com|onedrive\.live\.com)$/
        .test(ctx.hostname);
    }
  };

  /** A page with a real editing surface in its own document. The common case. */
  const ContentEditableDetector = {
    name: 'contenteditable',
    capability: 'full',
    inputInSubframe: false,
    matches(ctx) {
      return hasSubstantialEditable(ctx.document);
    }
  };

  /** Ordinary inputs and textareas — comment boxes, search-driven editors,
   *  code sandboxes, LMS answer fields. */
  const GenericInputDetector = {
    name: 'generic-input',
    capability: 'full',
    inputInSubframe: false,
    matches(ctx) {
      return hasAnyTextInput(ctx.document);
    }
  };

  /**
   * FallbackActivityDetector — claims a page only when the site map already
   * said this domain is worth tracking, but no editable surface can be
   * found. Reports active time, site and category honestly and declares
   * itself LIMITED so the dashboard can say so rather than showing a zero
   * that looks like "wrote nothing".
   */
  const FallbackActivityDetector = {
    name: 'fallback-activity',
    capability: 'limited',
    inputInSubframe: true,
    matches() { return true; }
  };

  // Order matters: most specific first. A site adapter always wins over the
  // generic detectors, and the fallback only ever runs last.
  const REGISTRY = [
    GoogleDocsAdapter,
    RichEditorAdapter,
    ContentEditableDetector,
    GenericInputDetector,
    FallbackActivityDetector
  ];

  /**
   * Picks the detector for a page.
   *
   * `knownSite` is true when site-map.js already classified this host — it
   * is what allows the fallback to claim a page. An UNLISTED host with no
   * editable surface is not tracked at all, which is what keeps this from
   * being a tracker for every website a person merely browses.
   */
  function pickDetector(ctx) {
    for (const detector of REGISTRY) {
      if (detector === FallbackActivityDetector && !ctx.knownSite) return null;
      if (detector.matches(ctx)) return detector;
    }
    return null;
  }

  /**
   * Attaches the shared capture listeners to a document.
   *
   * `capture: true` so a page that stops propagation on its own handlers
   * (Google Docs does, aggressively) cannot suppress the count.
   * `passive: true` so this can never delay or alter the page's own input
   * handling — the listener is strictly read-only: it never calls
   * preventDefault, never mutates the event, and never reads a value.
   *
   * Returns a detach function.
   */
  function attachCapture(doc, sink, hooks) {
    hooks = hooks || {};

    function onKeyDown(e) {
      if (hooks.onActivity) hooks.onActivity();
      sink.key(e, Date.now());
    }

    function onPaste(e) {
      if (hooks.onActivity) hooks.onActivity();
      let len = 0;
      try {
        // Read, measure, discard — all in one expression. The string is
        // never assigned anywhere that outlives this call.
        len = (e.clipboardData ? e.clipboardData.getData('text') : '').length;
      } catch (_) {
        len = 0;
      }
      sink.paste(len, e.target);
      if (len > 0 && hooks.onPaste) hooks.onPaste(len, e.target);
    }

    function onCut(e) {
      if (hooks.onActivity) hooks.onActivity();
      sink.cut(e.target);
    }

    const opts = { capture: true, passive: true };
    doc.addEventListener('keydown', onKeyDown, opts);
    doc.addEventListener('paste', onPaste, opts);
    doc.addEventListener('cut', onCut, opts);

    return function detach() {
      doc.removeEventListener('keydown', onKeyDown, opts);
      doc.removeEventListener('paste', onPaste, opts);
      doc.removeEventListener('cut', onCut, opts);
    };
  }

  /**
   * Runtime capability downgrade.
   *
   * A detector's declared capability is a claim made before any input has
   * happened. This turns it into an observation: if the page has been
   * focused and accruing active time for CAPABILITY_DOWNGRADE_MS and not a
   * single input event has been seen, this surface is not measurable here,
   * and saying "limited" is the truthful answer.
   */
  function resolveCapability(declared, sink, activeMsTotal) {
    if (declared === 'limited') return 'limited';
    if (sink.sawInput()) return 'full';
    return activeMsTotal >= CAPABILITY_DOWNGRADE_MS ? 'limited' : 'full';
  }

  const api = {
    IKI_BUCKET_COUNT,
    IKI_BUCKET_EDGES_MS,
    emptyRaw,
    addRaw,
    rawHasSignal,
    isSensitiveTarget,
    createSink,
    attachCapture,
    pickDetector,
    resolveCapability,
    CAPABILITY_DOWNGRADE_MS,
    detectors: {
      GoogleDocsAdapter,
      RichEditorAdapter,
      ContentEditableDetector,
      GenericInputDetector,
      FallbackActivityDetector
    },
    REGISTRY
  };

  global.AutonomizeTelemetry = api;
  // Also exported for the Node-based regression tests, which exercise the
  // registry and the aggregate shape without a browser.
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
