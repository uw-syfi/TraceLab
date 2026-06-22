# token_length_distribution

**Per LLM step, how large are the inputs and outputs — and how do Claude and Codex differ?**

## Experiment overview

This experiment produces the single combined paper table shared by the *Input length distribution*
and *Output length distribution* subsections (`tab:token_length_distribution` in
`src/05_LLMGeneration.tex`). For each provider (Claude, Codex), over **all LLM steps** (rounds), it
reports the avg / p25 / p50 / p90 / p99 of three per-step token counts:

- **Prefix tokens** — `prefix_tokens`, the replayed accumulated context.
- **Append tokens** — `newly_append_tokens`, the freshly added uncached input.
- **Output tokens** — `output_tokens`, generated tokens with reasoning included.

The prefix/append split is the same decomposition as
`llm_generation/prefix_append_distribution`, and the output column is the same metric as
`llm_generation/output_tokens`; this experiment exists only to emit the combined per-provider
`.tex` table. The other two experiments keep their figures and CDFs.

Method and assumptions:

- **Exact, not sampled.** DuckDB keeps every row, so the percentiles and means run over the full
  set of valid rounds (no reservoir sampling).
- **Per-column filtering.** Each token column is independently restricted to non-null,
  non-negative values (`column IS NOT NULL AND column >= 0`), matching the two source experiments.
- **Per step.** The unit is one LLM round; there is no session/request aggregation here.

## Running it

```bash
# default merged trace, output next to this README
uv run python artifacts/llm_generation/token_length_distribution/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/llm_generation/token_length_distribution/analyze.py -i trace/sample.jsonl

# a prebuilt DB, into a chosen dir
uv run python artifacts/llm_generation/token_length_distribution/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs (written to `-o`, default this folder)

- `token_length_distribution.tex` — the combined per-provider table; a copy with a provenance
  header lives at `figure-tex/tab_token_length_distribution.tex` in the paper repo.

The per-provider stats are also printed to stdout.
