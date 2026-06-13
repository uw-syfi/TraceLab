# generation_time_cdf

**How long does a single agent step's *observable* model generation take, and how is total generation
time distributed across fast vs. slow steps?**

## Experiment overview

The **observable generation time** of an agent step is the span from its **latest input event**
(`user_message` or `tool_result`) to its **last model-output event** (`reasoning`, `text`, or
`tool_call`), taken from the step's ordered `timing_events[]`. Concretely, per step: take the
earliest model-output timestamp as `first_output`; of the input timestamps, keep only those
at-or-before `first_output` (the inputs that could have triggered this output); the span is
`(latest output - latest such input)`, kept only when strictly positive. Steps with no input or no
output events, or with a non-positive span, contribute nothing.

This is a **trace-level estimate**, not a serving-engine timer. It excludes the preceding human wait
and any post-response usage-accounting events, and it can only reflect events the trace actually
recorded.

The experiment renders two complementary CDFs over a per-step generation-time threshold `T`, paneled
by provider on a fine log-spaced duration axis:

- **count CDF** — fraction of agent steps with generation time `≤ T`.
- **total CDF** — fraction of *summed* generation time contributed by steps with generation time
  `≤ T`, i.e. where the wall time actually goes.

Method and assumptions:

- **Exact, not sampled.** Every agent step with a positive observable span contributes one value to its
  provider's list; the CDFs, percentiles, and summed-time bins are computed over the full set. (The
  old loader already kept every value here — there was never a reservoir cap on this metric — so the
  migration is value-for-value identical.)
- **Provider grouping** mirrors the old loader's `str(provider) or "<unknown-provider>"` fallback, so
  a missing/empty provider falls into `<unknown-provider>`.
- **Engine-independent timestamps.** Timestamps are read from the DB as integer epoch-microseconds
  (`CAST(epoch_us(timestamp) AS BIGINT)`) and rebuilt to naive datetimes in Python, never fetched as
  a raw `TIMESTAMP` (native duckdb marshals that to a `datetime`, duckdb-wasm to a string). A span
  between two same-timezone datetimes equals the naive-microsecond span exactly, so durations match
  the pre-DuckDB result bit-for-bit.

## Code structure

`plot.py` is a query→shape→plot pipeline over the shared trace DuckDB:

- `load_generation_seconds_by_provider(con)` — the only data-loading code. It pulls per-step input
  and model-output timestamps from `timing_events` (as epoch-microsecond ints, in `round_pk` ingest
  order) and the per-step `provider` from `rounds`, then computes the observable span per step and
  appends it to that provider's list. The full per-provider lists are returned, no sampling.
- `_input_to_last_output_span_seconds(inputs, outputs)` — reproduces the pre-DuckDB
  `timing.input_to_last_output_span_seconds` for one step's events (first-output gate, candidate
  inputs, positive-span filter).
- `_epoch_us_to_datetime(...)` — rebuilds a naive datetime from epoch-microseconds.
- The two figures and two CSVs are produced by the shared `cdf.py` helpers
  (`plot_count_cdf_by_provider` / `plot_cumulative_duration_cdf_by_provider` and their
  `write_*` counterparts) — matplotlib/CSV behavior unchanged from the pre-migration script.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) and embeds
  the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/llm_generation/generation_time_cdf/plot.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/llm_generation/generation_time_cdf/plot.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/llm_generation/generation_time_cdf/plot.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs (written to `-o`, default this folder)

- `llm_generation_time_count_cdf_by_provider.png` / `.csv` — per-provider count CDF over the
  generation-time threshold (steps `≤ T`), with per-bin and cumulative counts/shares.
- `llm_generation_time_total_cdf_by_provider.png` / `.csv` — per-provider summed-time CDF, with
  per-bin seconds/hours and cumulative time shares.

Each PNG embeds this README, the CSVs above, and the plotting code (`plot.py` + shared
`artifacts/utils/` modules) as compressed text chunks. Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### llm_generation_time_count_cdf_by_provider.png

A per-provider cumulative count of agent steps against the observable generation-time threshold on a log
x-axis. Read the x-position where each curve reaches a given height to compare how quickly steps
finish: a curve that rises early means most steps generate fast, while a long flat tail to the right
marks the slow steps. The in-figure table reports `p25/p50/p90/p99` and the mean per provider, and
the dashed landmark lines anchor familiar durations (seconds, a minute, etc.) for quick reads.

### llm_generation_time_total_cdf_by_provider.png

The same per-step spans, but each step now contributes its *duration* rather than a unit count, so
the curve traces the cumulative summed generation time (in hours) up to threshold `T`. This shows
**where the wall time actually goes**: because slow steps carry disproportionate time, this curve
saturates much later than the count CDF — a small fraction of long steps can dominate total
generation time. Compare the gap between the two figures to see how concentrated each provider's time
spend is in its tail.
