use anyhow::{anyhow, Context, Result};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::sync::Arc;
use tokenizers::Tokenizer;

use crate::trace::SessionStep;

/// Cursor over a shared synthetic token pool. Each session seeds at a distinct
/// offset so replayed prompts are not byte-identical across sessions.
pub(crate) struct TokenProvider {
    pool: Arc<Vec<u32>>,
    cursor: usize,
}

impl TokenProvider {
    pub(crate) fn new(pool: Arc<Vec<u32>>, seed_offset: usize) -> Result<Self> {
        if pool.is_empty() {
            return Err(anyhow!("token pool is empty"));
        }
        Ok(Self {
            cursor: seed_offset % pool.len(),
            pool,
        })
    }

    fn take(&mut self, len: usize) -> Vec<u32> {
        let mut out = Vec::with_capacity(len);
        for _ in 0..len {
            out.push(self.pool[self.cursor]);
            self.cursor = (self.cursor + 1) % self.pool.len();
        }
        out
    }
}

/// Builds each round's prompt token ids by replaying `prefix_len` prior-context
/// tokens and appending `input_len` fresh synthetic tokens.
pub(crate) struct PromptBuilder {
    token_provider: TokenProvider,
    context_tokens: Vec<u32>,
}

impl PromptBuilder {
    pub(crate) fn new(token_provider: TokenProvider) -> Self {
        Self {
            token_provider,
            context_tokens: Vec::new(),
        }
    }

    pub(crate) fn build_prompt(&mut self, step: &SessionStep) -> Vec<u32> {
        if self.context_tokens.len() < step.prefix_len {
            let need = step.prefix_len - self.context_tokens.len();
            self.context_tokens.extend(self.token_provider.take(need));
        }

        let mut prompt_ids = self.context_tokens[..step.prefix_len].to_vec();
        prompt_ids.extend(self.token_provider.take(step.input_len));
        prompt_ids
    }

    /// Carry this round's prompt plus the model's real output tokens forward as the next round's
    /// context. Using the real output (not synthetic) keeps the previous-output region of the next
    /// prefix byte-identical to what the server cached, so it stays prefix-cache-hittable.
    pub(crate) fn commit_output(&mut self, prompt_ids: Vec<u32>, output_ids: Vec<u32>) {
        self.context_tokens = prompt_ids;
        self.context_tokens.extend(output_ids);
    }
}

/// Load a tokenizer from a local tokenizer.json / model directory, or download
/// it from the Hugging Face Hub when the path is a repo id.
pub(crate) fn load_tokenizer(path: &str) -> Result<Tokenizer> {
    let path = std::path::Path::new(path);
    let tokenizer = if path.exists() {
        let tokenizer_path = if path.is_dir() {
            path.join("tokenizer.json")
        } else {
            path.to_path_buf()
        };

        Tokenizer::from_file(&tokenizer_path).map_err(|err| {
            anyhow!(
                "failed to load tokenizer {}: {err}",
                tokenizer_path.display()
            )
        })?
    } else {
        let api = hf_hub::api::sync::Api::new()
            .map_err(|err| anyhow!("failed to create Hugging Face API client: {err}"))?;
        let repo = api.model(path.to_string_lossy().to_string());
        let tokenizer_path = repo.get("tokenizer.json").map_err(|err| {
            anyhow!(
                "failed to download tokenizer.json for {}: {err}",
                path.display()
            )
        })?;
        Tokenizer::from_file(tokenizer_path)
            .map_err(|err| anyhow!("failed to load downloaded tokenizer: {err}"))?
    };
    Ok(tokenizer)
}

/// Tokenize the text corpus into a bounded pool of token ids used as synthetic
/// prompt/input/output content.
pub(crate) fn build_token_pool(
    text_file: &str,
    tokenizer: &Tokenizer,
    limit: usize,
) -> Result<Vec<u32>> {
    let file = File::open(text_file)
        .with_context(|| format!("failed to open text corpus: {text_file}"))?;
    let reader = BufReader::new(file);
    let mut pool = Vec::with_capacity(limit);

    for line in reader.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let encoding = tokenizer
            .encode(line, false)
            .map_err(|err| anyhow!("tokenizer encode failed: {err}"))?;
        pool.extend(encoding.get_ids());
        if pool.len() >= limit {
            pool.truncate(limit);
            break;
        }
    }

    if pool.is_empty() {
        return Err(anyhow!("text corpus produced an empty token pool"));
    }
    Ok(pool)
}
