mod capabilities;
mod client;
mod llamacpp;
mod preflight;
mod sglang;
mod stream;
mod vllm;

use anyhow::Result;
use serde_json::Value;

use crate::cli::BackendKind;

pub(crate) use capabilities::BackendCapabilities;
pub(crate) use client::{GenerationClient, StepOutcome};
pub(super) use preflight::ensure_cache_preflight_supported;
pub(super) use stream::sse_data;

/// Normalized, backend-agnostic description of one generation request.
pub(crate) struct GenRequest<'a> {
    pub(crate) model: &'a str,
    pub(crate) prompt_ids: &'a [u32],
    pub(crate) max_tokens: usize,
    pub(crate) temperature: f64,
    pub(crate) stream: bool,
}

/// Server-reported token accounting, normalized across wire formats.
pub(crate) struct Usage {
    pub(crate) prompt_tokens: Option<usize>,
    pub(crate) completion_tokens: Option<usize>,
    pub(crate) total_tokens: Option<usize>,
    pub(crate) cached_prompt_tokens: Option<usize>,
}

/// Normalized view of one streamed response object (or a full non-streaming body).
pub(crate) struct StreamEvent {
    pub(crate) text_delta: Option<String>,
    /// Full generated text so far, for backends that stream cumulative text rather than deltas.
    pub(crate) cumulative_text: Option<String>,
    /// Exact generated token ids for this chunk, when the server echoes them.
    pub(crate) token_ids: Option<Vec<u32>>,
    /// Full generated token ids so far, for backends that stream cumulative token-id arrays.
    pub(crate) cumulative_token_ids: Option<Vec<u32>>,
    pub(crate) finish_reason: Option<String>,
    pub(crate) usage: Option<Usage>,
}

/// Per-backend wire-protocol adapter. Pure and synchronous: it only shapes JSON, so the
/// shared async streaming engine in `GenerationClient` stays backend-agnostic and `dyn Backend`
/// remains object-safe (no `async-trait`).
pub(crate) trait Backend: Send + Sync {
    /// Human-readable backend label used in diagnostics.
    fn name(&self) -> &'static str;
    /// Path appended to `--base-url` to form the request endpoint.
    fn endpoint_suffix(&self) -> &str;
    /// Static capabilities the adapter expects to provide. Runtime preflight still validates them.
    fn capabilities(&self) -> BackendCapabilities;
    /// Shape one generation request into this backend's request body.
    fn build_payload(&self, req: &GenRequest) -> Value;
    /// Normalize one response JSON object (a stream chunk or a full body).
    fn parse_event(&self, value: &Value) -> StreamEvent;
}

/// Build the backend adapter selected on the command line.
pub(crate) fn build_backend(kind: BackendKind) -> Result<Box<dyn Backend>> {
    match kind {
        BackendKind::Vllm => Ok(Box::new(vllm::VllmBackend)),
        BackendKind::Sglang => Ok(Box::new(sglang::SglangGenerateBackend)),
        BackendKind::Llamacpp => llamacpp::not_implemented(),
    }
}

pub(super) fn usage_usize(usage: &Value, key: &str) -> Option<usize> {
    usage
        .get(key)?
        .as_u64()
        .and_then(|value| value.try_into().ok())
}

pub(super) fn usage_usize_at_paths(value: &Value, paths: &[&[&str]]) -> Option<usize> {
    paths
        .iter()
        .find_map(|path| value_at_path(value, path)?.as_u64()?.try_into().ok())
}

pub(super) fn usage_cached_prompt_tokens(value: &Value) -> Option<usize> {
    [
        &["prompt_tokens_details", "cached_tokens"][..],
        &["cached_tokens"][..],
        &["cached_input_tokens"][..],
        &["cache_read_input_tokens"][..],
        &["prompt_cached_tokens"][..],
        &["num_cached_tokens"][..],
        &["meta_info", "cached_tokens"][..],
        &["meta_info", "cached_prompt_tokens"][..],
        &["meta_info", "cache_read_input_tokens"][..],
        &["meta_info", "cache_read_tokens"][..],
        &["meta_info", "prefix_cache_hit_tokens"][..],
        &["meta_info", "num_cached_tokens"][..],
    ]
    .into_iter()
    .find_map(|path| value_at_path(value, path)?.as_u64()?.try_into().ok())
}

pub(super) fn token_ids_at_paths(value: &Value, paths: &[&[&str]]) -> Option<Vec<u32>> {
    paths.iter().find_map(|path| {
        let ids = value_at_path(value, path)?
            .as_array()?
            .iter()
            .map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
            .collect::<Option<Vec<u32>>>()?;
        (!ids.is_empty()).then_some(ids)
    })
}

pub(super) fn string_at_paths(value: &Value, paths: &[&[&str]]) -> Option<String> {
    paths
        .iter()
        .find_map(|path| value_at_path(value, path)?.as_str().map(str::to_string))
}

fn value_at_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut cursor = value;
    for key in path {
        cursor = cursor.get(*key)?;
    }
    Some(cursor)
}
