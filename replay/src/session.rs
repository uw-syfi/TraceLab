use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::{mpsc, Semaphore};

use crate::cli::Args;
use crate::record::StepLog;
use crate::tokens::{PromptBuilder, TokenProvider};
use crate::trace::SessionStep;
use crate::util::{prefix_hit_rate, unix_seconds_now};
use crate::vllm::VllmClient;

/// Shared, immutable-per-run state handed to every session task.
pub(crate) struct AppState {
    pub(crate) args: Args,
    pub(crate) vllm: Arc<VllmClient>,
    pub(crate) token_pool: Arc<Vec<u32>>,
    pub(crate) stats: Arc<Stats>,
    pub(crate) run_start: Instant,
    pub(crate) session_semaphore: Option<Arc<Semaphore>>,
}

/// Lock-free progress counters shared with the status reporter.
#[derive(Default)]
pub(crate) struct Stats {
    submitted: AtomicUsize,
    completed: AtomicUsize,
    failed: AtomicUsize,
    finished_sessions: AtomicUsize,
}

impl Stats {
    pub(crate) fn record_submit(&self) {
        self.submitted.fetch_add(1, Ordering::Relaxed);
    }

    pub(crate) fn record_result(&self, success: bool) {
        if success {
            self.completed.fetch_add(1, Ordering::Relaxed);
        } else {
            self.failed.fetch_add(1, Ordering::Relaxed);
        }
    }

    pub(crate) fn record_session_done(&self) {
        self.finished_sessions.fetch_add(1, Ordering::Relaxed);
    }
}

/// Replay one session as an ordered, closed-loop chain of rounds.
pub(crate) async fn run_session(
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
            context_overflow_log(&step, request_id, prompt_ids.len(), state.args.max_model_len)
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

/// Periodic stderr progress reporter; exits once all sessions are finished.
pub(crate) async fn status_task(
    stats: Arc<Stats>,
    total_sessions: usize,
    total_steps: usize,
    start: Instant,
) {
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
