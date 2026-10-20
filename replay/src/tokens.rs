use anyhow::{anyhow, Context, Result};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::sync::Arc;
use tokenizers::Tokenizer;

use crate::backend::{ChatMessage, ChatRole};
use crate::cli::BackendKind;
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

pub(crate) enum BuiltPrompt {
    Completion(Vec<u32>),
    Chat {
        messages: Vec<ChatMessage>,
        content_token_len: usize,
    },
}

impl BuiltPrompt {
    pub(crate) fn prompt_len(&self) -> usize {
        match self {
            Self::Completion(prompt_ids) => prompt_ids.len(),
            Self::Chat {
                content_token_len, ..
            } => *content_token_len,
        }
    }
}

pub(crate) enum SessionPromptBuilder {
    Completion(PromptBuilder),
    Chat(ChatPromptBuilder),
}

impl SessionPromptBuilder {
    pub(crate) fn new(
        backend: BackendKind,
        token_provider: TokenProvider,
        tokenizer: Arc<Tokenizer>,
    ) -> Self {
        match backend {
            BackendKind::Openai => Self::Completion(PromptBuilder::new(token_provider)),
            BackendKind::Chat => Self::Chat(ChatPromptBuilder::new(token_provider, tokenizer)),
        }
    }

    pub(crate) fn build_prompt(&mut self, step: &SessionStep) -> BuiltPrompt {
        match self {
            Self::Completion(builder) => BuiltPrompt::Completion(builder.build_prompt(step)),
            Self::Chat(builder) => builder.build_prompt(step),
        }
    }

    pub(crate) fn commit_output(
        &mut self,
        prompt: BuiltPrompt,
        output_ids: Vec<u32>,
        output_text: String,
    ) {
        match (self, prompt) {
            (Self::Completion(builder), BuiltPrompt::Completion(prompt_ids)) => {
                builder.commit_output(prompt_ids, output_ids)
            }
            (Self::Chat(builder), BuiltPrompt::Chat { .. }) => {
                builder.commit_output(output_ids, output_text)
            }
            _ => unreachable!("prompt type must match session backend"),
        }
    }
}

#[derive(Clone, Debug)]
struct ChatSegment {
    role: ChatRole,
    token_ids: Vec<u32>,
    content: Arc<str>,
}

pub(crate) struct ChatPromptBuilder {
    token_provider: TokenProvider,
    tokenizer: Arc<Tokenizer>,
    context: Vec<ChatSegment>,
}

impl ChatPromptBuilder {
    fn new(token_provider: TokenProvider, tokenizer: Arc<Tokenizer>) -> Self {
        Self {
            token_provider,
            tokenizer,
            context: Vec::new(),
        }
    }

    fn build_prompt(&mut self, step: &SessionStep) -> BuiltPrompt {
        self.ensure_prefix_len(step.prefix_len);
        self.truncate_to_prefix(step.prefix_len);
        let token_ids = self.token_provider.take(step.input_len);
        let content = self
            .tokenizer
            .decode(&token_ids, false)
            .expect("session token ids must decode with their source tokenizer");
        self.context.push(ChatSegment {
            role: ChatRole::User,
            token_ids,
            content: content.into(),
        });

        let messages = self
            .context
            .iter()
            .filter(|segment| {
                !(segment.role == ChatRole::Assistant
                    && segment.token_ids.is_empty()
                    && segment.content.is_empty())
            })
            .map(|segment| ChatMessage {
                role: segment.role,
                content: segment.content.clone(),
            })
            .collect();
        BuiltPrompt::Chat {
            messages,
            content_token_len: step.prefix_len.saturating_add(step.input_len),
        }
    }

    fn commit_output(&mut self, output_ids: Vec<u32>, output_text: String) {
        self.context.push(ChatSegment {
            role: ChatRole::Assistant,
            token_ids: output_ids,
            content: output_text.into(),
        });
    }

    fn ensure_prefix_len(&mut self, prefix_len: usize) {
        let current_len: usize = self
            .context
            .iter()
            .map(|segment| segment.token_ids.len())
            .sum();
        if current_len >= prefix_len {
            return;
        }
        let filler = self.token_provider.take(prefix_len - current_len);
        if let Some(last) = self.context.last_mut() {
            let filler_content = self
                .tokenizer
                .decode(&filler, false)
                .expect("session token ids must decode with their source tokenizer");
            last.token_ids.extend(filler);
            let mut content = String::with_capacity(last.content.len() + filler_content.len());
            content.push_str(&last.content);
            content.push_str(&filler_content);
            last.content = content.into();
        } else {
            let content = self
                .tokenizer
                .decode(&filler, false)
                .expect("session token ids must decode with their source tokenizer");
            self.context.push(ChatSegment {
                role: ChatRole::System,
                token_ids: filler,
                content: content.into(),
            });
        }
    }

    fn truncate_to_prefix(&mut self, prefix_len: usize) {
        let mut remaining = prefix_len;
        let mut keep_len = 0;
        for index in 0..self.context.len() {
            if remaining == 0 {
                break;
            }
            let segment_len = self.context[index].token_ids.len();
            if segment_len <= remaining {
                remaining -= segment_len;
                keep_len = index + 1;
            } else {
                self.context[index].token_ids.truncate(remaining);
                let content = self
                    .tokenizer
                    .decode(&self.context[index].token_ids, false)
                    .expect("session token ids must decode with their source tokenizer");
                self.context[index].content = content.into();
                keep_len = index + 1;
                remaining = 0;
            }
        }
        debug_assert_eq!(remaining, 0);
        self.context.truncate(keep_len);
    }
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
