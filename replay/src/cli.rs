use clap::{Parser, ValueEnum};

/// Inference-server wire protocol selected with `--backend`.
#[derive(ValueEnum, Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum BackendKind {
    /// OpenAI-compatible `/completions` (vLLM, and SGLang's OpenAI endpoint).
    Openai,
    /// OpenAI-compatible `/chat/completions` with cumulative role-based messages.
    Chat,
}

impl BackendKind {
    pub(crate) fn default_cache_probe_tokens(self) -> usize {
        match self {
            Self::Openai => 512,
            Self::Chat => 4096,
        }
    }
}

#[derive(Parser, Debug, Clone)]
#[command(
    author,
    version,
    about = "Session-aware closed-loop workload runner for OpenAI-compatible inference servers"
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

    /// OpenAI-compatible base URL, normally http://host:port/v1.
    #[arg(long, default_value = "http://127.0.0.1:8000/v1")]
    pub(crate) base_url: String,

    /// Exact request endpoint. Overrides --base-url and the backend's default path.
    #[arg(long)]
    pub(crate) endpoint_url: Option<String>,

    #[arg(long)]
    pub(crate) model: String,

    /// Inference-server wire protocol. Use `chat` for role-based messages.
    #[arg(long, value_enum, default_value = "openai")]
    pub(crate) backend: BackendKind,

    /// Name of an environment variable containing a Bearer API key.
    #[arg(long)]
    pub(crate) api_key_env: Option<String>,

    /// Token count used by the strict two-request prefix-cache preflight.
    /// Defaults to 512 for completion and 4096 for chat.
    #[arg(long)]
    pub(crate) cache_probe_tokens: Option<usize>,

    /// Provider-specific top-level JSON merged into Chat Completions requests.
    #[arg(long, value_name = "JSON")]
    pub(crate) extra_body_json: Option<String>,

    #[arg(long, default_value_t = 0.0)]
    pub(crate) temperature: f64,

    #[arg(long)]
    pub(crate) max_sessions: Option<usize>,

    #[arg(long, default_value = "session_runner_output.jsonl")]
    pub(crate) log_path: String,

    /// Cap on synthetic token-pool size. Defaults to cover the workload's longest prompt with
    /// headroom, so synthetic content never repeats within a single request.
    #[arg(long)]
    pub(crate) token_pool_limit: Option<usize>,

    /// Max seconds to wait for the next streaming chunk before failing a request.
    #[arg(long, default_value_t = 600)]
    pub(crate) stream_idle_timeout_secs: u64,

    /// Stop a session after the first failed round.
    #[arg(long, default_value_t = true)]
    pub(crate) stop_session_on_error: bool,

    /// Maximum number of sessions allowed to actively run at once.
    #[arg(long)]
    pub(crate) max_active_sessions: Option<usize>,

    /// Validate and summarize the workload without contacting the inference server.
    #[arg(long, default_value_t = false)]
    pub(crate) dry_run: bool,

    /// Optional content-token context limit used for workload validation.
    /// Chat-template overhead is not included.
    #[arg(long)]
    pub(crate) max_model_len: Option<usize>,

    /// If set with --max-model-len, skip rounds whose prompt length exceeds the limit.
    #[arg(long, default_value_t = false)]
    pub(crate) fail_on_context_overflow: bool,

    /// Optional JSON summary path for one run.
    #[arg(long)]
    pub(crate) summary_path: Option<String>,
}
