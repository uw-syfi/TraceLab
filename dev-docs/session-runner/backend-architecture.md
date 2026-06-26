# Session Runner Backend Architecture

## Purpose

This document defines the backend architecture for supporting multiple serving engines in `session_runner`, specifically:

- vLLM support through the OpenAI-compatible completions adapter.
- SGLang support through the native `/generate` adapter.
- Reserved llama.cpp support through a future native `/completion` adapter.

The key requirement is that additional backends must follow the same runner-facing contract as the current vLLM path. Backend-specific code may differ only at the wire-protocol boundary.

## Design Goal

The session runner should be a workload runner, not a collection of provider-specific runners.

```text
CSV workload
  -> shared session runner core
  -> normalized backend interface
  -> backend adapter
  -> serving engine
```

The runner core owns workload semantics:

- session arrival scheduling
- closed-loop round ordering
- prompt construction from token ids
- output-token carry-forward
- tool waits
- context-overflow handling
- JSONL step logs
- run summaries

Backend adapters own serving protocol details:

- endpoint suffix
- request payload shape
- streaming format
- usage parsing
- generated-token-id extraction
- cached-token accounting
- cache preflight behavior

## Current Baseline: vLLM / OpenAI-Compatible Backend

The current vLLM path is implemented as the `vllm` backend. It targets:

```text
POST {base_url}/completions
```

It sends:

- `model`
- `prompt` as integer token ids
- `max_tokens`
- `temperature`
- `stream`
- `ignore_eos`
- `return_token_ids`
- `stream_options.include_usage` when streaming

It normalizes responses into:

- text deltas
- generated token ids when available
- finish reason
- prompt/completion/total usage
- cached prompt tokens

All future backend adapters should preserve this same normalized output surface.

## Target File Layout

The backend implementation is split by protocol adapter so SGLang and llama.cpp can be developed without changing the session runner core:

```text
replay/src/backend/
  mod.rs
  client.rs
  vllm.rs
  sglang.rs
  llamacpp.rs
  capabilities.rs
  preflight.rs
  stream.rs
```

Responsibilities:

- `mod.rs`: shared backend trait, normalized request/response types, and backend builder.
- `client.rs`: shared HTTP execution, streaming, preflight, and logging normalization.
- `vllm.rs`: vLLM adapter using the OpenAI-compatible `/completions` protocol.
- `sglang.rs`: SGLang native `/generate` adapter.
- `llamacpp.rs`: llama.cpp native `/completion` adapter.
- `capabilities.rs`: backend capability declarations.
- `preflight.rs`: cache and usage preflight logic.
- `stream.rs`: shared stream parsing helpers.

This split is an internal organization change. It should not change CLI behavior or log schema.

## Stable Runner-Facing Contract

Every backend must map its native protocol into the same normalized concepts.

### Normalized Request

The runner sends the same logical request to every backend:

| Field | Meaning |
|---|---|
| model | Served model name or id. |
| prompt token ids | Exact token ids constructed by `tokens.rs`. |
| max output tokens | Target decode length from the workload. |
| temperature | Sampling temperature. |
| stream | Whether the runner expects streamed output. |

### Normalized Stream Event

Each backend must normalize response chunks or full responses into:

| Field | Meaning |
|---|---|
| text delta | Generated text fragment, if present. |
| generated token ids | Exact output token ids required for strict session carry-forward. |
| finish reason | Server stop reason, normalized as a string. |
| usage | Token accounting, if available. |

### Normalized Usage

Usage must keep the same meaning across backends:

| Field | Meaning |
|---|---|
| prompt tokens | Total prompt tokens processed for the request. |
| completion tokens | Generated output tokens. |
| total tokens | Prompt plus completion tokens, when reported. |
| cached prompt tokens | Prompt tokens reused from cache. |

The `server_prefix_hit_rate` log field should only be emitted when `cached prompt tokens` is reliable.

## Capability Model

Backends should not be treated only as provider names. They should declare capabilities.

Recommended capabilities:

| Capability | Why it matters |
|---|---|
| accepts token-id prompt | Required for exact workload replay. |
| supports streaming | Required for current runner execution path. |
| returns generated token ids | Needed for exact output carry-forward. |
| returns usage | Needed for token accounting. |
| returns cached prompt tokens | Needed for prefix-cache hit-rate metrics. |
| supports cache preflight | Needed to fail fast when cache metrics are unavailable. |
| supports ignore-eos behavior | Needed to preserve target decode length for synthetic prompts. |

The runner should make decisions from capabilities, not from backend names.

## Backend Matrix

| Backend | Initial path | Native path | Expected role |
|---|---|---|---|
| vLLM | `vllm` | `/completions` | Baseline implementation using vLLM's OpenAI-compatible protocol. |
| SGLang | `sglang` | `/generate` | Native adapter implemented first so SGLang protocol differences stay isolated. |
| llama.cpp | `llamacpp` | `/completion` | Reserved for the next backend step. |

## SGLang Architecture

SGLang native support targets:

```text
--backend sglang
--base-url http://HOST:PORT
```

The native adapter must:

- Accept integer token-id prompts.
- Map shared generation settings into `/generate` without SGLang-specific CLI flags.
- Send `ignore_eos` through native sampling parameters.
- Request output-token metadata without adding user-facing flags.
- Avoid sending `model` in the native `/generate` payload; SGLang selects the model at server launch.
- Return usage in streaming mode.
- Return generated token ids or an equivalent cumulative token-id field.
- Expose reliable cached prompt-token accounting.
- Pass the runner's cache preflight.

The OpenAI-compatible SGLang path can still be tested manually through a separate adapter later, but it should not introduce extra runner flags. If that path needs protocol-specific request shaping later, add a backend adapter rather than adding SGLang-only CLI switches.

The native adapter must preserve the same normalized `Usage` and stream event meanings and avoid SGLang-specific fields in `session.rs`, `record.rs`, or `summary.rs`.

If SGLang cannot report cached prompt tokens reliably, it should not claim prefix-cache measurement support.

## llama.cpp Architecture

llama.cpp is reserved as a native `/completion` backend because that protocol documents cache and token-id fields.

Native llama.cpp support should target:

```text
POST {base_url}/completion
```

The adapter should map normalized runner fields to llama.cpp semantics:

| Runner concept | llama.cpp concept |
|---|---|
| prompt token ids | `prompt` as token-id array |
| max output tokens | `n_predict` |
| temperature | `temperature` |
| stream | `stream` |
| enable prompt cache reuse | `cache_prompt` |
| return output token ids | `return_tokens` |

The adapter should map response fields conservatively:

| llama.cpp field | Normalized meaning |
|---|---|
| `content` | text delta |
| `tokens` | generated token ids |
| `stop` / `stop_type` | finish reason |
| `tokens_cached` | cached prompt tokens |
| `tokens_evaluated` | do not use as prompt-token denominator unless verified |

For the first version, use the runner's submitted prompt length as the prompt-token denominator unless tests prove that llama.cpp's server-side field has the same meaning.

## Cache Preflight Architecture

Cache preflight should be shared conceptually but backend-aware in implementation.

Strict default behavior:

```text
If cache accounting is required:
  send the same probe prompt twice
  require the second response to report cached prompt tokens > 0
  fail if usage or cache fields are missing
```

Backends that cannot prove cache accounting should not silently produce cache-hit metrics.

Possible future mode:

```text
functional replay without cache metrics
```

That mode should be explicit if added. It should not be the default.

## Logging Contract

All backends must preserve the same JSONL log schema.

Backend-specific data should not leak into log fields unless it is normalized first.

Allowed normalized fields include:

- `server_prompt_tokens`
- `server_completion_tokens`
- `server_total_tokens`
- `server_cached_prompt_tokens`
- `server_uncached_prompt_tokens`
- `server_prefix_hit_rate`
- `server_prefix_hit_rate_delta`
- `finish_reason`
- output token counts
- request status and error

Avoid backend-specific log fields such as:

- `llamacpp_tokens_evaluated`
- `sglang_radix_cache_tokens`
- `vllm_prompt_tokens_details_raw`

If raw backend diagnostics are needed later, add a separate debug field or sidecar log after review.

## CLI Contract

The CLI should stay small and backend-neutral.

Recommended backend choices:

```text
--backend vllm
--backend sglang
--backend llamacpp
```

Shared options should remain shared:

- `--base-url`
- `--model`
- `--trace`
- `--text-file`
- `--tokenizer`
- `--max-model-len`
- `--stream-idle-timeout-secs`
- `--max-active-sessions`
- `--summary-path`
- `--log-path`

Do not add provider-specific duplicates unless the option changes request semantics in a way the normalized interface cannot express.

Avoid:

- `--sglang-temperature`
- `--llamacpp-max-tokens`
- `--vllm-cache-mode`

Server launch configuration belongs to the server launch command, not the workload runner.

## Development Order

Recommended sequence:

1. Preserve current `vllm` behavior and tests.
2. Keep vLLM OpenAI-compatible logic isolated in `backend/vllm.rs`.
3. Keep backend capabilities explicit in `backend/capabilities.rs`.
4. Keep cache preflight in backend-aware preflight logic.
5. Keep SGLang native `/generate` support validated by adapter tests and real-server runs.
6. Keep llama.cpp reserved behind `llamacpp` until it is implemented.
7. Add llama.cpp native support if OpenAI-compatible llama.cpp lacks reliable cache or token-id support.
8. Keep JSONL logs and summaries stable across all backends.

## Acceptance Criteria

Shared criteria:

- Current vLLM/OpenAI-compatible behavior remains unchanged.
- `session.rs` remains backend-agnostic.
- `tokens.rs` remains backend-agnostic.
- `record.rs` and `summary.rs` remain backend-agnostic.
- Every backend maps to the same normalized request, stream event, and usage concepts.
- Cache metrics are emitted only when reliable.
- Missing cache accounting fails fast by default.

SGLang criteria:

- Native `/generate` can replay at least a single-session trace.
- The adapter sends no SGLang-specific CLI-derived fields and does not send `model`.
- Startup preflight verifies generated token ids and cached prompt-token accounting.
- Unknown native response fields stay isolated in `backend/sglang.rs`.

llama.cpp criteria:

- Native `/completion` can replay at least a single-session trace.
- Token-id prompts are preserved.
- Generated token ids are carried forward.
- `tokens_cached` is validated by a two-request cache preflight.
- Multi-session behavior is tested for slot-related cache effects before adding slot pinning.

## Non-Goals

Do not build:

- a dynamic plugin system
- provider-specific session runners
- backend-specific log schemas
- backend-specific scheduling paths
- a server launcher inside `session_runner`

The scalable architecture is one shared runner core plus narrow backend adapters.
