# timing_fit

**Question.** How well can a simple quadratic model predict round timing from token
features, and what are the fitted coefficients per provider/model/segment?

## Input

This folder holds **two** scripts:

- `collect_timing_fit_trace.py` — derives `timing_fit_trace.csv` (the long-form timing
  segments, one row per timing segment) from the shared trace DuckDB. It reads the
  `rounds` / `timing_events` / `tool_calls` tables (via `artifacts/utils/trace_db.py`):
  pass a prebuilt DB with `--db`, or a normalized JSONL trace with `-i/--input` (it is
  materialized to a temp DuckDB cache), and write the CSV with `-o/--output` (a file
  path). This is the ROOT of the timing sub-chain — its CSV is consumed byte-for-byte by
  the downstream timing experiments, so the per-segment values match the legacy JSONL
  collector exactly.
- `fit_timing_trace.py` — fits the timing models over `timing_fit_trace.csv` in this
  folder (override the CSV with `-i`) and writes the coefficient/metric/summary outputs.

## Method / key assumptions

- Fits low-order (quadratic) timing models over token features per group. See
  `../timing_feature_ambiguity/` for how much variance is *irreducible* — that bounds
  how good any such fit can get.
- Segment definitions follow `collect_timing_fit_trace.py`: a Claude/Codex segment is
  latest-input-event → final `tool_call` emission; Codex additionally splits at the
  reasoning marker (it has exact reasoning-token accounting). Per-round timing events are
  read from the DB in list order (`event_index`); timestamps are pulled as `epoch_us`
  integers and rebuilt to datetimes (native/wasm-identical) rather than as raw
  `TIMESTAMP`.

## How to run

Recommended dispatcher path:

```bash
uv run python artifacts/run_all.py \
  --only llm_generation/timing_fit \
  --input trace/llm_round_trace.public.jsonl
```

The dispatcher first runs `llm_generation/timing_fit/build_trace`, which derives the
local CSV from the selected JSONL trace.

Manual path:

```bash
# Derive the CSV from a prebuilt DuckDB (or pass -i <trace.jsonl> instead of --db):
uv run python artifacts/llm_generation/timing_fit/collect_timing_fit_trace.py \
  --db /tmp/trace.duckdb -o artifacts/llm_generation/timing_fit/timing_fit_trace.csv
# ...then fit:
uv run python artifacts/llm_generation/timing_fit/fit_timing_trace.py
```

Use `--timing-input` with `artifacts/run_all.py` only when intentionally consuming an
existing external timing CSV instead of deriving one from `--input`.

## Outputs (written here)

- `timing_fit_trace.csv` — derived timing-segment input consumed by timing experiments.
- `timing_fit_coefficients.csv` — fitted model coefficients per group.
- `timing_fit_metrics.csv` — per-group fit quality metrics.
- `timing_fit_summary.json` — run summary.

## Notes

CSV/JSON only (no figures), so no self-contained PNGs here.
