use serde::Serialize;

/// One JSONL output record describing a single replayed round.
#[derive(Debug, Serialize)]
pub(crate) struct StepLog {
    pub(crate) session_id: String,
    pub(crate) round_idx: usize,
    pub(crate) request_id: String,
    pub(crate) prefix_len: usize,
    pub(crate) input_len: usize,
    pub(crate) prompt_len: usize,
    pub(crate) planned_prefix_hit_rate: f64,
    pub(crate) output_len_target: usize,
    pub(crate) output_len_actual: usize,
    pub(crate) output_len_text_tokens: usize,
    pub(crate) server_prompt_tokens: Option<usize>,
    pub(crate) server_completion_tokens: Option<usize>,
    pub(crate) server_total_tokens: Option<usize>,
    pub(crate) server_cached_prompt_tokens: Option<usize>,
    pub(crate) server_uncached_prompt_tokens: Option<usize>,
    pub(crate) server_prefix_hit_rate: Option<f64>,
    pub(crate) server_prefix_hit_rate_delta: Option<f64>,
    pub(crate) finish_reason: Option<String>,
    pub(crate) tool_wait_after_ms: f64,
    pub(crate) arrival_time_ms: f64,
    /// Wall-clock (unix seconds) when the round entered the client, before prompt decode.
    pub(crate) submit_timestamp: f64,
    /// Wall-clock (unix seconds) right before the HTTP request was sent. None if never sent.
    pub(crate) post_timestamp: Option<f64>,
    /// Wall-clock (unix seconds) when the response finished, or the round failed/was skipped.
    pub(crate) complete_timestamp: f64,
    /// Time to first token, measured from the send instant (`post_timestamp`), so it excludes
    /// client-side prompt decode. None if no token arrived.
    pub(crate) first_token_ms: Option<f64>,
    /// End-to-end duration from `submit_timestamp` (includes client-side prompt decode).
    pub(crate) total_duration_ms: f64,
    pub(crate) chunk_count: usize,
    pub(crate) status: String,
    pub(crate) output_preview: String,
    pub(crate) error: Option<String>,
}
