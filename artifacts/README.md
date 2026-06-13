# artifacts

Analysis & plotting **experiments**, organized by category, plus shared helpers in
`artifacts/utils/`.

Each experiment is a folder `artifacts/<category>/<experiment>/` containing:

- one analyze/plot script (`plot.py`, `analyze.py`, or a descriptively named script
  when the experiment ships more than one);
- a `README.md` stating the question, the input, the exact metric definitions and
  assumptions, how to run it, and its outputs;
- its generated outputs, written **into the same folder** when the script runs.

Only the scripts and `README.md` files are tracked in git. Generated outputs
(`*.png`, `*.csv`, `*.json`, `*.jsonl`, and non-README `*.md` reports) are gitignored.

## Categories

- `utils/` — shared "common helpers" imported by experiments (no experiments here):
  cohesive modules `style.py`, `accumulators.py`, `timing.py`, `formatters.py`,
  `tool_stats.py`, `cdf.py`, `trace_loader.py` (the loader + accumulators + formatters +
  generic CDF renderers split out of the former monolithic
  `scripts/plot_trace_stats.py`), plus `png_sidecar.py` (self-contained-PNG
  embedding/extraction) and `growth.py` (same-session growth stats). Per-figure payload
  lives in each experiment, not here. See `utils/README.md` for the module layout and the
  single source of truth on metric definitions.
- `trace_facts/` — `overview_summary`, `csv_export`.
- `llm_generation/` — input token composition (`prefix_append_distribution`,
  `adjusted_prefix_append`, `output_append_assignment`, `token_spindles`,
  `output_tokens`) and generation timing (`generation_time_cdf`,
  `append_vs_prefix_latency`, `timing_fit`, `timing_feature_ambiguity`).
- `tool_calls/` — `tool_latency_distribution`, `tool_call_counts`, `tool_time_by_kind`,
  `tool_category_distribution`, `claude_long_tool_calls`.
- `prefix_cache/` — cache hit-rate behavior: `cache_hit_ratio`,
  `cache_hit_idle_relationship`, `cache_replay`, `kv_cache_active_ratio`.
- `human_in_the_loop/` — `human_input_wait`, `user_turn_response_time`,
  `user_turn_decomposition`.
- `session/` — `session_token_steps`, `total_input_growth`.

Validation and audit checks live under `../validators/` and are run with
`uv run python validators/run_all.py`.

## Running

Each experiment defaults its input to `trace/llm_round_trace.merged.all_users.jsonl`
and writes outputs next to its script:

```bash
uv run python artifacts/<category>/<experiment>/<script>.py [-i trace/<file>.jsonl]
```

Plotting experiments embed their README, source CSV data, and plotting code into every
PNG as the final step; read them back with `python artifacts/utils/png_sidecar.py`.

### Running everything

`artifacts/run_all.py` is a dispatcher that runs every experiment (or a subset),
defaulting to 16 at a time. It knows each experiment's invocation style (`-i`,
`--input`, a module-level `INPUT` default, `csv_export`'s `-i`/`-o`, `overview_summary`'s
stdout→`summary.json`), derives the local timing CSV before timing experiments, and runs
`build_summary` only after `timing_feature_ambiguity` succeeds. Each experiment's console
output is captured to a per-experiment log under the `--log-dir`.

```bash
uv run python artifacts/run_all.py                 # all experiments, 16-way parallel, full trace
uv run python artifacts/run_all.py --list          # show the registry
uv run python artifacts/run_all.py --jobs 1        # serial
uv run python artifacts/run_all.py --only tool_calls
uv run python artifacts/run_all.py --only prefix_cache/cache_hit_ratio
uv run python artifacts/run_all.py --input trace/llm_round_trace.public.jsonl
```

Timing experiments (`append_vs_prefix_latency`, `timing_feature_ambiguity`, `timing_fit`)
read the local timing-segment CSV at
`artifacts/llm_generation/timing_fit/timing_fit_trace.csv`. The dispatcher builds it from
`--input` by running `llm_generation/timing_fit/build_trace` first. Pass `--timing-input`
only when you intentionally want to consume an existing external CSV instead.

The default dependency flow is:

```text
normalized JSONL trace
  -> llm_generation/timing_fit/build_trace
  -> llm_generation/timing_fit
  -> llm_generation/append_vs_prefix_latency
  -> llm_generation/timing_feature_ambiguity
  -> llm_generation/timing_feature_ambiguity/build_summary
```

Use these commands when checking the timing path by itself:

```bash
uv run python artifacts/run_all.py \
  --only llm_generation/timing_fit/build_trace \
  --input trace/llm_round_trace.public.jsonl

uv run python artifacts/run_all.py \
  --only llm_generation/timing_fit \
  --input trace/llm_round_trace.public.jsonl
```

Generated artifact outputs are intentionally local to each experiment folder and ignored
by git. Validation/audit outputs are separate and should be produced with
`validators/run_all.py`.
