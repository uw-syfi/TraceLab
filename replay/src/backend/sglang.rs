use serde_json::Value;

use super::{
    string_at_paths, token_ids_at_paths, usage_cached_prompt_tokens, usage_usize_at_paths, Backend,
    BackendCapabilities, GenRequest, StreamEvent, Usage,
};

/// SGLang native `/generate` protocol.
///
/// SGLang's documentation defines `input_ids`, `stream`, `sampling_params.max_new_tokens`,
/// `sampling_params.temperature`, and `sampling_params.ignore_eos` for `/generate`.
/// Cache and token-id response fields are normalized conservatively and still must pass runtime
/// preflight before cache metrics are trusted.
pub(crate) struct SglangGenerateBackend;

impl Backend for SglangGenerateBackend {
    fn name(&self) -> &'static str {
        "sglang"
    }

    fn endpoint_suffix(&self) -> &str {
        "/generate"
    }

    fn capabilities(&self) -> BackendCapabilities {
        BackendCapabilities::strict_cache_replay()
    }

    fn build_payload(&self, req: &GenRequest) -> Value {
        serde_json::json!({
            "input_ids": req.prompt_ids,
            "stream": req.stream,
            "return_logprob": true,
            "top_logprobs_num": 0,
            "return_text_in_logprobs": false,
            "sampling_params": {
                "max_new_tokens": req.max_tokens,
                "temperature": req.temperature,
                "ignore_eos": true,
            },
        })
    }

    fn parse_event(&self, value: &Value) -> StreamEvent {
        let cumulative_text = string_at_paths(
            value,
            &[
                &["text"],
                &["output_text"],
                &["generated_text"],
                &["meta_info", "text"],
                &["meta_info", "output_text"],
            ],
        );
        let cumulative_token_ids = token_ids_at_paths(
            value,
            &[
                &["output_ids"],
                &["output_token_ids"],
                &["token_ids"],
                &["tokens"],
                &["meta_info", "output_ids"],
                &["meta_info", "output_token_ids"],
                &["meta_info", "token_ids"],
            ],
        )
        .or_else(|| {
            token_ids_from_logprob_paths(
                value,
                &[
                    &["output_token_logprobs"],
                    &["output_token_logprobs_idx"],
                    &["meta_info", "output_token_logprobs"],
                    &["meta_info", "output_token_logprobs_idx"],
                ],
            )
        });
        let finish_reason = finish_reason_at_paths(
            value,
            &[
                &["finish_reason"],
                &["stop_reason"],
                &["meta_info", "finish_reason"],
                &["meta_info", "stop_reason"],
            ],
        );
        let usage = parse_usage(value);
        StreamEvent {
            text_delta: None,
            cumulative_text,
            token_ids: None,
            cumulative_token_ids,
            finish_reason,
            usage,
        }
    }
}

fn finish_reason_at_paths(value: &Value, paths: &[&[&str]]) -> Option<String> {
    paths.iter().find_map(|path| {
        let value = value_at_path(value, path)?;
        if let Some(reason) = value.as_str() {
            return Some(reason.to_string());
        }
        let object = value.as_object()?;
        ["type", "reason", "matched", "message"]
            .iter()
            .find_map(|key| object.get(*key)?.as_str().map(str::to_string))
    })
}

fn parse_usage(value: &Value) -> Option<Usage> {
    let usage_source = value.get("usage").unwrap_or(value);
    let prompt_tokens = usage_usize_at_paths(
        usage_source,
        &[
            &["prompt_tokens"],
            &["input_tokens"],
            &["meta_info", "prompt_tokens"],
            &["meta_info", "input_tokens"],
        ],
    )
    .or_else(|| {
        usage_usize_at_paths(
            value,
            &[
                &["prompt_tokens"],
                &["input_tokens"],
                &["meta_info", "prompt_tokens"],
                &["meta_info", "input_tokens"],
            ],
        )
    });
    let completion_tokens = usage_usize_at_paths(
        usage_source,
        &[
            &["completion_tokens"],
            &["output_tokens"],
            &["meta_info", "completion_tokens"],
            &["meta_info", "output_tokens"],
        ],
    )
    .or_else(|| {
        usage_usize_at_paths(
            value,
            &[
                &["completion_tokens"],
                &["output_tokens"],
                &["meta_info", "completion_tokens"],
                &["meta_info", "output_tokens"],
            ],
        )
    });
    let total_tokens = usage_usize_at_paths(
        usage_source,
        &[&["total_tokens"], &["meta_info", "total_tokens"]],
    )
    .or_else(|| usage_usize_at_paths(value, &[&["total_tokens"], &["meta_info", "total_tokens"]]))
    .or_else(|| match (prompt_tokens, completion_tokens) {
        (Some(prompt), Some(completion)) => Some(prompt + completion),
        _ => None,
    });
    let cached_prompt_tokens =
        usage_cached_prompt_tokens(usage_source).or_else(|| usage_cached_prompt_tokens(value));

    if prompt_tokens.is_some()
        || completion_tokens.is_some()
        || total_tokens.is_some()
        || cached_prompt_tokens.is_some()
    {
        Some(Usage {
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cached_prompt_tokens,
        })
    } else {
        None
    }
}

fn token_ids_from_logprob_paths(value: &Value, paths: &[&[&str]]) -> Option<Vec<u32>> {
    paths
        .iter()
        .find_map(|path| token_ids_from_logprob_entries(value_at_path(value, path)?))
}

fn token_ids_from_logprob_entries(value: &Value) -> Option<Vec<u32>> {
    let ids = value
        .as_array()?
        .iter()
        .filter_map(token_id_from_logprob_entry)
        .collect::<Vec<_>>();
    (!ids.is_empty()).then_some(ids)
}

fn token_id_from_logprob_entry(value: &Value) -> Option<u32> {
    if let Some(id) = value.as_u64().and_then(|n| u32::try_from(n).ok()) {
        return Some(id);
    }
    if let Some(object) = value.as_object() {
        return ["token_id", "tokenId", "id", "token"]
            .iter()
            .find_map(|key| object.get(*key)?.as_u64()?.try_into().ok());
    }
    value
        .as_array()?
        .iter()
        .find_map(|item| item.as_u64()?.try_into().ok())
}

fn value_at_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut cursor = value;
    for key in path {
        cursor = cursor.get(*key)?;
    }
    Some(cursor)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_native_generate_payload_with_token_ids() {
        let backend = SglangGenerateBackend;
        let payload = backend.build_payload(&GenRequest {
            model: "qwen",
            prompt_ids: &[1, 2, 3],
            max_tokens: 8,
            temperature: 0.0,
            stream: true,
        });

        assert!(payload.get("model").is_none());
        assert_eq!(payload["input_ids"], serde_json::json!([1, 2, 3]));
        assert_eq!(payload["stream"], true);
        assert_eq!(payload["return_logprob"], true);
        assert_eq!(payload["top_logprobs_num"], 0);
        assert_eq!(payload["return_text_in_logprobs"], false);
        assert_eq!(payload["sampling_params"]["max_new_tokens"], 8);
        assert_eq!(payload["sampling_params"]["temperature"], 0.0);
        assert_eq!(payload["sampling_params"]["ignore_eos"], true);
    }

    #[test]
    fn parses_cumulative_text_and_usage() {
        let backend = SglangGenerateBackend;
        let event = backend.parse_event(&serde_json::json!({
            "text": "hello",
            "meta_info": {
                "prompt_tokens": 10,
                "output_tokens": 2,
                "cached_tokens": 7,
                "finish_reason": "length"
            }
        }));

        assert_eq!(event.cumulative_text.as_deref(), Some("hello"));
        assert_eq!(event.finish_reason.as_deref(), Some("length"));
        let usage = event.usage.expect("usage");
        assert_eq!(usage.prompt_tokens, Some(10));
        assert_eq!(usage.completion_tokens, Some(2));
        assert_eq!(usage.total_tokens, Some(12));
        assert_eq!(usage.cached_prompt_tokens, Some(7));
    }

    #[test]
    fn parses_output_token_ids_from_logprob_entries() {
        let backend = SglangGenerateBackend;
        let event = backend.parse_event(&serde_json::json!({
            "text": "hello",
            "meta_info": {
                "output_token_logprobs": [
                    [-0.1, 111, "he"],
                    {"token_id": 222, "logprob": -0.2}
                ]
            }
        }));

        assert_eq!(event.cumulative_token_ids, Some(vec![111, 222]));
    }

    #[test]
    fn parses_object_finish_reason() {
        let backend = SglangGenerateBackend;
        let event = backend.parse_event(&serde_json::json!({
            "text": "hello",
            "meta_info": {
                "finish_reason": {"type": "length"}
            }
        }));

        assert_eq!(event.finish_reason.as_deref(), Some("length"));
    }
}
