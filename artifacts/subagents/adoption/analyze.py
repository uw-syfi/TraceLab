#!/usr/bin/env python3
"""Observed subagent adoption, family shape, and lineage coverage by provider."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, session_kinds_sql, summarize  # noqa: E402

PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))


def collect(con) -> dict:
    """Count observed identities, then summarize only positively identified matched families."""
    count_keys = (
        "observed_sessions", "main_origins", "subagent_origins", "unknown_origins",
        "eligible_mains", "unavailable_lineage", "resolved_descendants",
    )
    out: dict = {provider: dict.fromkeys(count_keys, 0) for provider, _ in PROVIDERS}
    for data in out.values():
        data["linked_kinds"] = dict.fromkeys(("worker", "guardian", "unknown"), 0)
    for provider, *counts in con.execute("""
        SELECT provider, count(*),
               count(*) FILTER (WHERE session_origin IN ('claude_top_level_layout', 'codex_top_level_source')),
               count(*) FILTER (WHERE session_origin IN ('claude_subagent_layout', 'codex_subagent_source')),
               count(*) FILTER (WHERE session_origin = 'unknown'),
               count(*) FILTER (WHERE session_origin IN ('claude_top_level_layout', 'codex_top_level_source')
                   AND root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id = session_id),
               count(*) FILTER (WHERE root_status = 'unavailable'),
               count(*) FILTER (WHERE root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id <> session_id)
        FROM sessions GROUP BY provider
    """).fetchall():
        if provider in out:
            out[provider].update(zip(count_keys, counts))

    families = {}
    depths = {provider: [] for provider, _ in PROVIDERS}
    for provider, root, parent_status, parent_role, depth, kind in con.execute(f"""
        WITH cohort AS ({COHORT_SQL}), kinds AS ({session_kinds_sql(con)})
        SELECT c.provider, c.root_session_id, s.parent_status, p.role, s.depth, k.session_kind
        FROM cohort c
        JOIN sessions s ON s.provider = c.provider AND s.session_id = c.session_id
        LEFT JOIN kinds k ON k.provider = c.provider AND k.session_id = c.session_id
        LEFT JOIN cohort p ON p.provider = c.provider
                          AND p.session_id = s.observed_parent_session_id
                          AND p.root_session_id = c.root_session_id
        WHERE c.role = 'descendant'
    """).fetchall():
        out[provider]["linked_kinds"][kind if kind in ("worker", "guardian") else "unknown"] += 1
        family = families.setdefault((provider, root), {"descendants": 0, "direct": 0, "nested": 0})
        family["descendants"] += 1
        if parent_status == "resolved":
            if parent_role == "main":
                family["direct"] += 1
            elif parent_role == "descendant":
                family["nested"] += 1
        if depth is not None:
            depths[provider].append(depth)

    for provider, _ in PROVIDERS:
        data = out[provider]
        members = [family for (prov, _), family in families.items() if prov == provider]
        data["delegating_mains"] = len(members)
        data["matched_descendants"] = sum(f["descendants"] for f in members)
        data["excluded_resolved_descendants"] = data["resolved_descendants"] - data["matched_descendants"]
        data["known_parent_edges"] = sum(f["direct"] + f["nested"] for f in members)
        data["nested_families"] = sum(f["nested"] > 0 for f in members)
        data["flat_families"] = sum(f["direct"] == f["descendants"] for f in members)
        data["unknown_families"] = len(members) - data["nested_families"] - data["flat_families"]
        data["descendants_per_main"] = summarize([f["descendants"] for f in members])
        data["direct_children_per_main"] = summarize([f["direct"] for f in members])
        data["depth"] = summarize(depths[provider])
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def _share(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "N/A"


def render_md(data: dict) -> str:
    blocks = [
        "# Observed subagent adoption\n",
        "These counts describe observed capture, not complete real execution. A main session is "
        "identified by provider source or file layout. An eligible main is also confirmed as its own "
        "family root. Linked subagents are sessions traced to that main, including indirect descendants; "
        "their own origin may be unknown. A family contains one such main and its linked subagents. "
        "Unknown-origin roots are excluded. Missing links do not prove no delegation.\n",
        "## Sessions\n",
    ]
    populations, topology = [], []
    for provider, label in PROVIDERS:
        d = data[provider]
        populations.append([
            ["Observed sessions", d["observed_sessions"]],
            ["Main sessions", d["main_origins"]],
            ["Subagents (linked or not)", d["subagent_origins"]],
            ["Sessions with unknown origin", d["unknown_origins"]],
            ["Main sessions with confirmed roots", d["eligible_mains"]],
            ["Main sessions with subagents", d["delegating_mains"]],
            ["Subagents in these families", d["matched_descendants"]],
            ["Linked workers", d["linked_kinds"]["worker"]],
            ["Linked guardians", d["linked_kinds"]["guardian"]],
            ["Linked subagents with unknown kind", d["linked_kinds"]["unknown"]],
            ["Linked sessions outside these families", d["excluded_resolved_descendants"]],
            ["Sessions with unavailable family links", d["unavailable_lineage"]],
        ])
        topology.append([
            ["Nested families", d["nested_families"]],
            ["Flat families", d["flat_families"]],
            ["Families with unknown structure", d["unknown_families"]],
            ["Subagents with a known parent in the family", d["known_parent_edges"]],
        ])
    for heading, note, pair in (
        ("", "Origin comes from provider source or file layout, not incoming links. "
         "The headline percentage uses main sessions with confirmed roots. "
         "Worker and guardian are recorded source kinds, not classifications inferred from behavior. "
         "Unknown includes absent, legacy or conflicting kind evidence. These three linked-kind rows sum to linked subagents.", populations),
        ("Family structure", """These are synthetic examples, not reconstructions of actual data. Each family has **4 sessions: one main + three subagents**. Arrows mean recorded, validated **direct launch links**, not just family membership.

**Flat:** Main directly launched A, B and C. Every subagent's direct parent is known to be Main.

```text
Main
├─→ A
├─→ B
└─→ C
```

**Nested:** Main launched A and C; A launched B. One proven subagent-to-subagent launch is enough to establish nesting, even if other parent links are unknown.

```text
Main
├─→ A ─→ B
└─→ C
```

**Unknown structure:** Main, A, B and C belong to one family, but the direct launch links are insufficient to say flat or nested. The box shows membership only, with no parent arrows. Recorded rounds, tokens and tool calls can still be counted for these members.

```text
┌─ Family ──────┐
│ Main  A  B  C │
└───────────────┘
```""", topology),
    ):
        if heading:
            blocks.append(f"## {heading}\n")
        blocks.append(note + "\n")
        blocks.append(md_table.gfm_table(["Measure", "Claude", "Codex"], [
            [left[0], left[1], right[1]] for left, right in zip(*pair)
        ]))
    blocks += ["## Family size and depth\n",
               "Each main with subagents counts once. Depth counts only known distances from the main "
               "(main = 0, direct child = 1, grandchild = 2). "
               "Zero known children does not prove no children.\n",
               "Avg is the average; P25/P50/P90/P99 are percentiles, with P50 the median. "
               "N/A means unavailable, not zero.\n"]
    for key, heading in (("descendants_per_main", "Subagents per main session"),
                         ("direct_children_per_main", "Known direct children per main session"),
                         ("depth", "Subagent depth")):
        headers = ["Provider"] + (["Count"] if key == "depth" else []) + ["Avg", "P25", "P50", "P90", "P99"]
        rows = []
        for provider, label in PROVIDERS:
            s = data[provider][key]
            rows.append([label] + ([f"{s['n']:,}"] if key == "depth" else [])
                        + [_number(s[stat]) for stat in ("avg", "p25", "p50", "p90", "p99")])
        blocks += [f"### {heading}\n", md_table.gfm_table(headers, rows)]
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} main sessions with subagents",
         "value": _share(data[provider]["delegating_mains"], data[provider]["eligible_mains"])}
        for provider, label in PROVIDERS
    ] + [
        {"label": f"{label} average subagents per main session", "value": _number(data[provider]["descendants_per_main"]["avg"])}
        for provider, label in PROVIDERS
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args(argv)
    con = trace_db.open_from_args(args)
    try:
        data = collect(con)
    finally:
        con.close()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "adoption.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
