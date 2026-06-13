// Interactive analytics dashboard — the shared render layer behind both the /lab sandbox and the
// Analyze surface. It owns NO data of its own: a host page renders the AnalyticsDashboard.astro
// skeleton, calls initDashboard() once to wire the static controls, then calls loadDashboard(payload,
// source) whenever data is ready. `source` is the real AnalyticsSource (drives session-detail / raw
// fetches over the QA RPC) or null for mock mode (the mock session-detail + raw maps stand in).
//
// All element lookups are by id against the server-rendered skeleton; the two hosts never coexist on
// one page, so the global ids don't collide.

import type {
  AnalyticsPayload,
  Fact,
  SessionRow,
  SessionDetail,
  SessionRoundPoint,
  RoundRaw,
} from './types';
import {
  renderCalendar,
  renderHourWeekday,
  renderCostByModel,
  renderSessionTimeline,
  renderDistribution,
  disposeAll,
} from '../charts';
import { intComma, compact, fmtUsd } from '../format';
import { PRICING_AS_OF } from './cost';
import { mockSessionDetails, mockRoundRaw } from '../mock/analytics';
import type { DashboardSource } from './source';

// ---- data source: set by loadDashboard. `a` is read only after a load (entry points guard on
// `loaded`), so the definite-assignment assertion is safe. `source` null => mock mode. ----
let a!: AnalyticsPayload;
let source: DashboardSource | null = null;
let loaded = false;
const detailCache = new Map<string, SessionDetail>();

const $ = (id: string) => document.getElementById(id)!;
const set = (key: string, val: string) => {
  const el = document.querySelector(`[data-kpi="${key}"]`);
  if (el) el.textContent = val;
};
const foot = (key: string, val: string) => {
  const el = document.querySelector(`[data-foot="${key}"]`);
  if (el) el.textContent = val;
};

// element refs resolved in initDashboard (after the skeleton is in the DOM)
let statsEl: HTMLElement;
let factsEl: HTMLElement;
let listEl: HTMLElement;
let roundDetailEl: HTMLElement;
let distEl: HTMLElement;

// ---- shared formatters (used across KPIs / stats / round inspector) ----
const pct = (r: number) => `${(r * 100).toFixed(1)}%`;
const xr = (r: number) => `${r.toFixed(2)}×`;
const n1 = (v: number) => v.toFixed(1);
const n2 = (v: number) => v.toFixed(2);
const tps = (v: number) => `${Math.round(v)} tok/s`;
const ms = (v: number) => `${Math.round(v)} ms`;
const tok = (v: number) => compact(Math.round(v));
const dur = (sec: number) =>
  sec < 1 ? `${Math.round(sec * 1000)} ms`
  : sec < 60 ? `${sec.toFixed(1)} s`
  : sec < 3600 ? `${Math.round(sec / 60)} min`
  : `${(sec / 3600).toFixed(1)} h`;
const fmtDur = (s: number) => (s >= 3600 ? `${(s / 3600).toFixed(1)}h` : `${Math.round(s / 60)}m`);
const esc = (s: string) => s.replace(/[&<>]/g, (c) => (c === '&' ? '&amp;' : c === '<' ? '&lt;' : '&gt;'));

// ---- KPIs ----
function renderKpis(): void {
  const days = Math.round((a.kpis.lastTsUs - a.kpis.firstTsUs) / 1e6 / 86400);
  set('sessions', intComma(a.kpis.sessions));
  set('rounds', intComma(a.kpis.rounds));
  set('cost', fmtUsd(a.kpis.totalCostUsd));
  set('saved', fmtUsd(a.kpis.cacheSavingsUsd));
  set('input', compact(a.kpis.inputTokens));
  set('cached', compact(a.kpis.cachedInputTokens));
  set('uncached', compact(a.kpis.uncachedInputTokens));
  set('output', compact(a.kpis.outputTokens));
  foot('span', `${a.kpis.activeDays} active days · ${days}-day span`);
  foot('users', `${a.kpis.users} users`);
  foot('costperday', a.kpis.activeDays ? `${fmtUsd(a.kpis.totalCostUsd / a.kpis.activeDays)} / active day` : '—');
  const cachedPct = a.kpis.inputTokens ? Math.round((a.kpis.cachedInputTokens / a.kpis.inputTokens) * 100) : 0;
  foot('cachedpct', `${cachedPct}% served from cache`);
  foot('outputperstep', a.kpis.rounds ? `${compact(Math.round(a.kpis.outputTokens / a.kpis.rounds))} avg / step` : '—');
}

// ---- providers split (read precomputed payload.providers; no frontend aggregation) ----
const provColor = (p: string) => (p === 'claude' ? 'var(--terra)' : p === 'codex' ? 'var(--sage)' : 'var(--gold)');
function renderProviders(): void {
  const barEl = $('prov-bar');
  const legEl = $('prov-legend');
  barEl.innerHTML = '';
  legEl.innerHTML = '';
  for (const p of a.providers) {
    const seg = document.createElement('div');
    seg.className = 'prov-seg';
    seg.style.flex = `${p.stepShare} 1 0`;
    seg.style.background = provColor(p.provider);
    seg.title = `${p.provider}: ${Math.round(p.stepShare * 100)}% of steps`;
    seg.innerHTML = `<span class="pl">${esc(p.provider)}</span><span class="pct">${Math.round(p.stepShare * 100)}%</span>`;
    barEl.appendChild(seg);

    const item = document.createElement('div');
    item.className = 'prov-item';
    item.innerHTML =
      `<span class="prov-dot" style="background:${provColor(p.provider)}"></span>` +
      `<span class="pn">${esc(p.provider)}</span>` +
      `<span class="pm">${Math.round(p.stepShare * 100)}% steps · ${Math.round(p.costShare * 100)}% cost · ${intComma(p.rounds)} steps · ${fmtUsd(p.cost)}</span>`;
    legEl.appendChild(item);
  }
}

// ---- charts ----
function renderTopCharts(): void {
  renderCalendar($('c-calendar'), a.perDay);
  renderHourWeekday($('c-heat'), a.hourWeekday);
  renderCostByModel($('c-cost'), a.cost.byModel);
  $('cost-asof').textContent = `Prices as of ${PRICING_AS_OF}. ${intComma(a.cost.unpricedRounds)} steps on unpriced models.`;
}

// ---- aggregate stats (full superset; grouped tables — review then prune) ----
type StatSub = { label?: string; rows: [string, string][] };
function buildStatGroups(): { title: string; subs: StatSub[] }[] {
  const s = a.stats;
  return [
  // Ordered as a granularity ladder, global -> local: whole-trace mix, then per-day, per-session,
  // per-request, the human gap, then the per-step mechanics. packStats() preserves this order
  // (newspaper fill: down each column, then the next), so the layout reads as a deliberate zoom.
  { title: 'Models & providers', subs: [
    { label: 'Mix', rows: [
      ['Steps · Claude', pct(s.claudeStepShare)],
      ['Steps · Codex', pct(s.codexStepShare)],
      ['Cost · Claude', pct(s.claudeCostShare)],
      ['Cost · Codex', pct(s.codexCostShare)],
    ] },
    { label: 'Models', rows: [
      ['Models represented', intComma(s.modelsRepresented)],
      [`Top model (${s.topModelName})`, pct(s.topModelStepShare)],
    ] },
  ] },
  { title: 'Per day & rates', subs: [
    { label: 'Per day', rows: [
      ['Steps / day', n1(s.avgStepsPerDay)],
      ['Sessions / day', n1(s.avgSessionsPerDay)],
      ['Active hours / day', n1(s.avgActiveHoursPerDay)],
      ['Cost / day', fmtUsd(s.avgCostPerDay)],
    ] },
    { label: 'Per week', rows: [
      ['Steps / week', n1(s.avgStepsPerWeek)],
      ['Sessions / week', n1(s.avgSessionsPerWeek)],
      ['Active days / week', n1(s.avgActiveDaysPerWeek)],
      ['Cost / week', fmtUsd(s.avgCostPerWeek)],
    ] },
    { label: 'Cost efficiency', rows: [
      ['$ / hour of agent work', fmtUsd(s.costPerHourUsd)],
      ['Steps per $1', n1(s.stepsPerUsd)],
      ['Cache saved', pct(s.cacheSavedPct)],
    ] },
  ] },
  { title: 'Per session', subs: [
    { label: 'Volume', rows: [
      ['Agent steps', n1(s.avgRoundsPerSession)],
      ['Tool calls', n1(s.avgToolCallsPerSession)],
      ['Duration', dur(s.avgSessionDurationS)],
      ['Cost', fmtUsd(s.avgSessionCostUsd)],
    ] },
    { label: 'Autonomy', rows: [
      ['Depth avg', n1(s.autonomyDepthAvg)],
      ['Depth p90', n1(s.autonomyDepthP90)],
      ['Human interjections', n1(s.avgHumanInterjections)],
    ] },
    { label: 'Models', rows: [
      ['Models used', n1(s.avgModelsPerSession)],
      ['Model switches', n1(s.avgModelSwitches)],
    ] },
    { label: 'Typical', rows: [
      ['Median session', `${Math.round(s.typicalSessionSteps)} steps · ${Math.round(s.typicalSessionMinutes)}m · ${fmtUsd(s.typicalSessionCostUsd)}`],
    ] },
  ] },
  { title: 'Request timing', subs: [
    { label: 'Wall time', rows: [
      ['Avg', dur(s.avgRequestS)],
      ['p50', dur(s.p50RequestS)],
      ['p90', dur(s.p90RequestS)],
      ['p99', dur(s.p99RequestS)],
    ] },
    { label: 'Generation', rows: [
      ['p50', dur(s.genTimeP50S)],
      ['p90', dur(s.genTimeP90S)],
      ['Total', dur(s.totalGenerationS)],
    ] },
    { label: 'Throughput', rows: [
      ['Avg decode', tps(s.avgDecodeTps)],
      ['Post-reasoning', tps(s.postReasoningDecodeTps)],
      ['Est. TTFT', dur(s.estTtftS)],
    ] },
    { label: 'Time split', rows: [
      ['Generation', pct(s.timeSplitGenerationShare)],
      ['Tools', pct(s.timeSplitToolShare)],
      ['Waiting on you', pct(s.timeSplitWaitingShare)],
    ] },
  ] },
  { title: 'Human in the loop', subs: [
    { label: 'Wait', rows: [
      ['Total', dur(s.totalHumanWaitS)],
      ['Avg', dur(s.humanWaitAvgS)],
      ['p50', dur(s.humanWaitP50S)],
      ['p90', dur(s.humanWaitP90S)],
    ] },
  ] },
  { title: 'Tokens / step', subs: [
    { label: 'Volume', rows: [
      ['Avg total input', tok(s.avgInputTokens)],
      ['Avg cached-read input', tok(s.avgCachedInputTokens)],
      ['Avg fresh (append) input', tok(s.avgUncachedInputTokens)],
      ['Avg output', tok(s.avgOutputTokens)],
      ['Avg reasoning (when present)', tok(s.avgReasoningTokens)],
    ] },
    { label: 'Ratios', rows: [
      ['Input : output amplification', xr(s.inputOutputRatio)],
      ['Fresh-token share', pct(s.freshTokenShare)],
      ['Reasoning share of output', pct(s.reasoningShareOfOutput)],
    ] },
    { label: 'Input · user-initiated', rows: [
      ['Total input', tok(s.userAvgTotalInput)],
      ['Append', tok(s.userAvgAppendInput)],
    ] },
    { label: 'Input · tool-triggered', rows: [
      ['Total input', tok(s.toolAvgTotalInput)],
      ['Append', tok(s.toolAvgAppendInput)],
    ] },
  ] },
  { title: 'Cache efficiency', subs: [
    { label: 'Overall', rows: [
      ['Prefix hit rate', pct(s.prefixHitRate)],
      ['p50', pct(s.hitRateP50)],
      ['p90', pct(s.hitRateP90)],
    ] },
    { label: 'By trigger', rows: [
      ['User-initiated', pct(s.userHitRate)],
      ['Tool-triggered', pct(s.toolHitRate)],
    ] },
  ] },
  { title: 'Context growth', subs: [
    { label: 'Total', rows: [
      ['Context increase', tok(s.totalContextIncrease)],
    ] },
    { label: 'User Δ', rows: [
      ['avg', tok(s.userContextDeltaAvg)],
      ['p50', tok(s.userContextDeltaP50)],
      ['p90', tok(s.userContextDeltaP90)],
    ] },
    { label: 'Tool Δ', rows: [
      ['avg', tok(s.toolContextDeltaAvg)],
      ['p50', tok(s.toolContextDeltaP50)],
      ['p90', tok(s.toolContextDeltaP90)],
    ] },
    { label: 'Increase : append', rows: [
      ['Overall', xr(s.contextIncreaseToAppendRatio)],
      ['User', xr(s.userContextIncreaseToAppendRatio)],
      ['Tool', xr(s.toolContextIncreaseToAppendRatio)],
    ] },
    { label: 'Change mix', rows: [
      ['Growth · user', pct(s.userGrowthShare)],
      ['Growth · tool', pct(s.toolGrowthShare)],
      ['Reduction · user', pct(s.userReductionShare)],
      ['Reduction · tool', pct(s.toolReductionShare)],
      ['Compaction · user', pct(s.userMajorCompactShare)],
      ['Compaction · tool', pct(s.toolMajorCompactShare)],
    ] },
  ] },
  { title: 'Tools', subs: [
    { label: 'Volume', rows: [
      ['Calls / step', n2(s.avgToolCallsPerRound)],
      ['Calls / request', n1(s.toolCallsPerRequest)],
      ['Steps with tools', pct(s.stepsWithToolsShare)],
    ] },
    { label: 'Latency', rows: [
      ['p50', ms(s.toolLatencyP50Ms)],
      ['p90', ms(s.toolLatencyP90Ms)],
      ['Total tool time', dur(s.totalToolTimeS)],
    ] },
    { label: 'Quality', rows: [
      ['Error rate', pct(s.toolErrorRate)],
    ] },
  ] },
  ];
}

let statCards: { card: HTMLElement; idx: number }[] = [];

// Rebuild the stat cards from the current payload, then lay them out.
function renderStats(): void {
  const rowHtml = ([k, v]: [string, string]) =>
    `<div class="stat-row"><span class="sk">${k}</span><span class="sv">${v}</span></div>`;
  const subHtml = (sub: StatSub) =>
    (sub.label ? `<div class="stat-sub"><span class="ssl">${sub.label}</span><span class="ssr"></span></div>` : '') +
    sub.rows.map(rowHtml).join('');
  statCards = buildStatGroups().map((g, idx) => {
    const card = document.createElement('div');
    card.className = 'stat-group';
    const count = g.subs.reduce((n, sub) => n + sub.rows.length, 0);
    card.innerHTML = `<h4>${g.title}<span class="gcount">${count}</span></h4>` + g.subs.map(subHtml).join('');
    return { card, idx };
  });
  packStats();
}

// Order-preserving newspaper fill: measure each card at the real column width, then lay the cards
// out IN ORDER, flowing down a column and starting the next once the running height passes the
// card's fair share (total / nCols). Unlike LPT bin-packing this keeps the granularity-ladder
// order (read down col 1, then col 2…) at the cost of slightly more ragged column bottoms.
function packStats(): void {
  if (!statCards.length) { statsEl.innerHTML = ''; return; }
  const GAP = 16;
  const MIN = 280; // min column width before dropping below 3 cols on narrow viewports
  const MAX_COLS = 3; // wider cards: cap at 3 even on very wide screens
  const W = statsEl.clientWidth || 1;
  const nCols = Math.max(1, Math.min(MAX_COLS, statCards.length, Math.floor((W + GAP) / (MIN + GAP))));
  const colW = (W - (nCols - 1) * GAP) / nCols;
  const meas = document.createElement('div');
  meas.style.cssText = `position:absolute;left:-9999px;top:0;visibility:hidden;width:${colW}px;`;
  document.body.appendChild(meas);
  const items = statCards.map(({ card }) => {
    meas.appendChild(card); // moves card; measure height at the true column width (labels may wrap)
    return { card, h: card.offsetHeight };
  });
  meas.remove();
  const total = items.reduce((t, it) => t + it.h + GAP, 0);
  const target = total / nCols; // fair share per column
  const cols = Array.from({ length: nCols }, () => [] as typeof items);
  let ci = 0;
  let cur = 0;
  for (const it of items) {
    // advance to the next column once this card's midpoint would cross the fair-share line — a
    // balanced break that still keeps the cards in their ladder order.
    if (ci < nCols - 1 && cur + (it.h + GAP) / 2 > target) {
      ci += 1;
      cur = 0;
    }
    cols[ci].push(it);
    cur += it.h + GAP;
  }
  statsEl.innerHTML = '';
  for (const col of cols) {
    const d = document.createElement('div');
    d.className = 'stat-col';
    col.forEach((it) => d.appendChild(it.card));
    statsEl.appendChild(d);
  }
}
let statsRt = 0;

// ---- facts (grouped into labeled categories) ----
const FACT_CATS: { key: string; label: string }[] = [
  { key: 'time', label: 'Time & cadence' },
  { key: 'cost', label: 'Cost' },
  { key: 'session', label: 'Conversations' },
  { key: 'tool', label: 'Tools' },
  { key: 'cache', label: 'Cache & efficiency' },
  { key: 'model', label: 'Models' },
];
const factCard = (f: Fact): HTMLElement => {
  const linked = !!f.sessionId;
  const card = document.createElement(linked ? 'button' : 'div');
  card.className = 'fact';
  card.dataset.dim = f.dimension;
  card.dataset.link = linked ? '1' : '0';
  if (linked) card.dataset.session = f.sessionId!;
  card.innerHTML =
    `<div class="fact-top"><span class="fact-chip">${f.dimension}</span>` +
    `${linked ? '<span class="fact-go" aria-hidden="true">→</span>' : ''}</div>` +
    `<div class="fact-title">${f.title}</div>` +
    `<div class="fact-val">${f.value}</div>` +
    (f.detail ? `<div class="fact-detail">${f.detail}</div>` : '');
  if (linked) {
    card.addEventListener('click', () => selectSession(f.sessionId!, { scroll: true }));
  }
  return card;
};
function renderFacts(): void {
  factsEl.innerHTML = '';
  for (const cat of FACT_CATS) {
    const items = a.facts.filter((f) => f.dimension === cat.key);
    if (!items.length) continue;
    const section = document.createElement('div');
    section.className = 'fact-cat';
    section.dataset.dim = cat.key;
    const head = document.createElement('div');
    head.className = 'fact-cat-head';
    head.innerHTML = `<span class="fact-cat-label">${cat.label}</span><span class="fact-cat-count">${items.length}</span><span class="fact-cat-rule"></span>`;
    const grid = document.createElement('div');
    grid.className = 'facts-grid';
    items.forEach((f) => grid.appendChild(factCard(f)));
    section.appendChild(head);
    section.appendChild(grid);
    factsEl.appendChild(section);
  }
}

// ---- sessions (filter by provider · sort · search) ----
let provFilter = 'all';
let sortKey = 'rounds';
let search = '';
let selectedId: string | null = null;

const sorters: Record<string, (x: SessionRow, y: SessionRow) => number> = {
  rounds: (x, y) => y.rounds - x.rounds,
  duration: (x, y) => y.durationS - x.durationS,
  cost: (x, y) => y.costUsd - x.costUsd,
  recent: (x, y) => y.lastTsUs - x.lastTsUs,
};

function visibleSessions(): SessionRow[] {
  const q = search.trim().toLowerCase();
  return a.sessions
    .filter((s) => provFilter === 'all' || s.provider === provFilter)
    .filter((s) => !q || s.sessionId.toLowerCase().includes(q) || s.primaryModel.toLowerCase().includes(q))
    .sort(sorters[sortKey] ?? sorters.rounds);
}

// local-date helpers for grouping the picker by day (2-line marker: weekday over month-day)
const dayOf = (tsUs: number) => {
  const d = new Date(tsUs / 1000);
  return {
    key: `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`,
    ts: d.getTime(),
    wd: d.toLocaleDateString(undefined, { weekday: 'short' }),
    md: d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
  };
};

function sessionCard(s: SessionRow): HTMLButtonElement {
  const row = document.createElement('button');
  row.className = 'sess-row';
  row.dataset.session = s.sessionId;
  row.setAttribute('role', 'option');
  if (s.sessionId === selectedId) row.classList.add('active');
  row.innerHTML =
    `<div class="sr-id"><span class="sr-name" title="${esc(s.title ?? s.sessionId)}">${esc(s.title ?? s.sessionId)}</span>` +
    `<span class="pill ${s.provider === 'codex' ? 'codex' : ''}">${s.provider}</span></div>` +
    `<div class="sr-meta">${intComma(s.rounds)} steps · ${fmtDur(s.durationS)} · ${fmtUsd(s.costUsd)}</div>`;
  row.addEventListener('click', () => selectSession(s.sessionId, { scroll: true }));
  return row;
}

function renderSessionList(): void {
  const rows = visibleSessions();
  listEl.innerHTML = '';
  if (!rows.length) {
    listEl.innerHTML = '<div class="sess-empty">No sessions match.</div>';
  } else {
    // group by local date (most recent day first); the chosen sort orders cards within a day
    const groups = new Map<string, { ts: number; wd: string; md: string; items: SessionRow[] }>();
    for (const s of rows) {
      const d = dayOf(s.firstTsUs);
      const g = groups.get(d.key) ?? { ts: d.ts, wd: d.wd, md: d.md, items: [] };
      g.items.push(s);
      groups.set(d.key, g);
    }
    for (const g of [...groups.values()].sort((x, y) => y.ts - x.ts)) {
      const section = document.createElement('div');
      section.className = 'sess-date-group';
      const head = document.createElement('div');
      head.className = 'sess-date-head';
      const n = g.items.length;
      head.innerHTML = `<span class="sd-wd">${g.wd}</span><span class="sd-md">${g.md}</span><span class="sd-count">${n} session${n > 1 ? 's' : ''}</span>`;
      const cards = document.createElement('div');
      cards.className = 'sess-cards';
      g.items.forEach((s) => cards.appendChild(sessionCard(s)));
      section.appendChild(head);
      section.appendChild(cards);
      listEl.appendChild(section);
    }
  }
  $('sess-count').textContent = `${rows.length} of ${a.sessions.length}`;
}

// round inspector (filled when a round is clicked in the timeline)
function roundHint(): void {
  roundDetailEl.innerHTML = '<div class="rd-hint">Click a round in the timeline to inspect its tokens, timing, and tools.</div>';
}
// Bumped on every round render so a slow async raw fetch can't inject into a newer round's card.
let rawRenderToken = 0;
function renderRoundDetail(r: SessionRoundPoint, idx: number, detail: SessionDetail): void {
  const myToken = ++rawRenderToken;
  const total = r.prefixTokens + r.appendTokens;
  const hit = total ? r.prefixTokens / total : 0;
  const decode = r.inferenceS ? r.outputTokens / r.inferenceS : 0;
  const prev = idx > 0 ? detail.rounds[idx - 1] : null;
  const sincePrev = prev ? (r.tsUs - prev.tsUs) / 1e6 : 0;
  const elapsed = (r.tsUs - detail.rounds[0].tsUs) / 1e6;
  const stat = (label: string, val: string) => `<div class="rd-stat"><span>${label}</span><b>${val}</b></div>`;
  const tools = r.tools.length
    ? r.tools.map((t) => `<span class="rd-tool ${t.error ? 'err' : ''}">${t.name} <b>${t.ms}ms</b>${t.error ? ' · err' : ''}</span>`).join('')
    : '<span class="rd-empty">no tool calls this round</span>';
  roundDetailEl.innerHTML =
    '<div class="rd-card">' +
    `<div class="rd-head"><span class="rd-seq">Round ${r.seq}</span>` +
    `<span class="rd-tag ${r.isUserInput ? 'user' : 'tool'}">${r.isUserInput ? 'user-initiated' : 'tool-step'}</span>` +
    `<span class="rd-time">${dur(elapsed)} in${prev ? ` · +${dur(sincePrev)} since prev` : ''}</span></div>` +
    '<div class="rd-stats">' +
    stat('Cached input', tok(r.prefixTokens)) +
    stat('Fresh input', tok(r.appendTokens)) +
    stat('Output', tok(r.outputTokens)) +
    stat('Reasoning', tok(r.reasoningTokens)) +
    stat('Cache hit', pct(hit)) +
    stat('Inference', `${r.inferenceS}s`) +
    stat('Decode', `${Math.round(decode)} tok/s`) +
    stat('Tool calls', String(r.toolCount)) +
    '</div>' +
    `<div class="rd-tools">${tools}</div>` +
    '<div class="rd-raw-slot"></div>' +
    '</div>';
  // Raw input/output, only when the trace carries it (local uploads). Fetched asynchronously and
  // injected into the slot above so the stats render instantly: real mode pulls the LOCAL-only
  // sidecar by trace_key (source.roundRaw); mock mode uses the synthetic raw map. Each section is an
  // independent <details> row (Output/user-Input open, Tools folded). rawAvailable gates it off for
  // sanitized/remote traces (no originals).
  void (async () => {
    if (!a.rawAvailable) return;
    const raw: RoundRaw | null = source
      ? await source.roundRaw(r.traceKey).catch(() => null)
      : mockRoundRaw(detail.sessionId, idx);
    if (myToken !== rawRenderToken || !raw) return; // a newer round was selected, or nothing to show
    const slot = roundDetailEl.querySelector('.rd-raw-slot');
    if (slot) slot.innerHTML = renderRaw(raw);
  })();
}

// one collapsible <details> row per raw section; `open` controls the default-expanded state.
function rawRow(title: string, bodyHtml: string, open = false): string {
  return `<details class="raw-sec"${open ? ' open' : ''}><summary class="raw-h">${title}</summary>${bodyHtml}</details>`;
}
function rawPre(body: string, cls = ''): string {
  return `<pre class="raw-pre ${cls}">${esc(body)}</pre>`;
}
function renderRaw(raw: RoundRaw): string {
  let html = '<div class="rd-raw">';
  // user rounds show their message open by default (it's the key context); tool-step rounds show
  // only the output (their input is just the prior round's tool results), per the inspector design.
  if (raw.inputKind === 'user') html += rawRow('Input · user message', rawPre(raw.input), true);
  html += rawRow('Output · assistant', rawPre(raw.output), true); // output stays open too
  if (raw.tools && raw.tools.length) {
    const items = raw.tools
      .map(
        (t) =>
          `<div class="raw-tool ${t.error ? 'err' : ''}"><div class="raw-tool-h">${esc(t.name)}${t.error ? ' · error' : ''}</div>` +
          rawPre(t.input, 'sm') +
          rawPre(t.result, 'sm res') +
          '</div>',
      )
      .join('');
    html += rawRow(`Tool calls · ${raw.tools.length}`, `<div class="raw-tools">${items}</div>`);
  }
  return html + '</div>';
}

// Detail comes from the real session_detail RPC (cached) when a trace is loaded, else the mock map.
let selectToken = 0;
async function getDetail(id: string): Promise<SessionDetail | null> {
  if (!source) return mockSessionDetails[id] ?? null;
  const cached = detailCache.get(id);
  if (cached) return cached;
  try {
    const d = await source.sessionDetail(id);
    detailCache.set(id, d);
    return d;
  } catch (err) {
    console.error('session detail failed', err);
    return null;
  }
}

async function selectSession(id: string, opts?: { scroll?: boolean }): Promise<void> {
  const s = a.sessions.find((x) => x.sessionId === id);
  if (!s) return;
  selectedId = id;
  const myToken = ++selectToken; // detail fetch may be async (real mode); ignore if superseded
  document.querySelectorAll('.sess-row').forEach((r) =>
    r.classList.toggle('active', (r as HTMLElement).dataset.session === id),
  );
  // use the conversation title when the trace carries one; keep the id visible in the meta line
  $('sess-title').textContent = s.title ?? `Session ${id}`;
  $('sess-meta').textContent =
    `${s.title ? `${id} · ` : ''}${s.primaryModel} · ${intComma(s.rounds)} steps · ${fmtDur(s.durationS)} · ${s.toolCalls} tool calls · ${s.errors} errors · ${fmtUsd(s.costUsd)}`;
  roundDetailEl.innerHTML = '<div class="rd-hint">Loading timeline…</div>';
  if (opts?.scroll) document.querySelector('.sess-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

  const detail = await getDetail(id);
  if (myToken !== selectToken) return; // a newer selection took over while we awaited
  const toolsEl = $('sess-tools');
  if (!detail) {
    roundDetailEl.innerHTML = '<div class="rd-hint">Could not load this session’s timeline.</div>';
    toolsEl.innerHTML = '';
    return;
  }
  renderSessionTimeline($('c-session'), detail, (round, i) => renderRoundDetail(round, i, detail));
  roundHint();
  toolsEl.innerHTML = '';
  for (const t of detail.tools) {
    const chip = document.createElement('span');
    chip.className = 'tool';
    chip.innerHTML = `${esc(t.name)} <b>${intComma(t.count)}</b> · ${t.p50Ms}ms p50 · ${t.errors} err`;
    toolsEl.appendChild(chip);
  }
}

// ---- distributions ----
const DIST_TITLES: Record<string, string> = {
  tool_latency_distribution: 'Tool latency (CDF)',
  tool_latency_by_category: 'Tool latency by category',
  tool_call_counts: 'Tool call counts',
  prefix_append_scatter: 'Append vs prefix (per round)',
  output_tokens: 'Output tokens / round',
  generation_time_cdf: 'Generation time (CDF)',
  cache_hit_ratio: 'Prefix cache hit ratio',
  human_input_wait: 'Human input wait (CDF)',
};
function renderDistributions(): void {
  distEl.innerHTML = '';
  for (const [key, spec] of Object.entries(a.distributions)) {
    const card = document.createElement('div');
    card.className = 'dist-card';
    const h = document.createElement('h4');
    h.textContent = DIST_TITLES[key] ?? key;
    const body = document.createElement('div');
    body.className = 'chart chart--dist';
    card.appendChild(h);
    card.appendChild(body);
    distEl.appendChild(card);
    renderDistribution(body, spec, key);
  }
}

// ---- full render pass (re-run whenever loadDashboard swaps `a`) ----
function renderAll(): void {
  disposeAll(); // free every mounted ECharts instance before a fresh pass (mountChart re-inits cleanly)
  detailCache.clear();
  selectedId = null;
  // reset the session toolbar to defaults so a new trace starts unfiltered
  provFilter = 'all';
  sortKey = 'rounds';
  search = '';
  $('sess-provider').querySelectorAll('button').forEach((b) =>
    b.classList.toggle('active', (b as HTMLElement).dataset.prov === 'all'),
  );
  ($('sess-sort') as HTMLSelectElement).value = 'rounds';
  ($('sess-search') as HTMLInputElement).value = '';

  renderKpis();
  renderProviders();
  renderTopCharts();
  renderStats();
  renderFacts();
  renderDistributions();
  renderSessionList();
  const first = visibleSessions()[0];
  if (first) {
    selectSession(first.sessionId);
  } else {
    $('sess-title').textContent = '—';
    $('sess-meta').textContent = '—';
    roundDetailEl.innerHTML = '';
    $('sess-tools').innerHTML = '';
  }
}

// ---- public API ----
let initialized = false;
/** Wire the static controls (session toolbar + resize) ONCE. Idempotent; call before loadDashboard. */
export function initDashboard(): void {
  if (initialized) return;
  initialized = true;
  statsEl = $('stats');
  factsEl = $('facts');
  listEl = $('sess-list');
  roundDetailEl = $('round-detail');
  distEl = $('dist');

  window.addEventListener('resize', () => {
    if (!loaded) return;
    clearTimeout(statsRt);
    statsRt = window.setTimeout(packStats, 150);
  });
  $('sess-provider').addEventListener('click', (e) => {
    if (!loaded) return;
    const btn = (e.target as HTMLElement).closest('button[data-prov]') as HTMLElement | null;
    if (!btn) return;
    provFilter = btn.dataset.prov!;
    $('sess-provider').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    renderSessionList();
  });
  ($('sess-sort') as HTMLSelectElement).addEventListener('change', (e) => {
    if (!loaded) return;
    sortKey = (e.target as HTMLSelectElement).value;
    renderSessionList();
  });
  ($('sess-search') as HTMLInputElement).addEventListener('input', (e) => {
    if (!loaded) return;
    search = (e.target as HTMLInputElement).value;
    renderSessionList();
  });
}

/** Render (or re-render) the whole dashboard from a payload. `src` is the real AnalyticsSource that
 *  backs session-detail / raw fetches, or null for mock mode. Disposes a superseded real source. */
export function loadDashboard(payload: AnalyticsPayload, src: DashboardSource | null): void {
  if (source && source !== src) source.dispose();
  a = payload;
  source = src;
  loaded = true;
  renderAll();
}
