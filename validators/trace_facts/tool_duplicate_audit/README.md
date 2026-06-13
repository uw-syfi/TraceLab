# tool_duplicate_audit

**Question.** Are there duplicate tool-call rows in the merged normalized trace, and how
many? (A trace-integrity / deduplication audit — duplicates would double-count tool
calls and latency in every downstream tool study.)

## Input

`trace/llm_round_trace.merged.all_users.jsonl` (override with `-i`).

## Method / key assumptions

- Groups tool-call entries by an identity key (e.g. session + tool_call_id + emit
  time + input/result sizes) and flags groups with more than one member as duplicates.
- Duplicates typically arise from subagent replay or merge artifacts when combining
  multiple users' traces (see the Codex caveat in `../../../docs/prompt_cache_accounting.md`).
- Reports the top duplicated groups for inspection.

## How to run

```bash
uv run python validators/trace_facts/tool_duplicate_audit/analyze.py
uv run python validators/trace_facts/tool_duplicate_audit/analyze.py -i trace/sample.jsonl
```

## Outputs (written here)

- `duplicate_tool_groups.csv` — the flagged duplicate groups.
- `result_analysis.md` — summary counts.

## Notes

CSV/Markdown only (no figures).
