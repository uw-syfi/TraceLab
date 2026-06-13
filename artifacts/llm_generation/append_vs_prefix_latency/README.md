# append_vs_prefix_latency

**Question.** Are append-heavy agent steps actually slower than otherwise-matched
prefix-heavy steps? Not just "append-heavy rows are slower on average", but: after
matching on provider, model, segment kind, total input length, and output length, do
append-heavy rows separate cleanly from prefix-heavy ones?

## Input

`../timing_fit/timing_fit_trace.csv` (override with `-i`) — the long-form
timing-segment CSV produced by `../timing_fit/collect_timing_fit_trace.py`. **Not** the
JSONL trace. `artifacts/run_all.py` builds it automatically from `--input` before running
this experiment.

## Method / key assumptions

- Rows are bucketed by `(provider, model, segment_kind, total-token bin,
  output-token bin)`. Within each bucket, **append-heavy** rows (append share
  `≥ --append-heavy-share`) are compared against **prefix-heavy** rows (append share
  `≤ --prefix-heavy-max-append-share`).
- Reports two things:
  - **effect size** — how often an append-heavy row is slower than a matched
    prefix-heavy row (`pair_weighted_append_slower_probability`);
  - **separation quality** — whether a duration threshold distinguishes the two
    classes after normalizing each row by its bucket's prefix-heavy median latency
    (`global_normalized_best_balanced_accuracy`).
- Durations are trimmed per group (`--trim-quantile`, default 0.99) and filtered to
  `[--min-duration-ms, --max-duration-ms]` to drop implausible spans.

## How to run

Recommended dispatcher path:

```bash
uv run python artifacts/run_all.py \
  --only llm_generation/append_vs_prefix_latency \
  --input trace/llm_round_trace.public.jsonl
```

The dispatcher builds `../timing_fit/timing_fit_trace.csv` from `--input` first. Manual
direct runs assume that CSV already exists:

```bash
uv run python artifacts/llm_generation/append_vs_prefix_latency/analyze.py
```

## Outputs (written here)

- `append_vs_prefix_latency.json` / `.md` — verdict + summary.
- `append_vs_prefix_matched_buckets.csv`, `append_vs_prefix_normalized_rows.csv`
- `append_vs_prefix_bucket_effects.png`, `append_vs_prefix_normalized_overlap.png`

## Self-contained PNGs

Each PNG embeds this README, the CSVs, and `analyze.py`. Unpack with
`python artifacts/utils/png_sidecar.py extract <png>`.
