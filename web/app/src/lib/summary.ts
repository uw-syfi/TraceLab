import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import type { Summary } from './types';

// Build-time loader. Reads the summary.json that build-payload.mjs places in public/data/ —
// the SAME render that produced the figures, so the site stays internally consistent. Falls
// back to the committed repo-root summary.json when nothing has been copied/rendered yet.
// Resolved from process.cwd() (web/app during `astro dev`/`astro build`) rather than
// import.meta.url, because the latter points into the bundled dist/ output at build time.
function resolveSummaryPath(): string {
  const env = process.env.SUMMARY_JSON;
  if (env && existsSync(env)) return env;
  const candidates = [
    resolve(process.cwd(), 'public/data/summary.json'), // build-payload output (cwd = web/app)
    resolve(process.cwd(), 'app/public/data/summary.json'), // cwd = web
    resolve(process.cwd(), '../../summary.json'), // repo-root fallback
    resolve(process.cwd(), 'summary.json'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  throw new Error(
    `summary.json not found (looked in: ${candidates.join(', ')}). ` +
      'Run `just all` (or `node web/scripts/build-payload.mjs`) first.',
  );
}

let cached: Summary | null = null;

export function loadSummary(): Summary {
  if (cached) return cached;
  const raw = readFileSync(resolveSummaryPath(), 'utf-8');
  cached = JSON.parse(raw) as Summary;
  return cached;
}
