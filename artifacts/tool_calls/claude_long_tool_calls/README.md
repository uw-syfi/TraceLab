# claude_long_tool_calls

**How common are Claude tool calls whose effective latency exceeds one hour, and what are
they?** A data-quality / outlier audit on the long tail of tool latency.

## Experiment overview

Each round carries a `tools[]` list of tool calls with measured latencies. This experiment flags
the Claude calls whose **effective latency** is above a threshold (default 1h) and dumps them for
inspection, then reports rollups (by source, tool, model, error status) and duplicate-key /
duplicate-signature accounting so the long tail can be understood rather than silently distorting
latency aggregates.

Method and assumptions:

- **Effective tool latency** = `tool_internal_latency_ms` if present, else `tool_wall_latency_ms`
  (the shared `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL` precedence; the legacy `latency_ms` field is
  not in the normalized data). Only **strictly positive** effective latencies are eligible; a call
  with no effective latency is "missing", a `≤ 0` value is "nonpositive".
- **Long-call definition / threshold.** A row is flagged when its effective latency is **strictly
  greater than** `--threshold-ms` (default `3,600,000` ms = 1h). Selection and all rollups are over
  Claude (`provider = 'claude'`) tool calls only.
- **Ranking.** The detail CSV is ordered by effective latency **descending**, with a total,
  deterministic tie-break `(round_pk, tool_index)` (file order) so the row order is stable across DB
  builds. The summary Counters are built in **file order**, so their `most_common()` tie order (and
  the per-tool latency ranking's stable-sort ties) reproduce the legacy file-scan order exactly.
- **Human/approval-like tools.** `AskUserQuestion`, `ExitPlanMode`, `PushNotification` block on a
  human/approval flow; their >1h values are wait time, called out separately in the summary.
- **Exact, not sampled.** Every Claude tool call is scanned in SQL; nothing is sampled.

Schema note (`input_preview`): the per-row `input_preview` needs the raw `tool.input` dict, which
`trace_db.materialize()` deliberately drops (a schema-drift trap), so the DB cannot supply it. The
experiment backfills it for the (small) flagged set from the source trace JSONL keyed by
`(round_pk = line, tool_index)`, trusting a line only when its `tool_call_id`/`input_chars` match
the DB row (otherwise the preview is left blank and a warning is printed). With `-i`, that trace is
the one given; with `--db`, it is `--input` (default the merged trace, of which the sample is a
prefix, so ordinals line up). See **Outputs** and `artifacts/utils/DB_SCHEMA.md`.

## Code structure

`analyze.py` is a query→shape→write pipeline over the shared trace DuckDB:

- `fetch_over_1h(con, threshold_ms)` — the flagged rows: Claude calls with positive effective
  latency `> threshold_ms`, ordered latency-desc with the `(round_pk, tool_index)` tie-break.
  Timestamps come back as `epoch_us` BIGINTs (engine-portable) and `_iso_ms()` rebuilds the original
  `…Z` millisecond strings; `computed_wall_latency_ms`/`tool_result_delay_hours` derive from them.
- `load_input_previews(trace_path, expected)` — the `input_preview` backfill (schema gap above),
  reading raw `tool.input` from the source JSONL with the same `preview()` the legacy loader used.
- `fetch_scan_counts` / `fetch_source_counts` / `fetch_wall_over_threshold_internal_won` /
  `fetch_timestamp_mismatches` / `fetch_all_dup_keys` / `fetch_all_dup_trace_tool_keys` — the
  all-Claude SQL rollups behind the summary block (`fetch_source_counts` orders by first appearance
  so `most_common()` ties follow file order).
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`), builds
  the per-flag Counters/signatures in file order, then writes the detail CSV and the markdown.

The data layer (parsing, surrogate `round_pk`, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/tool_calls/claude_long_tool_calls/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/tool_calls/claude_long_tool_calls/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/tool_calls/claude_long_tool_calls/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

Useful flag: `--threshold-ms` (long-call cutoff in ms, default `3,600,000` = 1h). For exact
`input_preview` under `--db`, pass `-i` pointing at the trace the DB was built from.

## Outputs

Written to `-o` (default this folder):

- `claude_gt1h_tool_calls.csv` — one row per flagged call, latency-desc: identity
  (`line_no`/`trace_key`/`session_id`/`round_id`/`round_index`/`model`/`tool_index`/`tool_name`/
  `tool_call_id`), `source`, effective / wall / internal / computed-wall latencies (ms and hours),
  raw `emitted_at`/`result_at`, `tool_result_delay_hours`, `is_error`, `input_chars`,
  `result_chars`, and `input_preview`.
- `result_analysis.md` — summary counts (scan tallies, >1h totals, duplicate-key and
  duplicate-signature accounting, timestamp-mismatch and wall-vs-internal checks) plus per-source /
  per-tool / per-model / error / missing-internal breakdowns, the duplicate signatures, the longest
  calls, and an interpretation.

CSV/Markdown only (no figures), so there is no self-contained PNG sidecar here.

## SyFI result analysis

### claude_gt1h_tool_calls.csv

The flagged calls are a thin long tail (tens of rows out of ~49k Claude calls in the sample) and are
overwhelmingly **wall-fallback** with no internal timing — i.e. trace-observed gaps between the
assistant `tool_use` and the later `tool_result`, not runner-measured execution time. Reading the
rows: `Bash`/`Agent`/`Write`/`Edit` dominate by summed time, but `source=wall_fallback` plus a null
`internal_latency_ms` means each gap could be active execution, a suspended/backgrounded command, or
a session-resume delay — the audit cannot distinguish them. `effective_latency_hours` and
`computed_wall_latency_ms` (rebuilt from the timestamps) let you sanity-check each gap against
`emitted_at`/`result_at`.

### result_analysis.md

The summary quantifies how much aggregate latency the long tail carries and how much is artifactual.
Key reads: the **signature-dedup** figures expose calls that appear under multiple sessions with an
identical `(round, tool, timestamps, latency)` signature (cross-session duplication inflates naive
totals); **human/approval-like** rows isolate wait-on-user time (`AskUserQuestion`, `ExitPlanMode`,
`PushNotification`) from genuine tool work; and **wall >1h but not effective >1h because internal
timing won** counts cases the effective-latency precedence correctly rescued from a misleading wall
gap. The duplicate `(session_id, round_id, tool_call_id)` and `(trace_key, tool_index, tool_call_id)`
key counts are a data-integrity check — nonzero values flag re-ingested or fan-out-duplicated rows.
