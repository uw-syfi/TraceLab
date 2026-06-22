#!/usr/bin/env python3
"""How often each tool is called, paneled by provider.

See README.md for how rare tools are collapsed into an "Other" bucket in figures.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
PAPER_ROOT = REPO_ROOT.parent if (REPO_ROOT.parent / "figures").exists() else REPO_ROOT
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
    readable_text_color,
    save_plot,
    short_label,
)  # noqa: E402
from formatters import format_count_tick  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

DEFAULT_TOP_TOOLS = 30
DEFAULT_MIN_TOOL_CALLS = 20  # tools with fewer provider-local calls collapse into "Other"
DEFAULT_RING_MAIN_MIN_SHARE = 0.02
DEFAULT_RING_TAIL_TOP_TOOLS = 6

# One hue per provider row (matching the paper's provider colours): every slice in a row —
# both the full ring and its expanded slice — is a shade of the same sequential colormap,
# darkest for the largest tool. The aggregated bucket stays neutral grey so it reads as the
# "everything else" slice that the funnel expands.
ROW_CMAPS = {"claude": "Blues", "codex": "Oranges"}
FALLBACK_CMAPS = ["Purples", "Greens", "Reds", "GnBu", "PuRd"]
TAIL_COLOR = "#C7CDD6"  # neutral grey for the aggregated "Small tools"/"Other small"
CONNECTOR_COLOR = "#9AA3B2"


def hue_ramp(cmap_name: str, n: int, *, lo: float = 0.34, hi: float = 0.90) -> list:
    """`n` colours from a sequential colormap, darkest (hi) first — ordered by slice size."""
    cmap = plt.get_cmap(cmap_name)
    if n <= 0:
        return []
    if n == 1:
        return [cmap(hi)]
    return [cmap(hi - (hi - lo) * i / (n - 1)) for i in range(n)]


def ring_colors(items, cmap_name: str, aggregate_name: str) -> list:
    """Sequential shades for real tools (size order) + neutral grey for the aggregate bucket."""
    ramp = hue_ramp(cmap_name, sum(1 for name, _stats in items if name != aggregate_name))
    colors: list = []
    cursor = 0
    for name, _stats in items:
        if name == aggregate_name:
            colors.append(TAIL_COLOR)
        else:
            colors.append(ramp[cursor])
            cursor += 1
    return colors


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


class RingSegments(NamedTuple):
    total_calls: int
    main: list[tuple[str, ToolCounts]]
    tail: list[tuple[str, ToolCounts]]


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


def combine_tool_counts(items: list[tuple[str, ToolCounts]]) -> ToolCounts:
    counts = ToolCounts()
    for _name, item in items:
        counts.calls += item.calls
        counts.error_calls += item.error_calls
        counts.first_seen = min(counts.first_seen, item.first_seen)
    return counts


def split_ring_segments(
    tools: dict[str, ToolCounts],
    *,
    main_min_share: float,
    tail_top_tools: int,
) -> RingSegments:
    selected = sorted(tools.items(), key=tool_sort_key)
    total_calls = sum(stats.calls for _name, stats in selected)
    if total_calls <= 0:
        return RingSegments(0, [], [])

    main: list[tuple[str, ToolCounts]] = []
    tail: list[tuple[str, ToolCounts]] = []
    for name, stats in selected:
        if stats.calls / total_calls >= main_min_share:
            main.append((name, stats))
        else:
            tail.append((name, stats))

    if not main and selected:
        main = [selected[0]]
        tail = selected[1:]

    if tail:
        main.append(("Small tools", combine_tool_counts(tail)))

    tail_share = sum(stats.calls for _name, stats in tail) / total_calls if total_calls else 0.0
    effective_tail_top_tools = tail_top_tools
    if tail_share < 0.04:
        effective_tail_top_tools = min(effective_tail_top_tools, 6)

    tail_expanded = tail[:effective_tail_top_tools]
    tail_remainder = tail[effective_tail_top_tools:]
    if tail_remainder:
        tail_expanded.append(("Other small", combine_tool_counts(tail_remainder)))

    return RingSegments(total_calls, main, tail_expanded)


def format_ring_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:,}"


def format_share_label(share: float) -> str:
    if 0 < share < 0.001:
        return "<0.1%"
    return f"{share * 100:.1f}%"


def wrap_two_lines(name: str, *, min_len: int = 13) -> str | None:
    """Break a long single-token tool name across two balanced lines, else None.

    Keeps wide outside labels (e.g. ``AskUserQuestion``) from reaching across the figure into
    the neighbouring ring. Names with a space (``Small tools``) or already short are left alone.
    """
    if len(name) < min_len or " " in name or "\n" in name:
        return None
    if "_" in name:
        cuts = [i for i, char in enumerate(name) if char == "_"]
        cut = min(cuts, key=lambda i: abs(i - len(name) / 2))
        return name[:cut] + "\n" + name[cut + 1 :]
    caps = [i for i in range(1, len(name)) if name[i].isupper()]
    if caps:
        cut = min(caps, key=lambda i: abs(i - len(name) / 2))
        return name[:cut] + "\n" + name[cut:]
    return None


def pretty_tool_name(name: str, *, inside: bool, max_len: int) -> str:
    display = name
    if name.startswith("Other (<"):
        display = "Other rare"
    if inside:
        replacements = {
            "exec_command": "exec\ncmd",
            "write_stdin": "write\nstdin",
            "apply_patch": "apply\npatch",
            "shell_command": "shell\ncmd",
            "TaskUpdate": "Task\nupdate",
        }
        if display in replacements:
            return replacements[display]
        if "_" in display and len(display) > 9:
            return display.replace("_", "\n")
    else:
        wrapped = wrap_two_lines(display)
        if wrapped is not None:
            return wrapped
    display = short_label(display, max_len)
    return display


def ring_label(name: str, share: float, *, inside: bool, max_len: int = 18) -> str:
    display = pretty_tool_name(name, inside=inside, max_len=max_len)
    return f"{display}\n{format_share_label(share)}"


def spread_label_positions(
    entries: list[dict[str, Any]],
    *,
    min_gap: float,
    y_min: float,
    y_max: float,
) -> None:
    entries.sort(key=lambda entry: entry["target_y"])
    for index, entry in enumerate(entries):
        if index == 0:
            entry["label_y"] = max(y_min, entry["target_y"])
        else:
            entry["label_y"] = max(entry["target_y"], entries[index - 1]["label_y"] + min_gap)

    if entries and entries[-1]["label_y"] > y_max:
        overflow = entries[-1]["label_y"] - y_max
        for entry in entries:
            entry["label_y"] -= overflow
        if entries[0]["label_y"] < y_min:
            underflow = y_min - entries[0]["label_y"]
            for entry in entries:
                entry["label_y"] += underflow


def annotate_ring(
    ax,
    wedges,
    items: list[tuple[str, ToolCounts]],
    *,
    total_calls: int,
    inside_min_share: float,
    outside_radius: float,
    min_label_gap: float,
    label_fontsize: float,
    count_is_total: bool,
    colors: list | None = None,
) -> None:
    palette = colors if colors is not None else [None] * len(items)
    outside_entries: list[dict[str, Any]] = []
    for wedge, (name, stats), fill in zip(wedges, items, palette, strict=True):
        share = stats.calls / total_calls if total_calls else 0.0
        theta = np.deg2rad((wedge.theta1 + wedge.theta2) / 2.0)
        x = np.cos(theta)
        y = np.sin(theta)

        if share >= inside_min_share and name not in ("Small tools", "Other small"):
            label = ring_label(name, share, inside=True, max_len=12)
            text_color = readable_text_color(fill) if isinstance(fill, tuple) else "white"
            ax.text(
                0.74 * x,
                0.74 * y,
                label,
                ha="center",
                va="center",
                fontsize=label_fontsize - 2.5,
                color=text_color,
                fontweight="bold",
                linespacing=0.88,
            )
            continue

        side = 1 if x >= 0 else -1
        label = ring_label(name, share, inside=False, max_len=17 if count_is_total else 20)
        outside_entries.append(
            {
                "xy": (0.98 * x, 0.98 * y),
                "text_x": outside_radius * side,
                "target_y": outside_radius * y,
                "ha": "left" if side > 0 else "right",
                "label": label,
            }
        )

    for side in (-1, 1):
        side_entries = [entry for entry in outside_entries if entry["text_x"] * side > 0]
        spread_label_positions(
            side_entries,
            min_gap=min_label_gap,
            y_min=-1.32,
            y_max=1.32,
        )
        for entry in side_entries:
            ax.annotate(
                entry["label"],
                xy=entry["xy"],
                xytext=(entry["text_x"], entry["label_y"]),
                textcoords="data",
                ha=entry["ha"],
                va="center",
                fontsize=label_fontsize,
                color=TEXT_COLOR,
                fontweight="semibold",
                linespacing=0.96,
                arrowprops={
                    "arrowstyle": "-",
                    "color": CONNECTOR_COLOR,
                    "linewidth": 0.85,
                    "shrinkA": 0,
                    "shrinkB": 4,
                    "connectionstyle": "angle3,angleA=0,angleB=90",
                },
                annotation_clip=False,
            )


def startangle_for_segment_center(
    items: list[tuple[str, ToolCounts]],
    *,
    target_name: str,
    target_degrees: float = 0.0,
) -> float:
    """Return a clockwise pie start angle that centers target_name at target_degrees."""
    total = sum(stats.calls for _name, stats in items)
    if total <= 0:
        return 92.0
    cumulative = 0
    for name, stats in items:
        if name == target_name:
            center_fraction = (cumulative + stats.calls / 2.0) / total
            return (target_degrees + 360.0 * center_fraction) % 360.0
        cumulative += stats.calls
    return 92.0


def write_ring_segments_csv(
    segments_by_provider: dict[str, RingSegments],
    output_dir: Path,
) -> Path:
    path = output_dir / "tool_call_count_ring_segments.csv"
    fieldnames = [
        "provider",
        "panel",
        "rank",
        "tool_name",
        "display_tool_name",
        "calls",
        "error_calls",
        "share_of_provider_calls",
        "share_of_panel_calls",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for provider in provider_order(segments_by_provider):
            segments = segments_by_provider[provider]
            for panel, items in (("main", segments.main), ("expanded_small", segments.tail)):
                panel_total = sum(stats.calls for _name, stats in items)
                for rank, (name, stats) in enumerate(items, start=1):
                    writer.writerow(
                        {
                            "provider": provider,
                            "panel": panel,
                            "rank": rank,
                            "tool_name": name,
                            "display_tool_name": short_label(name, 34),
                            "calls": stats.calls,
                            "error_calls": stats.error_calls,
                            "share_of_provider_calls": stats.calls / segments.total_calls
                            if segments.total_calls
                            else 0.0,
                            "share_of_panel_calls": stats.calls / panel_total
                            if panel_total
                            else 0.0,
                        }
                    )
    print(f"Saved {path}", file=sys.stderr)
    return path


def plot_tool_count_rings(
    tool_stats_by_provider: dict[str, dict[str, ToolCounts]],
    output_dir: Path,
    *,
    main_min_share: float,
    tail_top_tools: int,
) -> None:
    segments_by_provider: dict[str, RingSegments] = {}
    for provider in provider_order(tool_stats_by_provider):
        segments = split_ring_segments(
            tool_stats_by_provider[provider],
            main_min_share=main_min_share,
            tail_top_tools=tail_top_tools,
        )
        if segments.main:
            segments_by_provider[provider] = segments
    if not segments_by_provider:
        return

    providers = provider_order(segments_by_provider)
    fig, axes = plt.subplots(
        len(providers),
        2,
        figsize=(9.7, 4.7 * len(providers)),
        gridspec_kw={"width_ratios": [1.0, 1.0], "wspace": 0.12, "hspace": 0.16},
        squeeze=False,
    )
    for row_index, provider in enumerate(providers):
        segments = segments_by_provider[provider]
        ax_main = axes[row_index, 0]
        ax_tail = axes[row_index, 1]
        for ax in (ax_main, ax_tail):
            ax.set_facecolor("white")
            ax.axis("off")
            ax.set(aspect="equal")

        cmap_name = ROW_CMAPS.get(
            provider, FALLBACK_CMAPS[row_index % len(FALLBACK_CMAPS)]
        )
        main_values = [stats.calls for _name, stats in segments.main]
        main_colors = ring_colors(segments.main, cmap_name, "Small tools")
        # Anchor "Small tools" (and the small slices around it, which carry the outside labels)
        # on the left, away from the gap between the two panels, so their labels never collide
        # with the expanded ring's labels. The big inside-labelled slices face the gap.
        main_startangle = startangle_for_segment_center(
            segments.main,
            target_name="Small tools",
            target_degrees=180.0,
        )
        main_wedges, _ = ax_main.pie(
            main_values,
            startangle=main_startangle,
            counterclock=False,
            colors=main_colors,
            radius=1.0,
            wedgeprops={"width": 0.50, "edgecolor": "white", "linewidth": 1.6},
        )
        ax_main.set_xlim(-1.18, 1.18)
        ax_main.set_ylim(-1.20, 1.20)
        ax_main.text(
            0,
            0.10,
            format_ring_count(segments.total_calls),
            ha="center",
            va="center",
            fontsize=30,
            fontweight="bold",
            color=TEXT_COLOR,
        )
        ax_main.text(
            0,
            -0.17,
            "calls",
            ha="center",
            va="center",
            fontsize=17,
            color=MUTED_TEXT,
            fontweight="semibold",
        )
        ax_main.set_title(
            f"{provider_title(provider)} -- full distribution",
            loc="left",
            fontsize=19,
            fontweight="bold",
            pad=8,
            color=TEXT_COLOR,
        )
        annotate_ring(
            ax_main,
            main_wedges,
            segments.main,
            total_calls=segments.total_calls,
            inside_min_share=0.09,
            outside_radius=1.24,
            min_label_gap=0.40,
            label_fontsize=16.5,
            count_is_total=True,
            colors=main_colors,
        )

        if segments.tail:
            tail_values = [stats.calls for _name, stats in segments.tail]
            tail_total = sum(tail_values)
            tail_colors = ring_colors(segments.tail, cmap_name, "Other small")
            # Label placement: when one slice dominates (codex's `shell`), its many tiny
            # neighbours are adjacent slivers that no rotation can separate -- anchor the big
            # slice on the left so the slivers spread vertically down the right side. When the
            # tail is evenly split (claude), a top start scatters the slices across both sides.
            largest_name, largest_stats = segments.tail[0]
            if tail_total and largest_stats.calls / tail_total >= 0.4:
                tail_startangle = startangle_for_segment_center(
                    segments.tail, target_name=largest_name, target_degrees=180.0
                )
            else:
                tail_startangle = 92.0
            tail_wedges, _ = ax_tail.pie(
                tail_values,
                startangle=tail_startangle,
                counterclock=False,
                colors=tail_colors,
                radius=1.0,
                wedgeprops={"width": 0.50, "edgecolor": "white", "linewidth": 1.45},
            )
            ax_tail.set_xlim(-1.22, 1.22)
            ax_tail.set_ylim(-1.20, 1.20)
            ax_tail.text(
                0,
                0.10,
                f"{tail_total / segments.total_calls * 100:.1f}%",
                ha="center",
                va="center",
                fontsize=28,
                fontweight="bold",
                color=TEXT_COLOR,
            )
            ax_tail.text(
                0,
                -0.17,
                "small tools",
                ha="center",
                va="center",
                fontsize=15,
                color=MUTED_TEXT,
                fontweight="semibold",
            )
            ax_tail.set_title(
                "Expanded small-tool slice",
                loc="left",
                fontsize=19,
                fontweight="bold",
                pad=8,
                color=TEXT_COLOR,
            )
            annotate_ring(
                ax_tail,
                tail_wedges,
                segments.tail,
                total_calls=segments.total_calls,
                inside_min_share=0.025,
                outside_radius=1.30,
                min_label_gap=0.40,
                label_fontsize=16.5,
                count_is_total=False,
                colors=tail_colors,
            )
        else:
            ax_tail.text(
                0.5,
                0.5,
                "No small-tool slice",
                ha="center",
                va="center",
                fontsize=12,
                color=MUTED_TEXT,
                transform=ax_tail.transAxes,
            )

    fig.subplots_adjust(left=0.04, right=0.965, top=0.965, bottom=0.045)
    output_dir.mkdir(parents=True, exist_ok=True)
    png_out = output_dir / "tool_call_count_rings.png"
    pdf_out = output_dir / "tool_call_count_rings.pdf"
    fig.savefig(png_out, dpi=260, bbox_inches="tight", facecolor="white")
    fig.savefig(pdf_out, bbox_inches="tight", facecolor="white")

    paper_figures_dir = PAPER_ROOT / "figures"
    if paper_figures_dir.exists():
        fig.savefig(paper_figures_dir / "tool_call_count_rings.pdf", bbox_inches="tight", facecolor="white")
        fig.savefig(paper_figures_dir / "tool_call_count_rings.png", dpi=260, bbox_inches="tight", facecolor="white")

    plt.close(fig)
    print(f"Saved {png_out}", file=sys.stderr)
    print(f"Saved {pdf_out}", file=sys.stderr)
    write_ring_segments_csv(segments_by_provider, output_dir)


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
    parser.add_argument(
        "--ring-main-min-share",
        type=float,
        default=DEFAULT_RING_MAIN_MIN_SHARE,
        help="minimum provider-local call share for a tool to stay on the main ring",
    )
    parser.add_argument(
        "--ring-tail-top-tools",
        type=int,
        default=DEFAULT_RING_TAIL_TOP_TOOLS,
        help="maximum individual tools to label in each expanded small-tool ring",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    by_provider = load_tool_counts_by_provider(con, min_calls=args.min_tool_calls_for_plot)

    plot_tool_counts(by_provider, out, args.top_tools)
    write_tool_call_counts_by_provider(by_provider, out, args.top_tools)
    plot_tool_count_rings(
        by_provider,
        out,
        main_min_share=args.ring_main_min_share,
        tail_top_tools=args.ring_tail_top_tools,
    )

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
