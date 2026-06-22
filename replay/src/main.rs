mod cli;
mod record;
mod session;
mod summary;
mod tokens;
mod trace;
mod util;
mod vllm;
mod workload;

use anyhow::{anyhow, Result};
use clap::Parser;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{mpsc, Semaphore};

use cli::Args;
use record::StepLog;
use session::{run_session, status_task, AppState, Stats};
use summary::{write_logs, write_summary_if_requested, ReplaySummary, RunSummary};
use tokens::{build_token_pool, load_tokenizer};
use trace::load_sessions;
use vllm::VllmClient;
use workload::WorkloadSummary;

#[tokio::main]
async fn main() -> Result<()> {
    let args = Args::parse();
    fdlimit::raise_fd_limit().ok();

    if args.max_active_sessions == Some(0) {
        return Err(anyhow!("--max-active-sessions must be greater than 0"));
    }
    if args.fail_on_context_overflow && args.max_model_len.is_none() {
        return Err(anyhow!("--fail-on-context-overflow requires --max-model-len"));
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
    let total_steps = workload_summary.total_steps();

    let vllm = Arc::new(VllmClient::new(&args, tokenizer)?);

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
