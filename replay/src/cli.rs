use clap::{Parser, ValueEnum};

/// Inference server selected with `--backend`.
#[derive(ValueEnum, Clone, Copy, Debug)]
pub(crate) enum BackendKind {
    /// vLLM OpenAI-compatible `/completions`.
    Vllm,
    /// SGLang native `/generate`.
    Sglang,
    /// Reserved for llama.cpp native `/completion`; not implemented yet.
    Llamacpp,
}

#[derive(Parser, Debug, Clone)]
#[command(
    author,
    version,
    about = "Session-aware closed-loop workload runner for inference backends"
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

    /// Backend base URL. vLLM normally uses http://host:port/v1; native backends normally use
    /// the server root.
    #[arg(long, default_value = "http://127.0.0.1:8000/v1")]
    pub(crate) base_url: String,

    #[arg(long)]
    pub(crate) model: String,

    /// Inference server backend.
    #[arg(long, value_enum, default_value = "vllm")]
    pub(crate) backend: BackendKind,

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

    /// Maximum number of sessions allowed to actively run at once.
    #[arg(long)]
    pub(crate) max_active_sessions: Option<usize>,

    /// Validate and summarize the workload without contacting a serving backend.
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
