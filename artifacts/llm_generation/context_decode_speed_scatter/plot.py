#!/usr/bin/env python3
"""Scatter normalized decode speed against per-step context length."""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from formatters import format_token_label, token_axis_value, token_axis_values  # noqa: E402
from style import TEXT_COLOR, plot_color, provider_order, provider_title, save_plot, plt  # noqa: E402
import png_sidecar  # noqa: E402
import trace_db  # noqa: E402


INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
NON_REASONING_MODEL_OUTPUT_EVENT_TYPES = {"text", "tool_call"}
DEFAULT_MAX_SPEED_TOKENS_PER_SECOND = 160.0
DEFAULT_MAX_PURE_DECODE_TOKENS_PER_SECOND = 160.0
DEFAULT_MAX_TTFT_SECONDS = 40.0
DEFAULT_MIN_CONTEXT_TOKENS = 4096.0
DEFAULT_MIN_CODEX_POST_REASONING_SECONDS = 0.1
T = TypeVar("T")


@dataclass(frozen=True)
class Observation:
    round_pk: int
    provider: str
    model: str
    context_tokens: int
    output_tokens: int
    visible_output_tokens: int
    reasoning_output_tokens: int
    generation_seconds: float
    normalized_decode_speed: float
    input_to_reasoning_end_seconds: float | None
    post_reasoning_output_seconds: float | None


@dataclass(frozen=True)
class MetricPoint:
    context_tokens: int
    value: float


def weighted_speed_average(
    rows: Iterable[Observation],
    *,
    token_fn: Callable[[Observation], int],
    seconds_fn: Callable[[Observation], float | None],
) -> float | None:
    total_tokens = 0
    total_seconds = 0.0
    for row in rows:
        tokens = token_fn(row)
        seconds = seconds_fn(row)
        if tokens <= 0 or seconds is None or seconds <= 0:
            continue
        total_tokens += tokens
        total_seconds += seconds
    if total_tokens <= 0 or total_seconds <= 0:
        return None
    return total_tokens / total_seconds


def int_field(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return 0


def percentile(values: Iterable[float], q: float) -> float | None:
    arr = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not arr:
        return None
    if len(arr) == 1:
        return arr[0]
    pos = q * (len(arr) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return arr[lo]
    frac = pos - lo
    return arr[lo] * (1 - frac) + arr[hi] * frac


def fmt(value: float | None, *, digits: int = 1) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{digits}f}"


def load_observations(con: "duckdb.DuckDBPyConnection") -> list[Observation]:
    """Load per-step context length and normalized decode speed from the trace DB.

    The observable generation span matches the paper macro definition: latest input event
    (`user_message` or `tool_result`) at or before the first model-output event, through the last
    model-output event (`reasoning`, `text`, or `tool_call`). The speed is output tokens divided by
    this span. These are trace-observed timings rather than serving-engine internal timers.
    """
    events_by_round: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events WHERE timestamp IS NOT NULL ORDER BY round_pk, event_index"
    ).fetchall():
        if ts_us is None:
            continue
        events_by_round[int(round_pk)].append((str(event_type), int(ts_us)))

    observations: list[Observation] = []
    for (
        round_pk,
        provider,
        model,
        prefix_tokens,
        append_tokens,
        input_tokens_total,
        output_tokens,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, model, prefix_tokens, newly_append_tokens, "
        "input_tokens_total, output_tokens, reasoning_output_tokens FROM rounds ORDER BY round_pk"
    ).fetchall():
        round_pk = int(round_pk)
        events = events_by_round.get(round_pk, [])
        inputs = [ts for event_type, ts in events if event_type in INPUT_EVENT_TYPES]
        outputs = [
            ts for event_type, ts in events if event_type in MODEL_OUTPUT_EVENT_TYPES
        ]
        if not inputs or not outputs:
            continue

        first_output = min(outputs)
        candidate_inputs = [ts for ts in inputs if ts <= first_output]
        if not candidate_inputs:
            continue

        span_seconds = (max(outputs) - max(candidate_inputs)) / 1_000_000
        out = int_field(output_tokens)
        reasoning = int_field(reasoning_output_tokens)
        visible = max(0, out - reasoning)
        context = int_field(input_tokens_total)
        if context <= 0:
            context = int_field(prefix_tokens) + int_field(append_tokens)
        if span_seconds <= 0 or out <= 0 or context <= 0:
            continue

        reasoning_timestamps = [ts for event_type, ts in events if event_type == "reasoning"]
        non_reasoning_timestamps = [
            ts
            for event_type, ts in events
            if event_type in NON_REASONING_MODEL_OUTPUT_EVENT_TYPES
        ]

        input_to_reasoning_end_seconds: float | None = None
        post_reasoning_output_seconds: float | None = None
        if reasoning_timestamps:
            reasoning_end = max(reasoning_timestamps)
            reasoning_inputs = [ts for ts in inputs if ts <= reasoning_end]
            if reasoning_inputs:
                duration = (reasoning_end - max(reasoning_inputs)) / 1_000_000
                if duration > 0:
                    input_to_reasoning_end_seconds = duration

            later_outputs = [ts for ts in non_reasoning_timestamps if ts >= reasoning_end]
            if later_outputs:
                duration = (max(later_outputs) - reasoning_end) / 1_000_000
                if duration > 0:
                    post_reasoning_output_seconds = duration

        observations.append(
            Observation(
                round_pk=round_pk,
                provider=str(provider) if provider else "<unknown-provider>",
                model=str(model) if model else "<unknown-model>",
                context_tokens=context,
                output_tokens=out,
                visible_output_tokens=visible,
                reasoning_output_tokens=reasoning,
                generation_seconds=span_seconds,
                normalized_decode_speed=out / span_seconds,
                input_to_reasoning_end_seconds=input_to_reasoning_end_seconds,
                post_reasoning_output_seconds=post_reasoning_output_seconds,
            )
        )
    return observations


def deterministic_sample(rows: list[T], max_points: int) -> list[T]:
    if len(rows) <= max_points:
        return rows
    rng = random.Random(20260617)
    return rng.sample(rows, max_points)


def bin_quantiles(
    rows: Iterable[T],
    *,
    min_bin_count: int,
    context_fn: Callable[[T], int],
    value_fn: Callable[[T], float | None],
) -> list[tuple[float, float, float, float, int]]:
    buckets: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        context = context_fn(row)
        value = value_fn(row)
        if context <= 0 or value is None or not math.isfinite(value):
            continue
        buckets[int(math.floor(math.log2(context)))].append(value)

    points: list[tuple[float, float, float, float, int]] = []
    for log_bin in sorted(buckets):
        values = buckets[log_bin]
        if len(values) < min_bin_count:
            continue
        context_mid = 2 ** (log_bin + 0.5)
        p25_value = percentile(values, 0.25)
        median_value = percentile(values, 0.50)
        p90_value = percentile(values, 0.90)
        if p25_value is not None and median_value is not None and p90_value is not None:
            points.append((context_mid, p25_value, median_value, p90_value, len(values)))
    return points


def apply_context_axis_window(
    ax: plt.Axes,
    *,
    min_context: float,
    max_context: float,
    first_tick: float,
    max_ticks: int,
) -> None:
    tick = 2 ** math.floor(math.log2(max(min_context, 1.0)))
    ticks: list[float] = []
    while tick <= max_context * 1.000001:
        if tick >= min_context / 1.000001:
            ticks.append(float(tick))
        tick *= 2
    if ticks and ticks[-1] < max_context:
        ticks.append(float(tick))

    if len(ticks) > max_ticks:
        step = math.ceil(len(ticks) / max_ticks)
        ticks = ticks[::step]
        if ticks[-1] < max_context:
            ticks.append(float(tick))
    if not ticks:
        ticks = [min_context, max_context]

    upper_context = max(max_context, ticks[-1])
    lower = token_axis_value(max(min_context, 1.0), first_tick)
    upper = token_axis_value(max(upper_context, min_context, first_tick), first_tick)
    margin = max(0.03, (upper - lower) * 0.025)

    ax.set_xlim(lower - margin, upper + margin)
    ax.set_xticks([token_axis_value(value, first_tick) for value in ticks])
    ax.set_xticklabels([format_token_label(value) for value in ticks])


def write_summary(
    by_provider: dict[str, list[Observation]],
    out_dir: Path,
    *,
    max_speed: float,
) -> Path:
    out = out_dir / "context_decode_speed_summary.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "provider",
                "rows",
                "median_context_tokens",
                "p90_context_tokens",
                "p99_context_tokens",
                "token_weighted_average_normalized_decode_tokens_per_second",
                "p25_normalized_decode_tokens_per_second",
                "median_normalized_decode_tokens_per_second",
                "p90_normalized_decode_tokens_per_second",
                "p99_normalized_decode_tokens_per_second",
                "display_cap_tokens_per_second",
                "rows_above_display_cap",
                "share_above_display_cap",
            ],
        )
        writer.writeheader()
        for provider in provider_order(by_provider):
            rows = by_provider[provider]
            speeds = [row.normalized_decode_speed for row in rows]
            contexts = [row.context_tokens for row in rows]
            clipped = sum(1 for speed in speeds if speed > max_speed)
            writer.writerow(
                {
                    "provider": provider,
                    "rows": len(rows),
                    "median_context_tokens": fmt(percentile(contexts, 0.50), digits=0),
                    "p90_context_tokens": fmt(percentile(contexts, 0.90), digits=0),
                    "p99_context_tokens": fmt(percentile(contexts, 0.99), digits=0),
                    "token_weighted_average_normalized_decode_tokens_per_second": fmt(
                        weighted_speed_average(
                            rows,
                            token_fn=lambda row: row.output_tokens,
                            seconds_fn=lambda row: row.generation_seconds,
                        ),
                        digits=3,
                    ),
                    "p25_normalized_decode_tokens_per_second": fmt(
                        percentile(speeds, 0.25), digits=3
                    ),
                    "median_normalized_decode_tokens_per_second": fmt(
                        percentile(speeds, 0.50), digits=3
                    ),
                    "p90_normalized_decode_tokens_per_second": fmt(
                        percentile(speeds, 0.90), digits=3
                    ),
                    "p99_normalized_decode_tokens_per_second": fmt(
                        percentile(speeds, 0.99), digits=3
                    ),
                    "display_cap_tokens_per_second": fmt(max_speed, digits=3),
                    "rows_above_display_cap": clipped,
                    "share_above_display_cap": fmt(
                        clipped / len(rows) if rows else None, digits=6
                    ),
                }
            )
    print(f"Saved {out}", file=sys.stderr)
    return out


def write_bins(
    by_provider: dict[str, list[Observation]],
    out_dir: Path,
    *,
    min_bin_count: int,
) -> Path:
    out = out_dir / "context_decode_speed_bins.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "provider",
                "context_bin_midpoint_tokens",
                "rows",
                "p25_normalized_decode_tokens_per_second",
                "median_normalized_decode_tokens_per_second",
                "p90_normalized_decode_tokens_per_second",
            ],
        )
        writer.writeheader()
        for provider in provider_order(by_provider):
            for context_mid, p25_speed, median_speed, p90_speed, count in bin_quantiles(
                by_provider[provider],
                min_bin_count=min_bin_count,
                context_fn=lambda row: row.context_tokens,
                value_fn=lambda row: row.normalized_decode_speed,
            ):
                writer.writerow(
                    {
                        "provider": provider,
                        "context_bin_midpoint_tokens": fmt(context_mid, digits=0),
                        "rows": count,
                        "p25_normalized_decode_tokens_per_second": fmt(
                            p25_speed, digits=3
                        ),
                        "median_normalized_decode_tokens_per_second": fmt(
                            median_speed, digits=3
                        ),
                        "p90_normalized_decode_tokens_per_second": fmt(
                            p90_speed, digits=3
                        ),
                    }
                )
    print(f"Saved {out}", file=sys.stderr)
    return out


def eligible_codex_exact_reasoning_row(
    row: Observation, *, min_post_reasoning_seconds: float
) -> bool:
    return (
        row.provider == "codex"
        and row.reasoning_output_tokens > 0
        and row.visible_output_tokens > 0
        and row.post_reasoning_output_seconds is not None
        and row.post_reasoning_output_seconds >= min_post_reasoning_seconds
    )


def codex_decode_latency_seconds_per_token(
    rows: Iterable[Observation], *, min_post_reasoning_seconds: float
) -> float | None:
    total_seconds = 0.0
    total_tokens = 0
    for row in rows:
        if not eligible_codex_exact_reasoning_row(
            row, min_post_reasoning_seconds=min_post_reasoning_seconds
        ):
            continue
        total_seconds += row.post_reasoning_output_seconds or 0
        total_tokens += row.visible_output_tokens
    if total_tokens <= 0:
        return None
    return total_seconds / total_tokens


def codex_pure_decode_speed_points(
    rows: Iterable[Observation], *, min_post_reasoning_seconds: float
) -> list[MetricPoint]:
    points: list[MetricPoint] = []
    for row in rows:
        if not eligible_codex_exact_reasoning_row(
            row, min_post_reasoning_seconds=min_post_reasoning_seconds
        ):
            continue
        points.append(
            MetricPoint(
                context_tokens=row.context_tokens,
                value=row.visible_output_tokens / (row.post_reasoning_output_seconds or 1),
            )
        )
    return points


def codex_ttft_points(
    rows: Iterable[Observation],
    decode_latency_seconds_per_token: float | None,
    *,
    min_post_reasoning_seconds: float,
) -> list[MetricPoint]:
    if decode_latency_seconds_per_token is None:
        return []

    points: list[MetricPoint] = []
    for row in rows:
        if (
            not eligible_codex_exact_reasoning_row(
                row, min_post_reasoning_seconds=min_post_reasoning_seconds
            )
            or row.input_to_reasoning_end_seconds is None
        ):
            continue
        points.append(
            MetricPoint(
                context_tokens=row.context_tokens,
                value=(
                    row.input_to_reasoning_end_seconds
                    - row.reasoning_output_tokens * decode_latency_seconds_per_token
                ),
            )
        )
    return points


def write_codex_latency_summary(
    metric_points: dict[str, list[MetricPoint]],
    out_dir: Path,
    *,
    average_overrides: dict[str, float | None],
) -> Path:
    out = out_dir / "context_decode_speed_codex_timing_summary.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "metric",
                "rows",
                "average",
                "p25",
                "median",
                "p90",
                "p99",
            ],
        )
        writer.writeheader()
        for metric, points in metric_points.items():
            values = [point.value for point in points]
            average = average_overrides.get(metric)
            if average is None and values:
                average = sum(values) / len(values)
            writer.writerow(
                {
                    "metric": metric,
                    "rows": len(points),
                    "average": fmt(average, digits=3),
                    "p25": fmt(percentile(values, 0.25), digits=3),
                    "median": fmt(percentile(values, 0.50), digits=3),
                    "p90": fmt(percentile(values, 0.90), digits=3),
                    "p99": fmt(percentile(values, 0.99), digits=3),
                }
            )
    print(f"Saved {out}", file=sys.stderr)
    return out


def write_codex_latency_bins(
    metric_points: dict[str, list[MetricPoint]],
    out_dir: Path,
    *,
    min_bin_count: int,
) -> Path:
    out = out_dir / "context_decode_speed_codex_timing_bins.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "metric",
                "context_bin_midpoint_tokens",
                "rows",
                "p25",
                "median",
                "p90",
            ],
        )
        writer.writeheader()
        for metric, points in metric_points.items():
            for context_mid, p25_value, median_value, p90_value, count in bin_quantiles(
                points,
                min_bin_count=min_bin_count,
                context_fn=lambda point: point.context_tokens,
                value_fn=lambda point: point.value,
            ):
                writer.writerow(
                    {
                        "metric": metric,
                        "context_bin_midpoint_tokens": fmt(context_mid, digits=0),
                        "rows": count,
                        "p25": fmt(p25_value, digits=3),
                        "median": fmt(median_value, digits=3),
                        "p90": fmt(p90_value, digits=3),
                    }
                )
    print(f"Saved {out}", file=sys.stderr)
    return out


def clipped(value: float, *, y_min: float, y_max: float) -> float:
    return min(max(value, y_min), y_max)


def plot_metric_panel(
    ax: plt.Axes,
    points: list[MetricPoint],
    *,
    title: str,
    ylabel: str,
    unit: str,
    color: str,
    y_min: float,
    y_max: float,
    max_points: int,
    min_context: float,
    max_context: float,
    first_tick: float,
    min_bin_count: int,
    show_x_label: bool,
    average_value: float | None = None,
    average_label: str = "avg",
) -> None:
    sample = deterministic_sample(points, max_points)
    if sample:
        contexts = np.asarray([point.context_tokens for point in sample], dtype=float)
        values = np.asarray(
            [clipped(point.value, y_min=y_min, y_max=y_max) for point in sample],
            dtype=float,
        )
        ax.scatter(
            token_axis_values(contexts, first_tick),
            values,
            s=4.4,
            alpha=0.22,
            linewidths=0,
            color=color,
            rasterized=True,
        )

    quantiles = bin_quantiles(
        points,
        min_bin_count=min_bin_count,
        context_fn=lambda point: point.context_tokens,
        value_fn=lambda point: point.value,
    )
    if quantiles:
        bin_context = np.asarray([point[0] for point in quantiles], dtype=float)
        bin_x = token_axis_values(bin_context, first_tick)
        line_specs = [
            ("bin p90", [point[3] for point in quantiles], (0, (4, 3)), 0.95, 4),
            ("bin median", [point[2] for point in quantiles], "solid", 1.15, 0),
            ("bin p25", [point[1] for point in quantiles], (0, (4, 3)), 0.95, -4),
        ]
        for label, raw_values, linestyle, linewidth, label_dy in line_specs:
            line_y = np.asarray(
                [clipped(value, y_min=y_min, y_max=y_max) for value in raw_values],
                dtype=float,
            )
            ax.plot(
                bin_x,
                line_y,
                color="#111827",
                linewidth=linewidth,
                linestyle=linestyle,
                alpha=0.82,
            )
            va = "center"
            dy = label_dy
            if line_y[-1] >= y_max - (y_max - y_min) * 0.02:
                va = "top"
                dy = -7
            elif line_y[-1] <= y_min + (y_max - y_min) * 0.02:
                va = "bottom"
                dy = 7
            ax.annotate(
                label,
                xy=(bin_x[-1], line_y[-1]),
                xytext=(4, dy),
                textcoords="offset points",
                va=va,
                ha="left",
                fontsize=6.8,
                color="#111827",
                bbox={
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.74,
                    "pad": 0.55,
                },
            )

    ax.set_title(title, fontsize=10.4, fontweight="bold", pad=3)
    ax.set_ylabel(ylabel)
    ax.set_ylim(y_min, y_max)
    ax.set_yticks(np.linspace(y_min, y_max, 5))
    apply_context_axis_window(
        ax,
        min_context=min_context,
        max_context=max_context,
        first_tick=first_tick,
        max_ticks=6,
    )
    if show_x_label:
        ax.set_xlabel("Total input context tokens")
    else:
        ax.set_xlabel("")
        ax.tick_params(axis="x", labelbottom=False)
    ax.grid(True, alpha=0.25)

    values_all = [point.value for point in points if math.isfinite(point.value)]
    if values_all:
        avg_value = average_value
        if avg_value is None:
            avg_value = sum(values_all) / len(values_all)
        p25_value = percentile(values_all, 0.25)
        median_value = percentile(values_all, 0.50)
        p90_value = percentile(values_all, 0.90)
        stats = (
            f"n={len(values_all):,}\n"
            f"{average_label}={avg_value:.1f}{unit}\n"
            f"p25={p25_value:.1f}, med={median_value:.1f}\n"
            f"p90={p90_value:.1f}{unit}"
        )
    else:
        stats = "n=0"
    ax.text(
        0.02,
        0.98,
        stats,
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=7.2,
        color="#526070",
    )


def plot_scatter(
    by_provider: dict[str, list[Observation]],
    metric_points: dict[str, list[MetricPoint]],
    out_dir: Path,
    *,
    max_points_per_provider: int,
    max_speed: float,
    max_pure_decode_speed: float,
    max_ttft_seconds: float,
    min_codex_post_reasoning_seconds: float,
    min_context: float,
    min_bin_count: int,
) -> Path:
    providers = provider_order(by_provider)
    if not providers:
        raise ValueError("No observations to plot")

    max_context = max(row.context_tokens for rows in by_provider.values() for row in rows)
    first_tick = 1024.0

    panel_specs: list[dict[str, object]] = []
    for index, provider in enumerate(providers):
        rows = by_provider[provider]
        panel_specs.append(
            {
                "title": provider_title(provider),
                "ylabel": "Norm. decode\n(tokens/s)",
                "unit": " tok/s",
                "color": plot_color(provider, index),
                "y_min": 0.0,
                "y_max": max_speed,
                "average_value": weighted_speed_average(
                    rows,
                    token_fn=lambda row: row.output_tokens,
                    seconds_fn=lambda row: row.generation_seconds,
                ),
                "average_label": "w.avg",
                "points": [
                    MetricPoint(row.context_tokens, row.normalized_decode_speed)
                    for row in rows
                ],
            }
        )

    codex_color = plot_color("codex", providers.index("codex") if "codex" in providers else 1)
    panel_specs.extend(
        [
            {
                "title": "Codex pure decode",
                "ylabel": "Pure decode\n(tokens/s)",
                "unit": " tok/s",
                "color": codex_color,
                "y_min": 0.0,
                "y_max": max_pure_decode_speed,
                "average_value": weighted_speed_average(
                    by_provider.get("codex", []),
                    token_fn=lambda row: (
                        row.visible_output_tokens
                        if eligible_codex_exact_reasoning_row(
                            row,
                            min_post_reasoning_seconds=min_codex_post_reasoning_seconds,
                        )
                        else 0
                    ),
                    seconds_fn=lambda row: row.post_reasoning_output_seconds,
                ),
                "average_label": "w.avg",
                "points": metric_points.get("codex_pure_decode_tokens_per_second", []),
            },
            {
                "title": "Codex residual TTFT",
                "ylabel": "TTFT\n(s)",
                "unit": " s",
                "color": codex_color,
                "y_min": 0.0,
                "y_max": max_ttft_seconds,
                "average_value": None,
                "average_label": "avg",
                "points": metric_points.get("codex_ttft_seconds", []),
            },
        ]
    )

    fig, axes = plt.subplots(
        len(panel_specs),
        1,
        figsize=(6.1, 1.55 * len(panel_specs)),
        squeeze=False,
        sharex=True,
    )

    for index, spec in enumerate(panel_specs):
        plot_metric_panel(
            axes[index][0],
            spec["points"],  # type: ignore[arg-type]
            title=str(spec["title"]),
            ylabel=str(spec["ylabel"]),
            unit=str(spec["unit"]),
            color=str(spec["color"]),
            y_min=float(spec["y_min"]),
            y_max=float(spec["y_max"]),
            max_points=max_points_per_provider,
            min_context=min_context,
            max_context=max_context,
            first_tick=first_tick,
            min_bin_count=min_bin_count,
            show_x_label=index == len(panel_specs) - 1,
            average_value=spec.get("average_value"),  # type: ignore[arg-type]
            average_label=str(spec.get("average_label", "avg")),
        )

    fig.tight_layout(h_pad=0.45)
    out = out_dir / "context_decode_speed_scatter.png"
    pdf_out = out.with_suffix(".pdf")
    fig.savefig(pdf_out, bbox_inches="tight", facecolor="white")
    print(f"Saved {pdf_out}", file=sys.stderr)
    save_plot(fig, out)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=SCRIPT_DIR)
    parser.add_argument("--max-points-per-provider", type=int, default=35_000)
    parser.add_argument(
        "--max-speed-tokens-per-second",
        type=float,
        default=DEFAULT_MAX_SPEED_TOKENS_PER_SECOND,
        help="cap the plotted y-axis at this normalized decode speed; faster points are clipped",
    )
    parser.add_argument(
        "--max-pure-decode-tokens-per-second",
        type=float,
        default=DEFAULT_MAX_PURE_DECODE_TOKENS_PER_SECOND,
        help="cap the plotted Codex pure-decode y-axis at this tokens/s value",
    )
    parser.add_argument(
        "--max-ttft-seconds",
        type=float,
        default=DEFAULT_MAX_TTFT_SECONDS,
        help="cap the plotted Codex residual TTFT y-axis at this many seconds",
    )
    parser.add_argument(
        "--min-codex-post-reasoning-seconds",
        type=float,
        default=DEFAULT_MIN_CODEX_POST_REASONING_SECONDS,
        help=(
            "minimum post-reasoning span required for Codex pure-decode and "
            "residual-TTFT panels"
        ),
    )
    parser.add_argument(
        "--min-context-tokens",
        type=float,
        default=DEFAULT_MIN_CONTEXT_TOKENS,
        help="left edge of the plotted x-axis window",
    )
    parser.add_argument(
        "--min-bin-count",
        type=int,
        default=250,
        help="minimum observations required for a context-bin median trend point",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    observations = load_observations(con)
    by_provider: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_provider[observation.provider].append(observation)
    codex_latency = codex_decode_latency_seconds_per_token(
        observations,
        min_post_reasoning_seconds=args.min_codex_post_reasoning_seconds,
    )
    metric_points = {
        "codex_pure_decode_tokens_per_second": codex_pure_decode_speed_points(
            observations,
            min_post_reasoning_seconds=args.min_codex_post_reasoning_seconds,
        ),
        "codex_ttft_seconds": codex_ttft_points(
            observations,
            codex_latency,
            min_post_reasoning_seconds=args.min_codex_post_reasoning_seconds,
        ),
    }
    codex_pure_decode_average = 1 / codex_latency if codex_latency else None

    outputs = [
        write_summary(by_provider, args.output_dir, max_speed=args.max_speed_tokens_per_second),
        write_bins(by_provider, args.output_dir, min_bin_count=args.min_bin_count),
        write_codex_latency_summary(
            metric_points,
            args.output_dir,
            average_overrides={
                "codex_pure_decode_tokens_per_second": codex_pure_decode_average,
            },
        ),
        write_codex_latency_bins(
            metric_points,
            args.output_dir,
            min_bin_count=args.min_bin_count,
        ),
        plot_scatter(
            by_provider,
            metric_points,
            args.output_dir,
            max_points_per_provider=args.max_points_per_provider,
            max_speed=args.max_speed_tokens_per_second,
            max_pure_decode_speed=args.max_pure_decode_tokens_per_second,
            max_ttft_seconds=args.max_ttft_seconds,
            min_codex_post_reasoning_seconds=args.min_codex_post_reasoning_seconds,
            min_context=args.min_context_tokens,
            min_bin_count=args.min_bin_count,
        ),
    ]

    png_sidecar.make_self_contained(
        args.output_dir,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=SCRIPT_DIR / "README.md",
    )
    print("Generated:", file=sys.stderr)
    for output in outputs:
        print(f"  {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
