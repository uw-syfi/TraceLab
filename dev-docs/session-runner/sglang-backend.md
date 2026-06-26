# SGLang Backend

## Current Status

`session_runner` now supports SGLang through the native `sglang` backend adapter.
The adapter lives in:

```text
replay/src/backend/sglang.rs
```

The backend is selected with the shared protocol selector:

```text
--backend sglang
```

Do not add SGLang-specific flags unless a future requirement cannot be represented by the existing normalized request model. Server launch and tuning options belong to the SGLang server command, not to `session_runner`.

## References

- [SGLang OpenAI APIs - Completions](https://docs.sglang.io/docs/basic_usage/openai_api_completions)
- [SGLang sampling parameters and native generation](https://docs.sglang.io/docs/basic_usage/sampling_params)

## Architecture

SGLang-specific protocol details must stay behind the backend adapter boundary:

- `replay/src/backend/mod.rs`: shared `Backend` trait, `GenRequest`, `StreamEvent`, `Usage`, `GenerationClient`, and backend builder.
- `replay/src/backend/sglang.rs`: SGLang native `/generate` payload and response normalization.
- `replay/src/backend/capabilities.rs`: static capability declaration.
- `replay/src/backend/preflight.rs`: backend-aware cache metric preflight.
- `replay/src/backend/stream.rs`: shared Server-Sent Events parsing.

The main replay path should not branch on SGLang. Session scheduling, prompt-token construction, logging, summaries, and dry-run behavior must remain backend-agnostic.

## Payload Mapping

`SglangGenerateBackend` maps the normalized `GenRequest` to native `/generate`:

```json
{
  "input_ids": [1, 2, 3],
  "stream": true,
  "return_logprob": true,
  "top_logprobs_num": 0,
  "return_text_in_logprobs": false,
  "sampling_params": {
    "max_new_tokens": 128,
    "temperature": 0.0,
    "ignore_eos": true
  }
}
```

Mapping rules:

- `input_ids` maps from `req.prompt_ids` to preserve exact token ids.
- `stream` maps from `req.stream`.
- `return_logprob: true`, `top_logprobs_num: 0`, and `return_text_in_logprobs: false` request output-token metadata without exposing another CLI flag.
- `sampling_params.max_new_tokens` maps from `req.max_tokens`.
- `sampling_params.temperature` maps from `req.temperature`.
- `sampling_params.ignore_eos` stays enabled so synthetic decode length is not collapsed by early EOS.

Do not send `model` in the native SGLang `/generate` payload. The SGLang server model is selected at server launch time. Do not decode prompt ids to text for SGLang; the runner’s prefix-cache measurement depends on exact token ids.

## Response Mapping

`SglangGenerateBackend::parse_event` normalizes native SGLang responses into `StreamEvent`:

- cumulative generated text maps to `StreamEvent.cumulative_text`.
- cumulative generated token ids map to `StreamEvent.cumulative_token_ids`.
- stop or finish fields map to `StreamEvent.finish_reason`.
- prompt, completion, total, and cached-token counters map to `Usage`.

SGLang-specific response field names should be handled in `sglang.rs`; they should not be added to `StepLog`, `session.rs`, or `summary.rs` unless the project explicitly adds a backend-independent metric.

## Cache Metric Contract

This runner is used for prefix-cache measurement, so cache accounting must stay conservative:

- The backend must accept token-id prompts.
- The backend must stream.
- The backend must return generated token ids or a trustworthy generated-token-id equivalent.
- The backend must report usage.
- The backend must expose cached prompt-token accounting.
- Startup preflight must prove a positive cache hit with repeated probe prompts.

If a target SGLang version does not expose reliable cached prompt tokens, the correct behavior is to fail preflight rather than report fabricated hit rates.

## CLI Rules

Keep the CLI surface small and shared:

- Use `--backend sglang` for the native protocol.
- Use `--base-url http://HOST:PORT` for the SGLang server root.
- Reuse shared options such as `--model`, `--temperature`, `--stream-idle-timeout-secs`, and `--max-active-sessions`.

Do not add redundant flags such as:

- `--sglang-base-url`
- `--sglang-temperature`
- `--sglang-max-new-tokens`
- `--sglang-ignore-eos`

Those duplicate existing shared options or server-side configuration.

## Validation

Minimum validation for SGLang changes:

- `--backend vllm` remains unchanged for vLLM serving.
- `--backend sglang` builds the native `/generate` payload with token-id prompts.
- SGLang streamed cumulative text is converted into replay text deltas.
- SGLang generated token ids are carried forward across rounds.
- Usage and cached prompt-token fields are normalized into `Usage`.
- Preflight fails when cached prompt-token reporting or generated token ids are unavailable.
- No SGLang-specific branch is added to session scheduling, logging, or summary generation.

## Future Changes

When SGLang changes response field names, add the new field path in `sglang.rs` and cover it with an adapter-level unit test. Do not broaden the public CLI or the runner-facing normalized types unless the existing contract cannot represent the behavior.
