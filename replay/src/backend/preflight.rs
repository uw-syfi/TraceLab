use anyhow::{anyhow, Result};

use super::BackendCapabilities;

pub(crate) fn ensure_cache_preflight_supported(
    backend_name: &str,
    capabilities: BackendCapabilities,
) -> Result<()> {
    let missing = [
        (!capabilities.accepts_token_id_prompt).then_some("token-id prompts"),
        (!capabilities.supports_streaming).then_some("streaming"),
        (!capabilities.returns_generated_token_ids).then_some("generated token ids"),
        (!capabilities.returns_usage).then_some("usage reporting"),
        (!capabilities.returns_cached_prompt_tokens).then_some("cached prompt tokens"),
        (!capabilities.supports_cache_preflight).then_some("cache preflight"),
        (!capabilities.supports_ignore_eos).then_some("ignore-eos behavior"),
    ]
    .into_iter()
    .flatten()
    .collect::<Vec<_>>();

    if missing.is_empty() {
        Ok(())
    } else {
        Err(anyhow!(
            "preflight: backend {backend_name} does not declare required capability/capabilities: {}",
            missing.join(", ")
        ))
    }
}
