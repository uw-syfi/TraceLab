#!/usr/bin/env python3
"""Compare main and descendant tool usage within linked Claude and Codex families."""

from __future__ import annotations

import argparse
import csv
import html
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402
import png_sidecar  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, summarize  # noqa: E402
from tool_metrics import tool_aggregate_sql  # noqa: E402
from tool_stats import plot_tool_name  # noqa: E402
from style import (  # noqa: E402
    BAR_BLUE, BAR_GREEN, PROVIDER_COMPARISON_ORDER, mticker, plt,
    polish_axes, provider_title, save_plot, short_label,
)

ROLES = ("main", "descendant")
TOP_TOOLS = 12
COUNTS = ("calls", "errors", "successes", "unknown_outcomes",
          "positive_latency_calls", "missing_latency_calls", "nonpositive_latency_calls")
STATS = ("n", "avg", "p25", "p50", "p90", "p99")
FIELDS = ["row_type", "provider", "role", "tool_name", *COUNTS,
          "error_rate", "known_outcome_coverage", "call_share", "latency_ms",
          "latency_share", *[f"diversity_{key}" for key in STATS]]


def load_metrics(con):
    """Aggregate calls once; separately left-join sessions to retain zero-tool diversity."""
    agg = tool_aggregate_sql()
    result = con.execute(f"""
        WITH cohort AS ({COHORT_SQL}), calls AS (
            SELECT c.provider, c.role,
                   CASE WHEN tc.tool_name IS NULL OR trim(tc.tool_name) = ''
                        THEN NULL ELSE tc.tool_name END AS tool_name,
                   tc.is_error, ({trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL}) AS eff
            FROM cohort c
            JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
            JOIN tool_calls tc ON tc.round_pk = r.round_pk
        )
        SELECT provider, role, tool_name, {agg['calls']} AS calls,
               {agg['errors']} AS errors,
               {agg['successes']} AS successes,
               {agg['unknown_outcomes']} AS unknown_outcomes,
               {agg['positive_latency_calls']} AS positive_latency_calls,
               {agg['missing_latency_calls']} AS missing_latency_calls,
               {agg['nonpositive_latency_calls']} AS nonpositive_latency_calls,
               {agg['latency_sum']} AS latency_ms
        FROM calls GROUP BY provider, role, tool_name
        ORDER BY provider, role, calls DESC, tool_name NULLS LAST
    """)
    columns = [col[0] for col in result.description]
    tools = [dict(zip(columns, row)) for row in result.fetchall()]
    diversity = con.execute(f"""
        WITH cohort AS ({COHORT_SQL})
        SELECT c.provider, c.role, count(DISTINCT CASE
                   WHEN trim(tc.tool_name) <> '' THEN tc.tool_name END) AS named_tools
        FROM cohort c
        LEFT JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
        LEFT JOIN tool_calls tc ON tc.round_pk = r.round_pk
        GROUP BY c.provider, c.role, c.session_id
    """).fetchall()
    groups = []
    for provider in PROVIDER_COMPARISON_ORDER:
        for role in ROLES:
            rows = [r for r in tools if (r["provider"], r["role"]) == (provider, role)]
            stats = summarize([n for p, r, n in diversity if (p, r) == (provider, role)])
            group = {"row_type": "role", "provider": provider, "role": role,
                     **{f"diversity_{k}": v for k, v in stats.items()},
                     **{k: sum(r[k] for r in rows) for k in COUNTS}}
            group["latency_ms"] = (sum(r["latency_ms"] or 0 for r in rows)
                                   if group["positive_latency_calls"] else None)
            for row in [group, *rows]:
                known = row["errors"] + row["successes"]
                row["error_rate"] = row["errors"] / known if known else None
                row["known_outcome_coverage"] = known / row["calls"] if row["calls"] else None
                row["call_share"] = row["calls"] / group["calls"] if group["calls"] else None
                row["latency_share"] = ((row["latency_ms"] or 0) / group["latency_ms"]
                                        if group["latency_ms"] else None)
            groups.append(group)
    for row in tools:
        row["row_type"] = "tool"
    return groups, tools


def chart_rows(tools, metric):
    """Use the overall tool sections' MCP aliases; retain exact names in tables/CSV."""
    buckets = {}
    for row in tools:
        name = row["tool_name"] or "<unknown-tool>"
        if metric == "latency_ms":
            name = name.strip()  # The overall tool-time section trims names before plotting.
        name = plot_tool_name(name)
        key = row["provider"], row["role"], name
        bucket = buckets.setdefault(key, {"provider": row["provider"], "role": row["role"],
                                          "tool_name": name, metric: 0})
        bucket[metric] += row[metric] or 0
    return list(buckets.values())


def selected_names(tools, metric="calls"):
    """Rank counts by calls and time by canonical summed positive effective latency."""
    totals = {}
    for row in tools:
        name = row["tool_name"]
        totals[name] = totals.get(name, 0) + (row[metric] or 0)
    return sorted((name for name in totals if totals[name] > 0),
                  key=lambda name: (-totals[name], name is None, name or ""))[:TOP_TOOLS]


def display_name(name):
    return "[unnamed calls]" if name is None else name


def plot_usage(groups, tools, output_dir):
    panels = {metric: chart_rows(tools, metric) for metric in ("calls", "latency_ms")}
    selected = {metric: selected_names(rows, metric) for metric, rows in panels.items()}
    fig, axes = plt.subplots(2, 2, figsize=(16, max(9, max(map(len, selected.values())) * 0.8 + 3)), squeeze=False)
    try:
        fig.suptitle("Tool usage in linked subagent families", fontsize=18)
        latency_max = max((r["latency_ms"] for r in panels["latency_ms"]
                           if r["tool_name"] in selected["latency_ms"]), default=0) / 3_600_000
        for index, provider in enumerate(PROVIDER_COMPARISON_ORDER):
            provider_groups = [r for r in groups if r["provider"] == provider]
            for column, metric in enumerate(("calls", "latency_ms")):
                names = selected[metric]
                rows = panels[metric]
                lookup = {(r["provider"], r["role"], r["tool_name"]): r for r in rows}
                ax = axes[index, column]
                title = "Top tools by calls" if column == 0 else "Top tools by summed positive tool time"
                ax.set_title(f"{provider_title(provider)} — {title}", loc="left", pad=12)
                if not any(r["diversity_n"] for r in provider_groups):
                    ax.text(0.5, 0.5, "No linked data", ha="center", va="center", transform=ax.transAxes)
                    ax.set_axis_off()
                    continue
                available = False
                notes = []
                for role_index, (role, color) in enumerate(zip(ROLES, (BAR_BLUE, BAR_GREEN))):
                    group = next(r for r in provider_groups if r["role"] == role)
                    valid = group["calls"] if column == 0 else group["positive_latency_calls"]
                    if not valid:
                        message = "no calls" if not group["calls"] else "no positive latency recorded"
                        notes.append(f"{role.title()}: {message}")
                        continue
                    total = group[metric]
                    omitted = sum(r[metric] for r in rows if (r["provider"], r["role"]) == (provider, role)
                                  and r["tool_name"] not in names)
                    notes.append(f"{role.title()} omitted: {100 * omitted / total:.1f}%")
                    available = True
                    positions, values = [], []
                    for i, name in enumerate(names):
                        row = lookup.get((provider, role, name))
                        # Missing latency is unavailable, not a measured zero-duration bar.
                        if column == 1 and (row is None or row[metric] <= 0):
                            continue
                        positions.append(i + (role_index - 0.5) * 0.36)
                        value = row[metric] if row else 0
                        values.append(value / group["calls"] if column == 0 else value / 3_600_000)
                    ax.barh(positions, values, height=0.34, color=color, alpha=0.78,
                            label=f"{role.title()} (sessions: {group['diversity_n']:,})")
                if notes:
                    ax.set_title(f"{provider_title(provider)} — {title}\n" + "; ".join(notes),
                                 loc="left", pad=12)
                ax.set_yticks(range(len(names)), [short_label(display_name(n), 32) for n in names])
                ax.set_ylim(max(len(names), 3) + 0.5, -0.6)
                ax.set_xlabel("Share of all role calls" if column == 0 else "Summed effective latency (hours; not elapsed)")
                ax.set_xlim(0, 1 if column == 0 else (latency_max * 1.08 or 1))
                if column == 0:
                    ax.xaxis.set_major_formatter(mticker.PercentFormatter(1))
                polish_axes(ax, grid_axis="x")
                if available:
                    ax.legend(loc="lower right")
        fig.text(0.5, 0.01,
                 f"Each column selects its top {TOP_TOOLS} categories across providers and roles; mcp_* tools merge as mcp. "
                 "Time adds call latencies, not elapsed runtime. Tail shares and coverage are in the tables/CSV.",
                 ha="center", fontsize=10)
        fig.tight_layout(rect=(0, 0.04, 1, 0.96), h_pad=2, w_pad=3)
        save_plot(fig, output_dir / "tool_usage.png")
    finally:
        plt.close(fig)


def format_value(value):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


def render_md(groups, tools):
    def percent(value):
        return "N/A" if value is None else f"{100 * value:,.4g}%"

    def table(rows):
        return md_table.gfm_table(["Measure", "Claude", "Codex"], rows)

    blocks = ["# Tool usage in linked subagent families\n",
              "Main sessions are identified by provider source or file layout, confirmed as their own roots, "
              "and included only when they have linked subagents. Each main is counted once. "
              "Linked subagents include indirect descendants and may have unknown origins themselves; "
              "unknown-origin roots are excluded. A family is a main and its linked subagents.\n\n"
              "## Reading the tool tables\n\n"
              "Error rate uses only known outcomes. Outcome coverage is the share of calls with a known success or error. "
              "Call and time shares use all tools in the same provider and session group.\n\n"
              "Time sums positive call latencies (ms), not elapsed runtime. No positive time means N/A; "
              "time share is zero only when the group has positive time. Missing tool rows are N/A, not zero.\n"]
    for provider in PROVIDER_COMPARISON_ORDER:
        if not any(r["diversity_n"] for r in groups if r["provider"] == provider):
            blocks.append(f"**{provider_title(provider)}: no linked data.**\n")
    group_lookup = {(r["provider"], r["role"]): r for r in groups}
    tool_lookup = {(r["provider"], r["role"], r["tool_name"]): r for r in tools}
    for role, label in (("main", "Main sessions"), ("descendant", "Subagents")):
        pair = [group_lookup[p, role] for p in PROVIDER_COMPARISON_ORDER]
        rows = [[label] + [format_value(r[key]) for r in pair] for key, label in (
            ("diversity_n", "Sessions"), ("calls", "Tool calls"),
            ("errors", "Known errors"), ("successes", "Known successes"), ("unknown_outcomes", "Unknown outcomes"),
            ("positive_latency_calls", "Calls with positive time"), ("missing_latency_calls", "Calls with missing time"),
            ("nonpositive_latency_calls", "Calls with zero or negative time"), ("latency_ms", "Summed positive tool time (ms)"),
        )]
        rows += [[label] + [percent(r[key]) for r in pair] for key, label in (
            ("error_rate", "Error rate"),
            ("known_outcome_coverage", "Outcome coverage"),
        )]
        blocks += [f"## {label}\n", table(rows)]
        blocks += ["### Distinct tools per session\n", md_table.gfm_table(
            ["Provider", "Avg", "P25", "P50", "P90", "P99"],
            [[provider_title(r["provider"])] + [format_value(r[f"diversity_{stat}"]) for stat in ("avg", "p25", "p50", "p90", "p99")]
             for r in pair],
        )]
    blocks.append("Distinct tools counts exact, nonblank names; sessions with no named tools count as zero. "
                  "Avg is the average; P25/P50/P90/P99 are percentiles, with P50 the median.\n")
    blocks += ["## Tools outside the figure\n",
                f"Each column selects up to {TOP_TOOLS} categories across providers and roles: "
                "the call column ranks by calls; the time column ranks by summed positive effective time. "
                "Both provider rows share that column's categories and scale. MCP names merge as mcp in the figure, "
                "following the overall tool sections; the time column also trims surrounding whitespace. "
                "Exact names and every call remain in the tables/CSV. Omitted shares use all calls or all positive "
                "effective time in the same provider and role. No positive timing means N/A.\n"]
    for role, label in (("main", "Main sessions"), ("descendant", "Subagents")):
        rows = []
        for provider in PROVIDER_COMPARISON_ORDER:
            group = group_lookup[provider, role]
            for metric, panel in (("calls", "Calls"), ("latency_ms", "Positive time (ms)")):
                plotted = chart_rows(tools, metric)
                names = selected_names(plotted, metric)
                omitted = [r for r in plotted if (r["provider"], r["role"]) == (provider, role)
                           and r["tool_name"] not in names]
                value = sum(r[metric] for r in omitted)
                total = group[metric]
                rows.append([provider_title(provider), panel, len(omitted),
                             format_value(value if metric == "calls" or total else None),
                             percent(value / total if total else None)])
        blocks += [f"### {label}\n", md_table.gfm_table(
            ["Provider", "Panel", "Omitted categories", "Omitted value", "Omitted share"], rows)]
    blocks += ["## Every recorded tool\n",
               "Names are kept exactly as recorded; matching names do not guarantee matching behavior. "
               "Blank or missing names share one unnamed-call group, excluded from distinct-tool counts.\n"]
    for role, label in (("main", "Main sessions"), ("descendant", "Subagents")):
        role_names = sorted({r["tool_name"] for r in tools if r["role"] == role}, key=lambda n: (n is None, n or ""))
        blocks.append(f"### {label}\n")
        for name in role_names:
            safe_name = html.escape(display_name(name)).replace("|", "&#124;").replace("\n", " ").replace("\r", " ")
            rows = []
            for key, metric, formatter in (
                ("calls", "Calls", format_value), ("call_share", "Call share", percent),
                ("errors", "Known errors", format_value), ("successes", "Known successes", format_value),
                ("unknown_outcomes", "Unknown outcomes", format_value), ("error_rate", "Error rate", percent),
                ("known_outcome_coverage", "Outcome coverage", percent),
                ("latency_ms", "Summed tool time (ms)", format_value), ("latency_share", "Time share", percent),
                ("positive_latency_calls", "Calls with positive time", format_value),
                ("missing_latency_calls", "Calls with missing time", format_value),
                ("nonpositive_latency_calls", "Calls with zero or negative time", format_value),
            ):
                cells = []
                for provider in PROVIDER_COMPARISON_ORDER:
                    r = tool_lookup.get((provider, role, name))
                    cells.append("N/A" if r is None else formatter(r[key]))
                rows.append([metric, *cells])
            blocks += [f"#### <code>{safe_name}</code>\n", table(rows)]
    return "\n".join(blocks)


def write_tables(groups, tools, output_dir):
    with (output_dir / "tool_usage.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows([*groups, *tools])
    (output_dir / "tool_usage.md").write_text(render_md(groups, tools), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()
    con = trace_db.open_from_args(args)
    try:
        groups, tools = load_metrics(con)
    finally:
        con.close()
    write_tables(groups, tools, args.output_dir)
    plot_usage(groups, tools, args.output_dir)
    png_sidecar.make_self_contained(
        args.output_dir, code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md", png_names=["tool_usage.png"], data_glob="tool_usage.csv",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
