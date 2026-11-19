import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { compact } from './format.ts';

// Build-time loader for the card headline numbers. build-payload.mjs aggregates each table
// experiment's headline.json into public/data/headlines.json, keyed by experiment slug. The
// Overview gallery's stat cards render these few numbers; clicking through shows the full table.
// Resolved from process.cwd() (web/app during astro dev/build), mirroring lib/summary.ts.

export interface HeadlineStat {
  label: string;
  value: string;
}

export type Headlines = Record<string, HeadlineStat[]>;

function resolveHeadlinesPath(): string | null {
  const candidates = [
    resolve(process.cwd(), 'public/data/headlines.json'), // build-payload output (cwd = web/app)
    resolve(process.cwd(), 'app/public/data/headlines.json'), // cwd = web
  ];
  return candidates.find((c) => existsSync(c)) ?? null;
}

let cached: Headlines | null = null;

export function loadHeadlines(): Headlines {
  if (cached) return cached;
  const path = resolveHeadlinesPath();
  cached = path ? (JSON.parse(readFileSync(path, 'utf-8')) as Headlines) : {};
  return cached;
}

/** Headline stat lines for an experiment slug (empty when none have been rendered yet). */
export function headlinesFor(slug: string | undefined): HeadlineStat[] {
  if (!slug) return [];
  return loadHeadlines()[slug] ?? [];
}

/** Pair provider headlines for the compact subagent gallery preview. */
export function subagentHeadlineRows(stats: HeadlineStat[]) {
  const measures = [
    ['main sessions with subagents', 'Main sessions with subagents'],
    ['average subagents per main session', 'Avg subagents / delegating main'],
    ['median rounds per main session', 'Median rounds / delegating main'],
    ['median rounds per subagent', 'Median rounds / subagent'],
    ['average rounds per main session', 'Avg rounds / delegating main'],
    ['average rounds per subagent', 'Avg rounds / subagent'],
    ['median input tokens per complete family', 'Median input tokens / family'],
    ['median output tokens per complete family', 'Median output tokens / family'],
    ['median observed subagent elapsed time (seconds)', 'Median subagent time (s)'],
  ];
  return measures.flatMap(([measure, label]) => {
    const providers = ['Claude', 'Codex'].map((provider) => stats.find((stat) => stat.label === `${provider} ${measure}`));
    if (!providers.some(Boolean)) return [];
    const values = providers.map((stat) => {
      const value = stat?.value;
      const numeric = Number(value?.replaceAll(',', ''));
      return { value: value ?? 'N/A', display: numeric >= 1000 ? compact(numeric) : value ?? 'N/A' };
    });
    return [{ label, measure, values }];
  });
}
