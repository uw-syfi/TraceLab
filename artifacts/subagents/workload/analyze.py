#!/usr/bin/env python3
"""Retained rounds and tool calls in matched main/subagent sessions, by provider."""

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
ROLES = (("main", "Main sessions"), ("descendant", "Subagents"))
POPULATIONS = (
    ("all", "All recorded sessions"), ("all_main", "All recorded mains"),
    ("other_main", "Mains outside matched families"),
    ("main", "Delegating mains"), ("descendant", "Linked subagents"),
    ("worker", "Linked workers"), ("guardian", "Linked guardians"),
    ("unknown_kind", "Linked subagents with unknown kind"),
)


def collect(con) -> dict:
    """Use one row per retained round; aggregate tools before joining to avoid fanout."""
    out = {
        provider: {"observed_sessions": 0, "unavailable_lineage": 0, "resolved_descendants": 0,
                   "populations": {}, **{role: {"per_round": []} for role, _ in ROLES}}
        for provider, _ in PROVIDERS
    }
    for provider, observed, unavailable, resolved in con.execute("""
        SELECT provider, count(*), count(*) FILTER (WHERE root_status = 'unavailable'),
               count(*) FILTER (WHERE root_status IN ('resolved_explicit', 'resolved_chain')
                   AND resolved_root_session_id <> session_id)
        FROM sessions GROUP BY provider
    """).fetchall():
        if provider in out:
            out[provider].update(observed_sessions=observed, unavailable_lineage=unavailable,
                                 resolved_descendants=resolved)

    sessions = {}
    for provider, role, sid, tools in con.execute(f"""
        WITH cohort AS ({COHORT_SQL}),
        tools AS (SELECT round_pk, count(*) AS n FROM tool_calls GROUP BY round_pk)
        SELECT c.provider, c.role, c.session_id, coalesce(t.n, 0)
        FROM cohort c
        JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
        LEFT JOIN tools t ON t.round_pk = r.round_pk
    """).fetchall():
        counts = sessions.setdefault((provider, role, sid), [0, 0])
        counts[0] += 1
        counts[1] += tools
        out[provider][role]["per_round"].append(tools)

    populations = {(provider, key): [] for provider, _ in PROVIDERS for key, _ in POPULATIONS}
    for provider, sid, origin, role, kind, rounds, tools in con.execute(f"""
        WITH cohort AS ({COHORT_SQL}),
        kinds AS ({session_kinds_sql(con)}),
        tools AS (SELECT round_pk, count(*) AS n FROM tool_calls GROUP BY round_pk)
        SELECT s.provider, s.session_id, s.session_origin, c.role, coalesce(k.session_kind, 'unknown'),
               count(r.round_pk), coalesce(sum(t.n), 0)
        FROM sessions s
        LEFT JOIN cohort c ON c.provider = s.provider AND c.session_id = s.session_id
        LEFT JOIN kinds k ON k.provider = s.provider AND k.session_id = s.session_id
        LEFT JOIN rounds r ON r.provider = s.provider AND r.session_id = s.session_id
        LEFT JOIN tools t ON t.round_pk = r.round_pk
        GROUP BY 1, 2, 3, 4, 5
    """).fetchall():
        if provider not in out:
            continue
        keys = ["all"]
        if origin in ("claude_top_level_layout", "codex_top_level_source"):
            keys += ["all_main"]
            if role != "main":
                keys += ["other_main"]
        if role is not None:
            keys += [role]
        if role == "descendant":
            keys += [kind if kind in ("worker", "guardian") else "unknown_kind"]
        for key in keys:
            populations[provider, key].append((rounds, tools))

    for provider, _ in PROVIDERS:
        for key, _ in POPULATIONS:
            members = populations[provider, key]
            out[provider]["populations"][key] = {
                "sessions": len(members),
                "rounds_per_session": summarize([r for r, _ in members]),
                "tools_per_session": summarize([t for _, t in members]),
            }
        for role, _ in ROLES:
            data = out[provider][role]
            members = [counts for (prov, kind, _), counts in sessions.items() if (prov, kind) == (provider, role)]
            data["sessions"] = len(members)
            data["rounds"] = sum(rounds for rounds, _ in members)
            data["tools"] = sum(tools for _, tools in members)
            data["zero_tool_sessions"] = sum(tools == 0 for _, tools in members)
            data["one_round_sessions"] = sum(rounds == 1 for rounds, _ in members)
            data["rounds_per_session"] = summarize([rounds for rounds, _ in members])
            data["tools_per_session"] = summarize([tools for _, tools in members])
            data["zero_tool_rounds"] = sum(n == 0 for n in data["per_round"])
            data["multi_tool_rounds"] = sum(n > 1 for n in data["per_round"])
            data["largest_session_rounds"] = max((rounds for rounds, _ in members), default=None)
            data["rounds_without_largest_mean"] = (
                (data["rounds"] - data["largest_session_rounds"]) / (len(members) - 1)
                if len(members) > 1 else None
            )
            data["tools_per_round"] = summarize(data.pop("per_round"))
        out[provider]["excluded_resolved_descendants"] = (
            out[provider]["resolved_descendants"] - out[provider]["descendant"]["sessions"]
        )
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def _share(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:.1f}%" if denominator else "N/A"


def render_md(data: dict) -> str:
    blocks = [
        "# Observed subagent workload\n",
        "Retained capture, not complete real execution. Population baselines include all recorded sessions "
        "and all mains identified by provider source or file layout. Detailed matched tables include only "
        "confirmed self-root mains with linked subagents, counting each main once. Linked subagents include "
        "indirect descendants and may have unknown origins themselves. Unknown-origin root anchors and "
        "unrelated mains are excluded only from matched tables.\n",
        "One recorded model round is one retained LLM row, not a user request. "
        "Avg is the average. P25/P50/P90/P99 are the "
        "25th/50th/90th/99th percentiles; P50 is the median and about 90% of observations fall at or below P90. "
        "N/A means no eligible measurement, not zero workload.\n",
        "## Sessions and coverage\n",
    ]
    populations = []
    for provider, label in PROVIDERS:
        d = data[provider]
        populations.append([
            ["Observed sessions", d["observed_sessions"]],
            ["Sessions outside these groups", d["observed_sessions"] - d["main"]["sessions"] - d["descendant"]["sessions"]],
            ["Linked sessions outside these families", d["excluded_resolved_descendants"]],
            ["Sessions with unavailable family links", d["unavailable_lineage"]],
        ])
    blocks.append(md_table.gfm_table(["Sessions", "Claude", "Codex"], [
        [left[0], left[1], right[1]] for left, right in zip(*populations)
    ]))
    blocks += ["## Population comparison\n",
               "The overall session sections include all recorded sessions, including subagents. "
               "The matched comparison below uses only delegating mains and their linked subagents. "
               "Each row counts a session's own rounds and tool calls; a main never includes its children's rows. "
               "Mains outside matched families have no eligible linked child in this capture; this does not prove no delegation.\n",
               md_table.gfm_table(
                   ["Population", "Provider", "Sessions", "Avg rounds", "P50 rounds", "Avg tool calls", "P50 tool calls"],
                   [[label, provider_label, d["sessions"],
                     _number(d["rounds_per_session"]["avg"]), _number(d["rounds_per_session"]["p50"]),
                     _number(d["tools_per_session"]["avg"]), _number(d["tools_per_session"]["p50"])]
                    for key, label in POPULATIONS for provider, provider_label in PROVIDERS
                    for d in [data[provider]["populations"][key]]]),
               "A persistent main can cover many requests and days; one subagent session is not an equivalent task unit. "
               "The mean weights sessions equally and can be dominated by a long session. "
               "These differences do not measure task difficulty or efficiency. Worker and guardian are recorded source kinds, "
               "not inferred from length or tool use. Legacy, absent or conflicting kind evidence stays unknown. "
               "The three linked-kind rows partition linked subagents; the other population rows overlap.\n"]
    for role, role_label in ROLES:
        pair = []
        for provider, _ in PROVIDERS:
            s = data[provider][role]
            rows = [["Sessions", s["sessions"]], ["Rounds", s["rounds"]], ["Tool calls", s["tools"]]]
            rows += [
                [name, f"{s[key]:,} ({_share(s[key], s['sessions'])})"]
                for key, name in (("zero_tool_sessions", "Sessions with no tool calls"),
                                  ("one_round_sessions", "Sessions with one round"))
            ]
            rows += [
                ["Rounds with no tool calls", f"{s['zero_tool_rounds']:,} ({_share(s['zero_tool_rounds'], s['rounds'])})"],
                ["Rounds with multiple tool calls", f"{s['multi_tool_rounds']:,} ({_share(s['multi_tool_rounds'], s['rounds'])})"],
                ["Largest session's rounds", "N/A" if s["largest_session_rounds"] is None else
                 f"{s['largest_session_rounds']:,} ({_share(s['largest_session_rounds'], s['rounds'])})"],
                ["Avg rounds excluding largest session", _number(s["rounds_without_largest_mean"])],
            ]
            pair.append(rows)
        blocks += [f"## {role_label}\n", md_table.gfm_table(["Measure", "Claude", "Codex"], [
            [left[0], left[1], right[1]] for left, right in zip(*pair)
        ])]
        blocks.append("Tool-call distributions include sessions and rounds with zero calls. "
                      "Tools per round uses all retained model rounds, as in the overall counts section. "
                      "Per-request counts in that section use reconstructed request groups, a different denominator. "
                      "A round may emit several tool calls.\n")
        for key, heading in (("rounds_per_session", "Rounds per session"),
                             ("tools_per_session", "Tool calls per session"),
                             ("tools_per_round", "Tool calls per round")):
            blocks += [f"### {heading}\n", md_table.gfm_table(["Provider", "Avg", "P25", "P50", "P90", "P99"], [
                [label] + [_number(data[provider][role][key][stat]) for stat in ("avg", "p25", "p50", "p90", "p99")]
                for provider, label in PROVIDERS
            ])]
    blocks.append(
        "Zero calls means none were recorded, not proof that no tools ran. "
        "One round does not imply success or completion. See the reference for counting methods.\n"
    )
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} median rounds per {role_label}", "value": _number(data[provider][role]["rounds_per_session"]["p50"])}
        for provider, label in PROVIDERS
        for role, role_label in (("main", "main session"), ("descendant", "subagent"))
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
    md_path = out_dir / "workload.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
