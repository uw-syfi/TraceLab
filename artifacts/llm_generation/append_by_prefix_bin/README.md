# append_by_prefix_bin

**Given how large the cached prefix already is, how many *new* (uncached) tokens does a step
append?**

Populates `tab:append_by_prefix` (`src/05_LLMGeneration.tex`) — the quantitative companion to the
prefix-vs-append scatter (`fig:prefill_append_relationship`). For Claude and Codex, every agent
step is binned by its `prefix_tokens`, and within each bin we report the distribution of
`newly_append_tokens`: count, avg, p50, p90, p99.

Prefix bins are doubling, in 1024-token units: `<1k, 1-2k, 2-4k, 4-8k, 8-16k, 16-32k, 32-64k,
64-128k, 128-256k, >256k`. The `prefix_tokens` / `newly_append_tokens` accounting is the same one
used by `prefix_append_distribution` and `token_length_distribution`, so the numbers reconcile.

## Running it

```bash
uv run python artifacts/llm_generation/append_by_prefix_bin/analyze.py -i trace/syfi_coding_trace.jsonl
uv run python artifacts/llm_generation/append_by_prefix_bin/analyze.py        # default merged trace
```

## Outputs

- `append_by_prefix_bin.tex` — the Claude/Codex table for the paper (empty bins render as `--`).
- stdout — the same per-provider breakdown in plain text.

## Headline numbers (public trace)

- Append and prefix are **inversely** related. At `<1k` prefix (cold start — a cache miss or the
  first request) the median append is **78k** tokens for Claude and **124k** for Codex.
- Once the prefix exceeds ~32k (incremental tool-loop / user steps) the median append collapses
  to **well under 1k** for both providers, with only a modest p99 tail.
- Bins reveal provider structure: Claude's prefix jumps almost straight to large values (the
  `1-2k` bin is empty, `2-4k` has 2 steps) because its system prompt is large; Codex effectively
  caps near its 256k context (only 6 steps exceed it).

No figures.
