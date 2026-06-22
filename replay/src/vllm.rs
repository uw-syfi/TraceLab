use anyhow::Result;
use bytes::BytesMut;
use futures::StreamExt;
use serde_json::Value;
use std::sync::Arc;
use std::time::{Duration, SystemTime};
use tokenizers::Tokenizer;
use tokio::time::timeout;

use crate::cli::Args;
use crate::record::StepLog;
use crate::trace::SessionStep;
use crate::util::{elapsed_ms, prefix_hit_rate, ratio, unix_seconds_now};

/// Streaming client for vLLM's OpenAI-compatible `/completions` endpoint.
pub(crate) struct VllmClient {
    endpoint: String,
    client: reqwest::Client,
    tokenizer: Arc<Tokenizer>,
    model: String,
    temperature: f64,
    ignore_eos: bool,
    include_stream_usage: bool,
    assume_missing_cache_details_zero: bool,
    stream_idle_timeout_secs: u64,
}

impl VllmClient {
    pub(crate) fn new(args: &Args, tokenizer: Arc<Tokenizer>) -> Result<Self> {
        let endpoint = format!("{}/completions", args.base_url.trim_end_matches('/'));
        let client = reqwest::Client::builder()
            .pool_max_idle_per_host(20_000)
            .tcp_nodelay(true)
            .timeout(Duration::from_secs(3600))
            .build()?;
        Ok(Self {
            endpoint,
            client,
            tokenizer,
            model: args.model.clone(),
            temperature: args.temperature,
            ignore_eos: args.ignore_eos,
            include_stream_usage: !args.disable_stream_usage,
            assume_missing_cache_details_zero: args.assume_missing_cache_details_zero,
            stream_idle_timeout_secs: args.stream_idle_timeout_secs,
        })
    }

    pub(crate) async fn run_step(
        &self,
        step: &SessionStep,
        request_id: String,
        prompt_ids: &[u32],
    ) -> StepLog {
        let submit_timestamp = unix_seconds_now();
        let start = SystemTime::now();
        let prompt = match self.tokenizer.decode(prompt_ids, false) {
            Ok(text) => text,
            Err(err) => {
                return StepLog {
                    session_id: step.session_id.clone(),
                    round_idx: step.round_idx,
                    request_id,
                    prefix_len: step.prefix_len,
                    input_len: step.input_len,
                    prompt_len: prompt_ids.len(),
                    planned_prefix_hit_rate: prefix_hit_rate(step.prefix_len, prompt_ids.len()),
                    output_len_target: step.output_len,
                    output_len_actual: 0,
                    output_len_text_tokens: 0,
                    server_prompt_tokens: None,
                    server_completion_tokens: None,
                    server_total_tokens: None,
                    server_cached_prompt_tokens: None,
                    server_uncached_prompt_tokens: None,
                    server_prefix_hit_rate: None,
                    server_prefix_hit_rate_delta: None,
                    finish_reason: None,
                    tool_wait_after_ms: step.tool_wait_after_ms,
                    arrival_time_ms: step.arrival_time,
                    submit_timestamp,
                    post_timestamp: None,
                    first_token_ms: None,
                    total_duration_ms: elapsed_ms(start),
                    chunk_count: 0,
                    status: "FAILED".to_string(),
                    output_preview: String::new(),
                    error: Some(format!("prompt decode failed: {err}")),
                };
            }
        };

        let mut payload = serde_json::json!({
            "model": self.model.as_str(),
            "prompt": prompt,
            "max_tokens": step.output_len,
            "temperature": self.temperature,
            "stream": true,
        });
        if self.ignore_eos {
            payload["ignore_eos"] = serde_json::json!(true);
        }
        if self.include_stream_usage {
            payload["stream_options"] = serde_json::json!({"include_usage": true});
        }

        let post_timestamp = Some(unix_seconds_now());
        let mut first_token_ms = None;
        let mut chunk_count = 0usize;
        let mut output_text = String::new();
        let mut status = "SUCCESS".to_string();
        let mut error = None;
        let mut finish_reason = None;
        let mut server_prompt_tokens = None;
        let mut server_completion_tokens = None;
        let mut server_total_tokens = None;
        let mut server_cached_prompt_tokens = None;

        let response = self
            .client
            .post(&self.endpoint)
            .header("x-request-id", &request_id)
            .json(&payload)
            .send()
            .await;

        match response {
            Ok(response) if response.status().is_success() => {
                let mut stream = response.bytes_stream();
                let mut buffer = BytesMut::with_capacity(8192);
                let mut done = false;

                while !done {
                    match timeout(
                        Duration::from_secs(self.stream_idle_timeout_secs),
                        stream.next(),
                    )
                    .await
                    {
                        Ok(Some(Ok(chunk))) => {
                            buffer.extend_from_slice(&chunk);
                            while let Some(idx) = buffer.iter().position(|&b| b == b'\n') {
                                let line_bytes = buffer.split_to(idx + 1);
                                let line = String::from_utf8_lossy(&line_bytes);
                                let line = line.trim();
                                if !line.starts_with("data: ") {
                                    continue;
                                }
                                let data = line.trim_start_matches("data: ").trim();
                                if data == "[DONE]" {
                                    done = true;
                                    break;
                                }
                                if let Ok(value) = serde_json::from_str::<Value>(data) {
                                    if let Some(delta) = completion_text_delta(&value) {
                                        if !delta.is_empty() {
                                            if first_token_ms.is_none() {
                                                first_token_ms = Some(elapsed_ms(start));
                                            }
                                            output_text.push_str(delta);
                                        }
                                    }
                                    if let Some(reason) = completion_finish_reason(&value) {
                                        finish_reason = Some(reason.to_string());
                                    }
                                    if let Some(usage) = value.get("usage") {
                                        server_prompt_tokens = usage_usize(usage, "prompt_tokens")
                                            .or(server_prompt_tokens);
                                        server_completion_tokens =
                                            usage_usize(usage, "completion_tokens")
                                                .or(server_completion_tokens);
                                        server_total_tokens = usage_usize(usage, "total_tokens")
                                            .or(server_total_tokens);
                                        server_cached_prompt_tokens =
                                            usage_cached_prompt_tokens(usage)
                                                .or(server_cached_prompt_tokens);
                                    }
                                    chunk_count += 1;
                                }
                            }
                        }
                        Ok(Some(Err(err))) => {
                            status = "FAILED".to_string();
                            error = Some(format!("stream error: {err}"));
                            break;
                        }
                        Ok(None) => break,
                        Err(_) => {
                            status = "FAILED".to_string();
                            error = Some(format!(
                                "stream idle timeout after {}s",
                                self.stream_idle_timeout_secs
                            ));
                            break;
                        }
                    }
                }
            }
            Ok(response) => {
                status = "FAILED".to_string();
                error = Some(format!("HTTP {}", response.status()));
            }
            Err(err) => {
                status = "FAILED".to_string();
                error = Some(format!("request error: {err}"));
            }
        }

        let output_len_text_tokens = self
            .tokenizer
            .encode(output_text.clone(), false)
            .map(|encoding| encoding.len())
            .unwrap_or(0);
        let output_len_actual = server_completion_tokens.unwrap_or(output_len_text_tokens);
        if self.include_stream_usage
            && server_cached_prompt_tokens.is_none()
            && server_prompt_tokens.is_some()
            && self.assume_missing_cache_details_zero
        {
            server_cached_prompt_tokens = Some(0);
        }
        let prompt_len = prompt_ids.len();
        let planned_prefix_hit_rate = prefix_hit_rate(step.prefix_len, prompt_len);
        let server_uncached_prompt_tokens =
            match (server_prompt_tokens, server_cached_prompt_tokens) {
                (Some(prompt), Some(cached)) => Some(prompt.saturating_sub(cached)),
                _ => None,
            };
        let server_prefix_hit_rate = match (server_cached_prompt_tokens, server_prompt_tokens) {
            (Some(cached), Some(prompt)) => ratio(cached, prompt),
            _ => None,
        };
        let server_prefix_hit_rate_delta =
            server_prefix_hit_rate.map(|actual| actual - planned_prefix_hit_rate);

        StepLog {
            session_id: step.session_id.clone(),
            round_idx: step.round_idx,
            request_id,
            prefix_len: step.prefix_len,
            input_len: step.input_len,
            prompt_len,
            planned_prefix_hit_rate,
            output_len_target: step.output_len,
            output_len_actual,
            output_len_text_tokens,
            server_prompt_tokens,
            server_completion_tokens,
            server_total_tokens,
            server_cached_prompt_tokens,
            server_uncached_prompt_tokens,
            server_prefix_hit_rate,
            server_prefix_hit_rate_delta,
            finish_reason,
            tool_wait_after_ms: step.tool_wait_after_ms,
            arrival_time_ms: step.arrival_time,
            submit_timestamp,
            post_timestamp,
            first_token_ms,
            total_duration_ms: elapsed_ms(start),
            chunk_count,
            status,
            output_preview: output_text.chars().take(100).collect(),
            error,
        }
    }
}

fn completion_text_delta(value: &Value) -> Option<&str> {
    value
        .get("choices")?
        .as_array()?
        .first()?
        .get("text")?
        .as_str()
}

fn completion_finish_reason(value: &Value) -> Option<&str> {
    value
        .get("choices")?
        .as_array()?
        .first()?
        .get("finish_reason")?
        .as_str()
}

fn usage_usize(usage: &Value, key: &str) -> Option<usize> {
    usage
        .get(key)?
        .as_u64()
        .and_then(|value| value.try_into().ok())
}

fn usage_cached_prompt_tokens(usage: &Value) -> Option<usize> {
    [
        &["prompt_tokens_details", "cached_tokens"][..],
        &["cached_tokens"][..],
        &["cached_input_tokens"][..],
        &["cache_read_input_tokens"][..],
        &["prompt_cached_tokens"][..],
        &["num_cached_tokens"][..],
    ]
    .into_iter()
    .find_map(|path| value_at_path(usage, path)?.as_u64()?.try_into().ok())
}

fn value_at_path<'a>(value: &'a Value, path: &[&str]) -> Option<&'a Value> {
    let mut cursor = value;
    for key in path {
        cursor = cursor.get(*key)?;
    }
    Some(cursor)
}
