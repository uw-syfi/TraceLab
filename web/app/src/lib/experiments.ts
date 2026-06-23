// Registry of experiment detail pages. Each gallery card links to `/exp/<slug>`, where the
// page renders the experiment's README (the paired artifacts/<slug>/README.md), every figure it
// emits, and every Markdown table it emits. `slug` doubles as the artifacts subpath and the URL.
//
// Order follows the TraceLab paper: Session → LLM generation → Tool calls → Prefix cache. This
// drives the detail page's experiment-nav grouping and the prev/next navigation.

export interface ExperimentFigure {
  src: string;
  caption: string;
}

// A table emitted by an experiment's analyze.py as a GFM Markdown file. `src` is repo-relative and
// read at build time by the detail page, rendered via @astrojs/markdown-remark (no public copy).
export interface ExperimentTable {
  src: string;
  caption: string;
}

export interface Experiment {
  slug: string; // 'tool_calls/tool_call_counts' — artifacts subpath + URL
  category: string;
  title: string;
  blurb: string;
  readme: string; // repo-relative path to the README.md
  figures: ExperimentFigure[];
  tables?: ExperimentTable[]; // repo-relative Markdown tables, rendered after the figures
}

export const experiments: Experiment[] = [
  // ----- Session and context management (paper §3–§4 tables, then session progression + human waits) -----
  {
    slug: 'session/session_internal_counts',
    category: 'session',
    title: 'Session internal counts',
    blurb: 'Requests, user-/tool-initiated steps, and tool calls per session, request, and step.',
    readme: 'artifacts/session/session_internal_counts/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/session/session_internal_counts/session_internal_counts.md',
        caption: 'Per-session, per-request, and per-step counts (avg / p25 / p50 / p90 / p99).',
      },
    ],
  },
  {
    slug: 'session/total_input_growth',
    category: 'session',
    title: 'Total input growth',
    blurb: 'Net context growth after tool-triggered agent steps.',
    readme: 'artifacts/session/total_input_growth/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/session/total_input_growth/total_input_growth.md',
        caption:
          'Same-session context change by step trigger: per-step change in total input length (prefix + append tokens) versus the previous step in the session, split into growth and reduction bands.',
      },
    ],
  },
  {
    slug: 'session/session_compaction_counts',
    category: 'session',
    title: 'Context compactions',
    blurb: 'How often a session summarizes and drops its context near the limit.',
    readme: 'artifacts/session/session_compaction_counts/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/session/session_compaction_counts/session_compaction_counts.md',
        caption: 'Compactions per session and their user- vs tool-initiated trigger split, by provider.',
      },
    ],
  },
  {
    slug: 'session/session_cost_distribution',
    category: 'session',
    title: 'Cost distribution',
    blurb: 'What a session, request, and step cost — and where the money goes.',
    readme: 'artifacts/session/session_cost_distribution/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/session/session_cost_distribution/session_cost_distribution.md',
        caption: 'Per-session, per-request, and per-step cost (USD) by category; % cost is each category’s share of total spend.',
      },
    ],
  },
  {
    slug: 'session/session_timing_distribution',
    category: 'session',
    title: 'Timing distribution',
    blurb: 'Human thinking vs LLM generation vs tool execution across the wall clock.',
    readme: 'artifacts/session/session_timing_distribution/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/session/session_timing_distribution/session_timing_distribution.md',
        caption: 'Per-session, per-request, per-step, and individual latency time by category; % time is each category’s share.',
      },
    ],
  },
  {
    slug: 'session/session_token_steps',
    category: 'session',
    title: 'Session token steps',
    blurb: 'How context grows across a single session.',
    readme: 'artifacts/session/session_token_steps/README.md',
    figures: [{ src: '/figures/session/session_token_steps.png', caption: 'Token totals across a session.' }],
  },
  {
    slug: 'human_in_the_loop/human_input_wait',
    category: 'human_in_the_loop',
    title: 'Human input wait',
    blurb: 'How long the agent waits on a human between requests.',
    readme: 'artifacts/human_in_the_loop/human_input_wait/README.md',
    figures: [
      { src: '/figures/human_in_the_loop/human_input_wait_cdf.png', caption: 'Human input wait CDF.' },
      { src: '/figures/human_in_the_loop/human_input_wait_count_cdf_by_provider.png', caption: 'Human wait count CDF by provider.' },
      { src: '/figures/human_in_the_loop/human_input_wait_total_cdf_by_provider.png', caption: 'Human wait total-time CDF by provider.' },
    ],
  },

  // ----- LLM generation (paper §5: input length → output length → attribution → timing) -----
  {
    slug: 'llm_generation/token_length_distribution',
    category: 'llm_generation',
    title: 'Token length distribution',
    blurb: 'Prefix, append, and output token lengths per step, by provider.',
    readme: 'artifacts/llm_generation/token_length_distribution/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/llm_generation/token_length_distribution/token_length_distribution.md',
        caption: 'Per-step prefix, append, and output token lengths (avg / p25 / p50 / p90 / p99), by provider.',
      },
    ],
  },
  {
    slug: 'llm_generation/prefix_append_distribution',
    category: 'llm_generation',
    title: 'Prefix vs append token composition',
    blurb: 'Cached prefix against freshly appended input per agent step.',
    readme: 'artifacts/llm_generation/prefix_append_distribution/README.md',
    figures: [
      { src: '/figures/llm_generation/prefix_append_distribution.png', caption: 'Prefix vs appended input token histograms.' },
      { src: '/figures/llm_generation/prefix_append_cdf.png', caption: 'Prefix and append token CDFs.' },
      { src: '/figures/llm_generation/prefix_vs_append_sample.png', caption: 'Prefix versus append scatter sample.' },
      { src: '/figures/llm_generation/append_tokens_weighted_bins.png', caption: 'Append-token count share versus token-mass share.' },
    ],
  },
  {
    slug: 'llm_generation/append_by_prefix_bin',
    category: 'llm_generation',
    title: 'Append by prefix bin',
    blurb: 'How append length collapses as the cached prefix fills.',
    readme: 'artifacts/llm_generation/append_by_prefix_bin/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/llm_generation/append_by_prefix_bin/append_by_prefix_bin.md',
        caption: 'Append-token stats (steps / avg / p50 / p90 / p99) by prefix-length bin, per provider.',
      },
    ],
  },
  {
    slug: 'llm_generation/output_tokens',
    category: 'llm_generation',
    title: 'Output token distribution',
    blurb: "How long the agents' completions run.",
    readme: 'artifacts/llm_generation/output_tokens/README.md',
    figures: [{ src: '/figures/llm_generation/output_tokens_distribution.png', caption: 'Output token length distribution.' }],
  },
  {
    slug: 'llm_generation/output_append_assignment',
    category: 'llm_generation',
    title: 'Output attribution',
    blurb: 'How a prior step’s output is accounted in the next step — cached into the prefix or re-sent as append — illustrated, then measured per model.',
    readme: 'artifacts/llm_generation/output_append_assignment/README.md',
    figures: [
      { src: '/figures/llm_generation/output_attribution_schematic.png', caption: 'Two ways a prior step’s output is accounted in the next step: cached as prefix, or re-sent as append (schematic).' },
      { src: '/figures/llm_generation/output_vs_next_append_scatter_min2000.png', caption: 'Previous output versus next append, min 2000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_next_append_min2000.png', caption: 'Ranked previous output and next append, min 2000 output tokens.' },
      { src: '/figures/llm_generation/output_vs_prefix_gain_scatter_min2000.png', caption: 'Previous output versus next prefix gain, min 2000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_prefix_gain_min2000.png', caption: 'Ranked previous output and prefix gain, min 2000 output tokens.' },
    ],
  },
  {
    slug: 'llm_generation/context_decode_speed_scatter',
    category: 'llm_generation',
    title: 'Context vs decode speed',
    blurb: 'Observed LLM timing against total input context length.',
    readme: 'artifacts/llm_generation/context_decode_speed_scatter/README.md',
    figures: [{ src: '/figures/llm_generation/context_decode_speed_scatter.png', caption: 'Trace-observed LLM timing versus total input context length.' }],
  },
  {
    slug: 'llm_generation/adjusted_prefix_append',
    category: 'llm_generation',
    title: 'Adjusted prefix vs append',
    blurb: 'Fresh append after subtracting the prior assistant output replay.',
    readme: 'artifacts/llm_generation/adjusted_prefix_append/README.md',
    figures: [{ src: '/figures/llm_generation/prefix_vs_adjusted_append_sample.png', caption: 'Prefix versus adjusted append scatter sample.' }],
  },
  {
    slug: 'llm_generation/token_spindles',
    category: 'llm_generation',
    title: 'Token spindles',
    blurb: 'Prefix, adjusted append, and output token distributions on one compressed axis.',
    readme: 'artifacts/llm_generation/token_spindles/README.md',
    figures: [{ src: '/figures/llm_generation/token_spindles_transparent.png', caption: 'Prefix, adjusted append, and output token spindles.' }],
  },
  {
    slug: 'llm_generation/generation_time_cdf',
    category: 'llm_generation',
    title: 'Generation-time CDF',
    blurb: 'Wall-clock time to produce a full response, by provider.',
    readme: 'artifacts/llm_generation/generation_time_cdf/README.md',
    figures: [
      { src: '/figures/llm_generation/llm_generation_time_count_cdf_by_provider.png', caption: 'Generation-time count CDF by provider.' },
      { src: '/figures/llm_generation/llm_generation_time_total_cdf_by_provider.png', caption: 'Generation-time total CDF by provider.' },
    ],
  },
  {
    slug: 'llm_generation/append_vs_prefix_latency',
    category: 'llm_generation',
    title: 'Append-heavy latency match',
    blurb: 'Whether append-heavy agent steps are slower than comparable prefix-heavy steps.',
    readme: 'artifacts/llm_generation/append_vs_prefix_latency/README.md',
    figures: [
      { src: '/figures/llm_generation/append_vs_prefix_bucket_effects.png', caption: 'Matched-bucket append-heavy latency effects.' },
      { src: '/figures/llm_generation/append_vs_prefix_normalized_overlap.png', caption: 'Normalized latency overlap for append-heavy and prefix-heavy rows.' },
    ],
  },

  // ----- Tool calls (paper §6: counts → latency → overhead) -----
  {
    slug: 'tool_calls/tool_call_counts',
    category: 'tool_calls',
    title: 'Tool call counts by tool',
    blurb: 'Which tools the agents use the most, per provider.',
    readme: 'artifacts/tool_calls/tool_call_counts/README.md',
    figures: [{ src: '/figures/tool_calls/tool_call_counts.png', caption: 'Tool call counts by tool and provider.' }],
  },
  {
    slug: 'tool_calls/tool_latency_distribution',
    category: 'tool_calls',
    title: 'Tool latency distribution',
    blurb: 'How long individual tool calls take to return.',
    readme: 'artifacts/tool_calls/tool_latency_distribution/README.md',
    figures: [
      { src: '/figures/tool_calls/tool_latency_by_tool.png', caption: 'Per-tool latency distribution.' },
      { src: '/figures/tool_calls/tool_latency_weighted_bins.png', caption: 'Tool-call count share versus summed-latency share.' },
      { src: '/figures/tool_calls/tool_latency_count_cdf_by_provider.png', caption: 'Latency CDF by provider.' },
      { src: '/figures/tool_calls/tool_total_latency_cdf_by_provider.png', caption: 'Summed tool-latency CDF by provider.' },
    ],
  },
  {
    slug: 'tool_calls/codex_wall_internal_gap',
    category: 'tool_calls',
    title: 'Codex tool overhead',
    blurb: 'Codex tool end-to-end time versus internal execution time.',
    readme: 'artifacts/tool_calls/codex_wall_internal_gap/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/tool_calls/codex_wall_internal_gap/codex_tool_e2e_internal.md',
        caption: 'Codex tool end-to-end vs internal latency and the residual gap, by tool.',
      },
    ],
  },
  {
    slug: 'tool_calls/tool_category_distribution',
    category: 'tool_calls',
    title: 'Tool category distribution',
    blurb: 'How tool calls and latency split across coarse cross-provider categories.',
    readme: 'artifacts/tool_calls/tool_category_distribution/README.md',
    figures: [
      { src: '/figures/tool_calls/tool_category_count_ring.png', caption: 'Tool-call count share by coarse category.' },
      { src: '/figures/tool_calls/tool_category_latency_bar.png', caption: 'Summed effective latency by tool category.' },
      { src: '/figures/tool_calls/tool_category_dashboard.png', caption: 'Tool category dashboard with call share and latency quantiles.' },
      { src: '/figures/tool_calls/tool_latency_long_tail_imbalance.png', caption: 'Call share versus latency share across latency bins.' },
    ],
  },
  {
    slug: 'tool_calls/tool_time_by_kind',
    category: 'tool_calls',
    title: 'Total tool time by kind',
    blurb: 'Which tool kinds account for the most aggregate effective time.',
    readme: 'artifacts/tool_calls/tool_time_by_kind/README.md',
    figures: [{ src: '/figures/tool_calls/tool_total_time_by_kind.png', caption: 'Total effective tool time by kind and provider.' }],
  },

  // ----- Prefix cache (paper §7: hit rate → idle/eviction → redundant prefill → storage) -----
  {
    slug: 'prefix_cache/cache_hit_ratio',
    category: 'prefix_cache',
    title: 'Prefix cache hit ratio',
    blurb: 'How much input is served from the prefix cache.',
    readme: 'artifacts/prefix_cache/cache_hit_ratio/README.md',
    figures: [
      { src: '/figures/prefix_cache/cache_hit_ratio_histogram.png', caption: 'Per-step cache hit ratio.' },
      { src: '/figures/prefix_cache/cache_hit_ratio_append_weighted_histogram.png', caption: 'Append-weighted cache hit ratio.' },
    ],
    tables: [
      {
        src: 'artifacts/prefix_cache/cache_hit_ratio/cache_hit_ratio_table.md',
        caption: 'Token-weighted prefix-cache hit rate by provider.',
      },
    ],
  },
  {
    slug: 'prefix_cache/cache_hit_idle_relationship',
    category: 'prefix_cache',
    title: 'Cache hit versus idle gap',
    blurb: 'Whether low prefix-cache-hit agent steps follow long human or tool waits.',
    readme: 'artifacts/prefix_cache/cache_hit_idle_relationship/README.md',
    figures: [
      { src: '/figures/prefix_cache/user_wait_time_vs_hit_rate_scatter.png', caption: 'Human idle wait versus prefix-cache hit rate.' },
      { src: '/figures/prefix_cache/tool_result_wait_time_vs_hit_rate_scatter.png', caption: 'Tool-triggered wait time versus prefix-cache hit rate.' },
    ],
  },
  {
    slug: 'prefix_cache/redundant_prefill',
    category: 'prefix_cache',
    title: 'Redundant prefill',
    blurb: 'How much prefilled context is genuinely fresh versus replayed.',
    readme: 'artifacts/prefix_cache/redundant_prefill/README.md',
    figures: [],
    tables: [
      {
        src: 'artifacts/prefix_cache/redundant_prefill/redundant_prefill_table.md',
        caption: 'Fresh-token share of append and prefill amplification, overall and by trigger, per provider.',
      },
    ],
  },
  {
    slug: 'prefix_cache/eviction_tradeoff',
    category: 'prefix_cache',
    title: 'Eviction trade-off',
    blurb: 'Cache hit rate against storage as the eviction timeout grows.',
    readme: 'artifacts/prefix_cache/eviction_tradeoff/README.md',
    figures: [
      { src: '/figures/prefix_cache/eviction_tradeoff_by_timeout.png', caption: 'Prefix-cache hit rate and storage versus the eviction timeout.' },
      { src: '/figures/prefix_cache/eviction_tradeoff_pareto.png', caption: 'Hit-rate versus storage Pareto frontier.' },
    ],
  },
];

export const experimentSlugs = new Set(experiments.map((e) => e.slug));
