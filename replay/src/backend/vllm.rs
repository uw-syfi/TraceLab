use serde_json::Value;

use super::{
    token_ids_at_paths, usage_cached_prompt_tokens, usage_usize, Backend, BackendCapabilities,
    GenRequest, StreamEvent, Usage,
};

/// vLLM adapter using its OpenAI-compatible `/completions` protocol.
pub(crate) struct VllmBackend;

impl Backend for VllmBackend {
    fn name(&self) -> &'static str {
        "vllm"
    }

    fn endpoint_suffix(&self) -> &str {
        "/completions"
    }

    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities::strict_cache_replay()
    }

    fn build_payload(&self, req: &GenRequest) -> Value {
        let mut payload = serde_json::json!({
            "model": req.model,
            "prompt": req.prompt_ids,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": req.stream,
            "ignore_eos": true,
            "return_token_ids": true,
        });
        if req.stream {
            payload["stream_options"] = serde_json::json!({"include_usage": true});
        }
        payload
    }

    fn parse_event(&self, value: &Value) -> StreamEvent {
        let choice = value
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|choices| choices.first());
        let text_delta = choice
            .and_then(|c| c.get("text"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let token_ids = choice.and_then(|choice| token_ids_at_paths(choice, &[&["token_ids"]]));
        let finish_reason = choice
            .and_then(|c| c.get("finish_reason"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let usage = value.get("usage").map(|usage| Usage {
            prompt_tokens: usage_usize(usage, "prompt_tokens"),
            completion_tokens: usage_usize(usage, "completion_tokens"),
            total_tokens: usage_usize(usage, "total_tokens"),
            cached_prompt_tokens: usage_cached_prompt_tokens(usage),
        });
        StreamEvent {
            text_delta,
            cumulative_text: None,
            token_ids,
            cumulative_token_ids: None,
            finish_reason,
            usage,
        }
    }
}
