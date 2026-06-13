---
name: coding-trace-normalize
description: "Explain and work with the normalized coding-trace JSONL row format produced by extract_claude_rounds.py, extract_codex_rounds.py, and collect_llm_traces.py. Use when interpreting normalized rows, provider differences, token semantics, Claude cache creation/read/uncached fields, prefix versus append accounting, input-event summaries, timing_events, tools metadata, trace_key identity, or converting raw Claude/Codex concepts into the common analysis schema."
---

# Coding Trace Normalize

## Overview

Use this skill to interpret the common JSONL format produced by this repo. Each normalized row represents one LLM invocation, even though the raw Claude and Codex logs use different event schemas.

## First Steps

1. Work from the coding-trace repo root, identified by `pyproject.toml`, `README.md`, and `scripts/collect_llm_traces.py`.
2. Read `README.md` section "Normalized Rows" for the public contract.
3. Read `example_sessions/derived/README.md` and inspect `example_sessions/derived/round_trace.expanded.json` for a small worked example.
4. If a field is ambiguous, inspect the extractor that writes it: `scripts/extract_claude_rounds.py`, `scripts/extract_codex_rounds.py`, or `scripts/collect_llm_traces.py`.

## Row Identity

Common top-level fields:

- `provider`: `claude` or `codex`.
- `session_id`: provider session identifier.
- `round_index`: zero-based order within the extracted session when available.
- `round_id`: provider-specific round identifier.
- `trace_key`: stable dedupe key, built as `{provider}:{session_id}:{round_id}`. Some `session_id` values already include a provider prefix, so keys such as `claude:claude:...:msg_...` are expected.
- `model`: model reported by the provider trace.
- `project`, `store`, `user`, `home`, `turn_id`, `cwd`, or `session_file`: private extracted traces may include local context fields. Sanitized traces pseudonymize `project`, `user`, and ids, and remove local path/source fields.
- `current_input_event_count`, `current_user_message_count`, `current_tool_result_count`, `current_user_message_chars`, `current_tool_result_chars`, `current_input_chars`, and `first_input_event_type`: summaries derived from the row's input-side `timing_events`.

## Token Semantics

Normalized input fields follow this invariant:

```text
input_tokens_total = prefix_tokens + newly_append_tokens
```

Provider mappings:

- Claude `prefix_tokens` comes from `cache_read_input_tokens`.
- Claude `newly_append_tokens` comes from `input_tokens + cache_creation_input_tokens`.
- Claude `input_tokens_total` is `cache_read_input_tokens + cache_creation_input_tokens + input_tokens`.
- Claude rows also keep the provider-specific split as `claude_uncached_input_tokens`, `claude_cache_creation_input_tokens`, and `claude_cache_read_input_tokens`.
- Claude `reasoning_output_tokens` is normally `null` because Claude examples do not expose a separate reasoning-token field in this schema.
- Codex `prefix_tokens` comes from `cached_input_tokens`.
- Codex `newly_append_tokens` is `input_tokens - cached_input_tokens`, clamped at zero by the extractor.
- Codex `input_tokens_total` comes from `input_tokens`.
- Codex rows set the Claude-specific cache fields to `null`.
- Codex `reasoning_output_tokens` is a subset of `output_tokens`, not extra output.

Interpretation guidance:

- `prefix_tokens` approximates prompt-cache hit tokens.
- `newly_append_tokens` approximates prompt-side work not served from prompt cache.
- These are provider prompt-cache semantics, not a direct dump of local serving-engine KV-cache objects.
- `output_tokens` is inclusive generated output for the round. Do not add `reasoning_output_tokens` to it.

## Timing Events

`timing_events[]` preserves the ordered trace-observed timeline for the normalized round. Common event types:

- `user_message`: visible user input that triggered or preceded a model round.
- `tool_result`: tool output returned to the model before a later model round.
- `reasoning`: existence of a reasoning or thinking output item, usually without readable private content.
- `text`: visible assistant text.
- `tool_call`: model-emitted tool request.
- `usage_report`: Codex token accounting event.

For an "input ready to next tool input" latency proxy, use the latest `user_message` or `tool_result` before the first following `tool_call`, then subtract timestamps. For reasoning timing, the summarizer distinguishes latest input to reasoning marker/end from the exact-reasoning TPOT subset. TPOT and TTFT residual estimates require exact numeric `reasoning_output_tokens`; otherwise they should remain `null`.

The current-input summary fields are derived only from `user_message` and `tool_result` timing events in the row. They are convenience counters for analysis, not replacements for `timing_events[]` when exact ordering matters.

## Tool Metadata

Each row can contain `tools[]`, with one object per model-emitted tool call:

- `tool_index`: order within the row.
- `tool_name`: provider/tool runner name such as `Bash`, `exec_command`, `Read`, or `apply_patch`.
- `tool_call_id`: call id, paired with timing events when available.
- `emitted_at`: timestamp when the model emitted the tool call.
- `result_at`: timestamp when a tool result was observed, or `null`.
- `input`: raw tool input in private extracted traces. Sanitized traces remove it.
- `input_chars`: serialized input size retained even when `input` is removed.
- `result_chars`: result size; full outputs are not kept in normalized rows.
- `tool_wall_latency_ms`: trace-observed `result_at - emitted_at`.
- `tool_internal_latency_ms`: tool/runner-reported duration when available.
- `is_error`: error status if inferable.

Analysis experiments use `tool_internal_latency_ms` when present, then fall back to `tool_wall_latency_ms`. The multi-round CSV exporter uses `tool_wall_latency_ms` by default unless `--tool-latency-source internal` is passed.

## Quick Inspection

Inspect a small public normalized example:

```bash
sed -n '1,120p' example_sessions/derived/round_trace.expanded.json
```

Summarize a normalized file:

```bash
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.public.jsonl --json
```

When answering field questions, cite the relevant extractor or docs and avoid exposing private `tools[].input` or local context from unsanitized rows unless the user explicitly asks to inspect their own private data.
