---
name: coding-trace-analyze
description: "Run, summarize, plot, validate, and export normalized coding-trace JSONL files. Use when computing aggregate session/provider/model/token counts, normalized decoding-speed proxies, exact-reasoning TPOT/TTFT estimates, prefix versus append token distributions, tool latency/count summaries, generation-time and human-wait CDFs, KV-cache active ratio, cache-hit ratios, timing-fit CSV analyses, multi-round CSV traces, self-contained PNG artifacts, validator audits under validators/, or interpreting per-experiment artifacts under artifacts/."
---

# Coding Trace Analyze

## Overview

Use this skill to analyze normalized round traces after collection and optional sanitization. The analysis scripts expect the normalized schema described by `$coding-trace-normalize`.

Analyses are organized as **artifact experiments**. Each experiment is a folder under
`artifacts/<category>/<experiment>/` containing one analyze/plot script, a `README.md`
that states the question, inputs, and exact metric definitions, and (when run) the
generated outputs. Shared logic lives in `artifacts/utils/`. Validation and audit checks
live separately under `validators/<category>/<validator>/` and are run by
`validators/run_all.py`.

Categories:

- `artifacts/trace_facts/` — headline facts and format conversions (`overview_summary`,
  `csv_export`).
- `artifacts/llm_generation/` — input token composition + generation timing
  (`prefix_append_distribution`, `adjusted_prefix_append`, `output_append_assignment`,
  `token_spindles`, `output_tokens`, `generation_time_cdf`, `append_vs_prefix_latency`,
  `timing_fit`, `timing_feature_ambiguity`).
- `artifacts/tool_calls/` — `tool_latency_distribution`, `tool_call_counts`,
  `tool_time_by_kind`, `tool_category_distribution`, `claude_long_tool_calls`.
- `artifacts/prefix_cache/` — cache hit-rate behavior (`cache_hit_ratio`,
  `cache_hit_idle_relationship`, `cache_replay`, `kv_cache_active_ratio`).
- `artifacts/human_in_the_loop/` — `human_input_wait`, `user_turn_response_time`,
  `user_turn_decomposition`.
- `artifacts/session/` — `session_token_steps`, `total_input_growth`.
- `validators/` — integrity/formula/denominator checks such as
  `trace_facts/tool_duplicate_audit`, `human_in_the_loop/user_turn_response_audit`,
  `human_in_the_loop/user_turn_gap_audit`, and `human_in_the_loop/e2e_formula_check`.

## First Steps

1. Work from the coding-trace repo root, identified by `pyproject.toml`, `README.md`, and the `artifacts/` and `scripts/` directories.
2. Choose the input explicitly when possible. Prefer sanitized JSONL inputs for public reporting, usually `trace/llm_round_trace.public.jsonl`.
3. Use `uv run python ...` so `matplotlib`, `numpy`, and `pillow` resolve from the project environment.
4. Keep artifacts and validators separate: plotting/analysis outputs belong under `artifacts/`; formula, denominator, and integrity checks belong under `validators/`.
5. Let `artifacts/run_all.py` derive `artifacts/llm_generation/timing_fit/timing_fit_trace.csv` from the selected JSONL trace. Pass `--timing-input` only for an intentional external timing CSV override.
6. Read the experiment's `README.md` first — it documents exactly how that experiment computes its metric (TTFT, effective tool latency, generation time, cache-hit ratio, etc.). The shared definitions are collected in `artifacts/utils/README.md`.

## Quick Summary

```bash
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.public.jsonl
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.public.jsonl --json
```

Reports separate merged/Claude/Codex sections with scope, token, generation-timing, and tool totals. Treat normalized decoding speed as a trace-level proxy from input-ready event to last model-output event, not a serving-engine decode timer. The post-reasoning TPOT estimate and TTFT residual are computed only for rows with exact reasoning-token counts; provider sections without that accounting report these values as `null`.

## Run The Artifact Suite

Use the artifact dispatcher when running all artifact experiments, a category, or scripts with nonstandard input wiring (`--input` instead of `-i`, module-level `INPUT`, stdout capture, or timing CSVs).

```bash
uv run python artifacts/run_all.py --list
uv run python artifacts/run_all.py --input trace/llm_round_trace.public.jsonl
uv run python artifacts/run_all.py --only tool_calls
uv run python artifacts/run_all.py --only prefix_cache/cache_hit_ratio
```

The artifact dispatcher defaults to 16 concurrent jobs. Use `--jobs 1` for serial runs, `--dry-run` to inspect commands, and `--stop-on-fail` when failure should stop launching new experiments. For a full analysis run after sanitization, use:

```bash
uv run python artifacts/run_all.py \
  --input trace/llm_round_trace.public.jsonl \
  --log-dir /tmp/coding_trace_artifact_runlogs
```

## Run Validators

Use the validator dispatcher for checks that validate trace integrity, metric denominator
coverage, and formula assumptions. These scripts write reports under `validators/`, not
under `artifacts/`.

```bash
uv run python validators/run_all.py --list
uv run python validators/run_all.py --input trace/llm_round_trace.public.jsonl
uv run python validators/run_all.py --only human_in_the_loop
uv run python validators/run_all.py --only trace_facts/tool_duplicate_audit
```

For a full validator run paired with the artifact suite, use:

```bash
uv run python validators/run_all.py \
  --input trace/llm_round_trace.public.jsonl \
  --log-dir /tmp/coding_trace_validator_runlogs
```

## Running An Experiment

Most experiments can also be run directly; outputs land next to the script.

```bash
# token input composition
uv run python artifacts/llm_generation/prefix_append_distribution/plot.py -i trace/llm_round_trace.public.jsonl
# tool latency distribution, counts, time-by-kind
uv run python artifacts/tool_calls/tool_latency_distribution/plot.py -i trace/llm_round_trace.public.jsonl
uv run python artifacts/tool_calls/tool_call_counts/plot.py -i trace/llm_round_trace.public.jsonl
# generation-time and human-wait CDFs
uv run python artifacts/llm_generation/generation_time_cdf/plot.py -i trace/llm_round_trace.public.jsonl
uv run python artifacts/human_in_the_loop/human_input_wait/plot.py -i trace/llm_round_trace.public.jsonl
```

Scripts using `artifacts/utils/trace_loader.py` share these options:
`-i/--input`, `-o/--output-dir`, `--group-by {provider,model,provider_model}`,
`--sample-size`, `--pair-sample-size`, `--per-tool-sample-size`, `--max-groups`,
`--top-tools`, `--min-tool-calls-for-plot` (collapse rare tool names into `Other` in
PNGs; CSV summaries keep full detail), `--seed` (deterministic, default 42), and
`--progress-every`.

Some experiments intentionally use different flags: `adjusted_prefix_append`,
`output_append_assignment`, and `cache_replay` use `--input`; artifact scripts with a
module-level `INPUT` are easiest to run with `artifacts/run_all.py`; `csv_export`
requires both `-i` and `-o`; and `overview_summary` prints text or `--json` to stdout.

## Timing Fit Analyses

The timing-fit family reads a long-form timing-segment CSV, not the normalized JSONL trace.
The artifact dispatcher builds `artifacts/llm_generation/timing_fit/timing_fit_trace.csv`
automatically from `--input` before running timing experiments, including when the user
requests only a downstream timing experiment. Build it directly only when running timing
scripts by hand:

```bash
uv run python artifacts/llm_generation/timing_fit/collect_timing_fit_trace.py \
  -i trace/llm_round_trace.public.jsonl \
  -o artifacts/llm_generation/timing_fit/timing_fit_trace.csv
```

Then run the timing experiments:

```bash
uv run python artifacts/llm_generation/append_vs_prefix_latency/analyze.py
uv run python artifacts/llm_generation/timing_fit/fit_timing_trace.py
uv run python artifacts/llm_generation/timing_feature_ambiguity/analyze.py
uv run python artifacts/llm_generation/timing_feature_ambiguity/build_summary.py
```

Do not treat `scripts/` as the owner for this CSV. It is a generated artifact local to
`artifacts/llm_generation/timing_fit/` and should be consumed from there.

## Self-Contained PNGs

As the final step of every plotting experiment, the PNGs embed their README, the source
CSV data, and the plotting code as compressed PNG text chunks (the CSVs are still written
to disk normally). Inspect or unpack any figure:

```bash
python artifacts/utils/png_sidecar.py list    <figure>.png
python artifacts/utils/png_sidecar.py extract <figure>.png -o ./unpacked
```

## CSV Export

```bash
uv run python artifacts/trace_facts/csv_export/convert.py \
  -i trace/llm_round_trace.public.jsonl \
  -o artifacts/trace_facts/csv_export/coding_trace.csv
```

Writes `id,input_len,output_len,arrival_time,round_idx,tool_wait_after_ms,prefix_len`. Maps `newly_append_tokens` to `input_len`, `prefix_tokens` to `prefix_len`, and uses `tool_wall_latency_ms` for `tool_wait_after_ms` by default. Use `--tool-latency-source internal` only when the downstream consumer should model tool-runner-reported duration rather than client-observed wall wait.

## Interpretation Guidance

- `prefix_tokens` approximates prompt-cache hit size; `newly_append_tokens` approximates uncached prompt-side work.
- Tool latency uses `tool_internal_latency_ms` when available, then falls back to `tool_wall_latency_ms`.
- CSV export uses wall latency by default because it models client-observed tool wait between LLM rounds.
- Missing or nonpositive latency is tracked separately; do not silently treat it as zero.
- Sampling is deterministic by default with `--seed 42`.
- `bad_json` counts malformed or non-object JSONL lines skipped during loading.

## Reporting Guidance

When summarizing analysis results:

- Lead with the input path, row count, provider split, and whether the input was sanitized.
- Distinguish token totals from token distributions.
- Mention when quantiles are sample-based and include sample settings if they affect interpretation.
- Avoid claiming causality from trace timing alone. Use phrases such as "trace-level proxy" for normalized decoding speed and tool latency.
- Cite the experiment's `README.md` for the exact metric definition behind any number.
