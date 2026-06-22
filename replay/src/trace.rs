use anyhow::{Context, Result};
use serde::Deserialize;
use std::collections::BTreeMap;

/// One replayable round from the session trace CSV.
#[derive(Debug, Clone, Deserialize)]
pub(crate) struct SessionStep {
    #[serde(alias = "id")]
    pub(crate) session_id: String,
    #[serde(default)]
    pub(crate) arrival_time: f64,
    pub(crate) round_idx: usize,
    pub(crate) prefix_len: usize,
    pub(crate) input_len: usize,
    pub(crate) output_len: usize,
    pub(crate) tool_wait_after_ms: f64,
}

/// Load the trace CSV into per-session, round-ordered step lists.
pub(crate) fn load_sessions(
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
