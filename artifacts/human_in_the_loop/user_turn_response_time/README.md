# user_turn_response_time

**For one request, how long until the agent fully finishes responding (including
intermediate tool waits), end to end?**

## Experiment overview

**Request response time** is the span, within one session, from the user message that starts a
request to the agent's final response before the *next* user-started request. It is
computed by a stateful single-pass walk over agent steps in ingestion order (`round_pk` == file order),
keeping `current_user_turn_by_session: {session_id -> {provider, start_at, last_output_at}}`.

A request is bounded by a small state machine. `close_user_turn(session_id)` pops the session's open
request; if it has both a `start_at` and a `last_output_at` and the elapsed
`dur = (last_output_at − start_at).total_seconds()` is **strictly positive**, that duration is
appended to the `"all"` list and to the request's provider bucket. For each step, in order:

1. `start` = the **request-start user-message timestamp** for the step: among the step's
   `timing_events`, take the earliest model-output (`reasoning`/`text`/`tool_call`) timestamp as
   `first_output`, keep the `user_message` timestamps at-or-before `first_output`, and take the
   latest such candidate (None if there is no user message or no output, or none qualifies). If
   `start` is not None and the step has a string `session_id`, **close** any open request for that
   session, then **open** a fresh request `{provider, start_at: start, last_output_at: None}`.
2. `resp_end` = the step's **last response-end timestamp** (the latest `reasoning`/`text`/`tool_call`
   timestamp). If the session has a string `session_id` and `resp_end` is not None and the session
   has an open request, advance that request's `last_output_at` when it is unset or `resp_end` is strictly
   later.

After the walk, every still-open request is flushed with `close_user_turn` in dict-insertion order
(end-of-stream flush), so the final request of each session contributes its response time too.

This is a **trace-level estimate**, not a serving-engine timer; it reflects only recorded events.
The span includes intermediate tool-triggered generations and observed tool waits *within* the
request, and **excludes** the following human wait and post-response usage-accounting events. The
trigger is the latest `user_message` before the first model output in a row, so stale/resumed user
messages embedded earlier in the row are not counted.

Method and assumptions:

- **Exact, not sampled.** Every positive request duration contributes one value to its provider's list
  (and to `all`); the percentiles run over the full set. The old loader already kept every value here
  — there was never a reservoir cap on this metric — so the migration is value-for-value identical.
- **File-order state.** The walk is over `round_pk` (ingestion ordinal == file order), reproducing
  the line-order tie-break the old single-pass JSONL loader relied on for its session state,
  including the dict-insertion-order end-of-stream flush.
- **Provider grouping** mirrors the old loader's `str(provider) or "<unknown-provider>"` fallback, so
  a missing/empty provider falls into `<unknown-provider>`. The provider stored on a request is the one
  from the step that *opened* it.
- **Engine-independent timestamps.** Timestamps are read from the DB as integer epoch-microseconds
  (`CAST(epoch_us(timestamp) AS BIGINT)`) and rebuilt to naive datetimes in Python, never fetched as
  a raw `TIMESTAMP` (native duckdb marshals that to a `datetime`, duckdb-wasm to a string). A
  difference between two same-timezone datetimes equals the naive-microsecond difference exactly, so
  the durations match the pre-DuckDB result bit-for-bit.

## Code structure

`analyze.py` is a query→shape→write pipeline over the shared trace DuckDB:

- `load_user_turn_response_seconds_by_provider(con)` — the only data-loading code. It pulls per-step
  `timing_events` (event_type + epoch-microsecond timestamp, in `round_pk`/`event_index` ingest
  order) and the per-step `(session_id, provider)` from `rounds`, then runs the stateful request
  state machine above, returning `{"all": [...], provider: [...]}`. The full per-provider lists are
  returned, no sampling.
- `_response_trigger_user_message_timestamp(events)` / `_last_response_end_timestamp(events)` —
  reproduce the pre-DuckDB `timing.response_trigger_user_message_timestamp` and
  `timing.last_response_end_timestamp` for one step's events.
- `_epoch_us_to_datetime(...)` — rebuilds a naive datetime from epoch-microseconds.
- `ordered_provider_duration_items` / `duration_summary_row` / `write_user_turn_response_time_summary`
  — shape the per-group rows and write the summary CSV (unchanged from the pre-migration script).
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`).

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/human_in_the_loop/user_turn_response_time/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/human_in_the_loop/user_turn_response_time/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/human_in_the_loop/user_turn_response_time/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs (written to `-o`, default this folder)

- `user_turn_response_time_summary.csv` — per-group (`all` + each provider) `count`, `mean_seconds`,
  `p25/p50/p90/p99_seconds`, and `max_seconds` over the full request response-time lists.

CSV only (no figures). For decompositions of where this time goes, see the sibling experiment
`user_turn_decomposition/` and the validators `validators/human_in_the_loop/user_turn_gap_audit/`
and `validators/human_in_the_loop/e2e_formula_check/`.

## SyFI result analysis

This experiment emits no PNG figures — its single output is the summary CSV below.

### user_turn_response_time_summary.csv

One row per group: the `all` aggregate first, then each provider in the toolkit's canonical provider
order. Columns are the request `count`, the `mean_seconds`, the `p25/p50/p90/p99_seconds` quantiles, and
the `max_seconds`. Read the median against the tail percentiles to judge how heavy each provider's
right tail is: a `p50` of tens of seconds next to a `p99` in the thousands means most requests finish
quickly but a small fraction run very long (typically requests with many intermediate tool waits). Each
provider row is directly comparable to `all` and to the other providers, since the durations are
exact (unsampled).
