#!/usr/bin/env python3
"""Tool-call effective-latency distribution: per-tool box/quantiles, weighted bins, CDFs.

See README.md for how effective latency is defined and how rare tools are collapsed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from dataclasses import dataclass, field  # noqa: E402
from typing import Any  # noqa: E402
import csv  # noqa: E402
import math  # noqa: E402
import matplotlib  # noqa: E402
import numpy as np  # noqa: E402
from style import (
    AXIS_COLOR,
    BOX_EDGE,
    BOX_FACE,
    MUTED_TEXT,
    TEXT_COLOR,
    mticker,
    plot_color,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
    short_label,
)  # noqa: E402
from accumulators import ToolLatencyBinStats, make_tool_latency_bins  # noqa: E402
from formatters import (
    CDF_REFERENCE_MS,
    bin_edges_with_reference,
    fine_latency_bin_edges,
    format_hours_compact,
    format_hours_tick,
    format_latency_compact,
    format_latency_tick,
    latency_ticks,
    tool_latency_boundaries_ms,
)  # noqa: E402
from cdf import (
    active_bin_mask,
    annotate_cumulative_time_reference,
    plot_count_cdf_by_provider,
    plot_stacked_share_panels,
    write_count_cdf_by_provider,
)  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

DEFAULT_TOP_TOOLS = 30
DEFAULT_MIN_TOOL_CALLS = 20  # tools with fewer provider-local calls collapse into "Other"

# matplotlib 3.9 renamed boxplot's ``labels`` kwarg to ``tick_labels``. Support both so the
# toolkit also runs unchanged under the older matplotlib (3.8.x) bundled with Pyodide, which
# powers the web Analyze tab — otherwise this figure raises and never renders in the browser.
_BOXPLOT_LABEL_KW = (
    "tick_labels"
    if tuple(int(p) for p in matplotlib.__version__.split(".")[:2]) >= (3, 9)
    else "labels"
)

# The collapsed "Other" bucket sorts after any real first-appearance ordinal, so it lands last —
# matching the old loader, which appended it after the alphabetically-merged real tools.
_OTHER_FIRST_SEEN = 1 << 62

# Effective latency = ``tool_internal_latency_ms`` else ``tool_wall_latency_ms`` (the shared
# precedence). Only *strictly positive* effective latencies feed the quantile/sum stats, matching
# the old ``ToolStats`` (positive→sampler/latency_sum; NULL→missing; <=0→nonpositive).
_EFF = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL

# Trimmed tool name; blank/whitespace/NULL folds to ``<unknown-tool>`` (old ``tool_name()`` helper).
_TNAME = (
    "CASE WHEN tc.tool_name IS NULL OR trim(tc.tool_name) = '' "
    "THEN '<unknown-tool>' ELSE trim(tc.tool_name) END"
)


@dataclass
class ToolStats:
    """Per-tool latency figures, holding the *exact* set of positive effective latencies.

    DuckDB lets us pull every positive effective-latency value per tool (the old loader
    reservoir-sampled at 50k/tool to bound memory while parsing JSON), so quartiles/percentiles are
    exact rather than sampled. ``first_seen`` is the global ordinal of the tool's first call (file
    order) — the deterministic tie-break for equal call counts, not a plotted value.
    """

    values: list[float] = field(default_factory=list)
    calls: int = 0
    latency_count: int = 0
    missing_latency: int = 0
    nonpositive_latency: int = 0
    error_calls: int = 0
    latency_sum: float = 0.0
    latency_min: float | None = None
    latency_max: float | None = None
    providers: dict[str, int] = field(default_factory=dict)
    first_seen: int = _OTHER_FIRST_SEEN

    def summary(self) -> dict[str, Any]:
        arr = np.asarray(self.values, dtype=float)
        if arr.size:
            p50, p90, p99 = (float(v) for v in np.percentile(arr, [50, 90, 99]))
        else:
            p50 = p90 = p99 = None
        return {
            "calls": self.calls,
            "latency_count": self.latency_count,
            "missing_latency": self.missing_latency,
            "nonpositive_latency": self.nonpositive_latency,
            "error_calls": self.error_calls,
            "mean_ms": self.latency_sum / self.latency_count if self.latency_count else None,
            "min_ms": self.latency_min,
            "max_ms": self.latency_max,
            "p50_ms": p50,
            "p90_ms": p90,
            "p99_ms": p99,
            # Exact: the "sample" is the full set of positive latencies; never reservoir-truncated.
            "sample_count": int(arr.size),
            "sampled": False,
            "providers": dict(sorted(self.providers.items())),
        }


def _per_tool_query(plot_name_expr: str, *, by_provider: bool) -> str:
    """Pull every positive effective latency value per ``plot_name_expr`` bucket, plus counts.

    ``list(...)`` collects the exact positive-latency values (no reservoir). ``min(call_ord)`` is the
    first-appearance ordinal (file order) used as the equal-call-count tie-break. When
    ``by_provider`` is set, rows additionally carry ``rounds.provider`` and the GROUP BY splits per
    provider; otherwise the provider→count map is aggregated for the CSV ``providers`` column.
    """
    provider_select = "provider AS provider,\n               " if by_provider else ""
    group_by = f"provider, {plot_name_expr}" if by_provider else plot_name_expr
    # Per-provider rows split per provider, so each carries a single-provider count map implicitly;
    # the global CSV needs the per-tool provider histogram (the ``providers`` column).
    providers_select = (
        "" if by_provider else "histogram(provider)                          AS providers,\n               "
    )
    return f"""
        WITH ordered AS (
            SELECT
                {_TNAME}                                               AS tname,
                COALESCE(r.provider, '<unknown-provider>')             AS provider,
                tc.is_error                                            AS is_error,
                ({_EFF})                                               AS eff,
                row_number() OVER (ORDER BY tc.round_pk, tc.tool_index) AS call_ord
            FROM tool_calls tc JOIN rounds r USING (round_pk)
        )
        SELECT {provider_select}{plot_name_expr}                      AS plot_name,
               {providers_select}count(*)                             AS calls,
               count(*) FILTER (WHERE eff IS NOT NULL AND eff > 0)    AS latency_count,
               count(*) FILTER (WHERE eff IS NULL)                    AS missing_latency,
               count(*) FILTER (WHERE eff IS NOT NULL AND eff <= 0)   AS nonpositive_latency,
               count(*) FILTER (WHERE is_error)                       AS error_calls,
               sum(CASE WHEN eff IS NOT NULL AND eff > 0 THEN eff ELSE 0 END) AS latency_sum,
               min(eff) FILTER (WHERE eff IS NOT NULL AND eff > 0)    AS latency_min,
               max(eff) FILTER (WHERE eff IS NOT NULL AND eff > 0)    AS latency_max,
               list(eff) FILTER (WHERE eff IS NOT NULL AND eff > 0)   AS latency_values,
               min(call_ord)                                          AS first_seen
        FROM ordered
        GROUP BY {group_by}
    """


def load_tool_stats(con) -> dict[str, ToolStats]:
    """Global ``{tool_name: ToolStats}`` for the CSV — raw tool names, no aliasing/collapsing.

    Inserted in first-appearance (file) order so the stable ``sorted(..., key=calls, reverse=True)``
    in ``write_tool_summary`` reproduces the old dict-order tie-break for equal call counts.
    """
    rows = con.execute(_per_tool_query("tname", by_provider=False)).fetchall()
    parsed: list[tuple[int, str, ToolStats]] = []
    for (
        plot_name,
        providers,
        calls,
        latency_count,
        missing_latency,
        nonpositive_latency,
        error_calls,
        latency_sum,
        latency_min,
        latency_max,
        latency_values,
        first_seen,
    ) in rows:
        parsed.append(
            (
                int(first_seen),
                plot_name,
                ToolStats(
                    values=[float(v) for v in (latency_values or [])],
                    calls=int(calls),
                    latency_count=int(latency_count),
                    missing_latency=int(missing_latency),
                    nonpositive_latency=int(nonpositive_latency),
                    error_calls=int(error_calls or 0),
                    latency_sum=float(latency_sum or 0.0),
                    latency_min=None if latency_min is None else float(latency_min),
                    latency_max=None if latency_max is None else float(latency_max),
                    providers={k: int(v) for k, v in (providers or {}).items()},
                    first_seen=int(first_seen),
                ),
            )
        )
    parsed.sort(key=lambda item: item[0])  # file order -> stable dict insertion order
    return {plot_name: stats for _ord, plot_name, stats in parsed}


def load_tool_stats_by_provider(con, *, min_calls: int) -> dict[str, dict[str, ToolStats]]:
    """Per-provider ``{plot_tool_name: ToolStats}`` for the boxplot, mcp merged + rare collapsed.

    Mirrors the old ``plot_ready_tool_stats_by_provider``: alias every ``mcp_*`` tool to ``mcp`` (in
    SQL), insert tools in **alphabetical** plot-name order (the old ``sorted(groups.items())`` in
    ``merge_tool_stats_for_plot``), then collapse tools below ``min_calls`` provider-local calls into
    one ``Other (<N calls/tool)`` bucket appended last. That insertion order is the stable tie-break
    for equal call counts in the boxplot's ``candidates.sort(key=calls)``.
    """
    plot_name_expr = (
        "CASE WHEN tname LIKE 'mcp\\_%' ESCAPE '\\' THEN 'mcp' ELSE tname END"
    )
    rows = con.execute(_per_tool_query(plot_name_expr, by_provider=True)).fetchall()

    by_provider: dict[str, dict[str, tuple[str, ToolStats]]] = {}
    for (
        provider,
        plot_name,
        calls,
        latency_count,
        missing_latency,
        nonpositive_latency,
        error_calls,
        latency_sum,
        latency_min,
        latency_max,
        latency_values,
        first_seen,
    ) in rows:
        bucket = by_provider.setdefault(provider, {})
        bucket[plot_name] = (
            plot_name,
            ToolStats(
                values=[float(v) for v in (latency_values or [])],
                calls=int(calls),
                latency_count=int(latency_count),
                missing_latency=int(missing_latency),
                nonpositive_latency=int(nonpositive_latency),
                error_calls=int(error_calls or 0),
                latency_sum=float(latency_sum or 0.0),
                latency_min=None if latency_min is None else float(latency_min),
                latency_max=None if latency_max is None else float(latency_max),
                providers={provider: int(calls)},
                first_seen=int(first_seen),
            ),
        )

    result: dict[str, dict[str, ToolStats]] = {}
    other_label = f"Other (<{min_calls} calls/tool)"
    for provider, tools in by_provider.items():
        # Alphabetical plot-name order reproduces the old merged-dict insertion order.
        ordered = sorted(tools.values(), key=lambda item: item[0])
        kept: dict[str, ToolStats] = {}
        other: ToolStats | None = None
        for name, stats in ordered:
            if min_calls > 1 and stats.calls < min_calls:
                if other is None:
                    other = ToolStats(providers={})
                other.calls += stats.calls
                other.latency_count += stats.latency_count
                other.missing_latency += stats.missing_latency
                other.nonpositive_latency += stats.nonpositive_latency
                other.error_calls += stats.error_calls
                other.latency_sum += stats.latency_sum
                other.values.extend(stats.values)
                for prov, cnt in stats.providers.items():
                    other.providers[prov] = other.providers.get(prov, 0) + cnt
                if stats.latency_min is not None:
                    other.latency_min = (
                        stats.latency_min
                        if other.latency_min is None
                        else min(other.latency_min, stats.latency_min)
                    )
                if stats.latency_max is not None:
                    other.latency_max = (
                        stats.latency_max
                        if other.latency_max is None
                        else max(other.latency_max, stats.latency_max)
                    )
            else:
                kept[name] = stats
        if other is not None and other.calls:
            kept[other_label] = other
        result[provider] = kept
    return result


def load_tool_latency_values_by_provider(con) -> dict[str, list[float]]:
    """``{provider: [positive effective latency, ...]}`` for the two CDF outputs.

    Every positive effective latency, exact (no reservoir). Within a provider the values are emitted
    in (round_pk, tool_index) file order, matching the old single-pass append order — though the CDF
    consumers only histogram/percentile the multiset, so order does not affect their output.
    """
    rows = con.execute(
        f"""
        SELECT COALESCE(r.provider, '<unknown-provider>') AS provider, ({_EFF}) AS eff
        FROM tool_calls tc JOIN rounds r USING (round_pk)
        WHERE ({_EFF}) IS NOT NULL AND ({_EFF}) > 0
        ORDER BY tc.round_pk, tc.tool_index
        """
    ).fetchall()
    by_provider: dict[str, list[float]] = {}
    for provider, eff in rows:
        by_provider.setdefault(provider, []).append(float(eff))
    return by_provider


def load_tool_latency_bins(con, *, by_provider: bool) -> Any:
    """Coarse latency-bin stats (the 8 ``TOOL_LATENCY_BINS_MS`` half-open bins).

    Bins each positive effective latency by half-open ``[lo, hi)`` and sums total latency + counts
    (calls / errors), matching the old ``ToolLatencyBinStats`` accumulation. Returns a flat
    ``list[ToolLatencyBinStats]`` (global) or ``{provider: list[ToolLatencyBinStats]}``.
    """
    # SQL bucket index: find the bin whose [lo, hi) contains eff. Bins are contiguous so the index is
    # the count of lower bounds the value meets, minus one.
    bins_template = make_tool_latency_bins()
    los = [b.lo_ms for b in bins_template]
    # case expression mapping eff -> 0-based bin index.
    case_when = "\n".join(
        f"                     WHEN eff >= {lo} AND ({'TRUE' if hi is None else f'eff < {hi}'}) THEN {idx}"
        for idx, (lo, hi) in enumerate((b.lo_ms, b.hi_ms) for b in bins_template)
    )
    provider_select = "provider AS provider, " if by_provider else ""
    provider_join = "JOIN rounds r USING (round_pk)" if by_provider else ""
    group_by = "provider, bin_index" if by_provider else "bin_index"
    rows = con.execute(
        f"""
        WITH binned AS (
            SELECT {("COALESCE(r.provider, '<unknown-provider>') AS provider, " if by_provider else '')}
                   tc.is_error AS is_error,
                   ({_EFF}) AS eff,
                   CASE
{case_when}
                   END AS bin_index
            FROM tool_calls tc {provider_join}
            WHERE ({_EFF}) IS NOT NULL AND ({_EFF}) > 0
        )
        SELECT {provider_select}bin_index,
               count(*)                          AS tool_calls,
               count(*) FILTER (WHERE is_error)  AS error_calls,
               sum(eff)                          AS total_latency_ms
        FROM binned
        GROUP BY {group_by}
        """
    ).fetchall()

    def empty_bins() -> list[ToolLatencyBinStats]:
        return make_tool_latency_bins()

    if by_provider:
        out: dict[str, list[ToolLatencyBinStats]] = {}
        for provider, bin_index, tool_calls, error_calls, total_latency in rows:
            bins = out.setdefault(provider, empty_bins())
            b = bins[int(bin_index)]
            b.tool_calls = int(tool_calls)
            b.error_calls = int(error_calls or 0)
            b.total_latency_ms = float(total_latency or 0.0)
        return out

    bins = empty_bins()
    for bin_index, tool_calls, error_calls, total_latency in rows:
        b = bins[int(bin_index)]
        b.tool_calls = int(tool_calls)
        b.error_calls = int(error_calls or 0)
        b.total_latency_ms = float(total_latency or 0.0)
    return bins


def plot_tool_latency(
    tool_stats_by_provider: dict[str, dict[str, ToolStats]],
    output_dir: Path,
    top_tools: int,
) -> None:
    provider_panels: list[
        tuple[str, list[tuple[str, ToolStats]], list[list[float]]]
    ] = []
    global_max_latency = 1.0
    for provider in provider_order(tool_stats_by_provider):
        candidates = [
            (name, stats)
            for name, stats in tool_stats_by_provider[provider].items()
            if stats.latency_count > 0 and stats.values
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[1].calls, reverse=True)
        selected = candidates[:top_tools]
        selected.sort(
            key=lambda item: np.median(item[1].values),
            reverse=True,
        )
        data = [list(stats.values) for _name, stats in selected]
        global_max_latency = max(
            global_max_latency,
            max((max(values) for values in data if values), default=1.0),
        )
        provider_panels.append((provider, selected, data))
    if not provider_panels:
        return

    panel_heights = [
        max(5.2, min(14.5, 0.43 * len(selected) + 2.0))
        for _provider, selected, _data in provider_panels
    ]
    fig, axes = plt.subplots(
        len(provider_panels),
        1,
        figsize=(15.0, sum(panel_heights)),
        squeeze=False,
        gridspec_kw={"height_ratios": panel_heights},
    )
    fig.suptitle("Tool Call Latency by Tool and Provider", y=0.998, fontsize=18)

    for ax, (provider, selected, data) in zip(
        axes.ravel(), provider_panels, strict=True
    ):
        labels = [
            f"{short_label(name, 34)}  (n={stats.calls:,})" for name, stats in selected
        ]
        ax.set_title(
            f"{provider_title(provider)} - top {len(selected)} by call count",
            loc="left",
            pad=10,
            fontsize=15,
        )
        ax.set_xlabel("Latency (log scale)", fontsize=18, labelpad=10)
        ax.set_xscale("log")
        ax.set_xticks(latency_ticks(global_max_latency))
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_latency_tick))
        ax.xaxis.set_minor_formatter(mticker.NullFormatter())
        polish_axes(ax, grid_axis="x", minor=True)

        box = ax.boxplot(
            data,
            vert=False,
            **{_BOXPLOT_LABEL_KW: labels},
            showfliers=False,
            whis=(5, 95),
            patch_artist=True,
            widths=0.62,
            medianprops={"color": TEXT_COLOR, "linewidth": 1.4},
            whiskerprops={"color": MUTED_TEXT, "linewidth": 1.0},
            capprops={"color": MUTED_TEXT, "linewidth": 1.0},
        )
        for patch in box["boxes"]:
            patch.set_facecolor(BOX_FACE)
            patch.set_edgecolor(BOX_EDGE)
            patch.set_alpha(0.82)
            patch.set_linewidth(1.0)

        ax.tick_params(axis="y", labelsize=13.5)
        ax.tick_params(axis="x", labelsize=16, pad=6)

    fig.tight_layout(rect=(0, 0, 1, 0.992), h_pad=1.0)
    out = output_dir / "tool_latency_by_tool.png"
    save_plot(fig, out)


def plot_tool_latency_top12_wide(
    tool_stats_by_provider: dict[str, dict[str, ToolStats]],
    output_dir: Path,
) -> None:
    """Paper-sized side-by-side top-tool latency plot, without replacing the full plot."""
    provider_panels: list[
        tuple[str, list[tuple[str, ToolStats]], list[list[float]]]
    ] = []
    for provider in provider_order(tool_stats_by_provider):
        candidates = [
            (name, stats)
            for name, stats in tool_stats_by_provider[provider].items()
            if stats.latency_count > 0 and stats.values
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: item[1].calls, reverse=True)
        selected = candidates[:12]
        selected.sort(key=lambda item: np.median(item[1].values), reverse=True)
        data = [list(stats.values) for _name, stats in selected]
        provider_panels.append((provider, selected, data))
    if not provider_panels:
        return

    x_min = 30.0
    x_max = 600_000.0
    x_ticks = [100.0, 1_000.0, 10_000.0, 60_000.0, 600_000.0]
    fig, axes = plt.subplots(
        1,
        len(provider_panels),
        figsize=(7.15, 4.25),
        squeeze=False,
        sharex=True,
    )

    for ax, (provider, selected, data) in zip(
        axes.ravel(), provider_panels, strict=True
    ):
        labels = [
            f"{short_label(name, 20)} ({stats.calls:,})" for name, stats in selected
        ]
        ax.set_title(f"{provider_title(provider)} top 12", loc="left", pad=5, fontsize=8.8)
        ax.set_xscale("log")
        ax.set_xlim(x_min, x_max)
        ax.set_xticks(x_ticks)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_latency_tick))
        ax.xaxis.set_minor_formatter(mticker.NullFormatter())
        polish_axes(ax, grid_axis="x", minor=True)

        box = ax.boxplot(
            data,
            vert=False,
            **{_BOXPLOT_LABEL_KW: labels},
            showfliers=False,
            whis=(5, 95),
            patch_artist=True,
            widths=0.58,
            medianprops={"color": TEXT_COLOR, "linewidth": 0.95},
            whiskerprops={"color": MUTED_TEXT, "linewidth": 0.75},
            capprops={"color": MUTED_TEXT, "linewidth": 0.75},
        )
        for patch in box["boxes"]:
            patch.set_facecolor(BOX_FACE)
            patch.set_edgecolor(BOX_EDGE)
            patch.set_alpha(0.82)
            patch.set_linewidth(0.75)

        ax.tick_params(axis="y", labelsize=6.4, pad=1.5)
        ax.tick_params(axis="x", labelsize=7.0, pad=2)

    for ax in axes.ravel():
        ax.set_xlabel("Effective latency (log scale)", fontsize=7.6, labelpad=4)

    fig.subplots_adjust(left=0.125, right=0.995, top=0.92, bottom=0.13, wspace=0.55)
    pdf_out = output_dir / "tool_latency_by_tool_top12_wide.pdf"
    png_out = output_dir / "tool_latency_by_tool_top12_wide.png"
    fig.savefig(pdf_out, bbox_inches="tight", facecolor="white")
    print(f"Saved {pdf_out}", file=sys.stderr)
    save_plot(fig, png_out)


def plot_tool_latency_weighted_bins(
    bins_by_provider: dict[str, list[ToolLatencyBinStats]],
    output_dir: Path,
    *,
    compact: bool = False,
    out_name: str = "tool_latency_weighted_bins.png",
) -> None:
    provider_panels = [
        (provider, bins)
        for provider, bins in (
            (provider, bins_by_provider[provider])
            for provider in provider_order(bins_by_provider)
        )
        if sum(item.tool_calls for item in bins) > 0
    ]
    if not provider_panels:
        return

    labels = [item.label for item in provider_panels[0][1]]
    panels: list[tuple[str, list[float], list[float]]] = []
    for provider, bins in provider_panels:
        total_latency = sum(item.total_latency_ms for item in bins)
        total_calls = sum(item.tool_calls for item in bins)
        call_share = [
            item.tool_calls / total_calls * 100 if total_calls else 0.0 for item in bins
        ]
        latency_share = [
            item.total_latency_ms / total_latency * 100 if total_latency else 0.0
            for item in bins
        ]
        panels.append((provider_title(provider), call_share, latency_share))

    active = active_bin_mask(panels, len(labels))
    if not active:
        return
    active_panels = [
        (title, [count_share[i] for i in active], [mass_share[i] for i in active])
        for title, count_share, mass_share in panels
    ]
    plot_stacked_share_panels(
        active_panels,
        [labels[i] for i in active],
        output_dir,
        count_bar_label="tool calls",
        mass_bar_label="total latency",
        suptitle="Tool Calls vs Latency",
        caption="Most Calls Are Fast — But Most Latency Comes From the Rare Slow Calls",
        legend_title="Per-Call Latency",
        out_name=out_name,
        compact=compact,
    )


def plot_tool_total_latency_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> None:
    edges = fine_latency_bin_edges(values_by_provider)
    edges = bin_edges_with_reference(edges, CDF_REFERENCE_MS)
    if edges.size < 2:
        return

    boundaries = tool_latency_boundaries_ms()
    visible_boundaries = [
        boundary for boundary in boundaries if edges[0] <= boundary <= edges[-1]
    ]
    max_cumulative_hours = 0.0
    reference_points: list[tuple[str, float, str]] = []
    stats_rows: list[tuple[str, int, float, float, float, float, float]] = []

    fig, ax = plt.subplots(figsize=(10.8, 6.2))
    ax.set_title("Cumulative Tool Total Latency by Provider")
    ax.set_xlabel("Per-call effective latency threshold")
    ax.set_ylabel("Cumulative summed effective latency")
    ax.set_xscale("log")
    if visible_boundaries:
        ax.set_xticks(visible_boundaries)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_latency_tick))
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(format_hours_tick))
    polish_axes(ax, grid_axis="both")

    for boundary in visible_boundaries:
        ax.axvline(
            boundary,
            color=AXIS_COLOR,
            linestyle=(0, (4, 3)),
            linewidth=0.9,
            alpha=0.8,
            zorder=0,
        )

    for index, provider in enumerate(provider_order(values_by_provider)):
        values = [
            value
            for value in values_by_provider[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        latency_by_bin, _ = np.histogram(arr, bins=edges, weights=arr)
        cumulative_hours = np.cumsum(latency_by_bin) / 3_600_000
        total_hours = float(np.sum(latency_by_bin) / 3_600_000)
        p25, p50, p90, p99 = np.percentile(arr, [25, 50, 90, 99])
        color = plot_color(provider, index)
        if edges[0] <= CDF_REFERENCE_MS <= edges[-1]:
            reference_hours = float(np.sum(arr[arr <= CDF_REFERENCE_MS]) / 3_600_000)
            reference_points.append((provider_title(provider), reference_hours, color))
        stats_rows.append(
            (
                provider_title(provider),
                int(arr.size),
                float(p25),
                float(p50),
                float(p90),
                float(p99),
                float(np.mean(arr)),
            )
        )
        max_cumulative_hours = max(max_cumulative_hours, total_hours)
        ax.plot(
            edges[1:],
            cumulative_hours,
            linewidth=2.35,
            color=color,
            label=(
                f"{provider_title(provider)} "
                f"(total={format_hours_compact(total_hours)}, "
                f"calls={arr.size:,})"
            ),
        )

    ax.set_xlim(edges[0], edges[-1])
    ax.set_ylim(0, max_cumulative_hours * 1.06 if max_cumulative_hours > 0 else 1)
    annotate_cumulative_time_reference(
        ax,
        x_value=CDF_REFERENCE_MS,
        x_label="5m",
        points=reference_points,
    )
    ax.legend(fontsize=9.5, loc="upper left")
    if stats_rows:
        stats_lines = [
            "single-call latency",
            "provider   count      p25    p50    p90    p99    avg",
        ]
        for provider, count, p25, p50, p90, p99, avg in stats_rows:
            stats_lines.append(
                f"{provider:<8} {count:>7,} "
                f"{format_latency_compact(p25):>7} "
                f"{format_latency_compact(p50):>7} "
                f"{format_latency_compact(p90):>7} "
                f"{format_latency_compact(p99):>7} "
                f"{format_latency_compact(avg):>7}"
            )
        ax.text(
            0.012,
            0.84,
            "\n".join(stats_lines),
            transform=ax.transAxes,
            ha="left",
            va="top",
            fontsize=8.4,
            family="DejaVu Sans Mono",
            color=TEXT_COLOR,
            bbox={
                "boxstyle": "round,pad=0.32",
                "facecolor": "white",
                "edgecolor": AXIS_COLOR,
                "linewidth": 0.7,
                "alpha": 0.92,
            },
        )
    fig.tight_layout()
    out = output_dir / "tool_total_latency_cdf_by_provider.png"
    save_plot(fig, out)


def write_tool_summary(
    tool_stats: dict[str, ToolStats], output_dir: Path
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, stats in sorted(
        tool_stats.items(), key=lambda item: item[1].calls, reverse=True
    ):
        summary = stats.summary()
        providers = ";".join(
            f"{provider}:{count}" for provider, count in summary["providers"].items()
        )
        rows.append(
            {
                "tool_name": name,
                "calls": summary["calls"],
                "latency_count": summary["latency_count"],
                "missing_latency": summary["missing_latency"],
                "nonpositive_latency": summary["nonpositive_latency"],
                "error_calls": summary["error_calls"],
                "mean_ms": summary["mean_ms"],
                "min_ms": summary["min_ms"],
                "p50_ms": summary["p50_ms"],
                "p90_ms": summary["p90_ms"],
                "p99_ms": summary["p99_ms"],
                "max_ms": summary["max_ms"],
                "sample_count": summary["sample_count"],
                "sampled": summary["sampled"],
                "providers": providers,
            }
        )

    path = output_dir / "tool_latency_summary.csv"
    fieldnames = [
        "tool_name",
        "calls",
        "latency_count",
        "missing_latency",
        "nonpositive_latency",
        "error_calls",
        "mean_ms",
        "min_ms",
        "p50_ms",
        "p90_ms",
        "p99_ms",
        "max_ms",
        "sample_count",
        "sampled",
        "providers",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def write_tool_latency_weighted_bins(
    bins: list[ToolLatencyBinStats],
    output_dir: Path,
) -> list[dict[str, Any]]:
    total_latency = sum(item.total_latency_ms for item in bins)
    total_calls = sum(item.tool_calls for item in bins)
    rows: list[dict[str, Any]] = []
    for item in bins:
        rows.append(
            {
                "label": item.label,
                "lo_ms": item.lo_ms,
                "hi_ms": "" if item.hi_ms is None else item.hi_ms,
                "tool_calls": item.tool_calls,
                "error_calls": item.error_calls,
                "total_latency_ms": item.total_latency_ms,
                "total_latency_s": item.total_latency_ms / 1000,
                "total_latency_hours": item.total_latency_ms / 3_600_000,
                "latency_share": (
                    item.total_latency_ms / total_latency if total_latency else 0.0
                ),
                "call_share": item.tool_calls / total_calls if total_calls else 0.0,
            }
        )

    path = output_dir / "tool_latency_weighted_bins.csv"
    fieldnames = [
        "label",
        "lo_ms",
        "hi_ms",
        "tool_calls",
        "error_calls",
        "total_latency_ms",
        "total_latency_s",
        "total_latency_hours",
        "latency_share",
        "call_share",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def write_tool_total_latency_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    edges = fine_latency_bin_edges(values_by_provider)
    if edges.size < 2:
        return []

    boundaries = {float(boundary) for boundary in tool_latency_boundaries_ms()}
    rows: list[dict[str, Any]] = []
    for provider in provider_order(values_by_provider):
        values = [
            value
            for value in values_by_provider[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        calls_by_bin, _ = np.histogram(arr, bins=edges)
        latency_by_bin, _ = np.histogram(arr, bins=edges, weights=arr)
        total_latency = float(np.sum(latency_by_bin))
        total_calls = int(np.sum(calls_by_bin))
        cumulative_latency = 0.0
        cumulative_calls = 0
        for index, (lo_ms, hi_ms, bin_calls, bin_latency) in enumerate(
            zip(edges[:-1], edges[1:], calls_by_bin, latency_by_bin, strict=True),
            start=1,
        ):
            cumulative_latency += float(bin_latency)
            cumulative_calls += int(bin_calls)
            rows.append(
                {
                    "provider": provider,
                    "fine_bin_index": index,
                    "lo_ms": lo_ms,
                    "hi_ms": hi_ms,
                    "latency_threshold_ms": hi_ms,
                    "coarse_boundary": hi_ms in boundaries,
                    "tool_calls": int(bin_calls),
                    "total_latency_ms": float(bin_latency),
                    "total_latency_s": float(bin_latency) / 1000,
                    "total_latency_hours": float(bin_latency) / 3_600_000,
                    "latency_share": (
                        float(bin_latency) / total_latency if total_latency else 0.0
                    ),
                    "call_share": int(bin_calls) / total_calls if total_calls else 0.0,
                    "cumulative_tool_calls": cumulative_calls,
                    "cumulative_latency_ms": cumulative_latency,
                    "cumulative_latency_s": cumulative_latency / 1000,
                    "cumulative_latency_hours": cumulative_latency / 3_600_000,
                    "cumulative_call_share": (
                        cumulative_calls / total_calls if total_calls else 0.0
                    ),
                    "cumulative_latency_share": (
                        cumulative_latency / total_latency if total_latency else 0.0
                    ),
                }
            )

    path = output_dir / "tool_total_latency_cdf_by_provider.csv"
    fieldnames = [
        "provider",
        "fine_bin_index",
        "lo_ms",
        "hi_ms",
        "latency_threshold_ms",
        "coarse_boundary",
        "tool_calls",
        "total_latency_ms",
        "total_latency_s",
        "total_latency_hours",
        "latency_share",
        "call_share",
        "cumulative_tool_calls",
        "cumulative_latency_ms",
        "cumulative_latency_s",
        "cumulative_latency_hours",
        "cumulative_call_share",
        "cumulative_latency_share",
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
        "--paper-wide-only",
        action="store_true",
        help="only generate the compact side-by-side top-12 paper figure",
    )
    parser.add_argument(
        "--paper-weighted-only",
        action="store_true",
        help="only generate the compact weighted latency-bin paper figure",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    by_provider = load_tool_stats_by_provider(con, min_calls=args.min_tool_calls_for_plot)

    if args.paper_wide_only:
        plot_tool_latency_top12_wide(by_provider, out)
        png_sidecar.make_self_contained(
            out,
            code_files=[Path(__file__), *png_sidecar.util_code_files()],
            readme_path=EXP_DIR / "README.md",
            png_names=["tool_latency_by_tool_top12_wide.png"],
        )
        print(f"All outputs saved to {out}", file=sys.stderr)
        return 0

    if args.paper_weighted_only:
        latency_bins_by_provider = load_tool_latency_bins(con, by_provider=True)
        plot_tool_latency_weighted_bins(
            latency_bins_by_provider,
            out,
            compact=True,
            out_name="tool_latency_weighted_bins_paper.png",
        )
        png_sidecar.make_self_contained(
            out,
            code_files=[Path(__file__), *png_sidecar.util_code_files()],
            readme_path=EXP_DIR / "README.md",
            png_names=["tool_latency_weighted_bins_paper.png"],
        )
        print(f"All outputs saved to {out}", file=sys.stderr)
        return 0

    tool_stats = load_tool_stats(con)
    latency_values = load_tool_latency_values_by_provider(con)
    latency_bins = load_tool_latency_bins(con, by_provider=False)
    latency_bins_by_provider = load_tool_latency_bins(con, by_provider=True)

    plot_tool_latency(by_provider, out, args.top_tools)
    plot_tool_latency_top12_wide(by_provider, out)
    plot_tool_latency_weighted_bins(latency_bins_by_provider, out)
    plot_tool_latency_weighted_bins(
        latency_bins_by_provider,
        out,
        compact=True,
        out_name="tool_latency_weighted_bins_paper.png",
    )
    plot_tool_total_latency_cdf_by_provider(latency_values, out)
    plot_count_cdf_by_provider(
        latency_values,
        out,
        out_name="tool_latency_count_cdf_by_provider.png",
        title="Tool Latency Count CDF by Provider",
        x_label="Per-call effective latency threshold",
        table_title="single-call latency",
        edge_kind="latency_ms",
        unit_label="tool-call",
    )
    write_tool_summary(tool_stats, out)
    write_tool_latency_weighted_bins(latency_bins, out)
    write_tool_total_latency_cdf_by_provider(latency_values, out)
    write_count_cdf_by_provider(
        latency_values,
        out,
        out_name="tool_latency_count_cdf_by_provider.csv",
        edge_kind="latency_ms",
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
