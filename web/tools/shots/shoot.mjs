// Headless-browser screenshots for the web mockups.
//
//   npm i                 # once (installs playwright)
//   npm run browser       # once per machine (downloads Chromium into ~/.cache/ms-playwright)
//   npm run shoot                                   # default: ../../mockups/scheme-04-atelier.html
//   node shoot.mjs <file-or-url> [outDir]           # any page
//
// If the page contains the Ask-the-trace assistant (detected via #askTeaser), it drives the
// real flows — gallery→public, live ask, expand, then Analyze-tab→launcher→consent gate — and
// shoots each. Otherwise it just captures a full-page screenshot. Exits non-zero on page errors.

import { chromium } from 'playwright';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve, isAbsolute } from 'node:path';
import { mkdirSync, rmSync } from 'node:fs';

const here = dirname(fileURLToPath(import.meta.url));
const arg = process.argv[2] || resolve(here, '../../mockups/scheme-04-atelier.html');
const outDir = resolve(process.argv[3] || resolve(here, 'out'));
const target = /^https?:\/\//.test(arg) ? arg : pathToFileURL(isAbsolute(arg) ? arg : resolve(process.cwd(), arg)).href;

rmSync(outDir, { recursive: true, force: true });   // fresh run, no stale shots
mkdirSync(outDir, { recursive: true });
const shot = (page, name) => page.screenshot({ path: resolve(outDir, name) });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = [];
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push('console.error: ' + m.text()); });

await page.goto(target, { waitUntil: 'networkidle' });
await page.waitForTimeout(400);
await shot(page, '00-load.png');

const hasAssistant = await page.$('#askTeaser');
if (hasAssistant) {
  // gallery → public dock, live ask → first answer should surface the contribute nudge
  await page.click('#askTeaser'); await page.waitForTimeout(400);
  await page.fill('#asstInput', 'Which tools do Claude and Codex use the most?');
  await page.click('#asstSend'); await page.waitForTimeout(5200);
  await shot(page, '01-public-answer+nudge.png');
  if (!(await page.$('.contrib-nudge'))) errors.push('contribute nudge did not appear after the first answer');

  // the nudge must NOT block the next question
  await page.fill('#asstInput', 'How much input is served from the prefix cache?');
  if (await page.getAttribute('#asstSend', 'disabled') !== null) errors.push('composer was blocked while the nudge was showing');
  await page.click('#asstSend'); await page.waitForTimeout(5200);
  const answers = await page.$$eval('#asstBody .msg.bot', els => els.length);
  if (answers < 2) errors.push(`second question did not produce an answer (got ${answers} assistant msgs)`);
  await shot(page, '01b-second-answer.png');

  // expand to full page
  await page.click('#btnExpand', { force: true }); await page.waitForTimeout(700);
  await shot(page, '02-expanded.png');
  await page.click('#btnClose', { force: true }); await page.waitForTimeout(300);

  // Analyze tab → launcher defaults to "your trace". With no trace analyzed yet it must be LOCKED.
  const analyzeTab = await page.$('nav.tabs button[data-surface="analyze"]');
  if (analyzeTab) {
    await analyzeTab.click(); await page.waitForTimeout(300);
    await page.click('#launcher'); await page.waitForTimeout(500);
    await shot(page, '03-user-locked.png');
    const src = await page.getAttribute('#seg', 'data-active');
    const trace = await page.getAttribute('#seg', 'data-trace');
    if (src !== 'user') errors.push(`default source from Analyze tab was "${src}", expected "user"`);
    if (trace !== 'none') errors.push(`"Your trace" should be locked before analysis (data-trace="${trace}", expected "none")`);

    // analyze a trace via the real dropzone flow → should unlock "Your trace"
    await page.click('#btnClose', { force: true }); await page.waitForTimeout(200);
    await page.click('#dropzone'); await page.waitForTimeout(4600);   // runAnalysis ≈ 3.4s
    await page.click('#launcher'); await page.waitForTimeout(500);
    await shot(page, '04-user-consent.png');
    const trace2 = await page.getAttribute('#seg', 'data-trace');
    if (trace2 !== 'ready') errors.push(`"Your trace" should unlock after analysis (data-trace="${trace2}", expected "ready")`);
  }
}

await browser.close();
console.log(`shots → ${outDir}`);
console.log('pageerrors:', errors.length ? '\n  - ' + errors.join('\n  - ') : 'none');
process.exit(errors.length ? 1 : 0);
