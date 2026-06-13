# human_input_wait

**Between finishing a response and the human's next message, how long does the agent sit idle — and
where does total idle time accumulate?**

## Experiment overview

**Human input wait** is the gap, within one session, from the model's *previous* output to the
*next* user message. It is computed by a stateful single-pass walk over agent steps in ingestion order
(`round_pk` == file order), keeping `last_model_output_by_session: {session_id -> datetime}`. For
each step, in order:

1. `start` = the **request-start user-message timestamp** for the step: among the step's
   `timing_events`, take the earliest model-output (`reasoning`/`text`/`tool_call`) timestamp as
   `first_output`, keep the `user_message` timestamps at-or-before `first_output`, and take the
   latest such candidate (None if there is no user message or no output, or none qualifies).
2. If `start` is not None and the step has a non-empty string `session_id`, and that session
   already has a recorded previous model-output timestamp `prev`, the wait is
   `(start − prev).total_seconds()`; when **strictly positive** it is appended to the `"all"` list
   and to that step's provider bucket.
3. The session's `last_model_output_by_session[session_id]` is then updated with this step's
   **last model-output timestamp** (the latest `reasoning`/`text`/`tool_call` timestamp), when
   present.

This is a **trace-level estimate**, not a serving-engine timer; it reflects only recorded events.
The wait spans the human think/read time between requests and excludes the model's own generation.

The experiment renders the wait distribution three ways, with the x-axis on a log duration scale and
the count/total panels capped at 1h (a 5-minute reference line marks a plausible cache-eviction
horizon):

- a single-axis **wait CDF** overlaying `all` and each provider;
- a per-provider **count CDF** — fraction of waits `≤ T`;
- a per-provider **total CDF** — share of *summed* idle time from waits `≤ T`.

Method and assumptions:

- **Exact, not sampled.** Every positive wait contributes one value to its provider's list (and to
  `all`); the CDFs, percentiles, and summed-time bins run over the full set. The old loader already
  kept every wait here — there was never a reservoir cap on this metric — so the migration is
  value-for-value identical.
- **File-order state.** The walk is over `round_pk` (ingestion ordinal == file order), reproducing
  the line-order tie-break the old single-pass JSONL loader relied on for its session state.
- **Provider grouping** mirrors the old loader's `str(provider) or "<unknown-provider>"` fallback,
  so a missing/empty provider falls into `<unknown-provider>`.
- **Engine-independent timestamps.** Timestamps are read from the DB as integer epoch-microseconds
  (`CAST(epoch_us(timestamp) AS BIGINT)`) and rebuilt to naive datetimes in Python, never fetched as
  a raw `TIMESTAMP` (native duckdb marshals that to a `datetime`, duckdb-wasm to a string). A
  difference between two same-timezone datetimes equals the naive-microsecond difference exactly, so
  the waits match the pre-DuckDB result bit-for-bit.

## Code structure

`plot.py` is a query→shape→plot pipeline over the shared trace DuckDB:

- `load_human_input_wait_seconds_by_provider(con)` — the only data-loading code. It pulls per-step
  `timing_events` (event_type + epoch-microsecond timestamp, in `round_pk` ingest order) and the
  per-step `(session_id, provider)` from `rounds`, then runs the stateful walk above, returning
  `{"all": [...], provider: [...]}`. The full per-provider lists are returned, no sampling.
- `_response_trigger_user_message_timestamp(events)` / `_last_model_output_timestamp(events)` —
  reproduce the pre-DuckDB `timing.response_trigger_user_message_timestamp` and
  `timing.last_model_output_timestamp` for one step's events.
- `_epoch_us_to_datetime(...)` — rebuilds a naive datetime from epoch-microseconds.
- `ordered_human_wait_items` / `human_wait_summary_row` / `plot_human_input_wait_cdf` /
  `write_human_input_wait_summary` — shape the overlay CDF and the summary CSV (unchanged from the
  pre-migration script).
- The count/total figures and their CSVs are produced by the shared `cdf.py` helpers
  (`plot_count_cdf_by_provider` / `plot_cumulative_duration_cdf_by_provider` and their `write_*`
  counterparts) — matplotlib/CSV behavior unchanged.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) and embeds
  the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/human_in_the_loop/human_input_wait/plot.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/human_in_the_loop/human_input_wait/plot.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/human_in_the_loop/human_input_wait/plot.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs (written to `-o`, default this folder)

- `human_input_wait_cdf.png` — single-axis wait CDF overlaying `all` and each provider, with
  `n`/`p50`/`p90` in the legend.
- `human_input_wait_count_cdf_by_provider.png` / `.csv` — per-provider count CDF over the wait
  threshold (waits `≤ T`), with per-bin and cumulative counts/shares.
- `human_input_wait_total_cdf_by_provider.png` / `.csv` — per-provider summed-idle-time CDF, with
  per-bin seconds/hours and cumulative time shares.
- `human_input_wait_summary.csv` — per-group (`all` + providers) `count`, `mean`, `p50/p90/p95/p99`,
  and `max` in seconds.

Each PNG embeds this README, the CSVs above, and the plotting code (`plot.py` + shared
`artifacts/utils/` modules) as compressed text chunks. Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### human_input_wait_cdf.png

A single log-x CDF overlaying the `all` curve (in the neutral text color) and one curve per provider,
each labeled with its count and `p50`/`p90` wait. The x-axis is the wait from the previous model
output to the next user message, with landmark ticks from 1s out to a week. Read the height a curve
reaches at, say, the 5-minute or 1-hour tick to gauge what fraction of requests the human answers
quickly versus walking away: an early, steep rise means tight back-and-forth, while a long right tail
shows sessions resumed minutes, hours, or days later. Provider curves can be compared directly for
differences in interaction cadence.

### human_input_wait_count_cdf_by_provider.png

A per-provider cumulative count of waits against the wait threshold on a log x-axis, capped at 1h
with a 5-minute reference line. Read the x-position where each curve reaches a given height to
compare how promptly humans reply: a curve that rises early means most idle gaps are short. The
in-figure table reports the per-provider percentiles and mean, and the 5-minute landmark marks a
plausible prompt-cache eviction horizon — waits to the right of it are likely cold on the next request.

### human_input_wait_total_cdf_by_provider.png

The same per-session waits, but each wait now contributes its *duration* rather than a unit count, so
the curve traces the cumulative summed idle time up to threshold `T` (capped at 1h). This shows
**where total idle time accumulates**: because long waits carry disproportionate time, this curve
saturates much later than the count CDF — a small fraction of very long gaps can dominate total idle
time. Compare the gap between this figure and the count CDF to see how concentrated each provider's
idle time is in its long-wait tail, and how much of it sits past the 5-minute cache-eviction line.
