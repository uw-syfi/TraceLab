// One-off: full-page + section shots of /lab for design review.
import { chromium } from 'playwright';
import { resolve } from 'node:path';
import { mkdirSync, rmSync } from 'node:fs';

const url = process.argv[2] || 'http://localhost:4322/lab';
const outDir = resolve(process.argv[3] || './out-lab');
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1320, height: 1000 }, deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });

await page.goto(url, { waitUntil: 'networkidle' });
await page.reload({ waitUntil: 'networkidle' }); // settle Vite dep re-optimize (504s on first hit)
await page.waitForTimeout(1200);

await page.screenshot({ path: resolve(outDir, 'full.png'), fullPage: true });

const shoot = async (sel, name) => {
  const el = await page.$(sel);
  if (el) await el.screenshot({ path: resolve(outDir, name) });
  else errors.push('missing ' + sel);
};
await shoot('#facts', 'facts.png');
await shoot('.sess-layout', 'sessions.png');
await shoot('#dist', 'dist.png');

await browser.close();
console.log('shots →', outDir);
console.log('pageerrors:', errors.length ? '\n  - ' + errors.join('\n  - ') : 'none');
