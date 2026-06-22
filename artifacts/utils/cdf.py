"""Generic by-provider CDF and stacked-share renderers shared across experiments."""

from __future__ import annotations

from typing import Any
from pathlib import Path
import csv
import math
import numpy as np
import sys

from style import (
    AXIS_COLOR,
    MUTED_TEXT,
    TEXT_COLOR,
    mpatches,
    mticker,
    plot_color,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    readable_text_color,
    save_plot,
)
from formatters import (
    bin_edges_with_reference,
    duration_landmarks_seconds,
    fine_duration_bin_edges,
    fine_latency_bin_edges,
    format_count_tick,
    format_duration_compact,
    format_duration_seconds_tick,
    format_hours_compact,
    format_hours_tick,
    format_latency_compact,
    format_latency_tick,
    format_seconds_as_hours_compact,
    tool_latency_boundaries_ms,
)


def annotate_cumulative_time_reference(
    ax: plt.Axes,
    *,
    x_value: float,
    x_label: str,
    points: list[tuple[str, float, str]],
) -> None:
    if not points:
        return

    ax.axvline(
        x_value,
        color=MUTED_TEXT,
        linestyle=(0, (4, 3)),
        linewidth=1.05,
        alpha=0.9,
        zorder=1,
    )
    ax.text(
        x_value,
        0.985,
        x_label,
        transform=ax.get_xaxis_transform(),
        ha="center",
        va="top",
        fontsize=8.5,
        color=MUTED_TEXT,
        bbox={
            "boxstyle": "round,pad=0.18",
            "facecolor": "white",
            "edgecolor": AXIS_COLOR,
            "linewidth": 0.65,
            "alpha": 0.92,
        },
    )

    ordered_points = [
        (provider, y_hours, color)
        for provider, y_hours, color in points
        if y_hours > 0 and math.isfinite(y_hours)
    ]
    ordered_points.sort(key=lambda item: item[1], reverse=True)
    offsets = [14, -16, 32, -34, 50, -52]
    for index, (provider, y_hours, color) in enumerate(ordered_points):
        offset_y = offsets[index % len(offsets)]
        ax.scatter(
            [x_value],
            [y_hours],
            s=26,
            color=color,
            edgecolor="white",
            linewidth=0.7,
            zorder=4,
        )
        ax.annotate(
            f"{provider}: {format_hours_compact(y_hours)}",
            xy=(x_value, y_hours),
            xytext=(8, offset_y),
            textcoords="offset points",
            ha="left",
            va="center",
            fontsize=8.2,
            color=color,
            arrowprops={
                "arrowstyle": "-",
                "color": color,
                "linewidth": 0.75,
                "alpha": 0.85,
            },
            bbox={
                "boxstyle": "round,pad=0.18",
                "facecolor": "white",
                "edgecolor": AXIS_COLOR,
                "linewidth": 0.65,
                "alpha": 0.92,
            },
            zorder=5,
        )


def plot_stacked_share_panels(
    panels: list[tuple[str, list[float], list[float]]],
    active_labels: list[str],
    output_dir: Path,
    *,
    count_bar_label: str,
    mass_bar_label: str,
    suptitle: str,
    caption: str,
    legend_title: str,
    out_name: str,
    label_threshold: float = 6.0,
    compact: bool = False,
) -> None:
    """Two 100%-stacked horizontal bars per provider: a count-weighted and a
    mass-weighted composition over the same ordered bins, sharing one color ramp.

    panels carries ``(title, count_share, mass_share)`` where each share list is
    already expressed in percent and aligned to ``active_labels`` (small->large).
    """
    if not panels:
        return

    n_bins = len(active_labels)
    # Single-hue blue ramp: small bins light, large bins dark — brightness alone
    # carries the ordering, so the mass visibly darkens from the count bar to the
    # mass bar.
    cmap = plt.colormaps["Blues"]
    colors = [cmap(0.22 + 0.70 * (i / max(1, n_bins - 1))) for i in range(n_bins)]

    count_y, mass_y, bar_h = 1.3, 0.0, 0.5

    def block_centers(shares: list[float]) -> list[float]:
        """x-center of every segment in a left-to-right stacked bar."""
        centers, left = [], 0.0
        for share in shares:
            centers.append(left + share / 2.0)
            left += share
        return centers

    n_panels = len(panels)

    # A "compact" profile renders nicely at one LaTeX column. It uses one
    # shared axis instead of vertically stacked provider panels, so the figure is
    # designed for column width rather than being squeezed into it.
    if compact:
        fig, ax = plt.subplots(figsize=(3.45, 2.35))
        fs_bar, fs_group, fs_ytick, fs_xlabel, fs_legend = 6.0, 7.2, 6.6, 6.8, 4.9
        compact_bar_h = 0.34
        group_gap = 0.72
        row_gap = 0.42
        y_positions: list[tuple[float, str, list[float]]] = []
        y = (n_panels - 1) * (2 * row_gap + group_gap)
        group_labels: list[tuple[float, str]] = []
        for title, count_share, mass_share in panels:
            count_y_pos = y + row_gap
            mass_y_pos = y
            y_positions.append((count_y_pos, count_bar_label, count_share))
            y_positions.append((mass_y_pos, mass_bar_label, mass_share))
            group_labels.append((count_y_pos + compact_bar_h / 2 + 0.13, title))
            y -= 2 * row_gap + group_gap

        compact_label_threshold = max(label_threshold, 7.5)
        for y_pos, _row_label, shares in y_positions:
            left = 0.0
            for i, share in enumerate(shares):
                if share <= 0:
                    continue
                ax.barh(
                    y_pos,
                    share,
                    left=left,
                    height=compact_bar_h,
                    color=colors[i],
                    edgecolor="white",
                    linewidth=0.6,
                )
                if share >= compact_label_threshold:
                    ax.text(
                        left + share / 2,
                        y_pos,
                        f"{share:.0f}%",
                        ha="center",
                        va="center",
                        fontsize=fs_bar,
                        color=readable_text_color(colors[i]),
                    )
                left += share

        for label_y, title in group_labels:
            ax.text(
                50,
                label_y,
                title,
                ha="center",
                va="bottom",
                fontsize=fs_group,
                fontweight="semibold",
                color=TEXT_COLOR,
            )

        ax.set_yticks([item[0] for item in y_positions])
        ax.set_yticklabels([item[1] for item in y_positions], fontsize=fs_ytick)
        ax.tick_params(axis="y", length=0, pad=2)
        ax.set_xlim(0, 100)
        ax.set_ylim(y_positions[-1][0] - 0.42, group_labels[0][0] + 0.28)
        ax.set_xlabel("Share of total", fontsize=fs_xlabel, labelpad=2)
        ax.xaxis.set_major_locator(mticker.FixedLocator([0, 25, 50, 75, 100]))
        ax.xaxis.set_major_formatter(mticker.PercentFormatter())
        ax.tick_params(axis="x", labelsize=6.2, pad=1.5)
        polish_axes(ax, grid_axis="x")

        handles = [
            mpatches.Patch(facecolor=colors[i], edgecolor="white") for i in range(n_bins)
        ]
        compact_legend_title = legend_title.split(" per ")[0]
        legend_center_x = 0.57
        fig.text(
            legend_center_x,
            0.985,
            compact_legend_title,
            ha="center",
            va="center",
            fontsize=fs_legend + 0.3,
            color=TEXT_COLOR,
        )
        fig.legend(
            handles,
            active_labels,
            loc="upper center",
            ncol=n_bins,
            frameon=False,
            bbox_to_anchor=(legend_center_x, 0.955),
            fontsize=fs_legend,
            handlelength=0.65,
            handletextpad=0.2,
            columnspacing=0.42,
            borderaxespad=0.0,
        )
        fig.subplots_adjust(left=0.22, right=0.985, bottom=0.17, top=0.84)
        # Vector PDF sibling for the paper (these panels are pure vector — bars + text).
        fig.savefig(
            output_dir / f"{Path(out_name).stem}.pdf", bbox_inches="tight", facecolor="white"
        )
        save_plot(fig, output_dir / out_name)
        return

    # The non-compact branch is the original wide figure.
    figsize = (11.0, max(3.6, 2.7 * n_panels) + 0.5)
    fs_bar, fs_title, fs_ytick, fs_xlabel, fs_legend = 8.5, None, 10.0, None, None
    legend_ncol, legend_anchor = min(n_bins, 8), (0.5, -0.01)
    left_margin, right_margin = 0.13, 0.985
    margins = dict(left=left_margin, right=right_margin, bottom=0.19, top=0.835, hspace=0.62)

    fig, axes = plt.subplots(n_panels, 1, figsize=figsize, squeeze=False)
    plot_center = (left_margin + right_margin) / 2
    if not compact:
        fig.suptitle(suptitle, x=plot_center, fontsize=17, y=0.990)
        fig.text(
            plot_center,
            0.925,
            caption,
            ha="center",
            fontsize=12,
            color=MUTED_TEXT,
            style="italic",
        )

    # count bar on top, mass bar below, so the eye reads the shift from "where the
    # events are" down to "where the volume is".
    rows = [(count_y, count_bar_label), (mass_y, mass_bar_label)]
    for ax, (title, count_share, mass_share) in zip(axes.ravel(), panels, strict=True):
        series = {count_bar_label: count_share, mass_bar_label: mass_share}
        for y_pos, name in rows:
            left = 0.0
            for i, share in enumerate(series[name]):
                if share <= 0:
                    continue
                ax.barh(
                    y_pos,
                    share,
                    left=left,
                    height=bar_h,
                    color=colors[i],
                    edgecolor="white",
                    linewidth=0.7,
                )
                if share >= label_threshold:
                    ax.text(
                        left + share / 2,
                        y_pos,
                        f"{share:.0f}%",
                        ha="center",
                        va="center",
                        fontsize=fs_bar,
                        color=readable_text_color(colors[i]),
                    )
                left += share

        # Dashed arrows tie each bin's block in the count bar to the same bin's
        # block in the mass bar, pointing rounds -> tokens so the eye follows
        # where each slice moves.
        count_centers = block_centers(count_share)
        mass_centers = block_centers(mass_share)
        for i in range(len(active_labels)):
            if count_share[i] <= 0 or mass_share[i] <= 0:
                continue
            ax.annotate(
                "",
                xy=(mass_centers[i], mass_y + bar_h / 2),
                xytext=(count_centers[i], count_y - bar_h / 2),
                zorder=2.5,
                arrowprops={
                    "arrowstyle": "-|>",
                    "linestyle": (0, (5, 3)),
                    "color": "#6b7280",
                    "linewidth": 1.0,
                    "shrinkA": 0,
                    "shrinkB": 0,
                    "mutation_scale": 11,
                },
            )

        ax.set_title(title, loc="left", pad=3, fontsize=fs_title)
        ax.set_yticks([mass_y, count_y])
        ax.set_yticklabels(
            [mass_bar_label.title(), count_bar_label.title()], fontsize=fs_ytick
        )
        ax.set_ylim(-0.45, 1.75)
        ax.set_xlim(0, 100)
        ax.xaxis.set_major_formatter(mticker.PercentFormatter())
        # In the compact (single-column) profile only the bottom panel is labelled,
        # to avoid a mid-figure label and a collision with the legend.
        if (not compact) or ax is axes.ravel()[-1]:
            ax.set_xlabel("Share of Total (each bar sums to 100%)", fontsize=fs_xlabel)
        polish_axes(ax, grid_axis="x")

    handles = [
        mpatches.Patch(facecolor=colors[i], edgecolor="white") for i in range(n_bins)
    ]
    fig.legend(
        handles,
        active_labels,
        loc="lower center",
        ncol=legend_ncol,
        frameon=False,
        title=legend_title,
        bbox_to_anchor=legend_anchor,
        fontsize=fs_legend,
        title_fontsize=fs_legend,
    )
    fig.subplots_adjust(**margins)
    # Vector PDF sibling for the paper (these panels are pure vector — bars + text).
    fig.savefig(
        output_dir / f"{Path(out_name).stem}.pdf", bbox_inches="tight", facecolor="white"
    )
    save_plot(fig, output_dir / out_name)


def active_bin_mask(
    panels: list[tuple[str, list[float], list[float]]],
    n_bins: int,
    *,
    visibility: float = 0.5,
) -> list[int]:
    """Indices of bins that reach ``visibility`` percent in either series of any
    panel — drops the empty leading bins that only add clutter."""
    return [
        i
        for i in range(n_bins)
        if any(
            count_share[i] >= visibility or mass_share[i] >= visibility
            for _title, count_share, mass_share in panels
        )
    ]


def plot_count_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
    *,
    out_name: str,
    title: str,
    x_label: str,
    table_title: str,
    edge_kind: str,
    unit_label: str,
    x_max: float | None = None,
    x_max_label: str | None = None,
) -> None:
    provider_values = {
        provider: values
        for provider, values in values_by_provider.items()
        if provider != "all" and values
    }
    if edge_kind == "latency_ms":
        edges = fine_latency_bin_edges(provider_values)
        landmarks = tool_latency_boundaries_ms()
        tick_formatter = format_latency_tick
        value_formatter = format_latency_compact
    elif edge_kind == "duration_seconds":
        edges = fine_duration_bin_edges(provider_values)
        landmarks = duration_landmarks_seconds()
        tick_formatter = format_duration_seconds_tick
        value_formatter = format_duration_compact
    else:
        raise ValueError(f"Unsupported count CDF edge kind: {edge_kind}")

    if edges.size < 2:
        return
    x_limit_max = min(edges[-1], x_max) if x_max is not None else edges[-1]

    visible_landmarks = [
        value for value in landmarks if edges[0] <= value <= x_limit_max
    ]
    max_count = 0
    stats_rows: list[tuple[str, int, float, float, float, float, float]] = []

    fig, ax = plt.subplots(figsize=(10.8, 6.2))
    ax.set_title(f"{title} ({x_max_label})" if x_max_label else title)
    ax.set_xlabel(x_label)
    ax.set_ylabel(f"Cumulative {unit_label} count")
    ax.set_xscale("log")
    if visible_landmarks:
        ax.set_xticks(visible_landmarks)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(tick_formatter))
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(format_count_tick))
    polish_axes(ax, grid_axis="both")

    for landmark in visible_landmarks:
        ax.axvline(
            landmark,
            color=AXIS_COLOR,
            linestyle=(0, (4, 3)),
            linewidth=0.9,
            alpha=0.8,
            zorder=0,
        )

    for index, provider in enumerate(provider_order(provider_values)):
        values = [
            value
            for value in provider_values[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        count_by_bin, _ = np.histogram(arr, bins=edges)
        cumulative_count = np.cumsum(count_by_bin)
        total_count = int(np.sum(count_by_bin))
        visible_bin_count = np.searchsorted(edges[1:], x_limit_max, side="right")
        visible_count = (
            int(cumulative_count[visible_bin_count - 1]) if visible_bin_count > 0 else 0
        )
        p25, p50, p90, p99 = np.percentile(arr, [25, 50, 90, 99])
        stats_rows.append(
            (
                provider_title(provider),
                total_count,
                float(p25),
                float(p50),
                float(p90),
                float(p99),
                float(np.mean(arr)),
            )
        )
        max_count = max(max_count, visible_count if x_max is not None else total_count)
        label = f"{provider_title(provider)} (n={total_count:,})"
        if x_max_label:
            label = (
                f"{provider_title(provider)} "
                f"(<={x_max_label}={visible_count:,}, n={total_count:,})"
            )
        plot_bin_count = (
            visible_bin_count if x_max is not None else cumulative_count.size
        )
        ax.plot(
            edges[1 : plot_bin_count + 1],
            cumulative_count[:plot_bin_count],
            linewidth=2.35,
            color=plot_color(provider, index),
            label=label,
        )

    ax.set_xlim(edges[0], x_limit_max)
    ax.set_ylim(0, max_count * 1.06 if max_count > 0 else 1)
    ax.legend(fontsize=9.5, loc="upper left")
    if stats_rows:
        stats_lines = [
            table_title,
            "provider   count      p25    p50    p90    p99    avg",
        ]
        for provider, count, p25, p50, p90, p99, avg in stats_rows:
            stats_lines.append(
                f"{provider:<8} {count:>7,} "
                f"{value_formatter(p25):>7} "
                f"{value_formatter(p50):>7} "
                f"{value_formatter(p90):>7} "
                f"{value_formatter(p99):>7} "
                f"{value_formatter(avg):>7}"
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
    save_plot(fig, output_dir / out_name)


def plot_cumulative_duration_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
    *,
    out_name: str,
    title: str,
    x_label: str,
    table_title: str,
    x_max: float | None = None,
    x_max_label: str | None = None,
    reference_seconds: float | None = None,
    reference_label: str | None = None,
) -> None:
    provider_values = {
        provider: values
        for provider, values in values_by_provider.items()
        if provider != "all" and values
    }
    edges = fine_duration_bin_edges(provider_values)
    edges = bin_edges_with_reference(edges, reference_seconds)
    if edges.size < 2:
        return
    x_limit_max = min(edges[-1], x_max) if x_max is not None else edges[-1]

    visible_landmarks = [
        value
        for value in duration_landmarks_seconds()
        if edges[0] <= value <= x_limit_max
    ]
    max_cumulative_hours = 0.0
    reference_points: list[tuple[str, float, str]] = []
    stats_rows: list[tuple[str, int, float, float, float, float, float]] = []

    fig, ax = plt.subplots(figsize=(10.8, 6.2))
    ax.set_title(f"{title} ({x_max_label})" if x_max_label else title)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Cumulative summed time")
    ax.set_xscale("log")
    if visible_landmarks:
        ax.set_xticks(visible_landmarks)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_duration_seconds_tick))
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(format_hours_tick))
    polish_axes(ax, grid_axis="both")

    for landmark in visible_landmarks:
        ax.axvline(
            landmark,
            color=AXIS_COLOR,
            linestyle=(0, (4, 3)),
            linewidth=0.9,
            alpha=0.8,
            zorder=0,
        )

    for index, provider in enumerate(provider_order(provider_values)):
        values = [
            value
            for value in provider_values[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        seconds_by_bin, _ = np.histogram(arr, bins=edges, weights=arr)
        cumulative_hours = np.cumsum(seconds_by_bin) / 3600
        total_seconds = float(np.sum(seconds_by_bin))
        visible_bin_count = np.searchsorted(edges[1:], x_limit_max, side="right")
        visible_seconds = (
            float(cumulative_hours[visible_bin_count - 1] * 3600)
            if visible_bin_count > 0
            else 0.0
        )
        p25, p50, p90, p99 = np.percentile(arr, [25, 50, 90, 99])
        color = plot_color(provider, index)
        if (
            reference_seconds is not None
            and edges[0] <= reference_seconds <= x_limit_max
        ):
            reference_hours = float(np.sum(arr[arr <= reference_seconds]) / 3600)
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
        max_cumulative_hours = max(
            max_cumulative_hours,
            (visible_seconds if x_max is not None else total_seconds) / 3600,
        )
        if x_max_label:
            label = (
                f"{provider_title(provider)} "
                f"(<={x_max_label}={format_seconds_as_hours_compact(visible_seconds)}, "
                f"total={format_seconds_as_hours_compact(total_seconds)}, "
                f"n={arr.size:,})"
            )
        else:
            label = (
                f"{provider_title(provider)} "
                f"(total={format_seconds_as_hours_compact(total_seconds)}, "
                f"n={arr.size:,})"
            )
        plot_bin_count = (
            visible_bin_count if x_max is not None else cumulative_hours.size
        )
        ax.plot(
            edges[1 : plot_bin_count + 1],
            cumulative_hours[:plot_bin_count],
            linewidth=2.35,
            color=color,
            label=label,
        )

    ax.set_xlim(edges[0], x_limit_max)
    ax.set_ylim(0, max_cumulative_hours * 1.06 if max_cumulative_hours > 0 else 1)
    if reference_seconds is not None and reference_label:
        annotate_cumulative_time_reference(
            ax,
            x_value=reference_seconds,
            x_label=reference_label,
            points=reference_points,
        )
    ax.legend(fontsize=9.5, loc="upper left")
    if stats_rows:
        stats_lines = [
            table_title,
            "provider   count      p25    p50    p90    p99    avg",
        ]
        for provider, count, p25, p50, p90, p99, avg in stats_rows:
            stats_lines.append(
                f"{provider:<8} {count:>7,} "
                f"{format_duration_compact(p25):>7} "
                f"{format_duration_compact(p50):>7} "
                f"{format_duration_compact(p90):>7} "
                f"{format_duration_compact(p99):>7} "
                f"{format_duration_compact(avg):>7}"
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
    save_plot(fig, output_dir / out_name)


def write_count_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
    *,
    out_name: str,
    edge_kind: str,
) -> list[dict[str, Any]]:
    provider_values = {
        provider: values
        for provider, values in values_by_provider.items()
        if provider != "all" and values
    }
    if edge_kind == "latency_ms":
        edges = fine_latency_bin_edges(provider_values)
        boundaries = {float(boundary) for boundary in tool_latency_boundaries_ms()}
        lo_field = "lo_ms"
        hi_field = "hi_ms"
        threshold_field = "latency_threshold_ms"
        boundary_field = "coarse_boundary"
    elif edge_kind == "duration_seconds":
        edges = fine_duration_bin_edges(provider_values)
        boundaries = {float(value) for value in duration_landmarks_seconds()}
        lo_field = "lo_seconds"
        hi_field = "hi_seconds"
        threshold_field = "duration_threshold_seconds"
        boundary_field = "landmark_boundary"
    else:
        raise ValueError(f"Unsupported count CDF edge kind: {edge_kind}")

    if edges.size < 2:
        return []

    rows: list[dict[str, Any]] = []
    for provider in provider_order(provider_values):
        values = [
            value
            for value in provider_values[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        count_by_bin, _ = np.histogram(arr, bins=edges)
        total_count = int(np.sum(count_by_bin))
        cumulative_count = 0
        for index, (lo_value, hi_value, bin_count) in enumerate(
            zip(edges[:-1], edges[1:], count_by_bin, strict=True),
            start=1,
        ):
            cumulative_count += int(bin_count)
            rows.append(
                {
                    "provider": provider,
                    "fine_bin_index": index,
                    lo_field: lo_value,
                    hi_field: hi_value,
                    threshold_field: hi_value,
                    boundary_field: hi_value in boundaries,
                    "count": int(bin_count),
                    "count_share": int(bin_count) / total_count if total_count else 0.0,
                    "cumulative_count": cumulative_count,
                    "cumulative_count_share": (
                        cumulative_count / total_count if total_count else 0.0
                    ),
                }
            )

    path = output_dir / out_name
    fieldnames = [
        "provider",
        "fine_bin_index",
        lo_field,
        hi_field,
        threshold_field,
        boundary_field,
        "count",
        "count_share",
        "cumulative_count",
        "cumulative_count_share",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def write_cumulative_duration_cdf_by_provider(
    values_by_provider: dict[str, list[float]],
    output_dir: Path,
    *,
    out_name: str,
) -> list[dict[str, Any]]:
    provider_values = {
        provider: values
        for provider, values in values_by_provider.items()
        if provider != "all" and values
    }
    edges = fine_duration_bin_edges(provider_values)
    if edges.size < 2:
        return []

    landmarks = {float(value) for value in duration_landmarks_seconds()}
    rows: list[dict[str, Any]] = []
    for provider in provider_order(provider_values):
        values = [
            value
            for value in provider_values[provider]
            if value > 0 and math.isfinite(value)
        ]
        if not values:
            continue
        arr = np.asarray(values, dtype=float)
        count_by_bin, _ = np.histogram(arr, bins=edges)
        seconds_by_bin, _ = np.histogram(arr, bins=edges, weights=arr)
        total_seconds = float(np.sum(seconds_by_bin))
        total_count = int(np.sum(count_by_bin))
        cumulative_seconds = 0.0
        cumulative_count = 0
        for index, (lo_s, hi_s, bin_count, bin_seconds) in enumerate(
            zip(edges[:-1], edges[1:], count_by_bin, seconds_by_bin, strict=True),
            start=1,
        ):
            cumulative_seconds += float(bin_seconds)
            cumulative_count += int(bin_count)
            rows.append(
                {
                    "provider": provider,
                    "fine_bin_index": index,
                    "lo_seconds": lo_s,
                    "hi_seconds": hi_s,
                    "duration_threshold_seconds": hi_s,
                    "landmark_boundary": hi_s in landmarks,
                    "count": int(bin_count),
                    "total_seconds": float(bin_seconds),
                    "total_hours": float(bin_seconds) / 3600,
                    "time_share": (
                        float(bin_seconds) / total_seconds if total_seconds else 0.0
                    ),
                    "count_share": (
                        int(bin_count) / total_count if total_count else 0.0
                    ),
                    "cumulative_count": cumulative_count,
                    "cumulative_seconds": cumulative_seconds,
                    "cumulative_hours": cumulative_seconds / 3600,
                    "cumulative_count_share": (
                        cumulative_count / total_count if total_count else 0.0
                    ),
                    "cumulative_time_share": (
                        cumulative_seconds / total_seconds if total_seconds else 0.0
                    ),
                }
            )

    path = output_dir / out_name
    fieldnames = [
        "provider",
        "fine_bin_index",
        "lo_seconds",
        "hi_seconds",
        "duration_threshold_seconds",
        "landmark_boundary",
        "count",
        "total_seconds",
        "total_hours",
        "time_share",
        "count_share",
        "cumulative_count",
        "cumulative_seconds",
        "cumulative_hours",
        "cumulative_count_share",
        "cumulative_time_share",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows
