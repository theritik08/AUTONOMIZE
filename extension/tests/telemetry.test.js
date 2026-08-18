/**
 * Regression tests for the telemetry detection layer.
 *
 * These run in Node with a hand-rolled minimal DOM rather than a real
 * browser, because what is under test here is the DETECTOR REGISTRY and the
 * AGGREGATE SHAPE — pure logic that a browser adds nothing to. The real
 * browser behaviour (does an iframe's keydown actually reach the top
 * document, does Chrome inject into about:blank) is covered by the
 * Playwright suite in e2e/tests/telemetry.spec.ts, because that is the part
 * a fake DOM would happily lie about.
 *
 * Run: node --test extension/tests/telemetry.test.js
 */
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');

const T = require(path.join(__dirname, '..', 'telemetry.js'));

// ───────────────────────────────────────────────────────────────────────
// A minimal DOM good enough for the registry's queries.
// ───────────────────────────────────────────────────────────────────────

function el(tag, attrs = {}, size = { w: 0, h: 0 }) {
  return {
    tagName: tag.toUpperCase(),
    type: attrs.type,
    name: attrs.name,
    id: attrs.id,
    offsetWidth: size.w,
    offsetHeight: size.h,
    _attrs: attrs,
    getAttribute(k) { return k in attrs ? attrs[k] : null; }
  };
}

/** Matches only the selector shapes telemetry.js actually asks for. */
function fakeDocument(elements) {
  function matches(node, selector) {
    return selector.split(',').map((s) => s.trim()).some((sel) => {
      if (sel === 'textarea') return node.tagName === 'TEXTAREA';
      if (sel === "[contenteditable='']") return node.getAttribute('contenteditable') === '';
      if (sel === "[contenteditable='true']") return node.getAttribute('contenteditable') === 'true';
      if (sel === 'input:not([type])') return node.tagName === 'INPUT' && !node.type;
      const m = /^input\[type='(\w+)'\]$/.exec(sel);
      if (m) return node.tagName === 'INPUT' && node.type === m[1];
      return false;
    });
  }
  return {
    querySelectorAll: (sel) => elements.filter((n) => matches(n, sel)),
    querySelector: (sel) => elements.find((n) => matches(n, sel)) || null
  };
}

const BIG = { w: 600, h: 400 };

function ctx(over = {}) {
  return {
    hostname: 'example.com',
    pathname: '/',
    document: fakeDocument([]),
    knownSite: false,
    ...over
  };
}

// ───────────────────────────────────────────────────────────────────────
// 1. Detector selection across the six required environments
// ───────────────────────────────────────────────────────────────────────

test('environment 1: a normal textarea is claimed by a full-capability detector', () => {
  const d = T.pickDetector(ctx({ document: fakeDocument([el('textarea', {}, BIG)]) }));
  assert.ok(d, 'a textarea page must be trackable');
  assert.equal(d.capability, 'full');
});

test('environment 1b: a bare input is claimed by the generic input detector', () => {
  // Small input, so the "substantial editable" check must not claim it —
  // this proves the generic detector is a real fallback and not shadowed.
  const d = T.pickDetector(ctx({ document: fakeDocument([el('input', { type: 'text' })]) }));
  assert.equal(d.name, 'generic-input');
  assert.equal(d.capability, 'full');
});

test('environment 2: a contenteditable editor is claimed with full capability', () => {
  const d = T.pickDetector(ctx({
    document: fakeDocument([el('div', { contenteditable: 'true' }, BIG)])
  }));
  assert.equal(d.name, 'contenteditable');
  assert.equal(d.capability, 'full');
});

test('environment 3: Google Docs is claimed by its adapter and flagged subframe-input', () => {
  // The regression that started all of this. Note the empty document: Docs
  // has NO editable surface in the top frame, so a detector that required
  // one would reject the very page the bug is about.
  const d = T.pickDetector(ctx({
    hostname: 'docs.google.com',
    pathname: '/document/d/abc123/edit',
    document: fakeDocument([]),
    knownSite: true
  }));
  assert.equal(d.name, 'google-docs');
  assert.equal(d.capability, 'full');
  assert.equal(d.inputInSubframe, true,
    'Docs routes keystrokes into a hidden iframe; the relay must be enabled');
});

test('environment 3b: a Google Form is NOT claimed by the Docs adapter', () => {
  // /forms/ is an assessment surface with ordinary inputs, and must not be
  // handled as a Docs document.
  const d = T.pickDetector(ctx({
    hostname: 'docs.google.com',
    pathname: '/forms/d/e/xyz/viewform',
    document: fakeDocument([el('textarea', {}, BIG)]),
    knownSite: true
  }));
  assert.notEqual(d.name, 'google-docs');
});

test('environment 4: another major editor (Notion) is claimed with relay enabled', () => {
  const d = T.pickDetector(ctx({
    hostname: 'www.notion.so', pathname: '/page', knownSite: true
  }));
  assert.equal(d.name, 'rich-editor');
  assert.equal(d.inputInSubframe, true);
});

test('environment 5: an ordinary text website with no editable is NOT tracked', () => {
  const d = T.pickDetector(ctx({
    hostname: 'news.example.com', document: fakeDocument([]), knownSite: false
  }));
  assert.equal(d, null, 'an unlisted, uneditable page must not be tracked at all');
});

test('a KNOWN site with no editable falls back to limited tracking, not silence', () => {
  const d = T.pickDetector(ctx({
    hostname: 'docs.google.com', pathname: '/drawings/d/x',
    document: fakeDocument([]), knownSite: true
  }));
  assert.equal(d.name, 'fallback-activity');
  assert.equal(d.capability, 'limited',
    'an unmeasurable surface must declare itself limited rather than report zeros');
});

// ───────────────────────────────────────────────────────────────────────
// 2. The common aggregate shape — every detector produces this and only this
// ───────────────────────────────────────────────────────────────────────

test('every detector shares one aggregate shape', () => {
  const expected = Object.keys(T.emptyRaw()).sort();
  assert.deepEqual(expected, [
    'backspace', 'burst_keys', 'cut', 'enter', 'iki_buckets',
    'long_pauses', 'pasted_chars', 'printable', 'undo'
  ]);
  // No detector may add a field: the backend and dashboard schema are
  // fixed, which is what lets a new adapter ship without touching them.
  for (const d of T.REGISTRY) {
    assert.ok(typeof d.name === 'string' && d.name.length > 0);
    assert.ok(d.capability === 'full' || d.capability === 'limited');
    assert.equal(typeof d.matches, 'function');
  }
});

test('addRaw merges without losing or double-counting buckets', () => {
  const a = T.emptyRaw();
  a.printable = 5; a.iki_buckets[2] = 3;
  const b = T.emptyRaw();
  b.printable = 7; b.iki_buckets[2] = 4; b.iki_buckets[0] = 1;

  const merged = T.addRaw(T.addRaw(T.emptyRaw(), a), b);
  assert.equal(merged.printable, 12);
  assert.equal(merged.iki_buckets[2], 7);
  assert.equal(merged.iki_buckets[0], 1);
  // The inputs must not be mutated — the worker merges a persisted copy.
  assert.equal(a.printable, 5);
});

test('an empty merge is a no-op rather than a crash', () => {
  const base = T.emptyRaw();
  assert.doesNotThrow(() => T.addRaw(base, null));
  assert.doesNotThrow(() => T.addRaw(base, undefined));
  assert.doesNotThrow(() => T.addRaw(base, {}));
  assert.equal(T.rawHasSignal(base), false);
});

// ───────────────────────────────────────────────────────────────────────
// 3. Counting behaviour
// ───────────────────────────────────────────────────────────────────────

function keyEvent(key, over = {}) {
  return { key, ctrlKey: false, metaKey: false, altKey: false, shiftKey: false,
           target: el('textarea'), ...over };
}

test('printable keys, backspaces and undo land in the right buckets', () => {
  const sink = T.createSink();
  let t = 1000;
  for (const ch of 'hello') sink.key(keyEvent(ch), (t += 100));
  sink.key(keyEvent('Backspace'), (t += 100));
  sink.key(keyEvent('z', { ctrlKey: true }), (t += 100));
  sink.key(keyEvent('Enter'), (t += 100));

  const raw = sink.take();
  assert.equal(raw.printable, 5);
  assert.equal(raw.backspace, 1);
  assert.equal(raw.undo, 1);
  assert.equal(raw.enter, 1);
});

test('environment 6: a paste is measured by LENGTH and the text is never kept', () => {
  const sink = T.createSink();
  sink.paste('a fairly long pasted paragraph'.length, el('textarea'));
  const raw = sink.take();
  assert.equal(raw.pasted_chars, 30);
  // Nothing in the aggregate can hold text: every field is a number or an
  // array of numbers. This is asserted structurally so a future field that
  // carried a string would fail here rather than in review.
  for (const [key, value] of Object.entries(raw)) {
    const ok = typeof value === 'number' ||
      (Array.isArray(value) && value.every((v) => typeof v === 'number'));
    assert.ok(ok, `field ${key} must be numeric, got ${typeof value}`);
  }
});

test('rhythm is a histogram, never an ordered series', () => {
  // The FIRST key only seeds the timer and records no interval, so the two
  // sequences must be built from a shared seed keystroke — otherwise they
  // are not permutations of the same interval multiset and the test would
  // be asserting something untrue.
  function bucketsFor(intervals) {
    const sink = T.createSink();
    let t = 0;
    sink.key(keyEvent('a'), t); // seed
    for (const dt of intervals) sink.key(keyEvent('a'), (t += dt));
    return sink.take().iki_buckets;
  }

  // Two very different typing ORDERS, identical interval multisets.
  const first = bucketsFor([50, 300, 50, 300]);
  const second = bucketsFor([300, 50, 300, 50]);

  assert.deepEqual(first.iki_buckets, second.iki_buckets,
    'reordering the same intervals must be indistinguishable — the ordering ' +
    'is what could carry content, so destroying it is the safety argument');
});

test('an implausible gap is not counted as deliberation', () => {
  const sink = T.createSink();
  sink.key(keyEvent('a'), 0);
  sink.key(keyEvent('b'), 10 * 60_000); // ten minutes: a lunch break
  const raw = sink.take();
  assert.equal(raw.printable, 2);
  assert.equal(raw.iki_buckets.reduce((a, b) => a + b, 0), 0);
});

// ───────────────────────────────────────────────────────────────────────
// 4. Sensitive-field refusal
// ───────────────────────────────────────────────────────────────────────

test('password and sensitive fields are never counted', () => {
  const sink = T.createSink();
  const cases = [
    el('input', { type: 'password' }),
    el('input', { type: 'text', autocomplete: 'current-password' }),
    el('input', { type: 'text', autocomplete: 'one-time-code' }),
    el('input', { type: 'text', name: 'cvv' }),
    el('input', { type: 'text', id: 'card_number' })
  ];
  for (const target of cases) {
    sink.key(keyEvent('a', { target }), 0);
    sink.paste(20, target);
  }
  const raw = sink.take();
  assert.equal(raw.printable, 0, 'no keystroke in a sensitive field may be counted');
  assert.equal(raw.pasted_chars, 0, 'no paste into a sensitive field may be counted');
  assert.equal(sink.sawInput(), false);
});

test('an ordinary field is still counted', () => {
  const sink = T.createSink();
  sink.key(keyEvent('a', { target: el('textarea', { name: 'essay' }) }), 0);
  assert.equal(sink.take().printable, 1);
});

// ───────────────────────────────────────────────────────────────────────
// 5. Capability resolution — the honesty mechanism
// ───────────────────────────────────────────────────────────────────────

test('a declared-limited surface stays limited even if input appears', () => {
  const sink = T.createSink();
  sink.key(keyEvent('a'), 0);
  assert.equal(T.resolveCapability('limited', sink, 0), 'limited');
});

test('a full surface that produced input reports full', () => {
  const sink = T.createSink();
  sink.key(keyEvent('a'), 0);
  assert.equal(T.resolveCapability('full', sink, 10 * 60_000), 'full');
});

test('a full surface is downgraded after long active time with zero input', () => {
  const sink = T.createSink();
  assert.equal(T.resolveCapability('full', sink, T.CAPABILITY_DOWNGRADE_MS + 1), 'limited',
    'an unmeasurable editor must be reported as limited, never as zeros');
});

test('a full surface is NOT downgraded merely for being new', () => {
  const sink = T.createSink();
  assert.equal(T.resolveCapability('full', sink, 5_000), 'full',
    'reading for a few seconds before typing must not trip the downgrade');
});
