# total_input_growth

**Within a single coding session, how does the total input length (`prefix + append`) move from one
agent step to the next — and when it shrinks, is that accounting jitter or a real context
compaction?**

## Experiment overview

Each row in the trace is one agent step. Walking the steps of a session in order, this
experiment records the per-step change in **total input length** (`prefix_tokens +
newly_append_tokens`) — a positive delta is the window growing, a negative delta is it shrinking —
and classifies every drop into one of three buckets.

Method and assumptions:

- **Total input** for a step is `prefix_tokens + newly_append_tokens` (cached prefix plus freshly
  appended input). The per-step metric is the signed delta of that quantity from the **previous
  step seen in the same session**.
- **Pairing.** A growth event is emitted only when the current step's **first timing event** is a
  visible input event — a `user_message` or a `tool_result` (the step's *trigger*) — and the session
  has been seen before. The `previous` step is whatever step was last observed for that session in
  trace order, regardless of its trigger. Steps are ordered within a session by ingestion order
  (`round_pk` = file order), the same line-order sequencing the pre-DuckDB scan used.
- **Reduction buckets** (thresholds from `artifacts/utils/growth.py`, overridable on the CLI):
  - **micro-reduction** — drop `≤ 1024` tokens (accounting jitter);
  - **major-reduction** — drop `≥ 50000` tokens (a real context compaction);
  - **ordinary reduction** — anything between the two.
- **Triggers reported.** Summary rows are cut three ways — `all`, `user`, and `tool_result` — by the
  current step's trigger, and per scope (`merged` plus each provider).
- Shares the growth helpers (`build_growth_stats`, `reduction_bucket`, the CSV writers) with the
  `trace_facts` overview summaries.

## Code structure

This is a **hybrid** experiment: the trace DuckDB does the single-pass ingest, and Python keeps the
per-session sequencing and growth bucketing.

- `iter_growth_events_from_db(con)` — the only data-loading code. Two queries in ingestion order
  (step scalars `ORDER BY round_pk`, and each step's *first* timing event at `event_index = 1` for
  the trigger type and timestamp), walked in Python with a `last_by_session` map to emit one growth
  event per qualifying step — exactly reproducing the old line-by-line JSONL scan.
- `_epoch_us_to_iso(...)` — timestamps are pulled as integer epoch-microseconds (native/wasm
  identical) and rebuilt to the canonical `…Z` ISO string, so the timestamp columns match the
  pre-DuckDB output bit-for-bit.
- `build_growth_stats(...)` / `reduction_bucket(...)` / `write_summary_csv(...)` /
  `write_events_csv(...)` — unchanged shared helpers in `artifacts/utils/growth.py`.
- `write_filtered_events_csv(...)` — the stable-sorted reduction / micro-reduction drilldowns.

The data layer lives in `artifacts/utils/trace_db.py` (see `artifacts/utils/DB_SCHEMA.md`).

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/session/total_input_growth/analyze.py

# a specific trace
uv run python artifacts/session/total_input_growth/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/session/total_input_growth/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

Knobs: `--micro-reduction-max-tokens` / `--major-reduction-min-tokens` retune the reduction buckets,
`--no-drilldowns` writes only the summary, `--limit-events` caps each drilldown after a stable sort,
and `--summary-csv` / `--events-csv` / `--reductions-csv` / `--micro-csv` override individual paths.

## Outputs (written to `-o`, default this folder)

- `total_input_growth_summary.csv` — growth/reduction bucket counts and delta stats per
  `(scope, trigger)`.
- `total_input_growth_events.csv` — every same-session growth event, in trace order.
- `total_input_reductions.csv` — only the negative-delta events (all three reduction buckets).
- `total_input_micro_reductions.csv` — only the micro-reduction events.

CSV only — no figures.

## SyFI result analysis

### total_input_growth_summary.csv

The headline table. Each row is a `(scope, trigger)` cut. Read the **positive / zero / negative**
split first: in the sample the window grows on ~99.6% of steps, so context accumulation is the
overwhelming norm and reductions are rare. Within the negatives, the **micro / ordinary / major**
columns separate harmless accounting jitter from genuine compactions — major-reductions are a small
minority but carry the largest `max_reduction`. The `avg_raw_delta` / `p10` / `median` / `p90`
columns describe the per-step growth distribution, and `total_context_increase` is the summed
positive growth. Comparing the `user` vs `tool_result` trigger rows shows whether reductions cluster
around user-initiated steps or tool-triggered steps.

### total_input_growth_events.csv

The full event-level drilldown — one row per same-session step pair, with the previous/current
step index (`round_index`), total/prefix/append tokens and their deltas, trigger, model, timestamp, and trace key.
This is the raw material behind the summary; use it to trace any individual delta back to its two
steps.

### total_input_reductions.csv

The negative-delta subset, stably sorted by `(provider, trigger, session_id, current_line_number)`.
This is where the real compactions live — sort or filter by `raw_delta_tokens` to find the largest
context collapses and inspect the prefix-vs-append split that produced them.

### total_input_micro_reductions.csv

The micro-reduction subset (drops `≤ 1024` tokens). These are almost always token-accounting jitter
rather than deliberate compaction; the file exists to confirm that the small negatives are noise and
not something the bucketing is mislabeling.
