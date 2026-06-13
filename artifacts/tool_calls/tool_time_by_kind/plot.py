#!/usr/bin/env python3
"""Total effective tool time attributed to each tool kind (where the time goes).

See README.md for the additive-latency assumption (parallel tools are not collapsed
into elapsed wall-clock time).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402
import csv  # noqa: E402
from style import (
    BAR_BLUE,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
    short_label,
)  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

DEFAULT_TOP_TOOLS = 30
DEFAULT_MIN_TOOL_CALLS = 20  # tools with fewer provider-local calls collapse into "Other"

# The collapsed "Other" bucket sorts after any real first-appearance ordinal, matching the old
# loader, which appended it last.
_OTHER_FIRST_SEEN = 1 << 62


@dataclass
class ToolTimeStats:
    """Summed-effective-latency figures `tool_total_time_by_kind` needs for one tool.

    Effective latency = ``tool_internal_latency_ms`` else ``tool_wall_latency_ms`` (the shared
    ``trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`` precedence). Only *positive* latencies feed
    ``latency_sum``/``latency_count``; ``missing_latency`` counts calls with no effective latency at
    all (the old ``ToolStats`` ``nonpositive_latency`` partition is unused by these outputs and is
    not tracked). ``first_seen`` is the global ordinal of the tool's first call (file order) — the
    deterministic tie-break for equal summed latency, not a plotted value.
    """

    calls: int = 0
    latency_count: int = 0
    missing_latency: int = 0
    error_calls: int = 0
    latency_sum: float = 0.0
    first_seen: int = _OTHER_FIRST_SEEN


def tool_sort_key(item: tuple[str, ToolTimeStats]) -> tuple[float, int]:
    """Sort key: most summed latency first, ties broken by first appearance — deterministic.

    Mirrors the old ``sorted(..., key=latency_sum, reverse=True)`` whose stable Python sort kept
    equal-latency tools in first-call (file) order.
    """
    return (-item[1].latency_sum, item[1].first_seen)


# Effective latency only matters when strictly positive (the old loader added only positive
# latencies to latency_sum / latency_count). NULL effective latency is "missing".
_EFF = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL


def _tool_time_query(plot_name_expr: str, *, by_provider: bool) -> str:
    """Aggregate effective tool latency per ``plot_name_expr`` bucket (file-order tie-break).

    ``plot_name_expr`` is a SQL expression over the per-call ``tname`` (normalized tool name): the
    raw name for the exact CSV, or the mcp-aliased name for the figure. Blank/whitespace/NULL tool
    names fold to ``<unknown-tool>`` (matching the old ``tool_name()`` helper). When ``by_provider``
    is set, rows additionally carry the joined ``rounds.provider`` and the GROUP BY splits per
    provider (deterministic ``first_seen`` ordinal pins equal-latency ties).
    """
    provider_select = "r.provider AS provider,\n               " if by_provider else ""
    provider_join = "JOIN rounds r USING (round_pk)\n        " if by_provider else ""
    # GROUP BY the plot-name expression itself (not a positional alias): with by_provider the
    # provider column shifts the positions, so reference the expression directly.
    group_by = f"r.provider, {plot_name_expr}" if by_provider else plot_name_expr
    return f"""
        WITH ordered AS (
            SELECT
                CASE WHEN tc.tool_name IS NULL OR trim(tc.tool_name) = ''
                     THEN '<unknown-tool>' ELSE trim(tc.tool_name) END AS tname,
                tc.round_pk                                            AS round_pk,
                tc.is_error                                            AS is_error,
                ({_EFF})                                               AS eff,
                row_number() OVER (ORDER BY tc.round_pk, tc.tool_index) AS call_ord
            FROM tool_calls tc
        )
        SELECT {provider_select}{plot_name_expr}                      AS plot_name,
               count(*)                                               AS calls,
               count(*) FILTER (WHERE eff IS NOT NULL AND eff > 0)    AS latency_count,
               count(*) FILTER (WHERE eff IS NULL)                    AS missing_latency,
               count(*) FILTER (WHERE is_error)                       AS error_calls,
               sum(CASE WHEN eff IS NOT NULL AND eff > 0 THEN eff ELSE 0 END) AS latency_sum,
               min(call_ord)                                          AS first_seen
        FROM ordered {provider_join}
        GROUP BY {group_by}
    """


def load_tool_time(con) -> dict[str, ToolTimeStats]:
    """Global ``{tool_name: ToolTimeStats}`` for the CSV — raw tool names, no aliasing/collapsing.

    Sums/counts are over ALL tool calls (no reservoir sampling), so the totals are exact.
    """
    rows = con.execute(_tool_time_query("tname", by_provider=False)).fetchall()
    return {
        plot_name: ToolTimeStats(
            calls=int(calls),
            latency_count=int(latency_count),
            missing_latency=int(missing_latency),
            error_calls=int(error_calls or 0),
            latency_sum=float(latency_sum or 0.0),
            first_seen=int(first_seen),
        )
        for plot_name, calls, latency_count, missing_latency, error_calls, latency_sum, first_seen in rows
    }


def load_tool_time_by_provider(con, *, min_calls: int) -> dict[str, dict[str, ToolTimeStats]]:
    """Per-provider ``{plot_tool_name: ToolTimeStats}`` for the figure, mcp merged + rare collapsed.

    Mirrors the old ``plot_ready_tool_stats_by_provider``: alias every ``mcp_*`` tool to ``mcp``
    (in SQL), then collapse tools below ``min_calls`` provider-local calls into one
    ``Other (<N calls/tool)`` bucket (here, summing — order-independent). ``first_seen`` (min global
    call ordinal) pins the equal-latency ordering so the figure is stable across DB builds.
    """
    plot_name_expr = (
        "CASE WHEN tname LIKE 'mcp\\_%' ESCAPE '\\' THEN 'mcp' ELSE tname END"
    )
    rows = con.execute(_tool_time_query(plot_name_expr, by_provider=True)).fetchall()

    by_provider: dict[str, dict[str, ToolTimeStats]] = {}
    for provider, plot_name, calls, latency_count, missing_latency, error_calls, latency_sum, first_seen in rows:
        bucket = by_provider.setdefault(provider, {})
        bucket[plot_name] = ToolTimeStats(
            calls=int(calls),
            latency_count=int(latency_count),
            missing_latency=int(missing_latency),
            error_calls=int(error_calls or 0),
            latency_sum=float(latency_sum or 0.0),
            first_seen=int(first_seen),
        )

    if min_calls > 1:
        other_label = f"Other (<{min_calls} calls/tool)"
        for provider, tools in list(by_provider.items()):
            kept: dict[str, ToolTimeStats] = {}
            other = ToolTimeStats()
            for name, stats in tools.items():
                if stats.calls < min_calls:
                    other.calls += stats.calls
                    other.latency_count += stats.latency_count
                    other.missing_latency += stats.missing_latency
                    other.error_calls += stats.error_calls
                    other.latency_sum += stats.latency_sum
                else:
                    kept[name] = stats
            if other.calls:
                kept[other_label] = other
            by_provider[provider] = kept

    return by_provider


def plot_tool_total_time_by_kind(
    tool_stats_by_provider: dict[str, dict[str, ToolTimeStats]],
    output_dir: Path,
    top_tools: int,
) -> None:
    provider_panels: list[tuple[str, list[tuple[str, ToolTimeStats]]]] = []
    for provider in provider_order(tool_stats_by_provider):
        selected = [
            (name, stats)
            for name, stats in sorted(
                tool_stats_by_provider[provider].items(),
                key=tool_sort_key,
            )
            if stats.latency_count > 0
        ][:top_tools]
        if selected:
            provider_panels.append((provider, selected))
    if not provider_panels:
        return

    panel_heights = [
        max(5.2, min(14.5, 0.43 * len(selected) + 2.0))
        for _provider, selected in provider_panels
    ]
    fig, axes = plt.subplots(
        len(provider_panels),
        1,
        figsize=(15.0, sum(panel_heights)),
        squeeze=False,
        gridspec_kw={"height_ratios": panel_heights},
    )
    fig.suptitle("Total Tool Time by Tool Kind and Provider", y=0.998, fontsize=18)

    for ax, (provider, selected) in zip(axes.ravel(), provider_panels, strict=True):
        names = [short_label(name, 34) for name, _stats in selected][::-1]
        total_hours = [stats.latency_sum / 3_600_000 for _name, stats in selected][::-1]
        calls = [stats.calls for _name, stats in selected][::-1]
        ax.set_title(
            f"{provider_title(provider)} - top {len(selected)} by summed latency",
            loc="left",
            pad=10,
            fontsize=15,
        )
        ax.set_xlabel("Summed effective latency (hours)", fontsize=18, labelpad=10)
        bars = ax.barh(names, total_hours, color=BAR_BLUE, alpha=0.72)
        polish_axes(ax, grid_axis="x")
        ax.tick_params(axis="y", labelsize=13.5)
        ax.tick_params(axis="x", labelsize=16, pad=6)
        for bar, call_count in zip(bars, calls, strict=True):
            width = bar.get_width()
            if width > 0:
                ax.text(
                    width,
                    bar.get_y() + bar.get_height() / 2,
                    f"  n={call_count:,}",
                    va="center",
                    fontsize=12,
                )

    fig.tight_layout(rect=(0, 0, 1, 0.992), h_pad=1.0)
    out = output_dir / "tool_total_time_by_kind.png"
    save_plot(fig, out)


def write_tool_total_time_by_kind(
    tool_stats: dict[str, ToolTimeStats],
    output_dir: Path,
) -> list[dict[str, Any]]:
    total_latency = sum(stats.latency_sum for stats in tool_stats.values())
    rows: list[dict[str, Any]] = []
    for name, stats in sorted(
        tool_stats.items(),
        key=tool_sort_key,
    ):
        if stats.latency_count <= 0:
            continue
        rows.append(
            {
                "tool_name": name,
                "tool_calls": stats.calls,
                "valid_latency_calls": stats.latency_count,
                "missing_latency_calls": stats.missing_latency,
                "error_calls": stats.error_calls,
                "total_latency_ms": stats.latency_sum,
                "total_latency_s": stats.latency_sum / 1000,
                "total_latency_hours": stats.latency_sum / 3_600_000,
                "latency_share": stats.latency_sum / total_latency
                if total_latency
                else 0.0,
                "avg_latency_ms": (
                    stats.latency_sum / stats.latency_count
                    if stats.latency_count
                    else None
                ),
            }
        )

    path = output_dir / "tool_total_time_by_kind.csv"
    fieldnames = [
        "tool_name",
        "tool_calls",
        "valid_latency_calls",
        "missing_latency_calls",
        "error_calls",
        "total_latency_ms",
        "total_latency_s",
        "total_latency_hours",
        "latency_share",
        "avg_latency_ms",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument(
        "--top-tools", type=int, default=DEFAULT_TOP_TOOLS,
        help="maximum tools to show per provider panel",
    )
    parser.add_argument(
        "--min-tool-calls-for-plot", type=int, default=DEFAULT_MIN_TOOL_CALLS,
        help="collapse tools with fewer provider-local calls into an Other bucket",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    tool_stats = load_tool_time(con)
    by_provider = load_tool_time_by_provider(con, min_calls=args.min_tool_calls_for_plot)

    plot_tool_total_time_by_kind(by_provider, out, args.top_tools)
    write_tool_total_time_by_kind(tool_stats, out)

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
