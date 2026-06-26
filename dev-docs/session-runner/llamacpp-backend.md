# Support llama.cpp Backend

## Background

The llama.cpp server provides both an OpenAI-compatible path and a native `/completion` path. The official server README states:

- `/completion` is not an OpenAI-compatible endpoint; OpenAI-compatible clients should use `/v1/completions`.
- `/completion` accepts `prompt` as either a string or an array of token ids.
- `cache_prompt` can reuse the KV cache from the previous request.
- `return_tokens` can return generated token ids.
- Responses include fields such as `tokens_cached` and `tokens_evaluated`.

Reference:

- [llama.cpp server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

This means llama.cpp should first be validated through the OpenAI-compatible path. If cache and token-id information are insufficient there, then add a native `/completion` adapter.

## Goals

Support running `session_runner` against llama.cpp server while preserving the current code structure:

- Keep the existing `--backend vllm` behavior unchanged.
- Use native `/completion` only as an adapter when it is genuinely required.
- Keep llama.cpp-specific slot, timing, and cache fields out of the session runner main flow.
- Verify cache accounting empirically; do not trust field names without confirming semantics.

## Phase 1: Validate the OpenAI-Compatible Path

First test:

```text
--backend vllm
--base-url http://HOST:PORT/v1
```

Validate:

- Whether `/v1/completions` accepts integer token id prompts.
- Whether `/v1/completions` accepts or explicitly ignores the nonstandard fields currently sent by the vLLM adapter: `ignore_eos`, `return_token_ids`, and `stream_options.include_usage`.
- Whether it returns usage.
- Whether usage includes cached prompt token accounting.
- Whether it returns generated token ids for exact session carry-forward.
- Whether the current `preflight_cache_check` passes.

If a future OpenAI-compatible llama.cpp adapter satisfies cache measurement, keep it isolated as an adapter rather than adding llama.cpp-specific flags.

## Phase 2: Native `/completion` Design

If the OpenAI-compatible path cannot provide cache or token-id statistics, add:

```rust
BackendKind::Llamacpp
LlamaCppCompletionBackend
```

Endpoint:

```text
/completion
```

When using the native backend, `--base-url` should point to the llama.cpp server root:

```text
--base-url http://HOST:PORT
```

Do not pass `/v1`, because `/completion` is not under `/v1`.

## Payload Mapping

Map `GenRequest` to llama.cpp `/completion`:

```json
{
  "prompt": [1, 2, 3],
  "n_predict": 128,
  "temperature": 0.0,
  "stream": true,
  "cache_prompt": true,
  "return_tokens": true
}
```

Field meanings:

- `prompt` uses `req.prompt_ids` to preserve exact tokens.
- `n_predict` maps to `req.max_tokens`.
- `temperature` maps to `req.temperature`.
- `stream` maps to `req.stream`.
- `cache_prompt: true` enables prompt cache reuse.
- `return_tokens: true` requests generated token ids for exact session carry-forward.

Even if streaming responses may already include `tokens` because `stream: true`, keep `return_tokens: true` so non-streaming preflight and streaming replay have consistent field expectations. Verify behavior against the exact target llama.cpp version.

Do not add `id_slot` by default yet. Slot semantics affect cache reuse scope and should be designed only after llama.cpp multi-session behavior is confirmed.

## Response Mapping

llama.cpp streaming uses Server-Sent Events. The native adapter should map each JSON event into `StreamEvent`:

- `content` -> `text_delta`
- `tokens` -> `token_ids`
- `stop` or `stop_type` -> `finish_reason`
- token statistics in the final response -> `Usage`

The official documentation indicates that, before completion ends, streaming mode typically returns only `content`, `tokens`, and `stop`. Usage may therefore appear only in the final event or in a non-streaming probe. Confirm with actual responses; do not assume every chunk carries complete usage.

## Cache Accounting Mapping

llama.cpp native responses may include:

- `tokens_cached`
- `tokens_evaluated`
- `timings`
- `truncated`

Recommended conservative mapping for the first version:

- `Usage.cached_prompt_tokens = tokens_cached`
- `Usage.prompt_tokens = req.prompt_ids.len()`, unless testing proves `tokens_evaluated` is exactly the server's processed prompt-token total.
- `Usage.completion_tokens` should prefer the number of generated token ids; use a server field only if the final response provides a more reliable value.

The reason is that `tokens_evaluated` can be easy to misread across versions or configurations. The project primarily cares about prefix hit rate, so the denominator should initially use the prompt token count submitted by the runner instead of treating an internal server counter as the CSV-level prompt count.

Before implementation, validate with a two-identical-prompt probe:

- The first request has `tokens_cached` equal to zero or low.
- The second identical request has clearly higher `tokens_cached`.
- `server_prefix_hit_rate` moves in the expected direction.

## Slot Semantics Risk

llama.cpp `id_slot` can assign a completion task to a specific slot; the default `-1` assigns an idle slot. This can affect session cache behavior:

- With a single slot, repeated prompt cache behavior is easy to validate.
- With multiple slots, if `id_slot` is unspecified, adjacent rounds from the same session may be assigned to different slots.
- If slots do not share KV cache, prefix-cache measurement is distorted.

Do not extend `GenRequest` for slot pinning in the first version. First validate single-slot or default-slot behavior. Only if multi-session measurements prove that slot pinning is needed should `GenRequest` be extended, for example by adding session metadata and passing it from `run_session` to `run_step`.

That extension changes the normalized backend interface and must be reviewed separately. Do not combine it with the first native backend implementation.

## Acceptance Tests

Minimum validation:

- Default `vllm` backend behavior is unaffected.
- `llamacpp` can complete a single-session trace.
- Preflight can verify `tokens_cached > 0`.
- JSONL logs contain reasonable `server_cached_prompt_tokens` values.
- Output token ids are carried forward correctly.
- Streaming timeout, HTTP error, and JSON parsing error handling reuse the existing failure path.

Multi-session validation:

- First validate determinism with `--max-active-sessions 1`.
- Then increase concurrency and validate slot behavior.
- If cache hit rate deviates substantially from expectation, inspect slot assignment before changing the workload or trace.

## Out-of-Scope Design

General backend extension rules are defined in [README.md](./README.md). For this phase, also do not:

- Add a llama.cpp-specific scheduler.
- Expose `id_slot` as a required first-version parameter.
- Wrap llama.cpp server startup inside the session runner.
- Parse every `timings` field unless there is already a logging requirement.
