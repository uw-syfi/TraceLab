// Provider-comparison view model, derived entirely from summary.claude / summary.codex.
// Keeps Compare.astro declarative: it maps over groups/segments produced here.

import type { Summary, ProviderSummary } from './types';
import { compact, fmtRange, fmtSeconds, intComma, pct, pct1 } from './format';

/** Upcoming providers shown in the crossfade ticker (not yet in the pool). */
export const COMING_SOON = ['DeepSeek', 'Moonshot', 'GLM', 'Qwen'];

export interface ProviderHead {
  name: string;
  steps: string; // formatted integer
  sharePct: number; // integer percent of all agent steps
}

export interface CompareCell {
  text: string;
  na?: boolean;
}

export interface CompareRow {
  metric: string;
  dir?: 'up' | 'down'; // directional metric — higher/lower is better
  claude: CompareCell;
  codex: CompareCell;
  win?: 'claude' | 'codex'; // judged winner (directional rows only)
  profile?: boolean; // descriptive usage figure — never ranked
}

export interface CompareSubsection {
  kind: 'subsection';
  title: string;
  note?: string;
}

export type CompareEntry = CompareRow | CompareSubsection;

export interface CompareGroup {
  name: string;
  note: string;
  rows: CompareEntry[];
}

export interface ModelSeg {
  label: string;
  count: number;
  pct: number; // 0..100
}

export interface CompareData {
  claudeHead: ProviderHead;
  codexHead: ProviderHead;
  groups: CompareGroup[];
  claudeModels: ModelSeg[];
  codexModels: ModelSeg[];
}

const NA: CompareCell = { text: '—', na: true };
const cell = (text: string): CompareCell => ({ text });
const sub = (title: string, note?: string): CompareSubsection => ({ kind: 'subsection', title, note });
const hasText = (text: string | null | undefined): text is string => text != null && text !== '';

/** Tidy a raw model id into a human label: claude-haiku-4-5-20251001 -> "Haiku 4.5". */
export function prettyModel(raw: string): string {
  let s = raw.replace(/-\d{8}$/, ''); // drop a trailing yyyymmdd snapshot suffix
  if (s.startsWith('claude-')) {
    const [tier, ...ver] = s.slice('claude-'.length).split('-');
    const Tier = tier.charAt(0).toUpperCase() + tier.slice(1);
    return ver.length ? `${Tier} ${ver.join('.')}` : Tier;
  }
  return s; // gpt-5.x ids are already readable
}

/** Top-N models by agent-step count, with the remainder folded into a trailing "other" segment. */
export function modelSegments(rbm: Record<string, number>, topN = 3): ModelSeg[] {
  const entries = Object.entries(rbm).sort((a, b) => b[1] - a[1]);
  const total = entries.reduce((sum, [, v]) => sum + v, 0) || 1;
  const segs: ModelSeg[] = entries.slice(0, topN).map(([name, count]) => ({
    label: prettyModel(name),
    count,
    pct: (count / total) * 100,
  }));
  const rest = entries.slice(topN).reduce((sum, [, v]) => sum + v, 0);
  if (rest > 0) segs.push({ label: 'other', count: rest, pct: (rest / total) * 100 });
  return segs;
}

type P = ProviderSummary | undefined;

function head(name: string, p: P, total: number): ProviderHead {
  if (!p) return { name, steps: '—', sharePct: 0 };
  return {
    name,
    steps: intComma(p.scope.llm_rounds_total),
    sharePct: total ? pct(p.scope.llm_rounds_total / total) : 0,
  };
}

function avgReasoning(p: P): CompareCell {
  if (!p || p.tokens.output.rounds_with_positive_reasoning_output_tokens <= 0) return NA;
  const subset = p.tokens.output.reasoning_output_tokens_subset;
  return cell(intComma(subset / p.tokens.output.rounds_with_positive_reasoning_output_tokens));
}

function fmtHours(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const h = seconds / 3600;
  if (h >= 1000) return `${(h / 1000).toFixed(h >= 10000 ? 0 : 1)}K h`;
  if (h >= 10) return `${intComma(h)} h`;
  return `${h.toFixed(1)} h`;
}

function fmtTokens(n: number): string {
  return `${intComma(n)} tok`;
}

function fmtTokenTriplet(
  average: number | null | undefined,
  p50: number | null | undefined,
  p90: number | null | undefined,
): string {
  const f = (v: number | null | undefined) => (v == null || !Number.isFinite(v) ? '—' : intComma(v));
  return `${f(average)} / ${f(p50)} / ${f(p90)} tok`;
}

function fmtSecondsTriplet(
  average: number | null | undefined,
  p50: number | null | undefined,
  p90: number | null | undefined,
): string {
  const values = [average, p50, p90].filter((v): v is number => v != null && Number.isFinite(v));
  if (!values.length) return '—';
  const useMs = Math.max(...values) < 1;
  const f = (v: number | null | undefined) => {
    if (v == null || !Number.isFinite(v)) return '—';
    return useMs ? String(Math.round(v * 1000)) : v.toFixed(1);
  };
  return `${f(average)} / ${f(p50)} / ${f(p90)} ${useMs ? 'ms' : 's'}`;
}

function fmtTokenMass(n: number): string {
  return `${compact(n)} tok`;
}

function fmtCountPct(count: number, total: number): string {
  return total > 0 ? `${intComma(count)} (${pct1(count / total)})` : intComma(count);
}

function fmtTokenRate(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? '—' : `${v.toFixed(1)} tok/s`;
}

function comparedRounds(p: ProviderSummary): string {
  return `${intComma(p.scope.llm_rounds_total)} steps`;
}

function modelCount(p: ProviderSummary): string {
  return intComma(Object.keys(p.rounds_by_model).length);
}

function topModel(p: ProviderSummary): string | null {
  const top = Object.entries(p.rounds_by_model).sort((a, b) => b[1] - a[1])[0];
  return top ? `${prettyModel(top[0])} (${pct1(top[1] / p.scope.llm_rounds_total)})` : null;
}

function observedReasoningTokens(p: ProviderSummary): string | null {
  return p.tokens.output.rounds_with_positive_reasoning_output_tokens > 0
    ? fmtTokenMass(p.tokens.output.reasoning_output_tokens_subset)
    : null;
}

type StartSlice = 'user' | 'tool' | 'all';

function appendTokens(p: ProviderSummary, slice: StartSlice): number {
  if (slice === 'user') return p.tokens.input.total_new_input_tokens_when_started_with_user_message;
  if (slice === 'tool') return p.tokens.input.total_new_input_tokens_when_started_with_tool_result;
  return (
    p.tokens.input.total_new_input_tokens_when_started_with_user_message
    + p.tokens.input.total_new_input_tokens_when_started_with_tool_result
  );
}

function contextIncreaseTokens(p: ProviderSummary, slice: StartSlice): number {
  if (slice === 'user') {
    return p.tokens.input.total_input_growth_when_started_with_user_message.total_context_increase_tokens;
  }
  if (slice === 'tool') {
    return p.tokens.input.total_input_growth_when_started_with_tool_result.total_context_increase_tokens;
  }
  return p.tokens.input.total_context_increase_tokens;
}

function contextShareOfAppend(p: ProviderSummary, slice: StartSlice): number | null {
  const append = appendTokens(p, slice);
  if (append <= 0) return null;
  return contextIncreaseTokens(p, slice) / append;
}

/** Pick the winner of a directional metric; ties (within tolerance) are unjudged. */
function judge(
  dir: 'up' | 'down',
  c: number,
  x: number,
  eps = 1e-9,
): 'claude' | 'codex' | undefined {
  if (Math.abs(c - x) <= eps) return undefined;
  const claudeWins = dir === 'up' ? c > x : c < x;
  return claudeWins ? 'claude' : 'codex';
}

export function buildCompare(s: Summary): CompareData {
  const c = s.claude;
  const x = s.codex;
  const total = (c?.scope.llm_rounds_total ?? 0) + (x?.scope.llm_rounds_total ?? 0);

  // A descriptive (never-ranked) row, pulling the same field from each present provider.
  const prof = (metric: string, get: (p: ProviderSummary) => string | null | undefined): CompareRow => {
    const cv = c ? get(c) : null;
    const xv = x ? get(x) : null;
    return {
      metric,
      profile: true,
      claude: hasText(cv) ? cell(cv) : NA,
      codex: hasText(xv) ? cell(xv) : NA,
    };
  };

  // A directional row: judged only when both providers are present.
  const perf = (
    metric: string,
    dir: 'up' | 'down',
    get: (p: ProviderSummary) => number | null | undefined,
    fmt: (v: number) => string,
  ): CompareRow => {
    const cv = c ? get(c) : null;
    const xv = x ? get(x) : null;
    const claude = cv != null && Number.isFinite(cv) ? cell(fmt(cv)) : NA;
    const codex = xv != null && Number.isFinite(xv) ? cell(fmt(xv)) : NA;
    return {
      metric,
      dir,
      claude,
      codex,
      win: !claude.na && !codex.na ? judge(dir, cv as number, xv as number) : undefined,
    };
  };

  const traceFacts: CompareGroup = {
    name: 'Trace facts',
    note: 'sessions, requests & agent-step coverage',
    rows: [
      sub('Coverage'),
      prof('Agent steps', comparedRounds),
      prof('Sessions', (p) => intComma(p.scope.total_sessions)),
      prof('Distinct users', (p) => intComma(p.scope.distinct_users)),
      prof('Collection window', (p) => fmtRange(p.scope.earliest_observed_timestamp, p.scope.latest_observed_timestamp)),
      prof('Requests', (p) => intComma(p.scope.rounds_with_visible_user_message)),
      prof('Tool-triggered steps', (p) => fmtCountPct(p.scope.rounds_started_from_tool_result, p.scope.llm_rounds_total)),
      sub('Models'),
      prof('Models represented', modelCount),
      prof('Top model', topModel),
    ],
  };

  const llmGeneration: CompareGroup = {
    name: 'LLM generation',
    note: 'tokens and timing per agent step',
    rows: [
      sub('Token distributions'),
      prof('Total input tokens', (p) => fmtTokenMass(p.tokens.input.total_input_tokens)),
      prof('Cached-read input tokens', (p) => fmtTokenMass(p.tokens.input.cached_read_input_tokens)),
      prof('Append input tokens', (p) => fmtTokenMass(p.tokens.input.new_input_tokens)),
      prof('Avg total input / agent step', (p) => fmtTokens(p.tokens.input.average_total_input_tokens_per_round)),
      prof('Avg cached-read input / agent step', (p) => fmtTokens(p.tokens.input.average_cached_read_input_tokens_per_round)),
      prof('Avg append input / agent step', (p) => fmtTokens(p.tokens.input.average_new_input_tokens_per_round)),
      sub('Input by step trigger'),
      prof('User-initiated avg total input', (p) => fmtTokens(p.tokens.input.average_total_input_tokens_when_started_with_user_message)),
      prof('User-initiated avg append input', (p) => fmtTokens(p.tokens.input.average_new_input_tokens_when_started_with_user_message)),
      prof('Tool-triggered avg total input', (p) => fmtTokens(p.tokens.input.average_total_input_tokens_when_started_with_tool_result)),
      prof('Tool-triggered avg append input', (p) => fmtTokens(p.tokens.input.average_new_input_tokens_when_started_with_tool_result)),
      sub('Output tokens'),
      prof('Total output tokens', (p) => fmtTokenMass(p.tokens.output.total_output_tokens_including_reasoning)),
      prof('Avg output / agent step', (p) => fmtTokens(p.tokens.output.average_output_tokens_including_reasoning_per_round)),
      prof('Reasoning tokens', observedReasoningTokens),
      { metric: 'Avg reasoning / reasoning step', profile: true, claude: avgReasoning(c), codex: avgReasoning(x) },
      sub('Timing'),
      perf('Generation time p50', 'down', (p) => p.generation_timing.p50_observable_generation_time_seconds, fmtSeconds),
      perf('Generation time p90', 'down', (p) => p.generation_timing.p90_observable_generation_time_seconds, fmtSeconds),
      prof('Total generation time', (p) => fmtHours(p.generation_timing.total_observable_generation_time_seconds)),
      perf(
        'Output decode throughput',
        'up',
        (p) => p.generation_timing.average_normalized_decoding_speed_tokens_per_second,
        fmtTokenRate,
      ),
      perf(
        'Post-reasoning decode throughput',
        'up',
        (p) => p.generation_timing.post_reasoning_tpot_estimate.average_decode_speed_tokens_per_second,
        fmtTokenRate,
      ),
      perf(
        'Estimated TTFT from reasoning tokens',
        'down',
        (p) => p.generation_timing.estimated_ttft_from_exact_reasoning_tokens.estimated_average_seconds,
        fmtSeconds,
      ),
    ],
  };

  const toolCalls: CompareGroup = {
    name: 'Tool calls',
    note: 'tool volume and latency across agent steps',
    rows: [
      sub('Activity'),
      prof('Tool calls', (p) => intComma(p.tools.total_tool_calls)),
      prof('Agent steps with tool calls', (p) => fmtCountPct(p.tools.rounds_with_tool_calls, p.scope.llm_rounds_total)),
      prof('Tool calls / request', (p) =>
        p.tools.tool_calls_per_visible_user_message_round == null
          ? null
          : p.tools.tool_calls_per_visible_user_message_round.toFixed(1),
      ),
      sub('Timing'),
      perf('Tool latency p50', 'down', (p) => p.tools.effective_latency.p50_seconds, fmtSeconds),
      perf('Tool latency p90', 'down', (p) => p.tools.effective_latency.p90_seconds, fmtSeconds),
      prof('Total attributed tool time', (p) => fmtHours(p.tools.effective_latency.total_seconds)),
    ],
  };

  const prefixCache: CompareGroup = {
    name: 'Prefix cache',
    note: 'cache reuse by agent-step trigger',
    rows: [
      sub('Cache rates'),
      perf('Overall prefix hit rate', 'up', (p) => p.tokens.input.prefix_hit_rate, pct1),
      perf('User-initiated step hit rate', 'up', (p) => p.tokens.input.prefix_hit_rate_when_started_with_user_message, pct1),
      perf('Tool-triggered step hit rate', 'up', (p) => p.tokens.input.prefix_hit_rate_when_started_with_tool_result, pct1),
      sub('Append vs context growth'),
      prof('User-initiated append tokens', (p) => fmtTokenMass(appendTokens(p, 'user'))),
      prof('User-initiated context increase', (p) => fmtTokenMass(contextIncreaseTokens(p, 'user'))),
      perf('User-initiated context / append', 'up', (p) => contextShareOfAppend(p, 'user'), pct1),
      prof('Tool-triggered append tokens', (p) => fmtTokenMass(appendTokens(p, 'tool'))),
      prof('Tool-triggered context increase', (p) => fmtTokenMass(contextIncreaseTokens(p, 'tool'))),
      perf('Tool-triggered context / append', 'up', (p) => contextShareOfAppend(p, 'tool'), pct1),
      prof('All classified append tokens', (p) => fmtTokenMass(appendTokens(p, 'all'))),
      prof('All classified context increase', (p) => fmtTokenMass(contextIncreaseTokens(p, 'all'))),
      perf('All classified context / append', 'up', (p) => contextShareOfAppend(p, 'all'), pct1),
    ],
  };

  const sessionContext: CompareGroup = {
    name: 'Session context',
    note: 'context growth across sessions and agent steps',
    rows: [
      sub('Step-level context growth'),
      prof('Total context increase', (p) => fmtTokenMass(p.tokens.input.total_context_increase_tokens)),
      prof('User-initiated context increase avg / p50 / p90', (p) =>
        fmtTokenTriplet(
          p.tokens.input.average_user_context_delta_tokens,
          p.tokens.input.median_user_context_delta_tokens,
          p.tokens.input.p90_user_context_delta_tokens,
        ),
      ),
      prof('Tool-triggered context increase avg / p50 / p90', (p) =>
        fmtTokenTriplet(
          p.tokens.input.average_tool_result_context_delta_tokens,
          p.tokens.input.median_tool_result_context_delta_tokens,
          p.tokens.input.p90_tool_result_context_delta_tokens,
        ),
      ),
      sub('Growth / reductions'),
      prof('User-initiated growth share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_user_message.positive_growth_share)),
      prof('User-initiated reduction share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_user_message.negative_growth_share)),
      prof('User-initiated major compaction share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_user_message.major_compact_share)),
      prof('Tool-triggered growth share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_tool_result.positive_growth_share)),
      prof('Tool-triggered reduction share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_tool_result.negative_growth_share)),
      prof('Tool-triggered major compaction share', (p) => pct1(p.tokens.input.total_input_growth_when_started_with_tool_result.major_compact_share)),
    ],
  };

  const humanInLoop: CompareGroup = {
    name: 'Human in the loop',
    note: 'human waits before the next model response',
    rows: [
      sub('Timing'),
      prof('Total human wait time', (p) => fmtHours(p.generation_timing.total_waiting_for_human_input_seconds)),
      prof('Human wait avg / p50 / p90', (p) =>
        fmtSecondsTriplet(
          p.generation_timing.average_waiting_for_human_input_seconds,
          p.generation_timing.median_waiting_for_human_input_seconds,
          p.generation_timing.p90_waiting_for_human_input_seconds,
        ),
      ),
    ],
  };

  return {
    claudeHead: head('Claude', c, total),
    codexHead: head('Codex', x, total),
    groups: [traceFacts, llmGeneration, toolCalls, prefixCache, sessionContext, humanInLoop],
    claudeModels: c ? modelSegments(c.rounds_by_model) : [],
    codexModels: x ? modelSegments(x.rounds_by_model) : [],
  };
}
