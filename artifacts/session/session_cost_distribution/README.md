# session_cost_distribution

**What does a coding session / request / step cost, and where does the money go?**

Computes the USD-cost distribution behind `tab:cost_distribution` (`src/04_SessionContext.tex`).
For each granularity (per session, per request, per step) and each billed category, the paper
table reports the **cost** as avg / p50 / p90 / p99 plus the category's share of total spend
(the script also prints the underlying token distributions, incl. p25, to stdout):

- **Append tokens** — `newly_append_tokens`, billed at the fresh-input/cache-write rates.
- **Prefix tokens** — `prefix_tokens`, billed at the cache-read rate.
- **Output tokens** — `output_tokens` (reasoning included), billed at the output rate.
- **Total** — the sum of the three.

## Definitions

- **Cost** uses the single-source price table `artifacts/utils/pricing.json` via
  `web_analytics/pricing.py` (`price_for` → per-model exact/family resolve; `round_cost` → append
  at input/cache-write rates, prefix at cache-read rate, output at output rate — the same billing
  the web dashboard uses). Rounds whose model has no price are *unpriced* and excluded. The
  analysis prints exact priced and unpriced coverage for the selected trace.
- **Request** — one user turn, via the same turn state machine as
  `human_in_the_loop/user_turn_decomposition`. **Step** — one LLM round. **Session** — one
  `session_id`.

## Running it

```bash
uv run python artifacts/session/session_cost_distribution/analyze.py --db trace/syfi_coding_trace.duckdb
uv run python artifacts/session/session_cost_distribution/analyze.py            # default merged trace
```

## Outputs

- `session_cost_distribution.tex` — the merged single-column cost table (Avg / P50 / P90 / P99
  + % cost) for the paper.
- `session_cost_distribution.md` — GFM Markdown mirror of the table, rendered on the web detail page.
- `headline.json` — the few headline numbers for the Overview gallery card.
- stdout — merged + per-provider (Claude / Codex) token and cost percentiles, plus the
  append / prefix / output cost composition.

## Headline numbers (public trace, list prices as of 2026-07)

- **Cost composition: prefix/cached 58.6%, append/new-input 30.5%, output 11.0%.** Cached input
  dominates spend despite the ~10× cache-read discount, purely on volume.
- Avg cost: **$11.8 / session**, **$1.10 / request**, **$0.14 / step**; medians are far lower
  ($0.79 / $0.34 / $0.09) with a heavy session tail (p99 = $209).

No figures.

## SyFI result analysis

### session_cost_distribution.md

For a coding agent the bill is dominated by re-reading context, not by generation (the paper's
`tab:cost_distribution`). Cached **prefix tokens are 58.6%** of total spend even though they are
billed at roughly a tenth of the fresh-input rate — pure volume, since the accumulating context is
replayed on every step — against **30.5%** for append/new-input and only **11.0%** for output. Output
is cheap in aggregate despite its high per-token price because each step emits so few tokens. The
absolute costs are modest at the median ($0.79/session, $0.34/request, $0.09/step) but carry a heavy
tail: the average session is $11.8 and p99 reaches **$209**, a few very long sessions driving most of
the spend. This inverts the usual intuition that generation is the expensive part.

## Shared request ownership update

Request groups use `artifacts/utils/request_timing.py`. Native turn identity owns rows; legacy user-triggered groups remain countable for workload and attributed cost. Missing completion or nonpositive/contradictory timestamps no longer erase those counts. Only native completed lifecycles with consistent timestamps yield response duration. This can increase counts relative to historical tables that dropped groups solely for missing/nonpositive duration. Rebuild generated outputs for the intended capture.

## Request ownership coverage

The shared ownership audit reports recorded rounds that have no resolvable request owner, including their output tokens, tool-call count, known attributed tool work, and native lifecycle markers. Session and step totals retain these rows; request statistics exclude them. Their request identity and completion are not guessed. Count/cost tables include this coverage, and response-time/paper-macro outputs also write `request_ownership_coverage.md`. Session identity is always the pair `(provider, session_id)`.
