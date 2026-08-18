/**
 * AUTONOMIZE — pure presentation logic
 * ====================================
 *
 * The calculations behind the dashboard's charts, with no DOM, no network
 * and no state. Everything here is a function of its arguments, which is
 * the entire point: this is the part that is worth unit-testing directly,
 * and it was previously buried inside closures in `script.js` where
 * nothing could reach it.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * The consolidation removed a second, React dashboard that carried its own
 * Vitest suite. Four of its modules — color, heatmap, weeklyBuckets,
 * compositionLayout — tested pure logic that this dashboard has its own
 * equivalents of. Deleting the components was right; losing direct
 * coverage of the arithmetic was not.
 *
 * This is an EXTRACTION, not a reimplementation. Each function below was
 * lifted from `script.js` with its behaviour unchanged, and `script.js`
 * now calls into here rather than keeping a second copy — two
 * implementations of a bucketing rule is exactly the kind of drift this
 * whole consolidation exists to remove.
 *
 * NOTHING HERE COMPUTES A SCORE. Every score, baseline and risk level
 * arrives already computed from the backend, because the extension, this
 * dashboard and any future client must agree on what a score means, and
 * they only can if exactly one place decides.
 *
 * Loaded as a classic script in the browser (`window.AutonomizeLib`) and
 * required directly by the Node test suite — the same dual export the
 * extension's telemetry.js uses, for the same reason: one file, one
 * implementation, testable without a browser.
 */
(function (global) {
  'use strict';

  var MS_DAY = 86400000;

  // ───────────────────────────────────────────────────────────────────
  // Weekly buckets — tracked minutes per weekday
  // ───────────────────────────────────────────────────────────────────

  /** Monday-first, matching how the axis labels read. */
  var WEEK_ORDER = [1, 2, 3, 4, 5, 6, 0];
  var WEEK_LETTERS = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];

  /**
   * Tracked time per weekday over the last 7 days, as a percentage of the
   * busiest day.
   *
   * A share of the busiest day rather than of a fixed ceiling: the bars
   * are about the SHAPE of a week, and a fixed ceiling would render a
   * light week as a row of stubs that reads as "barely tracked" when it
   * may be a perfectly normal week for that person.
   *
   * `now` is a parameter rather than a call to Date.now() so this is
   * deterministic and can be tested across day boundaries.
   */
  function weeklyBuckets(sessions, now) {
    now = now == null ? Date.now() : now;
    var byDay = {};

    (sessions || []).forEach(function (s) {
      if (!s || !s.started_at) return;
      // Strictly the last seven days. A session older than that belongs to
      // a previous week and would silently inflate the same weekday.
      if (now - s.started_at > 7 * MS_DAY) return;
      var day = new Date(s.started_at).getDay();
      byDay[day] = (byDay[day] || 0) + (s.active_ms || 0);
    });

    // The `concat([1])` floor keeps an all-zero week from dividing by
    // zero; every bar then renders at 0%, which is the truthful picture.
    var busiest = Math.max.apply(null, WEEK_ORDER.map(function (d) {
      return byDay[d] || 0;
    }).concat([1]));

    return WEEK_ORDER.map(function (day, i) {
      var ms = byDay[day] || 0;
      return {
        d: WEEK_LETTERS[i],
        v: Math.round((ms / busiest) * 100),
        minutes: Math.round(ms / 60000)
      };
    });
  }

  // ───────────────────────────────────────────────────────────────────
  // Heatmap — calendar intensity
  // ───────────────────────────────────────────────────────────────────

  /**
   * Intensity bucket for a day's character count.
   *
   * Four levels, and level 0 is reserved for EXACTLY zero. A day with one
   * character is not the same as a day with none, and collapsing them
   * would make the calendar claim a person did nothing on a day they
   * showed up.
   */
  function heatLevel(chars) {
    if (!chars || chars <= 0) return 0;
    if (chars < 700) return 1;
    if (chars < 1600) return 2;
    return 3;
  }

  /** Are two dates the same calendar day, in local time? */
  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() &&
           a.getMonth() === b.getMonth() &&
           a.getDate() === b.getDate();
  }

  /**
   * Totals for one calendar day across every session that fell on it.
   *
   * `flagged` marks a graded session scoring below 40 — the calendar shows
   * it distinctly because a low score on an assessment means something
   * different from a low score while drafting.
   */
  function dayTotals(sessions, date) {
    return (sessions || []).reduce(function (acc, s) {
      if (!s || !s.started_at) return acc;
      if (!sameDay(new Date(s.started_at), date)) return acc;
      acc.typed += s.typed_chars || 0;
      acc.pasted += s.pasted_chars || 0;
      acc.mins += Math.round((s.active_ms || 0) / 60000);
      if (s.category === 'assessment' && s.score != null && s.score < 40) {
        acc.flagged = true;
      }
      return acc;
    }, { typed: 0, pasted: 0, mins: 0, flagged: false });
  }

  // ───────────────────────────────────────────────────────────────────
  // Composition chart layout
  // ───────────────────────────────────────────────────────────────────

  /**
   * A "nice" axis maximum at or above `v`.
   *
   * Rounds up to a half-power-of-ten so the axis lands on a readable
   * number instead of whatever the data happened to peak at.
   */
  function niceMax(v) {
    if (!v || v <= 0) return 1;
    var step = Math.pow(10, Math.floor(Math.log(v) / Math.LN10)) / 2;
    return Math.ceil(v / step) * step;
  }

  var DEFAULT_DIMS = { W: 760, H: 300, PL: 46, PR: 14, PT: 16, PB: 44 };

  /**
   * Pixel geometry for the typed-vs-pasted chart.
   *
   * BOTH SERIES SHARE ONE SCALE, derived from the larger of the two peaks.
   * Giving each its own axis would make a day of 40 pasted characters look
   * identical to a day of 4000 typed ones — the comparison between them is
   * the entire content of the chart, and separate scales destroy it. This
   * is the same reason the project refuses dual-axis charts generally.
   *
   * The single-row case is the reason `step` exists as its own value:
   * `PLOT_W / (rows.length - 1)` divides by zero at length 1 — which is
   * the NORMAL state for a new account, not an edge case — and every x
   * came out NaN, so the SVG rejected the path and the chart silently
   * vanished. One point sits at the left edge.
   */
  function compositionLayout(rows, dims) {
    dims = Object.assign({}, DEFAULT_DIMS, dims || {});
    var plotW = dims.W - dims.PL - dims.PR;
    var plotH = dims.H - dims.PT - dims.PB;
    rows = rows || [];

    var peak = 0;
    rows.forEach(function (r) {
      peak = Math.max(peak, r.wrote || 0, r.pasted || 0);
    });
    var top = niceMax(peak);

    var step = rows.length > 1 ? plotW / (rows.length - 1) : 0;

    function xAt(i) { return dims.PL + i * step; }
    function yAt(value) {
      return dims.PT + plotH - ((value || 0) / top) * plotH;
    }

    return {
      top: top,
      step: step,
      plotW: plotW,
      plotH: plotH,
      baseline: dims.PT + plotH,
      xAt: xAt,
      yAt: yAt,
      points: function (key) {
        return rows.map(function (r, i) { return [xAt(i), yAt(r[key])]; });
      },
      /** Index of the row nearest an x pixel, clamped into range. */
      indexAt: function (x) {
        if (!rows.length) return -1;
        if (step <= 0) return 0;
        var i = Math.round((x - dims.PL) / step);
        return Math.max(0, Math.min(rows.length - 1, i));
      }
    };
  }

  // ───────────────────────────────────────────────────────────────────
  // Colour / tone selection
  //
  // Tone is chosen by what a value MEANS, never by its rank in a list —
  // a filter that changes how many sessions are shown must not repaint
  // the survivors.
  // ───────────────────────────────────────────────────────────────────

  /**
   * Tone for a session row.
   *
   * AI-assistant sessions are amber regardless of score: they are not bad,
   * but they are not independent work either, and colouring them by score
   * would imply a judgement the data does not support.
   */
  function sessionTone(session) {
    if (!session) return 'muted';
    if (session.category === 'ai_assistant') return 'amber';
    if (session.score != null && session.score < 40) return 'risk';
    return 'muted';
  }

  /** Badge modifier for a server-supplied risk level. Status colours are
   *  reserved and never reused for an ordinary series. */
  function riskBadgeClass(level) {
    if (level === 'low') return 'ok';
    if (level === 'medium') return 'warn';
    if (level === 'high') return 'risk';
    return 'muted';
  }

  // ───────────────────────────────────────────────────────────────────
  // Formatting
  // ───────────────────────────────────────────────────────────────────

  /** Splits minutes into `{h, m}` display parts. */
  function fmtMinutes(mins) {
    var total = Math.round(mins || 0);
    var h = Math.floor(total / 60);
    return { h: (h ? h : 0) + 'h', m: (total % 60) + 'm' };
  }

  /**
   * A duration for inline text.
   *
   * Null renders as an em dash, never as "0m". "No data yet" and "zero"
   * are different claims, and a new user reading 0 concludes the tracking
   * is broken — or worse, believes it.
   */
  function duration(mins) {
    if (mins == null) return '—';
    var total = Math.round(mins);
    var h = Math.floor(total / 60);
    var m = total % 60;
    return h ? h + 'h ' + m + 'm' : m + 'm';
  }

  /** Thousands-abbreviated axis label. */
  function kfmt(v) {
    return v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + 'k' : String(v);
  }

  var api = {
    MS_DAY: MS_DAY,
    WEEK_ORDER: WEEK_ORDER,
    WEEK_LETTERS: WEEK_LETTERS,
    weeklyBuckets: weeklyBuckets,
    heatLevel: heatLevel,
    sameDay: sameDay,
    dayTotals: dayTotals,
    niceMax: niceMax,
    compositionLayout: compositionLayout,
    DEFAULT_DIMS: DEFAULT_DIMS,
    sessionTone: sessionTone,
    riskBadgeClass: riskBadgeClass,
    fmtMinutes: fmtMinutes,
    duration: duration,
    kfmt: kfmt
  };

  global.AutonomizeLib = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
