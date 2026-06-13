// Drive the Analyze surface end-to-end against the dev server: switch to the Analyze tab, upload a
// real trace, wait for the interactive dashboard to compute, and screenshot it.
//   BASE=http://localhost:4337 TAR=/path/to/trace.tar.gz node shoot_analyze.mjs
import { chromium } from 'playwright';
import { resolve, dirname, extname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, rmSync, readFileSync, existsSync } from 'node:fs';
import { createServer } from 'node:http';

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, '../../app/dist');
const outDir = resolve(here, 'out/analyze');
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const TAR = process.env.TAR || '/m-coriander/coriander/kanzhu/coding_trace_refactor/test_workload_claude_sessions.tar.gz';

// Serve the built dist/ ourselves (stable, no dev-server HMR that would destroy the page context
// mid-analysis). Mirrors shoot_compare.mjs.
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.wasm': 'application/wasm', '.map': 'application/json' };
const server = createServer((req, res) => {
  let p = decodeURIComponent(req.url.split('?')[0].split('#')[0]);
  if (p.endsWith('/')) p += 'index.html';
  let f = join(dist, p);
  if (!existsSync(f) && existsSync(f + '.html')) f += '.html';
  if (!existsSync(f) || extname(f) === '') f = join(dist, p, 'index.html');
  try {
    const body = readFileSync(f);
    res.writeHead(200, { 'content-type': MIME[extname(f)] || 'application/octet-stream' });
    res.end(body);
  } catch { res.writeHead(404); res.end('not found'); }
});
await new Promise((r) => server.listen(0, r));
const BASE = `http://localhost:${server.address().port}`;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1700 }, deviceScaleFactor: 1 });
const logs = [];
page.on('console', (m) => logs.push(`[${m.type()}] ${m.text()}`));
page.on('pageerror', (e) => logs.push('PAGEERROR: ' + e.message));
page.on('response', (r) => { if (r.status() === 404) logs.push('404: ' + r.url()); });

console.log('goto', BASE);
await page.goto(`${BASE}/#analyze`, { waitUntil: 'domcontentloaded' });
await page.click('nav.tabs button[data-surface="analyze"]').catch(() => {});
await page.waitForSelector('#file-input', { state: 'attached', timeout: 20000 });

console.log('uploading', TAR);
await page.setInputFiles('#file-input', TAR);

// Pyodide cold boot + ingest (9.7k rounds) + materialize + bulk is slow; give it room.
// Poll explicitly (raf-based waitForFunction was flaky here — context churn during heavy worker
// traffic) and log progress so we can see exactly which stage is live and when it finishes.
const t0 = Date.now();
let done = false;
for (let i = 0; i < 160; i++) {
  const title = await page.$eval('#dz-title', (el) => el.textContent || '').catch(() => '');
  const el = ((Date.now() - t0) / 1000).toFixed(0);
  if (/Analysis complete/.test(title)) { done = true; console.log(`t+${el}s done: ${title}`); break; }
  if (/Couldn.t read/.test(title)) { console.log(`t+${el}s ERROR: ${title}`); break; }
  if (i % 4 === 0) console.log(`t+${el}s dz-title: ${title}`);
  await page.waitForTimeout(2000);
}
console.log('completed:', done);
// Wait for the dashboard to actually have rendered charts (canvas/svg inside the chart hosts), then
// give ECharts a moment to finish its enter animation before capturing.
await page
  .waitForFunction(
    () => {
      const d = document.getElementById('analytics-dashboard');
      if (!d) return false;
      return d.querySelectorAll('canvas, .echarts-for-react, svg').length >= 4;
    },
    { timeout: 20000 },
  )
  .catch(() => {});
await page.waitForTimeout(2500); // let ECharts finish drawing

await page.screenshot({ path: resolve(outDir, 'analyze-full.png'), fullPage: true });
const dash = await page.$('#analytics-dashboard');
if (dash) await dash.screenshot({ path: resolve(outDir, 'dashboard.png') });

// drill into a session + a round to exercise the timeline + LOCAL-only raw viewer
try {
  await page.click('.sess-row', { timeout: 8000 });
  await page.waitForTimeout(1500);
  // click a point in the session timeline to open the round inspector
  await page.click('.chart--session', { position: { x: 120, y: 300 } }).catch(() => {});
  await page.waitForTimeout(1800);
  // the raw text is fetched async from the prepare-worker sidecar (LOCAL-only); wait for the slot to
  // fill before capturing so the screenshot proves the per-round raw input/output/tools rendered.
  const rawFilled = await page
    .waitForFunction(() => {
      const slot = document.querySelector('.rd-raw-slot');
      return !!slot && slot.children.length > 0;
    }, { timeout: 12000 })
    .then(() => true)
    .catch(() => false);
  console.log('raw slot filled:', rawFilled);
  const detail = await page.$('.sess-detail');
  if (detail) {
    await detail.scrollIntoViewIfNeeded();
    await detail.screenshot({ path: resolve(outDir, 'session-detail.png') });
  }
  // also grab just the round inspector (includes the raw sections) at full height
  const rd = await page.$('.round-detail, #round-detail');
  if (rd) await rd.screenshot({ path: resolve(outDir, 'round-raw.png') });
} catch (e) {
  logs.push('drilldown skipped: ' + e.message);
}

console.log('rawAvailable:', await page.evaluate(() => {
  const slot = document.querySelector('.rd-raw-slot');
  return slot ? `slot has ${slot.children.length} child(ren)` : 'no slot';
}));

const read = async (sel) => page.$eval(sel, (el) => el.textContent).catch(() => '(missing)');
console.log('dz-title   :', await read('#dz-title'));
console.log('KPI rounds :', await read('[data-kpi="rounds"]'));
console.log('KPI cost   :', await read('[data-kpi="cost"]'));
console.log('KPI sessions:', await read('[data-kpi="sessions"]'));
console.log('sess-count :', await read('#sess-count'));
console.log('\n--- last 50 console/page logs ---\n' + logs.slice(-50).join('\n'));

await browser.close();
server.close();
console.log('\nshots ->', outDir);
