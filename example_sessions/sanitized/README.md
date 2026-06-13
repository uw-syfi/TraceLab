# Sanitized Round Trace Example

This directory shows the public-release shape produced by
`scripts/sanitize_round_trace.py` from `example_sessions/derived/round_trace.jsonl`.

- `round_trace.jsonl`: sanitized machine-readable JSONL.
- `round_trace.expanded.json`: the same sanitized rows pretty-printed for human inspection.

The sanitizer preserves row shape and cross-record id relationships while
replacing ids, removing local context fields such as `home`, `user`, `cwd`,
`workdir`, and path-like keys, and dropping `tools[].input`.

## What Remains

Sanitized rows intentionally retain the analytic structure needed for token,
cache, latency, and tool-use analysis:

- Provider and model labels: `provider`, `model`, and `store`.
- Pseudonymized relationship ids: `project`, `session_id`, `round_id`,
  `turn_id`, `trace_key`, and `tool_call_id`.
- Round ordering: `round_index`.
- Token accounting: `input_tokens_total`, `prefix_tokens`,
  `newly_append_tokens`, `output_tokens`, `reasoning_output_tokens`, and the
  Claude-specific `claude_uncached_input_tokens`,
  `claude_cache_creation_input_tokens`, and `claude_cache_read_input_tokens`.
- Current-input size summaries: `current_input_event_count`,
  `current_user_message_count`, `current_tool_result_count`,
  `current_user_message_chars`, `current_tool_result_chars`,
  `current_input_chars`, and `first_input_event_type`.
- Event timing shape: `timing_events[].event_type`, `timestamp`, `source`,
  `tool_name`, `tool_index`, pseudonymized `tool_call_id`, size fields such as
  `content_chars` and `result_chars`, and `is_error`.
- Tool timing metadata: `tool_name`, `tool_index`, pseudonymized
  `tool_call_id`, `emitted_at`, `result_at`, `input_chars`, `result_chars`,
  `tool_wall_latency_ms`, `tool_internal_latency_ms`, and `is_error`.

Sanitized rows remove raw content and local context:

- Raw user message text.
- Raw assistant text.
- Raw tool inputs, commands, and arguments.
- Raw tool output content.
- Local paths and path-like fields, including `cwd`, `workdir`,
  `session_file`, `file_path`, and repository/path fields.
- User and home identifiers such as `home`, `user`, `username`, and
  `user_name`.
- Original session, round, turn, project, and tool-call ids.

Some retained fields can still be sensitive depending on the release context:

- Exact timestamps can reveal activity timing. For a stricter public release,
  consider replacing them with per-session relative offsets or coarse buckets.
- Model names reveal which systems were used.
- Tool names reveal workflow shape, even without tool payloads.
- Token and character counts reveal content-size patterns.
- Stable pseudonyms preserve cross-row relationships inside the released file,
  which is useful for analysis but still allows session-level grouping.
- `store` is redundant with `provider` for many analyses and can be removed for
  a tighter release schema.

Reproduce this example with:

```bash
python scripts/sanitize_round_trace.py \
  example_sessions/derived/round_trace.jsonl \
  -o example_sessions/sanitized/round_trace.jsonl \
  --seed derived-example-v2
```
