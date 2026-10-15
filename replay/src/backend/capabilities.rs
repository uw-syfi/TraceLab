#[derive(Clone, Copy, Debug)]
pub(crate) struct BackendCapabilities {
    pub(crate) accepts_token_id_prompt: bool,
    pub(crate) supports_streaming: bool,
    pub(crate) returns_generated_token_ids: bool,
    pub(crate) returns_usage: bool,
    pub(crate) returns_cached_prompt_tokens: bool,
    pub(crate) supports_cache_preflight: bool,
    pub(crate) supports_ignore_eos: bool,
}

impl BackendCapabilities {
    pub(crate) const fn strict_cache_replay() -> Self {
        Self {
            accepts_token_id_prompt: true,
            supports_streaming: true,
            returns_generated_token_ids: true,
            returns_usage: true,
            returns_cached_prompt_tokens: true,
            supports_cache_preflight: true,
            supports_ignore_eos: true,
        }
    }
}
