# Matched main and subagent workload

Compare recorded rounds and tool calls across all sessions, all positively identified mains, mains outside matched families, delegating mains, and their linked subagents. The detailed matched comparison includes only mains with linked subagents. Each session counts once. A **round** is not a user request; a main's count never adds its children's rounds.

**Avg** is the average; P25/P50/P90/P99 are percentiles, with P50 the median. **N/A** means unavailable, not zero. The tables describe recorded activity, not everything that ran.

## Cohorts and denominators

The shared `artifacts/utils/subagent_cohorts.py::COHORT_SQL` returns `provider, session_id, root_session_id, role`:

- **Main (`F`):** positive known main origin (`claude_top_level_layout` or `codex_top_level_source`), resolved self-root (`root_status IN ('resolved_explicit', 'resolved_chain')`), and at least one observed resolved non-self descendant.
- **Descendant (`D`):** a session whose root status is `resolved_explicit` or `resolved_chain`, whose resolved root differs from itself, and whose root is one of those mains. A descendant need not have a known origin itself. Unknown-origin root-anchor families and unrelated mains are excluded.

Each delegating main contributes **once**, no matter how many descendants it has. Descendant distributions are session-weighted over all linked descendants, not equally weighted family averages or paired per-family differences. Both roles are reported within each provider; no merged-only headline substitutes for the provider comparisons.

The population comparison reports sample size, mean and median rounds and tool calls for each baseline, using each session's own retained rows. The overall `session/session_internal_counts` section includes all recorded sessions, including subagents, whereas the detailed tables here use `F` and `D`. Mains outside matched families have no eligible linked descendant in this capture, which does not prove no real delegation. These population rows overlap and must not be added together.

Linked worker, guardian and unknown-kind rows partition `D` using recorded `rounds.session_kind`, summarized by provider/session. Missing/legacy evidence and conflicting known kinds remain unknown; behavior and session length never establish kind. These rows explain source composition without changing family membership.

One recorded model round is one retained LLM row, not a user request.

| Metric | Observations / denominator |
| :-- | :-- |
| Retained rounds/session | One count of retained `rounds` rows per cohort session. |
| Tool calls/session | One count of observed `tool_calls` rows per cohort session; zero is included. |
| Zero-tool share | Cohort sessions with zero observed tool rows / all sessions in that same provider and role. |
| One-observed-round share | Cohort sessions with exactly one retained round / all sessions in that same provider and role. |
| Tool calls/round | One count per retained round in the cohort, including zero-tool rounds. The mean equals total tools / total rounds, **not** the mean of session-level ratios. |

The overall count section uses the same tool-row and model-round units. Its **per-request** figures instead use reconstructed request groups, a different denominator. Several tool calls in one round are valid. The report shows zero-tool and multi-tool round counts explicitly.

For each matched role the report also shows the largest session's round count and share, plus the mean after excluding that one largest session. This is an outlier sensitivity diagnostic, not a replacement sample or a reason to discard data. A persistent main can span many requests and days; its length is not comparable to a single delegated task. Use mean, median, sample size and family composition together; session length alone does not measure efficiency.

`round_pk` is the retained ingestion identity. Duplicate `round_id`, `trace_key`, and `(session_id, round_index)` values remain separate observations. Tools are aggregated by `round_pk` **before** joining rounds to avoid multiplying round counts by tool fanout. No request/turn state machine or completion filter is used. Session population is the observed valid `(provider, session_id)` identity from `sessions`.

Each distribution has provider rows and separate Avg/P25/P50/P90/P99 columns, using NumPy's linearly interpolated percentiles. Observation counts are the session or round totals directly above, not repeated in each distribution. Empty distributions or zero-denominator shares are **N/A**; an observed tool count of zero stays zero. The tables show observed sessions, included mains and subagents, sessions outside these groups, excluded resolved descendants, and unavailable linkage. Included and excluded descendants sum to all resolved non-self descendants. These distinguish no cohort evidence from zero observed workload. Unscoped traces and legacy DBs without `sessions` work through the shared DB interface but have no matched cohorts while linkage is unavailable.

## Limitations

The measurements describe **retained capture**, not complete real execution. Zero tool rows means no calls observed in that capture, not proof no tools were used. One observed round is not success, completion, a single user request, or evidence that a task was simple. Missing descendants or truncated sessions can bias workload comparisons. Unknown origins, unresolved links, and provider-specific capture conventions affect who enters the cohort; the separate adoption experiment reports eligibility and classification denominators in detail.

No raw session identifiers/content, token counts, or inferred request counts are exported. This README reports definitions, not invented real-corpus findings.

## Run

From the repository root, using an existing DB:

```bash
uv run --no-sync python artifacts/subagents/workload/analyze.py --db trace/syfi_coding_trace.duckdb
```

Or use the shared normalized-JSONL override and choose an output directory:

```bash
uv run --no-sync python artifacts/subagents/workload/analyze.py --input path/to/normalized.jsonl --output-dir /tmp/tracelab-workload
```

The shared CLI opens the DB read-only; the connection is closed after collection. `--input` uses the existing temporary materialization cache, without changing the source DB. The script emits **only** `workload.md` and `headline.json` in `--output-dir` (default: this directory). Headlines use the house list of `{label, value}` objects and label each provider's median recorded rounds per delegating main or linked subagent. A delegating main here has at least one observed linked subagent. Markdown and stdout show both providers, population baselines, denominators, and coverage.

Focused synthetic validation (including output smoke checks for both scripts):

```bash
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_counts.py
```
