// Headless DOM-wiring smoke for the Ask-the-trace assistant in the real Astro app.
// Serves the built dist/ (no backend needed: load + open + render + source-switch don't hit /api).
// Asserts: the dock mounts, opens, shows the empty state, and switching to "Your trace" (locked,
// no trace analyzed) shows the no-trace gate — all with zero page/console errors.
//
//   APP_URL=http://localhost:4399 node drive_app.mjs

import { chromium } from 'playwright';

const URL = process.env.APP_URL || 'http://localhost:4399';
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('console.error: ' + m.text());
});

console.log('→ goto', URL);
await page.goto(URL, { waitUntil: 'networkidle' });
await page.waitForTimeout(300);

// launcher present + opens the dock
await page.waitForSelector('#launcher', { timeout: 10000 });
await page.click('#launcher');
await page.waitForTimeout(400);
const open = await page.evaluate(() => document.body.classList.contains('assistant-open'));
if (!open) errors.push('dock did not open (body.assistant-open missing)');
const panelVisible = await page.isVisible('.assistant');
if (!panelVisible) errors.push('.assistant panel not visible after open');

// empty state rendered in the body
const bodyHtmlLen = await page.evaluate(() => (document.getElementById('asstBody')?.innerHTML || '').length);
if (bodyHtmlLen < 1) errors.push('#asstBody is empty (no greeting/empty state)');

// switch to "Your trace" with no trace analyzed → no-trace (locked) gate
await page.click('#seg button[data-src="user"]');
await page.waitForTimeout(400);
const seg = await page.getAttribute('#seg', 'data-active');
const hasNoTraceGate = await page.evaluate(() => !!document.querySelector('.gate.no-trace'));
const traceState = await page.getAttribute('#seg', 'data-trace');
console.log(`  seg data-active=${seg} data-trace=${traceState} noTraceGate=${hasNoTraceGate}`);
if (traceState !== 'none') errors.push(`expected #seg data-trace="none" before analysis, got "${traceState}"`);
if (!hasNoTraceGate) errors.push('no-trace gate did not render when switching to "Your trace" without a trace');

// composer should be disabled at empty input
const sendDisabled = await page.getAttribute('#asstSend', 'disabled');
console.log('  send disabled at empty input:', sendDisabled !== null);

await page.screenshot({ path: '/tmp/drive_app.png', fullPage: true });
await browser.close();
console.log('page errors:', errors.length ? '\n - ' + errors.join('\n - ') : 'none');
process.exit(errors.length ? 1 : 0);
