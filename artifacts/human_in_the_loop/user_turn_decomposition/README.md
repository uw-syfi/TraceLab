# user_turn_decomposition

**For a user turn, how does the end-to-end response time split into model generation, tool time, and
unexplained residual?**

## Experiment overview

This audit reuses the **user-turn** definition from `user_turn_response_time/` — within one session,
a turn runs from the user message that *triggers* it to the last response-end output before the
*next* triggering user message — and asks where that elapsed time goes. For each turn it accumulates:

- **generation total** = the sum of per-round observed generation spans (latest qualifying input →
  last model output in the round) across the turn's rounds;
- **tool effective total** = the sum of effective tool latencies (`tool_internal_latency_ms` if
  present, else `tool_wall_latency_ms`) over the turn's tool calls;
- **tool wall total** = the same sum using wall latency only;
- **residual** = `e2e − generation − tool_latency`, reported once with effective and once with wall
  tool latency. The residual is a **subtraction diagnostic**, not a semantic state, and can go
  negative when overlapping/observed spans exceed the measured e2e.

It is computed by a stateful single-pass walk over rounds in ingestion order (`round_pk` == file
order), keeping `active_by_session: {session_id -> ActiveTurn}`. A turn is bounded by a small state
machine. `close_turn(session_id)` pops the session's open turn and folds it into both the turn's
provider `ProviderStats` and the `all` aggregate; `add_turn` drops a turn with no response end
(`dropped_no_end`) or with nonpositive e2e (`dropped_nonpositive`), and otherwise records its e2e,
generation, tool effective/wall, residuals, and the post-output usage-report gap. For each round, in
order:

1. `user_start_at` = the **response-trigger user-message timestamp** for the round: among the round's
   `timing_events`, take the earliest model-output (`reasoning`/`text`/`tool_call`) timestamp as
   `first_output`, keep the `user_message` timestamps at-or-before `first_output`, and take the
   latest such candidate (None if there is no user message or no output, or none qualifies). If
   `user_start_at` is not None and the round has a string `session_id`, **close** any open turn for
   that session, count one response-triggering user row for the provider, then **open** a fresh turn
   starting at `user_start_at`.
2. If the session has an open turn, the round is folded into it: `rows` increments; a positive
   generation span (latest input-at-or-before-first-output → last output) is added to
   `generation_seconds`; `last_model_output_at` advances to the round's latest model output; `end_at`
   advances to the round's latest response-end output; `usage_report_at` advances to the round's
   latest `usage_report` timestamp; and each of the round's tool calls adds its positive effective
   and positive wall latency (and bumps the tool-call count).

After the walk, every still-open turn is flushed with `close_turn` in dict-insertion order
(end-of-stream flush), so the final turn of each session contributes too. Codex `usage_report`
events are tracked separately (the post-output usage-accounting gap) and are **not** part of e2e.

Method and assumptions:

- **Exact, not sampled.** Every retained turn contributes one e2e/generation/tool value and one
  per-turn residual sample to its provider's lists (and to `all`); the totals and the `p25/p50/p90`
  residual percentiles run over the full sets. The old loader kept every value here — no reservoir —
  so the migration is value-for-value identical.
- **File-order state.** The walk is over `round_pk` (ingestion ordinal == file order), reproducing
  the line-order tie-break the old single-pass JSONL loader relied on for its session state,
  including the dict-insertion-order end-of-stream flush.
- **Provider grouping** mirrors the old loader's `str(provider) or "<unknown-provider>"` fallback, so
  a missing/empty provider falls into `<unknown-provider>`. The provider stored on a turn is the one
  from the round that *opened* it.
- **Tool latency precedence.** Effective tool latency is internal-else-wall; the legacy `latency_ms`
  fallback in the pre-DuckDB code was already dead (that key is absent from the normalized
  `tool_calls` schema), so it is not reproduced. Effective and wall sums each include only strictly
  positive per-tool latencies, while the tool-call count includes every tool — matching the old
  per-`tools`-list reduction.
- **Engine-independent timestamps.** Timestamps are read from the DB as integer epoch-microseconds
  (`CAST(epoch_us(timestamp) AS BIGINT)`) and rebuilt to naive datetimes in Python, never fetched as
  a raw `TIMESTAMP` (native duckdb marshals that to a `datetime`, duckdb-wasm to a string). A
  difference between two same-timezone datetimes equals the naive-microsecond difference exactly, so
  every span and residual matches the pre-DuckDB result bit-for-bit.

## Code structure

`analyze.py` is a query→walk→write pipeline over the shared trace DuckDB:

- `load_timing_events(con)` / `load_round_tools(con)` — the data-loading code. The first pulls
  per-round `timing_events` (event_type + epoch-microsecond timestamp, in `round_pk`/`event_index`
  ingest order) into `{round_pk -> [(event_type, datetime)]}`; the second aggregates each round's
  `tool_calls` rows into `{round_pk -> RoundTools}` (count, positive effective seconds, positive wall
  seconds), matching the old per-`tools`-list reduction.
- `response_trigger_user_message_timestamp` / `last_model_output_timestamp` /
  `last_response_end_timestamp` / `input_to_last_output_span_seconds` / `timestamps_for` — reproduce
  the pre-DuckDB `artifacts/utils/timing.py` helpers over one round's `(event_type, timestamp)` list.
- `_epoch_us_to_datetime(...)` — rebuilds a naive datetime from epoch-microseconds.
- `ActiveTurn` / `ProviderStats` — the per-turn accumulator and the per-provider rollup (e2e,
  generation, tool effective/wall, residual samples, dropped-turn counters); carried over unchanged.
- `percentile` / `hours` / `fmt_h` / `fmt_s` — the residual percentile and number formatting used in
  the markdown tables (unchanged).
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`), runs the
  stateful per-session walk, and writes the markdown report. To keep the `run_all.py` / web-driver
  `style="global"` contract working, a module-level `INPUT` default remains: the driver assigns
  `module.INPUT = <path>` and calls `main()` with no flags, and `main()` falls back to `INPUT` when
  neither `--db` nor an explicit `-i` is given.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/human_in_the_loop/user_turn_decomposition/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/human_in_the_loop/user_turn_decomposition/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/human_in_the_loop/user_turn_decomposition/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

The `run_all.py` registry launches this experiment with `style="global"` (no CLI flag): the shim
imports the module, sets `module.INPUT = <trace>`, and calls `main()`, which honors that `INPUT` as
its `-i` default.

## Outputs (written to `-o`, default this folder)

- `result_analysis.md` — the decomposition audit: a **Provider Totals** table (response-triggering
  user rows, sampled turns, and `e2e` / `generation` / `tool effective` / `tool wall` /
  `residual effective` / `residual wall` totals in hours, plus the usage-report-after-output gap), a
  **Per-Turn Residual Distribution** table (`p25/p50/p90`, average, and negative-turn count for the
  effective and wall residuals), and a **Dropped Turns** table (turns with no response end or
  nonpositive duration). The `Input:` line records the trace or DB the run read.

Markdown only (no figures). For the per-turn response-time summary this decomposes, see the sibling
experiment `user_turn_response_time/`; the residual is drilled into by the validators
`validators/human_in_the_loop/user_turn_gap_audit/` and
`validators/human_in_the_loop/e2e_formula_check/`.

## SyFI result analysis

This experiment emits no PNG figures — its single output is the markdown report below.

### result_analysis.md

Read the three tables together. In **Provider Totals**, the `e2e` column is the denominator: compare
`generation` and `tool effective`/`tool wall` against it to see how a provider's elapsed turn time
divides between model thinking and tool execution, and read the `residual` columns as the
unattributed remainder (`e2e − generation − tool`). A large positive residual means measured spans
fall short of the e2e (idle/overhead inside the turn); a negative residual means generation and tool
spans overlap or exceed e2e, which is expected when concurrent tool waits double-count. The
**Per-Turn Residual Distribution** shows whether that residual is centered near zero with a heavy
tail (most turns are well-explained, a few are not) and how often it goes negative per provider, for
both the effective and wall tool-latency definitions. The **Dropped Turns** table is a data-quality
check: `no response end` counts turns that never saw a closing output, and `nonpositive duration`
counts turns whose e2e was ≤ 0; both should stay small relative to the sampled-turn count. Each
provider row is directly comparable to `all` and to the other providers, since the totals and
percentiles are exact (unsampled).
