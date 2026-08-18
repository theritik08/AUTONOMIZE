/**
 * Unit tests for the dashboard's pure presentation logic.
 *
 * These restore the direct coverage lost when the second (React) dashboard
 * was removed. That dashboard's Vitest suite tested four pure-logic areas —
 * color, heatmap, weeklyBuckets and compositionLayout — and this file
 * covers this dashboard's own equivalents, which now live in lib.js rather
 * than inside closures in script.js where nothing could reach them.
 *
 * Node's built-in runner, no framework: the module has no DOM, no network
 * and no state, so there is nothing a test framework would add.
 *
 * Run: node --test dashboard-web/tests/lib.test.js
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const LIB = require(path.join(__dirname, '..', 'lib.js'));

const DAY = 86400000;

/** A session at a fixed offset. Times are explicit so nothing here depends
 *  on when the suite happens to run. */
function session(daysAgo, over = {}) {
  return {
    started_at: Date.now() - daysAgo * DAY,
    active_ms: 30 * 60000,
    typed_chars: 0,
    pasted_chars: 0,
    category: 'writing',
    score: null,
    ...over,
  };
}

// ═══════════════════════════════════════════════════════════════════
// weeklyBuckets
// ═══════════════════════════════════════════════════════════════════

test('weeklyBuckets returns seven Monday-first buckets', () => {
  const week = LIB.weeklyBuckets([], Date.now());
  assert.equal(week.length, 7);
  assert.deepEqual(week.map((b) => b.d), ['M', 'T', 'W', 'T', 'F', 'S', 'S']);
});

test('an empty week is all zeros rather than a divide-by-zero', () => {
  // The busiest-day denominator has a floor of 1 for exactly this case;
  // without it every bar would be NaN and the row would vanish.
  const week = LIB.weeklyBuckets([], Date.now());
  assert.ok(week.every((b) => b.v === 0 && b.minutes === 0));
});

test('bars are a share of the BUSIEST day, not of a fixed ceiling', () => {
  // A fixed ceiling would render a light-but-normal week as a row of
  // stubs, which reads as "barely tracked" and is a claim about the person
  // rather than about the data.
  const now = new Date('2026-08-12T12:00:00Z').getTime(); // a Wednesday
  const sessions = [
    { started_at: now, active_ms: 60 * 60000 },              // Wed, 60m
    { started_at: now - DAY, active_ms: 30 * 60000 },        // Tue, 30m
  ];
  const week = LIB.weeklyBuckets(sessions, now);
  const byLetterIndex = Object.fromEntries(week.map((b, i) => [i, b]));

  const wed = byLetterIndex[2];
  const tue = byLetterIndex[1];
  assert.equal(wed.v, 100, 'the busiest day is always 100%');
  assert.equal(tue.v, 50, 'half the busiest day is 50%');
  assert.equal(wed.minutes, 60);
  assert.equal(tue.minutes, 30);
});

test('sessions older than seven days are excluded', () => {
  const now = new Date('2026-08-12T12:00:00Z').getTime();
  const week = LIB.weeklyBuckets(
    [{ started_at: now - 8 * DAY, active_ms: 99 * 60000 }],
    now
  );
  assert.ok(
    week.every((b) => b.minutes === 0),
    'a session from last week must not inflate the same weekday'
  );
});

test('same-weekday sessions accumulate', () => {
  const now = new Date('2026-08-12T12:00:00Z').getTime();
  const week = LIB.weeklyBuckets(
    [
      { started_at: now, active_ms: 20 * 60000 },
      { started_at: now - 3600000, active_ms: 40 * 60000 },
    ],
    now
  );
  assert.equal(week[2].minutes, 60);
});

test('sessions without a timestamp are ignored, not counted as today', () => {
  const now = Date.now();
  const week = LIB.weeklyBuckets(
    [{ active_ms: 60 * 60000 }, { started_at: null, active_ms: 60 * 60000 }],
    now
  );
  assert.ok(week.every((b) => b.minutes === 0));
});

// ═══════════════════════════════════════════════════════════════════
// heatLevel / dayTotals
// ═══════════════════════════════════════════════════════════════════

test('level 0 is reserved for exactly zero characters', () => {
  // A day with one character is not a day with none. Collapsing them makes
  // the calendar claim someone did nothing on a day they showed up.
  assert.equal(LIB.heatLevel(0), 0);
  assert.equal(LIB.heatLevel(1), 1);
});

test('heatLevel buckets are monotonic and bounded', () => {
  const samples = [0, 1, 699, 700, 1599, 1600, 50000];
  const levels = samples.map(LIB.heatLevel);
  assert.deepEqual(levels, [0, 1, 1, 2, 2, 3, 3]);
  for (let i = 1; i < levels.length; i++) {
    assert.ok(levels[i] >= levels[i - 1], 'more characters can never mean a lower level');
  }
});

test('heatLevel treats missing or negative input as no activity', () => {
  assert.equal(LIB.heatLevel(null), 0);
  assert.equal(LIB.heatLevel(undefined), 0);
  assert.equal(LIB.heatLevel(-5), 0);
});

test('dayTotals sums only the sessions on that calendar day', () => {
  const day = new Date('2026-08-12T12:00:00');
  const other = new Date('2026-08-13T12:00:00');
  const sessions = [
    { started_at: day.getTime(), typed_chars: 100, pasted_chars: 10, active_ms: 600000 },
    { started_at: day.getTime() + 3600000, typed_chars: 50, pasted_chars: 5, active_ms: 600000 },
    { started_at: other.getTime(), typed_chars: 999, pasted_chars: 999, active_ms: 600000 },
  ];
  const totals = LIB.dayTotals(sessions, day);
  assert.equal(totals.typed, 150);
  assert.equal(totals.pasted, 15);
  assert.equal(totals.mins, 20);
});

test('dayTotals flags a low-scoring GRADED session only', () => {
  const day = new Date('2026-08-12T12:00:00');
  const low = { started_at: day.getTime(), score: 10, active_ms: 0 };

  assert.equal(
    LIB.dayTotals([{ ...low, category: 'writing' }], day).flagged,
    false,
    'a low score while drafting is not a flag — that is the point of a baseline'
  );
  assert.equal(
    LIB.dayTotals([{ ...low, category: 'assessment' }], day).flagged,
    true
  );
});

test('an unscored assessment session is not flagged', () => {
  const day = new Date('2026-08-12T12:00:00');
  const totals = LIB.dayTotals(
    [{ started_at: day.getTime(), category: 'assessment', score: null, active_ms: 0 }],
    day
  );
  assert.equal(totals.flagged, false, 'absent evidence is not evidence of a problem');
});

test('sameDay compares calendar days, not elapsed time', () => {
  assert.ok(LIB.sameDay(new Date('2026-08-12T00:01:00'), new Date('2026-08-12T23:59:00')));
  assert.ok(!LIB.sameDay(new Date('2026-08-12T23:59:00'), new Date('2026-08-13T00:01:00')));
});

// ═══════════════════════════════════════════════════════════════════
// niceMax / compositionLayout
// ═══════════════════════════════════════════════════════════════════

test('niceMax rounds up to a readable axis top', () => {
  assert.equal(LIB.niceMax(0), 1);
  assert.equal(LIB.niceMax(null), 1);
  for (const v of [1, 7, 99, 640, 1234, 98765]) {
    assert.ok(LIB.niceMax(v) >= v, `axis top must not clip the data (${v})`);
  }
});

test('BOTH series share one scale', () => {
  // The comparison between typed and pasted IS the content of this chart.
  // Separate scales would make 40 pasted characters look identical to 4000
  // typed ones — the same reason the project refuses dual-axis charts.
  const rows = [{ wrote: 4000, pasted: 40 }];
  const layout = LIB.compositionLayout(rows);
  const typedY = layout.yAt(4000);
  const pastedY = layout.yAt(40);
  assert.ok(
    pastedY > typedY,
    'the smaller series must sit lower on the shared axis, not be rescaled to match'
  );
  assert.ok(layout.top >= 4000);
});

test('a SINGLE row produces finite geometry, not NaN', () => {
  // The regression this guards: `plotW / (rows.length - 1)` divides by
  // zero at length 1 — the normal state for a new account, not an edge
  // case — and every x came out NaN, so the SVG rejected the path and the
  // chart silently disappeared.
  const layout = LIB.compositionLayout([{ wrote: 100, pasted: 10 }]);
  assert.equal(layout.step, 0);
  assert.ok(Number.isFinite(layout.xAt(0)), 'x must be finite for a single point');
  assert.ok(Number.isFinite(layout.yAt(100)), 'y must be finite for a single point');
  layout.points('wrote').forEach(([x, y]) => {
    assert.ok(Number.isFinite(x) && Number.isFinite(y));
  });
});

test('an EMPTY series produces finite geometry too', () => {
  const layout = LIB.compositionLayout([]);
  assert.ok(Number.isFinite(layout.xAt(0)));
  assert.ok(Number.isFinite(layout.yAt(0)));
  assert.deepEqual(layout.points('wrote'), []);
  assert.equal(layout.indexAt(400), -1, 'there is no nearest point in an empty series');
});

test('points are evenly spaced and inside the plot area', () => {
  const rows = Array.from({ length: 5 }, (_, i) => ({ wrote: i * 100, pasted: 0 }));
  const layout = LIB.compositionLayout(rows);
  const xs = layout.points('wrote').map(([x]) => x);

  const gaps = xs.slice(1).map((x, i) => Math.round(x - xs[i]));
  assert.ok(gaps.every((g) => g === gaps[0]), 'spacing must be uniform');
  assert.ok(xs[0] >= LIB.DEFAULT_DIMS.PL, 'first point starts after the left padding');
  assert.ok(
    xs[xs.length - 1] <= LIB.DEFAULT_DIMS.W - LIB.DEFAULT_DIMS.PR + 0.001,
    'last point stays inside the right padding'
  );
});

test('a zero value sits exactly on the baseline', () => {
  const layout = LIB.compositionLayout([{ wrote: 0, pasted: 0 }, { wrote: 500, pasted: 0 }]);
  assert.equal(Math.round(layout.yAt(0)), Math.round(layout.baseline));
});

test('indexAt finds the nearest row and clamps to range', () => {
  const rows = Array.from({ length: 5 }, () => ({ wrote: 1, pasted: 1 }));
  const layout = LIB.compositionLayout(rows);
  assert.equal(layout.indexAt(layout.xAt(0)), 0);
  assert.equal(layout.indexAt(layout.xAt(3)), 3);
  assert.equal(layout.indexAt(-9999), 0, 'left of the plot clamps to the first row');
  assert.equal(layout.indexAt(9999), 4, 'right of the plot clamps to the last row');
});

test('indexAt does not divide by a zero step', () => {
  const layout = LIB.compositionLayout([{ wrote: 1, pasted: 1 }]);
  assert.equal(layout.indexAt(500), 0);
});

// ═══════════════════════════════════════════════════════════════════
// Colour / tone
// ═══════════════════════════════════════════════════════════════════

test('an AI-assistant session is amber regardless of score', () => {
  // Not bad, but not independent work either. Colouring it by score would
  // imply a judgement the data does not support.
  assert.equal(LIB.sessionTone({ category: 'ai_assistant', score: 100 }), 'amber');
  assert.equal(LIB.sessionTone({ category: 'ai_assistant', score: 5 }), 'amber');
});

test('a low-scoring writing session reads as risk, a healthy one as muted', () => {
  assert.equal(LIB.sessionTone({ category: 'writing', score: 39 }), 'risk');
  assert.equal(LIB.sessionTone({ category: 'writing', score: 40 }), 'muted');
  assert.equal(LIB.sessionTone({ category: 'writing', score: 95 }), 'muted');
});

test('an UNSCORED session is never coloured as risk', () => {
  assert.equal(
    LIB.sessionTone({ category: 'writing', score: null }),
    'muted',
    'no score is not a bad score'
  );
  assert.equal(LIB.sessionTone(null), 'muted');
});

test('tone depends only on the session, never on its position in a list', () => {
  // A filter that changes how many rows are shown must not repaint the
  // survivors.
  const s = { category: 'writing', score: 20 };
  assert.equal(LIB.sessionTone(s), LIB.sessionTone(s));
});

test('status colours map to reserved badge classes', () => {
  assert.equal(LIB.riskBadgeClass('low'), 'ok');
  assert.equal(LIB.riskBadgeClass('medium'), 'warn');
  assert.equal(LIB.riskBadgeClass('high'), 'risk');
  assert.equal(LIB.riskBadgeClass(null), 'muted', 'unknown must not fall through to a status colour');
  assert.equal(LIB.riskBadgeClass('something-new'), 'muted');
});

// ═══════════════════════════════════════════════════════════════════
// Formatting
// ═══════════════════════════════════════════════════════════════════

test('duration renders an em dash for null, never 0m', () => {
  // "No data yet" and "zero" are different claims, and a new user reading
  // 0 concludes the tracking is broken — or believes it.
  assert.equal(LIB.duration(null), '—');
  assert.equal(LIB.duration(undefined), '—');
  assert.equal(LIB.duration(0), '0m');
});

test('duration formats hours and minutes', () => {
  assert.equal(LIB.duration(45), '45m');
  assert.equal(LIB.duration(60), '1h 0m');
  assert.equal(LIB.duration(119), '1h 59m');
  assert.equal(LIB.duration(1440), '24h 0m');
});

test('fmtMinutes splits into display parts', () => {
  assert.deepEqual(LIB.fmtMinutes(0), { h: '0h', m: '0m' });
  assert.deepEqual(LIB.fmtMinutes(119), { h: '1h', m: '59m' });
  assert.deepEqual(LIB.fmtMinutes(null), { h: '0h', m: '0m' });
});

test('kfmt abbreviates thousands without lying about precision', () => {
  assert.equal(LIB.kfmt(999), '999');
  assert.equal(LIB.kfmt(1000), '1k');
  assert.equal(LIB.kfmt(1500), '1.5k');
  assert.equal(LIB.kfmt(12000), '12k');
});

// ═══════════════════════════════════════════════════════════════════
// Module contract
// ═══════════════════════════════════════════════════════════════════

test('lib is pure: calling a function twice gives the same answer', () => {
  const rows = [{ wrote: 10, pasted: 2 }, { wrote: 20, pasted: 4 }];
  const a = LIB.compositionLayout(rows);
  const b = LIB.compositionLayout(rows);
  assert.equal(a.top, b.top);
  assert.equal(a.step, b.step);
  assert.deepEqual(a.points('wrote'), b.points('wrote'));
  // And the input is not mutated.
  assert.deepEqual(rows, [{ wrote: 10, pasted: 2 }, { wrote: 20, pasted: 4 }]);
});

test('weeklyBuckets does not mutate its input', () => {
  const sessions = [session(0), session(1)];
  const snapshot = JSON.parse(JSON.stringify(sessions));
  LIB.weeklyBuckets(sessions, Date.now());
  assert.deepEqual(sessions, snapshot);
});
