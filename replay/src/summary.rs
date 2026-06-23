use anyhow::{Context, Result};
use serde::Serialize;
use std::fs::File;
use std::io::Write;
use tokio::sync::mpsc;

use crate::record::StepLog;
use crate::util::ratio;
use crate::workload::WorkloadSummary;

/// Aggregate replay-side statistics accumulated over every logged round.
#[derive(Debug, Default, Serialize)]
pub(crate) struct ReplaySummary {
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

/// Combined dry-run/full-run JSON summary: workload shape plus replay results.
#[derive(Debug, Serialize)]
pub(crate) struct RunSummary {
    pub(crate) workload: WorkloadSummary,
    pub(crate) replay: ReplaySummary,
}

/// Drain logged rounds to JSONL on disk and fold them into a `ReplaySummary`.
pub(crate) async fn write_logs(path: String, mut rx: mpsc::Receiver<StepLog>) -> ReplaySummary {
    let file = File::create(&path).expect("failed to create log file");
    let mut writer = std::io::BufWriter::with_capacity(1024 * 1024, file);
    let mut summary = ReplaySummary::default();
    let mut total_durations = Vec::new();
    let mut ttfts = Vec::new();

    while let Some(record) = rx.recv().await {
        summary.add(&record);
        total_durations.push(record.total_duration_ms);
        if let Some(ttft) = record.first_token_ms {
            ttfts.push(ttft);
        }
        log_server_prefix_hit_rate(&record);
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

fn log_server_prefix_hit_rate(record: &StepLog) {
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

/// Write the combined run summary to `--summary-path` when one was requested.
pub(crate) fn write_summary_if_requested(
    summary_path: Option<&str>,
    summary: RunSummary,
) -> Result<()> {
    let Some(path) = summary_path else {
        return Ok(());
    };
    let file = File::create(path).with_context(|| format!("failed to create summary: {path}"))?;
    serde_json::to_writer_pretty(file, &summary)
        .with_context(|| format!("failed to write summary: {path}"))?;
    Ok(())
}
