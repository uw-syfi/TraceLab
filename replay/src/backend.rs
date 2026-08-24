use anyhow::{anyhow, Context, Result};
use bytes::BytesMut;
use futures::StreamExt;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION};
use serde_json::{Map, Value};
use std::sync::Arc;
use std::time::{Duration, SystemTime};
use tokenizers::Tokenizer;
use tokio::time::timeout;

use crate::cli::{Args, BackendKind};
use crate::record::StepLog;
use crate::trace::SessionStep;
use crate::util::{elapsed_ms, prefix_hit_rate, ratio, unix_seconds_now};

/// Normalized, backend-agnostic description of one generation request.
pub(crate) struct GenRequest<'a> {
    pub(crate) model: &'a str,
    pub(crate) input: GenInput<'a>,
    pub(crate) max_tokens: usize,
    pub(crate) temperature: f64,
    pub(crate) stream: bool,
    pub(crate) extra_body: Option<&'a Map<String, Value>>,
}

#[derive(Clone, Copy)]
pub(crate) enum GenInput<'a> {
    PromptIds(&'a [u32]),
    ChatMessages(&'a [ChatMessage]),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum ChatRole {
    System,
    User,
    Assistant,
}

impl ChatRole {
    fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::User => "user",
            Self::Assistant => "assistant",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct ChatMessage {
    pub(crate) role: ChatRole,
    pub(crate) content: Arc<str>,
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
    pub(crate) token_delta_observed: bool,
    /// Exact generated token ids for this chunk, when the server echoes them
    /// (vLLM `return_token_ids`). Lets us carry the real output forward without re-tokenizing.
    pub(crate) token_ids: Option<Vec<u32>>,
    pub(crate) finish_reason: Option<String>,
    pub(crate) usage: Option<Usage>,
}

/// Per-backend wire-protocol adapter. Pure and synchronous: it only shapes JSON, so the
/// shared async streaming engine in `GenerationClient` stays backend-agnostic and `dyn Backend`
/// remains object-safe (no `async-trait`).
pub(crate) trait Backend: Send + Sync {
    /// Path appended to `--base-url` to form the request endpoint.
    fn endpoint_suffix(&self) -> &str;
    /// Shape one generation request into this backend's request body.
    fn build_payload(&self, req: &GenRequest) -> Value;
    /// Normalize one response JSON object (a stream chunk or a full body).
    fn parse_event(&self, value: &Value) -> StreamEvent;
}

/// Build the backend adapter selected on the command line.
pub(crate) fn build_backend(kind: BackendKind) -> Box<dyn Backend> {
    match kind {
        BackendKind::Openai => Box::new(OpenAiCompletionsBackend),
        BackendKind::Chat => Box::new(OpenAiChatCompletionsBackend),
    }
}

/// OpenAI-compatible `/completions` protocol. Works against vLLM and SGLang's OpenAI endpoint.
pub(crate) struct OpenAiCompletionsBackend;

impl Backend for OpenAiCompletionsBackend {
    fn endpoint_suffix(&self) -> &str {
        "/completions"
    }

    fn build_payload(&self, req: &GenRequest) -> Value {
        let GenInput::PromptIds(prompt_ids) = req.input else {
            unreachable!("completion backend requires prompt token ids")
        };
        let mut payload = serde_json::json!({
            "model": req.model,
            // Submit raw token ids (OpenAI `prompt` accepts an int array): no client-side decode,
            // and the server uses the exact ids so prefix-cache keys match what we constructed.
            "prompt": prompt_ids,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": req.stream,
            // Always run decode to the trace's target length; synthetic prompts otherwise emit
            // EOS almost immediately and collapse the decode workload.
            "ignore_eos": true,
            // Echo the generated token ids (recent vLLM) so we carry the real output forward
            // exactly. Older servers ignore this; we fall back to re-encoding the output text.
            "return_token_ids": true,
        });
        if req.stream {
            // Ask for the trailing usage chunk: server token counts and prefix-cache details
            // are the whole point of this runner.
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
        let token_ids = choice
            .and_then(|c| c.get("token_ids"))
            .and_then(Value::as_array)
            .map(|arr| {
                arr.iter()
                    .filter_map(|v| v.as_u64().and_then(|n| u32::try_from(n).ok()))
                    .collect::<Vec<u32>>()
            });
        let finish_reason = choice
            .and_then(|c| c.get("finish_reason"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let usage = value.get("usage").map(parse_usage);
        let token_delta_observed = text_delta.as_deref().is_some_and(|text| !text.is_empty());
        StreamEvent {
            text_delta,
            token_delta_observed,
            token_ids,
            finish_reason,
            usage,
        }
    }
}

/// OpenAI-compatible `/chat/completions` protocol.
pub(crate) struct OpenAiChatCompletionsBackend;

impl Backend for OpenAiChatCompletionsBackend {
    fn endpoint_suffix(&self) -> &str {
        "/chat/completions"
    }

    fn build_payload(&self, req: &GenRequest) -> Value {
        let GenInput::ChatMessages(messages) = req.input else {
            unreachable!("chat completion backend requires messages")
        };
        let messages: Vec<Value> = messages
            .iter()
            .map(|message| {
                serde_json::json!({
                    "role": message.role.as_str(),
                    "content": message.content.as_ref(),
                })
            })
            .collect();
        let mut payload = serde_json::json!({
            "model": req.model,
            "messages": messages,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "stream": req.stream,
        });
        if req.stream {
            payload["stream_options"] = serde_json::json!({"include_usage": true});
        }
        if let Some(extra_body) = req.extra_body {
            payload
                .as_object_mut()
                .expect("chat payload must be a JSON object")
                .extend(extra_body.clone());
            if extra_body.contains_key("max_completion_tokens") {
                payload["max_completion_tokens"] = serde_json::json!(req.max_tokens);
                payload
                    .as_object_mut()
                    .expect("chat payload must be a JSON object")
                    .remove("max_tokens");
            }
        }
        payload
    }

    fn parse_event(&self, value: &Value) -> StreamEvent {
        let choice = value
            .get("choices")
            .and_then(Value::as_array)
            .and_then(|choices| choices.first());
        let text_delta = choice
            .and_then(|choice| {
                choice
                    .get("delta")
                    .and_then(|delta| delta.get("content"))
                    .or_else(|| {
                        choice
                            .get("message")
                            .and_then(|message| message.get("content"))
                    })
            })
            .and_then(Value::as_str)
            .map(str::to_string);
        let finish_reason = choice
            .and_then(|choice| choice.get("finish_reason"))
            .and_then(Value::as_str)
            .map(str::to_string);
        let reasoning_delta_observed =
            choice
                .and_then(|choice| choice.get("delta"))
                .is_some_and(|delta| {
                    ["reasoning_content", "reasoning"].into_iter().any(|key| {
                        delta
                            .get(key)
                            .and_then(Value::as_str)
                            .is_some_and(|text| !text.is_empty())
                    })
                });
        let token_delta_observed =
            text_delta.as_deref().is_some_and(|text| !text.is_empty()) || reasoning_delta_observed;
        StreamEvent {
            text_delta,
            token_delta_observed,
            token_ids: None,
            finish_reason,
            usage: value.get("usage").map(parse_usage),
        }
    }
}

/// Result of replaying one round: the log record plus the model's output token ids. The caller
/// carries the output ids forward as the next round's context so the previous-output region of
/// the next prefix matches what the server cached and stays prefix-cache-hittable.
pub(crate) struct StepOutcome {
    pub(crate) log: StepLog,
    pub(crate) output_ids: Vec<u32>,
    pub(crate) output_text: String,
}

/// Shared streaming engine. Owns the HTTP client, tokenizer, and a pluggable `Backend`, and
/// turns each trace round into a `StepOutcome`. All wire-format specifics live in the backend.
pub(crate) struct GenerationClient {
    endpoint: String,
    client: reqwest::Client,
    tokenizer: Arc<Tokenizer>,
    backend_kind: BackendKind,
    model: String,
    temperature: f64,
    extra_body: Option<Map<String, Value>>,
    stream_idle_timeout_secs: u64,
    backend: Box<dyn Backend>,
}

impl GenerationClient {
    pub(crate) fn new(args: &Args, tokenizer: Arc<Tokenizer>) -> Result<Self> {
        let backend = build_backend(args.backend);
        let endpoint = resolve_endpoint(
            &args.base_url,
            args.endpoint_url.as_deref(),
            backend.endpoint_suffix(),
        )?;
        let headers = build_auth_headers(args.api_key_env.as_deref())?;
        let extra_body = parse_extra_body(args.extra_body_json.as_deref())?;
        let client = reqwest::Client::builder()
            .pool_max_idle_per_host(20_000)
            .tcp_nodelay(true)
            .timeout(Duration::from_secs(3600))
            .default_headers(headers)
            .build()?;
        Ok(Self {
            endpoint,
            client,
            tokenizer,
            backend_kind: args.backend,
            model: args.model.clone(),
            temperature: args.temperature,
            extra_body,
            stream_idle_timeout_secs: args.stream_idle_timeout_secs,
            backend,
        })
    }

    pub(crate) fn tokenizer(&self) -> Arc<Tokenizer> {
        self.tokenizer.clone()
    }

    pub(crate) async fn run_step(
        &self,
        step: &SessionStep,
        request_id: String,
        input: GenInput<'_>,
        prompt_len: usize,
    ) -> StepOutcome {
        let submit_timestamp = unix_seconds_now();
        let start = SystemTime::now();

        // The selected backend turns the normalized token-id or message input into wire JSON.
        let max_tokens = self.effective_max_tokens(step.output_len);
        let payload = self.backend.build_payload(&GenRequest {
            model: &self.model,
            input,
            max_tokens,
            temperature: self.temperature,
            stream: true,
            extra_body: self.extra_body.as_ref(),
        });

        let post_timestamp = Some(unix_seconds_now());
        // Monotonic anchor at the send instant: TTFT is measured from here.
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
                                    let event = self.backend.parse_event(&value);
                                    if event.token_delta_observed && first_token_ms.is_none() {
                                        first_token_ms = Some(elapsed_ms(send_instant));
                                    }
                                    if let Some(delta) = event.text_delta {
                                        if !delta.is_empty() {
                                            output_text.push_str(&delta);
                                        }
                                    }
                                    if let Some(ids) = event.token_ids {
                                        output_token_ids.extend(ids);
                                    }
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
                                        server_cached_prompt_tokens =
                                            usage.cached_prompt_tokens.or(server_cached_prompt_tokens);
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

        // Re-encode the output text for a diagnostic token count and as a carry-forward fallback.
        let reencoded_output_ids: Vec<u32> = self
            .tokenizer
            .encode(output_text.clone(), false)
            .map(|encoding| encoding.get_ids().to_vec())
            .unwrap_or_default();
        let output_len_text_tokens = reencoded_output_ids.len();
        let output_len_actual = server_completion_tokens.unwrap_or(output_len_text_tokens);
        // Prefer the server's exact generated token ids (return_token_ids) for carry-forward, but
        // trust them only when their count matches the server's completion_tokens. Otherwise (an
        // older server that ignored the flag, or a shape mismatch) fall back to the re-encoded ids.
        let output_ids: Vec<u32> = if !output_token_ids.is_empty()
            && server_completion_tokens.is_none_or(|n| output_token_ids.len() == n)
        {
            output_token_ids
        } else {
            reencoded_output_ids
        };
        // Servers omit cached-token details when nothing was cached, so usage-present but
        // cache-detail-absent means zero cached tokens. Requires the server to report
        // prompt-token details (vLLM: --enable-prompt-tokens-details) to be meaningful.
        if server_cached_prompt_tokens.is_none() && server_prompt_tokens.is_some() {
            server_cached_prompt_tokens = Some(0);
        }
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
        let server_prefix_hit_rate_delta = match (self.backend_kind, server_prefix_hit_rate) {
            (BackendKind::Openai, Some(actual)) => Some(actual - planned_prefix_hit_rate),
            _ => None,
        };
        StepOutcome {
            log: StepLog {
            session_id: step.session_id.clone(),
            round_idx: step.round_idx,
            request_id,
            prefix_len: step.prefix_len,
            input_len: step.input_len,
            prompt_len,
            planned_prefix_hit_rate,
            output_len_target: max_tokens,
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
            output_text,
        }
    }

    /// Abort early unless the server actually reports prefix-cache hits.
    ///
    /// Servers omit cached-token details when nothing is cached, so a single response cannot tell
    /// "feature disabled" apart from "cache cold". We force a guaranteed hit by sending the same
    /// probe prompt twice and require the second response to report cached tokens. This also
    /// confirms prefix caching itself is enabled server-side.
    pub(crate) async fn preflight_cache_check(&self, probe_ids: &[u32]) -> Result<()> {
        let probe_text;
        let probe_messages;
        let input = match self.backend_kind {
            BackendKind::Openai => GenInput::PromptIds(probe_ids),
            BackendKind::Chat => {
                probe_text = self
                    .tokenizer
                    .decode(probe_ids, false)
                    .map_err(|err| anyhow!("preflight prompt decode failed: {err}"))?;
                probe_messages = [ChatMessage {
                    role: ChatRole::User,
                    content: probe_text.into(),
                }];
                GenInput::ChatMessages(&probe_messages)
            }
        };
        // First request warms the prefix cache; the identical second request must hit it.
        self.post_probe(input)
            .await
            .context("preflight warm-up request failed")?;
        let usage = self
            .post_probe(input)
            .await
            .context("preflight cache-hit request failed")?;

        let usage = usage.ok_or_else(|| {
            anyhow!("preflight: server response carried no usage block; cannot verify prefix-cache reporting")
        })?;
        match usage.cached_prompt_tokens {
            Some(cached) if cached > 0 => Ok(()),
            other => Err(anyhow!(
                "preflight: server reported no prefix-cache hit (prompt_tokens={:?}, cached_tokens={:?}). \
                 Launch the server with prompt-token details and prefix caching enabled \
                 (vLLM: --enable-prompt-tokens-details / ENABLE_PROMPT_TOKENS_DETAILS=1); see replay/README.md.",
                usage.prompt_tokens, other
            )),
        }
    }

    /// Send one non-streaming completion and return its normalized usage, if present.
    async fn post_probe(&self, input: GenInput<'_>) -> Result<Option<Usage>> {
        let payload = self.backend.build_payload(&GenRequest {
            model: &self.model,
            input,
            max_tokens: 1,
            temperature: 0.0,
            stream: false,
            extra_body: self.extra_body.as_ref(),
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
        Ok(self.backend.parse_event(&body).usage)
    }

    fn effective_max_tokens(&self, trace_max_tokens: usize) -> usize {
        self.extra_body
            .as_ref()
            .and_then(|extra_body| extra_body.get("max_completion_tokens"))
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .map_or(trace_max_tokens, |provider_max| {
                trace_max_tokens.min(provider_max)
            })
    }
}

fn resolve_endpoint(base_url: &str, endpoint_url: Option<&str>, suffix: &str) -> Result<String> {
    if let Some(endpoint) = endpoint_url {
        let endpoint = endpoint.trim();
        if endpoint.is_empty() {
            return Err(anyhow!("--endpoint-url must not be empty"));
        }
        return Ok(endpoint.to_string());
    }
    let base = base_url.trim().trim_end_matches('/');
    if base.is_empty() {
        return Err(anyhow!("--base-url must not be empty"));
    }
    Ok(format!("{base}{suffix}"))
}

fn parse_extra_body(raw: Option<&str>) -> Result<Option<Map<String, Value>>> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let extra_body: Map<String, Value> =
        serde_json::from_str(raw).context("--extra-body-json must be a valid JSON object")?;
    const RESERVED_FIELDS: &[&str] = &[
        "model",
        "messages",
        "prompt",
        "max_tokens",
        "temperature",
        "stream",
        "stream_options",
    ];
    if let Some(field) = RESERVED_FIELDS
        .iter()
        .find(|field| extra_body.contains_key(**field))
    {
        return Err(anyhow!(
            "--extra-body-json must not override runner-owned field {field}"
        ));
    }
    if let Some(value) = extra_body.get("max_completion_tokens") {
        let max_completion_tokens = value
            .as_u64()
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| {
                anyhow!("--extra-body-json max_completion_tokens must be a positive integer")
            })?;
        if max_completion_tokens == 0 {
            return Err(anyhow!(
                "--extra-body-json max_completion_tokens must be a positive integer"
            ));
        }
    }
    Ok(Some(extra_body))
}

fn build_auth_headers(api_key_env: Option<&str>) -> Result<HeaderMap> {
    let mut headers = HeaderMap::new();
    let Some(env_name) = api_key_env else {
        return Ok(headers);
    };
    let env_name = env_name.trim();
    if env_name.is_empty() {
        return Err(anyhow!("--api-key-env must not be empty"));
    }
    let api_key = std::env::var(env_name)
        .map_err(|_| anyhow!("API key environment variable {env_name} is missing"))?;
    if api_key.trim().is_empty() {
        return Err(anyhow!("API key environment variable {env_name} is empty"));
    }
    let mut value = HeaderValue::from_str(&format!("Bearer {api_key}"))
        .map_err(|_| anyhow!("API key environment variable {env_name} is not a valid header"))?;
    value.set_sensitive(true);
    headers.insert(AUTHORIZATION, value);
    Ok(headers)
}

fn parse_usage(usage: &Value) -> Usage {
    Usage {
        prompt_tokens: usage_usize(usage, "prompt_tokens"),
        completion_tokens: usage_usize(usage, "completion_tokens"),
        total_tokens: usage_usize(usage, "total_tokens"),
        cached_prompt_tokens: usage_cached_prompt_tokens(usage),
    }
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
