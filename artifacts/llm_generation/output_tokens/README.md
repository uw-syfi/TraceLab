# output_tokens

**How many tokens does the model generate per agent step, and how does that distribution differ by
provider / model?**

## Experiment overview

For every agent step the trace records `output_tokens` — the step's generated-token count. This
experiment plots the **distribution** of that count, grouped by provider (or model, or
provider:model), on a base-2 log token axis, and writes per-group quantiles.

Method and assumptions:

- **What counts.** Every step whose `output_tokens` is non-null and `>= 0` contributes one value to
  its group (and to the synthetic `all` group). This matches the old loader's `allow_zero` numeric
  rule — zero-output steps are kept, negatives (never observed) are dropped.
- **Provider caveat.** For **Codex**, `output_tokens` *includes* reasoning tokens; for **Claude** it
  is the message-level output count. The distributions are therefore not strictly like-for-like
  across providers — read each provider on its own terms.
- **Exact, not sampled.** The distribution, percentiles, and histogram are computed over **every**
  observation. The pre-DuckDB loader reservoir-sampled at 200k values per group to bound memory while
  parsing JSON; querying the materialized DuckDB removes that constraint, so the stats are now exact.
  The summary CSV's `sampled` column is therefore always `False` and `sample_count` equals the full
  `count`. (On any trace below the old 200k cap — e.g. `trace/sample.jsonl` — the old path was
  already exact, so the migration is value-for-value identical there.)
- **Group fallbacks.** Grouping mirrors the old `group_key()` `"<unknown-provider>"` /
  `"<unknown-model>"` fallbacks via SQL `COALESCE`, so missing/empty provider or model values fall
  into an explicit `<unknown-*>` bucket rather than being dropped.

## Code structure

`plot.py` is a query→shape→plot pipeline over the shared trace DuckDB:

- `load_metric_by_group(con, *, column, group_by)` — the only data-loading code. It pulls every
  non-null, non-negative `output_tokens` value with its group label (one SQL `GROUP BY`-free scan)
  and returns `{group_label: MetricStats}` plus an `all` group. No sampling.
- `MetricStats` — a thin wrapper over the group's full `np.ndarray` of values, exposing exact
  `count` / `min` / `max` / `mean` and `percentiles(...)` (NumPy linear interpolation, matching the
  old percentile method).
- `selected_groups(stats, max_groups)` — the plotted groups: everything except `all`, biggest first,
  capped at `--max-groups`.
- `plot_output_tokens(...)` — renders the stepped histogram on the shared binary token axis
  (`formatters.token_axis_*`, `style.*`); matplotlib behavior unchanged from the pre-migration script.
- `write_output_token_summary(...)` — the per-group quantile CSV.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`), plus
  `--group-by` and `--max-groups`, and embeds the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# against the shared DuckDB built by run_all (no per-script re-parse)
uv run python artifacts/llm_generation/output_tokens/plot.py --db /tmp/trace.duckdb

# or straight from a JSONL trace (materialized to a temp cache on first use)
uv run python artifacts/llm_generation/output_tokens/plot.py -i trace/sample.jsonl

# group by model instead of provider, show more groups
uv run python artifacts/llm_generation/output_tokens/plot.py -i trace/sample.jsonl --group-by model --max-groups 12
```

## Outputs

- `output_tokens_distribution.png` — per-group output-token histogram on a base-2 token axis; the
  legend reports each group's `n`, `p50`, and `p90`.
- `output_tokens_summary.csv` — per-group quantiles: `count, min, p50, p90, p95, p99, max, mean`,
  plus `sample_count` (= `count`) and `sampled` (always `False`, since the stats are exact).

The PNG is self-contained: it embeds this README, `output_tokens_summary.csv`, and the plotting code
as compressed text chunks. Unpack with `python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### output_tokens_distribution.png

Read the curve together with `output_tokens_summary.csv`. The distribution is strongly
**right-skewed**: most steps emit a modest number of tokens (the bulk sits well left of the p90
tick), while a thin tail of long generations stretches toward the per-group `max`. The gap between
`p50` and `p99` in the summary quantifies that spread — the median is a poor summary of cost on its
own, because the upper-percentile steps dominate total generated tokens.

When more than one provider is present, compare the curves rather than overlaying a single number:
because Codex folds reasoning tokens into `output_tokens` while Claude does not, a heavier Codex tail
can reflect reasoning rather than more visible output. The `<unknown-provider>` / `<unknown-model>`
buckets, if they appear, flag steps with missing provenance and are worth checking before drawing
cross-provider conclusions.
