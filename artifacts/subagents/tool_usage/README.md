# Tool usage in linked subagent families

Compare tool calls, errors, and tool time in a main session and its subagents. Only mains with linked subagents are included.

**Tool time is summed call time, not elapsed runtime:** calls may overlap. Unknown outcomes are not successes, and **N/A** means unavailable, not zero.

## Population and definitions

The shared `subagent_cohorts.COHORT_SQL` selects positively known delegating main
sessions with resolved self-roots and their resolved descendants. A main is counted
once regardless of its number of children. Descendants include deeper descendants,
not just direct children. Families with unknown-origin roots, unresolved links, and mains
without linked descendants are excluded. These are matched families, not all
sessions and not a controlled comparison of equivalent tasks.

Main origin comes from provider source or file layout, not incoming links. A linked descendant may itself have unknown origin.

Sessions join rounds on **provider and session ID**. Calls join rounds on
**round_pk**. Calls are not expanded by child count. Tool names are compared exactly
as stored in the sanitized input: no case folding, MCP merging, or inferred aliases.
Null and whitespace-only names form an unnamed-call bucket for call and time
accounting, but never count as a distinct named tool. Other names are not trimmed.

The figure reuses the overall tool sections' display policy: `mcp_*` names merge as
`mcp`, and the time column trims surrounding whitespace. This changes display
categories, not call totals or exact-name table/CSV rows. The shared raw-call
aggregate expressions and effective-latency precedence match the overall sections.

- **Calls:** all recorded calls for a provider, role, and exact tool name.
- **Call share:** tool calls / all calls in the same provider and role, including
  unnamed calls. A role with no calls has an unavailable share, not zero percent.
- **Error rate:** known errors divided by known errors plus known successes for that tool, provider, and role.
  Unknown outcomes are excluded from this denominator, never treated as success.
- **Known-outcome coverage:** known errors plus known successes, divided by all calls; unknown counts appear
  alongside it. No known outcomes means an unavailable error rate.
- **Tool diversity:** distinct nonblank names per session. Sessions without calls
  or with only unnamed calls contribute a genuine zero. The distribution has provider rows and separate Avg/P25/P50/P90/P99 columns using the shared `summarize` method; session counts appear in the preceding totals. An empty
  cohort has count zero and unavailable statistics, not measured zero diversity.
- **Effective tool time:** the existing `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`
  selects internal latency when present, otherwise wall latency. A zero or negative
  internal value does **not** fall back to wall time. Only positive effective values
  are summed. Positive, missing, and nonpositive call counts are reported separately.
  With no positive observations the sum is unavailable. Time share divides a tool's
  positive sum by the positive sum for all tools in its provider and role; a tool
  with no positive observation contributes zero share only when that denominator
  exists. Overlapping calls are added, so this is **not elapsed runtime** or savings.

## Figure and full tables

The figure uses the existing shared Matplotlib style and save helper. Claude and
Codex occupy separate rows; blue means main and green means descendant in both.
The two columns use different top-12 lists: call panels rank by combined calls,
while time panels rank by summed positive effective latency across providers and
roles. Each column shares its list and scale between provider rows; tools need not
occur in every group. Ties use display names. An unnamed-call category can appear.
Long display labels are shortened; exact names remain in tables/CSV. Time panels
use a common hours scale derived only from the displayed categories, so a hidden
tool cannot stretch the visible axis.

The figure shows omitted shares for each provider and role; the Markdown reports
omitted calls and time separately, including category counts and shares. Neither
the full tool tables nor the CSV drops the tail. The call mix is
not renormalized to the displayed names. Empty provider panels say “No linked
data”; linked roles without calls and roles without positive latency are labeled
separately. Missing latency is not drawn as measured zero.

Full tool tables use one measure per row, with Claude and Codex columns, under each exact tool name. Counts, shares, outcomes, and time are separate rows, not combined cells.

Exact-name comparisons do not imply equivalent tool behavior between providers.
Different task mixes, tool availability, family sizes, outcome recording, and
latency coverage limit comparisons. Counts and time are pooled within each role,
not equally weighted per family or session. No observed findings are assumed here.

## Commands and outputs

From the repository root, using an approved sanitized database:

```sh
uv run --no-sync python artifacts/subagents/tool_usage/plot.py --db /path/to/approved.duckdb
uv run --no-sync python artifacts/subagents/tool_usage/plot.py --db /path/to/approved.duckdb --output-dir /path/to/output
```

The house `--input /path/to/normalized.jsonl` option is also supported. The default
output directory is this experiment's directory. The script writes only:

- `tool_usage.png` — provider rows with main/descendant call mix and additive time.
- `tool_usage.md` — role outcome coverage, session diversity, omitted tail, and all
  tool rows for both providers.
- `tool_usage.csv` — aggregate `role` and `tool` rows, with no session identifiers.
  `diversity_*` fields apply only to role rows; blank tool names identify unnamed
  call buckets on tool rows. Empty numeric cells mean unavailable/not applicable.

The PNG embeds this README, plotting/shared utility source, and **only**
`tool_usage.csv`, using the existing PNG sidecar helper. It does not attach raw
traces, session rows, or unrelated CSV files from the output directory. Inputs and
tool names must already be approved for publication; this script is not a sanitizer.

Assigned synthetic validation (no corpus required):

```sh
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_tools.py
```
