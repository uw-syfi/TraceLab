# cache_hit_ratio

**What fraction of each agent step's input is served from the cached prefix, and how does that prefix
hit ratio distribute across user-initiated and tool-triggered steps?**

## Experiment overview

Every agent step in the trace carries an input-token accounting
(`input_tokens_total = prefix_tokens + newly_append_tokens`). This experiment treats the cached
prefix as a cache *hit* and the freshly-appended tokens as the *miss*, and reports the per-step
hit ratio, split by what started the step.

Method and assumptions:

- **Prefix hit ratio** = `prefix_tokens / (prefix_tokens + newly_append_tokens)` per step — the
  share of input tokens that were a cache read rather than newly charged.
- **Step eligibility / trigger.** A step is included only when its **first timing event**
  (`timing_events` with `event_index = 1`, the in-order first event) is a `user_message` or a
  `tool_result`; all other first-event types (and steps with no timing events) are dropped. That
  first event also sets the trigger: `user` for `user_message`, `tool_result` otherwise. The
  `first_input_event_type` *column* is **not** used — it diverges from the first timing event (it
  has nulls where a first timing event exists), so the legacy `timing_events[0]` semantics are
  reproduced via the timing-events table.
- **Validity gate.** Both `prefix_tokens` and `newly_append_tokens` must be non-null, and their
  sum must be `> 0` (a zero/empty step contributes nothing and never divides by zero).
- **Exact, not sampled.** Means, percentiles (custom linear interpolation, matching
  `np.percentile`), histograms, and bins are computed over **every** eligible step via the shared
  trace DuckDB — there is no reservoir cap.
- **Grouping.** Each step feeds both its provider scope and the `merged` scope, under both its
  trigger and the `all` trigger. Scopes reported: `merged` / `claude` / `codex` (a null provider
  falls back to `unknown`). The **append-weighted** views weight each step by its append tokens to
  show where the token mass sits, not just step counts.

## Code structure

`analyze.py` is a query→shape→write/plot pipeline over the shared trace DuckDB:

- `read_groups(con)` — one join of `rounds` to the first timing event
  (`timing_events WHERE event_index = 1`), gated and ordered by `round_pk` (file order), returning
  `{(scope, trigger): HitRatioGroup}` where each group keeps every step's `(hit_ratio,
  append_tokens)`. The cache-hit definition, eligibility gate, and provider/`merged` fan-out live
  here.
- `percentile(...)` / `hit_bin_index(...)` / `bin_color(...)` — the exact percentile interpolation
  and the fixed hit-ratio bin edges/colors shared by the CSVs and the figures.
- `write_summary_csv` / `write_bins_csv` / `write_round_split_csv` — the three CSVs.
- `plot_histograms(..., weighted_by_append=...)` — the `SCOPES × TRIGGERS` panel grid, rendered
  once step-weighted and once append-token-weighted (matplotlib behavior unchanged from the
  pre-migration script).
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) to the
  above and embeds the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/prefix_cache/cache_hit_ratio/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/prefix_cache/cache_hit_ratio/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/prefix_cache/cache_hit_ratio/analyze.py --db /tmp/trace.duckdb -o /tmp/out

# CSV-only (skip figures)
uv run python artifacts/prefix_cache/cache_hit_ratio/analyze.py --no-plots
```

## Outputs (written to `-o`, default this folder)

- `cache_hit_ratio_histogram.png` — step-weighted hit-ratio histogram, paneled scope × trigger.
- `cache_hit_ratio_append_weighted_histogram.png` — the same panels weighted by append tokens.
- `cache_hit_ratio_summary.csv` — per `(scope, trigger)`: step/append counts, mean, percentiles
  (p01…p99), and the step-share / append-share thresholds (`<0.5`, `0.5–0.9`, `>=0.9/0.95/0.98/0.99`).
- `cache_hit_ratio_bins.csv` — per `(scope, trigger, bin)`: step count + share and append-token
  count + share across the fixed hit-ratio bins.
- `cache_hit_ratio_round_split.csv` — per `(scope, trigger)`: step counts/shares across the coarse
  `<10% / 10-40% / 40-80% / 80%+` buckets.

The PNGs are self-contained — they embed this README, the CSVs, and the plotting code (`analyze.py`
+ shared `artifacts/utils/` modules). Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### cache_hit_ratio_histogram.png

A `scope × trigger` grid of step-weighted hit-ratio histograms (bars colored red/amber/blue by
hit-ratio band). The dominant signal is a tall bar in the top `(.99,.995]`/`(.995,1]` bins: the
**vast majority of steps are near-perfect cache hits**, so coding agents pay for only a thin
freshly-appended slice each step. The split by trigger is the interesting part: `tool_result`-started
steps cluster even harder at the high end (the prior context is reused almost verbatim), while
`user`-started steps carry a visible low-ratio tail — a fresh user message can invalidate more of
the prefix. Compare the per-provider rows against `merged` to see whether one provider drives the tail.

### cache_hit_ratio_append_weighted_histogram.png

The same panels, but each step is weighted by its append tokens, so the bars show where the
*token mass* (and thus the billable cost) actually lands rather than where the step *count* lands.
The shape shifts markedly versus the step-weighted view: the high-hit bins shrink and the low-hit
bins grow, because the rare low-ratio steps are exactly the ones appending large amounts of new
text. Read this panel to find where new-token spend concentrates — `cache_hit_ratio_summary.csv`'s
`append_hit_*` columns give the exact mass shares per band.
