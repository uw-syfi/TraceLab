use anyhow::{anyhow, Context, Result};
use bytes::BytesMut;
use clap::Parser;
use futures::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Write};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokenizers::Tokenizer;
use tokio::sync::{mpsc, Semaphore};
use tokio::time::timeout;

#[derive(Parser, Debug, Clone)]
#[command(
    author,
    version,
    about = "Session-aware closed-loop workload runner for vLLM"
)]
struct Args {
    /// CSV with session_id/id,round_idx,prefix_len,input_len,output_len,tool_wait_after_ms.
    #[arg(long)]
    trace: String,

    /// Text corpus used to build synthetic prompt/input/output token pools.
    #[arg(long)]
    text_file: String,

    /// tokenizer.json path or a model directory containing tokenizer.json.
    #[arg(long)]
    tokenizer: String,

    /// vLLM OpenAI-compatible base URL, normally http://host:port/v1.
    #[arg(long, default_value = "http://127.0.0.1:8000/v1")]
    base_url: String,

    #[arg(long)]
    model: String,

    #[arg(long, default_value_t = 0.0)]
    temperature: f64,

    /// Ask vLLM to continue generation after EOS until max_tokens is reached.
    #[arg(long, default_value_t = false)]
    ignore_eos: bool,

    /// Do not request usage accounting in streaming responses.
    #[arg(long, default_value_t = false)]
    disable_stream_usage: bool,

    /// Treat missing server cache-detail fields as zero cached tokens when usage is present.
    ///
    /// vLLM omits prompt_tokens_details when no tokens were cached. Enable this only when the
    /// server is launched with --enable-prompt-tokens-details; otherwise missing details mean
    /// "not reported", not necessarily zero.
    #[arg(long, default_value_t = false)]
    assume_missing_cache_details_zero: bool,

    #[arg(long)]
    max_sessions: Option<usize>,

    #[arg(long, default_value = "session_runner_output.jsonl")]
    log_path: String,

    #[arg(long, default_value_t = 200_000)]
    token_pool_limit: usize,

    /// Max seconds to wait for the next streaming chunk before failing a request.
    #[arg(long, default_value_t = 600)]
    stream_idle_timeout_secs: u64,

    /// Stop a session after the first failed round.
    #[arg(long, default_value_t = true)]
    stop_session_on_error: bool,

    /// Do not delay each session by the CSV arrival_time. Useful for old immediate-start runs.
    #[arg(long, default_value_t = false)]
    ignore_arrival_time: bool,

    /// Maximum number of sessions allowed to actively run at once.
    #[arg(long)]
    max_active_sessions: Option<usize>,

    /// Validate and summarize the workload without contacting vLLM.
    #[arg(long, default_value_t = false)]
    dry_run: bool,

    /// Optional model context limit used for workload validation.
    #[arg(long)]
    max_model_len: Option<usize>,

    /// If set with --max-model-len, skip rounds whose prompt length exceeds the limit.
    #[arg(long, default_value_t = false)]
    fail_on_context_overflow: bool,

    /// Optional JSON summary path for one run.
    #[arg(long)]
    summary_path: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
struct SessionStep {
    #[serde(alias = "id")]
    session_id: String,
    #[serde(default)]
    arrival_time: f64,
    round_idx: usize,
    prefix_len: usize,
    input_len: usize,
    output_len: usize,
    tool_wait_after_ms: f64,
}

#[derive(Debug, Serialize)]
struct StepLog {
    session_id: String,
    round_idx: usize,
    request_id: String,
    prefix_len: usize,
    input_len: usize,
    prompt_len: usize,
    planned_prefix_hit_rate: f64,
    output_len_target: usize,
    output_len_actual: usize,
    output_len_text_tokens: usize,
    server_prompt_tokens: Option<usize>,
    server_completion_tokens: Option<usize>,
    server_total_tokens: Option<usize>,
    server_cached_prompt_tokens: Option<usize>,
    server_uncached_prompt_tokens: Option<usize>,
    server_prefix_hit_rate: Option<f64>,
    server_prefix_hit_rate_delta: Option<f64>,
    finish_reason: Option<String>,
    tool_wait_after_ms: f64,
    arrival_time_ms: f64,
    submit_timestamp: f64,
    post_timestamp: Option<f64>,
    first_token_ms: Option<f64>,
    total_duration_ms: f64,
    chunk_count: usize,
    status: String,
    output_preview: String,
    error: Option<String>,
}

#[derive(Debug, Default, Serialize)]
struct ReplaySummary {
    attempted_steps: usize,
    success_steps: usize,
    failed_steps: usize,
    target_output_tokens: usize,
    actual_output_tokens: usize,
    output_mismatch_steps: usize,
    output_token_delta: i64,
    total_duration_ms_sum: f64,
    total_duration_ms_avg: f64,
    total_duration_ms_p50: f64,
    total_duration_ms_p90: f64,
    total_duration_ms_max: f64,
    ttft_ms_avg: Option<f64>,
    ttft_ms_p50: Option<f64>,
    ttft_ms_p90: Option<f64>,
    ttft_ms_max: Option<f64>,
    context_overflow_steps: usize,
    planned_prefix_tokens: usize,
    planned_prompt_tokens: usize,
    planned_prefix_hit_rate: Option<f64>,
    measured_cache_steps: usize,
    measured_server_cached_prompt_tokens: usize,
    measured_server_prompt_tokens: usize,
    planned_prefix_tokens_for_measured_cache_steps: usize,
    planned_prompt_tokens_for_measured_cache_steps: usize,
    planned_prefix_hit_rate_for_measured_cache_steps: Option<f64>,
    server_prefix_hit_rate: Option<f64>,
    server_prefix_hit_rate_delta: Option<f64>,
}

impl ReplaySummary {
    fn add(&mut self, record: &StepLog) {
        self.attempted_steps += 1;
        if record.status == "SUCCESS" {
            self.success_steps += 1;
        } else {
            self.failed_steps += 1;
        }
        if record.status == "SKIPPED_CONTEXT_OVERFLOW" {
            self.context_overflow_steps += 1;
        }
        self.planned_prefix_tokens += record.prefix_len;
        self.planned_prompt_tokens += record.prompt_len;
        self.target_output_tokens += record.output_len_target;
        self.actual_output_tokens += record.output_len_actual;
        if record.status == "SUCCESS" && record.output_len_actual != record.output_len_target {
            self.output_mismatch_steps += 1;
            self.output_token_delta +=
                record.output_len_actual as i64 - record.output_len_target as i64;
        }
        if let (Some(cached), Some(prompt)) = (
            record.server_cached_prompt_tokens,
            record.server_prompt_tokens,
        ) {
            self.measured_cache_steps += 1;
            self.measured_server_cached_prompt_tokens += cached;
            self.measured_server_prompt_tokens += prompt;
            self.planned_prefix_tokens_for_measured_cache_steps += record.prefix_len;
            self.planned_prompt_tokens_for_measured_cache_steps += record.prompt_len;
        }
        self.total_duration_ms_sum += record.total_duration_ms;
    }
}

#[derive(Debug, Serialize)]
struct RunSummary {
    workload: WorkloadSummary,
    replay: ReplaySummary,
}

#[derive(Default)]
struct Stats {
    submitted: AtomicUsize,
    completed: AtomicUsize,
    failed: AtomicUsize,
    finished_sessions: AtomicUsize,
}

impl Stats {
    fn record_submit(&self) {
        self.submitted.fetch_add(1, Ordering::Relaxed);
    }

    fn record_result(&self, success: bool) {
        if success {
            self.completed.fetch_add(1, Ordering::Relaxed);
        } else {
            self.failed.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn record_session_done(&self) {
        self.finished_sessions.fetch_add(1, Ordering::Relaxed);
    }
}

struct AppState {
    args: Args,
    vllm: Arc<VllmClient>,
    token_pool: Arc<Vec<u32>>,
    stats: Arc<Stats>,
    run_start: Instant,
    session_semaphore: Option<Arc<Semaphore>>,
}

struct VllmClient {
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

struct TokenProvider {
    pool: Arc<Vec<u32>>,
    cursor: usize,
}

impl TokenProvider {
    fn new(pool: Arc<Vec<u32>>, seed_offset: usize) -> Result<Self> {
        if pool.is_empty() {
            return Err(anyhow!("token pool is empty"));
        }
        Ok(Self {
            cursor: seed_offset % pool.len(),
            pool,
        })
    }

    fn take(&mut self, len: usize) -> Vec<u32> {
        let mut out = Vec::with_capacity(len);
        for _ in 0..len {
            out.push(self.pool[self.cursor]);
            self.cursor = (self.cursor + 1) % self.pool.len();
        }
        out
    }
}

struct PromptBuilder {
    token_provider: TokenProvider,
    context_tokens: Vec<u32>,
}

impl PromptBuilder {
    fn new(token_provider: TokenProvider) -> Self {
        Self {
            token_provider,
            context_tokens: Vec::new(),
        }
    }

    fn build_prompt(&mut self, step: &SessionStep) -> Vec<u32> {
        if self.context_tokens.len() < step.prefix_len {
            let need = step.prefix_len - self.context_tokens.len();
            self.context_tokens.extend(self.token_provider.take(need));
        }

        let mut prompt_ids = self.context_tokens[..step.prefix_len].to_vec();
        prompt_ids.extend(self.token_provider.take(step.input_len));
        prompt_ids
    }

    fn commit_synthetic_output(&mut self, prompt_ids: Vec<u32>, output_len: usize) {
        self.context_tokens = prompt_ids;
        self.context_tokens
            .extend(self.token_provider.take(output_len));
    }
}

#[derive(Debug, Clone, Serialize)]
struct WorkloadSummary {
    sessions: usize,
    steps: usize,
    first_context_overflow_round_idx: Option<usize>,
    first_context_overflow_prompt_len: Option<usize>,
    max_prompt_len: usize,
    max_prefix_len: usize,
    max_input_len: usize,
    max_output_len: usize,
    total_output_len: usize,
    max_arrival_time_ms: f64,
    total_tool_wait_after_ms: f64,
}

impl WorkloadSummary {
    fn from_sessions(
        sessions: &BTreeMap<String, Vec<SessionStep>>,
        max_model_len: Option<usize>,
    ) -> Self {
        let mut summary = Self {
            sessions: sessions.len(),
            steps: 0,
            first_context_overflow_round_idx: None,
            first_context_overflow_prompt_len: None,
            max_prompt_len: 0,
            max_prefix_len: 0,
            max_input_len: 0,
            max_output_len: 0,
            total_output_len: 0,
            max_arrival_time_ms: 0.0,
            total_tool_wait_after_ms: 0.0,
        };

        for steps in sessions.values() {
            for step in steps {
                let prompt_len = step.prefix_len.saturating_add(step.input_len);
                summary.steps += 1;
                summary.max_prompt_len = summary.max_prompt_len.max(prompt_len);
                summary.max_prefix_len = summary.max_prefix_len.max(step.prefix_len);
                summary.max_input_len = summary.max_input_len.max(step.input_len);
                summary.max_output_len = summary.max_output_len.max(step.output_len);
                summary.total_output_len += step.output_len;
                summary.max_arrival_time_ms = summary.max_arrival_time_ms.max(step.arrival_time);
                summary.total_tool_wait_after_ms += step.tool_wait_after_ms;
                if let Some(limit) = max_model_len {
                    if prompt_len > limit && summary.first_context_overflow_round_idx.is_none() {
                        summary.first_context_overflow_round_idx = Some(step.round_idx);
                        summary.first_context_overflow_prompt_len = Some(prompt_len);
                    }
                }
            }
        }

        summary
    }

    fn print(&self) {
        eprintln!(
            "workload summary | sessions={} steps={} max_prompt_len={} max_prefix_len={} max_input_len={} max_output_len={} total_output_len={} max_arrival_time_ms={:.3} total_tool_wait_after_ms={:.3}",
            self.sessions,
            self.steps,
            self.max_prompt_len,
            self.max_prefix_len,
            self.max_input_len,
            self.max_output_len,
            self.total_output_len,
            self.max_arrival_time_ms,
            self.total_tool_wait_after_ms,
        );
        if let (Some(round_idx), Some(prompt_len)) = (
            self.first_context_overflow_round_idx,
            self.first_context_overflow_prompt_len,
        ) {
            eprintln!(
                "context overflow | first_round_idx={} first_prompt_len={}",
                round_idx, prompt_len
            );
        }
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    fdlimit::raise_fd_limit().ok();

    if args.max_active_sessions == Some(0) {
        return Err(anyhow!("--max-active-sessions must be greater than 0"));
    }
    if args.fail_on_context_overflow && args.max_model_len.is_none() {
        return Err(anyhow!(
            "--fail-on-context-overflow requires --max-model-len"
        ));
    }

    let sessions = load_sessions(&args.trace, args.max_sessions)?;
    let workload_summary = WorkloadSummary::from_sessions(&sessions, args.max_model_len);
    workload_summary.print();
    if args.dry_run {
        write_summary_if_requested(
            &args,
            RunSummary {
                workload: workload_summary,
                replay: ReplaySummary::default(),
            },
        )?;
        return Ok(());
    }

    let tokenizer = Arc::new(load_tokenizer(&args.tokenizer)?);
    let token_pool = Arc::new(build_token_pool(
        &args.text_file,
        tokenizer.as_ref(),
        args.token_pool_limit,
    )?);
    let total_steps = workload_summary.steps;

    let endpoint = format!("{}/completions", args.base_url.trim_end_matches('/'));
    let client = reqwest::Client::builder()
        .pool_max_idle_per_host(20_000)
        .tcp_nodelay(true)
        .timeout(Duration::from_secs(3600))
        .build()?;
    let vllm = Arc::new(VllmClient {
        endpoint,
        client,
        tokenizer,
        model: args.model.clone(),
        temperature: args.temperature,
        ignore_eos: args.ignore_eos,
        include_stream_usage: !args.disable_stream_usage,
        assume_missing_cache_details_zero: args.assume_missing_cache_details_zero,
        stream_idle_timeout_secs: args.stream_idle_timeout_secs,
    });

    let state = Arc::new(AppState {
        args: args.clone(),
        vllm,
        token_pool,
        stats: Arc::new(Stats::default()),
        run_start: Instant::now(),
        session_semaphore: args
            .max_active_sessions
            .map(|n| Arc::new(Semaphore::new(n))),
    });

    let (log_tx, log_rx) = mpsc::channel::<StepLog>(100_000);
    let log_task = tokio::spawn(write_logs(args.log_path.clone(), log_rx));
    tokio::spawn(status_task(
        state.stats.clone(),
        sessions.len(),
        total_steps,
        state.run_start,
    ));

    let mut join_set = tokio::task::JoinSet::new();
    for (session_ordinal, (session_id, steps)) in sessions.into_iter().enumerate() {
        let state_ref = state.clone();
        let log_tx_ref = log_tx.clone();
        join_set.spawn(async move {
            run_session(state_ref, log_tx_ref, session_ordinal, session_id, steps).await;
        });
    }
    drop(log_tx);

    while let Some(result) = join_set.join_next().await {
        if let Err(err) = result {
            eprintln!("session task join error: {err}");
        }
    }

    let replay_summary = log_task.await?;
    write_summary_if_requested(
        &args,
        RunSummary {
            workload: workload_summary,
            replay: replay_summary,
        },
    )?;

    Ok(())
}

async fn run_session(
    state: Arc<AppState>,
    log_tx: mpsc::Sender<StepLog>,
    session_ordinal: usize,
    session_id: String,
    steps: Vec<SessionStep>,
) {
    wait_for_session_arrival(&state, &steps).await;
    let _session_permit = match &state.session_semaphore {
        Some(semaphore) => semaphore.clone().acquire_owned().await.ok(),
        None => None,
    };

    let token_provider = match TokenProvider::new(
        state.token_pool.clone(),
        session_ordinal.wrapping_mul(9_973),
    ) {
        Ok(provider) => provider,
        Err(err) => {
            eprintln!("session {session_id}: {err}");
            return;
        }
    };
    let mut prompt_builder = PromptBuilder::new(token_provider);

    for step in steps {
        let prompt_ids = prompt_builder.build_prompt(&step);
        let request_id = format!("{}_round_{:06}", session_id, step.round_idx);
        state.stats.record_submit();
        let log = if should_skip_context_overflow(&state.args, prompt_ids.len()) {
            context_overflow_log(
                &step,
                request_id,
                prompt_ids.len(),
                state.args.max_model_len,
            )
        } else {
            state.vllm.run_step(&step, request_id, &prompt_ids).await
        };
        let success = log.status == "SUCCESS";
        let _ = log_tx.send(log).await;

        state.stats.record_result(success);
        if !success && state.args.stop_session_on_error {
            break;
        }

        // Use synthetic output tokens so the replayed context shape exactly follows the trace.
        prompt_builder.commit_synthetic_output(prompt_ids, step.output_len);

        if step.tool_wait_after_ms > 0.0 {
            tokio::time::sleep(Duration::from_secs_f64(step.tool_wait_after_ms / 1000.0)).await;
        }
    }

    state.stats.record_session_done();
}

fn should_skip_context_overflow(args: &Args, prompt_len: usize) -> bool {
    args.fail_on_context_overflow
        && args
            .max_model_len
            .map(|limit| prompt_len > limit)
            .unwrap_or(false)
}

fn context_overflow_log(
    step: &SessionStep,
    request_id: String,
    prompt_len: usize,
    max_model_len: Option<usize>,
) -> StepLog {
    let limit = max_model_len
        .map(|value| value.to_string())
        .unwrap_or_else(|| "unknown".to_string());
    StepLog {
        session_id: step.session_id.clone(),
        round_idx: step.round_idx,
        request_id,
        prefix_len: step.prefix_len,
        input_len: step.input_len,
        prompt_len,
        planned_prefix_hit_rate: prefix_hit_rate(step.prefix_len, prompt_len),
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
        submit_timestamp: unix_seconds_now(),
        post_timestamp: None,
        first_token_ms: None,
        total_duration_ms: 0.0,
        chunk_count: 0,
        status: "SKIPPED_CONTEXT_OVERFLOW".to_string(),
        output_preview: String::new(),
        error: Some(format!(
            "prompt_len {} exceeds max_model_len {}",
            prompt_len, limit
        )),
    }
}

async fn wait_for_session_arrival(state: &AppState, steps: &[SessionStep]) {
    if state.args.ignore_arrival_time {
        return;
    }
    let arrival_ms = steps
        .first()
        .map(|step| step.arrival_time.max(0.0))
        .unwrap_or(0.0);
    if arrival_ms <= 0.0 {
        return;
    }

    let target = state.run_start + Duration::from_secs_f64(arrival_ms / 1000.0);
    let now = Instant::now();
    if target > now {
        tokio::time::sleep_until(tokio::time::Instant::from_std(target)).await;
    }
}

impl VllmClient {
    async fn run_step(
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

fn prefix_hit_rate(prefix_tokens: usize, prompt_tokens: usize) -> f64 {
    ratio(prefix_tokens, prompt_tokens).unwrap_or(0.0)
}

fn ratio(numerator: usize, denominator: usize) -> Option<f64> {
    if denominator == 0 {
        None
    } else {
        Some(numerator as f64 / denominator as f64)
    }
}

async fn write_logs(path: String, mut rx: mpsc::Receiver<StepLog>) -> ReplaySummary {
    let file = File::create(&path).expect("failed to create log file");
    let mut writer = std::io::BufWriter::with_capacity(1024 * 1024, file);
    let mut summary = ReplaySummary::default();
    let mut total_durations = Vec::new();
    let mut ttfts = Vec::new();
    let mut warned_missing_cache_details = false;

    while let Some(record) = rx.recv().await {
        summary.add(&record);
        total_durations.push(record.total_duration_ms);
        if let Some(ttft) = record.first_token_ms {
            ttfts.push(ttft);
        }
        log_server_prefix_hit_rate(&record, &mut warned_missing_cache_details);
        if let Ok(json) = serde_json::to_string(&record) {
            let _ = writeln!(writer, "{json}");
            let _ = writer.flush();
        }
    }
    let _ = writer.flush();
    let summary = finalize_replay_summary(summary, &mut total_durations, &mut ttfts);
    log_server_prefix_hit_rate_summary(&summary);
    summary
}

fn log_server_prefix_hit_rate(record: &StepLog, warned_missing_cache_details: &mut bool) {
    if record.status != "SUCCESS" {
        return;
    }
    if let (Some(actual), Some(cached), Some(prompt)) = (
        record.server_prefix_hit_rate,
        record.server_cached_prompt_tokens,
        record.server_prompt_tokens,
    ) {
        eprintln!(
            "prefix hit rate | request_id={} planned={:.4} actual={:.4} delta={:+.4} server_cached_prompt_tokens={} server_prompt_tokens={}",
            record.request_id,
            record.planned_prefix_hit_rate,
            actual,
            actual - record.planned_prefix_hit_rate,
            cached,
            prompt,
        );
    } else if record.server_prompt_tokens.is_some() && !*warned_missing_cache_details {
        eprintln!(
            "prefix hit rate | server usage did not include cached prompt tokens; start vLLM with ENABLE_PROMPT_TOKENS_DETAILS=1 / --enable-prompt-tokens-details to log the real hit rate"
        );
        *warned_missing_cache_details = true;
    }
}

fn log_server_prefix_hit_rate_summary(summary: &ReplaySummary) {
    if let (Some(actual), Some(planned)) = (
        summary.server_prefix_hit_rate,
        summary.planned_prefix_hit_rate_for_measured_cache_steps,
    ) {
        eprintln!(
            "prefix hit rate summary | measured_steps={} planned={:.4} actual={:.4} delta={:+.4} server_cached_prompt_tokens={} server_prompt_tokens={}",
            summary.measured_cache_steps,
            planned,
            actual,
            actual - planned,
            summary.measured_server_cached_prompt_tokens,
            summary.measured_server_prompt_tokens,
        );
    } else {
        eprintln!(
            "prefix hit rate summary | measured_steps=0 actual unavailable; no server cached prompt token details were reported"
        );
    }
}

fn finalize_replay_summary(
    mut summary: ReplaySummary,
    total_durations: &mut [f64],
    ttfts: &mut [f64],
) -> ReplaySummary {
    if !total_durations.is_empty() {
        total_durations.sort_by(|a, b| a.total_cmp(b));
        summary.total_duration_ms_avg =
            summary.total_duration_ms_sum / total_durations.len() as f64;
        summary.total_duration_ms_p50 = percentile_sorted(total_durations, 0.50);
        summary.total_duration_ms_p90 = percentile_sorted(total_durations, 0.90);
        summary.total_duration_ms_max = *total_durations.last().unwrap_or(&0.0);
    }

    if !ttfts.is_empty() {
        ttfts.sort_by(|a, b| a.total_cmp(b));
        let sum: f64 = ttfts.iter().sum();
        summary.ttft_ms_avg = Some(sum / ttfts.len() as f64);
        summary.ttft_ms_p50 = Some(percentile_sorted(ttfts, 0.50));
        summary.ttft_ms_p90 = Some(percentile_sorted(ttfts, 0.90));
        summary.ttft_ms_max = ttfts.last().copied();
    }

    summary.planned_prefix_hit_rate =
        ratio(summary.planned_prefix_tokens, summary.planned_prompt_tokens);
    summary.planned_prefix_hit_rate_for_measured_cache_steps = ratio(
        summary.planned_prefix_tokens_for_measured_cache_steps,
        summary.planned_prompt_tokens_for_measured_cache_steps,
    );
    summary.server_prefix_hit_rate = ratio(
        summary.measured_server_cached_prompt_tokens,
        summary.measured_server_prompt_tokens,
    );
    summary.server_prefix_hit_rate_delta = match (
        summary.server_prefix_hit_rate,
        summary.planned_prefix_hit_rate_for_measured_cache_steps,
    ) {
        (Some(actual), Some(planned)) => Some(actual - planned),
        _ => None,
    };

    summary
}

fn percentile_sorted(values: &[f64], q: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    if values.len() == 1 {
        return values[0];
    }
    let pos = q.clamp(0.0, 1.0) * (values.len() - 1) as f64;
    let lo = pos.floor() as usize;
    let hi = pos.ceil() as usize;
    if lo == hi {
        values[lo]
    } else {
        let frac = pos - lo as f64;
        values[lo] * (1.0 - frac) + values[hi] * frac
    }
}

fn write_summary_if_requested(args: &Args, summary: RunSummary) -> Result<()> {
    let Some(path) = &args.summary_path else {
        return Ok(());
    };
    let file = File::create(path).with_context(|| format!("failed to create summary: {path}"))?;
    serde_json::to_writer_pretty(file, &summary)
        .with_context(|| format!("failed to write summary: {path}"))?;
    Ok(())
}

async fn status_task(stats: Arc<Stats>, total_sessions: usize, total_steps: usize, start: Instant) {
    loop {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let submitted = stats.submitted.load(Ordering::Relaxed);
        let completed = stats.completed.load(Ordering::Relaxed);
        let failed = stats.failed.load(Ordering::Relaxed);
        let finished_sessions = stats.finished_sessions.load(Ordering::Relaxed);
        let active = submitted.saturating_sub(completed + failed);
        let finished_steps = completed + failed;

        eprintln!(
            "sessions {}/{} | steps {}/{} completed={} submitted={} active={} failed={} | elapsed={:.1}s",
            finished_sessions,
            total_sessions,
            finished_steps,
            total_steps,
            completed,
            submitted,
            active,
            failed,
            start.elapsed().as_secs_f64(),
        );

        if finished_sessions >= total_sessions {
            break;
        }
    }
}

fn load_sessions(
    path: &str,
    max_sessions: Option<usize>,
) -> Result<BTreeMap<String, Vec<SessionStep>>> {
    let mut reader = csv::Reader::from_path(path)
        .with_context(|| format!("failed to open session trace: {path}"))?;
    let mut sessions: BTreeMap<String, Vec<SessionStep>> = BTreeMap::new();

    for row in reader.deserialize() {
        let step: SessionStep = row.context("failed to parse session trace row")?;
        sessions
            .entry(step.session_id.clone())
            .or_default()
            .push(step);
    }

    for steps in sessions.values_mut() {
        steps.sort_by_key(|step| step.round_idx);
    }

    if let Some(max) = max_sessions {
        let keys: Vec<String> = sessions.keys().skip(max).cloned().collect();
        for key in keys {
            sessions.remove(&key);
        }
    }

    Ok(sessions)
}

fn load_tokenizer(path: &str) -> Result<Tokenizer> {
    let path = std::path::Path::new(path);
    let tokenizer = if path.exists() {
        let tokenizer_path = if path.is_dir() {
            path.join("tokenizer.json")
        } else {
            path.to_path_buf()
        };

        Tokenizer::from_file(&tokenizer_path).map_err(|err| {
            anyhow!(
                "failed to load tokenizer {}: {err}",
                tokenizer_path.display()
            )
        })?
    } else {
        let api = hf_hub::api::sync::Api::new()
            .map_err(|err| anyhow!("failed to create Hugging Face API client: {err}"))?;
        let repo = api.model(path.to_string_lossy().to_string());
        let tokenizer_path = repo.get("tokenizer.json").map_err(|err| {
            anyhow!(
                "failed to download tokenizer.json for {}: {err}",
                path.display()
            )
        })?;
        Tokenizer::from_file(tokenizer_path)
            .map_err(|err| anyhow!("failed to load downloaded tokenizer: {err}"))?
    };
    Ok(tokenizer)
}

fn build_token_pool(text_file: &str, tokenizer: &Tokenizer, limit: usize) -> Result<Vec<u32>> {
    let file = File::open(text_file)
        .with_context(|| format!("failed to open text corpus: {text_file}"))?;
    let reader = BufReader::new(file);
    let mut pool = Vec::with_capacity(limit);

    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let encoding = tokenizer
            .encode(line, false)
            .map_err(|err| anyhow!("tokenizer encode failed: {err}"))?;
        pool.extend(encoding.get_ids());
        if pool.len() >= limit {
            pool.truncate(limit);
            break;
        }
    }

    if pool.is_empty() {
        return Err(anyhow!("text corpus produced an empty token pool"));
    }
    Ok(pool)
}

fn unix_seconds_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn elapsed_ms(start: SystemTime) -> f64 {
    let ms = SystemTime::now()
        .duration_since(start)
        .unwrap_or_default()
        .as_secs_f64()
        * 1000.0;
    (ms * 100.0).round() / 100.0
}
