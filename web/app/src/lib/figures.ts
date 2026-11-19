// Single source of truth for the Overview gallery. `overviewFigureSections` is an EXPLICIT, ordered
// manifest: sections in display order, each listing its cards in display order. This mirrors the
// TraceLab paper's layout (Session → LLM generation → Tool calls → Prefix cache, §4–§7).
// Subagents extends Session before LLM generation without changing the existing paper order.
//
// Within each section, cards are ordered to match the paper's float sequence (the `\input` order in
// src/0{4,5,6,7}_*.tex) FIRST, then the non-paper "good to have" extras (CDFs, scatters) AFTER. So
// the gallery reads the paper's figures/tables in the paper's order, with supporting cuts trailing.
//
// Card variants:
//   'figure' — points at a pre-rendered matplotlib PNG under /figures/<category>/<file>.png (copied
//              by web/scripts/build-payload.mjs); the card shows a themed motif, the detail page the
//              real chart.
//   'stat'   — no PNG. Shows a few headline numbers and links to a detail page that renders the full
//              table. Numbers come from summary.json (legacy `stat` key) or, for the paper tables,
//              from public/data/headlines.json keyed by `slug` (see lib/headlines.ts).
// `slug` links a card to its experiment detail page (/exp/<slug>); see lib/experiments.ts.

export type StatKey = 'input_growth';

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

// `human_in_the_loop` cards are displayed under the Session section/tag (the paper folds human-wait
// timing into Session & Context); every other category maps to itself.
export const gallerySectionKey = (category: string) =>
  category === 'human_in_the_loop' ? 'session' : category;

export const overviewFigureSections: FigureSection[] = [
  {
    key: 'session',
    title: 'Session',
    description: 'Session-level context, cost, timing, compaction, and human waits across a full agent session.',
    figures: [
      // --- paper §3–§4 tables, in document order ---
      {
        category: 'session',
        title: 'Session internal counts',
        blurb: 'Requests, steps, and tool calls per session, request, and step.',
        variant: 'stat',
        slug: 'session/session_internal_counts',
      },
      {
        category: 'session',
        title: 'Total input growth',
        blurb: 'Net context growth after tool-triggered agent steps.',
        variant: 'stat',
        stat: 'input_growth',
        slug: 'session/total_input_growth',
      },
      {
        category: 'session',
        title: 'Context compactions',
        blurb: 'How often a session summarizes and drops its context near the limit.',
        variant: 'stat',
        slug: 'session/session_compaction_counts',
      },
      {
        category: 'session',
        title: 'Cost distribution',
        blurb: 'USD per session, request, and step — and where the money goes.',
        variant: 'stat',
        slug: 'session/session_cost_distribution',
      },
      {
        category: 'session',
        title: 'Timing distribution',
        blurb: 'Human thinking vs LLM generation vs tool execution across the wall clock.',
        variant: 'stat',
        slug: 'session/session_timing_distribution',
      },
      // session progression example (paper fig in §7, but a session-level figure)
      {
        category: 'session',
        title: 'Session token steps',
        blurb: 'How context grows across a single session.',
        variant: 'figure',
        src: '/figures/session/session_token_steps.png',
        slug: 'session/session_token_steps',
      },
      // --- non-paper extras (human-wait cuts) ---
      {
        category: 'human_in_the_loop',
        title: 'Human input wait',
        blurb: 'How long the agent waits on a human.',
        variant: 'figure',
        src: '/figures/human_in_the_loop/human_input_wait_cdf.png',
        slug: 'human_in_the_loop/human_input_wait',
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
    ],
  },
  {
    key: 'subagents',
    title: 'Subagents',
    description: 'Compare recorded subagent use, workload, tokens, tools, and timing in Claude and Codex, with missing data kept visible.',
    figures: [
      {
        category: 'subagents',
        title: 'Subagent adoption',
        blurb: 'How often main sessions have linked subagents, how many, and what the recorded links reveal.',
        variant: 'stat',
        slug: 'subagents/adoption',
      },
      {
        category: 'subagents',
        title: 'Subagent workload',
        blurb: 'Recorded rounds and tool calls in main sessions and their subagents.',
        variant: 'stat',
        slug: 'subagents/workload',
      },
      {
        category: 'subagents',
        title: 'Subagent tokens and reports',
        blurb: 'Recorded tokens in main sessions and linked subagents, plus input changes around possible or completed subagent replies.',
        variant: 'stat',
        slug: 'subagents/tokens_and_reports',
      },
      {
        category: 'subagents',
        title: 'Subagent tool usage',
        blurb: 'Tools used by main sessions and linked subagents, with errors, missing outcomes, and summed call time.',
        variant: 'figure',
        src: '/figures/subagents/tool_usage.png',
        slug: 'subagents/tool_usage',
      },
      {
        category: 'subagents',
        title: 'Subagent timing',
        blurb: 'Recorded elapsed time and the share spent with tool calls outstanding.',
        variant: 'stat',
        slug: 'subagents/timing',
      },
    ],
  },
  {
    key: 'llm_generation',
    title: 'LLM generation',
    description: 'Token composition, output length, output attribution, and end-to-end generation timing.',
    figures: [
      // --- paper §5 floats, in document order ---
      {
        category: 'llm_generation',
        title: 'Token length distribution',
        blurb: 'Prefix, append, and output token lengths per step, by provider.',
        variant: 'stat',
        slug: 'llm_generation/token_length_distribution',
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
        title: 'Append by prefix bin',
        blurb: 'How append length collapses as the cached prefix fills.',
        variant: 'stat',
        slug: 'llm_generation/append_by_prefix_bin',
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
        title: 'Output token distribution',
        blurb: "How long the agents' completions run.",
        variant: 'figure',
        src: '/figures/llm_generation/output_tokens_distribution.png',
        slug: 'llm_generation/output_tokens',
      },
      {
        category: 'llm_generation',
        title: 'Output attribution',
        blurb: 'Two ways a prior step’s output is accounted in the next step.',
        variant: 'figure',
        src: '/figures/llm_generation/output_attribution_schematic.png',
        slug: 'llm_generation/output_append_assignment',
      },
      {
        category: 'llm_generation',
        title: 'Previous output placement',
        blurb: 'Whether a long response returns as fresh append or cached-prefix growth.',
        variant: 'figure',
        src: '/figures/llm_generation/output_vs_next_append_scatter_min2000.png',
        slug: 'llm_generation/output_append_assignment',
      },
      {
        category: 'llm_generation',
        title: 'Context vs decode speed',
        blurb: 'Observed LLM timing against total input context length.',
        variant: 'figure',
        src: '/figures/llm_generation/context_decode_speed_scatter.png',
        slug: 'llm_generation/context_decode_speed_scatter',
      },
      // --- non-paper extras ---
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
        title: 'Adjusted append scatter',
        blurb: 'Fresh context after subtracting replayed prior output.',
        variant: 'figure',
        src: '/figures/llm_generation/prefix_vs_adjusted_append_sample.png',
        slug: 'llm_generation/adjusted_prefix_append',
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
        title: 'Generation-time CDF',
        blurb: 'Wall-clock time to produce a full response.',
        variant: 'figure',
        src: '/figures/llm_generation/llm_generation_time_count_cdf_by_provider.png',
        slug: 'llm_generation/generation_time_cdf',
      },
      {
        category: 'llm_generation',
        title: 'Generation total-time CDF',
        blurb: 'Where summed model-generation time accumulates.',
        variant: 'figure',
        src: '/figures/llm_generation/llm_generation_time_total_cdf_by_provider.png',
        slug: 'llm_generation/generation_time_cdf',
      },
    ],
  },
  {
    key: 'tool_calls',
    title: 'Tool calls',
    description: 'How agents choose tools, how often they call them, how long those calls take, and their overhead.',
    figures: [
      // --- paper §6 floats, in document order ---
      {
        category: 'tool_calls',
        title: 'Tool call counts by tool',
        blurb: 'Which tools the agents use the most.',
        variant: 'figure',
        src: '/figures/tool_calls/tool_call_counts.png',
        slug: 'tool_calls/tool_call_counts',
      },
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
        title: 'Tool latency distribution',
        blurb: 'Per-tool latency spread for the most-used tools.',
        variant: 'figure',
        src: '/figures/tool_calls/tool_latency_by_tool.png',
        slug: 'tool_calls/tool_latency_distribution',
      },
      {
        category: 'tool_calls',
        title: 'Codex tool overhead',
        blurb: 'Codex tool end-to-end time versus internal execution time.',
        variant: 'stat',
        slug: 'tool_calls/codex_wall_internal_gap',
      },
      // --- non-paper extras ---
      {
        category: 'tool_calls',
        title: 'Tool category distribution',
        blurb: 'How tool calls and latency split across coarse categories.',
        variant: 'figure',
        src: '/figures/tool_calls/tool_category_count_ring.png',
        slug: 'tool_calls/tool_category_distribution',
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
        category: 'tool_calls',
        title: 'Bash command breakdown',
        blurb: 'Which executables shell commands run, and how long each takes.',
        variant: 'figure',
        src: '/figures/tool_calls/executable_popularity.png',
        slug: 'tool_calls/bash_command_breakdown',
      },
    ],
  },
  {
    key: 'prefix_cache',
    title: 'Prefix cache',
    description: 'Cache reuse, idle-gap eviction, redundant prefill, and the share of context kept active.',
    figures: [
      // --- paper §7 floats, in document order ---
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
        category: 'prefix_cache',
        title: 'Redundant prefill',
        blurb: 'How much prefilled context is genuinely fresh versus replayed.',
        variant: 'stat',
        slug: 'prefix_cache/redundant_prefill',
      },
      {
        category: 'prefix_cache',
        title: 'Cost of human thinking time',
        blurb: 'Upper-bound savings if user-initiated steps kept their prefix cache.',
        variant: 'stat',
        slug: 'prefix_cache/human_idle_cache_counterfactual',
      },
      {
        category: 'prefix_cache',
        title: 'Eviction trade-off',
        blurb: 'Cache hit rate versus storage as the eviction timeout grows.',
        variant: 'figure',
        src: '/figures/prefix_cache/eviction_tradeoff_by_timeout.png',
        slug: 'prefix_cache/eviction_tradeoff',
      },
      // --- non-paper extras ---
      {
        category: 'prefix_cache',
        title: 'Cache hit ratio (append-weighted)',
        blurb: 'Prefix cache hit ratio weighted by appended tokens.',
        variant: 'figure',
        src: '/figures/prefix_cache/cache_hit_ratio_append_weighted_histogram.png',
        slug: 'prefix_cache/cache_hit_ratio',
      },
    ],
  },
];

/** Categories rendered with the sage tag tint (rest use terracotta). */
export const sageCategories = new Set(['llm_generation', 'session']);
