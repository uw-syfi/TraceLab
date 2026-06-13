---
name: coding-trace-raw
description: "Read and explain raw Claude Code and Codex CLI session logs in this coding-trace repo. Use when inspecting example_sessions, raw .claude/.codex JSONL records, provider record families, duplicate-looking runtime layers, raw tool-call pairing, exposed or omitted fields, token accounting records before normalization, or representative raw windows from find_representative_session_segments.py."
---

# Coding Trace Raw

## Overview

Use this skill to reason about raw coding-agent trace files before they are converted into normalized round rows. Keep the distinction clear: raw traces preserve provider-specific runtime records, while normalized traces collapse those records into one JSONL row per LLM invocation.

## First Steps

1. Work from the coding-trace repo root, identified by `pyproject.toml`, `README.md`, and `scripts/collect_llm_traces.py`.
2. Identify the provider before interpreting records: Claude Code raw logs and Codex CLI raw logs have different schemas.
3. Prefer the public examples for orientation:
   - `example_sessions/claude/explanation.md`
   - `example_sessions/codex/explanation.md`
   - `example_sessions/claude/trace.json`
   - `example_sessions/codex/trace.json`
4. Treat public `trace.json` examples and generated `*.expanded.json` files as human-readable record dumps with separators. Individual JSON blocks are valid, but the whole expanded file is not one JSON document.
5. Avoid reading large private trace files wholesale. Use targeted `sed`, `rg`, `head`, `python -c`, or `scripts/find_representative_session_segments.py`.

## Claude Raw Logs

Claude Code raw records commonly include:

- `user`: visible user messages and tool result messages. Tool results are stored as user records with `message.content[]` blocks of type `tool_result`.
- `assistant`: model output records. One assistant message can be split across several records that share the same `message.id`.
- `attachment`: auxiliary runtime context such as skill listings or task reminders.
- `system`, `last-prompt`, `ai-title`, `mode`, `permission-mode`, `file-history-snapshot`: UI/runtime bookkeeping, not separate LLM calls.

When interpreting Claude:

- Do not count each split `assistant` record as a separate LLM invocation. Deduplicate by assistant `message.id` when reading usage.
- Pair tool calls by matching assistant `tool_use.id` to later user `tool_result.tool_use_id`.
- Read token accounting from `message.usage`: `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, and `output_tokens`.
- Treat `cache_read_input_tokens` as prompt-cache hit prefix tokens.
- Treat `cache_creation_input_tokens + input_tokens` as new prompt-side work for that API call.
- Do not infer readable private thinking from `thinking` blocks. Public examples preserve that a thinking block existed, not its contents.

## Codex Raw Logs

Codex CLI raw records commonly include:

- `session_meta`: session metadata, base instructions, source, model provider, cwd, and git metadata.
- `turn_context`: per-turn runtime context such as cwd, sandbox, model, date, and collaboration mode.
- `response_item`: structured API-stream items such as messages, reasoning placeholders, function calls, and function-call outputs.
- `event_msg`: runtime/UI events such as `task_started`, `user_message`, `agent_message`, `token_count`, and `task_complete`.

When interpreting Codex:

- Expect duplicate-looking layers. A user-visible message can appear as both `response_item` and `event_msg.user_message`; an assistant reply can appear as both `response_item` and `event_msg.agent_message`.
- Pair tool calls by `response_item.payload.type == "function_call"` and later `function_call_output` records with the same call id.
- Treat `event_msg.token_count` as accounting for the preceding model round. `last_token_usage.input_tokens` already includes cached tokens.
- Treat `cached_input_tokens` as prompt-cache hit prefix tokens and `input_tokens - cached_input_tokens` as uncached prompt-side work.
- Treat `reasoning_output_tokens` as a subset of `output_tokens`, not an additional count.

## Finding Examples

Use the finder when the user needs compact raw examples from local histories:

```bash
python scripts/find_representative_session_segments.py --provider claude codex --export-dir artifacts/raw_windows
```

Useful options:

- `--provider claude` or `--provider codex` to focus one format.
- `--min-tool-calls`, `--min-tool-results`, `--min-usage`, and `--min-token-count` to require richer windows.
- `--max-file-size-mb 0` only when a full scan is acceptable.
- `--export-dir` to write raw JSONL, expanded text, per-record JSON, and a manifest.

## Reporting Guidance

When answering raw-trace questions:

- State whether the evidence is raw-provider behavior or normalized interpretation.
- Explain duplicate-looking records as runtime layers unless the data proves an extraction bug.
- Avoid exposing private raw contents in summaries. Prefer counts, schema fields, and short paraphrases unless the user explicitly asks to inspect a public example.
- If the user wants cross-provider metrics, switch to `$coding-trace-normalize` or `$coding-trace-analyze` after identifying the raw source.
