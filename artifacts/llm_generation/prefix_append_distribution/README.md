# prefix_append_distribution

**For each agent step, how large is the input prompt and how is it split between the *cached prefix*
it reuses and the *newly appended* tokens it must pay for?**

## Experiment overview

Every agent step in the trace carries an input-token accounting:
`input_tokens_total = prefix_tokens + newly_append_tokens`.

- **prefix_tokens** = the cached prefix reused from the previous request
  (Claude `cache_read_input_tokens`; Codex `cached_input_tokens`).
- **newly_append_tokens** = tokens charged as new in this step (Claude
  `input_tokens + cache_creation_input_tokens`; Codex `input_tokens_total − cached_input_tokens`).
  See `../../../docs/prompt_cache_accounting.md` for the full cache-accounting derivation.

This experiment renders the prefix/append distributions (histogram + CDF), a prefix-vs-append
scatter, and a token-mass-weighted view of append lengths.

Method and assumptions:

- **Exact, not sampled.** Histograms, CDFs, percentiles, means, and the append-weighted bins are
  computed over **every** step via the shared trace DuckDB. (The old per-script loader
  reservoir-sampled at 200k/group to bound memory; that cap is gone, so the `sampled` column is
  always `False` and `sample_count` equals the full `count`.) Percentiles use `np.percentile`
  (linear interpolation); the mean reproduces the old running float sum exactly by summing the
  per-group values in ingest (`round_pk`) order.
- **Validity gate.** A token value counts when it is non-null and `>= 0` (the old NumericTracker's
  `allow_zero` rule); nulls feed `missing`, negatives feed `invalid`. The append-weighted bins and
  the scatter use rows where **both** prefix and append are `>= 0` (the old loader's pair gate).
- **Binary token axis.** Distributions are plotted on a base-2 log token axis.
- **Grouping** follows `--group-by` (default `provider`; also `model` / `provider_model`), with
  `<unknown-provider>` / `<unknown-model>` COALESCE fallbacks mirroring the old `group_key`.
- **Append-weighted bins** weight each append-token bucket by total tokens, so the bars show where
  the *token mass* lives, not just step counts.
- **The scatter is a deterministic visual subsample.** A prefix-vs-append scatter cannot draw
  350k+ points, so it keeps a fixed-size subsample (`--pair-sample-size`, default 80k). Instead of
  the old reservoir, the subsample is chosen in SQL by a Knuth-multiplicative hash of the surrogate
  key: `ORDER BY (round_pk * 2654435761) % 1000000, round_pk LIMIT <pair-sample-size>` over rows
  with `prefix_tokens >= 0 AND newly_append_tokens >= 0`. This is reproducible across DB builds and
  engines but is **not** the old reservoir, so the scatter figure is not byte-compatible with the
  pre-migration run (the CSVs are).

## Code structure

`plot.py` is a query→shape→plot pipeline over the shared trace DuckDB:

- `load_token_groups(con, *, group_by)` — per-group prefix/append `MetricStats` (every valid value
  in ingest order, plus `missing`/`invalid` counts) and the group's total `rows`, plus an `all`
  group. `MetricStats.summary()` derives count/mean/min/max/percentiles exactly.
- `scatter_pairs(con, *, group_by, sample_size)` — the deterministic `(group, prefix, append)`
  visual subsample described above.
- `append_bins(con, *, by_provider)` — the global and per-provider append-token weighted bins
  (steps + summed append tokens per half-open bucket), exact over the pair-gated rows.
- `plot_token_histograms` / `plot_token_cdfs` / `plot_prefix_append_scatter` /
  `plot_append_weighted_bins` — the figures (matplotlib behavior unchanged from the pre-migration
  script).
- `write_token_summary` / `write_append_weighted_bins` — the two CSVs.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`) and
  embeds the self-contained PNG sidecar.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/llm_generation/prefix_append_distribution/plot.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/llm_generation/prefix_append_distribution/plot.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/llm_generation/prefix_append_distribution/plot.py --db /tmp/trace.duckdb -o /tmp/out
```

Useful flags: `--group-by` (`provider` / `model` / `provider_model`), `--max-groups` (max plotted
groups, default 8), `--pair-sample-size` (scatter subsample, default 80000).

## Outputs (written to `-o`, default this folder)

- `prefix_append_distribution.png` — prefix vs append token histograms.
- `prefix_append_cdf.png` — CDFs of prefix / append token length.
- `prefix_vs_append_sample.png` — prefix-vs-append scatter (deterministic visual subsample).
- `append_tokens_weighted_bins.png` / `.csv` — token-mass-weighted append bins.
- `token_length_summary.csv` — per-group prefix/append quantiles, mean, min/max, and counts.

The PNGs are self-contained — they embed this README, the CSVs, and the plotting code
(`plot.py` + shared `artifacts/utils/` modules). Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.

## SyFI result analysis

### prefix_append_distribution.png

Two histograms side by side: the **prefix** (cached) distribution sits far to the right — most
steps reuse a large cached prefix — while the **append** distribution is centered much lower, since
each step only pays for a modest freshly-appended slice. The legend's `p50`/`p90` quantify the gap;
both are exact over all steps now (no `sampled` note appears).

### prefix_append_cdf.png

The CDFs make the median and tail crossover explicit: the prefix curve rises late (large reused
prefixes dominate), while the append curve saturates early (most appends are small). Read the
x-position where each curve hits 50%/90% to compare typical vs tail input cost per provider.

### prefix_vs_append_sample.png

The scatter shows the joint shape: a large reused prefix does **not** imply a large append — points
spread widely in append for any given prefix band. This is a deterministic visual subsample (up to
`--pair-sample-size` points), so it conveys structure, not exact density; use the CSV/CDF for
quantitative reads.

### append_tokens_weighted_bins.png

Two stacked bars per provider — step-share on top, append-token-mass-share below — over the same
append-length buckets. The arrows pointing down from the count bar to the mass bar show the headline
result: **most steps are small, but most appended tokens come from the rare large steps.** The
small buckets dominate by count yet the large buckets dominate the token mass.
