"""Streaming accumulators, samplers, and numeric helpers for trace statistics."""

from __future__ import annotations

from typing import Any
from collections import Counter
from dataclasses import dataclass, field
import math
import numpy as np
import random

APPEND_TOKEN_BINS: list[tuple[str, int, int | None]] = [
    ("0", 0, 1),
    ("1-10", 1, 10),
    ("10-100", 10, 100),
    ("100-1k", 100, 1_000),
    ("1k-10k", 1_000, 10_000),
    ("10k-100k", 10_000, 100_000),
    ("100k-500k", 100_000, 500_000),
    (">=500k", 500_000, None),
]


TOOL_LATENCY_BINS_MS: list[tuple[str, int, int | None]] = [
    ("<10ms", 0, 10),
    ("10-100ms", 10, 100),
    ("100ms-1s", 100, 1_000),
    ("1-10s", 1_000, 10_000),
    ("10s-1m", 10_000, 60_000),
    ("1-10m", 60_000, 600_000),
    ("10m-1h", 600_000, 3_600_000),
    (">=1h", 3_600_000, None),
]


def make_append_token_bins() -> list[AppendTokenBinStats]:
    return [
        AppendTokenBinStats(label=label, lo_tokens=lo, hi_tokens=hi)
        for label, lo, hi in APPEND_TOKEN_BINS
    ]


def make_tool_latency_bins() -> list[ToolLatencyBinStats]:
    return [
        ToolLatencyBinStats(label=label, lo_ms=lo, hi_ms=hi)
        for label, lo, hi in TOOL_LATENCY_BINS_MS
    ]


class ReservoirSampler:
    """Deterministic fixed-size reservoir sampler."""

    def __init__(self, capacity: int, seed: int) -> None:
        self.capacity = max(0, capacity)
        self.seed = seed
        self.rng = random.Random(seed)
        self.seen = 0
        self.values: list[Any] = []

    def add(self, value: Any) -> None:
        self.seen += 1
        if self.capacity == 0:
            return
        if len(self.values) < self.capacity:
            self.values.append(value)
            return
        index = self.rng.randrange(self.seen)
        if index < self.capacity:
            self.values[index] = value

    @property
    def sampled(self) -> bool:
        return self.seen > len(self.values)


@dataclass
class NumericTracker:
    sample_size: int
    seed: int
    count: int = 0
    missing: int = 0
    invalid: int = 0
    total: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    sampler: ReservoirSampler = field(init=False)

    def __post_init__(self) -> None:
        self.sampler = ReservoirSampler(self.sample_size, self.seed)

    def add(self, value: Any, *, allow_zero: bool = True) -> float | None:
        number = safe_float(value)
        if number is None:
            self.missing += 1
            return None
        if not allow_zero and number <= 0:
            self.invalid += 1
            return None
        if allow_zero and number < 0:
            self.invalid += 1
            return None
        self.count += 1
        self.total += number
        self.minimum = number if self.minimum is None else min(self.minimum, number)
        self.maximum = number if self.maximum is None else max(self.maximum, number)
        self.sampler.add(number)
        return number

    def summary(self) -> dict[str, Any]:
        sample = numeric_sample(self.sampler.values)
        quantiles = sample_quantiles(sample)
        return {
            "count": self.count,
            "missing": self.missing,
            "invalid": self.invalid,
            "mean": self.total / self.count if self.count else None,
            "min": self.minimum,
            "max": self.maximum,
            "sample_count": len(sample),
            "sampled": self.sampler.sampled,
            **quantiles,
        }


@dataclass
class TokenGroup:
    sample_size: int
    seed: int
    rows: int = 0
    prefix: NumericTracker = field(init=False)
    append: NumericTracker = field(init=False)
    output: NumericTracker = field(init=False)

    def __post_init__(self) -> None:
        self.prefix = NumericTracker(self.sample_size, self.seed + 1)
        self.append = NumericTracker(self.sample_size, self.seed + 2)
        self.output = NumericTracker(self.sample_size, self.seed + 3)

    def add(self, prefix_tokens: Any, append_tokens: Any, output_tokens: Any) -> None:
        self.rows += 1
        self.prefix.add(prefix_tokens)
        self.append.add(append_tokens)
        self.output.add(output_tokens)

    def summary(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "prefix_tokens": self.prefix.summary(),
            "newly_append_tokens": self.append.summary(),
            "output_tokens": self.output.summary(),
        }


@dataclass
class AppendTokenBinStats:
    label: str
    lo_tokens: int
    hi_tokens: int | None
    rounds: int = 0
    total_append_tokens: float = 0.0

    def add(self, tokens: float) -> None:
        self.rounds += 1
        self.total_append_tokens += tokens


@dataclass
class ToolLatencyBinStats:
    label: str
    lo_ms: int
    hi_ms: int | None
    tool_calls: int = 0
    error_calls: int = 0
    total_latency_ms: float = 0.0

    def add(self, latency_ms: float, *, is_error: bool) -> None:
        self.tool_calls += 1
        self.total_latency_ms += latency_ms
        if is_error:
            self.error_calls += 1


@dataclass
class ToolStats:
    sample_size: int
    seed: int
    calls: int = 0
    latency_count: int = 0
    missing_latency: int = 0
    nonpositive_latency: int = 0
    error_calls: int = 0
    latency_sum: float = 0.0
    latency_min: float | None = None
    latency_max: float | None = None
    providers: Counter[str] = field(default_factory=Counter)
    sampler: ReservoirSampler = field(init=False)

    def __post_init__(self) -> None:
        self.sampler = ReservoirSampler(self.sample_size, self.seed)

    def add(self, tool: dict[str, Any], provider: str) -> None:
        self.calls += 1
        self.providers[provider] += 1
        if tool.get("is_error") is True:
            self.error_calls += 1

        latency = tool_latency_ms(tool)
        if latency is None:
            self.missing_latency += 1
            return
        if latency <= 0:
            self.nonpositive_latency += 1
            return

        self.latency_count += 1
        self.latency_sum += latency
        self.latency_min = (
            latency if self.latency_min is None else min(self.latency_min, latency)
        )
        self.latency_max = (
            latency if self.latency_max is None else max(self.latency_max, latency)
        )
        self.sampler.add(latency)

    def summary(self) -> dict[str, Any]:
        sample = numeric_sample(self.sampler.values)
        return {
            "calls": self.calls,
            "latency_count": self.latency_count,
            "missing_latency": self.missing_latency,
            "nonpositive_latency": self.nonpositive_latency,
            "error_calls": self.error_calls,
            "mean_ms": self.latency_sum / self.latency_count
            if self.latency_count
            else None,
            "min_ms": self.latency_min,
            "max_ms": self.latency_max,
            "sample_count": len(sample),
            "sampled": self.sampler.sampled,
            "providers": dict(sorted(self.providers.items())),
            **sample_quantiles(sample, suffix="_ms"),
        }


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def numeric_sample(values: list[Any]) -> list[float]:
    sample: list[float] = []
    for value in values:
        number = safe_float(value)
        if number is not None:
            sample.append(number)
    return sample


def sample_quantiles(values: list[float], suffix: str = "") -> dict[str, float | None]:
    if not values:
        return {f"p50{suffix}": None, f"p90{suffix}": None, f"p99{suffix}": None}
    arr = np.asarray(values, dtype=float)
    p50, p90, p99 = np.percentile(arr, [50, 90, 99])
    return {
        f"p50{suffix}": float(p50),
        f"p90{suffix}": float(p90),
        f"p99{suffix}": float(p99),
    }


def sample_percentiles(
    values: list[float], percentiles: list[int]
) -> dict[str, float | None]:
    if not values:
        return {f"p{percentile}": None for percentile in percentiles}
    arr = np.asarray(values, dtype=float)
    quantiles = np.percentile(arr, percentiles)
    return {
        f"p{percentile}": float(value)
        for percentile, value in zip(percentiles, quantiles, strict=True)
    }


def in_half_open_bin(value: float, lo: int, hi: int | None) -> bool:
    return value >= lo and (hi is None or value < hi)


def group_key(row: dict[str, Any], mode: str) -> str:
    provider = str(row.get("provider") or "<unknown-provider>")
    model = str(row.get("model") or "<unknown-model>")
    if mode == "provider":
        return provider
    if mode == "model":
        return model
    if mode == "provider_model":
        return f"{provider}:{model}"
    raise ValueError(f"Unsupported group mode: {mode}")


def tool_name(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "<unknown-tool>"


def tool_latency_ms(tool: dict[str, Any]) -> float | None:
    internal = safe_float(tool.get("tool_internal_latency_ms"))
    if internal is not None:
        return internal
    wall = safe_float(tool.get("tool_wall_latency_ms"))
    if wall is not None:
        return wall
    return safe_float(tool.get("latency_ms"))


def selected_token_groups(
    token_groups: dict[str, TokenGroup],
    max_groups: int,
) -> list[tuple[str, TokenGroup]]:
    items = [(key, value) for key, value in token_groups.items() if key != "all"]
    if not items:
        items = [("all", token_groups["all"])]
    items.sort(key=lambda item: item[1].rows, reverse=True)
    return items[:max_groups]
