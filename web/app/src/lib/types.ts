// Typed subset of the toolkit's summary.json (produced by overview_summary).
// Only the fields the UI consumes are typed; the file has many more.

export interface Scope {
  total_sessions: number;
  distinct_users: number;
  llm_rounds_total: number;
  rounds_with_visible_user_message: number;
  rounds_started_from_tool_result: number;
  earliest_observed_timestamp: string;
  latest_observed_timestamp: string;
}

export interface GrowthBucket {
  rounds: number;
  positive_growth_rounds: number;
  zero_growth_rounds: number;
  negative_growth_rounds: number;
  micro_reduction_rounds: number;
  ordinary_reduction_rounds: number;
  positive_growth_share: number;
  zero_growth_share: number;
  negative_growth_share: number;
  micro_reduction_share: number;
  ordinary_reduction_share: number;
  major_compact_share: number;
  major_compact_rounds: number;
  total_context_increase_tokens: number;
  total_raw_delta_tokens: number;
  total_reduction_tokens: number;
  average_raw_delta_tokens: number;
  median_raw_delta_tokens: number;
  p10_raw_delta_tokens: number;
  p90_raw_delta_tokens: number;
  average_positive_growth_tokens: number;
  average_clipped_growth_tokens: number;
  average_reduction_tokens: number;
  max_reduction_tokens: number;
}

export interface TokensInput {
  total_input_tokens: number;
  new_input_tokens: number;
  cached_read_input_tokens: number;
  average_total_input_tokens_per_round: number;
  average_new_input_tokens_per_round: number;
  average_cached_read_input_tokens_per_round: number;
  rounds_started_with_user_message_for_input_token_average: number;
  average_total_input_tokens_when_started_with_user_message: number;
  average_new_input_tokens_when_started_with_user_message: number;
  total_new_input_tokens_when_started_with_user_message: number;
  average_new_input_tokens_when_started_with_tool_result: number;
  average_total_input_tokens_when_started_with_tool_result: number;
  total_new_input_tokens_when_started_with_tool_result: number;
  prefix_hit_rate_when_started_with_user_message: number;
  prefix_hit_rate_when_started_with_tool_result: number;
  user_context_delta_rounds: number;
  average_user_context_delta_tokens: number;
  median_user_context_delta_tokens: number;
  p90_user_context_delta_tokens: number;
  tool_result_context_delta_rounds: number;
  average_tool_result_context_delta_tokens: number;
  median_tool_result_context_delta_tokens: number;
  p90_tool_result_context_delta_tokens: number;
  total_context_increase_tokens: number;
  prefix_hit_rate: number;
  total_input_growth_when_started_with_tool_result: GrowthBucket;
  total_input_growth_when_started_with_user_message: GrowthBucket;
}

export interface TokensOutput {
  total_output_tokens_including_reasoning: number;
  visible_or_structured_output_tokens_estimate: number;
  rounds_with_observed_reasoning: number;
  reasoning_output_tokens_subset: number;
  rounds_with_positive_reasoning_output_tokens: number;
  average_output_tokens_including_reasoning_per_round: number;
}

export interface PostReasoningTpotEstimate {
  rounds: number;
  visible_or_structured_output_tokens: number;
  post_reasoning_output_decode_time_seconds: number | null;
  average_decode_speed_tokens_per_second: number | null;
  average_decode_latency_seconds_per_token: number | null;
}

export interface EstimatedTtftFromExactReasoningTokens {
  rounds: number;
  input_to_reasoning_end_total_seconds: number;
  reasoning_tokens: number;
  decode_latency_seconds_per_token_used: number | null;
  decode_speed_tokens_per_second_used: number | null;
  rounds_used_to_estimate_decode_latency: number;
  estimated_total_seconds: number | null;
  estimated_average_seconds: number | null;
}

export interface GenerationTiming {
  total_observable_generation_time_seconds: number;
  rounds_with_observable_generation_time: number;
  p50_observable_generation_time_seconds: number | null;
  p90_observable_generation_time_seconds: number | null;
  average_normalized_decoding_speed_tokens_per_second: number;
  rounds_used_for_normalized_decoding_speed: number;
  total_input_to_reasoning_end_time_seconds: number;
  rounds_with_input_to_reasoning_end_time: number;
  rounds_with_user_message_before_model_output: number;
  rounds_with_waiting_for_human_input_time: number;
  total_waiting_for_human_input_seconds: number;
  average_waiting_for_human_input_seconds: number | null;
  median_waiting_for_human_input_seconds: number | null;
  p90_waiting_for_human_input_seconds: number | null;
  post_reasoning_tpot_estimate: PostReasoningTpotEstimate;
  estimated_ttft_from_exact_reasoning_tokens: EstimatedTtftFromExactReasoningTokens;
}

export interface EffectiveLatency {
  total_seconds: number;
  tool_calls_with_latency: number;
  tool_calls_missing_latency: number;
  tool_calls_nonpositive_latency: number;
  p50_seconds: number | null;
  p90_seconds: number | null;
  tool_calls_using_internal_latency: number;
  tool_calls_using_wall_latency_fallback: number;
  tool_calls_using_legacy_latency_fallback?: number;
}

export interface Tools {
  total_tool_calls: number;
  rounds_with_tool_calls: number;
  tool_calls_per_visible_user_message_round: number | null;
  effective_latency: EffectiveLatency;
}

export interface ProviderSummary {
  scope: Scope;
  tokens: { input: TokensInput; output: TokensOutput };
  generation_timing: GenerationTiming;
  tools: Tools;
  rounds_by_provider: Record<string, number>;
  rounds_by_model: Record<string, number>;
}

export interface Summary {
  merged: ProviderSummary;
  // overview_summary only emits a key for providers actually present in the trace, so a
  // single-provider trace may omit one of these.
  claude?: ProviderSummary;
  codex?: ProviderSummary;
}
