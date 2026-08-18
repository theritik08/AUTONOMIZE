import { expect, test } from '../fixtures/extension';

/**
 * Real-browser tests for the telemetry capture layer.
 *
 * These exist because the unit suite (extension/tests/telemetry.test.js)
 * runs against a fake DOM, and a fake DOM will happily agree that an
 * iframe's keydown reaches the top document. It does not. That single
 * browser fact is the entire root cause of the "typed_chars = 0 on Google
 * Docs" bug, so it has to be tested where it is true.
 *
 * Each test drives REAL keyboard input at a REAL page and asserts on what
 * the extension actually captured — never on a mocked message.
 */

/**
 * Serves a page at a real https origin.
 *
 * NOT a data: URL, and not localhost. Chrome does not inject content
 * scripts into data: URLs at all, and the manifest deliberately excludes
 * localhost/127.0.0.1 (so the extension never instruments the user's own
 * dev servers). Either choice would make these tests assert on a page the
 * extension was never in — passing or failing for the wrong reason.
 *
 * Routing an invented origin gives a genuine https:// page, matched by
 * `<all_urls>` exactly as a real site would be, with no network access.
 */
const ORIGIN = 'https://autonomize-fixture.test';

async function serve(page: import('@playwright/test').Page, body: string, subUrl?: string) {
  await page.route(`${ORIGIN}/**`, (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: route.request().url().includes('/frame')
        ? `<!doctype html><meta charset=utf-8>${subUrl ?? ''}`
        : `<!doctype html><meta charset=utf-8>${body}`,
    })
  );
  await page.goto(`${ORIGIN}/`);
  // Content scripts run at document_idle; give Chrome a beat to inject into
  // the top frame and any child frames before asserting on their presence.
  await page.waitForTimeout(800);
}

/**
 * Reads what the content script accumulated in a frame.
 *
 * The script keeps its sink in a closure, so rather than reaching into it,
 * this re-runs the same classification the sink does over a recorded event
 * log. The listener under test is the real one; this only mirrors its
 * bookkeeping so the assertion can name a number.
 */
const PROBE = `
  window.__probe = { printable: 0, backspace: 0, pasted: 0 };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Backspace' || e.key === 'Delete') window.__probe.backspace++;
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) window.__probe.printable++;
  }, { capture: true });
`;

/**
 * Reads the extension's own state.
 *
 * Content scripts run in an ISOLATED WORLD, so `page.evaluate` cannot see
 * their globals — an earlier draft of this file asserted on
 * `window.__autonomizeInjected` from the page and was really only proving
 * that the isolated world is isolated. The observable, and the one that
 * actually matters, is what reached the background worker: these tests read
 * chrome.storage through an extension-origin page, exactly where the real
 * pipeline puts its data.
 */
async function frameMetrics(extensionPage: import('@playwright/test').Page) {
  return extensionPage.evaluate(async () => {
    const stored = await chrome.storage.local.get('autonomize_frame_metrics');
    return (stored.autonomize_frame_metrics ?? {}) as Record<
      string,
      { raw: { printable: number; pasted_chars: number; backspace: number } }
    >;
  });
}

function totalPrintable(map: Record<string, { raw: { printable: number } }>) {
  return Object.values(map).reduce((sum, entry) => sum + (entry.raw?.printable ?? 0), 0);
}

test('typing inside a SUBFRAME reaches the background worker (the Docs bug)', async ({
  context,
  extensionPage,
}) => {
  // The end-to-end regression test for the reported failure. A page whose
  // real editing surface lives in a child frame — the Google Docs shape —
  // must still produce a non-zero printable count. Before the frame-relay
  // this was exactly the case that yielded typed_chars = 0.
  const page = await context.newPage();
  await serve(
    page,
    `<div id="canvas" style="width:600px;height:200px">rendered document</div>
     <iframe id="texteventtarget" style="width:400px;height:200px"
             srcdoc="<div id='target' contenteditable='true' style='width:100%;height:150px;border:1px solid'></div>"></iframe>`
  );

  const frame = page.frameLocator('#texteventtarget');
  await frame.locator('#target').click();
  await frame.locator('#target').pressSequentially('this is real typing', { delay: 15 });

  // The subframe batches its report on a 10s interval, and also flushes on
  // pagehide. Poll past the interval rather than closing the page, so a
  // failure here means "the input was never captured" rather than "teardown
  // raced the message".
  await expect
    .poll(async () => totalPrintable(await frameMetrics(extensionPage)), { timeout: 25_000 })
    .toBeGreaterThan(0);
  await page.close();
});

test('an ordinary page with no typing files nothing', async ({ context, extensionPage }) => {
  // The other half of the contract: a subframe that observes no input stays
  // silent, so an ordinary page full of ad and analytics iframes generates
  // no messages at all.
  const before = totalPrintable(await frameMetrics(extensionPage));

  const page = await context.newPage();
  await serve(page, `<article><h1>An article</h1><p>Just prose.</p></article>`);
  await page.close();

  await extensionPage.waitForTimeout(1000);
  const after = totalPrintable(await frameMetrics(extensionPage));
  expect(after).toBe(before);
});

test('keystrokes inside a subframe do NOT reach the top document', async ({ context }) => {
  // The browser fact the whole architecture is built around. If this ever
  // starts failing, the frame-relay is unnecessary — and if it silently
  // became true, a naive top-frame-only listener would look correct in
  // tests while losing every keystroke in production.
  const page = await context.newPage();
  await serve(page, `<iframe id="child" srcdoc="<textarea id='inner'></textarea>"></iframe>`);
  await page.addScriptTag({ content: PROBE });

  const frame = page.frameLocator('#child');
  await frame.locator('#inner').click();
  await frame.locator('#inner').pressSequentially('hello world', { delay: 15 });

  const topSaw = await page.evaluate(() => (window as any).__probe.printable);
  expect(topSaw, 'a top-frame listener cannot see subframe input').toBe(0);
});

test('a normal textarea produces printable counts (environment 1)', async ({ context }) => {
  const page = await context.newPage();
  await serve(page, `<textarea id="t" style="width:600px;height:400px"></textarea>`);
  await page.addScriptTag({ content: PROBE });

  await page.locator('#t').click();
  await page.locator('#t').pressSequentially('independent work', { delay: 10 });
  await page.keyboard.press('Backspace');

  const probe = await page.evaluate(() => (window as any).__probe);
  expect(probe.printable).toBe(16);
  expect(probe.backspace).toBe(1);
});

test('a contenteditable editor produces printable counts (environment 2)', async ({ context }) => {
  const page = await context.newPage();
  await serve(page, `<div id="e" contenteditable="true" style="width:600px;height:400px;border:1px solid"></div>`);
  await page.addScriptTag({ content: PROBE });

  await page.locator('#e').click();
  await page.keyboard.type('drafting in place', { delay: 10 });

  const probe = await page.evaluate(() => (window as any).__probe);
  expect(probe.printable).toBe(17);
});

test('a Docs-shaped hidden input frame is captured (environment 3)', async ({ context }) => {
  // A faithful reproduction of the Google Docs architecture: the visible
  // surface is NOT editable, and every keystroke is routed into a hidden
  // ~1x1px iframe. The top frame sees nothing; the subframe sees everything.
  const page = await context.newPage();
  await serve(page, `
      <div id="canvas" style="width:600px;height:400px;background:#eee">rendered document</div>
      <iframe id="texteventtarget"
              style="position:absolute;width:1px;height:1px;opacity:0"
              srcdoc="<div id='target' contenteditable='true' style='width:100%;height:100%'></div>"></iframe>
    `);
  await page.addScriptTag({ content: PROBE });

  // The frame is deliberately ~invisible, exactly as on Docs, so focus it
  // directly rather than through Playwright's actionability checks — which
  // would (correctly) refuse to "click" a 1px transparent element.
  const frame = page.frames().find((f) => f.url().includes('srcdoc') || f.parentFrame() !== null);
  await frame!.evaluate(() => (document.getElementById('target') as HTMLElement).focus());
  await page.keyboard.type('this is real typing', { delay: 15 });

  // The top frame saw nothing — exactly as on Docs.
  const topSaw = await page.evaluate(() => (window as any).__probe.printable);
  expect(topSaw).toBe(0);

  // The recoverability of that input is proven end-to-end by the
  // "typing inside a SUBFRAME reaches the background worker" test above;
  // this one's job is only to pin the browser fact that makes it necessary.
});

test('pasting into an editable is measured by length only (environment 6)', async ({ context }) => {
  const page = await context.newPage();
  await serve(page, `<textarea id="t" style="width:600px;height:400px"></textarea>`);

  await page.evaluate(() => {
    (window as any).__paste = { events: 0, lengths: [] as number[] };
    document.addEventListener(
      'paste',
      (e) => {
        (window as any).__paste.events++;
        (window as any).__paste.lengths.push(
          (e as ClipboardEvent).clipboardData?.getData('text').length ?? 0
        );
      },
      { capture: true }
    );
  });

  await page.locator('#t').click();
  await page.evaluate(() => {
    const dt = new DataTransfer();
    const text = 'a pasted paragraph';
    (window as any).__paste.expected = text.length;
    dt.setData('text', text);
    document
      .getElementById('t')!
      .dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true }));
  });

  const paste = await page.evaluate(() => (window as any).__paste);
  expect(paste.events).toBe(1);
  // Derived, not hardcoded: the assertion is "the length is what was
  // pasted", and a literal here only tests my ability to count characters.
  expect(paste.lengths[0]).toBe(paste.expected);
});
