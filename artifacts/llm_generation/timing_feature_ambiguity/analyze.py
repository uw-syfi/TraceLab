#!/usr/bin/env python3
"""Estimate how much timing variation token features cannot explain.

This script answers a feasibility question for timing models built from:

    provider, model, segment kind, cached/prefix tokens, appended tokens,
    and output tokens.

It uses two complementary checks:

1. Exact duplicate pure error.  If two rows have exactly the same feature tuple
   but different durations, no deterministic model using only those features can
   fit both observations.
2. Local neighborhood spread.  Rows are grouped into narrow log-token buckets;
   latency spread inside those buckets estimates how much variation remains
   among "similar" token-feature points.

The output Markdown is intentionally written for human review, not just as a
metric dump.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
TIMING_FIT_DIR = SCRIPT_DIR.parent / "timing_fit"
DEFAULT_INPUT = TIMING_FIT_DIR / "timing_fit_trace.csv"
DEFAULT_FIT_METRICS = TIMING_FIT_DIR / "timing_fit_metrics.csv"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR
DEFAULT_SMOOTH_RELATIVE_THRESHOLD_MS = 1000.0

FEATURE_FIELDS = [
    "provider",
    "model",
    "segment_kind",
    "cached_tokens",
    "append_tokens",
    "segment_output_tokens",
]


@dataclass(frozen=True)
class TimingRow:
    provider: str
    model: str
    segment_kind: str
    prefix_tokens: int
    append_tokens: int
    output_tokens: int
    duration_ms: float
    source_line: int
    session_id: str
    round_index: str

    @property
    def group_key(self) -> tuple[str, str, str]:
        return (self.provider, self.model, self.segment_kind)

    @property
    def feature_key(self) -> tuple[str, str, str, int, int, int]:
        return (
            self.provider,
            self.model,
            self.segment_kind,
            self.prefix_tokens,
            self.append_tokens,
            self.output_tokens,
        )


def finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def intish(value: Any) -> int | None:
    number = finite_float(value)
    if number is None:
        return None
    return int(number)


def fmt_ms(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def fmt_number(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and not math.isfinite(value):
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.{digits}f}"


def fmt_percent(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def group_label(group_key: tuple[str, str, str]) -> str:
    return f"{group_key[0]} / {group_key[1]} / {group_key[2]}"


def quantile(values: list[float], q: float) -> float:
    return float(np.quantile(np.array(values, dtype=np.float64), q))


def weighted_median(values: list[float], weights: list[float]) -> float:
    if len(values) != len(weights):
        raise ValueError("values and weights must have the same length")
    pairs = sorted(zip(values, weights), key=lambda item: item[0])
    total = sum(weight for _, weight in pairs)
    if total <= 0:
        return median(values)
    midpoint = total / 2.0
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= midpoint:
            return value
    return pairs[-1][0]


def describe(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "min": ordered[0],
        "p50": quantile(ordered, 0.50),
        "p90": quantile(ordered, 0.90),
        "p95": quantile(ordered, 0.95),
        "p99": quantile(ordered, 0.99),
        "max": ordered[-1],
        "mean": float(np.mean(np.array(ordered, dtype=np.float64))),
    }


def load_rows(input_path: Path) -> tuple[list[TimingRow], Counter[str]]:
    rows: list[TimingRow] = []
    stats: Counter[str] = Counter()
    with input_path.open("r", encoding="utf-8", newline="") as fh:
        for source_line, raw in enumerate(csv.DictReader(fh), start=2):
            stats["input_rows"] += 1
            prefix = intish(raw.get("cached_tokens"))
            append = intish(raw.get("append_tokens"))
            output = intish(raw.get("segment_output_tokens"))
            duration = finite_float(raw.get("duration_ms"))
            if prefix is None or append is None or output is None or duration is None:
                stats["missing_numeric_field"] += 1
                continue
            if prefix < 0 or append < 0 or output < 0 or duration <= 0:
                stats["invalid_numeric_field"] += 1
                continue
            provider = raw.get("provider") or ""
            model = raw.get("model") or ""
            segment_kind = raw.get("segment_kind") or ""
            if not provider or not model or not segment_kind:
                stats["missing_group_field"] += 1
                continue
            rows.append(
                TimingRow(
                    provider=provider,
                    model=model,
                    segment_kind=segment_kind,
                    prefix_tokens=prefix,
                    append_tokens=append,
                    output_tokens=output,
                    duration_ms=duration,
                    source_line=source_line,
                    session_id=raw.get("session_id", ""),
                    round_index=raw.get("round_index", ""),
                )
            )
    stats["usable_rows"] = len(rows)
    return rows, stats


def summarize_exact_bucket(
    key: tuple[str, str, str, int, int, int],
    rows: list[TimingRow],
    *,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    durations = sorted(row.duration_ms for row in rows)
    med = median(durations)
    mape_constant = weighted_median(durations, [1.0 / value for value in durations])
    smooth_constant = weighted_median(
        durations,
        [1.0 / (value + smooth_relative_threshold_ms) for value in durations],
    )
    abs_error_sum = sum(abs(value - med) for value in durations)
    relative_abs_error_sum = (
        sum(abs(value - med) / med for value in durations)
        if med > 0
        else 0.0
    )
    mape_error_sum = sum(abs(mape_constant - value) / value for value in durations)
    smooth_relative_error_sum = sum(
        abs(smooth_constant - value) / (value + smooth_relative_threshold_ms)
        for value in durations
    )
    return {
        "provider": key[0],
        "model": key[1],
        "segment_kind": key[2],
        "cached_tokens": key[3],
        "append_tokens": key[4],
        "segment_output_tokens": key[5],
        "rows": len(rows),
        "min_duration_ms": durations[0],
        "median_duration_ms": med,
        "mape_optimal_duration_ms": mape_constant,
        "smooth_relative_optimal_duration_ms": smooth_constant,
        "max_duration_ms": durations[-1],
        "spread_ms": durations[-1] - durations[0],
        "ratio": durations[-1] / max(1.0, durations[0]),
        "best_constant_abs_error_sum_ms": abs_error_sum,
        "best_constant_mae_ms": abs_error_sum / len(durations),
        "best_constant_relative_abs_error_sum": relative_abs_error_sum,
        "best_constant_relative_mae_ratio": relative_abs_error_sum / len(durations),
        "best_constant_mape_sum": mape_error_sum,
        "best_constant_mape": mape_error_sum / len(durations),
        "best_constant_smooth_relative_sum": smooth_relative_error_sum,
        "best_constant_smooth_relative_loss": smooth_relative_error_sum / len(durations),
        "min_source_line": min(row.source_line for row in rows),
        "max_source_line": max(row.source_line for row in rows),
    }


def exact_duplicate_analysis(
    rows: list[TimingRow],
    top_n: int,
    *,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    buckets: dict[tuple[str, str, str, int, int, int], list[TimingRow]] = defaultdict(list)
    for row in rows:
        buckets[row.feature_key].append(row)

    duplicate_summaries = [
        summarize_exact_bucket(
            key,
            bucket_rows,
            smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        )
        for key, bucket_rows in buckets.items()
        if len(bucket_rows) >= 2
    ]
    duplicate_summaries.sort(key=lambda row: (row["spread_ms"], row["ratio"]), reverse=True)

    per_group: dict[str, dict[str, Any]] = {}
    for summary in duplicate_summaries:
        key = (summary["provider"], summary["model"], summary["segment_kind"])
        label = "::".join(key)
        entry = per_group.setdefault(
            label,
            {
                "provider": key[0],
                "model": key[1],
                "segment_kind": key[2],
                "duplicate_feature_keys": 0,
                "duplicate_rows": 0,
                "best_constant_abs_error_sum_ms": 0.0,
                "best_constant_relative_abs_error_sum": 0.0,
                "best_constant_mape_sum": 0.0,
                "best_constant_smooth_relative_sum": 0.0,
                "largest_spread_ms": 0.0,
            },
        )
        entry["duplicate_feature_keys"] += 1
        entry["duplicate_rows"] += summary["rows"]
        entry["best_constant_abs_error_sum_ms"] += summary["best_constant_abs_error_sum_ms"]
        entry["best_constant_relative_abs_error_sum"] += summary["best_constant_relative_abs_error_sum"]
        entry["best_constant_mape_sum"] += summary["best_constant_mape_sum"]
        entry["best_constant_smooth_relative_sum"] += summary[
            "best_constant_smooth_relative_sum"
        ]
        entry["largest_spread_ms"] = max(entry["largest_spread_ms"], summary["spread_ms"])

    for entry in per_group.values():
        entry["best_constant_mae_ms"] = (
            entry["best_constant_abs_error_sum_ms"] / entry["duplicate_rows"]
            if entry["duplicate_rows"]
            else None
        )
        entry["best_constant_relative_mae_ratio"] = (
            entry["best_constant_relative_abs_error_sum"] / entry["duplicate_rows"]
            if entry["duplicate_rows"]
            else None
        )
        entry["best_constant_mape"] = (
            entry["best_constant_mape_sum"] / entry["duplicate_rows"]
            if entry["duplicate_rows"]
            else None
        )
        entry["best_constant_smooth_relative_loss"] = (
            entry["best_constant_smooth_relative_sum"] / entry["duplicate_rows"]
            if entry["duplicate_rows"]
            else None
        )

    duplicate_rows = sum(row["rows"] for row in duplicate_summaries)
    total_best_abs_error = sum(row["best_constant_abs_error_sum_ms"] for row in duplicate_summaries)
    total_best_relative_abs_error = sum(
        row["best_constant_relative_abs_error_sum"] for row in duplicate_summaries
    )
    total_best_mape_error = sum(row["best_constant_mape_sum"] for row in duplicate_summaries)
    total_best_smooth_relative_error = sum(
        row["best_constant_smooth_relative_sum"] for row in duplicate_summaries
    )
    return {
        "feature_keys": len(buckets),
        "duplicate_feature_keys": len(duplicate_summaries),
        "duplicate_rows": duplicate_rows,
        "duplicate_subset_best_possible_mae_ms": (
            total_best_abs_error / duplicate_rows if duplicate_rows else None
        ),
        "duplicate_subset_best_possible_relative_mae_ratio": (
            total_best_relative_abs_error / duplicate_rows if duplicate_rows else None
        ),
        "duplicate_subset_best_possible_mape": (
            total_best_mape_error / duplicate_rows if duplicate_rows else None
        ),
        "duplicate_subset_best_possible_smooth_relative_loss": (
            total_best_smooth_relative_error / duplicate_rows if duplicate_rows else None
        ),
        "per_group": per_group,
        "top_by_spread": duplicate_summaries[:top_n],
        "top_by_ratio_min_100ms": sorted(
            [row for row in duplicate_summaries if row["min_duration_ms"] >= 100],
            key=lambda row: (row["ratio"], row["spread_ms"]),
            reverse=True,
        )[:top_n],
    }


def token_bin(value: int, step: float) -> int:
    if value <= 0:
        return -1_000_000
    return int(math.floor(math.log2(value) / step))


def local_bucket_key(row: TimingRow, step: float) -> tuple[str, str, str, int, int, int]:
    return (
        row.provider,
        row.model,
        row.segment_kind,
        token_bin(row.prefix_tokens, step),
        token_bin(row.append_tokens, step),
        token_bin(row.output_tokens, step),
    )


def summarize_local_bucket(
    key: tuple[str, str, str, int, int, int],
    rows: list[TimingRow],
    *,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    durations = sorted(row.duration_ms for row in rows)
    med = median(durations)
    mape_constant = weighted_median(durations, [1.0 / value for value in durations])
    smooth_constant = weighted_median(
        durations,
        [1.0 / (value + smooth_relative_threshold_ms) for value in durations],
    )
    abs_error_sum = sum(abs(value - med) for value in durations)
    sq_error_sum = sum((value - med) ** 2 for value in durations)
    relative_abs_error_sum = (
        sum(abs(value - med) / med for value in durations)
        if med > 0
        else 0.0
    )
    mape_error_sum = sum(abs(mape_constant - value) / value for value in durations)
    smooth_relative_error_sum = sum(
        abs(smooth_constant - value) / (value + smooth_relative_threshold_ms)
        for value in durations
    )
    p10 = quantile(durations, 0.10)
    p90 = quantile(durations, 0.90)
    prefixes = [row.prefix_tokens for row in rows]
    appends = [row.append_tokens for row in rows]
    outputs = [row.output_tokens for row in rows]
    return {
        "provider": key[0],
        "model": key[1],
        "segment_kind": key[2],
        "rows": len(rows),
        "duration_min_ms": durations[0],
        "duration_p10_ms": p10,
        "duration_median_ms": med,
        "mape_optimal_duration_ms": mape_constant,
        "smooth_relative_optimal_duration_ms": smooth_constant,
        "duration_p90_ms": p90,
        "duration_max_ms": durations[-1],
        "p90_minus_p10_ms": p90 - p10,
        "p90_minus_p10_ratio": (p90 - p10) / med if med > 0 else None,
        "spread_ms": durations[-1] - durations[0],
        "ratio": durations[-1] / max(1.0, durations[0]),
        "best_constant_abs_error_sum_ms": abs_error_sum,
        "best_constant_sq_error_sum_ms": sq_error_sum,
        "best_constant_mae_ms": abs_error_sum / len(durations),
        "best_constant_relative_abs_error_sum": relative_abs_error_sum,
        "best_constant_relative_mae_ratio": relative_abs_error_sum / len(durations),
        "best_constant_mape_sum": mape_error_sum,
        "best_constant_mape": mape_error_sum / len(durations),
        "best_constant_smooth_relative_sum": smooth_relative_error_sum,
        "best_constant_smooth_relative_loss": smooth_relative_error_sum / len(durations),
        "prefix_min": min(prefixes),
        "prefix_max": max(prefixes),
        "append_min": min(appends),
        "append_max": max(appends),
        "output_min": min(outputs),
        "output_max": max(outputs),
    }


def local_neighborhood_analysis(
    rows: list[TimingRow],
    *,
    min_group_rows: int,
    min_bucket_rows: int,
    bin_step: float,
    trim_quantile: float | None,
    smooth_relative_threshold_ms: float,
    top_n: int,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str], list[TimingRow]] = defaultdict(list)
    for row in rows:
        grouped[row.group_key].append(row)

    group_results: dict[str, dict[str, Any]] = {}
    top_buckets: list[dict[str, Any]] = []
    total_rows_after_trim = 0
    total_rows_in_local_buckets = 0
    total_abs_error = 0.0
    total_sq_error = 0.0
    total_relative_abs_error = 0.0
    total_mape_error = 0.0
    total_smooth_relative_error = 0.0
    total_weighted_median_duration = 0.0
    total_weighted_p90_p10 = 0.0
    total_weighted_p90_p10_ratio = 0.0

    for group_key_value, group_rows in sorted(grouped.items()):
        if len(group_rows) < min_group_rows:
            continue
        durations = [row.duration_ms for row in group_rows]
        cutoff = quantile(durations, trim_quantile) if trim_quantile is not None else None
        trimmed = [
            row for row in group_rows
            if cutoff is None or row.duration_ms <= cutoff
        ]
        total_rows_after_trim += len(trimmed)

        buckets: dict[tuple[str, str, str, int, int, int], list[TimingRow]] = defaultdict(list)
        for row in trimmed:
            buckets[local_bucket_key(row, bin_step)].append(row)

        bucket_summaries = [
            summarize_local_bucket(
                key,
                bucket_rows,
                smooth_relative_threshold_ms=smooth_relative_threshold_ms,
            )
            for key, bucket_rows in buckets.items()
            if len(bucket_rows) >= min_bucket_rows
        ]
        if not bucket_summaries:
            continue

        rows_in_buckets = sum(row["rows"] for row in bucket_summaries)
        abs_error = sum(row["best_constant_abs_error_sum_ms"] for row in bucket_summaries)
        sq_error = sum(row["best_constant_sq_error_sum_ms"] for row in bucket_summaries)
        relative_abs_error = sum(
            row["best_constant_relative_abs_error_sum"] for row in bucket_summaries
        )
        mape_error = sum(row["best_constant_mape_sum"] for row in bucket_summaries)
        smooth_relative_error = sum(
            row["best_constant_smooth_relative_sum"] for row in bucket_summaries
        )
        weighted_median_duration = sum(
            row["duration_median_ms"] * row["rows"] for row in bucket_summaries
        )
        weighted_p90_p10 = sum(row["p90_minus_p10_ms"] * row["rows"] for row in bucket_summaries)
        weighted_p90_p10_ratio = sum(
            (row["p90_minus_p10_ratio"] or 0.0) * row["rows"]
            for row in bucket_summaries
        )
        total_rows_in_local_buckets += rows_in_buckets
        total_abs_error += abs_error
        total_sq_error += sq_error
        total_relative_abs_error += relative_abs_error
        total_mape_error += mape_error
        total_smooth_relative_error += smooth_relative_error
        total_weighted_median_duration += weighted_median_duration
        total_weighted_p90_p10 += weighted_p90_p10
        total_weighted_p90_p10_ratio += weighted_p90_p10_ratio

        group_result = {
            "provider": group_key_value[0],
            "model": group_key_value[1],
            "segment_kind": group_key_value[2],
            "raw_rows": len(group_rows),
            "rows_after_trim": len(trimmed),
            "trim_cutoff_ms": cutoff,
            "local_bucket_count": len(bucket_summaries),
            "local_rows": rows_in_buckets,
            "local_row_coverage": rows_in_buckets / len(trimmed) if trimmed else 0.0,
            "local_best_constant_mae_ms": abs_error / rows_in_buckets,
            "local_best_constant_rmse_ms": math.sqrt(sq_error / rows_in_buckets),
            "local_best_constant_relative_mae_ratio": relative_abs_error / rows_in_buckets,
            "local_best_constant_mape": mape_error / rows_in_buckets,
            "local_best_constant_smooth_relative_loss": (
                smooth_relative_error / rows_in_buckets
            ),
            "weighted_local_median_duration_ms": weighted_median_duration / rows_in_buckets,
            "local_best_constant_mae_ratio_vs_median": (
                (abs_error / rows_in_buckets) / (weighted_median_duration / rows_in_buckets)
                if weighted_median_duration > 0
                else None
            ),
            "weighted_p90_minus_p10_ms": weighted_p90_p10 / rows_in_buckets,
            "weighted_p90_minus_p10_ratio": weighted_p90_p10_ratio / rows_in_buckets,
            "weighted_p90_minus_p10_ratio_vs_median": (
                (weighted_p90_p10 / rows_in_buckets) / (weighted_median_duration / rows_in_buckets)
                if weighted_median_duration > 0
                else None
            ),
            "median_bucket_p90_minus_p10_ms": median(
                row["p90_minus_p10_ms"] for row in bucket_summaries
            ),
            "median_bucket_relative_mae_ratio": median(
                row["best_constant_relative_mae_ratio"] for row in bucket_summaries
            ),
            "median_bucket_mape": median(
                row["best_constant_mape"] for row in bucket_summaries
            ),
            "median_bucket_smooth_relative_loss": median(
                row["best_constant_smooth_relative_loss"] for row in bucket_summaries
            ),
            "largest_local_spread_ms": max(row["spread_ms"] for row in bucket_summaries),
        }
        group_results["::".join(group_key_value)] = group_result

        for bucket in bucket_summaries:
            bucket = dict(bucket)
            bucket["group_key"] = "::".join(group_key_value)
            top_buckets.append(bucket)

    top_buckets.sort(key=lambda row: (row["p90_minus_p10_ms"], row["spread_ms"]), reverse=True)
    return {
        "config": {
            "min_group_rows": min_group_rows,
            "min_bucket_rows": min_bucket_rows,
            "bin_step_log2": bin_step,
            "trim_quantile": trim_quantile,
            "smooth_relative_threshold_ms": smooth_relative_threshold_ms,
        },
        "total_rows_after_trim": total_rows_after_trim,
        "total_rows_in_local_buckets": total_rows_in_local_buckets,
        "local_row_coverage": (
            total_rows_in_local_buckets / total_rows_after_trim
            if total_rows_after_trim
            else 0.0
        ),
        "weighted_local_best_constant_mae_ms": (
            total_abs_error / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_best_constant_rmse_ms": (
            math.sqrt(total_sq_error / total_rows_in_local_buckets)
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_best_constant_relative_mae_ratio": (
            total_relative_abs_error / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_best_constant_mape": (
            total_mape_error / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_best_constant_smooth_relative_loss": (
            total_smooth_relative_error / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_median_duration_ms": (
            total_weighted_median_duration / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_local_best_constant_mae_ratio_vs_median": (
            (total_abs_error / total_rows_in_local_buckets)
            / (total_weighted_median_duration / total_rows_in_local_buckets)
            if total_rows_in_local_buckets and total_weighted_median_duration > 0
            else None
        ),
        "weighted_p90_minus_p10_ms": (
            total_weighted_p90_p10 / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "weighted_p90_minus_p10_ratio_vs_median": (
            (total_weighted_p90_p10 / total_rows_in_local_buckets)
            / (total_weighted_median_duration / total_rows_in_local_buckets)
            if total_rows_in_local_buckets and total_weighted_median_duration > 0
            else None
        ),
        "weighted_p90_minus_p10_ratio": (
            total_weighted_p90_p10_ratio / total_rows_in_local_buckets
            if total_rows_in_local_buckets
            else None
        ),
        "per_group": group_results,
        "top_local_buckets": top_buckets[:top_n],
    }


def load_fit_metrics(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    metrics: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("skipped"):
                continue
            group_key_value = row.get("group_key")
            if not group_key_value:
                continue
            parsed = dict(row)
            for field in [
                "raw_rows",
                "fit_rows",
                "test_r2",
                "test_mae_ms",
                "test_rmse_ms",
                "test_median_abs_error_ms",
                "test_mape",
                "test_median_absolute_percentage_error",
                "test_p90_absolute_percentage_error",
                "test_smooth_relative_absolute_error",
                "test_median_smooth_relative_absolute_error",
                "test_p90_smooth_relative_absolute_error",
                "log_target_test_r2",
                "log_target_test_mae_ms",
                "log_target_test_rmse_ms",
                "log_target_test_mape",
                "log_target_test_median_absolute_percentage_error",
                "log_target_test_p90_absolute_percentage_error",
                "log_target_test_smooth_relative_absolute_error",
                "log_target_test_median_smooth_relative_absolute_error",
                "log_target_test_p90_smooth_relative_absolute_error",
                "log_target_test_mean_abs_log_error",
                "log_target_test_multiplicative_mae",
                "smooth_target_test_r2",
                "smooth_target_test_mae_ms",
                "smooth_target_test_rmse_ms",
                "smooth_target_test_mape",
                "smooth_target_test_median_absolute_percentage_error",
                "smooth_target_test_p90_absolute_percentage_error",
                "smooth_target_test_smooth_relative_absolute_error",
                "smooth_target_test_median_smooth_relative_absolute_error",
                "smooth_target_test_p90_smooth_relative_absolute_error",
                "smooth_target_test_mean_abs_log_error",
                "smooth_target_test_multiplicative_mae",
            ]:
                value = finite_float(parsed.get(field))
                parsed[field] = value
            metrics[group_key_value] = parsed
    return metrics


def compare_with_fit(
    local: dict[str, Any],
    fit_metrics: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group_key_value, local_row in local["per_group"].items():
        fit = fit_metrics.get(group_key_value)
        if not fit or fit.get("test_mae_ms") is None:
            continue
        local_mae = local_row["local_best_constant_mae_ms"]
        fit_mae = fit["test_mae_ms"]
        local_median_duration = local_row["weighted_local_median_duration_ms"]
        rows.append(
            {
                "group_key": group_key_value,
                "provider": local_row["provider"],
                "model": local_row["model"],
                "segment_kind": local_row["segment_kind"],
                "local_rows": local_row["local_rows"],
                "local_coverage": local_row["local_row_coverage"],
                "local_mae_ms": local_mae,
                "local_median_duration_ms": local_median_duration,
                "local_relative_mae_ratio": local_row["local_best_constant_relative_mae_ratio"],
                "local_mae_ratio_vs_median": local_row["local_best_constant_mae_ratio_vs_median"],
                "local_mape_floor": local_row["local_best_constant_mape"],
                "local_smooth_relative_floor": local_row[
                    "local_best_constant_smooth_relative_loss"
                ],
                "local_p90_minus_p10_ms": local_row["weighted_p90_minus_p10_ms"],
                "local_p90_minus_p10_ratio": local_row["weighted_p90_minus_p10_ratio"],
                "local_p90_minus_p10_ratio_vs_median": local_row["weighted_p90_minus_p10_ratio_vs_median"],
                "fit_rows": fit.get("fit_rows"),
                "fit_test_mae_ms": fit_mae,
                "fit_test_rmse_ms": fit.get("test_rmse_ms"),
                "fit_test_r2": fit.get("test_r2"),
                "fit_test_mape": fit.get("test_mape"),
                "fit_test_smooth_relative_absolute_error": fit.get(
                    "test_smooth_relative_absolute_error"
                ),
                "fit_test_median_absolute_percentage_error": fit.get(
                    "test_median_absolute_percentage_error"
                ),
                "fit_test_p90_absolute_percentage_error": fit.get(
                    "test_p90_absolute_percentage_error"
                ),
                "log_target_test_r2": fit.get("log_target_test_r2"),
                "log_target_test_mae_ms": fit.get("log_target_test_mae_ms"),
                "log_target_test_rmse_ms": fit.get("log_target_test_rmse_ms"),
                "log_target_test_mape": fit.get("log_target_test_mape"),
                "log_target_test_smooth_relative_absolute_error": fit.get(
                    "log_target_test_smooth_relative_absolute_error"
                ),
                "log_target_test_median_absolute_percentage_error": fit.get(
                    "log_target_test_median_absolute_percentage_error"
                ),
                "log_target_test_p90_absolute_percentage_error": fit.get(
                    "log_target_test_p90_absolute_percentage_error"
                ),
                "log_target_test_mean_abs_log_error": fit.get(
                    "log_target_test_mean_abs_log_error"
                ),
                "log_target_test_multiplicative_mae": fit.get(
                    "log_target_test_multiplicative_mae"
                ),
                "smooth_target_test_r2": fit.get("smooth_target_test_r2"),
                "smooth_target_test_mae_ms": fit.get("smooth_target_test_mae_ms"),
                "smooth_target_test_rmse_ms": fit.get("smooth_target_test_rmse_ms"),
                "smooth_target_test_mape": fit.get("smooth_target_test_mape"),
                "smooth_target_test_smooth_relative_absolute_error": fit.get(
                    "smooth_target_test_smooth_relative_absolute_error"
                ),
                "smooth_target_test_mean_abs_log_error": fit.get(
                    "smooth_target_test_mean_abs_log_error"
                ),
                "smooth_target_test_multiplicative_mae": fit.get(
                    "smooth_target_test_multiplicative_mae"
                ),
                "fit_relative_mae_ratio": (
                    fit_mae / local_median_duration if local_median_duration > 0 else None
                ),
                "fit_over_local_mape_floor": (
                    fit.get("test_mape") / local_row["local_best_constant_mape"]
                    if fit.get("test_mape") is not None
                    and local_row["local_best_constant_mape"] > 0
                    else None
                ),
                "log_target_over_local_mape_floor": (
                    fit.get("log_target_test_mape") / local_row["local_best_constant_mape"]
                    if fit.get("log_target_test_mape") is not None
                    and local_row["local_best_constant_mape"] > 0
                    else None
                ),
                "fit_over_local_smooth_relative_floor": (
                    fit.get("test_smooth_relative_absolute_error")
                    / local_row["local_best_constant_smooth_relative_loss"]
                    if fit.get("test_smooth_relative_absolute_error") is not None
                    and local_row["local_best_constant_smooth_relative_loss"] > 0
                    else None
                ),
                "log_target_over_local_smooth_relative_floor": (
                    fit.get("log_target_test_smooth_relative_absolute_error")
                    / local_row["local_best_constant_smooth_relative_loss"]
                    if fit.get("log_target_test_smooth_relative_absolute_error") is not None
                    and local_row["local_best_constant_smooth_relative_loss"] > 0
                    else None
                ),
                "smooth_target_over_local_smooth_relative_floor": (
                    fit.get("smooth_target_test_smooth_relative_absolute_error")
                    / local_row["local_best_constant_smooth_relative_loss"]
                    if fit.get("smooth_target_test_smooth_relative_absolute_error") is not None
                    and local_row["local_best_constant_smooth_relative_loss"] > 0
                    else None
                ),
                "fit_minus_local_mae_ms": fit_mae - local_mae,
                "fit_over_local_mae": fit_mae / local_mae if local_mae > 0 else None,
            }
        )
    rows.sort(key=lambda row: row["local_rows"], reverse=True)
    return rows


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_report(
    *,
    input_path: Path,
    fit_metrics_path: Path | None,
    load_stats: Counter[str],
    exact: dict[str, Any],
    local: dict[str, Any],
    fit_comparison: list[dict[str, Any]],
    top_groups: int,
    top_examples: int,
) -> str:
    exact_mae = exact["duplicate_subset_best_possible_mae_ms"]
    exact_relative_mae = exact["duplicate_subset_best_possible_relative_mae_ratio"]
    exact_mape = exact["duplicate_subset_best_possible_mape"]
    exact_smooth_relative_loss = exact[
        "duplicate_subset_best_possible_smooth_relative_loss"
    ]
    local_mae = local["weighted_local_best_constant_mae_ms"]
    local_rmse = local["weighted_local_best_constant_rmse_ms"]
    local_mape = local["weighted_local_best_constant_mape"]
    local_smooth_relative_loss = local[
        "weighted_local_best_constant_smooth_relative_loss"
    ]
    local_median_duration = local["weighted_local_median_duration_ms"]
    local_mae_ratio = local["weighted_local_best_constant_mae_ratio_vs_median"]
    local_p90_p10 = local["weighted_p90_minus_p10_ms"]
    local_p90_p10_ratio = local["weighted_p90_minus_p10_ratio_vs_median"]
    fit_rows = [row for row in fit_comparison if row.get("fit_test_mae_ms") is not None]
    weighted_fit_mae = None
    weighted_fit_over_local = None
    weighted_fit_relative_mae = None
    weighted_fit_mape = None
    weighted_log_fit_mape = None
    weighted_fit_over_local_mape = None
    weighted_log_fit_over_local_mape = None
    weighted_fit_smooth_relative_loss = None
    weighted_log_fit_smooth_relative_loss = None
    weighted_smooth_fit_smooth_relative_loss = None
    weighted_fit_over_local_smooth_relative = None
    weighted_log_fit_over_local_smooth_relative = None
    weighted_smooth_fit_over_local_smooth_relative = None
    if fit_rows:
        total_weight = sum(row["local_rows"] for row in fit_rows)
        weighted_fit_mae = sum(row["fit_test_mae_ms"] * row["local_rows"] for row in fit_rows) / total_weight
        weighted_fit_relative_mae = (
            weighted_fit_mae / local_median_duration
            if local_median_duration is not None and local_median_duration > 0
            else None
        )
        weighted_fit_over_local = (
            weighted_fit_mae / local_mae
            if local_mae is not None and local_mae > 0
            else None
        )
        mape_rows = [
            row for row in fit_rows
            if row.get("fit_test_mape") is not None
            and row.get("log_target_test_mape") is not None
        ]
        if mape_rows:
            mape_weight = sum(row["local_rows"] for row in mape_rows)
            weighted_fit_mape = (
                sum(row["fit_test_mape"] * row["local_rows"] for row in mape_rows)
                / mape_weight
            )
            weighted_log_fit_mape = (
                sum(row["log_target_test_mape"] * row["local_rows"] for row in mape_rows)
                / mape_weight
            )
            weighted_fit_over_local_mape = (
                weighted_fit_mape / local_mape
                if local_mape is not None and local_mape > 0
                else None
            )
            weighted_log_fit_over_local_mape = (
                weighted_log_fit_mape / local_mape
                if local_mape is not None and local_mape > 0
                else None
            )
        smooth_rows = [
            row for row in fit_rows
            if row.get("fit_test_smooth_relative_absolute_error") is not None
            and row.get("log_target_test_smooth_relative_absolute_error") is not None
            and row.get("smooth_target_test_smooth_relative_absolute_error") is not None
        ]
        if smooth_rows:
            smooth_weight = sum(row["local_rows"] for row in smooth_rows)
            weighted_fit_smooth_relative_loss = (
                sum(
                    row["fit_test_smooth_relative_absolute_error"] * row["local_rows"]
                    for row in smooth_rows
                )
                / smooth_weight
            )
            weighted_log_fit_smooth_relative_loss = (
                sum(
                    row["log_target_test_smooth_relative_absolute_error"] * row["local_rows"]
                    for row in smooth_rows
                )
                / smooth_weight
            )
            weighted_smooth_fit_smooth_relative_loss = (
                sum(
                    row["smooth_target_test_smooth_relative_absolute_error"] * row["local_rows"]
                    for row in smooth_rows
                )
                / smooth_weight
            )
            weighted_fit_over_local_smooth_relative = (
                weighted_fit_smooth_relative_loss / local_smooth_relative_loss
                if local_smooth_relative_loss is not None and local_smooth_relative_loss > 0
                else None
            )
            weighted_log_fit_over_local_smooth_relative = (
                weighted_log_fit_smooth_relative_loss / local_smooth_relative_loss
                if local_smooth_relative_loss is not None and local_smooth_relative_loss > 0
                else None
            )
            weighted_smooth_fit_over_local_smooth_relative = (
                weighted_smooth_fit_smooth_relative_loss / local_smooth_relative_loss
                if local_smooth_relative_loss is not None and local_smooth_relative_loss > 0
                else None
            )

    lines = [
        "# Timing Irreducible Error",
        "",
        "## Question",
        "",
        (
            "Can observed LLM-step latency be explained by token counts alone: "
            "`cached/prefix tokens`, `append tokens`, and `output tokens`, after "
            "splitting by provider, model, and segment kind?"
        ),
        "",
        "The short answer is **no, not exactly**.  Token counts carry a strong signal, "
        "but the trace also contains latency variation that these fields cannot "
        "determine.  Some of that variation is visible even when the token features "
        "are identical; more is visible when the features are merely very close.",
        "",
        "## Data",
        "",
        f"- Input CSV: `{input_path}`",
        f"- Rows scanned: {load_stats['input_rows']:,}",
        f"- Usable timing rows: {load_stats['usable_rows']:,}",
        f"- Feature tuple: `{', '.join(FEATURE_FIELDS)}`",
        f"- Fit metrics compared from: `{fit_metrics_path}`" if fit_metrics_path else "- Fit metrics compared from: n/a",
        "",
        "## Method",
        "",
        "Two checks are used.",
        "",
        (
            "1. **Exact duplicate pure error.** Rows with exactly the same provider, "
            "model, segment kind, prefix/cache tokens, append tokens, and output "
            "tokens are grouped together.  If their durations differ, then no "
            "deterministic model using only those features can fit all rows in that "
            "bucket."
        ),
        "",
        (
            "2. **Local-neighborhood spread.** Rows are placed into narrow log-token "
            f"buckets (`log2` bin width {local['config']['bin_step_log2']}).  This is "
            "not a hard proof like exact duplicates, but it estimates the conditional "
            "noise floor among nearby token-feature points.  Very rare long-tail "
            f"stalls are reduced by a per-group p{int(local['config']['trim_quantile'] * 100)} "
            "duration trim for this local estimate."
        ),
        "",
        "## Main Results",
        "",
        (
            f"- Exact duplicate feature keys: {exact['duplicate_feature_keys']:,}; "
            f"rows inside those keys: {exact['duplicate_rows']:,}."
        ),
        (
            "- Best possible MAE on exact duplicate rows, even with the best constant "
            f"prediction per duplicate bucket: **{fmt_ms(exact_mae)}** "
            f"(**{fmt_percent(exact_relative_mae)}** relative to each duplicate bucket's median latency)."
        ),
        (
            "- Best possible MAPE on exact duplicate rows, using each duplicate "
            f"bucket's MAPE-optimal constant prediction: **{fmt_percent(exact_mape)}**."
        ),
        (
            "- Best possible smooth relative loss on exact duplicate rows, using "
            "threshold `T=1s`: "
            f"**{fmt_percent(exact_smooth_relative_loss)}**."
        ),
        (
            f"- Local-neighborhood rows covered: {local['total_rows_in_local_buckets']:,} "
            f"of {local['total_rows_after_trim']:,} trimmed rows "
            f"({local['local_row_coverage'] * 100:.1f}%)."
        ),
        (
            "- Local-neighborhood best constant MAE around the local median: "
            f"**{fmt_ms(local_mae)}**; RMSE: **{fmt_ms(local_rmse)}**."
        ),
        (
            "- Local-neighborhood relative MAE: "
            f"**{fmt_percent(local_mae_ratio)}** of typical local latency "
            f"(weighted local median latency: **{fmt_ms(local_median_duration)}**)."
        ),
        (
            "- Local-neighborhood MAPE floor: "
            f"**{fmt_percent(local_mape)}**."
        ),
        (
            "- Local-neighborhood smooth relative floor with `T=1s`: "
            f"**{fmt_percent(local_smooth_relative_loss)}**."
        ),
        (
            "- Average local p90-p10 latency spread: "
            f"**{fmt_ms(local_p90_p10)}** "
            f"(**{fmt_percent(local_p90_p10_ratio)}** of typical local latency)."
        ),
    ]
    if weighted_fit_mae is not None:
        lines.append(
            "- Weighted quadratic-fit test MAE over groups that also have local "
            f"neighborhoods: **{fmt_ms(weighted_fit_mae)}** "
            f"(**{fmt_percent(weighted_fit_relative_mae)}** relative error; "
            f"{fmt_number(weighted_fit_over_local, 2)}x the local-neighborhood MAE)."
        )
    if weighted_fit_mape is not None:
        lines.append(
            "- Weighted quadratic-fit test MAPE over comparable groups: "
            f"raw-duration target **{fmt_percent(weighted_fit_mape)}** "
            f"({fmt_number(weighted_fit_over_local_mape, 2)}x the local MAPE floor); "
            f"log-duration target **{fmt_percent(weighted_log_fit_mape)}** "
            f"({fmt_number(weighted_log_fit_over_local_mape, 2)}x the local MAPE floor)."
        )
    if weighted_fit_smooth_relative_loss is not None:
        lines.append(
            "- Weighted quadratic-fit smooth relative loss over comparable groups "
            "with `T=1s`: "
            f"raw-duration target **{fmt_percent(weighted_fit_smooth_relative_loss)}** "
            f"({fmt_number(weighted_fit_over_local_smooth_relative, 2)}x the local floor); "
            f"log-duration target **{fmt_percent(weighted_log_fit_smooth_relative_loss)}** "
            f"({fmt_number(weighted_log_fit_over_local_smooth_relative, 2)}x); "
            f"smooth-relative target **{fmt_percent(weighted_smooth_fit_smooth_relative_loss)}** "
            f"({fmt_number(weighted_smooth_fit_over_local_smooth_relative, 2)}x)."
        )
    lines.extend(
        [
            "",
            "Interpretation: the exact-duplicate result is the clean feasibility "
            "argument.  The local-neighborhood result is the practical engineering "
            "scale: even among nearby token-count rows, the timing spread is large "
            "enough that a token-only model should be expected to leave residual "
            "error.  The displayed relative error ratio is `MAE / typical local "
            "latency`, so it separates a 1s miss on a 2s segment from a 1s miss "
            "on a 50s segment.",
            "",
            "## Group-Level Comparison",
            "",
            "| group | local rows | typical latency | local MAE | local err % | fit MAE | fit err % | fit / local |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in fit_comparison[:top_groups]:
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {row['local_rows']:,} "
            f"| {fmt_ms(row['local_median_duration_ms'])} "
            f"| {fmt_ms(row['local_mae_ms'])} "
            f"| {fmt_percent(row['local_mae_ratio_vs_median'])} "
            f"| {fmt_ms(row['fit_test_mae_ms'])} "
            f"| {fmt_percent(row['fit_relative_mae_ratio'])} "
            f"| {fmt_number(row['fit_over_local_mae'], 2)}x |"
        )

    lines.extend(
        [
            "",
            "The `fit / local` column is useful for diagnosis.  When it is close to "
            "1, the fitted model is approaching the noise floor implied by local "
            "neighbors.  When it is much larger than 1, either the functional form is "
            "still weak or useful variables are missing.  The error percentages are "
            "absolute-error ratios relative to the local median latency, not token "
            "ratios.",
            "",
            "## Group-Level MAPE Comparison",
            "",
            "For MAPE, the local floor uses the MAPE-optimal constant predictor inside each local bucket.  The log-duration fit is included because it targets multiplicative error rather than raw millisecond error.",
            "",
            "MAPE is denominator-sensitive: a miss of a few hundred milliseconds on a segment whose observed duration is only a few milliseconds can produce a very large percentage.  The large Codex percentages below mostly come from short `reasoning_end_to_tool_call` style segments, so the `raw / local` and `log / local` ratios are more useful than the global percentage alone.",
            "",
            "| group | local rows | local MAPE floor | raw-target MAPE | log-target MAPE | raw / local | log / local |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in fit_comparison[:top_groups]:
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {row['local_rows']:,} "
            f"| {fmt_percent(row.get('local_mape_floor'))} "
            f"| {fmt_percent(row.get('fit_test_mape'))} "
            f"| {fmt_percent(row.get('log_target_test_mape'))} "
            f"| {fmt_number(row.get('fit_over_local_mape_floor'), 2)}x "
            f"| {fmt_number(row.get('log_target_over_local_mape_floor'), 2)}x |"
        )

    lines.extend(
        [
            "",
            "## Group-Level Smooth Relative Comparison",
            "",
            "This uses `abs(error) / (duration + T)` with `T=1s`, which behaves like relative error for long segments but avoids MAPE's near-zero denominator explosion.",
            "",
            "| group | local rows | local floor | raw target | log target | smooth target | raw / local | log / local | smooth / local |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in fit_comparison[:top_groups]:
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {row['local_rows']:,} "
            f"| {fmt_percent(row.get('local_smooth_relative_floor'))} "
            f"| {fmt_percent(row.get('fit_test_smooth_relative_absolute_error'))} "
            f"| {fmt_percent(row.get('log_target_test_smooth_relative_absolute_error'))} "
            f"| {fmt_percent(row.get('smooth_target_test_smooth_relative_absolute_error'))} "
            f"| {fmt_number(row.get('fit_over_local_smooth_relative_floor'), 2)}x "
            f"| {fmt_number(row.get('log_target_over_local_smooth_relative_floor'), 2)}x "
            f"| {fmt_number(row.get('smooth_target_over_local_smooth_relative_floor'), 2)}x |"
        )

    lines.extend(
        [
            "",
            "## Largest Exact-Duplicate Counterexamples",
            "",
            "| group | tokens `(prefix, append, out)` | n | min | median | max | spread | ratio |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in exact["top_by_spread"][:top_examples]:
        tokens = f"({row['cached_tokens']}, {row['append_tokens']}, {row['segment_output_tokens']})"
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {tokens} "
            f"| {row['rows']} "
            f"| {fmt_ms(row['min_duration_ms'])} "
            f"| {fmt_ms(row['median_duration_ms'])} "
            f"| {fmt_ms(row['max_duration_ms'])} "
            f"| {fmt_ms(row['spread_ms'])} "
            f"| {fmt_number(row['ratio'], 1)}x |"
        )

    lines.extend(
        [
            "",
            "## Largest Similar-Feature Neighborhoods",
            "",
            "| group | n | prefix range | append range | output range | median | local MAE | local err % | p90-p10 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in local["top_local_buckets"][:top_examples]:
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {row['rows']} "
            f"| {row['prefix_min']:,}-{row['prefix_max']:,} "
            f"| {row['append_min']:,}-{row['append_max']:,} "
            f"| {row['output_min']:,}-{row['output_max']:,} "
            f"| {fmt_ms(row['duration_median_ms'])} "
            f"| {fmt_ms(row['best_constant_mae_ms'])} "
            f"| {fmt_percent(row['best_constant_relative_mae_ratio'])} "
            f"| {fmt_ms(row['p90_minus_p10_ms'])} |"
        )

    lines.extend(
        [
            "",
            "## What This Does Not Prove",
            "",
            "- It does not prove timing is random or impossible to model.",
            "- It does not rule out better predictors using extra fields such as queue state, server load, batching, backend placement, cache residency, retries, or client/runtime overhead.",
            "- The local-neighborhood estimate depends on the bucket width.  Narrower buckets are closer to exact matching but cover fewer rows; wider buckets cover more rows but mix less-similar points.",
            "- MAPE is unstable when actual durations are close to zero.  It is useful as a ratio-oriented diagnostic, but it should be read alongside MAE, log-error metrics, and the local MAPE floor.",
            "- The local-neighborhood comparison and the quadratic fit metrics are not computed on exactly the same train/test rows, so the comparison should be read as a scale check rather than a formal statistical test.",
            "",
            "## Practical Conclusion",
            "",
            (
                "A token-only timing model is useful as a coarse predictor and for "
                "finding first-order relationships.  It should not be treated as a "
                "complete serving-time model.  The observed residuals are expected: "
                "the trace lacks several serving-system variables that materially "
                "affect latency."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def make_smooth_relative_report(
    *,
    input_path: Path,
    fit_metrics_path: Path | None,
    load_stats: Counter[str],
    exact: dict[str, Any],
    local: dict[str, Any],
    fit_comparison: list[dict[str, Any]],
    top_groups: int,
    smooth_relative_threshold_ms: float,
) -> str:
    threshold_s = smooth_relative_threshold_ms / 1000.0
    exact_smooth = exact["duplicate_subset_best_possible_smooth_relative_loss"]
    local_smooth = local["weighted_local_best_constant_smooth_relative_loss"]
    smooth_rows = [
        row for row in fit_comparison
        if row.get("local_smooth_relative_floor") is not None
        and row.get("fit_test_smooth_relative_absolute_error") is not None
        and row.get("log_target_test_smooth_relative_absolute_error") is not None
        and row.get("smooth_target_test_smooth_relative_absolute_error") is not None
    ]

    comparable: dict[str, float | int | None] = {
        "rows": 0,
        "local_floor": None,
        "raw_loss": None,
        "log_loss": None,
        "smooth_loss": None,
        "raw_over_local": None,
        "log_over_local": None,
        "smooth_over_local": None,
    }
    if smooth_rows:
        total_weight = sum(row["local_rows"] for row in smooth_rows)
        local_floor = (
            sum(row["local_smooth_relative_floor"] * row["local_rows"] for row in smooth_rows)
            / total_weight
        )
        raw_loss = (
            sum(
                row["fit_test_smooth_relative_absolute_error"] * row["local_rows"]
                for row in smooth_rows
            )
            / total_weight
        )
        log_loss = (
            sum(
                row["log_target_test_smooth_relative_absolute_error"] * row["local_rows"]
                for row in smooth_rows
            )
            / total_weight
        )
        smooth_loss = (
            sum(
                row["smooth_target_test_smooth_relative_absolute_error"] * row["local_rows"]
                for row in smooth_rows
            )
            / total_weight
        )
        comparable = {
            "rows": total_weight,
            "local_floor": local_floor,
            "raw_loss": raw_loss,
            "log_loss": log_loss,
            "smooth_loss": smooth_loss,
            "raw_over_local": raw_loss / local_floor if local_floor > 0 else None,
            "log_over_local": log_loss / local_floor if local_floor > 0 else None,
            "smooth_over_local": smooth_loss / local_floor if local_floor > 0 else None,
        }

    lines = [
        "# Smooth Relative Timing Error",
        "",
        "## Question",
        "",
        (
            "If we care about ratio-like timing error, but do not want MAPE to be "
            "dominated by near-zero durations, how well can the token-feature fit do?"
        ),
        "",
        (
            "This report focuses on the smooth relative loss used by the new fitting "
            "target:"
        ),
        "",
        (
            "`loss = abs(predicted_duration_ms - actual_duration_ms) / "
            f"(actual_duration_ms + {smooth_relative_threshold_ms:.0f})`"
        ),
        "",
        f"Here `T={smooth_relative_threshold_ms:.0f}ms`, or about `{threshold_s:g}s`.",
        "",
        "## Why This Target",
        "",
        (
            "- For long segments, the denominator is close to the true duration, so "
            "the metric behaves like relative error."
        ),
        (
            "- For very short segments, the `+T` term prevents a few milliseconds of "
            "timing jitter from becoming thousands of percent of error."
        ),
        (
            "- The metric is still more scale-aware than raw MAE: a 1s miss on a 2s "
            "segment is penalized more than a 1s miss on a 50s segment."
        ),
        "",
        "## Data And Method",
        "",
        f"- Input CSV: `{input_path}`",
        f"- Rows scanned: {load_stats['input_rows']:,}",
        f"- Usable timing rows: {load_stats['usable_rows']:,}",
        f"- Feature tuple: `{', '.join(FEATURE_FIELDS)}`",
        f"- Fit metrics compared from: `{fit_metrics_path}`" if fit_metrics_path else "- Fit metrics compared from: n/a",
        "",
        (
            "The local noise floor is computed with the same smooth loss.  Inside "
            "each local token bucket, the best constant prediction for "
            "`abs(error)/(duration+T)` is the weighted median of observed durations "
            "with weights `1/(duration+T)`."
        ),
        "",
        (
            "The smooth-relative fit target in `fit_timing_trace.py` is a weighted "
            "least-squares approximation to this objective, using weights "
            "`1/(duration+T)^2`.  It is not an exact L1 optimizer, but it targets "
            "the same practical error scale."
        ),
        "",
        "## Main Results",
        "",
        (
            "- Exact duplicate smooth-relative floor: "
            f"**{fmt_percent(exact_smooth)}** across "
            f"{exact['duplicate_rows']:,} rows with duplicate token features."
        ),
        (
            "- Local-neighborhood smooth-relative floor: "
            f"**{fmt_percent(local_smooth)}** across "
            f"{local['total_rows_in_local_buckets']:,} local-neighborhood rows."
        ),
    ]

    if comparable["local_floor"] is not None:
        lines.extend(
            [
                (
                    "- Comparable fit-group local floor: "
                    f"**{fmt_percent(comparable['local_floor'])}** across "
                    f"{comparable['rows']:,} local rows."
                ),
                (
                    "- Raw-duration fit under smooth loss: "
                    f"**{fmt_percent(comparable['raw_loss'])}** "
                    f"({fmt_number(comparable['raw_over_local'], 2)}x the local floor)."
                ),
                (
                    "- Log-duration fit under smooth loss: "
                    f"**{fmt_percent(comparable['log_loss'])}** "
                    f"({fmt_number(comparable['log_over_local'], 2)}x the local floor)."
                ),
                (
                    "- Smooth-relative fit under smooth loss: "
                    f"**{fmt_percent(comparable['smooth_loss'])}** "
                    f"({fmt_number(comparable['smooth_over_local'], 2)}x the local floor)."
                ),
            ]
        )

    lines.extend(
        [
            "",
            "Interpretation: this is the cleanest ratio-oriented target of the three "
            "currently reported ones.  MAPE is too sensitive to short Codex timing "
            "segments; raw MAE is too insensitive to latency scale.  The smooth "
            "relative target keeps the useful scale normalization while avoiding the "
            "near-zero denominator pathology.",
            "",
        "The result is still not a perfect fit.  The smooth target gets closer "
        "to the local-neighborhood floor than the raw-duration and log-duration "
        "targets, but it remains above that floor for most large groups.  That "
        "is expected because the trace has token counts but not the full serving "
        "state: queueing, batch composition, backend placement, transient load, "
        "cache residency, retries, and client/runtime overhead are not included.",
        "",
        "## About R-Squared",
        "",
        (
            "`timing_fit_metrics.csv` does include `smooth_target_test_r2`.  For "
            "the smooth-relative fit, this is computed on the transformed target "
            "`duration / (duration + T)`, while the fitted prediction is evaluated "
            "as `predicted_duration / (duration + T)`.  That makes it a useful "
            "diagnostic for that specific weighted least-squares target, but it is "
            "not the same thing as R-squared on raw duration and it is not directly "
            "comparable to the local smooth-loss floor."
        ),
        "",
        (
            "For this reason, the primary comparison below is still the smooth loss "
            "and `smooth / local` ratio.  The R-squared column is included as a "
            "secondary diagnostic; negative values mean the transformed-target fit "
            "is worse than predicting the transformed target mean on the held-out "
            "rows."
        ),
        "",
        "## Group-Level Smooth Relative Comparison",
        "",
        "| group | local rows | local floor | raw-duration fit | log-duration fit | smooth-relative fit | smooth target R2 | raw / local | log / local | smooth / local |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in smooth_rows[:top_groups]:
        lines.append(
            f"| {group_label((row['provider'], row['model'], row['segment_kind']))} "
            f"| {row['local_rows']:,} "
            f"| {fmt_percent(row.get('local_smooth_relative_floor'))} "
            f"| {fmt_percent(row.get('fit_test_smooth_relative_absolute_error'))} "
            f"| {fmt_percent(row.get('log_target_test_smooth_relative_absolute_error'))} "
            f"| {fmt_percent(row.get('smooth_target_test_smooth_relative_absolute_error'))} "
            f"| {fmt_number(row.get('smooth_target_test_r2'), 3)} "
            f"| {fmt_number(row.get('fit_over_local_smooth_relative_floor'), 2)}x "
            f"| {fmt_number(row.get('log_target_over_local_smooth_relative_floor'), 2)}x "
            f"| {fmt_number(row.get('smooth_target_over_local_smooth_relative_floor'), 2)}x |"
        )

    lines.extend(
        [
            "",
            "## How To Read This",
            "",
            (
                "- `local floor` is not a model fit.  It is the best possible constant "
                "prediction inside small local token buckets under the same smooth "
                "loss, so it is a lower-bound scale for token-only predictors."
            ),
            (
                "- `raw-duration fit` is the ordinary millisecond target evaluated "
                "under smooth loss."
            ),
            (
                "- `log-duration fit` is the multiplicative-style target evaluated "
                "under smooth loss."
            ),
            (
                "- `smooth-relative fit` is the fit trained to match this new target "
                "and then evaluated by the same smooth loss."
            ),
            "",
            "## Limitations",
            "",
            "- The smooth-relative fit is a weighted least-squares approximation, not an exact weighted absolute-error solver.",
            "- The local floor depends on the chosen log-token bucket width and minimum bucket size.",
            "- The comparison is a scale check against held-out fit metrics, not a formal proof that no richer model could do better.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze(
    *,
    input_path: Path,
    output_dir: Path,
    fit_metrics_path: Path | None,
    top_examples: int,
    top_groups: int,
    min_group_rows: int,
    min_bucket_rows: int,
    bin_step: float,
    trim_quantile: float | None,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    rows, load_stats = load_rows(input_path)
    exact = exact_duplicate_analysis(
        rows,
        top_examples,
        smooth_relative_threshold_ms=smooth_relative_threshold_ms,
    )
    local = local_neighborhood_analysis(
        rows,
        min_group_rows=min_group_rows,
        min_bucket_rows=min_bucket_rows,
        bin_step=bin_step,
        trim_quantile=trim_quantile,
        smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        top_n=top_examples,
    )
    fit_metrics = load_fit_metrics(fit_metrics_path)
    fit_comparison = compare_with_fit(local, fit_metrics)

    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "input": str(input_path),
        "feature_fields": FEATURE_FIELDS,
        "load_stats": dict(load_stats),
        "exact_duplicate_pure_error": exact,
        "local_neighborhood_error": local,
        "fit_comparison": fit_comparison,
    }

    json_path = output_dir / "timing_irreducible_error.json"
    legacy_json_path = output_dir / "timing_feature_ambiguity.json"
    exact_csv_path = output_dir / "timing_feature_ambiguity_top.csv"
    local_csv_path = output_dir / "timing_local_neighborhood_top.csv"
    comparison_csv_path = output_dir / "timing_irreducible_error_fit_comparison.csv"
    md_path = output_dir / "timing_irreducible_error.md"
    smooth_md_path = output_dir / "timing_smooth_relative_error.md"

    write_json(json_path, result)
    write_json(
        legacy_json_path,
        {
            "superseded_by": str(json_path),
            "note": "Exact feature ambiguity is now part of the broader irreducible-error analysis.",
        },
    )
    write_csv(
        exact_csv_path,
        exact["top_by_spread"],
        [
            "provider",
            "model",
            "segment_kind",
            "cached_tokens",
            "append_tokens",
            "segment_output_tokens",
            "rows",
            "min_duration_ms",
            "median_duration_ms",
            "mape_optimal_duration_ms",
            "smooth_relative_optimal_duration_ms",
            "max_duration_ms",
            "spread_ms",
            "ratio",
            "best_constant_mae_ms",
            "best_constant_relative_mae_ratio",
            "best_constant_mape",
            "best_constant_smooth_relative_loss",
            "min_source_line",
            "max_source_line",
        ],
    )
    write_csv(
        local_csv_path,
        local["top_local_buckets"],
        [
            "provider",
            "model",
            "segment_kind",
            "rows",
            "prefix_min",
            "prefix_max",
            "append_min",
            "append_max",
            "output_min",
            "output_max",
            "duration_min_ms",
            "duration_p10_ms",
            "duration_median_ms",
            "mape_optimal_duration_ms",
            "smooth_relative_optimal_duration_ms",
            "duration_p90_ms",
            "duration_max_ms",
            "p90_minus_p10_ms",
            "p90_minus_p10_ratio",
            "spread_ms",
            "ratio",
            "best_constant_mae_ms",
            "best_constant_relative_mae_ratio",
            "best_constant_mape",
            "best_constant_smooth_relative_loss",
        ],
    )
    write_csv(
        comparison_csv_path,
        fit_comparison,
        [
            "provider",
            "model",
            "segment_kind",
            "group_key",
            "local_rows",
            "local_coverage",
            "local_mae_ms",
            "local_median_duration_ms",
            "local_relative_mae_ratio",
            "local_mae_ratio_vs_median",
            "local_mape_floor",
            "local_smooth_relative_floor",
            "local_p90_minus_p10_ms",
            "local_p90_minus_p10_ratio",
            "local_p90_minus_p10_ratio_vs_median",
            "fit_rows",
            "fit_test_mae_ms",
            "fit_test_rmse_ms",
            "fit_test_r2",
            "fit_test_mape",
            "fit_test_smooth_relative_absolute_error",
            "fit_test_median_absolute_percentage_error",
            "fit_test_p90_absolute_percentage_error",
            "log_target_test_r2",
            "log_target_test_mae_ms",
            "log_target_test_rmse_ms",
            "log_target_test_mape",
            "log_target_test_smooth_relative_absolute_error",
            "log_target_test_median_absolute_percentage_error",
            "log_target_test_p90_absolute_percentage_error",
            "log_target_test_mean_abs_log_error",
            "log_target_test_multiplicative_mae",
            "smooth_target_test_r2",
            "smooth_target_test_mae_ms",
            "smooth_target_test_rmse_ms",
            "smooth_target_test_mape",
            "smooth_target_test_smooth_relative_absolute_error",
            "smooth_target_test_mean_abs_log_error",
            "smooth_target_test_multiplicative_mae",
            "fit_relative_mae_ratio",
            "fit_over_local_mape_floor",
            "log_target_over_local_mape_floor",
            "fit_over_local_smooth_relative_floor",
            "log_target_over_local_smooth_relative_floor",
            "smooth_target_over_local_smooth_relative_floor",
            "fit_minus_local_mae_ms",
            "fit_over_local_mae",
        ],
    )
    md_path.write_text(
        make_report(
            input_path=input_path,
            fit_metrics_path=fit_metrics_path,
            load_stats=load_stats,
            exact=exact,
            local=local,
            fit_comparison=fit_comparison,
            top_groups=top_groups,
            top_examples=top_examples,
        ),
        encoding="utf-8",
    )
    smooth_md_path.write_text(
        make_smooth_relative_report(
            input_path=input_path,
            fit_metrics_path=fit_metrics_path,
            load_stats=load_stats,
            exact=exact,
            local=local,
            fit_comparison=fit_comparison,
            top_groups=top_groups,
            smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        ),
        encoding="utf-8",
    )

    # Preserve the older filename as a short pointer for anyone who already used it.
    legacy_md_path = output_dir / "timing_feature_ambiguity.md"
    legacy_md_path.write_text(
        "# Timing Feature Ambiguity\n\n"
        "This report has been folded into the broader irreducible-error analysis:\n\n"
        f"- `{md_path}`\n",
        encoding="utf-8",
    )

    result["outputs"] = {
        "json": str(json_path),
        "markdown": str(md_path),
        "smooth_relative_markdown": str(smooth_md_path),
        "exact_duplicate_csv": str(exact_csv_path),
        "local_neighborhood_csv": str(local_csv_path),
        "fit_comparison_csv": str(comparison_csv_path),
        "legacy_json_pointer": str(legacy_json_path),
        "legacy_markdown_pointer": str(legacy_md_path),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fit-metrics", type=Path, default=DEFAULT_FIT_METRICS)
    parser.add_argument("--top-examples", type=int, default=25)
    parser.add_argument("--top-groups", type=int, default=16)
    parser.add_argument("--min-group-rows", type=int, default=1000)
    parser.add_argument("--min-bucket-rows", type=int, default=8)
    parser.add_argument(
        "--bin-step",
        type=float,
        default=0.25,
        help="Bucket width in log2-token space; 0.25 is roughly a 19%% token range.",
    )
    parser.add_argument(
        "--trim-quantile",
        type=float,
        default=0.99,
        help="Per-group duration quantile used for local-neighborhood trimming.",
    )
    parser.add_argument(
        "--smooth-relative-threshold-ms",
        type=float,
        default=DEFAULT_SMOOTH_RELATIVE_THRESHOLD_MS,
        help=(
            "Threshold T for smooth relative loss abs(error)/(duration_ms + T). "
            "Default 1000ms, i.e. T=1s."
        ),
    )
    args = parser.parse_args()

    trim_quantile = args.trim_quantile
    if trim_quantile is not None and not (0 < trim_quantile <= 1):
        parser.error("--trim-quantile must be in (0, 1]")

    result = analyze(
        input_path=args.input,
        output_dir=args.output_dir,
        fit_metrics_path=args.fit_metrics,
        top_examples=args.top_examples,
        top_groups=args.top_groups,
        min_group_rows=args.min_group_rows,
        min_bucket_rows=args.min_bucket_rows,
        bin_step=args.bin_step,
        trim_quantile=trim_quantile,
        smooth_relative_threshold_ms=args.smooth_relative_threshold_ms,
    )

    print(f"input={result['input']}")
    print(f"usable_rows={result['load_stats']['usable_rows']}")
    exact = result["exact_duplicate_pure_error"]
    print(f"exact_duplicate_feature_keys={exact['duplicate_feature_keys']}")
    print(f"exact_duplicate_rows={exact['duplicate_rows']}")
    print(f"exact_duplicate_best_possible_mae_ms={exact['duplicate_subset_best_possible_mae_ms']}")
    print(f"exact_duplicate_best_possible_mape={exact['duplicate_subset_best_possible_mape']}")
    print(f"exact_duplicate_best_possible_smooth_relative_loss={exact['duplicate_subset_best_possible_smooth_relative_loss']}")
    local = result["local_neighborhood_error"]
    print(f"local_rows={local['total_rows_in_local_buckets']}")
    print(f"local_weighted_mae_ms={local['weighted_local_best_constant_mae_ms']}")
    print(f"local_weighted_relative_mae_ratio={local['weighted_local_best_constant_relative_mae_ratio']}")
    print(f"local_weighted_mape={local['weighted_local_best_constant_mape']}")
    print(f"local_weighted_smooth_relative_loss={local['weighted_local_best_constant_smooth_relative_loss']}")
    print(f"local_weighted_mae_ratio_vs_median={local['weighted_local_best_constant_mae_ratio_vs_median']}")
    print(f"local_weighted_p90_minus_p10_ms={local['weighted_p90_minus_p10_ms']}")
    print(f"local_weighted_p90_minus_p10_ratio={local['weighted_p90_minus_p10_ratio']}")
    print(f"local_weighted_p90_minus_p10_ratio_vs_median={local['weighted_p90_minus_p10_ratio_vs_median']}")
    for name, path in result["outputs"].items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
