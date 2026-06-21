use clap::Parser;

#[derive(Parser, Debug, Clone)]
#[command(
    author,
    version,
    about = "Session-aware closed-loop workload runner for vLLM"
)]
pub(crate) struct Args {
    /// CSV with session_id/id,round_idx,prefix_len,input_len,output_len,tool_wait_after_ms.
    #[arg(long)]
    pub(crate) trace: String,

    /// Text corpus used to build synthetic prompt/input/output token pools.
    #[arg(long)]
    pub(crate) text_file: String,

    /// tokenizer.json path or a model directory containing tokenizer.json.
    #[arg(long)]
    pub(crate) tokenizer: String,

    /// vLLM OpenAI-compatible base URL, normally http://host:port/v1.
    #[arg(long, default_value = "http://127.0.0.1:8000/v1")]
    pub(crate) base_url: String,

    #[arg(long)]
    pub(crate) model: String,

    #[arg(long, default_value_t = 0.0)]
    pub(crate) temperature: f64,

    /// Ask vLLM to continue generation after EOS until max_tokens is reached.
    #[arg(long, default_value_t = false)]
    pub(crate) ignore_eos: bool,

    /// Do not request usage accounting in streaming responses.
    #[arg(long, default_value_t = false)]
    pub(crate) disable_stream_usage: bool,

    /// Treat missing server cache-detail fields as zero cached tokens when usage is present.
    ///
    /// vLLM omits prompt_tokens_details when no tokens were cached. Enable this only when the
    /// server is launched with --enable-prompt-tokens-details; otherwise missing details mean
    /// "not reported", not necessarily zero.
    #[arg(long, default_value_t = false)]
    pub(crate) assume_missing_cache_details_zero: bool,

    #[arg(long)]
    pub(crate) max_sessions: Option<usize>,

    #[arg(long, default_value = "session_runner_output.jsonl")]
    pub(crate) log_path: String,

    #[arg(long, default_value_t = 200_000)]
    pub(crate) token_pool_limit: usize,

    /// Max seconds to wait for the next streaming chunk before failing a request.
    #[arg(long, default_value_t = 600)]
    pub(crate) stream_idle_timeout_secs: u64,

    /// Stop a session after the first failed round.
    #[arg(long, default_value_t = true)]
    pub(crate) stop_session_on_error: bool,

    /// Do not delay each session by the CSV arrival_time. Useful for old immediate-start runs.
    #[arg(long, default_value_t = false)]
    pub(crate) ignore_arrival_time: bool,

    /// Maximum number of sessions allowed to actively run at once.
    #[arg(long)]
    pub(crate) max_active_sessions: Option<usize>,

    /// Validate and summarize the workload without contacting vLLM.
    #[arg(long, default_value_t = false)]
    pub(crate) dry_run: bool,

    /// Optional model context limit used for workload validation.
    #[arg(long)]
    pub(crate) max_model_len: Option<usize>,

    /// If set with --max-model-len, skip rounds whose prompt length exceeds the limit.
    #[arg(long, default_value_t = false)]
    pub(crate) fail_on_context_overflow: bool,

    /// Optional JSON summary path for one run.
    #[arg(long)]
    pub(crate) summary_path: Option<String>,
}
