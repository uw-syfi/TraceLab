mod backend;
mod cli;
mod record;
mod session;
mod summary;
mod tokens;
mod trace;
mod util;
mod workload;

use anyhow::{anyhow, Context, Result};
use clap::Parser;
use std::sync::Arc;
use std::time::Instant;
use tokio::sync::{mpsc, Semaphore};

use backend::GenerationClient;
use cli::Args;
use record::StepLog;
use session::{run_session, status_task, AppState, Stats};
use summary::{write_logs, write_summary_if_requested, ReplaySummary, RunSummary};
use tokens::{build_token_pool, load_tokenizer};
use trace::load_sessions;
use workload::WorkloadSummary;

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
            args.summary_path.as_deref(),
            RunSummary {
                workload: workload_summary,
                replay: ReplaySummary::default(),
            },
        )?;
        return Ok(());
    }

    let tokenizer = Arc::new(load_tokenizer(&args.tokenizer)?);
    // Size the synthetic token pool to the workload by default: it must exceed the longest
    // prompt so no single request repeats content, and stay larger than the session count so
    // per-session seed offsets stay distinct (otherwise distant sessions draw identical content
    // and fabricate cross-session prefix-cache hits). The 100M floor gives ~100 sessions of
    // 1M-token context their own non-overlapping content window (~400 MB of u32).
    const MIN_TOKEN_POOL: usize = 100_000_000;
    let pool_limit = args.token_pool_limit.unwrap_or_else(|| {
        workload_summary
            .max_prompt_len()
            .saturating_mul(2)
            .max(sessions.len())
            .max(MIN_TOKEN_POOL)
    });
    let token_pool = Arc::new(build_token_pool(
        &args.text_file,
        tokenizer.as_ref(),
        pool_limit,
    )?);
    if token_pool.len() < workload_summary.max_prompt_len() {
        eprintln!(
            "warning: token pool ({} tokens) is smaller than the longest prompt ({} tokens); \
             synthetic content will repeat within a single request and may distort prefix-cache \
             measurement. Use a larger --text-file corpus.",
            token_pool.len(),
            workload_summary.max_prompt_len(),
        );
    }
    let total_steps = workload_summary.total_steps();

    let client = Arc::new(GenerationClient::new(&args, tokenizer)?);

    // Fail fast if the server won't report prefix-cache hits: otherwise every measured hit
    // rate would silently read as zero. Dry-run returns earlier and never reaches here.
    // Probe the TAIL of the pool: session 0 seeds at offset 0, so a head probe would warm its
    // first round's prefix and fabricate a cache hit there.
    let probe_len = token_pool.len().min(512);
    if token_pool.len() <= probe_len + workload_summary.max_prompt_len() {
        eprintln!(
            "warning: token pool ({} tokens) is too small to fully separate the {}-token \
             preflight probe from the longest {}-token replay prompt; preflight may warm \
             replay content and distort early prefix-cache measurements. Use a larger \
             --text-file corpus or --token-pool-limit.",
            token_pool.len(),
            probe_len,
            workload_summary.max_prompt_len(),
        );
    }
    client
        .preflight_cache_check(&token_pool[token_pool.len() - probe_len..])
        .await
        .context("prefix-cache preflight failed")?;

    let state = Arc::new(AppState {
        args: args.clone(),
        client,
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
        args.summary_path.as_deref(),
        RunSummary {
            workload: workload_summary,
            replay: replay_summary,
        },
    )?;

    Ok(())
}
