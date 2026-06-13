# csv_export

**Convert the normalized round trace into the canonical multi-round CSV trace format used by
downstream simulators.** (A utility transform, not a study.)

## Experiment overview

Each round in the trace is one LLM invocation. This utility groups rounds into sessions and emits a
single CSV with the canonical multi-round columns:

```
id,input_len,output_len,arrival_time,round_idx,tool_wait_after_ms,prefix_len
```

Method and key assumptions:

- **Column mapping** — `input_len = max(newly_append_tokens, 1)`, `prefix_len = max(prefix_tokens, 0)`,
  `output_len = max(output_tokens, 1)`. `round_idx` is a contiguous `0..N` within each emitted
  session (rounds sorted by `round_index`, ties broken by file order).
- **Sessions** — rounds are grouped by `session_id` in first-appearance (file) order. Rows with no
  string `session_id` are skipped; an optional `--provider` keeps only one provider's rounds.
- **`arrival_time`** is **synthetic** per session, in milliseconds: a seeded Poisson process by
  default (`--arrival-pattern poisson`, `--arrival-rate` sessions/s) or evenly spaced
  (`--arrival-pattern constant`). Sessions are shuffled with a seeded RNG before arrivals are
  assigned (`--session-order shuffle`, the default) or kept in input order (`stable`).
- **`tool_wait_after_ms`** is the summed tool latency observed *after* a round, `0` on a session's
  final round. By default it uses trace-observed **wall** latency (`tool_wall_latency_ms`);
  `--tool-latency-source internal` uses `tool_internal_latency_ms`. Null latencies are skipped;
  negative ones are counted as invalid (both reported on stderr).
- **Determinism** — everything keys off file order, so the output is reproducible for a given
  `--seed` (default `0`). `--max-sessions` caps the number of sessions emitted (after shuffling).

## Code structure

This is a **DB-backed** utility: the shared trace DuckDB does the single-pass ingest, and Python
keeps the session grouping, seeded arrival synthesis, and tool-wait summation unchanged.

- `load_tool_wait_by_round(con, latency_source)` — one query over `tool_calls`
  (`ORDER BY round_pk, tool_index`) folded into a per-round aggregate (`wait_ms` plus
  `seen`/`used`/`missing`/`invalid` counts), so the final-round skip and per-tool stats are applied
  in Python exactly as the pre-DuckDB per-tool loop did.
- `load_sessions(con, provider, stats)` — the only round-loading query. Pulls round scalars
  `ORDER BY ingest_seq` (== file order), so the first-appearance **session order** and the
  per-session **row order** match the old JSONL line scan byte-for-byte. `round_pk` plays the role
  of the old line number (the trace has no blank lines, and it is only ever a sort tie-break, where
  relative order is identical to file order).
- `build_session_rounds(...)` — sorts each session's rounds by `(round_index, round_pk)` and applies
  the column mapping and per-round tool-wait lookup.
- `ordered_items(...)` / `poisson_arrivals(...)` / `constant_arrivals(...)` — unchanged seeded
  session shuffle (`--seed`) and synthetic arrival generation (`--seed + 1` for Poisson).
- `generate_trace(con, output_path, ...)` — assembles `trace_rows` (with compaction detection) and
  writes the CSV via `csv.DictWriter` over the fixed `TRACE_FIELDS` order.

I/O follows the shared surface: `-i/--input` a normalized JSONL trace (materialized to a temp
DuckDB cache on first use via `trace_db`) or a prebuilt `--db`; `-o/--output` is the CSV file.
The data layer lives in `artifacts/utils/trace_db.py` (see `artifacts/utils/DB_SCHEMA.md`).

## Running it

```bash
# materialize a trace to a temp DuckDB and write the CSV
uv run python artifacts/trace_facts/csv_export/convert.py \
  -i trace/llm_round_trace.public.jsonl \
  -o artifacts/trace_facts/csv_export/coding_trace.csv

# a prebuilt DB (skips materialize)
uv run python artifacts/trace_facts/csv_export/convert.py \
  --db /tmp/trace.duckdb \
  -o artifacts/trace_facts/csv_export/coding_trace.csv

# claude-only, evenly spaced arrivals, a fixed seed
uv run python artifacts/trace_facts/csv_export/convert.py \
  -i trace/sample.jsonl -o /tmp/coding_trace.csv \
  --provider claude --arrival-pattern constant --arrival-rate 2 --seed 7
```

`-o` is required (this is a transform, not a plot). `run_all.py` drives this via its `io` style:
`-i <jsonl> -o coding_trace.csv`.

## Outputs

Writes a single CSV to the path given by `-o` (conventionally `coding_trace.csv` inside this folder).
The file has the header `id,input_len,output_len,arrival_time,round_idx,tool_wait_after_ms,prefix_len`
followed by one row per emitted round, ordered by session then `round_idx`. `arrival_time` and
`tool_wait_after_ms` are formatted with six decimal places; the remaining columns are integers. A
short run summary (input rows, sessions seen/emitted, rounds emitted, compaction sessions, total tool
wait, and tool-latency used/missing/invalid counts) is printed to stderr. No figures are produced.

## SyFI result analysis

The CSV is the bridge from the observed coding trace to a simulator workload: it preserves the real
per-round token shapes (`input_len`/`prefix_len`/`output_len`) and the real inter-round tool waits,
while substituting a synthetic, reproducible session arrival process. Read the columns as the
serving inputs they feed: `prefix_len` is the cached/reused context, `input_len` the freshly appended
prompt, `output_len` the decode length, and `tool_wait_after_ms` the idle gap before the next round
in the same session. Because session shuffling and arrival times are seeded, two runs with the same
`--seed` are byte-identical, and the `--provider` / `--arrival-*` knobs let a study isolate one
provider or stress a chosen request rate without changing the underlying token/tool structure.
