# session_internal_counts

**How much work does one coding session, and one request, contain?**

Computes the count distributions behind `tab:session_internal_counts`
(`src/04_SessionContext.tex`): requests, user-/tool-initiated steps, and tool calls per
session; tool-initiated steps and tool calls per request; and tool calls per step — each as
avg / p25 / p50 / p90 / p99.

## Definitions (reused so the numbers reconcile with the rest of the paper)

- **Session** — a non-empty `session_id` (the grouping `session_token_steps` and the overview
  use; 4,265 sessions in the public trace).
- **Request** — one user turn: a response-triggering `user_message` to the next one in the same
  session. This replays the exact turn state machine from
  `human_in_the_loop/user_turn_decomposition` (identical boundaries to `user_turn_response_time`);
  turns with no response-end event or non-positive duration are dropped.
- **Step** — one LLM round. `user-initiated` vs `tool-initiated` splits on the round's
  `first_input_event_type` (`user_message` / `tool_result`), the loader's `is_user_input` trigger.
- **Tool calls** — `tool_calls` rows; per session = all of a session's calls, per request = the
  calls inside one turn.

`Requests` (turns) can slightly exceed `User-initiated steps` because a turn may be opened by a
`user_message` that arrives inside a round whose *first* input event was a `tool_result`.

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/session/session_internal_counts/analyze.py

# the pinned public trace
uv run python artifacts/session/session_internal_counts/analyze.py -i trace/syfi_coding_trace.jsonl

# a prebuilt DB, into a chosen dir
uv run python artifacts/session/session_internal_counts/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs

- `session_internal_counts.tex` — the merged three-line (booktabs) table, ready to `\input` or
  paste into `src/04_SessionContext.tex`.
- stdout — the full merged + per-provider (Claude / Codex) breakdown.

No figures.
