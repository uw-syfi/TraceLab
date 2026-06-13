// Registry of experiment detail pages. Each gallery card links to `/exp/<slug>`, where the
// page renders the experiment's README (the paired artifacts/<slug>/README.md) plus every
// figure that experiment emits. `slug` doubles as the artifacts subpath and the URL.

export interface ExperimentFigure {
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
}

export const experiments: Experiment[] = [
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
    slug: 'llm_generation/adjusted_prefix_append',
    category: 'llm_generation',
    title: 'Adjusted prefix vs append',
    blurb: 'Fresh append after subtracting the prior assistant output replay.',
    readme: 'artifacts/llm_generation/adjusted_prefix_append/README.md',
    figures: [{ src: '/figures/llm_generation/prefix_vs_adjusted_append_sample.png', caption: 'Prefix versus adjusted append scatter sample.' }],
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
  {
    slug: 'llm_generation/output_append_assignment',
    category: 'llm_generation',
    title: 'Output append assignment',
    blurb: 'Where the previous assistant output lands in the next agent step: append or prefix.',
    readme: 'artifacts/llm_generation/output_append_assignment/README.md',
    figures: [
      { src: '/figures/llm_generation/output_vs_next_append_scatter_min2000.png', caption: 'Previous output versus next append, min 2000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_next_append_min2000.png', caption: 'Ranked previous output and next append, min 2000 output tokens.' },
      { src: '/figures/llm_generation/output_vs_prefix_gain_scatter_min2000.png', caption: 'Previous output versus next prefix gain, min 2000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_prefix_gain_min2000.png', caption: 'Ranked previous output and prefix gain, min 2000 output tokens.' },
      { src: '/figures/llm_generation/output_vs_next_append_scatter_min4000.png', caption: 'Previous output versus next append, min 4000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_next_append_min4000.png', caption: 'Ranked previous output and next append, min 4000 output tokens.' },
      { src: '/figures/llm_generation/output_vs_prefix_gain_scatter_min4000.png', caption: 'Previous output versus next prefix gain, min 4000 output tokens.' },
      { src: '/figures/llm_generation/ranked_output_vs_prefix_gain_min4000.png', caption: 'Ranked previous output and prefix gain, min 4000 output tokens.' },
    ],
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
    slug: 'llm_generation/output_tokens',
    category: 'llm_generation',
    title: 'Output token distribution',
    blurb: "How long the agents' completions run.",
    readme: 'artifacts/llm_generation/output_tokens/README.md',
    figures: [{ src: '/figures/llm_generation/output_tokens_distribution.png', caption: 'Output token length distribution.' }],
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
    slug: 'prefix_cache/cache_hit_ratio',
    category: 'prefix_cache',
    title: 'Prefix cache hit ratio',
    blurb: 'How much input is served from the prefix cache.',
    readme: 'artifacts/prefix_cache/cache_hit_ratio/README.md',
    figures: [
      { src: '/figures/prefix_cache/cache_hit_ratio_histogram.png', caption: 'Per-step cache hit ratio.' },
      { src: '/figures/prefix_cache/cache_hit_ratio_append_weighted_histogram.png', caption: 'Append-weighted cache hit ratio.' },
    ],
  },
  {
    slug: 'prefix_cache/kv_cache_active_ratio',
    category: 'prefix_cache',
    title: 'KV cache active ratio',
    blurb: 'Share of context kept warm in the KV cache.',
    readme: 'artifacts/prefix_cache/kv_cache_active_ratio/README.md',
    figures: [{ src: '/figures/prefix_cache/kv_cache_active_ratio_by_provider.png', caption: 'KV cache active ratio by provider.' }],
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
  {
    slug: 'session/session_token_steps',
    category: 'session',
    title: 'Session token steps',
    blurb: 'How context grows across a single session.',
    readme: 'artifacts/session/session_token_steps/README.md',
    figures: [{ src: '/figures/session/session_token_steps.png', caption: 'Token totals across a session.' }],
  },
  // README-only experiments (analyze-only, no committed figure) — the two Overview stat cards.
  {
    slug: 'human_in_the_loop/user_turn_response_time',
    category: 'human_in_the_loop',
    title: 'Waiting on a human',
    blurb: 'Time the agent sits idle waiting for the next human message.',
    readme: 'artifacts/human_in_the_loop/user_turn_response_time/README.md',
    figures: [],
  },
  {
    slug: 'session/total_input_growth',
    category: 'session',
    title: 'Total input growth',
    blurb: 'Net context growth after tool-triggered agent steps.',
    readme: 'artifacts/session/total_input_growth/README.md',
    figures: [],
  },
];

export const experimentSlugs = new Set(experiments.map((e) => e.slug));
