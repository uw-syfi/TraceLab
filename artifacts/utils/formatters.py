"""Axis formatters and bin/threshold generators for token, latency, and duration scales."""

from __future__ import annotations

import math
import numpy as np

from style import plt
from accumulators import TOOL_LATENCY_BINS_MS

TOOL_LATENCY_CDF_FINE_BINS_PER_DECADE = 64


DURATION_CDF_FINE_BINS_PER_DECADE = 64


CDF_REFERENCE_SECONDS = 5 * 60


CDF_REFERENCE_MS = CDF_REFERENCE_SECONDS * 1000


KV_CACHE_TIMEOUT_MIN_SECONDS = 1


KV_CACHE_TIMEOUT_MAX_SECONDS = 60 * 60


KV_CACHE_TIMEOUT_LANDMARKS_SECONDS = [60, 5 * 60, 10 * 60, 30 * 60, 60 * 60]


KV_CACHE_TIMEOUT_TICK_SECONDS = [
    1,
    10,
    30,
    60,
    2 * 60,
    5 * 60,
    10 * 60,
    15 * 60,
    30 * 60,
    60 * 60,
]


def infer_first_token_tick(values: list[float]) -> float:
    positives = [value for value in values if value > 0]
    if not positives:
        return 1024.0
    return 1024.0 if min(positives) >= 1024 else 1.0


def token_axis_value(value: float, first_tick: float) -> float:
    if value <= 0:
        return 0.0
    if value < first_tick:
        return value / first_tick
    return 1.0 + math.log2(value / first_tick)


def token_axis_values(
    values: list[float] | np.ndarray, first_tick: float
) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.zeros_like(arr)
    positive = arr > 0
    below_first_tick = positive & (arr < first_tick)
    out[below_first_tick] = arr[below_first_tick] / first_tick
    at_or_above = positive & ~below_first_tick
    out[at_or_above] = 1.0 + np.log2(arr[at_or_above] / first_tick)
    return out


def token_axis_bins(
    max_value: float, first_tick: float, bins_per_power: int = 8
) -> np.ndarray:
    axis_max = token_axis_value(max(max_value, first_tick), first_tick)
    bin_count = max(8, int(math.ceil(axis_max * bins_per_power)))
    return np.linspace(0.0, axis_max, bin_count + 1)


def token_axis_bins_with_merged_left(
    max_value: float,
    first_tick: float,
    bins_per_power: int = 8,
) -> np.ndarray:
    """One bin for [0, first_tick), then regular binary-scale bins."""
    axis_max = token_axis_value(max(max_value, first_tick), first_tick)
    if axis_max <= 1.0:
        return np.asarray([0.0, 1.0])
    tail_count = max(1, int(math.ceil((axis_max - 1.0) * bins_per_power)))
    tail = np.linspace(1.0, axis_max, tail_count + 1)
    return np.concatenate((np.asarray([0.0]), tail))


def format_token_label(value: float) -> str:
    if value == 0:
        return "0"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):g}M"
    if value >= 1024:
        return f"{value / 1024:g}k"
    return f"{value:g}"


def binary_token_ticks(
    max_value: float,
    first_tick: float,
    max_ticks: int = 14,
) -> tuple[list[float], list[str]]:
    limit = max(max_value, first_tick)
    raw_ticks = []
    tick = first_tick
    while tick <= limit * 1.000001:
        raw_ticks.append(tick)
        tick *= 2
    if not raw_ticks:
        raw_ticks = [first_tick]

    max_positive_ticks = max(1, max_ticks - 1)
    if len(raw_ticks) > max_positive_ticks:
        step = math.ceil(len(raw_ticks) / max_positive_ticks)
        selected = raw_ticks[::step]
        if raw_ticks[-1] not in selected:
            selected.append(raw_ticks[-1])
    else:
        selected = raw_ticks

    tick_values = [0.0, *selected]
    positions = [token_axis_value(value, first_tick) for value in tick_values]
    labels = [format_token_label(value) for value in tick_values]
    return positions, labels


def apply_binary_token_axis(
    ax: plt.Axes,
    *,
    axis: str,
    max_value: float,
    first_tick: float,
    max_ticks: int = 14,
) -> None:
    positions, labels = binary_token_ticks(max_value, first_tick, max_ticks=max_ticks)
    limit = token_axis_value(max(max_value, first_tick), first_tick)
    margin = max(0.05, limit * 0.02)
    if axis == "x":
        ax.set_xlim(-margin, limit + margin)
        ax.set_xticks(positions)
        ax.set_xticklabels(labels)
    elif axis == "y":
        ax.set_ylim(-margin, limit + margin)
        ax.set_yticks(positions)
        ax.set_yticklabels(labels)
    else:
        raise ValueError(f"Unsupported axis: {axis}")


def format_ms(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


def format_latency_tick(value: float, _pos: int) -> str:
    if value <= 0 or not math.isfinite(value):
        return ""
    if value < 1000:
        return f"{value:g}ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:g}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:g}min"
    hours = minutes / 60
    return f"{hours:g}h"


def format_latency_compact(value_ms: float) -> str:
    if value_ms <= 0 or not math.isfinite(value_ms):
        return ""
    if value_ms < 10:
        return f"{value_ms:.1f}ms"
    if value_ms < 1000:
        return f"{value_ms:.0f}ms"
    seconds = value_ms / 1000
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 10:
        return f"{minutes:.2f}m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    if hours < 10:
        return f"{hours:.2f}h"
    return f"{hours:.1f}h"


def format_duration_seconds_tick(value: float, _pos: int) -> str:
    if value <= 0 or not math.isfinite(value):
        return ""
    if value < 60:
        return f"{value:g}s"
    minutes = value / 60
    if minutes < 60:
        return f"{minutes:g}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:g}h"
    return f"{hours / 24:g}d"


def format_duration_compact(value_seconds: float) -> str:
    if value_seconds <= 0 or not math.isfinite(value_seconds):
        return ""
    if value_seconds < 1:
        return f"{value_seconds * 1000:.0f}ms"
    if value_seconds < 10:
        return f"{value_seconds:.2f}s"
    if value_seconds < 60:
        return f"{value_seconds:.1f}s"
    minutes = value_seconds / 60
    if minutes < 10:
        return f"{minutes:.2f}m"
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    if hours < 10:
        return f"{hours:.2f}h"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def format_seconds_as_hours_compact(value_seconds: float) -> str:
    return format_hours_compact(value_seconds / 3600)


def format_count_tick(value: float, _pos: int) -> str:
    if not math.isfinite(value):
        return ""
    if abs(value) < 1e-9:
        return "0"
    if value < 1:
        return ""
    if value < 1_000:
        return f"{int(value):,}"
    if value < 1_000_000:
        return f"{value / 1_000:g}k"
    return f"{value / 1_000_000:g}M"


def format_hours_tick(value: float, _pos: int) -> str:
    if not math.isfinite(value):
        return ""
    if abs(value) < 1e-9:
        return "0"
    if value < 1:
        return f"{value * 60:g}m"
    if value < 1_000:
        return f"{value:g}h"
    return f"{value / 1_000:g}k h"


def format_hours_compact(value: float) -> str:
    if value < 1:
        return f"{value * 60:.1f}m"
    if value < 1_000:
        return f"{value:.1f}h"
    return f"{value / 1_000:.1f}k h"


def tool_latency_boundaries_ms() -> list[int]:
    return sorted(
        {
            boundary
            for _label, lo_ms, hi_ms in TOOL_LATENCY_BINS_MS
            for boundary in (lo_ms, hi_ms)
            if boundary is not None and boundary > 0
        }
    )


def latency_ticks(max_ms: float) -> list[float]:
    candidates = [0.1, 1, 10, 100, 1_000, 10_000, 60_000, 600_000, 3_600_000]
    ticks = [tick for tick in candidates if tick <= max_ms * 1.05]
    if not ticks:
        ticks = [0.1]
    if max_ms > ticks[-1]:
        ticks.append(10 ** math.ceil(math.log10(max_ms)))
    return ticks


def fine_latency_bin_edges(values_by_provider: dict[str, list[float]]) -> np.ndarray:
    positive_values = [
        value
        for values in values_by_provider.values()
        for value in values
        if value > 0 and math.isfinite(value)
    ]
    if not positive_values:
        return np.asarray([], dtype=float)

    min_value = min(positive_values)
    max_value = max(positive_values)
    lower_power = math.floor(math.log10(min_value))
    upper_power = math.ceil(math.log10(max_value))
    if upper_power <= lower_power:
        upper_power = lower_power + 1
    bin_count = max(
        16,
        int((upper_power - lower_power) * TOOL_LATENCY_CDF_FINE_BINS_PER_DECADE),
    )
    raw_edges = np.logspace(lower_power, upper_power, bin_count + 1)
    boundaries = [
        float(boundary)
        for boundary in tool_latency_boundaries_ms()
        if min_value < boundary < max_value
    ]
    bounded_edges = [float(edge) for edge in raw_edges if min_value < edge < max_value]
    edges = np.asarray(sorted({*bounded_edges, *boundaries, min_value, max_value}))
    return edges


def duration_landmarks_seconds() -> list[float]:
    return [
        0.001,
        0.01,
        0.1,
        1,
        10,
        60,
        10 * 60,
        60 * 60,
        6 * 60 * 60,
        24 * 60 * 60,
        7 * 24 * 60 * 60,
    ]


def fine_duration_bin_edges(values_by_provider: dict[str, list[float]]) -> np.ndarray:
    positive_values = [
        value
        for values in values_by_provider.values()
        for value in values
        if value > 0 and math.isfinite(value)
    ]
    if not positive_values:
        return np.asarray([], dtype=float)

    min_value = min(positive_values)
    max_value = max(positive_values)
    lower_power = math.floor(math.log10(min_value))
    upper_power = math.ceil(math.log10(max_value))
    if upper_power <= lower_power:
        upper_power = lower_power + 1
    bin_count = max(
        16,
        int((upper_power - lower_power) * DURATION_CDF_FINE_BINS_PER_DECADE),
    )
    raw_edges = np.logspace(lower_power, upper_power, bin_count + 1)
    landmarks = [
        float(value)
        for value in duration_landmarks_seconds()
        if min_value < value < max_value
    ]
    bounded_edges = [float(edge) for edge in raw_edges if min_value < edge < max_value]
    return np.asarray(sorted({*bounded_edges, *landmarks, min_value, max_value}))


def bin_edges_with_reference(edges: np.ndarray, reference: float | None) -> np.ndarray:
    if reference is None or edges.size < 2:
        return edges
    if not (edges[0] < reference < edges[-1]):
        return edges
    return np.asarray(sorted({*map(float, edges), float(reference)}), dtype=float)


def kv_cache_timeout_thresholds_seconds() -> np.ndarray:
    lower = KV_CACHE_TIMEOUT_MIN_SECONDS
    upper = KV_CACHE_TIMEOUT_MAX_SECONDS
    decades = math.log10(upper) - math.log10(lower)
    point_count = max(64, int(math.ceil(decades * DURATION_CDF_FINE_BINS_PER_DECADE)))
    fine = np.logspace(math.log10(lower), math.log10(upper), point_count + 1)
    thresholds = {
        *map(float, fine),
        *map(float, KV_CACHE_TIMEOUT_LANDMARKS_SECONDS),
        float(lower),
        float(upper),
    }
    return np.asarray(sorted(thresholds), dtype=float)


def cumulative_values_at_thresholds_seconds(
    values_seconds: list[float],
    thresholds_seconds: np.ndarray,
) -> np.ndarray:
    values = [value for value in values_seconds if value > 0 and math.isfinite(value)]
    if not values or thresholds_seconds.size == 0:
        return np.zeros_like(thresholds_seconds, dtype=float)
    arr = np.sort(np.asarray(values, dtype=float))
    cumulative = np.cumsum(arr)
    indices = np.searchsorted(arr, thresholds_seconds, side="right") - 1
    out = np.zeros_like(thresholds_seconds, dtype=float)
    valid = indices >= 0
    out[valid] = cumulative[indices[valid]]
    return out
