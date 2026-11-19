# session_internal_counts

**How much work does one coding session, and one request, contain?**

Computes the count distributions behind `tab:session_internal_counts`
(`src/04_SessionContext.tex`): requests, user-/tool-initiated steps, and tool calls per
session; tool-initiated steps and tool calls per request; and tool calls per step — each as
avg / p25 / p50 / p90 / p99.

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/session/session_internal_counts/analyze.py

# the pinned public trace
uv run python artifacts/session/session_internal_counts/analyze.py --db trace/syfi_coding_trace.duckdb

# a prebuilt DB, into a chosen dir
uv run python artifacts/session/session_internal_counts/analyze.py --db "$TMPDIR/trace.duckdb" -o "$TMPDIR/out"
```

## Outputs

- `session_internal_counts.tex` — the merged three-line (booktabs) table, ready to `\input` or
  paste into `src/04_SessionContext.tex`.
- `session_internal_counts.md` — GFM Markdown mirror of the table, rendered on the web detail page.
- `headline.json` — the few headline numbers for the Overview gallery card.
- stdout — the full merged + per-provider (Claude / Codex) breakdown.

No figures.


## Request ownership and current results

Request groups now use `artifacts/utils/request_timing.py`, shared with timing and cost. Explicit native turn identity takes priority; legacy user-triggered groups remain countable without verified duration. Counts do not drop a group because timestamps are missing, compressed, or completion is unavailable. This separates workload measurement from response-time coverage. Rebuild the generated table on the intended capture; no undated historical averages are authoritative.

## Request ownership coverage

The shared ownership audit reports recorded rounds that have no resolvable request owner, including their output tokens, tool-call count, known attributed tool work, and native lifecycle markers. Session and step totals retain these rows; request statistics exclude them. Their request identity and completion are not guessed. Count/cost tables include this coverage, and response-time/paper-macro outputs also write `request_ownership_coverage.md`. Session identity is always the pair `(provider, session_id)`.
