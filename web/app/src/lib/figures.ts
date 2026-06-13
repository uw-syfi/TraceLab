// Data-driven gallery manifest. Figure cards point at pre-rendered matplotlib PNGs under
// /figures/<category>/<file>.png (copied by web/scripts/build-payload.mjs). Stat cards have
// no PNG (their experiments are analyze.py) and render summary-derived numbers instead.
// `slug` links a card to its experiment detail page (/exp/<slug>); see lib/experiments.ts.

export type StatKey = 'human_wait' | 'input_growth';

export interface FigureCard {
  category: string;
  title: string;
  blurb: string;
  variant: 'figure' | 'stat';
  src?: string;
  stat?: StatKey;
  slug?: string;
}

export interface FigureSection {
  key: string;
  title: string;
  description: string;
  figures: FigureCard[];
}

export const overviewFigures: FigureCard[] = [
  {
    category: 'tool_calls',
    title: 'Tool latency distribution',
    blurb: 'Cumulative distribution of per-call tool latency.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_latency_by_tool.png',
    slug: 'tool_calls/tool_latency_distribution',
  },
  {
    category: 'tool_calls',
    title: 'Tool call counts by tool',
    blurb: 'Which tools the agents use the most.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_call_counts.png',
    slug: 'tool_calls/tool_call_counts',
  },
  {
    category: 'llm_generation',
    title: 'Prefix vs append token composition',
    blurb: 'Cached prefix against freshly appended input.',
    variant: 'figure',
    src: '/figures/llm_generation/prefix_append_distribution.png',
    slug: 'llm_generation/prefix_append_distribution',
  },
  {
    category: 'llm_generation',
    title: 'Output token distribution',
    blurb: "How long the agents' completions run.",
    variant: 'figure',
    src: '/figures/llm_generation/output_tokens_distribution.png',
    slug: 'llm_generation/output_tokens',
  },
  {
    category: 'llm_generation',
    title: 'Generation-time CDF',
    blurb: 'Wall-clock time to produce a full response.',
    variant: 'figure',
    src: '/figures/llm_generation/llm_generation_time_count_cdf_by_provider.png',
    slug: 'llm_generation/generation_time_cdf',
  },
  {
    category: 'prefix_cache',
    title: 'Cache hit ratio',
    blurb: 'How much input is served from the prefix cache.',
    variant: 'figure',
    src: '/figures/prefix_cache/cache_hit_ratio_histogram.png',
    slug: 'prefix_cache/cache_hit_ratio',
  },
  {
    category: 'prefix_cache',
    title: 'KV cache active ratio',
    blurb: 'Share of context kept warm in the KV cache.',
    variant: 'figure',
    src: '/figures/prefix_cache/kv_cache_active_ratio_by_provider.png',
    slug: 'prefix_cache/kv_cache_active_ratio',
  },
  {
    category: 'human_in_the_loop',
    title: 'Human input wait',
    blurb: 'How long the agent waits on a human.',
    variant: 'figure',
    src: '/figures/human_in_the_loop/human_input_wait_cdf.png',
    slug: 'human_in_the_loop/human_input_wait',
  },
  {
    category: 'session',
    title: 'Session token steps',
    blurb: 'How context grows across a session.',
    variant: 'figure',
    src: '/figures/session/session_token_steps.png',
    slug: 'session/session_token_steps',
  },
  {
    category: 'human_in_the_loop',
    title: 'Waiting on a human',
    blurb: 'Time the agent sits idle waiting for the next human message.',
    variant: 'stat',
    stat: 'human_wait',
    slug: 'human_in_the_loop/user_turn_response_time',
  },
  {
    category: 'session',
    title: 'Total input growth',
    blurb: 'Net context growth after tool-triggered agent steps.',
    variant: 'stat',
    stat: 'input_growth',
    slug: 'session/total_input_growth',
  },
];

// Extra public-pool figures that are already generated under artifacts/. These are intentionally
// not part of `overviewFigures`, because the browser-local Analyze flow only renders the smaller
// v1 set above.
export const publicArtifactFigures: FigureCard[] = [
  {
    category: 'tool_calls',
    title: 'Tool latency mass bins',
    blurb: 'Fast-call counts versus where aggregate tool time accumulates.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_latency_weighted_bins.png',
    slug: 'tool_calls/tool_latency_distribution',
  },
  {
    category: 'tool_calls',
    title: 'Total tool time by kind',
    blurb: 'Which tool kinds account for the most attributed work.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_total_time_by_kind.png',
    slug: 'tool_calls/tool_time_by_kind',
  },
  {
    category: 'llm_generation',
    title: 'Prefix / append CDF',
    blurb: 'Median and tail length for cached prefix and fresh append.',
    variant: 'figure',
    src: '/figures/llm_generation/prefix_append_cdf.png',
    slug: 'llm_generation/prefix_append_distribution',
  },
  {
    category: 'llm_generation',
    title: 'Append token mass bins',
    blurb: 'Short agent steps by count, large agent steps by appended-token mass.',
    variant: 'figure',
    src: '/figures/llm_generation/append_tokens_weighted_bins.png',
    slug: 'llm_generation/prefix_append_distribution',
  },
  {
    category: 'llm_generation',
    title: 'Token spindles',
    blurb: 'Prefix, adjusted append, and output distributions on one axis.',
    variant: 'figure',
    src: '/figures/llm_generation/token_spindles_transparent.png',
    slug: 'llm_generation/token_spindles',
  },
  {
    category: 'llm_generation',
    title: 'Adjusted append scatter',
    blurb: 'Fresh context after subtracting replayed prior output.',
    variant: 'figure',
    src: '/figures/llm_generation/prefix_vs_adjusted_append_sample.png',
    slug: 'llm_generation/adjusted_prefix_append',
  },
  {
    category: 'llm_generation',
    title: 'Previous output placement',
    blurb: 'Whether a long response returns as fresh append or cached-prefix growth.',
    variant: 'figure',
    src: '/figures/llm_generation/output_vs_next_append_scatter_min4000.png',
    slug: 'llm_generation/output_append_assignment',
  },
  {
    category: 'llm_generation',
    title: 'Generation total-time CDF',
    blurb: 'Where summed model-generation time accumulates.',
    variant: 'figure',
    src: '/figures/llm_generation/llm_generation_time_total_cdf_by_provider.png',
    slug: 'llm_generation/generation_time_cdf',
  },
  {
    category: 'prefix_cache',
    title: 'Cache hit after human waits',
    blurb: 'Prefix-cache hit rate against the preceding human idle gap.',
    variant: 'figure',
    src: '/figures/prefix_cache/user_wait_time_vs_hit_rate_scatter.png',
    slug: 'prefix_cache/cache_hit_idle_relationship',
  },
  {
    category: 'prefix_cache',
    title: 'Cache hit after tool waits',
    blurb: 'Prefix-cache hit rate after tool-triggered waits.',
    variant: 'figure',
    src: '/figures/prefix_cache/tool_result_wait_time_vs_hit_rate_scatter.png',
    slug: 'prefix_cache/cache_hit_idle_relationship',
  },
  {
    category: 'human_in_the_loop',
    title: 'Human wait count CDF',
    blurb: 'How quickly human-response waits resolve by provider.',
    variant: 'figure',
    src: '/figures/human_in_the_loop/human_input_wait_count_cdf_by_provider.png',
    slug: 'human_in_the_loop/human_input_wait',
  },
  {
    category: 'human_in_the_loop',
    title: 'Human wait total-time CDF',
    blurb: 'Where the summed human idle time accumulates.',
    variant: 'figure',
    src: '/figures/human_in_the_loop/human_input_wait_total_cdf_by_provider.png',
    slug: 'human_in_the_loop/human_input_wait',
  },
];

export const atlasFigures: FigureCard[] = [...overviewFigures, ...publicArtifactFigures];

const overviewSectionMeta = [
  {
    key: 'tool_calls',
    title: 'Tool calls',
    description: 'How agents choose tools, how often they call them, and how long those calls take.',
  },
  {
    key: 'llm_generation',
    title: 'LLM generation',
    description: 'Token composition, output length, and end-to-end generation timing.',
  },
  {
    key: 'prefix_cache',
    title: 'Prefix cache',
    description: 'Cache reuse and the share of context kept active across agent steps.',
  },
  {
    key: 'human_in_the_loop',
    title: 'Human in the loop',
    description: 'Where human waiting time appears in the workload timeline.',
  },
  {
    key: 'session',
    title: 'Session context',
    description: 'How context evolves across a full agent session.',
  },
] as const;

export const overviewFigureSections: FigureSection[] = overviewSectionMeta
  .map((section) => ({
    ...section,
    figures: atlasFigures.filter((figure) => figure.category === section.key),
  }))
  .filter((section) => section.figures.length > 0);

// Provider Comparison gallery — the by-provider matplotlib variants (Claude vs Codex are
// drawn in the same figure). The two model-mix cards are rendered live, not as PNGs.
export const compareFigures: FigureCard[] = [
  {
    category: 'llm_generation',
    title: 'Generation-time CDF by provider',
    blurb: 'Wall-clock time to a full response, split by provider.',
    variant: 'figure',
    src: '/figures/llm_generation/llm_generation_time_count_cdf_by_provider.png',
    slug: 'llm_generation/generation_time_cdf',
  },
  {
    category: 'llm_generation',
    title: 'Generation total-time CDF',
    blurb: 'Summed generation time by threshold, split by provider.',
    variant: 'figure',
    src: '/figures/llm_generation/llm_generation_time_total_cdf_by_provider.png',
    slug: 'llm_generation/generation_time_cdf',
  },
  {
    category: 'tool_calls',
    title: 'Tool latency CDF by provider',
    blurb: 'Per-call tool latency, split by provider.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_latency_count_cdf_by_provider.png',
    slug: 'tool_calls/tool_latency_distribution',
  },
  {
    category: 'tool_calls',
    title: 'Tool total-latency CDF',
    blurb: 'Summed tool latency by threshold, split by provider.',
    variant: 'figure',
    src: '/figures/tool_calls/tool_total_latency_cdf_by_provider.png',
    slug: 'tool_calls/tool_latency_distribution',
  },
  {
    category: 'human_in_the_loop',
    title: 'Human wait count CDF',
    blurb: 'Human-response wait thresholds, split by provider.',
    variant: 'figure',
    src: '/figures/human_in_the_loop/human_input_wait_count_cdf_by_provider.png',
    slug: 'human_in_the_loop/human_input_wait',
  },
  {
    category: 'human_in_the_loop',
    title: 'Human wait total-time CDF',
    blurb: 'Summed idle time by wait threshold, split by provider.',
    variant: 'figure',
    src: '/figures/human_in_the_loop/human_input_wait_total_cdf_by_provider.png',
    slug: 'human_in_the_loop/human_input_wait',
  },
  {
    category: 'prefix_cache',
    title: 'Cache hit ratio (append-weighted)',
    blurb: 'Prefix cache hit ratio weighted by appended tokens.',
    variant: 'figure',
    src: '/figures/prefix_cache/cache_hit_ratio_append_weighted_histogram.png',
    slug: 'prefix_cache/cache_hit_ratio',
  },
  {
    category: 'prefix_cache',
    title: 'KV cache active ratio by provider',
    blurb: 'Share of context kept warm in the KV cache, per provider.',
    variant: 'figure',
    src: '/figures/prefix_cache/kv_cache_active_ratio_by_provider.png',
    slug: 'prefix_cache/kv_cache_active_ratio',
  },
];

/** Categories rendered with the sage tag tint (rest use terracotta). */
export const sageCategories = new Set(['llm_generation', 'session']);
