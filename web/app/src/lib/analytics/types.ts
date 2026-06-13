// Data contract for the interactive analytics surface.
//
// This is the single shape shared by (a) the Phase-1 mock (lib/mock/analytics.ts) and (b) the
// real worker payload produced later by the Python `analytics` experiment. Charts and cards read
// ONLY these types, so swapping mock -> real is a data-source change, never a component change.
//
// Conventions:
//  - Timestamps are microseconds-since-epoch (BIGINT `epoch_us(...)` from DuckDB), as numbers.
//  - `day` strings are 'YYYY-MM-DD' in the viewer's LOCAL timezone (naive-UTC -> local at compute).
//  - Costs are USD. Token-derived costs use the prefix/append/output split (see lib/analytics/pricing.ts).

export type Provider = 'claude' | 'codex' | (string & {});

/** Top-of-page headline numbers. */
export interface Kpis {
  sessions: number;
  users: number;
  rounds: number;
  inputTokens: number; // total input = cached + uncached
  cachedInputTokens: number; // prefix tokens served from cache
  uncachedInputTokens: number; // freshly appended input tokens
  outputTokens: number;
  totalCostUsd: number;
  cacheSavingsUsd: number;
  /** Whole-trace span (for "you've used it X days"). */
  firstTsUs: number;
  lastTsUs: number;
  activeDays: number;
}

/** One calendar day of activity (local time). Drives the calendar heatmap + daily bars. */
export interface PerDay {
  day: string; // 'YYYY-MM-DD'
  rounds: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
}

/** One (weekday, hour) cell. weekday: 0=Mon .. 6=Sun. hour: 0..23. Drives the work-rhythm heatmap. */
export interface HourWeekday {
  weekday: number;
  hour: number;
  rounds: number;
}

/** Cost decomposed for one (provider, model). `priced=false` when the model is missing from the table. */
export interface CostByModel {
  provider: Provider;
  model: string;
  rounds: number;
  inputCost: number; // newly_append_tokens billed at fresh-input rate
  cachedCost: number; // prefix_tokens billed at cache-read rate
  outputCost: number; // output_tokens (incl. reasoning) billed at output rate
  reasoningCost: number; // subset of outputCost attributable to reasoning tokens
  costUsd: number;
  priced: boolean;
}

/** Dry-but-useful aggregates (averages + percentiles). Raw numbers; the UI formats them.
 *  This is the FULL superset (catalog in ./METRICS.md). Ratios/shares are 0–1; times in seconds;
 *  *PerStep means per LLM round. We're shipping every metric into the mock to review on /lab, then
 *  pruning — so fields here are intentionally exhaustive, not yet final. */
export interface Stats {
  // --- 2a. tokens per step ---
  avgInputTokens: number;
  avgCachedInputTokens: number;
  avgUncachedInputTokens: number; // avg fresh/append input
  avgOutputTokens: number;
  avgReasoningTokens: number; // over reasoning-bearing rounds
  inputOutputRatio: number; // total input ÷ total output (amplification)
  freshTokenShare: number; // append ÷ total input (lower = more reuse)
  reasoningShareOfOutput: number; // reasoning ÷ output

  // --- 2b. input by step trigger (user message vs tool result) ---
  userAvgTotalInput: number;
  userAvgAppendInput: number;
  toolAvgTotalInput: number;
  toolAvgAppendInput: number;

  // --- 2c. context growth ---
  totalContextIncrease: number;
  userContextDeltaAvg: number;
  userContextDeltaP50: number;
  userContextDeltaP90: number;
  toolContextDeltaAvg: number;
  toolContextDeltaP50: number;
  toolContextDeltaP90: number;
  contextIncreaseToAppendRatio: number; // overall
  userContextIncreaseToAppendRatio: number;
  toolContextIncreaseToAppendRatio: number;
  userGrowthShare: number;
  toolGrowthShare: number;
  userReductionShare: number;
  toolReductionShare: number;
  userMajorCompactShare: number;
  toolMajorCompactShare: number;

  // --- 2d. cache efficiency ---
  prefixHitRate: number;
  hitRateP50: number;
  hitRateP90: number;
  userHitRate: number;
  toolHitRate: number;
  hitRateDecayPer100: number; // change in hit rate per 100 rounds (negative = decays)

  // --- 2e. request timing (per round wall time) ---
  avgRequestS: number;
  p50RequestS: number;
  p90RequestS: number;
  p99RequestS: number;
  genTimeP50S: number;
  genTimeP90S: number;
  totalGenerationS: number;
  avgDecodeTps: number; // output tokens / second
  postReasoningDecodeTps: number;
  estTtftS: number; // estimated time-to-first-token

  // --- 2f. tools ---
  avgToolCallsPerRound: number;
  toolCallsPerRequest: number;
  stepsWithToolsShare: number;
  toolLatencyP50Ms: number;
  toolLatencyP90Ms: number;
  totalToolTimeS: number;
  toolErrorRate: number;
  readWriteRatio: number;
  avgToolResultChars: number;
  topToolName: string;
  topToolShare: number;

  // --- 2g. human-in-the-loop ---
  totalHumanWaitS: number;
  humanWaitAvgS: number;
  humanWaitP50S: number;
  humanWaitP90S: number;
  humanInLoopShare: number;
  timeSplitGenerationShare: number;
  timeSplitToolShare: number;
  timeSplitWaitingShare: number;

  // --- 2h. per session (agentic shape) ---
  avgRoundsPerSession: number; // average agentic steps
  avgToolCallsPerSession: number;
  avgSessionDurationS: number;
  avgSessionCostUsd: number;
  autonomyDepthAvg: number;
  autonomyDepthP90: number;
  avgHumanInterjections: number;
  avgModelsPerSession: number;
  avgModelSwitches: number;
  typicalSessionSteps: number;
  typicalSessionMinutes: number;
  typicalSessionCostUsd: number;

  // --- 2i. per day / relatable rates ---
  avgStepsPerDay: number;
  avgSessionsPerDay: number;
  avgActiveHoursPerDay: number;
  avgCostPerDay: number;
  avgStepsPerWeek: number;
  avgSessionsPerWeek: number;
  avgActiveDaysPerWeek: number;
  avgCostPerWeek: number;
  costPerHourUsd: number;
  tokensPerUsd: number;
  stepsPerUsd: number;
  toolCallsPerUsd: number;
  blendedUsdPerMtok: number;
  cacheSavedPct: number;
  reasoningTaxPerStepUsd: number;

  // --- 2j. models & providers (Claude vs Codex split, model mix) ---
  claudeStepShare: number;
  codexStepShare: number;
  claudeCostShare: number;
  codexCostShare: number;
  modelsRepresented: number;
  topModelName: string;
  topModelStepShare: number;
  opusStepShare: number; // share of steps on the expensive (opus-tier) model
}

export interface CostBreakdown {
  byModel: CostByModel[];
  /** What prefix caching saved vs. billing every cached token at the fresh-input rate. */
  cacheSavingsUsd: number;
  reasoningCostUsd: number;
  pricedRounds: number;
  unpricedRounds: number;
}

/** Provider-level rollup for the "Providers" split band — precomputed (Python `build_providers`)
 *  so the frontend reads it instead of re-aggregating `cost.byModel`. Sorted by `rounds` desc. */
export interface ProviderSplit {
  provider: Provider;
  rounds: number;
  cost: number;
  stepShare: number; // rounds ÷ total rounds (0–1)
  costShare: number; // cost ÷ total cost (0–1)
}

/** A clickable "superlative". When sessionId is set, the card deep-links into the session view. */
export interface Fact {
  id: string;
  /** Grouping dimension, e.g. 'time' | 'cost' | 'session' | 'reasoning' | 'tool' | 'cache' | 'model'. */
  dimension: string;
  title: string;
  /** Pre-formatted headline value (the compute side decides units/rounding). */
  value: string;
  unit?: string;
  detail?: string;
  sessionId?: string;
  roundIndex?: number;
}

/** One row in the session selector list. */
export interface SessionRow {
  sessionId: string;
  /** Human-readable conversation title when the source carries one (e.g. Claude Code's `summary`
   *  line). Absent for traces/providers that don't record one — fall back to the sessionId. */
  title?: string;
  provider: Provider;
  primaryModel: string;
  rounds: number;
  firstTsUs: number;
  lastTsUs: number;
  durationS: number;
  inputTokens: number;
  outputTokens: number;
  costUsd: number;
  toolCalls: number;
  errors: number;
}

/** One tool call within a round (for the round-inspector panel). */
export interface SessionRoundTool {
  name: string;
  ms: number;
  error: boolean;
}

/** One round inside a session timeline (x = seq within session). */
export interface SessionRoundPoint {
  seq: number;
  /** Stable per-round id (provider:session_id:round_id, sanitized). The key for fetching this
   *  round's LOCAL-only raw text via getRoundRaw(); non-sensitive (already in sanitized data). */
  traceKey: string;
  prefixTokens: number; // cached read
  appendTokens: number; // fresh input (newly_append_tokens)
  outputTokens: number;
  reasoningTokens: number;
  tsUs: number;
  isUserInput: boolean; // round started from a visible user message (vs. a tool result)
  toolCount: number;
  inferenceS: number;
  tools: SessionRoundTool[]; // the tools this round called (for the click-to-inspect panel)
}

export interface SessionToolStat {
  name: string;
  count: number;
  errors: number;
  p50Ms: number;
}

/** Per-session detail, fetched on demand (Phase 4 query RPC); mocked in Phase 1. */
export interface SessionDetail {
  sessionId: string;
  rounds: SessionRoundPoint[];
  tools: SessionToolStat[];
}

/** Per-round RAW message text — the actual input/output, not aggregates. Surfaced in the session
 *  view ONLY when the user analyzed their OWN uploaded file (see AnalyticsPayload.rawAvailable);
 *  never for contributed/remote traces, whose sanitized payload carries no text. Fetched on demand
 *  per round (Phase-4 RPC, keyed by round_pk); never inlined into SessionDetail (too heavy).
 *  `input` is the round's appended delta — the user message (inputKind:'user') or the tool results
 *  (inputKind:'tool') that triggered it; `output` is the assistant reply (incl. any tool-call args). */
export interface RoundRawTool {
  name: string;
  input: string; // the tool-call arguments, as text
  result: string; // the tool's returned content
  error: boolean;
}
export interface RoundRaw {
  input: string;
  inputKind: 'user' | 'tool';
  output: string;
  tools?: RoundRawTool[];
}

// ---- distribution chart specs (the existing 8 figures, as data instead of PNG) ----

export interface CdfSpec {
  kind: 'cdf';
  xLabel: string;
  yLabel: string;
  xLog?: boolean;
  series: { name: string; points: [number, number][] }[];
}
export interface HistogramSpec {
  kind: 'histogram';
  xLabel: string;
  yLabel: string;
  xLog?: boolean;
  /** Category bars (single series), e.g. cache-hit-ratio buckets. */
  bins?: { label: string; count: number }[];
  /** Line histogram / frequency polygon: one line per series over a numeric x (bin center → share). */
  series?: { name: string; points: [number, number][] }[];
}
export interface BoxplotSpec {
  kind: 'boxplot';
  yLabel: string;
  yLog?: boolean; // log y-axis (e.g. latency that spans ms→tens-of-seconds)
  yUnit?: 'ms'; // values are milliseconds — render ticks as durations (ms / s / min) instead of raw ms
  groups: { name: string; min: number; q1: number; median: number; q3: number; max: number }[];
}
/** x/y point cloud, one series per group. Used for the append-vs-prefix round scatter. */
export interface ScatterSpec {
  kind: 'scatter';
  xLabel: string;
  yLabel: string;
  xLog?: boolean;
  yLog?: boolean;
  series: { name: string; points: [number, number][] }[];
}
export interface BarhSpec {
  kind: 'barh';
  xLabel: string;
  items: { name: string; value: number }[];
}
export type SeriesSpec = CdfSpec | HistogramSpec | BoxplotSpec | BarhSpec | ScatterSpec;

/** The full analytics payload for one analyzed trace. */
export interface AnalyticsPayload {
  kpis: Kpis;
  perDay: PerDay[];
  hourWeekday: HourWeekday[];
  cost: CostBreakdown;
  /** Provider split band (rounds + spend share per provider), precomputed from `cost.byModel`. */
  providers: ProviderSplit[];
  stats: Stats;
  facts: Fact[];
  sessions: SessionRow[];
  /** True only when analyzing a locally-uploaded raw file — gates the per-round raw text viewer.
   *  Contributed/remote traces ship sanitized (text-free) payloads, so this is false there. */
  rawAvailable: boolean;
  /** Keyed by experiment slug-tail, e.g. 'tool_call_counts'. */
  distributions: Record<string, SeriesSpec>;
}
