# Derived Round Trace Example

This directory shows the normalized trace format produced by the extraction scripts.

- `round_trace.jsonl`: machine-readable JSONL. Each line is one LLM invocation.
- `round_trace.expanded.json`: the same rows pretty-printed for human inspection.

The sanitized companion example is in `example_sessions/sanitized/`.

The rows are derived from the public examples in:

- `example_sessions/claude/trace.json`
- `example_sessions/codex/trace.json`

Each row includes an ordered `timing_events[]` list and nests the tool calls produced by that
LLM invocation under `tools[]`.
Token fields are normalized into:

```text
input_tokens_total = prefix_tokens + newly_append_tokens
```

For Claude, `prefix_tokens` comes from `cache_read_input_tokens`.
For Codex, `prefix_tokens` comes from `cached_input_tokens`.
Claude rows also expose the provider usage split:

```text
claude_uncached_input_tokens       = usage.input_tokens
claude_cache_creation_input_tokens = usage.cache_creation_input_tokens
claude_cache_read_input_tokens     = usage.cache_read_input_tokens
```

Rows include current-input summaries derived from their input-side timing events:

```text
current_user_message_count
current_tool_result_count
current_input_event_count
current_user_message_chars
current_tool_result_chars
current_input_chars
first_input_event_type
```

These fields let downstream analysis distinguish small current inputs from large
user/tool payloads without reparsing raw provider logs.

`timing_events[]` preserves the trace-observed round timeline. Common event types are
`user_message`, `tool_result`, `reasoning`, `text`, `tool_call`, and Codex `usage_report`.
For "input ready -> next tool input" latency, compare the latest input event
(`user_message` or `tool_result`) with the first following `tool_call`.

Tool latency is split into:

- `tool_wall_latency_ms`: trace-observed wall latency, computed from `result_at - emitted_at`.
- `tool_internal_latency_ms`: tool/runner-reported duration when the raw trace exposes one; otherwise `null`.

Before publishing a derived trace from private data, run `scripts/sanitize_round_trace.py`.
It preserves row shape and cross-record id relationships while replacing ids, removing local
context fields such as `home`, `user`, `cwd`, `workdir`, and path-like keys, and dropping
`tools[].input` entirely.

The sanitized example was produced with:

```bash
python scripts/sanitize_round_trace.py \
  example_sessions/derived/round_trace.jsonl \
  -o example_sessions/sanitized/round_trace.jsonl \
  --seed derived-example-v2
```
