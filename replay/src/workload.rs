use serde::Serialize;
use std::collections::BTreeMap;

use crate::trace::SessionStep;

/// Aggregate, vLLM-free description of a parsed workload. Doubles as dry-run output.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct WorkloadSummary {
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
    pub(crate) fn from_sessions(
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

    /// Total replayable rounds across all sessions.
    pub(crate) fn total_steps(&self) -> usize {
        self.steps
    }

    /// Longest single-round prompt (`prefix_len + input_len`) in the workload.
    pub(crate) fn max_prompt_len(&self) -> usize {
        self.max_prompt_len
    }

    pub(crate) fn print(&self) {
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
