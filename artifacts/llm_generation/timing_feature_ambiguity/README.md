# timing_feature_ambiguity

**Question.** How much round-to-round timing variation can token features
*not* explain? I.e. what is the irreducible error floor for any timing model built
only from `(provider, model, segment kind, cached/prefix tokens, appended tokens,
output tokens)`?

## Input

`../timing_fit/timing_fit_trace.csv` (override with `-i`) from
`../timing_fit/collect_timing_fit_trace.py`. `artifacts/run_all.py` builds it
automatically from `--input` before running this experiment. The fit-metrics comparison
reads `../timing_fit/timing_fit_metrics.csv` by default (`--fit-metrics`).

## Method / key assumptions

- **Exact-duplicate pure error.** If two rows share the *exact* feature tuple but
  have different durations, no deterministic model on those features can fit both —
  this is a hard lower bound on achievable error.
- **Local neighborhood spread.** Rows are grouped into narrow log-token buckets;
  the latency spread inside each bucket estimates residual variation among
  "similar" points.
- Output Markdown is written for human review, not just machine consumption.

## How to run

Recommended dispatcher path:

```bash
uv run python artifacts/run_all.py \
  --only llm_generation/timing_feature_ambiguity \
  --input trace/llm_round_trace.public.jsonl
```

The dispatcher builds the timing CSV, runs `timing_fit` first so
`../timing_fit/timing_fit_metrics.csv` is available, then runs this experiment and its
compact summary. Manual direct runs assume those upstream files already exist:

```bash
uv run python artifacts/llm_generation/timing_feature_ambiguity/analyze.py
# optional compact rollup of the irreducible-error comparison (run after analyze.py):
uv run python artifacts/llm_generation/timing_feature_ambiguity/build_summary.py
```

## Outputs (written here)

- `timing_feature_ambiguity.json` / `.md`, `timing_feature_ambiguity_top.csv`
- `timing_irreducible_error.json` / `.md`, `timing_irreducible_error_fit_comparison.csv`
- `timing_local_neighborhood_top.csv`, `timing_smooth_relative_error.md`,
  `timing_fit_metrics.csv`
- `build_summary.py` adds `timing_fit_compact_summary.md` (+ a `result_analysis.md` log).

## Notes

This experiment produces only Markdown/CSV/JSON (no figures), so there are no
self-contained PNGs here.
