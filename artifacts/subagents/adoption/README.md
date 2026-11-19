# Subagent adoption and observed family structure

Compare how often main sessions have subagents, how many they have, and how they are linked. A **family** is one main session plus all subagents belonging to it, including subagents launched by other subagents. It is a group, not a separate agent type.

The **root** is the family's main session; the **immediate parent** is the session that directly launched a particular subagent. Knowing a subagent's root tells us which family it belongs to, but not necessarily who launched it.

A workflow script is not itself an agent session and adds nothing to the session count. Being in the same workflow does not prove a direct parent link.

These are recorded links, not a complete account of delegation. Missing links do not prove no subagents.

## Definitions and denominators

The unit of identity is an observed valid `(provider, session_id)` in `sessions`, not a round or a tool call. Scope validation and relationship resolution belong to `trace_db`; this script only reads their results. Origin, family links, parent links, and depth can be missing independently.

| Symbol | Definition |
| :-- | :-- |
| `M0` | Observed sessions with canonical origin `claude_top_level_layout` or `codex_top_level_source`, including those whose linkage is ineligible. |
| `C0` | Observed sessions with canonical origin `claude_subagent_layout` or `codex_subagent_source`, regardless of whether they link to an eligible main. |
| `U` | Observed sessions with canonical origin `unknown`. Origin classification is not inferred from incoming references. |
| `M` | Members of `M0` whose `root_status` is `resolved_explicit` or `resolved_chain` and whose resolved root is themselves. |
| `F` | Members of `M` with at least one observed resolved non-self descendant. |
| `D` | Observed sessions with `root_status IN ('resolved_explicit', 'resolved_chain')`, a non-self resolved root, and that root in `F`. Descendants need not have a known origin themselves. |

`artifacts/utils/subagent_cohorts.py::COHORT_SQL` supplies `F` mains once each plus `D` descendants, with columns `provider, session_id, root_session_id, role`. Root anchors of unknown origin are **not** mains. The session table shows included descendants and linked sessions outside these families; their sum is all resolved non-self descendants. In particular, Claude can have resolved membership to an unknown-origin anchor without that anchor being an eligible main; Codex may leave such membership unresolved. Unknown-origin and unavailable-linkage counts remain visible rather than being promoted into the cohort. The headline adoption share is `F/M`.

Each descendant-count and direct-child-count distribution has **one observation per F main**, not one per child. A known direct child has `parent_status = 'resolved'` and the observed parent equals its matched root. A zero in the direct-child distribution means **zero known direct children**, not proof of zero real children. The known-parent count includes resolved parent edges to another member of the same matched family; a resolved edge to an unclassified/outside-family ancestor does not qualify.

The linked worker/guardian/unknown-kind rows partition `D`. Kind comes from recorded `rounds.session_kind`, aggregated by provider/session, never from observed tool use or session length. Absent/legacy evidence and conflicting known kinds remain unknown. These rows reveal source composition without changing cohort eligibility or inventing task boundaries. Workload reports the corresponding counts, means and medians alongside all-session and all-main baselines.

- **Nested:** at least one validated descendant-to-descendant edge within the matched family, even if other edges or all depths are unknown.
- **Flat:** every matched descendant has a resolved direct edge to the root.
- **Unknown:** neither condition holds. Root-only membership cannot establish flatness.
- All three family counts are shown; they sum to `F`. Unknown structure is not counted as flat.
- Depth is the resolver's known distance from the root (root depth 0); only descendant depths enter the distribution. Its Count column shows how many of the `D` descendants have known depth. Root-only membership never supplies an invented depth.

Each distribution has provider rows and separate Avg/P25/P50/P90/P99 columns, using NumPy's linearly interpolated percentiles. Size uses the main-session count shown above; only depth needs a separate Count column. Empty distributions and a zero-denominator headline share are **N/A**; observed zeros remain zero. No request state machine or token measurement is used.

## Limitations

These are **observed capture** statistics, not complete real-execution adoption. An eligible main without an observed descendant may still have delegated outside the retained capture. Unknown origins, missing parents/roots, conflicts, invalid links, and cycles can reduce eligibility or topology coverage. Flat means flat among matched observed descendants only. Provider capture conventions can differ; compare the session, unknown-structure, and known-depth counts alongside the distributions.

Unscoped normalized traces retain origin classification when available, but linkage is unavailable, so `M` and `F` are empty and adoption is N/A. Legacy DBs without `sessions` use the shared read-only compatibility view: origins are unknown and linkage unavailable, never fabricated. No raw identifiers or content are exported. This README makes no claims about real-corpus findings.

## Run

From the repository root, using an existing DB:

```bash
uv run --no-sync python artifacts/subagents/adoption/analyze.py --db trace/syfi_coding_trace.duckdb
```

Or materialize a normalized JSONL override through the shared CLI and choose an output directory:

```bash
uv run --no-sync python artifacts/subagents/adoption/analyze.py --input path/to/normalized.jsonl --output-dir /tmp/tracelab-adoption
```

The shared CLI opens the DB read-only and closes it after collection. `--input` uses the shared temporary materialization cache; it does not update the source DB. The script emits **only** `adoption.md` and `headline.json` in `--output-dir` (default: this directory). Headlines use the house list of `{label, value}` objects. Labels identify each provider's share of confirmed-root mains with subagents and average subagents per main with subagents. Markdown uses measure/Claude/Codex tables for counts and provider rows for distributions; stdout mirrors it. There is no separate rates section.

Focused synthetic validation (also smoke-tests both scripts' `main(argv=None)` entry points):

```bash
uv run --no-sync --with pytest python -m pytest -q scripts/tests/test_subagent_counts.py
```
