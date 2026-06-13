# user_turn_response_audit

**Question.** Which user-message-triggered rows do *not* become user-turn response-time
samples, and why? (A coverage/validity audit for the `user_turn_response_time` metric.)

## Input

`trace/llm_round_trace.merged.all_users.jsonl` (edit `INPUT` in `analyze.py` to
override).

## Method / key assumptions

- Walks every row whose trigger is a `user_message` and classifies why it does or does
  not yield a response-time sample (e.g. no following response-end event, session
  boundary, stale/resumed trigger).
- Uses the same trigger/response-end definitions as `user_turn_response_time/` so the
  audit explains exactly that metric's denominator.

## How to run

```bash
uv run python validators/human_in_the_loop/user_turn_response_audit/analyze.py
```

(For a fast check, temporarily point `INPUT` at `trace/sample.jsonl`.)

## Outputs (written here)

- `result_analysis.md` — counts and category breakdown of included/excluded rows.

## Notes

Markdown only (no figures).
