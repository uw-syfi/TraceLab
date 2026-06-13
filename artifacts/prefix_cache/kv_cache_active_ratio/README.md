# kv_cache_active_ratio

**If a serving engine evicts a session's KV cache after an idle timeout `T`, what fraction of total
wall time would the cache still be "active" (reused rather than rebuilt) — bounding how useful prefix
caching is across eviction policies?**

## Experiment overview

For each candidate eviction timeout `T` (a fine log sweep from 1s to 1h, with the 1m/5m/10m/30m/1h
landmarks pinned in), the active ratio is

```
active_ratio(T) = gen_total / (gen_total + tool_total≤T + human_wait_total≤T)
```

evaluated **per provider**:

- `gen_total` — the full observed generation total: the sum over agent steps of the
  input→last-output span (the time the cache is doing useful work). Only finite, strictly-positive
  spans are summed.
- `tool_total≤T` — cumulative time in tool waits whose *individual* effective latency is `≤ T`
  (short enough that the cache survives the gap; longer gaps are assumed to evict and so don't count
  against active time). Effective tool latency is converted ms→s.
- `human_wait_total≤T` — cumulative time in inter-request human waits whose individual duration is
  `≤ T`, same survives-the-gap logic.

The cumulative-at-threshold sums (`cumulative_values_at_thresholds_seconds`) and the threshold grid
(`kv_cache_timeout_thresholds_seconds`) are the shared `formatters.py` helpers, unchanged.

Method and assumptions:

- **Generation span per agent step** mirrors `timing.input_to_last_output_span_seconds`: with
  `first_output = min(reasoning/text/tool_call timestamp)` and `last_input = max(user_message or
  tool_result timestamp ≤ first_output)`, the span is `last_output − last_input`, kept only when
  strictly positive (a step with no qualifying input or output contributes nothing).
- **Effective tool latency** = `tool_internal_latency_ms` if present, else `tool_wall_latency_ms`
  (the shared `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL` precedence; the legacy `latency_ms` field is
  not in the normalized data). Only strictly-positive latencies enter the tool total.
- **Human input wait** is the stateful inter-request gap, within a session, from the model's *previous*
  output to the *next* request-start user message, taken over agent steps in ingestion order
  (`round_pk` == file order). Only strictly-positive waits count. This matches the `human_input_wait`
  experiment's definition exactly.
- **Provider grouping** mirrors the old loader's `str(provider) or "<unknown-provider>"` fallback
  (SQL `COALESCE(provider, '<unknown-provider>')`), so a missing/empty provider lands in
  `<unknown-provider>`.
- **Exact, not sampled.** All three inputs (per-step generation spans, per-call tool latencies,
  per-session human waits) are full lists — the old loader already kept every value here, with no
  reservoir cap on any of these metrics — so the migration is value-for-value identical and the
  output CSV is byte-for-byte unchanged.
- **Engine-independent timestamps.** Timestamps are read as integer epoch-microseconds
  (`CAST(epoch_us(timestamp) AS BIGINT)`) and differenced in SQL/Python, never fetched as a raw
  `TIMESTAMP` (native duckdb marshals that to a `datetime`, duckdb-wasm to a string). A difference of
  two same-timezone instants equals the naive-microsecond difference exactly, so spans and waits
  match the pre-DuckDB result bit-for-bit.
- These are **trace-level estimates**, not engine timers.

## Code structure

`plot.py` is a query→shape→plot pipeline over the shared trace DuckDB:

- `load_llm_generation_seconds_by_provider(con)` — `{provider: [span_s, …]}`, the per-step
  input→last-output span (positive only). The input/output event-type sets and the
  `last_input ≤ first_output` rule are reproduced in SQL via the `bounds`→`agg` CTEs.
- `load_tool_latency_values_by_provider(con)` — `{provider: [latency_ms, …]}`, every strictly-
  positive effective tool latency, joined back to its step's provider.
- `load_human_input_wait_seconds_by_provider(con)` — `{"all": [...], provider: [...]}`. SQL produces,
  per step in `round_pk` order, the request-start user timestamp and the last-model-output
  timestamp; a stateful Python walk keeping `last_output_by_session` then emits each positive
  inter-request wait. Reproduces the old single-pass loader's session state.
- `_in_set_sql(...)` — small helper to render an `event_type IN (...)` list.
- `plot_kv_cache_active_ratio_by_provider(...)` / `write_kv_cache_active_ratio_by_provider(...)` —
  the figure and CSV; unchanged from the pre-migration script (same curve math, landmark lines,
  inset table, and CSV columns).
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) and embeds
  the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/prefix_cache/kv_cache_active_ratio/plot.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/prefix_cache/kv_cache_active_ratio/plot.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/prefix_cache/kv_cache_active_ratio/plot.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs

Written to `-o` (default this folder):

- `kv_cache_active_ratio_by_provider.png` — active ratio vs eviction timeout, one curve per provider.
- `kv_cache_active_ratio_by_provider.csv` — the underlying curve: per `(provider, timeout)` the
  `cache_eviction_timeout_seconds`/`_label`, `landmark_timeout`, `generation_seconds`/`_hours`,
  `tool_cumulative_seconds`/`_hours`, `human_cumulative_seconds`/`_hours`,
  `denominator_seconds`/`_hours`, and `kv_cache_active_ratio`.

Each PNG is self-contained — it embeds this README, the CSV, and the plotting code (`plot.py` + the
shared `artifacts/utils/` modules). Unpack with `python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### kv_cache_active_ratio_by_provider.png

The active ratio vs the cache-eviction timeout `T`, one curve per provider, on a log-x timeout axis
from 1s to 1h with dashed landmark lines at 1m/5m/10m/30m/1h and the y-axis as a percentage. Each
curve is monotonically non-decreasing in `T`: as the engine tolerates longer idle gaps before
evicting, more tool/human waits fall under the survives-the-gap threshold and count as active rather
than as cold rebuild time, so the ratio rises toward 1. Read a curve's height at a landmark to gauge
how much of total wall time the KV cache would be live under that eviction policy — a high value at a
short `T` means the cache pays off even with aggressive eviction, while a curve that only climbs near
the 30m/1h marks signals that most reuse depends on holding the cache through long human/tool idle
gaps. The inset table reports each provider's generation total and its active ratio at the five
landmark timeouts; the legend carries the generation hours per provider.
