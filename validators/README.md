# validators

Validation and audit checks live here, separate from plotting and analysis artifacts.

Each validator is a folder `validators/<category>/<validator>/` containing:

- an `analyze.py` script;
- a `README.md` describing the validation question, inputs, method, and outputs;
- generated reports written next to the script when it runs.

Generated outputs are ignored by git; validator code and `README.md` files are tracked.

## Validators

- `human_in_the_loop/e2e_formula_check` — checks whether user-turn response time can be
  approximated by an average generation/tool-cost formula.
- `human_in_the_loop/user_turn_gap_audit` — audits unclassified elapsed time inside
  user-turn response windows.
- `human_in_the_loop/user_turn_response_audit` — audits which user-message-triggered
  rows do or do not become response-time samples.
- `trace_facts/tool_duplicate_audit` — checks for duplicate tool-call rows that would
  overcount downstream tool studies.

## Running

Use the same normalized JSONL input that was used for the artifact suite. For public
reporting, prefer the sanitized trace.

```bash
uv run python validators/run_all.py
uv run python validators/run_all.py --list
uv run python validators/run_all.py --only human_in_the_loop
uv run python validators/run_all.py --only trace_facts/tool_duplicate_audit
uv run python validators/run_all.py --input trace/llm_round_trace.public.jsonl
```

The dispatcher runs validators four at a time by default, captures console output under
`--log-dir`, and supports `--dry-run` and `--stop-on-fail` for debugging.
