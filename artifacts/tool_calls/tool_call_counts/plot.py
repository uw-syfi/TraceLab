#!/usr/bin/env python3
"""How often each tool is called, paneled by provider.

See README.md for how rare tools are collapsed into an "Other" bucket in figures.
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
import numpy as np  # noqa: E402
from style import (
    BAR_BLUE,
    BAR_RED,
    MUTED_TEXT,
    TEXT_COLOR,
    mticker,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
    short_label,
)  # noqa: E402
from formatters import format_count_tick  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

DEFAULT_TOP_TOOLS = 30
DEFAULT_MIN_TOOL_CALLS = 20  # tools with fewer provider-local calls collapse into "Other"


# Equal-count tools are ordered by first appearance in the trace (file order), so two tools with
# the same call count never swap places run-to-run. The collapsed "Other" bucket sorts after any
# real tie, matching the old loader, which appended it last.
_OTHER_FIRST_SEEN = 1 << 62


@dataclass
class ToolCounts:
    """The per-(provider, tool) figures `tool_call_counts.png` needs: total + error call counts.

    `first_seen` is the global ordinal of the tool's first call (file order); it is the deterministic
    tie-break for equal call counts, not a plotted value.
    """

    calls: int = 0
    error_calls: int = 0
    first_seen: int = _OTHER_FIRST_SEEN


def tool_sort_key(item: tuple[str, ToolCounts]) -> tuple[int, int]:
    """Sort key: most calls first, ties broken by first appearance (file order) — deterministic."""
    return (-item[1].calls, item[1].first_seen)


def load_tool_counts_by_provider(
    con, *, min_calls: int
) -> dict[str, dict[str, ToolCounts]]:
    """Per-provider {plot_tool_name: ToolCounts}, mcp_* merged and rare tools collapsed.

    Mirrors the old `plot_ready_tool_stats_by_provider`: alias every `mcp_*` tool to `mcp`
    (done in SQL), then collapse tools below `min_calls` provider-local calls into one
    `Other (<N calls/tool)` bucket (done here, summing — order-independent). `first_seen` (min
    global call ordinal) pins the equal-count ordering so output is stable across DB builds.
    """
    rows = con.execute(
        """
        WITH ordered AS (
            SELECT tc.round_pk, tc.tool_name, tc.is_error,
                   row_number() OVER (ORDER BY tc.round_pk, tc.tool_index) AS call_ord
            FROM tool_calls tc
        )
        SELECT r.provider AS provider,
               CASE WHEN o.tool_name LIKE 'mcp\\_%' ESCAPE '\\' THEN 'mcp'
                    ELSE o.tool_name END                        AS plot_name,
               count(*)                                         AS calls,
               count(*) FILTER (WHERE o.is_error)               AS error_calls,
               min(o.call_ord)                                  AS first_seen
        FROM ordered o JOIN rounds r USING (round_pk)
        GROUP BY 1, 2
        """
    ).fetchall()

    by_provider: dict[str, dict[str, ToolCounts]] = {}
    for provider, plot_name, calls, error_calls, first_seen in rows:
        bucket = by_provider.setdefault(provider, {})
        bucket[plot_name] = ToolCounts(int(calls), int(error_calls or 0), int(first_seen))

    if min_calls > 1:
        other_label = f"Other (<{min_calls} calls/tool)"
        for provider, tools in list(by_provider.items()):
            kept: dict[str, ToolCounts] = {}
            other = ToolCounts()
            for name, counts in tools.items():
                if counts.calls < min_calls:
                    other.calls += counts.calls
                    other.error_calls += counts.error_calls
                else:
                    kept[name] = counts
            if other.calls:
                kept[other_label] = other
            by_provider[provider] = kept

    return by_provider


def plot_tool_counts(
    tool_stats_by_provider: dict[str, dict[str, ToolCounts]],
    output_dir: Path,
    top_tools: int,
) -> None:
    provider_panels: list[tuple[str, list[tuple[str, ToolCounts]]]] = []
    for provider in provider_order(tool_stats_by_provider):
        selected = sorted(
            tool_stats_by_provider[provider].items(),
            key=tool_sort_key,
        )[:top_tools]
        if selected:
            provider_panels.append((provider, selected))
    if not provider_panels:
        return

    panel_heights = [
        max(4.8, min(13.5, 0.36 * len(selected) + 2.0))
        for _provider, selected in provider_panels
    ]
    fig, axes = plt.subplots(
        len(provider_panels),
        1,
        figsize=(15.0, sum(panel_heights)),
        squeeze=False,
        gridspec_kw={"height_ratios": panel_heights},
    )
    fig.suptitle("Tool Call Counts by Provider", fontsize=18, y=0.998)

    for ax, (provider, selected) in zip(axes.ravel(), provider_panels, strict=True):
        names = [short_label(name, 34) for name, _stats in selected][::-1]
        calls = [stats.calls for _name, stats in selected][::-1]
        errors = [stats.error_calls for _name, stats in selected][::-1]
        y_positions = np.arange(len(names))
        panel_cap, second_largest_call = tool_count_panel_cap(selected)
        call_widths = [min(call, panel_cap) for call in calls]
        error_widths = [min(error, panel_cap) if error else 0 for error in errors]

        ax.set_title(
            f"{provider_title(provider)} - top {len(selected)}",
            loc="left",
            pad=10,
            fontsize=15,
        )
        ax.set_xlabel("Calls (linear, clipped)", fontsize=18, labelpad=10)
        ax.set_xlim(0, panel_cap)
        ax.axvline(
            panel_cap, color=MUTED_TEXT, linestyle="--", linewidth=0.9, alpha=0.65
        )
        ax.barh(y_positions, call_widths, color=BAR_BLUE, alpha=0.88, label="calls")
        if any(errors):
            ax.barh(
                y_positions, error_widths, color=BAR_RED, alpha=0.88, label="errors"
            )
            ax.legend(frameon=False, fontsize=13, loc="lower right")
        for y_pos, call in zip(y_positions, calls, strict=True):
            if call > panel_cap:
                ax.text(
                    panel_cap,
                    y_pos,
                    f"  {call:,}",
                    va="center",
                    ha="left",
                    fontsize=12.5,
                    color=TEXT_COLOR,
                    fontweight="semibold",
                    clip_on=False,
                )
                continue
            if call != second_largest_call:
                continue
            ax.text(
                call,
                y_pos,
                f"  {call:,}",
                va="center",
                ha="left",
                fontsize=12.5,
                color=TEXT_COLOR,
                clip_on=False,
            )
        ax.set_yticks(y_positions)
        ax.set_yticklabels(names)
        ax.xaxis.set_major_locator(mticker.MaxNLocator(6))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_count_tick))
        ax.tick_params(axis="x", labelsize=16, pad=6)
        ax.tick_params(axis="y", labelsize=13.5)
        polish_axes(ax, grid_axis="x")

    fig.tight_layout(rect=(0, 0, 1, 0.992), h_pad=1.15)
    out = output_dir / "tool_call_counts.png"
    save_plot(fig, out)


def tool_count_panel_cap(
    selected: list[tuple[str, ToolCounts]],
) -> tuple[float, int | None]:
    calls = [stats.calls for _name, stats in selected]
    descending_calls = sorted(calls, reverse=True)
    if len(descending_calls) > 1:
        second_largest_call = descending_calls[1]
        return max(1.0, second_largest_call * 1.05), second_largest_call
    if descending_calls:
        return max(1.0, descending_calls[0]), None
    return 1.0, None


def write_tool_call_counts_by_provider(
    tool_stats_by_provider: dict[str, dict[str, ToolCounts]],
    output_dir: Path,
    top_tools: int,
) -> list[dict[str, Any]]:
    """Write the table corresponding to tool_call_counts.png."""
    rows: list[dict[str, Any]] = []
    for provider in provider_order(tool_stats_by_provider):
        selected = sorted(
            tool_stats_by_provider[provider].items(),
            key=tool_sort_key,
        )[:top_tools]
        if not selected:
            continue
        panel_cap, second_largest_call = tool_count_panel_cap(selected)
        for rank, (name, stats) in enumerate(selected, start=1):
            call_plot_width = min(float(stats.calls), panel_cap)
            error_plot_width = (
                min(float(stats.error_calls), panel_cap) if stats.error_calls else 0.0
            )
            rows.append(
                {
                    "provider": provider,
                    "rank": rank,
                    "tool_name": name,
                    "display_tool_name": short_label(name, 34),
                    "calls": stats.calls,
                    "error_calls": stats.error_calls,
                    "error_rate": stats.error_calls / stats.calls
                    if stats.calls
                    else None,
                    "panel_cap": panel_cap,
                    "second_largest_call": second_largest_call,
                    "call_plot_width": call_plot_width,
                    "error_plot_width": error_plot_width,
                    "call_is_clipped": stats.calls > panel_cap,
                    "error_is_clipped": stats.error_calls > panel_cap,
                }
            )

    path = output_dir / "tool_call_counts_by_provider.csv"
    fieldnames = [
        "provider",
        "rank",
        "tool_name",
        "display_tool_name",
        "calls",
        "error_calls",
        "error_rate",
        "panel_cap",
        "second_largest_call",
        "call_plot_width",
        "error_plot_width",
        "call_is_clipped",
        "error_is_clipped",
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
    by_provider = load_tool_counts_by_provider(con, min_calls=args.min_tool_calls_for_plot)

    plot_tool_counts(by_provider, out, args.top_tools)
    write_tool_call_counts_by_provider(by_provider, out, args.top_tools)

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
