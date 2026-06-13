// Drive the SYFI LLM tester end-to-end in a real headless browser: select the uploaded-trace
// source, load the sample trace (boots Pyodide), ask a question, and assert a non-empty answer
// comes back over the WebSocket. This exercises the actual WS client + Pyodide worker + DOM wiring.
//
//   TESTER_URL=http://127.0.0.1:8141/tester.html node drive_tester.mjs
//
// Exits non-zero on page errors or if no answer arrives.

import { chromium } from 'playwright';

const URL = process.env.TESTER_URL || 'http://127.0.0.1:8141/tester.html';
const QUESTION = process.env.TESTER_Q || 'How many rows are in the tool_calls table? Reply with just the number.';

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => {
  const text = m.text();
  if (m.type() === 'error') errors.push('console.error: ' + text);
  else console.log('  [console] ' + text);
});

console.log('→ goto', URL);
await page.goto(URL, { waitUntil: 'domcontentloaded' });

await page.selectOption('#executorMode', 'user');
console.log('→ loading sample trace (boots Pyodide)…');
await page.click('#loadSampleTrace');
await page.waitForFunction(
  () => /database ready|Loaded/i.test(document.getElementById('traceStatus').textContent || ''),
  null,
  { timeout: 180000 },
);
console.log('  trace status:', (await page.textContent('#traceStatus')).trim());

console.log('→ asking:', QUESTION);
await page.fill('#question', QUESTION);
await page.click('#send');
await page.waitForFunction(
  () => {
    const msgs = [...document.querySelectorAll('.msg.assistant')];
    const last = msgs[msgs.length - 1];
    if (!last || last.classList.contains('pending')) return false;
    return last.textContent.replace(/^\s*assistant\s*/i, '').trim().length > 0;
  },
  null,
  { timeout: 240000 },
);

const answer = await page.evaluate(() => {
  const msgs = [...document.querySelectorAll('.msg.assistant')];
  return msgs[msgs.length - 1].textContent.replace(/^\s*assistant\s*/i, '').trim();
});
console.log('✓ ANSWER:', answer);
const images = await page.evaluate(
  () => document.querySelectorAll('.image-card img[src^="data:image"]').length,
);
console.log('rendered images:', images);
if (process.env.EXPECT_IMAGE && images < 1) errors.push('expected an inline chart but none rendered');
await page.screenshot({ path: '/tmp/tester_drive.png', fullPage: true });

await browser.close();
console.log('page errors:', errors.length ? '\n - ' + errors.join('\n - ') : 'none');
process.exit(errors.length ? 1 : 0);
