use anyhow::{anyhow, Context, Result};
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

use super::{ensure_cache_preflight_supported, sse_data, Backend, GenRequest, StreamEvent, Usage};

/// Result of replaying one round: the log record plus the model's output token ids. The caller
/// carries the output ids forward as the next round's context so the previous-output region of
/// the next prefix matches what the server cached and stays prefix-cache-hittable.
pub(crate) struct StepOutcome {
    pub(crate) log: StepLog,
    pub(crate) output_ids: Vec<u32>,
}

struct ProbeOutcome {
    usage: Option<Usage>,
    generated_token_ids: Vec<u32>,
}

/// Shared streaming engine. Owns the HTTP client, tokenizer, and pluggable backend adapter.
pub(crate) struct GenerationClient {
    endpoint: String,
    client: reqwest::Client,
    tokenizer: Arc<Tokenizer>,
    model: String,
    temperature: f64,
    stream_idle_timeout_secs: u64,
    backend: Box<dyn Backend>,
}

impl GenerationClient {
    pub(crate) fn new(args: &Args, tokenizer: Arc<Tokenizer>) -> Result<Self> {
        let backend = super::build_backend(args.backend)?;
        let endpoint = format!(
            "{}{}",
            args.base_url.trim_end_matches('/'),
            backend.endpoint_suffix()
        );
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
            stream_idle_timeout_secs: args.stream_idle_timeout_secs,
            backend,
        })
    }

    pub(crate) async fn run_step(
        &self,
        step: &SessionStep,
        request_id: String,
        prompt_ids: &[u32],
    ) -> StepOutcome {
        let submit_timestamp = unix_seconds_now();
        let start = SystemTime::now();
        let payload = self.backend.build_payload(&GenRequest {
            model: &self.model,
            prompt_ids,
            max_tokens: step.output_len,
            temperature: self.temperature,
            stream: true,
        });

        let post_timestamp = Some(unix_seconds_now());
        let send_instant = SystemTime::now();
        let mut first_token_ms = None;
        let mut chunk_count = 0usize;
        let mut output_text = String::new();
        let mut output_token_ids: Vec<u32> = Vec::new();
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
                                let Some(data) = sse_data(&line) else {
                                    continue;
                                };
                                if data == "[DONE]" {
                                    done = true;
                                    break;
                                }
                                if let Ok(value) = serde_json::from_str::<Value>(data) {
                                    let event = self.backend.parse_event(&value);
                                    append_text_event(
                                        &event,
                                        &mut output_text,
                                        &mut first_token_ms,
                                        send_instant,
                                    );
                                    append_token_event(&event, &mut output_token_ids);
                                    if let Some(reason) = event.finish_reason {
                                        finish_reason = Some(reason);
                                    }
                                    if let Some(usage) = event.usage {
                                        server_prompt_tokens =
                                            usage.prompt_tokens.or(server_prompt_tokens);
                                        server_completion_tokens =
                                            usage.completion_tokens.or(server_completion_tokens);
                                        server_total_tokens =
                                            usage.total_tokens.or(server_total_tokens);
                                        server_cached_prompt_tokens = usage
                                            .cached_prompt_tokens
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

        let reencoded_output_ids: Vec<u32> = self
            .tokenizer
            .encode(output_text.clone(), false)
            .map(|encoding| encoding.get_ids().to_vec())
            .unwrap_or_default();
        let output_len_text_tokens = reencoded_output_ids.len();
        let output_len_actual = server_completion_tokens.unwrap_or(output_len_text_tokens);
        let output_token_ids_valid = !output_token_ids.is_empty()
            && server_completion_tokens.is_none_or(|n| output_token_ids.len() == n);
        let expected_token_ids = server_completion_tokens
            .or_else(|| (output_len_text_tokens > 0).then_some(output_len_text_tokens));

        if self.backend.capabilities().returns_generated_token_ids
            && expected_token_ids.is_some_and(|n| n > 0)
            && !output_token_ids_valid
        {
            status = "FAILED".to_string();
            error = Some(format!(
                "{} returned {} generated token ids, expected {:?}; exact session \
                 carry-forward is unavailable",
                self.backend.name(),
                output_token_ids.len(),
                expected_token_ids
            ));
        }
        let output_ids: Vec<u32> = if output_token_ids_valid {
            output_token_ids
        } else {
            reencoded_output_ids
        };

        if server_cached_prompt_tokens.is_none() && server_prompt_tokens.is_some() {
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

        StepOutcome {
            log: StepLog {
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
                complete_timestamp: unix_seconds_now(),
                first_token_ms,
                total_duration_ms: elapsed_ms(start),
                chunk_count,
                status,
                output_preview: output_text.chars().take(100).collect(),
                error,
            },
            output_ids,
        }
    }

    /// Abort early unless the server reports both cache hits and generated token ids.
    pub(crate) async fn preflight_cache_check(&self, probe_ids: &[u32]) -> Result<()> {
        let capabilities = self.backend.capabilities();
        ensure_cache_preflight_supported(self.backend.name(), capabilities)?;

        self.post_probe(probe_ids)
            .await
            .context("preflight warm-up request failed")?;
        let usage = self
            .post_probe(probe_ids)
            .await
            .context("preflight cache-hit request failed")?;

        let token_id_count = usage.generated_token_ids.len();
        let usage = usage.usage.ok_or_else(|| {
            anyhow!(
                "preflight: server response carried no usage block; cannot verify prefix-cache reporting"
            )
        })?;
        if capabilities.returns_generated_token_ids && token_id_count == 0 {
            return Err(anyhow!(
                "preflight: {} reported no generated token ids; exact session carry-forward \
                 would rely on text re-encoding",
                self.backend.name()
            ));
        }
        match usage.cached_prompt_tokens {
            Some(cached) if cached > 0 => Ok(()),
            other => Err(anyhow!(
                "preflight: {} reported no prefix-cache hit (prompt_tokens={:?}, cached_tokens={:?}). \
                 Launch the server with cache accounting enabled, or use a backend that exposes \
                 cached prompt tokens.",
                self.backend.name(), usage.prompt_tokens, other
            )),
        }
    }

    async fn post_probe(&self, prompt_ids: &[u32]) -> Result<ProbeOutcome> {
        let payload = self.backend.build_payload(&GenRequest {
            model: &self.model,
            prompt_ids,
            max_tokens: 1,
            temperature: 0.0,
            stream: false,
        });
        let response = self
            .client
            .post(&self.endpoint)
            .json(&payload)
            .send()
            .await
            .map_err(|err| anyhow!("request error: {err}"))?;
        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(anyhow!(
                "HTTP {status}: {}",
                body.chars().take(200).collect::<String>()
            ));
        }
        let body: Value = response
            .json()
            .await
            .map_err(|err| anyhow!("invalid JSON response: {err}"))?;
        let event = self.backend.parse_event(&body);
        Ok(ProbeOutcome {
            generated_token_ids: event_generated_token_ids(&event),
            usage: event.usage,
        })
    }
}

fn append_text_event(
    event: &StreamEvent,
    output_text: &mut String,
    first_token_ms: &mut Option<f64>,
    send_instant: SystemTime,
) {
    if let Some(delta) = &event.text_delta {
        if !delta.is_empty() {
            if first_token_ms.is_none() {
                *first_token_ms = Some(elapsed_ms(send_instant));
            }
            output_text.push_str(delta);
        }
    }
    if let Some(cumulative) = &event.cumulative_text {
        if let Some(delta) = cumulative.strip_prefix(output_text.as_str()) {
            if !delta.is_empty() {
                if first_token_ms.is_none() {
                    *first_token_ms = Some(elapsed_ms(send_instant));
                }
                output_text.push_str(delta);
            }
        } else if cumulative != output_text {
            if first_token_ms.is_none() && !cumulative.is_empty() {
                *first_token_ms = Some(elapsed_ms(send_instant));
            }
            output_text.clear();
            output_text.push_str(cumulative);
        }
    }
}

fn append_token_event(event: &StreamEvent, output_token_ids: &mut Vec<u32>) {
    if let Some(ids) = &event.token_ids {
        output_token_ids.extend(ids);
    }
    if let Some(ids) = &event.cumulative_token_ids {
        if ids.len() >= output_token_ids.len()
            && ids
                .get(..output_token_ids.len())
                .is_some_and(|prefix| prefix == output_token_ids.as_slice())
        {
            output_token_ids.extend_from_slice(&ids[output_token_ids.len()..]);
        } else {
            output_token_ids.clear();
            output_token_ids.extend_from_slice(ids);
        }
    }
}

fn event_generated_token_ids(event: &StreamEvent) -> Vec<u32> {
    if let Some(ids) = &event.cumulative_token_ids {
        return ids.clone();
    }
    event.token_ids.clone().unwrap_or_default()
}
