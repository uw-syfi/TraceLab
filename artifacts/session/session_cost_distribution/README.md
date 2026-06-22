# session_cost_distribution

**What does a coding session / request / step cost, and where does the money go?**

Computes the USD-cost distribution behind `tab:cost_distribution` (`src/04_SessionContext.tex`).
For each granularity (per session, per request, per step) and each billed category, the paper
table reports the **cost** as avg / p50 / p90 / p99 plus the category's share of total spend
(the script also prints the underlying token distributions, incl. p25, to stdout):

- **Append tokens** — `newly_append_tokens`, billed at the fresh-input rate.
- **Prefix tokens** — `prefix_tokens`, billed at the cache-read rate.
- **Output tokens** — `output_tokens` (reasoning included), billed at the output rate.
- **Total** — the sum of the three.

## Definitions (reused so the numbers reconcile)

- **Cost** uses the single-source price table `artifacts/utils/pricing.json` via
  `web_analytics/pricing.py` (`price_for` → per-model exact/family resolve; `round_cost` → append
  at input rate, prefix at cache-read rate, output at output rate — the same billing the web
  dashboard uses). Rounds whose model has no price are *unpriced* and excluded; 99.1% of rounds
  are priced (the rest are `codex:codex-auto-review` / null-model rows). Coverage is printed.
- **Request** — one user turn, via the same turn state machine as
  `human_in_the_loop/user_turn_decomposition` (39,202 turns, matching `user_turn_response_time`
  and `session_internal_counts`). **Step** — one LLM round. **Session** — one `session_id`.

## Running it

```bash
uv run python artifacts/session/session_cost_distribution/analyze.py -i trace/syfi_coding_trace.jsonl
uv run python artifacts/session/session_cost_distribution/analyze.py            # default merged trace
```

## Outputs

- `session_cost_distribution.tex` — the merged single-column cost table (Avg / P50 / P90 / P99
  + % cost) for the paper.
- stdout — merged + per-provider (Claude / Codex) token and cost percentiles, plus the
  append / prefix / output cost composition.

## Headline numbers (public trace, list prices as of 2026-06)

- **Cost composition: prefix/cached 61.7%, append/new-input 26.7%, output 11.6%.** Cached input
  dominates spend despite the ~10× cache-read discount, purely on volume.
- Avg cost: **$9.36 / session**, **$0.97 / request**, **$0.11 / step**; medians are far lower
  ($0.59 / $0.33 / $0.074) with a heavy session tail (p99 = $172).

No figures.
