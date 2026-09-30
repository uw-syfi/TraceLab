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
  SessionTopologyChild,
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
import { browserCopy, template } from '../../i18n/browser';

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
// Also escapes quotes: values land in quoted attributes (such as title) as well as in text.
const ESC: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s: string) => s.replace(/[&<>"']/g, (c) => ESC[c]);
const rt = () => browserCopy().dashboardRuntime;
const L = (label: string): string => {
  const labels = rt().labels;
  const topModel = label.match(/^Top model \((.*)\)$/);
  if (topModel) return `${labels['Top model'] ?? 'Top model'} (${topModel[1]})`;
  return labels[label] ?? label;
};
const localDur = (s: number): string => {
  const value = fmtDur(s);
  if (browserCopy().locale !== 'zh') return value;
  return value.replace('h', ' 小时').replace('m', ' 分钟');
};
const localDurationText = (value: string): string => {
  if (browserCopy().locale !== 'zh') return value;
  return value
    .replace(/\b(\d+(?:\.\d+)?) ms\b/g, '$1 毫秒')
    .replace(/\b(\d+(?:\.\d+)?) s\b/g, '$1 秒')
    .replace(/\b(\d+(?:\.\d+)?) min\b/g, '$1 分钟')
    .replace(/\b(\d+(?:\.\d+)?) h\b/g, '$1 小时');
};
const localValue = (value: string): string => {
  if (browserCopy().locale !== 'zh') return value;
  return localDurationText(value)
    .replace(/\b(\d+(?:\.\d+)?) steps\b/g, '$1 步')
    .replace(/\b(\d+(?:\.\d+)?)m\b/g, '$1 分钟')
    .replace(/\btok\/s\b/g, 'token/秒');
};
const localFactText = (text: string): string => {
  if (browserCopy().locale !== 'zh') return text;
  let out = text;
  for (const [from, to] of rt().facts.replacements) out = out.split(from).join(to);
  return out
    .replace(/\b(\d+)h\b/g, '$1 小时')
    .replace(/\b(\d+)m\b/g, '$1 分')
    .replace(/\b(\d+)\s*min\b/g, '$1 分钟')
    .replace(/\b(\d+)s\b/g, '$1 秒')
    .replace(/(\d{1,2}) 月 (\d{1,2}) (\d{4})/g, '$3 年 $1 月 $2 日')
    .replace(/\btok\b/g, 'token');
};

// ---- KPIs ----
function renderKpis(): void {
  const t = rt().kpi;
  const days = Math.round((a.kpis.lastTsUs - a.kpis.firstTsUs) / 1e6 / 86400);
  set('sessions', intComma(a.kpis.sessions));
  set('rounds', intComma(a.kpis.rounds));
  set('cost', fmtUsd(a.kpis.totalCostUsd));
  set('saved', fmtUsd(a.kpis.cacheSavingsUsd));
  set('input', compact(a.kpis.inputTokens));
  set('cached', compact(a.kpis.cachedInputTokens));
  set('uncached', compact(a.kpis.uncachedInputTokens));
  set('output', compact(a.kpis.outputTokens));
  foot('span', template(t.activeSpan, { active: a.kpis.activeDays, days }));
  foot('users', template(t.users, { count: a.kpis.users }));
  foot('costperday', a.kpis.activeDays ? template(t.costPerActiveDay, { cost: fmtUsd(a.kpis.totalCostUsd / a.kpis.activeDays) }) : '—');
  const cachedPct = a.kpis.inputTokens ? Math.round((a.kpis.cachedInputTokens / a.kpis.inputTokens) * 100) : 0;
  foot('cachedpct', template(t.servedFromCache, { pct: cachedPct }));
  foot('outputperstep', a.kpis.rounds ? template(t.avgPerStep, { value: compact(Math.round(a.kpis.outputTokens / a.kpis.rounds)) }) : '—');
}

// ---- providers split (read precomputed payload.providers; no frontend aggregation) ----
const provColor = (p: string) => (p === 'claude' ? 'var(--terra)' : p === 'codex' ? 'var(--sage)' : 'var(--gold)');
function renderProviders(): void {
  const t = rt().providers;
  const barEl = $('prov-bar');
  const legEl = $('prov-legend');
  barEl.innerHTML = '';
  legEl.innerHTML = '';
  for (const p of a.providers) {
    const seg = document.createElement('div');
    seg.className = 'prov-seg';
    seg.style.flex = `${p.stepShare} 1 0`;
    seg.style.background = provColor(p.provider);
    seg.title = template(t.stepTitle, { provider: p.provider, pct: Math.round(p.stepShare * 100) });
    seg.innerHTML = `<span class="pl">${esc(p.provider)}</span><span class="pct">${Math.round(p.stepShare * 100)}%</span>`;
    barEl.appendChild(seg);

    const item = document.createElement('div');
    item.className = 'prov-item';
    item.innerHTML =
      `<span class="prov-dot" style="background:${provColor(p.provider)}"></span>` +
      `<span class="pn">${esc(p.provider)}</span>` +
      `<span class="pm">${esc(template(t.legend, {
        stepPct: Math.round(p.stepShare * 100),
        costPct: Math.round(p.costShare * 100),
        steps: intComma(p.rounds),
        cost: fmtUsd(p.cost),
      }))}</span>`;
    legEl.appendChild(item);
  }
}

// ---- charts ----
function renderTopCharts(): void {
  renderCalendar($('c-calendar'), a.perDay);
  renderHourWeekday($('c-heat'), a.hourWeekday);
  renderCostByModel($('c-cost'), a.cost.byModel);
  $('cost-asof').textContent = template(rt().providers.pricesAsOf, { date: PRICING_AS_OF, steps: intComma(a.cost.unpricedRounds) });
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
    `<div class="stat-row"><span class="sk">${esc(L(k))}</span><span class="sv">${esc(localValue(v))}</span></div>`;
  const subHtml = (sub: StatSub) =>
    (sub.label ? `<div class="stat-sub"><span class="ssl">${esc(L(sub.label))}</span><span class="ssr"></span></div>` : '') +
    sub.rows.map(rowHtml).join('');
  statCards = buildStatGroups().map((g, idx) => {
    const card = document.createElement('div');
    card.className = 'stat-group';
    const count = g.subs.reduce((n, sub) => n + sub.rows.length, 0);
    card.innerHTML = `<h4>${esc(L(g.title))}<span class="gcount">${count}</span></h4>` + g.subs.map(subHtml).join('');
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
  const facts = rt().facts;
  const linked = !!f.sessionId;
  const card = document.createElement(linked ? 'button' : 'div');
  card.className = 'fact';
  card.dataset.dim = f.dimension;
  card.dataset.link = linked ? '1' : '0';
  if (linked) card.dataset.session = f.sessionId!;
  card.innerHTML =
    `<div class="fact-top"><span class="fact-chip">${esc((facts.dimensions as Record<string, string>)[f.dimension] ?? f.dimension)}</span>` +
    `${linked ? '<span class="fact-go" aria-hidden="true">→</span>' : ''}</div>` +
    `<div class="fact-title">${esc(facts.titles[f.id] ?? f.title)}</div>` +
    `<div class="fact-val">${esc(localFactText(f.value))}</div>` +
    (f.detail ? `<div class="fact-detail">${esc(localFactText(f.detail))}</div>` : '');
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
    head.innerHTML = `<span class="fact-cat-label">${esc((rt().facts.categories as Record<string, string>)[cat.key] ?? cat.label)}</span><span class="fact-cat-count">${items.length}</span><span class="fact-cat-rule"></span>`;
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
// Thread population: all | main | subagent | guardian | unknown. Hidden when the trace predates the fields.
let popFilter = 'all';
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
    .filter((s) => popFilter === 'all' || (popFilter === 'unknown' ? s.threadKind == null : s.threadKind === popFilter))
    .filter((s) => !q || s.sessionId.toLowerCase().includes(q) || s.primaryModel.toLowerCase().includes(q))
    .sort(sorters[sortKey] ?? sorters.rounds);
}

// local-date helpers for grouping the picker by day (2-line marker: weekday over month-day)
const dayOf = (tsUs: number) => {
  const d = new Date(tsUs / 1000);
  const locale = rt().dateLocale;
  return {
    key: `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`,
    ts: d.getTime(),
    wd: d.toLocaleDateString(locale, { weekday: 'short' }),
    md: d.toLocaleDateString(locale, { month: 'short', day: 'numeric' }),
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
    `<span class="pill ${s.provider === 'codex' ? 'codex' : ''}">${esc(s.provider)}</span></div>` +
    // the subagent / auto-review pill gets a line of its own, so the title keeps the width it has on main
    (s.threadKind === 'subagent' || s.threadKind === 'guardian'
      ? `<div class="sr-kind"><span class="pill sub" title="${esc(s.agentRole ?? '')}">${esc(s.threadKind === 'guardian' ? rt().sessions.guardianPill : rt().sessions.subagentPill)}</span></div>`
      : '') +
    `<div class="sr-meta">${esc(template(rt().sessions.meta, { steps: intComma(s.rounds), duration: localDur(s.durationS), cost: fmtUsd(s.costUsd) }))}</div>` +
    (s.childSessions
      ? `<div class="sr-meta">${esc(s.childSessions === 1 ? rt().sessions.childCountOne : template(rt().sessions.childCount, { count: intComma(s.childSessions) }))}</div>`
      : '');
  row.addEventListener('click', () => selectSession(s.sessionId, { scroll: true }));
  return row;
}

function renderSessionList(): void {
  const rows = visibleSessions();
  listEl.innerHTML = '';
  if (!rows.length) {
    listEl.innerHTML = `<div class="sess-empty">${esc(rt().sessions.noMatch)}</div>`;
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
      head.innerHTML = `<span class="sd-wd">${esc(g.wd)}</span><span class="sd-md">${esc(g.md)}</span><span class="sd-count">${esc(n === 1 ? rt().sessions.groupCountOne : template(rt().sessions.groupCountMany, { count: n }))}</span>`;
      const cards = document.createElement('div');
      cards.className = 'sess-cards';
      g.items.forEach((s) => cards.appendChild(sessionCard(s)));
      section.appendChild(head);
      section.appendChild(cards);
      listEl.appendChild(section);
    }
  }
  $('sess-count').textContent = template(rt().sessions.count, { shown: rows.length, total: a.sessions.length });
}

// round inspector (filled when a round is clicked in the timeline)
function roundHint(): void {
  roundDetailEl.innerHTML = `<div class="rd-hint">${esc(rt().sessions.clickHint)}</div>`;
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
  const tr = rt().sessions;
  const rs = rt().roundStats;
  const msText = (v: number) => (browserCopy().locale === 'zh' ? `${v} 毫秒` : `${v}ms`);
  const tools = r.tools.length
    ? r.tools.map((t) => `<span class="rd-tool ${t.error ? 'err' : ''}">${esc(t.name)} <b>${esc(msText(t.ms))}</b>${t.error ? ` · ${esc(tr.err)}` : ''}</span>`).join('')
    : `<span class="rd-empty">${esc(tr.noTools)}</span>`;
  roundDetailEl.innerHTML =
    '<div class="rd-card">' +
    `<div class="rd-head"><span class="rd-seq">${esc(template(tr.round, { seq: r.seq }))}</span>` +
    `<span class="rd-tag ${r.isUserInput ? 'user' : 'tool'}">${esc(r.isUserInput ? tr.userInitiated : tr.toolStep)}</span>` +
    `<span class="rd-time">${esc(template(tr.elapsed, { elapsed: localDurationText(dur(elapsed)) }))}${prev ? esc(template(tr.sincePrev, { delta: localDurationText(dur(sincePrev)) })) : ''}</span></div>` +
    '<div class="rd-stats">' +
    stat(esc(rs.cachedInput), tok(r.prefixTokens)) +
    stat(esc(rs.freshInput), tok(r.appendTokens)) +
    stat(esc(rs.output), tok(r.outputTokens)) +
    stat(esc(rs.reasoning), tok(r.reasoningTokens)) +
    stat(esc(rs.cacheHit), pct(hit)) +
    stat(esc(rs.inference), localDurationText(`${r.inferenceS} s`)) +
    stat(esc(rs.decode), localValue(`${Math.round(decode)} tok/s`)) +
    stat(esc(rs.toolCalls), String(r.toolCount)) +
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
  if (raw.inputKind === 'user') html += rawRow(rt().sessions.rawInput, rawPre(raw.input), true);
  html += rawRow(rt().sessions.rawOutput, rawPre(raw.output), true); // output stays open too
  if (raw.tools && raw.tools.length) {
    const items = raw.tools
      .map(
        (t) =>
          `<div class="raw-tool ${t.error ? 'err' : ''}"><div class="raw-tool-h">${esc(t.name)}${t.error ? ` · ${esc(rt().sessions.rawError)}` : ''}</div>` +
          rawPre(t.input, 'sm') +
          rawPre(t.result, 'sm res') +
          '</div>',
      )
      .join('');
    html += rawRow(template(rt().sessions.rawTools, { count: raw.tools.length }), `<div class="raw-tools">${items}</div>`);
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
  $('sess-title').textContent = s.title ?? template(rt().sessions.titleFallback, { id });
  $('sess-meta').textContent =
    template(rt().sessions.detailMeta, {
      prefix: s.title ? `${id} · ` : '',
      model: s.primaryModel,
      steps: intComma(s.rounds),
      duration: localDur(s.durationS),
      tools: s.toolCalls,
      errors: s.errors,
      cost: fmtUsd(s.costUsd),
    });
  roundDetailEl.innerHTML = `<div class="rd-hint">${esc(rt().sessions.loadingTimeline)}</div>`;
  $('sess-topology').innerHTML = '';
  if (opts?.scroll) document.querySelector('.sess-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' });

  const detail = await getDetail(id);
  if (myToken !== selectToken) return; // a newer selection took over while we awaited
  const toolsEl = $('sess-tools');
  if (!detail) {
    roundDetailEl.innerHTML = `<div class="rd-hint">${esc(rt().sessions.timelineError)}</div>`;
    toolsEl.innerHTML = '';
    return;
  }
  renderSessionTimeline($('c-session'), detail, (round, i) => renderRoundDetail(round, i, detail));
  renderTopology(detail, s);
  roundHint();
  toolsEl.innerHTML = '';
  for (const t of detail.tools) {
    const chip = document.createElement('span');
    chip.className = 'tool';
    chip.textContent = template(rt().sessions.toolChip, { name: t.name, count: intComma(t.count), p50: t.p50Ms, errors: t.errors });
    toolsEl.appendChild(chip);
  }
}

// ---- subagent topology of the opened session ----
// A heavy parent can hold dozens of workflows and hundreds of agents: show the first groups and chips,
// expand the rest on request, so the timeline below stays in reach.
const TOPO_CHIPS_PER_GROUP = 24;
const TOPO_GROUPS_SHOWN = 6;
let topoShown: {
  detail: SessionDetail;
  s: SessionRow;
  allGroups: boolean;
  fullGroups: Set<string>;
  openGroups: Set<string> | null; // groups open before the last expand; null = only the largest group opens
} | null = null;

function renderTopology(detail: SessionDetail, s: SessionRow): void {
  if (topoShown?.s.sessionId === s.sessionId) Object.assign(topoShown, { detail, s });
  else topoShown = { detail, s, allGroups: false, fullGroups: new Set(), openGroups: null };
  const { allGroups, fullGroups, openGroups } = topoShown;
  const el = $('sess-topology');
  const c = rt().sessions;
  const topo = detail.topology;
  if (!topo || !topo.known) {
    // An untagged session in a trace that has tags elsewhere: say so instead of implying main-thread
    // work. A trace with no tags at all hides the thread filter and shows nothing here (as before).
    el.innerHTML = s.threadKind == null && hierarchyKnown ? `<div class="tp-line">${esc(c.hierarchyUnknown)}</div>` : '';
    return;
  }
  const parts: string[] = [];
  if (topo.parentSessionId) {
    // Offer the link only when the parent is part of this trace (a child can be loaded on its own).
    const parentHere = a.sessions.some((x) => x.sessionId === topo.parentSessionId);
    const review = topo.threadKind === 'guardian';
    const lead = review ? c.topoReviewOf : template(c.topoParent, { role: topo.agentRole ?? c.roleUnknown });
    parts.push(
      `<div class="tp-line">${esc(lead)} ` +
        (parentHere
          ? `<button class="tp-link" data-goto="${esc(topo.parentSessionId)}">` +
            `${esc(review ? c.topoOpenReviewed : c.topoOpenParent)}</button>`
          : esc(review ? c.topoReviewedMissing : c.topoParentMissing)) +
        `</div>`,
    );
  }
  if (topo.children.length) {
    const n = topo.children.length;
    parts.push(`<div class="tp-line">${esc(n === 1 ? c.topoChildrenOne : template(c.topoChildren, { count: intComma(n) }))}</div>`);
    // Workflow ids are pseudonyms; name workflows by the order they started (children are start-sorted).
    const ordinal = new Map<string, number>();
    // Children by workflow (null = launched directly), built once instead of filtering per group.
    const kidsByWorkflow = new Map<string | null, SessionTopologyChild[]>();
    for (const k of topo.children) {
      if (k.workflowId && !ordinal.has(k.workflowId)) ordinal.set(k.workflowId, ordinal.size + 1);
      const list = kidsByWorkflow.get(k.workflowId);
      if (list) list.push(k);
      else kidsByWorkflow.set(k.workflowId, [k]);
    }
    const groups = allGroups ? topo.workflows : topo.workflows.slice(0, TOPO_GROUPS_SHOWN);
    groups.forEach((w, gi) => {
      const group = w.workflowId ? `wf:${w.workflowId}` : 'direct';
      const kids = kidsByWorkflow.get(w.workflowId) ?? [];
      const roles = new Set(kids.map((k) => k.agentRole ?? c.roleUnknown));
      const sharedRole = roles.size === 1 ? [...roles][0] : null;
      const counts = {
        agents: w.agents === 1 ? c.agentsOne : template(c.agentsMany, { count: intComma(w.agents) }),
        steps: intComma(w.rounds),
      };
      const head = w.workflowId
        ? template(c.topoWorkflow, { n: ordinal.get(w.workflowId) ?? '?', ...counts })
        : template(c.topoDirect, counts);
      const role = sharedRole ? ` · ${sharedRole}` : '';
      const full = fullGroups.has(group);
      const chips = (full ? kids : kids.slice(0, TOPO_CHIPS_PER_GROUP)).map((k) => {
        const hit = k.prefixShare == null ? '—' : `${Math.round(k.prefixShare * 100)}%`;
        const label = sharedRole
          ? template(c.topoAgentShort, { steps: intComma(k.rounds), hit })
          : template(c.topoAgent, { role: k.agentRole ?? c.roleUnknown, steps: intComma(k.rounds), hit });
        return `<button class="tp-chip" data-goto="${esc(k.sessionId)}" title="${esc(k.sessionId)}">${esc(label)}</button>`;
      });
      if (!full && kids.length > TOPO_CHIPS_PER_GROUP) {
        chips.push(
          `<button class="tp-more" data-expand-group="${esc(group)}">` +
            `${esc(template(c.topoMore, { count: intComma(kids.length - TOPO_CHIPS_PER_GROUP) }))}</button>`,
        );
      }
      // Largest group open at first (and any group the reader expanded); the rest collapsed to one
      // summary line. After that, groups stay as the reader left them.
      const open = full || (openGroups ? openGroups.has(group) : gi === 0);
      parts.push(
        `<details class="tp-group" data-group="${esc(group)}"${open ? ' open' : ''}>` +
          `<summary class="tp-head" title="${esc(w.workflowId ?? '')}">` +
          `${esc(head)}${esc(role)}</summary><div class="tp-chips">${chips.join('')}</div></details>`,
      );
    });
    if (groups.length < topo.workflows.length) {
      parts.push(
        `<button class="tp-more" data-expand-groups="1">` +
          `${esc(template(c.topoAllWorkflows, { count: intComma(topo.workflows.length) }))}</button>`,
      );
    }
  } else if (topo.threadKind === 'main' && !hierarchyMixed) {
    // In a trace that also holds untagged sessions, a child may be among them (not linked): say nothing.
    parts.push(`<div class="tp-line">${esc(c.topoNone)}</div>`);
  } else if (topo.threadKind === 'guardian' && !topo.parentSessionId) {
    parts.push(`<div class="tp-line">${esc(c.topoGuardian)}</div>`);
  }
  const reviews = topo.reviews ?? [];
  if (reviews.length) {
    // Automatic reviews are not delegated work: their own group, collapsed when subagent groups exist.
    const full = fullGroups.has('reviews');
    const chips = (full ? reviews : reviews.slice(0, TOPO_CHIPS_PER_GROUP)).map((k) => {
      const hit = k.prefixShare == null ? '—' : `${Math.round(k.prefixShare * 100)}%`;
      const label = template(c.topoAgentShort, { steps: intComma(k.rounds), hit });
      return `<button class="tp-chip" data-goto="${esc(k.sessionId)}" title="${esc(k.sessionId)}">${esc(label)}</button>`;
    });
    if (!full && reviews.length > TOPO_CHIPS_PER_GROUP) {
      chips.push(
        `<button class="tp-more" data-expand-group="reviews">` +
          `${esc(template(c.topoMore, { count: intComma(reviews.length - TOPO_CHIPS_PER_GROUP) }))}</button>`,
      );
    }
    const head = template(c.topoReviews, {
      count: intComma(reviews.length),
      steps: intComma(reviews.reduce((sum, k) => sum + k.rounds, 0)),
    });
    const open = full || (openGroups ? openGroups.has('reviews') : !topo.children.length);
    parts.push(
      `<details class="tp-group" data-group="reviews"${open ? ' open' : ''}>` +
        `<summary class="tp-head">${esc(head)}</summary><div class="tp-chips">${chips.join('')}</div></details>`,
    );
  }
  el.innerHTML = parts.join('');
}

// Whether the loaded trace carries thread tags at all (decided per payload in configurePopulation).
let hierarchyKnown: boolean | null = null;
// Whether the trace mixes tagged and untagged sessions (e.g. new rows appended to an older trace file).
let hierarchyMixed = false;

function configurePopulation(): void {
  const seg = $('sess-population');
  hierarchyKnown = a.sessions.some((s) => s.threadKind != null);
  const hasGuardian = a.sessions.some((s) => s.threadKind === 'guardian');
  // A trace mixing tagged and untagged sessions: untagged ones get their own button, not "main".
  const hasUnknown = hierarchyKnown && a.sessions.some((s) => s.threadKind == null);
  hierarchyMixed = hasUnknown;
  // A trace with tagged subagents or automatic reviews can hold thousands of such sessions that would bury
  // the sessions a person started; open on the main thread (each parent's subagents and reviews are one
  // click away in its topology).
  // Only when there is a main thread to open on: a child uploaded on its own would otherwise show nothing.
  // Not when some sessions are untagged: "main" would hide them, and they are often most of the history.
  const hasSubagents = a.sessions.some((s) => s.threadKind === 'subagent');
  const hasMain = a.sessions.some((s) => s.threadKind === 'main');
  popFilter = (hasSubagents || hasGuardian) && hasMain && !hasUnknown ? 'main' : 'all';
  // A trace with no thread tags (collected before they existed) has nothing to filter by: the control
  // is hidden and the list looks as it always did.
  seg.hidden = !hierarchyKnown;
  seg.querySelectorAll('button').forEach((b) => {
    const pop = (b as HTMLElement).dataset.pop;
    b.classList.toggle('active', pop === popFilter);
    b.setAttribute('aria-pressed', String(pop === popFilter));
    if (pop === 'main') (b as HTMLButtonElement).hidden = !hasMain;
    if (pop === 'subagent') (b as HTMLButtonElement).hidden = !hasSubagents;
    if (pop === 'guardian') (b as HTMLButtonElement).hidden = !hasGuardian;
    if (pop === 'unknown') (b as HTMLButtonElement).hidden = !hasUnknown;
  });
}

// ---- distributions ----
function renderDistributions(): void {
  distEl.innerHTML = '';
  for (const [key, spec] of Object.entries(a.distributions)) {
    const card = document.createElement('div');
    card.className = 'dist-card';
    const h = document.createElement('h4');
    h.textContent = (rt().distributions as Record<string, string>)[key] ?? key;
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
  topoShown = null;
  selectedId = null;
  // reset the session toolbar to defaults (the thread filter is set per trace in configurePopulation)
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
  $('sess-population').addEventListener('click', (e) => {
    if (!loaded) return;
    const btn = (e.target as HTMLElement).closest('button[data-pop]') as HTMLButtonElement | null;
    if (!btn) return;
    popFilter = btn.dataset.pop!;
    $('sess-population').querySelectorAll('button').forEach((b) => {
      b.classList.toggle('active', b === btn);
      b.setAttribute('aria-pressed', String(b === btn));
    });
    renderSessionList();
  });
  $('sess-topology').addEventListener('click', (e) => {
    if (!loaded) return;
    const target = e.target as HTMLElement;
    const more = target.closest('[data-expand-group], [data-expand-groups]') as HTMLElement | null;
    if (more && topoShown) {
      if (more.dataset.expandGroups) topoShown.allGroups = true;
      if (more.dataset.expandGroup) topoShown.fullGroups.add(more.dataset.expandGroup);
      // Keep the groups the reader opened or closed; the re-render replaces the whole block.
      const topo = $('sess-topology');
      topoShown.openGroups = new Set(
        [...topo.querySelectorAll<HTMLElement>('details.tp-group[open]')].map((d) => d.dataset.group ?? ''),
      );
      renderTopology(topoShown.detail, topoShown.s);
      // The clicked button is gone after the re-render: move focus to the first newly shown item.
      const next = more.dataset.expandGroup
        ? topo.querySelectorAll<HTMLElement>(
            `details.tp-group[data-group="${CSS.escape(more.dataset.expandGroup)}"] .tp-chip`,
          )[TOPO_CHIPS_PER_GROUP]
        : topo.querySelectorAll<HTMLElement>('details.tp-group > summary')[TOPO_GROUPS_SHOWN];
      next?.focus();
      return;
    }
    const btn = target.closest('[data-goto]') as HTMLElement | null;
    if (btn?.dataset.goto) void selectSession(btn.dataset.goto, { scroll: true });
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
  configurePopulation();
  renderAll();
}
