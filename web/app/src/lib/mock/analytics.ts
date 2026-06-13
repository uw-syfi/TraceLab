// Deterministic mock data for the Phase-1 layout page (/lab). Shapes match lib/analytics/types.ts
// exactly, so wiring real worker data later is a drop-in swap. Costs flow through the real pricing
// helpers so the cost chart, KPIs, and cache-savings number all reconcile.
//
// No Date.now()/Math.random(): a seeded PRNG keeps the page identical across reloads and builds.

import type {
  AnalyticsPayload,
  CostByModel,
  Fact,
  HourWeekday,
  PerDay,
  RoundRaw,
  SessionDetail,
  SessionRow,
} from '../analytics/types';
import { priceFor, roundCost, cacheSavings } from '../analytics/cost';

// ---- seeded PRNG (mulberry32) ----
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rnd = mulberry32(0x5a17c0de);
const between = (lo: number, hi: number) => lo + (hi - lo) * rnd();
const intBetween = (lo: number, hi: number) => Math.round(between(lo, hi));
const pick = <T>(xs: T[]) => xs[Math.floor(rnd() * xs.length)];

// ---- dates ----
const MS = 1000; // -> microseconds
const START = new Date(2026, 2, 2); // Mar 2 2026 (local)
const END = new Date(2026, 5, 10); // Jun 10 2026
const dayKey = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
const usAt = (d: Date) => d.getTime() * MS;

// ---- per-day activity (skip ~weekends-ish + random rest days) ----
function buildPerDay(): PerDay[] {
  const out: PerDay[] = [];
  for (let d = new Date(START); d <= END; d.setDate(d.getDate() + 1)) {
    const wd = (d.getDay() + 6) % 7; // 0=Mon..6=Sun
    const weekend = wd >= 5;
    if (rnd() < (weekend ? 0.65 : 0.12)) continue; // rest days
    const rounds = intBetween(weekend ? 8 : 40, weekend ? 90 : 360);
    const inputTokens = rounds * intBetween(9000, 26000);
    const outputTokens = rounds * intBetween(400, 1600);
    const costUsd = (inputTokens * 0.7 * 0.15 + inputTokens * 0.3 * 1.5 + outputTokens * 75) / 1e6;
    out.push({ day: dayKey(new Date(d)), rounds, inputTokens, outputTokens, costUsd });
  }
  return out;
}
const perDay = buildPerDay();

// ---- work-rhythm heatmap (evenings + weekday bias) ----
function buildHourWeekday(): HourWeekday[] {
  const out: HourWeekday[] = [];
  for (let weekday = 0; weekday < 7; weekday++) {
    for (let hour = 0; hour < 24; hour++) {
      const weekend = weekday >= 5;
      const evening = hour >= 19 && hour <= 23;
      const daytime = hour >= 9 && hour <= 18;
      const night = hour >= 0 && hour <= 3;
      let base = 2;
      if (daytime) base += weekend ? 8 : 26;
      if (evening) base += weekend ? 20 : 30;
      if (night) base += 10; // the "night owl" easter egg
      const rounds = Math.max(0, intBetween(base * 0.4, base * 1.6));
      out.push({ weekday, hour, rounds });
    }
  }
  return out;
}
const hourWeekday = buildHourWeekday();

// ---- cost by model (computed via real pricing) ----
const MODEL_SPECS: { provider: string; model: string; rounds: number; prefix: number; append: number; output: number; reasoning: number }[] = [
  { provider: 'claude', model: 'claude-opus-4-8', rounds: 4200, prefix: 410_000_000, append: 38_000_000, output: 9_400_000, reasoning: 3_100_000 },
  { provider: 'claude', model: 'claude-opus-4-7', rounds: 1800, prefix: 150_000_000, append: 14_000_000, output: 3_600_000, reasoning: 1_200_000 },
  { provider: 'claude', model: 'claude-sonnet-4-6', rounds: 2600, prefix: 220_000_000, append: 21_000_000, output: 5_200_000, reasoning: 0 },
  { provider: 'codex', model: 'gpt-5.5', rounds: 3100, prefix: 180_000_000, append: 26_000_000, output: 7_100_000, reasoning: 2_400_000 },
  { provider: 'codex', model: 'gpt-5.4', rounds: 900, prefix: 41_000_000, append: 6_500_000, output: 1_900_000, reasoning: 600_000 },
  { provider: 'codex', model: 'experimental-o5-preview', rounds: 240, prefix: 9_000_000, append: 1_500_000, output: 520_000, reasoning: 0 },
];

function buildCost() {
  const byModel: CostByModel[] = [];
  let cacheSavingsUsd = 0;
  let reasoningCostUsd = 0;
  let pricedRounds = 0;
  let unpricedRounds = 0;
  for (const m of MODEL_SPECS) {
    const price = priceFor(m.provider, m.model);
    if (!price) {
      byModel.push({ provider: m.provider, model: m.model, rounds: m.rounds, inputCost: 0, cachedCost: 0, outputCost: 0, reasoningCost: 0, costUsd: 0, priced: false });
      unpricedRounds += m.rounds;
      continue;
    }
    const rc = roundCost(price, { prefixTokens: m.prefix, appendTokens: m.append, outputTokens: m.output, reasoningTokens: m.reasoning });
    byModel.push({ provider: m.provider, model: m.model, rounds: m.rounds, inputCost: rc.inputCost, cachedCost: rc.cachedCost, outputCost: rc.outputCost, reasoningCost: rc.reasoningCost, costUsd: rc.total, priced: true });
    cacheSavingsUsd += cacheSavings(price, m.prefix);
    reasoningCostUsd += rc.reasoningCost;
    pricedRounds += m.rounds;
  }
  byModel.sort((a, b) => b.costUsd - a.costUsd);
  return { byModel, cacheSavingsUsd, reasoningCostUsd, pricedRounds, unpricedRounds };
}
const cost = buildCost();

// ---- providers split band (rollup of cost.byModel — mirrors Python build_providers) ----
function buildProviders(): AnalyticsPayload['providers'] {
  const agg = new Map<string, { provider: string; rounds: number; cost: number }>();
  for (const m of cost.byModel) {
    const e = agg.get(m.provider) ?? { provider: m.provider, rounds: 0, cost: 0 };
    e.rounds += m.rounds;
    e.cost += m.costUsd;
    agg.set(m.provider, e);
  }
  const totalRounds = [...agg.values()].reduce((t, p) => t + p.rounds, 0) || 1;
  const totalCost = [...agg.values()].reduce((t, p) => t + p.cost, 0) || 1;
  return [...agg.values()]
    .map((p) => ({ ...p, stepShare: p.rounds / totalRounds, costShare: p.cost / totalCost }))
    .sort((a, b) => b.rounds - a.rounds);
}
const providers = buildProviders();

// ---- sessions list ----
const SESSION_IDS = [
  'a1f3-marathon', 'b2e7-refactor', 'c4d9-debug', 'd5a1-feature', 'e6b3-docs',
  'f7c8-spike', '08d2-review', '19e4-migrate', '2af6-hotfix', '3b07-explore',
];
// Conversation titles — present for SOME sessions only (Claude Code records a `summary`; Codex / older
// traces often don't), so a few ids deliberately have none to exercise the sessionId fallback.
const SESSION_TITLES: Record<string, string> = {
  'a1f3-marathon': 'End-to-end session timeline rebuild',
  'b2e7-refactor': 'Refactor plots into shared chart modules',
  'c4d9-debug': 'Stop dashed markers flashing on dataZoom',
  'd5a1-feature': 'Add per-round raw input/output viewer',
  'f7c8-spike': 'Spike: bridge ECharts theme from CSS tokens',
  '19e4-migrate': 'Migrate experiments onto shared DuckDB',
  '2af6-hotfix': 'Hotfix: single-hue ramp for spend-by-model',
  // e6b3-docs, 08d2-review, 3b07-explore intentionally have no title
};

function buildSessions(): SessionRow[] {
  return SESSION_IDS.map((id, i) => {
    const provider = i % 3 === 1 ? 'codex' : 'claude';
    const model = provider === 'codex' ? pick(['gpt-5.5', 'gpt-5.4']) : pick(['claude-opus-4-8', 'claude-opus-4-7', 'claude-sonnet-4-6']);
    const rounds = i === 0 ? 412 : intBetween(24, 260);
    const durationS = i === 0 ? 27_400 : intBetween(600, 18_000);
    // cluster sessions onto a handful of days (with repeats) so the date-grouped picker shows
    // several sessions per day, not ten lone dates.
    const dayOffset = pick([3, 3, 7, 12, 12, 12, 18, 26, 26, 34]);
    const start = new Date(START.getTime() + dayOffset * 86_400_000 + intBetween(8, 21) * 3_600_000);
    const inputTokens = rounds * intBetween(9000, 24000);
    const outputTokens = rounds * intBetween(450, 1500);
    const price = priceFor(provider, model)!;
    const costUsd = roundCost(price, { prefixTokens: inputTokens * 0.72, appendTokens: inputTokens * 0.28, outputTokens }).total;
    return {
      sessionId: id, title: SESSION_TITLES[id], provider, primaryModel: model, rounds,
      firstTsUs: usAt(start), lastTsUs: usAt(new Date(start.getTime() + durationS * 1000)),
      durationS, inputTokens, outputTokens, costUsd,
      toolCalls: Math.round(rounds * between(1.2, 3.1)), errors: intBetween(0, Math.max(1, rounds / 30)),
    };
  }).sort((a, b) => b.rounds - a.rounds);
}
const sessions = buildSessions();

// ---- per-session detail (timeline) ----
function buildSessionDetail(id: string, n: number): SessionDetail {
  const detail = mulberry32(Array.from(id).reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 7));
  const rnd2 = () => detail();
  const rounds: SessionDetail['rounds'] = [];
  let prefix = 0;
  let t = START.getTime() + 86_400_000 * Math.floor(rnd2() * 60);
  for (let seq = 1; seq <= n; seq++) {
    const isUserInput = seq === 1 || rnd2() < 0.12;
    if (isUserInput && seq > 1 && rnd2() < 0.3) prefix = Math.max(0, prefix * 0.35); // compaction
    const append = isUserInput ? intBetween(2500, 14000) : intBetween(200, 2600);
    prefix += append + intBetween(0, 1500);
    const output = intBetween(150, 3200);
    const reasoning = rnd2() < 0.6 ? Math.round(output * between(0.2, 0.7)) : 0;
    const toolCount = isUserInput ? 0 : intBetween(0, 5);
    const tools = Array.from({ length: toolCount }, () => {
      const name = pick(['Read', 'Edit', 'Bash', 'Grep', 'Glob', 'Write']);
      const ms = name === 'Bash' ? intBetween(120, 4200) : name === 'Grep' || name === 'Glob' ? intBetween(20, 260) : intBetween(8, 90);
      return { name, ms, error: rnd2() < 0.06 };
    });
    const inferenceS = +between(0.6, 9).toFixed(1);
    t += inferenceS * 1000 + (isUserInput ? intBetween(20000, 240000) : intBetween(500, 6000));
    if (isUserInput && seq > 1 && rnd2() < 0.18) t += intBetween(8, 40) * 60_000; // occasional break -> wall-clock gap
    rounds.push({ seq, traceKey: `mock:${id}:${seq}`, prefixTokens: Math.round(prefix), appendTokens: append, outputTokens: output, reasoningTokens: reasoning, tsUs: t * MS, isUserInput, toolCount, inferenceS, tools });
  }
  const tools = [
    { name: 'Read', count: intBetween(40, 200), errors: intBetween(0, 4), p50Ms: intBetween(8, 40) },
    { name: 'Edit', count: intBetween(20, 120), errors: intBetween(0, 8), p50Ms: intBetween(10, 50) },
    { name: 'Bash', count: intBetween(15, 90), errors: intBetween(0, 16), p50Ms: intBetween(120, 4000) },
    { name: 'Grep', count: intBetween(10, 70), errors: intBetween(0, 2), p50Ms: intBetween(20, 200) },
  ].sort((a, b) => b.count - a.count);
  return { sessionId: id, rounds, tools };
}

export const mockSessionDetails: Record<string, SessionDetail> = Object.fromEntries(
  sessions.map((s) => [s.sessionId, buildSessionDetail(s.sessionId, Math.min(s.rounds, 220))]),
);

// ---- mock RAW per-round text -------------------------------------------------------------------
// Phase-1 stand-in for the round_pk -> raw-text map a real LOCAL ingest would keep (see the design
// note). Deterministic from (sessionId, roundIndex). In production this is fetched on demand over the
// query RPC and only exists when the user analyzed their own uploaded file.
const RAW_USER_MSGS = [
  'Refactor the session timeline so the 5-minute time blocks span every round in their window, not one fixed tick per round.',
  'The dark-mode toggle in the topbar should persist across reloads — can you wire that up?',
  '`astro build` keeps failing with "Missing pages directory: src/pages". Take a look and fix it.',
  'Write a markdown catalog of every metric we brainstormed, including the neglected ones, in a folder.',
  'Move the highlights and sessions sections above the stats block on /lab.',
  'Add a provider split band above Activity showing how steps and spend divide across Claude vs Codex.',
  'The dashed user-input lines flash a lot while I scale the chart — make that stop, but keep it natural.',
  'The date column in the session picker looks empty — give it a divider with a small node.',
];
const RAW_OUTPUTS = [
  'Done. I moved the strip to a wall-clock bucket map keyed by 5-minute windows, then drew each band across that bucket’s first..last round index with markArea, alternating shade by bucket order.',
  'Traced it to the bash cwd resetting between calls — the build has to run from `web/app`. I prefixed the command with the cd and verified `astro build` completes with 20 pages.',
  'Added the toggle: it reads `localStorage.theme` on load, flips `data-theme` on the root element, and falls back to the OS `prefers-color-scheme` when unset.',
  'I set `animationDurationUpdate: 0` so the dashed markLines snap instead of tweening on dataZoom — the flashing is gone while the initial render still animates.',
  'Reordered the blocks in the template; the populate scripts all use getElementById, so nothing else had to change.',
];
function rawToolFor(name: string): { input: string; result: string } {
  switch (name) {
    case 'Read':
      return { input: '{ "file_path": "web/app/src/pages/lab.astro" }', result: '   742\\tfunction roundHint(): void {\\n   743\\t  roundDetailEl.innerHTML = ...\\n   …(118 more lines)' };
    case 'Edit':
      return { input: '{ "old_string": "height: 480px;", "new_string": "height: 576px;" }', result: 'The file lab.astro has been updated successfully.' };
    case 'Bash':
      return { input: 'cd web/app && npx astro build', result: '> astro build\\n14:42 [build] 20 page(s) built in 9.74s\\n[build] Complete!' };
    case 'Grep':
      return { input: '{ "pattern": "sess-date-head", "output_mode": "content" }', result: '220:  .sess-date-head { position: relative; flex: none; width: 90px; … }' };
    case 'Glob':
      return { input: '{ "pattern": "src/lib/charts/*.ts" }', result: 'src/lib/charts/theme.ts\\nsrc/lib/charts/session.ts\\nsrc/lib/charts/cost.ts' };
    case 'Write':
      return { input: '{ "file_path": "METRICS.md", "content": "# Metrics catalog\\n…" }', result: 'File created successfully at METRICS.md' };
    default:
      return { input: '{}', result: 'ok' };
  }
}
export function mockRoundRaw(sessionId: string, roundIndex: number): RoundRaw | null {
  const detail = mockSessionDetails[sessionId];
  const r = detail?.rounds[roundIndex];
  if (!r) return null;
  const seed = mulberry32(
    ((Array.from(sessionId).reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) >>> 0, 13) >>> 0) ^ ((roundIndex + 1) * 2654435761)) >>> 0,
  );
  const pkOf = <T>(arr: T[]): T => arr[Math.floor(seed() * arr.length)];
  const tools = r.tools.map((t) => ({ name: t.name, ...rawToolFor(t.name), error: t.error }));
  if (r.isUserInput) {
    return { input: pkOf(RAW_USER_MSGS), inputKind: 'user', output: pkOf(RAW_OUTPUTS), tools };
  }
  // tool-step: the input is the tool results that came back from the previous round's tool calls
  const prevTools = detail.rounds[roundIndex - 1]?.tools ?? [];
  const src = prevTools.length ? prevTools : r.tools;
  const input =
    src.map((t) => `[tool_result · ${t.name}${t.error ? ' · error' : ''}]\n${rawToolFor(t.name).result}`).join('\n\n') ||
    '[tool_result]\n(no content)';
  return { input, inputKind: 'tool', output: pkOf(RAW_OUTPUTS), tools };
}

// ---- facts (clickable superlatives) ----
const facts: Fact[] = [
  { id: 'longest-wallclock', dimension: 'time', title: 'Longest continuous session', value: '7h 37m', detail: 'Session a1f3 · 412 steps end-to-end', sessionId: 'a1f3-marathon' },
  { id: 'longest-autonomous', dimension: 'time', title: 'Longest unattended run', value: '54 min', detail: '38 tool-result steps with no human turn', sessionId: 'c4d9-debug' },
  { id: 'busiest-day', dimension: 'time', title: 'Busiest day', value: '361 steps', detail: 'May 14 2026' },
  { id: 'streak', dimension: 'time', title: 'Longest daily streak', value: '11 days', detail: 'Apr 21 → May 1' },
  { id: 'night-owl', dimension: 'time', title: 'Peak hour', value: '11pm', detail: 'Most steps land 23:00–00:00 local' },
  { id: 'priciest-session', dimension: 'cost', title: 'Most expensive session', value: '$48.10', detail: 'Session a1f3', sessionId: 'a1f3-marathon' },
  { id: 'priciest-round', dimension: 'cost', title: 'Most expensive round', value: '$1.74', detail: '212K-token context reused', sessionId: '19e4-migrate', roundIndex: 140 },
  { id: 'cache-saved', dimension: 'cost', title: 'Saved by prefix cache', value: '$' + (cost.cacheSavingsUsd >= 1000 ? (cost.cacheSavingsUsd / 1000).toFixed(1) + 'K' : cost.cacheSavingsUsd.toFixed(0)), detail: 'vs. billing every cached token fresh' },
  { id: 'longest-convo', dimension: 'session', title: 'Longest conversation', value: '412 steps', detail: 'Session a1f3', sessionId: 'a1f3-marathon' },
  { id: 'context-peak', dimension: 'session', title: 'Context peak', value: '214K tok', detail: 'fullest single input', sessionId: '19e4-migrate', roundIndex: 140 },
  { id: 'biggest-output', dimension: 'session', title: 'Biggest single output', value: '8.1K tok', detail: 'one round', sessionId: 'd5a1-feature', roundIndex: 33 },
  { id: 'longest-think', dimension: 'session', title: 'Deepest single think', value: '14.2K tok', detail: 'reasoning tokens in one round', sessionId: 'b2e7-refactor', roundIndex: 88 },
  { id: 'think-ratio', dimension: 'session', title: 'Most think-heavy round', value: '4.8×', detail: 'reasoning : output ratio', sessionId: 'f7c8-spike', roundIndex: 12 },
  { id: 'top-tool', dimension: 'tool', title: 'Most-used tool', value: 'Read', detail: '3,184 calls across the trace' },
  { id: 'slow-tool', dimension: 'tool', title: 'Slowest tool (p90)', value: 'Bash · 6.2s', detail: 'effective latency' },
  { id: 'flaky-tool', dimension: 'tool', title: 'Most error-prone tool', value: 'Bash · 14%', detail: 'share of calls returning an error' },
];

// ---- distributions (the figures, as data) ----
function cdfCurve(xs: number[]): [number, number][] {
  return xs.map((x, i) => [x, +((i + 1) / xs.length).toFixed(3)]);
}
// A line "histogram" (frequency polygon) for a log-normal-ish per-round metric: share of rounds at
// each log-spaced bin center, peaking near `median`, spread by `sigma` (natural-log units). Areas
// are normalized so the curve reads as a share.
function freqPolygon(median: number, sigma: number, bins: number[]): [number, number][] {
  const w = bins.map((x) => Math.exp(-0.5 * ((Math.log(x) - Math.log(median)) / sigma) ** 2));
  const area = w.reduce((s, v) => s + v, 0);
  return bins.map((x, i) => [x, +(w[i] / area).toFixed(4)]);
}
const OUTPUT_TOKEN_BINS = [50, 90, 160, 280, 500, 900, 1600, 2800, 5000, 9000];
const logUnif = (lo: number, hi: number) => lo * Math.pow(hi / lo, rnd());
// A plausible per-round (prefix, append) cloud: mostly low-append tool steps deep in context, some
// high-append user turns, a few big pastes. prefix rarely falls far below the fresh delta.
function scatterCloud(n: number, prefixHi: number, bigPaste: number): [number, number][] {
  const pts: [number, number][] = [];
  for (let i = 0; i < n; i++) {
    let append = rnd() < 0.78 ? logUnif(200, 3500) : logUnif(2500, 16000); // tool step vs user turn
    if (rnd() < 0.03) append = logUnif(16000, bigPaste); // occasional big paste
    let prefix = logUnif(2000, prefixHi) * (0.6 + rnd() * 0.8); // context depth
    prefix = Math.max(prefix, append * (0.4 + rnd())); // prefix seldom far below the append
    pts.push([Math.round(prefix), Math.round(append)]); // x = prefix tokens, y = new append tokens
  }
  return pts;
}
const distributions: AnalyticsPayload['distributions'] = {
  tool_latency_distribution: {
    kind: 'cdf', xLabel: 'effective latency (ms)', yLabel: 'cumulative share', xLog: true,
    series: [
      { name: 'claude', points: cdfCurve([8, 12, 18, 26, 40, 70, 130, 280, 600, 1400, 3200, 6800]) },
      { name: 'codex', points: cdfCurve([10, 16, 24, 38, 60, 110, 220, 480, 980, 2100, 4600, 9000]) },
    ],
  },
  tool_latency_by_category: {
    kind: 'boxplot', yLabel: 'effective latency', yLog: true, yUnit: 'ms',
    groups: [
      { name: 'Planning', min: 4, q1: 10, median: 18, q3: 38, max: 140 },
      { name: 'File read / search', min: 5, q1: 14, median: 26, q3: 60, max: 380 },
      { name: 'File edit / patch', min: 8, q1: 22, median: 44, q3: 95, max: 520 },
      { name: 'Shell / command', min: 40, q1: 260, median: 900, q3: 2800, max: 9000 },
      { name: 'Web / lookup', min: 120, q1: 480, median: 1300, q3: 3600, max: 12000 },
      { name: 'Agent / task', min: 220, q1: 950, median: 2600, q3: 7200, max: 22000 },
    ],
  },
  tool_call_counts: {
    kind: 'barh', xLabel: 'calls',
    items: [
      { name: 'Read', value: 3184 }, { name: 'Edit', value: 1962 }, { name: 'Bash', value: 1488 },
      { name: 'Grep', value: 1104 }, { name: 'Glob', value: 642 }, { name: 'Write', value: 388 }, { name: 'WebFetch', value: 121 },
    ],
  },
  prefix_append_scatter: {
    kind: 'scatter', xLabel: 'prefix tokens / round', yLabel: 'new append tokens / round', xLog: true, yLog: true,
    series: [
      { name: 'claude', points: scatterCloud(190, 340_000, 130_000) },
      { name: 'codex', points: scatterCloud(150, 230_000, 90_000) },
    ],
  },
  output_tokens: {
    kind: 'histogram', xLabel: 'output tokens / round', yLabel: 'share of rounds', xLog: true,
    series: [
      { name: 'claude', points: freqPolygon(760, 0.95, OUTPUT_TOKEN_BINS) },
      { name: 'codex', points: freqPolygon(590, 0.95, OUTPUT_TOKEN_BINS) },
    ],
  },
  generation_time_cdf: {
    kind: 'cdf', xLabel: 'generation time (s)', yLabel: 'cumulative share',
    series: [
      { name: 'claude', points: cdfCurve([0.4, 0.8, 1.3, 2.1, 3.4, 5.2, 8.0, 12.5, 19, 31, 52]) },
      { name: 'codex', points: cdfCurve([0.5, 1.0, 1.7, 2.8, 4.3, 6.6, 9.8, 15, 24, 38, 61]) },
    ],
  },
  cache_hit_ratio: {
    kind: 'histogram', xLabel: 'prefix hit ratio', yLabel: 'rounds',
    bins: [
      { label: '<50%', count: 210 }, { label: '50–70%', count: 540 }, { label: '70–85%', count: 1820 },
      { label: '85–95%', count: 4360 }, { label: '95–100%', count: 5210 },
    ],
  },
  human_input_wait: {
    kind: 'cdf', xLabel: 'human wait (s)', yLabel: 'cumulative share', xLog: true,
    series: [
      { name: 'all', points: cdfCurve([2, 5, 11, 22, 45, 90, 180, 360, 720, 1800, 5400]) },
    ],
  },
};

// ---- KPIs (reconciled with the buckets above) ----
const totalRounds = MODEL_SPECS.reduce((s, m) => s + m.rounds, 0);
const totalCachedInput = MODEL_SPECS.reduce((s, m) => s + m.prefix, 0);
const totalUncachedInput = MODEL_SPECS.reduce((s, m) => s + m.append, 0);
const totalInput = totalCachedInput + totalUncachedInput;
const totalOutput = MODEL_SPECS.reduce((s, m) => s + m.output, 0);
const totalCost = cost.byModel.reduce((s, m) => s + m.costUsd, 0);

// ---- aggregate stats (the full superset; mostly derived, some plausible synthetic) ----
const totalReasoning = MODEL_SPECS.reduce((s, m) => s + m.reasoning, 0);
const totalToolCalls = sessions.reduce((s, x) => s + x.toolCalls, 0);
const sessRounds = sessions.reduce((s, x) => s + x.rounds, 0);
const mean = (xs: number[]) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : 0);
const median = (xs: number[]) => {
  const a = [...xs].sort((p, q) => p - q);
  const m = Math.floor(a.length / 2);
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
};

function buildStats(): AnalyticsPayload['stats'] {
  const I = totalInput / totalRounds; // avg total input / step
  const U = totalUncachedInput / totalRounds; // avg append / step
  const O = totalOutput / totalRounds;
  const reasoningRounds = totalRounds * 0.62; // rounds that carry any reasoning
  const avgActiveHoursPerDay = 3.4;
  const avgActiveDaysPerWeek = 4.2; // mock: days/week you actually code — per-week rates scale by this
  const totalActiveHours = avgActiveHoursPerDay * perDay.length;
  const totalGenerationS = totalOutput / 55; // @ ~55 tok/s blended decode
  const totalToolTimeS = totalToolCalls * 0.31; // ~310ms effective avg
  const totalHumanWaitS = totalGenerationS * 1.7;
  const allTokens = totalInput + totalOutput;

  // provider / model mix (from MODEL_SPECS + priced cost)
  const roundsWhere = (pred: (m: (typeof MODEL_SPECS)[number]) => boolean) =>
    MODEL_SPECS.filter(pred).reduce((sum, m) => sum + m.rounds, 0);
  const costWhere = (prov: string) =>
    cost.byModel.filter((m) => m.provider === prov).reduce((sum, m) => sum + m.costUsd, 0);
  const topModel = [...MODEL_SPECS].sort((p, q) => q.rounds - p.rounds)[0];

  return {
    // 2a. tokens per step
    avgInputTokens: I,
    avgCachedInputTokens: totalCachedInput / totalRounds,
    avgUncachedInputTokens: U,
    avgOutputTokens: O,
    avgReasoningTokens: totalReasoning / reasoningRounds,
    inputOutputRatio: totalInput / totalOutput,
    freshTokenShare: totalUncachedInput / totalInput,
    reasoningShareOfOutput: totalReasoning / totalOutput,

    // 2b. input by step trigger
    userAvgTotalInput: I * 0.9,
    userAvgAppendInput: U * 2.8,
    toolAvgTotalInput: I * 1.04,
    toolAvgAppendInput: U * 0.62,

    // 2c. context growth
    totalContextIncrease: totalUncachedInput * 0.82,
    userContextDeltaAvg: U * 2.8 * 0.95,
    userContextDeltaP50: U * 2.8 * 0.7,
    userContextDeltaP90: U * 2.8 * 2.4,
    toolContextDeltaAvg: U * 0.62 * 0.9,
    toolContextDeltaP50: U * 0.62 * 0.6,
    toolContextDeltaP90: U * 0.62 * 2.6,
    contextIncreaseToAppendRatio: 0.82,
    userContextIncreaseToAppendRatio: 0.95,
    toolContextIncreaseToAppendRatio: 0.74,
    userGrowthShare: 0.86,
    toolGrowthShare: 0.79,
    userReductionShare: 0.11,
    toolReductionShare: 0.18,
    userMajorCompactShare: 0.05,
    toolMajorCompactShare: 0.03,

    // 2d. cache efficiency
    prefixHitRate: totalCachedInput / totalInput,
    hitRateP50: 0.93,
    hitRateP90: 0.985,
    userHitRate: 0.46,
    toolHitRate: 0.94,
    hitRateDecayPer100: -0.023,

    // 2e. request timing
    avgRequestS: 4.8,
    p50RequestS: 3.1,
    p90RequestS: 11.4,
    p99RequestS: 28.7,
    genTimeP50S: 2.4,
    genTimeP90S: 8.9,
    totalGenerationS,
    avgDecodeTps: 55,
    postReasoningDecodeTps: 78,
    estTtftS: 0.42,

    // 2f. tools
    avgToolCallsPerRound: totalToolCalls / sessRounds,
    toolCallsPerRequest: (totalToolCalls / sessRounds) * 6.5,
    stepsWithToolsShare: 0.71,
    toolLatencyP50Ms: 42,
    toolLatencyP90Ms: 1850,
    totalToolTimeS,
    toolErrorRate: 0.058,
    readWriteRatio: 2.6,
    avgToolResultChars: 2400,
    topToolName: 'Read',
    topToolShare: 0.34,

    // 2g. human-in-the-loop
    totalHumanWaitS,
    humanWaitAvgS: 64,
    humanWaitP50S: 22,
    humanWaitP90S: 210,
    humanInLoopShare: 0.63,
    timeSplitGenerationShare: 0.22,
    timeSplitToolShare: 0.15,
    timeSplitWaitingShare: 0.63,

    // 2h. per session
    avgRoundsPerSession: mean(sessions.map((s) => s.rounds)),
    avgToolCallsPerSession: mean(sessions.map((s) => s.toolCalls)),
    avgSessionDurationS: mean(sessions.map((s) => s.durationS)),
    avgSessionCostUsd: mean(sessions.map((s) => s.costUsd)),
    autonomyDepthAvg: 5.4,
    autonomyDepthP90: 18,
    avgHumanInterjections: 7.2,
    avgModelsPerSession: 1.6,
    avgModelSwitches: 0.7,
    typicalSessionSteps: median(sessions.map((s) => s.rounds)),
    typicalSessionMinutes: median(sessions.map((s) => s.durationS)) / 60,
    typicalSessionCostUsd: median(sessions.map((s) => s.costUsd)),

    // 2i. per day / rates
    avgStepsPerDay: mean(perDay.map((d) => d.rounds)),
    avgSessionsPerDay: 2.3,
    avgActiveHoursPerDay,
    avgCostPerDay: mean(perDay.map((d) => d.costUsd)),
    avgStepsPerWeek: mean(perDay.map((d) => d.rounds)) * avgActiveDaysPerWeek,
    avgSessionsPerWeek: 2.3 * avgActiveDaysPerWeek,
    avgActiveDaysPerWeek,
    avgCostPerWeek: mean(perDay.map((d) => d.costUsd)) * avgActiveDaysPerWeek,
    costPerHourUsd: totalCost / totalActiveHours,
    tokensPerUsd: allTokens / totalCost,
    stepsPerUsd: totalRounds / totalCost,
    toolCallsPerUsd: totalToolCalls / totalCost,
    blendedUsdPerMtok: totalCost / (allTokens / 1e6),
    cacheSavedPct: cost.cacheSavingsUsd / (totalCost + cost.cacheSavingsUsd),
    reasoningTaxPerStepUsd: cost.reasoningCostUsd / totalRounds,

    // 2j. models & providers
    claudeStepShare: roundsWhere((m) => m.provider === 'claude') / totalRounds,
    codexStepShare: roundsWhere((m) => m.provider === 'codex') / totalRounds,
    claudeCostShare: costWhere('claude') / totalCost,
    codexCostShare: costWhere('codex') / totalCost,
    modelsRepresented: MODEL_SPECS.length,
    topModelName: topModel.model,
    topModelStepShare: topModel.rounds / totalRounds,
    opusStepShare: roundsWhere((m) => m.model.includes('opus')) / totalRounds,
  };
}
const stats = buildStats();

export const mockAnalytics: AnalyticsPayload = {
  kpis: {
    sessions: sessions.length,
    users: 3,
    rounds: totalRounds,
    inputTokens: totalInput,
    cachedInputTokens: totalCachedInput,
    uncachedInputTokens: totalUncachedInput,
    outputTokens: totalOutput,
    totalCostUsd: totalCost,
    cacheSavingsUsd: cost.cacheSavingsUsd,
    firstTsUs: usAt(START),
    lastTsUs: usAt(END),
    activeDays: perDay.length,
  },
  perDay,
  hourWeekday,
  cost,
  providers,
  stats,
  facts,
  sessions,
  rawAvailable: true, // mock = "user uploaded their own file", so the per-round raw viewer is enabled
  distributions,
};
