# overview_summary

**What are the headline aggregate facts of a normalized trace — sessions, requests, agent steps,
token accounting, generation timing, human wait, and tool use — for `merged`, `claude`, and `codex`
separately?**

## Experiment overview

Each row in the trace is one agent step: a single model call inside a request. This experiment rolls
every agent step up into one compact
summary (printed as text, or as JSON with `--json`), computed for the `merged` scope and again for
each provider seen. The sections:

- **scope** — total sessions, distinct users, agent steps, requests with a visible `user_message`,
  tool-triggered steps, and the earliest / latest observed timestamp.
- **tokens** — total / append / cached-read input lengths and per-step averages; user-initiated vs
  tool-triggered prompt-growth and context-delta stats; prefix hit rates; output and reasoning
  token totals. Growth bucketing reuses `artifacts/utils/growth.py` (`InputGrowthStats`,
  micro-reduction `≤ 1024`, major-reduction `≥ 50000` tokens).
- **generation_timing** — observable generation span (latest input event → last model-output event),
  input-to-reasoning-end span, waiting-for-human-input stats, and the post-reasoning TPOT / residual
  TTFT estimates that are populated **only** for agent steps with exact `reasoning_output_tokens` (the
  Codex subset; `null` otherwise). These are trace-observed spans, not serving-engine timers.
- **tools** — tool-call totals, calls per request, and effective-latency stats
  (internal latency preferred, else wall; nonpositive latencies counted separately).
- **rounds_by_provider** / **rounds_by_model** — provider and model step counts. The field names are
  kept for schema compatibility.

Method and key assumptions:

- **Total input** for an agent step is `prefix_tokens + newly_append_tokens`; `output_tokens` is inclusive
  of reasoning, and `reasoning_output_tokens` (when present) is a subset of it.
- An agent step's **trigger** / first-input event is its **first timing event** (`event_index = 1`).
  Context deltas and prompt-growth pair each step against the **previous step seen in the same
  session** in trace order (`round_pk` = file order).
- Percentiles use the same linear-interpolation method as the rest of the toolkit, over the
  in-memory per-step value lists.

## Code structure

This is a **DB-backed** experiment: the trace DuckDB does the single-pass ingest, and Python keeps
the per-step aggregation and growth bucketing unchanged.

- `read_summary_from_db(con)` — the core computation entry point. Loads per-step dicts from the DB
  and folds each into a `SummaryBundle` (merged plus per-provider).
- `_rows_from_db(con)` — the only data-loading code. Three queries in ingestion order: step scalars
  (`ORDER BY round_pk`), each step's `timing_events` (`ORDER BY round_pk, event_index` so
  `event_index = 1` is first), and each step's `tool_calls` (`ORDER BY round_pk, tool_index`). It
  rebuilds per-step dicts carrying exactly the keys the summary helpers read, so the unchanged
  `Summary.add` / growth logic runs over them verbatim.
- `_epoch_us_to_iso(...)` — timing and tool timestamps are pulled as integer epoch-microseconds
  (native/wasm identical) and rebuilt to the canonical `…Z` ISO string, so the datetime-parsing
  helpers behave bit-for-bit like the pre-DuckDB JSONL path.
- `read_summary(path)` — preserved public API. Materializes the JSONL path into a temp trace DB via
  `trace_db`, then defers to `read_summary_from_db`. The web driver calls
  `read_summary(input_path).as_dict()` in-process and `run_all.py` runs `-i <trace> --json` as a
  subprocess; both keep working unchanged.
- `Summary` / `SummaryBundle` / `as_dict` / `print_text` and all per-step metric helpers are
  unchanged — only the data source was swapped from line-parsing JSONL to DB fetches.
- `InputGrowthStats`, `MAJOR_REDUCTION_MIN_TOKENS`, `MICRO_REDUCTION_MAX_TOKENS`,
  `first_timing_event_type` — unchanged shared helpers from `artifacts/utils/growth.py`.

The data layer lives in `artifacts/utils/trace_db.py` (see `artifacts/utils/DB_SCHEMA.md`).

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/trace_facts/overview_summary/analyze.py

# JSON instead of text
uv run python artifacts/trace_facts/overview_summary/analyze.py --json > summary.json

# a specific trace
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this)
uv run python artifacts/trace_facts/overview_summary/analyze.py --db /tmp/trace.duckdb --json
```

`-o/--output-dir` is accepted (shared I/O surface) but unused here — no files are written.

## Outputs

Prints to stdout only: a structured text report by default, or the same data as JSON with `--json`.
No files or figures are written. The JSON is a dict keyed by scope (`merged`, then each provider)
mapping to the `scope` / `tokens` / `generation_timing` / `tools` / `rounds_by_provider` /
`rounds_by_model` sections described above.

On sanitized traces, `distinct_users` counts stable pseudonymous `user` values, not the original
local account names.

## SyFI result analysis

The summary is the entry point for reading a trace before drilling into any single experiment.

- **scope** anchors everything: how many agent steps, sessions, requests, and users the numbers are computed over,
  and the time window the trace covers. Read it first to size the dataset.
- **tokens** is where the context story lives. The total / append / cached-read split and the per-step
  averages show how heavily prefix caching is reused; the user-initiated vs tool-triggered cuts separate
  human-driven steps from tool-driven follow-up steps, and the growth/reduction buckets flag how
  often the window genuinely compacts versus merely jitters.
- **generation_timing** gives observable, trace-level latency: the generation span and normalized
  decode speed apply to every agent step, while the TPOT / TTFT estimates are populated only for the
  exact-reasoning (Codex) subset and are `null` elsewhere — do not read them as engine timers.
- **tools** summarizes tool intensity (calls per request) and effective latency; the
  internal-vs-wall source counts indicate how much of the latency is runner-reported.
- **rounds_by_provider** / **rounds_by_model** give the provider/model step mix behind the merged numbers,
  so a skewed `merged` headline can be traced to the dominant provider.

Because the same `merged` and per-provider summaries are emitted together, the fastest read is to
compare a provider's section against `merged` to see which behaviors it drives.
