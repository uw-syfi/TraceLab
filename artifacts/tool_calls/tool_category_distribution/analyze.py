#!/usr/bin/env python3
"""Plot coarse tool-call category distribution and latency bars.

Reads the shared trace DuckDB (``tool_calls ⋈ rounds``) instead of re-parsing the JSONL: one
GROUP BY over ``(provider, tool_name)`` yields the per-tool aggregates, and the verbatim
``category_for_tool`` / ``presentation_category_for_tool`` maps fold those into the coarse
categories in Python (so the mapping stays identical to the old loader). Latency percentiles are
fed the same positive-latency values via a ``(tool_name, latency)`` histogram.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np

plt.rcParams["font.family"] = "Liberation Sans"

SCRIPT_DIR = Path(__file__).resolve().parent
EXP_DIR = SCRIPT_DIR
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

CATEGORY_ORDER = [
    "execute_command",
    "file_write_edit",
    "file_read_search_inspect",
    "agent_task",
    "web_remote_lookup",
    "other",
]

CATEGORY_LABELS = {
    "execute_command": "Execute command",
    "file_write_edit": "File write/edit",
    "file_read_search_inspect": "File read/search",
    "agent_task": "Agent/task",
    "web_remote_lookup": "Web/remote/lookup",
    "other": "Other",
}

CATEGORY_COLORS = {
    "execute_command": "#2440B5",
    "file_write_edit": "#F59E0B",
    "file_read_search_inspect": "#60A5FA",
    "agent_task": "#10B981",
    "web_remote_lookup": "#8B5CF6",
    "other": "#94A3B8",
}

PRESENTATION_CATEGORY_ORDER = [
    "shell_command",
    "file_edit_patch",
    "file_read_search",
    "planning_control",
    "agent_task",
    "web_lookup",
    "other",
]

PRESENTATION_LABELS = {
    "shell_command": "Shell / command",
    "file_edit_patch": "File edit / patch",
    "file_read_search": "File read / search",
    "planning_control": "Planning",
    "agent_task": "Agent / task",
    "web_lookup": "Web / lookup",
    "other": "Other",
}

PRESENTATION_COLORS = {
    "shell_command": "#0B6B43",
    "file_edit_patch": "#4E8F43",
    "file_read_search": "#87B960",
    "planning_control": "#B6D7A8",
    "agent_task": "#CFE5C7",
    "web_lookup": "#DDEDD8",
    "other": "#B8B9BD",
}

LONG_TAIL_BINS = [
    ("lt_1s", "<1s", 0.0, 1_000.0, PRESENTATION_COLORS["shell_command"]),
    ("1_10s", "1-10s", 1_000.0, 10_000.0, PRESENTATION_COLORS["file_edit_patch"]),
    ("10s_1m", "10s-1m", 10_000.0, 60_000.0, PRESENTATION_COLORS["file_read_search"]),
    ("gt_1m", ">1m", 60_000.0, None, "#CFE5C7"),
]

EXECUTE_COMMAND_TOOLS = {
    "Bash",
    "BashOutput",
    "KillBash",
    "KillShell",
    "exec",
    "exec_command",
    "write_stdin",
    "shell_command",
    "shell",
    "send_input",
    "mcp__ide__executeCode",
}

FILE_WRITE_TOOLS = {
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "apply_patch",
}

FILE_READ_TOOLS = {
    "Read",
    "Grep",
    "Glob",
    "LS",
    "view_image",
    "SendUserFile",
    "mcp__filesystem__list_directory",
    "mcp__filesystem__directory_tree",
}

AGENT_TASK_TOOLS = {
    "TaskCreate",
    "TaskOutput",
    "TaskStop",
    "TaskList",
    "TaskGet",
    "Task",
    "Agent",
    "wait_agent",
    "spawn_agent",
    "close_agent",
    "resume_agent",
}

WEB_REMOTE_LOOKUP_TOOLS = {
    "WebFetch",
    "WebSearch",
    "ToolSearch",
    "Skill",
    "mcp__sequential-thinking__sequentialthinking",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
    "mcp__ide__getDiagnostics",
    "LSP",
    "mcp__claude_ai_Notion__notion-fetch",
    "mcp__claude_ai_Notion__notion-search",
    "mcp__claude_ai_Notion__notion-create-database",
    "mcp__claude_ai_Notion__notion-create-pages",
    "mcp__claude_ai_Notion__notion-create-view",
    "mcp__codex_apps__github_fetch",
    "mcp__codex_apps__github_get_repo",
    "mcp__codex_apps__github_search_commits",
    "mcp__codex_apps__github_create_branch",
    "mcp__codex_apps__github_create_file",
    "mcp__codex_apps__github_get_user_login",
    "mcp__codex_apps__github_list_repositories",
    "mcp__codex_apps__github_search_repositories",
    "mcp__codex_apps__github_search_branches",
    "mcp__codex_apps__github_download_user_content",
    "mcp__codex_apps__github_fetch_commit",
    "mcp__codex_apps__github_compare_commits",
    "_search_branches",
}

PLANNING_CONTROL_TOOLS = {
    "TaskUpdate",
    "TodoWrite",
    "EnterPlanMode",
    "ExitPlanMode",
    "update_plan",
    "create_goal",
    "update_goal",
    "StructuredOutput",
}


# Effective tool latency precedence, expressed in SQL: internal then wall (the legacy `latency_ms`
# fallback is not present in the normalized tool_calls schema, so it never fired). NULL when neither
# is present, which maps to the Python `tool_latency_ms(...) is None` "missing" branch.
_LAT_SQL = (
    "CASE WHEN tc.tool_internal_latency_ms IS NOT NULL THEN tc.tool_internal_latency_ms "
    "WHEN tc.tool_wall_latency_ms IS NOT NULL THEN tc.tool_wall_latency_ms END"
)
# provider / tool_name normalization, matching the old Python loader exactly:
#   provider: "<unknown-provider>" when null/empty (not stripped).
#   tool_name: trim, and "<unknown-tool>" when null/blank.
_PROVIDER_SQL = (
    "CASE WHEN r.provider IS NULL OR r.provider = '' THEN '<unknown-provider>' ELSE r.provider END"
)
_TOOL_NAME_SQL = (
    "CASE WHEN tc.tool_name IS NOT NULL AND trim(tc.tool_name) <> '' "
    "THEN trim(tc.tool_name) ELSE '<unknown-tool>' END"
)


@dataclass
class CategoryStats:
    calls: int = 0
    error_calls: int = 0
    valid_latency_calls: int = 0
    missing_latency_calls: int = 0
    nonpositive_latency_calls: int = 0
    total_latency_ms: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)


@dataclass
class ToolAgg:
    """Per-(provider, tool_name) aggregate straight from one GROUP BY — the unit the category folds
    sum over. Latency counts/sum already split into valid/missing/nonpositive in SQL."""

    calls: int = 0
    error_calls: int = 0
    valid_latency_calls: int = 0
    missing_latency_calls: int = 0
    nonpositive_latency_calls: int = 0
    total_latency_ms: float = 0.0


def category_for_tool(tool_name: str) -> str:
    if tool_name in EXECUTE_COMMAND_TOOLS:
        return "execute_command"
    if tool_name in FILE_WRITE_TOOLS:
        return "file_write_edit"
    if tool_name in FILE_READ_TOOLS:
        return "file_read_search_inspect"
    if tool_name in AGENT_TASK_TOOLS:
        return "agent_task"
    if tool_name in WEB_REMOTE_LOOKUP_TOOLS:
        return "web_remote_lookup"
    return "other"


def presentation_category_for_tool(tool_name: str) -> str:
    if tool_name in EXECUTE_COMMAND_TOOLS:
        return "shell_command"
    if tool_name in FILE_WRITE_TOOLS:
        return "file_edit_patch"
    if tool_name in FILE_READ_TOOLS:
        return "file_read_search"
    if tool_name in PLANNING_CONTROL_TOOLS:
        return "planning_control"
    if tool_name in AGENT_TASK_TOOLS:
        return "agent_task"
    if tool_name in WEB_REMOTE_LOOKUP_TOOLS:
        return "web_lookup"
    return "other"


def format_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return f"{value:,}"


def format_hours(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f}h"
    if value >= 10:
        return f"{value:,.1f}h"
    return f"{value:,.2f}h"


def format_large_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 100_000:
        return f"{value / 1_000:.0f}K"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=float), q))


def _add_agg_to_stats(stats: CategoryStats, agg: ToolAgg) -> None:
    """Fold one per-tool aggregate into a coarse-category accumulator (sums are order-free over the
    integer-valued millisecond latencies, so this reproduces the old per-call summation exactly)."""
    stats.calls += agg.calls
    stats.error_calls += agg.error_calls
    stats.valid_latency_calls += agg.valid_latency_calls
    stats.missing_latency_calls += agg.missing_latency_calls
    stats.nonpositive_latency_calls += agg.nonpositive_latency_calls
    stats.total_latency_ms += agg.total_latency_ms


def load_tool_aggregates(con) -> dict[tuple[str, str], ToolAgg]:
    """One GROUP BY over ``tool_calls ⋈ rounds`` → {(provider, tool_name): ToolAgg}.

    The grouping keys are the normalized provider/tool_name (matching the old Python loader); the
    coarse category is derived later in Python so the tool→category map stays verbatim.
    """
    rows = con.execute(
        f"""
        SELECT {_PROVIDER_SQL}                                        AS provider,
               {_TOOL_NAME_SQL}                                       AS tool_name,
               count(*)                                               AS calls,
               count(*) FILTER (WHERE tc.is_error IS TRUE)            AS error_calls,
               count(*) FILTER (WHERE {_LAT_SQL} IS NOT NULL AND {_LAT_SQL} > 0)  AS valid_calls,
               count(*) FILTER (WHERE {_LAT_SQL} IS NULL)             AS missing_calls,
               count(*) FILTER (WHERE {_LAT_SQL} IS NOT NULL AND {_LAT_SQL} <= 0) AS nonpos_calls,
               COALESCE(SUM(CASE WHEN {_LAT_SQL} IS NOT NULL AND {_LAT_SQL} > 0
                                 THEN {_LAT_SQL} END), 0)             AS total_latency_ms
        FROM tool_calls tc JOIN rounds r USING (round_pk)
        GROUP BY 1, 2
        """
    ).fetchall()
    aggregates: dict[tuple[str, str], ToolAgg] = {}
    for provider, name, calls, errors, valid, missing, nonpos, total_lat in rows:
        aggregates[(provider, name)] = ToolAgg(
            calls=int(calls),
            error_calls=int(errors or 0),
            valid_latency_calls=int(valid or 0),
            missing_latency_calls=int(missing or 0),
            nonpositive_latency_calls=int(nonpos or 0),
            total_latency_ms=float(total_lat or 0),
        )
    return aggregates


def load_positive_latency_histogram(con) -> list[tuple[str, float, int]]:
    """``(tool_name, latency_ms, count)`` rows for positive effective latencies — expanded in Python
    into the same per-category latency lists ``np.percentile`` consumed before (order-free)."""
    return con.execute(
        f"""
        SELECT {_TOOL_NAME_SQL} AS tool_name,
               {_LAT_SQL}       AS latency_ms,
               count(*)         AS c
        FROM tool_calls tc
        WHERE {_LAT_SQL} IS NOT NULL AND {_LAT_SQL} > 0
        GROUP BY 1, 2
        """
    ).fetchall()


def scan_trace(
    con,
) -> tuple[dict[str, CategoryStats], dict[tuple[str, str, str], CategoryStats], Counter[str]]:
    aggregates = load_tool_aggregates(con)
    category_stats = {category: CategoryStats() for category in CATEGORY_ORDER}
    tool_stats: dict[tuple[str, str, str], CategoryStats] = defaultdict(CategoryStats)
    raw_tool_counts: Counter[str] = Counter()

    for (provider, name), agg in aggregates.items():
        category = category_for_tool(name)
        raw_tool_counts[name] += agg.calls
        for stats in (
            category_stats[category],
            tool_stats[(category, provider, name)],
        ):
            _add_agg_to_stats(stats, agg)

    return category_stats, tool_stats, raw_tool_counts


def scan_trace_presentation(con) -> dict[str, CategoryStats]:
    aggregates = load_tool_aggregates(con)
    category_stats = {category: CategoryStats() for category in PRESENTATION_CATEGORY_ORDER}
    for (_provider, name), agg in aggregates.items():
        category = presentation_category_for_tool(name)
        _add_agg_to_stats(category_stats[category], agg)

    # Latency lists (positive only) for the percentile columns, bucketed by the same verbatim map.
    for name, latency_ms, count in load_positive_latency_histogram(con):
        category = presentation_category_for_tool(name)
        category_stats[category].latencies_ms.extend([float(latency_ms)] * int(count))
    return category_stats


def long_tail_bin_for_latency(latency_ms: float) -> str:
    for key, _label, lower_ms, upper_ms, _color in LONG_TAIL_BINS:
        if latency_ms >= lower_ms and (upper_ms is None or latency_ms < upper_ms):
            return key
    raise ValueError(f"latency did not fit a long-tail bin: {latency_ms}")


def scan_trace_long_tail_latency(con) -> dict[str, CategoryStats]:
    bin_stats = {key: CategoryStats() for key, *_rest in LONG_TAIL_BINS}
    for name, latency_ms, count in load_positive_latency_histogram(con):
        latency = float(latency_ms)
        count = int(count)
        key = long_tail_bin_for_latency(latency)
        stats = bin_stats[key]
        stats.calls += count
        stats.valid_latency_calls += count
        stats.total_latency_ms += latency * count
    return bin_stats


def category_rows(category_stats: dict[str, CategoryStats]) -> list[dict[str, Any]]:
    total_calls = sum(stats.calls for stats in category_stats.values())
    total_latency_ms = sum(stats.total_latency_ms for stats in category_stats.values())
    rows = []
    for category in CATEGORY_ORDER:
        stats = category_stats[category]
        avg_latency_ms = (
            stats.total_latency_ms / stats.valid_latency_calls
            if stats.valid_latency_calls
            else 0.0
        )
        rows.append(
            {
                "category": category,
                "label": CATEGORY_LABELS[category],
                "calls": stats.calls,
                "call_share": stats.calls / total_calls if total_calls else 0.0,
                "error_calls": stats.error_calls,
                "error_rate": stats.error_calls / stats.calls if stats.calls else 0.0,
                "valid_latency_calls": stats.valid_latency_calls,
                "missing_latency_calls": stats.missing_latency_calls,
                "nonpositive_latency_calls": stats.nonpositive_latency_calls,
                "total_latency_ms": stats.total_latency_ms,
                "total_latency_hours": stats.total_latency_ms / 3_600_000.0,
                "latency_share": stats.total_latency_ms / total_latency_ms if total_latency_ms else 0.0,
                "avg_latency_ms": avg_latency_ms,
                "avg_latency_seconds": avg_latency_ms / 1_000.0,
            }
        )
    return rows


def write_category_csv(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    path = output_dir / "tool_category_summary.csv"
    fieldnames = [
        "category",
        "label",
        "calls",
        "call_share",
        "error_calls",
        "error_rate",
        "valid_latency_calls",
        "missing_latency_calls",
        "nonpositive_latency_calls",
        "total_latency_ms",
        "total_latency_hours",
        "latency_share",
        "avg_latency_ms",
        "avg_latency_seconds",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def write_tool_map_csv(tool_stats: dict[tuple[str, str, str], CategoryStats], output_dir: Path) -> Path:
    path = output_dir / "tool_category_tool_map.csv"
    fieldnames = [
        "category",
        "provider",
        "tool_name",
        "calls",
        "error_calls",
        "valid_latency_calls",
        "missing_latency_calls",
        "nonpositive_latency_calls",
        "total_latency_ms",
        "total_latency_hours",
        "avg_latency_ms",
    ]
    rows = []
    for (category, provider, name), stats in tool_stats.items():
        rows.append(
            {
                "category": category,
                "provider": provider,
                "tool_name": name,
                "calls": stats.calls,
                "error_calls": stats.error_calls,
                "valid_latency_calls": stats.valid_latency_calls,
                "missing_latency_calls": stats.missing_latency_calls,
                "nonpositive_latency_calls": stats.nonpositive_latency_calls,
                "total_latency_ms": stats.total_latency_ms,
                "total_latency_hours": stats.total_latency_ms / 3_600_000.0,
                "avg_latency_ms": (
                    stats.total_latency_ms / stats.valid_latency_calls
                    if stats.valid_latency_calls
                    else 0.0
                ),
            }
        )
    rows.sort(key=lambda row: (row["category"], -int(row["calls"]), row["provider"], row["tool_name"]))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def presentation_rows(category_stats: dict[str, CategoryStats]) -> list[dict[str, Any]]:
    total_calls = sum(stats.calls for stats in category_stats.values())
    rows: list[dict[str, Any]] = []
    for category in PRESENTATION_CATEGORY_ORDER:
        stats = category_stats[category]
        latencies_s = [value / 1000.0 for value in stats.latencies_ms]
        rows.append(
            {
                "category": category,
                "label": PRESENTATION_LABELS[category],
                "calls": stats.calls,
                "call_share": stats.calls / total_calls if total_calls else 0.0,
                "valid_latency_calls": stats.valid_latency_calls,
                "missing_latency_calls": stats.missing_latency_calls,
                "nonpositive_latency_calls": stats.nonpositive_latency_calls,
                "p25_seconds": percentile(latencies_s, 25),
                "p50_seconds": percentile(latencies_s, 50),
                "p90_seconds": percentile(latencies_s, 90),
                "p99_seconds": percentile(latencies_s, 99),
            }
        )
    return rows


def write_presentation_csv(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    path = output_dir / "tool_category_dashboard_summary.csv"
    fieldnames = [
        "category",
        "label",
        "calls",
        "call_share",
        "valid_latency_calls",
        "missing_latency_calls",
        "nonpositive_latency_calls",
        "p25_seconds",
        "p50_seconds",
        "p90_seconds",
        "p99_seconds",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def long_tail_rows(bin_stats: dict[str, CategoryStats]) -> list[dict[str, Any]]:
    total_calls = sum(stats.calls for stats in bin_stats.values())
    total_latency_ms = sum(stats.total_latency_ms for stats in bin_stats.values())
    rows: list[dict[str, Any]] = []
    for key, label, lower_ms, upper_ms, color in LONG_TAIL_BINS:
        stats = bin_stats[key]
        rows.append(
            {
                "bin": key,
                "label": label,
                "min_latency_ms": lower_ms,
                "max_latency_ms": "" if upper_ms is None else upper_ms,
                "color": color,
                "calls": stats.calls,
                "call_share": stats.calls / total_calls if total_calls else 0.0,
                "total_latency_ms": stats.total_latency_ms,
                "total_latency_hours": stats.total_latency_ms / 3_600_000.0,
                "latency_share": stats.total_latency_ms / total_latency_ms if total_latency_ms else 0.0,
            }
        )
    return rows


def write_long_tail_csv(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    path = output_dir / "tool_latency_long_tail_imbalance.csv"
    fieldnames = [
        "bin",
        "label",
        "min_latency_ms",
        "max_latency_ms",
        "color",
        "calls",
        "call_share",
        "total_latency_ms",
        "total_latency_hours",
        "latency_share",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def plot_long_tail_imbalance(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10.8, 3.7), facecolor="none")
    ax.set_facecolor("none")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")

    title_color = PRESENTATION_COLORS["shell_command"]
    text_color = "#172033"
    muted_color = "#334155"
    rule_color = "#C9D5CC"

    ax.text(
        0.018,
        0.93,
        "LONG-TAIL LATENCY IMBALANCE",
        fontsize=18,
        fontweight="bold",
        color=title_color,
        ha="left",
        va="center",
    )
    ax.text(
        0.018,
        0.85,
        "Rare long-running calls dominate total tool latency.",
        fontsize=13.5,
        color=muted_color,
        ha="left",
        va="center",
    )

    left_label_x = 0.018
    bar_x = 0.125
    bar_w = 0.855
    bar_h = 0.175
    row_specs = [
        ("Share of\ntool calls", "call_share", 0.62),
        ("Share of\ntotal tool\nlatency", "latency_share", 0.38),
    ]
    for row_label, share_key, y in row_specs:
        ax.text(
            left_label_x,
            y + bar_h / 2.0,
            row_label,
            fontsize=13.2,
            fontweight="bold",
            color=text_color,
            ha="left",
            va="center",
            linespacing=1.05,
        )
        clip = patches.FancyBboxPatch(
            (bar_x, y),
            bar_w,
            bar_h,
            boxstyle="round,pad=0,rounding_size=0.012",
            linewidth=0,
            facecolor="none",
            transform=ax.transAxes,
        )
        ax.add_patch(clip)

        cursor = bar_x
        boundaries: list[float] = []
        for row in rows:
            share = float(row[share_key])
            width = bar_w * share
            rect = patches.Rectangle(
                (cursor, y),
                width,
                bar_h,
                linewidth=0,
                facecolor=row["color"],
                transform=ax.transAxes,
            )
            rect.set_clip_path(clip)
            ax.add_patch(rect)

            center_x = cursor + width / 2.0
            label = f"{share * 100:.1f}%"
            if share >= 0.025:
                label_color = "white" if row["bin"] in {"lt_1s", "1_10s"} else title_color
                ax.text(
                    center_x,
                    y + bar_h / 2.0,
                    label,
                    fontsize=12.5 if share >= 0.07 else 10.8,
                    fontweight="bold",
                    color=label_color,
                    ha="center",
                    va="center",
                )
            cursor += width
            boundaries.append(cursor)
        for boundary in boundaries[:-1]:
            ax.plot(
                [boundary, boundary],
                [y, y + bar_h],
                color="white",
                linewidth=1.2,
                alpha=0.78,
                transform=ax.transAxes,
            )

    ax.plot([0.018, 0.982], [0.25, 0.25], color=rule_color, linewidth=1.2, transform=ax.transAxes)

    footer_y_label = 0.145
    footer_y_meta = 0.065
    block_w = 0.235
    square_w = 0.018
    for index, row in enumerate(rows):
        x = 0.06 + index * block_w
        if index:
            ax.plot(
                [x - 0.028, x - 0.028],
                [0.055, 0.205],
                color=rule_color,
                linewidth=1.0,
                transform=ax.transAxes,
            )
        ax.add_patch(
            patches.FancyBboxPatch(
                (x, footer_y_label - 0.027),
                square_w,
                0.054,
                boxstyle="round,pad=0.002,rounding_size=0.004",
                linewidth=0,
                facecolor=row["color"],
                transform=ax.transAxes,
            )
        )
        ax.text(
            x + square_w + 0.018,
            footer_y_label,
            row["label"],
            fontsize=14.0,
            fontweight="bold",
            color=title_color,
            ha="left",
            va="center",
        )
        ax.text(
            x + square_w + 0.018,
            footer_y_meta,
            f"{int(row['calls']):,} calls - {format_hours(float(row['total_latency_hours']))}",
            fontsize=11.2,
            color=text_color,
            ha="left",
            va="center",
        )

    path = output_dir / "tool_latency_long_tail_imbalance.png"
    fig.savefig(path, dpi=220, transparent=True)
    plt.close(fig)
    return path


def plot_dashboard(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    rows = sorted(rows, key=lambda row: row["calls"], reverse=True)
    total_calls = sum(int(row["calls"]) for row in rows)
    n_rows = len(rows)
    y_positions = np.arange(n_rows)[::-1]
    row_ylim = (-0.65, n_rows + 0.65)

    fig = plt.figure(figsize=(15.2, 5.1), facecolor="none")
    gs = fig.add_gridspec(1, 3, width_ratios=[1.24, 1.04, 1.54], wspace=0.03)
    ax_donut = fig.add_subplot(gs[0, 0])
    ax_table = fig.add_subplot(gs[0, 1])
    ax_latency = fig.add_subplot(gs[0, 2])
    for ax in (ax_donut, ax_table, ax_latency):
        ax.set_facecolor("none")

    # Left: donut.
    values = [row["calls"] for row in rows]
    colors = [PRESENTATION_COLORS[row["category"]] for row in rows]
    wedges, _ = ax_donut.pie(
        values,
        startangle=300,
        counterclock=False,
        colors=colors,
        radius=1.24,
        wedgeprops={"width": 0.50, "edgecolor": "white", "linewidth": 1.4},
    )
    for wedge, row in zip(wedges, rows, strict=True):
        share = row["call_share"]
        if share < 0.018:
            continue
        theta = np.deg2rad((wedge.theta1 + wedge.theta2) / 2.0)
        radius = 0.99
        text_color = "white" if share > 0.05 else "#172033"
        ax_donut.text(
            radius * np.cos(theta),
            radius * np.sin(theta),
            f"{share * 100:.0f}%",
            ha="center",
            va="center",
            fontsize=19,
            color=text_color,
            fontweight="bold",
        )
    ax_donut.text(
        0,
        0.08,
        format_large_count(total_calls),
        ha="center",
        va="center",
        fontsize=36,
        color=PRESENTATION_COLORS["shell_command"],
        fontweight="bold",
    )
    ax_donut.text(
        0,
        -0.18,
        "tool calls",
        ha="center",
        va="center",
        fontsize=18,
        color="#172033",
        fontweight="bold",
    )
    ax_donut.set(aspect="equal")
    ax_donut.axis("off")

    # Middle: category table.
    ax_table.set_xlim(0, 1)
    ax_table.set_ylim(*row_ylim)
    ax_table.axis("off")
    ax_table.text(
        0.11,
        n_rows + 0.23,
        "CATEGORY",
        fontsize=16,
        fontweight="bold",
        color=PRESENTATION_COLORS["shell_command"],
        va="center",
    )
    ax_table.text(
        0.96,
        n_rows + 0.23,
        "COUNT",
        fontsize=16,
        fontweight="bold",
        color=PRESENTATION_COLORS["shell_command"],
        va="center",
        ha="right",
    )
    ax_table.hlines(n_rows - 0.24, 0.0, 1.0, color="#D1D5DB", linewidth=1.1)
    for y, row in zip(y_positions, rows, strict=True):
        ax_table.hlines(y - 0.5, 0.0, 1.0, color="#D1D5DB", linewidth=1.1)
        color = PRESENTATION_COLORS[row["category"]]
        ax_table.add_patch(
            patches.FancyBboxPatch(
                (0.0, y - 0.18),
                0.075,
                0.36,
                boxstyle="round,pad=0.02,rounding_size=0.025",
                linewidth=0,
                facecolor=color,
            )
        )
        ax_table.text(
            0.14,
            y,
            row["label"],
            fontsize=16.5,
            color="#172033",
            va="center",
            fontweight="semibold",
        )
        ax_table.text(
            0.96,
            y,
            f"{row['calls']:,}",
            fontsize=16.5,
            color="#172033",
            va="center",
            ha="right",
            fontweight="semibold",
        )
    # Right: latency quantiles.
    ax_latency.set_xscale("log")
    max_p99 = max(float(row["p99_seconds"]) for row in rows) if rows else 10_000.0
    x_max = 100_000 if max_p99 > 10_000 else 10_000
    ax_latency.set_xlim(0.01, x_max)
    ax_latency.set_ylim(*row_ylim)
    ax_latency.set_yticks([])
    ax_latency.hlines(
        n_rows - 0.24,
        0.01,
        x_max,
        color="#D1D5DB",
        linewidth=1.1,
        zorder=0,
    )
    ax_latency.grid(True, axis="x", color="#D7DDD7", linestyle="--", linewidth=0.65, alpha=0.65)
    ax_latency.grid(True, axis="y", color="#ECEFED", linewidth=0.8, alpha=0.9)
    ax_latency.set_axisbelow(True)
    for spine in ["top", "right", "left"]:
        ax_latency.spines[spine].set_visible(False)
    ax_latency.spines["bottom"].set_color("#9CA3AF")
    ax_latency.tick_params(axis="x", labelsize=13.5, colors="#172033")
    x_ticks = [0.01, 0.1, 1, 10, 100, 1000]
    x_labels = ["10ms", "100ms", "1s", "10s", "100s", "1000s"]
    if x_max > 10_000:
        x_ticks.append(100_000)
        x_labels.append("100000s")
    ax_latency.set_xticks(x_ticks)
    ax_latency.set_xticklabels(x_labels)
    ax_latency.set_xlabel("")
    ax_latency.text(
        0.5,
        n_rows + 0.23,
        "LATENCY DISTRIBUTION",
        transform=ax_latency.get_yaxis_transform(),
        ha="center",
        va="center",
        fontsize=16,
        fontweight="bold",
        color=PRESENTATION_COLORS["shell_command"],
    )

    q25_color = "#A8CE90"
    q50_color = PRESENTATION_COLORS["shell_command"]
    q90_color = "#8FBD64"
    q99_color = "#C9DCC2"
    for y, row in zip(y_positions, rows, strict=True):
        p25 = max(float(row["p25_seconds"]), 0.01)
        p50 = max(float(row["p50_seconds"]), 0.01)
        p90 = max(float(row["p90_seconds"]), 0.01)
        p99 = max(float(row["p99_seconds"]), 0.01)
        ax_latency.hlines(y, p25, p99, color="#6E9D70", linewidth=1.25, alpha=0.78)
        ax_latency.scatter([p25], [y], s=84, color=q25_color, zorder=3)
        ax_latency.scatter([p50], [y], s=90, color=q50_color, zorder=4)
        ax_latency.scatter([p90], [y], s=84, color=q90_color, zorder=3)
        ax_latency.scatter([p99], [y], s=84, color=q99_color, zorder=3)

    legend_handles = [
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=q25_color, markeredgecolor=q25_color, markersize=8, label="p25"),
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=q50_color, markeredgecolor=q50_color, markersize=8, label="p50"),
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=q90_color, markeredgecolor=q90_color, markersize=8, label="p90"),
        plt.Line2D([0], [0], marker="o", color="none", markerfacecolor=q99_color, markeredgecolor=q99_color, markersize=8, label="p99"),
    ]
    ax_latency.legend(
        handles=legend_handles,
        loc="upper right",
        bbox_to_anchor=(0.985, 0.87),
        ncol=1,
        frameon=False,
        fontsize=13,
        handletextpad=0.45,
        borderaxespad=0.0,
    )

    fig.subplots_adjust(left=0.018, right=0.985, top=0.90, bottom=0.10, wspace=0.03)
    header_y_display = ax_table.transData.transform((0, n_rows - 0.24))[1]
    header_y_fig = fig.transFigure.inverted().transform((0, header_y_display))[1]
    fig.add_artist(
        plt.Line2D(
            [ax_table.get_position().x0, ax_latency.get_position().x1],
            [header_y_fig, header_y_fig],
            transform=fig.transFigure,
            color="#D1D5DB",
            linewidth=1.1,
            zorder=0,
        )
    )
    path = output_dir / "tool_category_dashboard.png"
    fig.savefig(path, dpi=220, transparent=True)
    plt.close(fig)
    return path


def plot_count_ring(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    rows = sorted(rows, key=lambda row: row["calls"], reverse=True)
    values = [row["calls"] for row in rows]
    colors = [CATEGORY_COLORS[row["category"]] for row in rows]
    total_calls = sum(values)

    fig, ax = plt.subplots(figsize=(9.8, 6.4))
    fig.patch.set_facecolor("white")
    wedges, _texts = ax.pie(
        values,
        startangle=92,
        counterclock=False,
        colors=colors,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 2.2},
    )
    ax.text(
        0,
        0.08,
        format_count(total_calls),
        ha="center",
        va="center",
        fontsize=22,
        fontweight="bold",
        color="#172033",
    )
    ax.text(
        0,
        -0.14,
        "tool calls",
        ha="center",
        va="center",
        fontsize=13,
        color="#526070",
    )
    legend_labels = [
        f"{row['label']}  {format_count(row['calls'])}  ({row['call_share'] * 100:.1f}%)"
        for row in rows
    ]
    ax.legend(
        wedges,
        legend_labels,
        loc="center left",
        bbox_to_anchor=(0.92, 0.5),
        frameon=False,
        fontsize=12.5,
        handlelength=1.2,
        handletextpad=0.7,
    )
    ax.set_title("Tool Call Count Distribution", fontsize=20, fontweight="bold", pad=16)
    ax.set(aspect="equal")
    fig.tight_layout()

    path = output_dir / "tool_category_count_ring.png"
    fig.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return path


def plot_latency_bar(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    rows = sorted(rows, key=lambda row: row["total_latency_hours"], reverse=True)
    labels = [row["label"] for row in rows]
    values = [row["total_latency_hours"] for row in rows]
    colors = [CATEGORY_COLORS[row["category"]] for row in rows]
    y = np.arange(len(rows))

    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    fig.patch.set_facecolor("white")
    bars = ax.barh(y, values, color=colors, alpha=0.95)
    ax.invert_yaxis()
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=13)
    ax.set_xlabel("Summed effective tool latency (hours)", fontsize=14, labelpad=10)
    ax.set_title("Tool Latency by Category", fontsize=20, fontweight="bold", pad=16)
    ax.grid(True, axis="x", color="#CBD5E1", linewidth=0.9, alpha=0.72)
    ax.set_axisbelow(True)
    max_value = max(values) if values else 1.0
    ax.set_xlim(0, max_value * 1.18)
    for bar, row in zip(bars, rows, strict=True):
        x = bar.get_width()
        avg_s = row["avg_latency_seconds"]
        label = f"{format_hours(x)}  avg {avg_s:.1f}s"
        ax.text(
            x + max_value * 0.015,
            bar.get_y() + bar.get_height() / 2,
            label,
            va="center",
            ha="left",
            fontsize=12,
            color="#172033",
        )
    for spine in ["top", "right", "left"]:
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#CBD5E1")
    ax.tick_params(axis="x", labelsize=12, colors="#526070")
    ax.tick_params(axis="y", length=0, colors="#172033")
    fig.tight_layout()

    path = output_dir / "tool_category_latency_bar.png"
    fig.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return path


def write_results(
    rows: list[dict[str, Any]],
    raw_tool_counts: Counter[str],
    input_label: str,
    output_dir: Path,
    count_plot: Path,
    latency_plot: Path,
    dashboard_plot: Path,
    long_tail_plot: Path,
    category_csv: Path,
    tool_map_csv: Path,
    dashboard_csv: Path,
    long_tail_csv: Path,
    long_tail_summary_rows: list[dict[str, Any]],
) -> None:
    lines = [
        "# Tool Category Distribution",
        "",
        f"Input: `{input_label}`",
        f"Output dir: `{output_dir}`",
        "",
        "Classification: 5 explicit categories plus `other`, merging Claude and Codex tool names by function.",
        "Dashboard presentation classification: `Shell / command`, `File edit / patch`, `File read / search`, `Planning`, `Agent / task`, `Web / lookup`, and `Other`; `Other` is only user/schedule/workspace misc after agent tools are split out.",
        "Latency: same precedence as `scripts/plot_trace_stats.py`: `tool_internal_latency_ms`, then `tool_wall_latency_ms`, then `latency_ms`; only positive latencies contribute to summed latency.",
        "",
        "Generated figures:",
        f"- `{count_plot}`",
        f"- `{latency_plot}`",
        f"- `{dashboard_plot}`",
        f"- `{long_tail_plot}`",
        "",
        "Generated CSVs:",
        f"- `{category_csv}`",
        f"- `{tool_map_csv}`",
        f"- `{dashboard_csv}`",
        f"- `{long_tail_csv}`",
        "",
        f"Unique tool names: `{len(raw_tool_counts)}`",
        f"Total tool calls: `{sum(raw_tool_counts.values()):,}`",
        "",
        "Category summary:",
        "",
        "| category | calls | call share | total latency | latency share | avg valid latency |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {category} | {calls:,} | {call_share:.2%} | {hours} | {latency_share:.2%} | {avg:.2f}s |".format(
                category=row["category"],
                calls=row["calls"],
                call_share=row["call_share"],
                hours=format_hours(row["total_latency_hours"]),
                latency_share=row["latency_share"],
                avg=row["avg_latency_seconds"],
            )
        )
    lines.extend(
        [
            "",
            "Long-tail latency imbalance from merged data:",
            "",
            "| bin | positive-latency calls | call share | total latency | latency share |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in long_tail_summary_rows:
        lines.append(
            "| {label} | {calls:,} | {call_share:.2%} | {hours} | {latency_share:.2%} |".format(
                label=row["label"],
                calls=row["calls"],
                call_share=row["call_share"],
                hours=format_hours(row["total_latency_hours"]),
                latency_share=row["latency_share"],
            )
        )
    (output_dir / "result_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    input_label = str(args.db) if getattr(args, "db", None) is not None else str(args.input)

    category_stats, tool_stats, raw_tool_counts = scan_trace(con)
    rows = category_rows(category_stats)
    category_csv = write_category_csv(rows, output_dir)
    tool_map_csv = write_tool_map_csv(tool_stats, output_dir)
    count_plot = plot_count_ring(rows, output_dir)
    latency_plot = plot_latency_bar(rows, output_dir)

    dashboard_stats = scan_trace_presentation(con)
    dashboard_rows = presentation_rows(dashboard_stats)
    dashboard_csv = write_presentation_csv(dashboard_rows, output_dir)
    dashboard_plot = plot_dashboard(dashboard_rows, output_dir)

    long_tail_stats = scan_trace_long_tail_latency(con)
    long_tail_summary_rows = long_tail_rows(long_tail_stats)
    long_tail_csv = write_long_tail_csv(long_tail_summary_rows, output_dir)
    long_tail_plot = plot_long_tail_imbalance(long_tail_summary_rows, output_dir)
    write_results(
        rows,
        raw_tool_counts,
        input_label,
        output_dir,
        count_plot,
        latency_plot,
        dashboard_plot,
        long_tail_plot,
        category_csv,
        tool_map_csv,
        dashboard_csv,
        long_tail_csv,
        long_tail_summary_rows,
    )
    png_sidecar.make_self_contained(
        output_dir,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=SCRIPT_DIR / "README.md",
    )
    print(f"All outputs saved to {output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
