#!/usr/bin/env python3
"""Fit simple quadratic timing models from timing-fit trace CSV rows."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
DEFAULT_INPUT = SCRIPT_DIR / "timing_fit_trace.csv"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR
DEFAULT_SMOOTH_RELATIVE_THRESHOLD_MS = 1000.0

FEATURE_NAMES = [
    "intercept",
    "prefix_k",
    "append_k",
    "out_k",
    "prefix_k^2",
    "append_k^2",
    "out_k^2",
    "prefix_k*append_k",
    "prefix_k*out_k",
    "append_k*out_k",
]


@dataclass
class FitInput:
    provider: str
    model: str
    segment_kind: str
    prefix_tokens: float
    append_tokens: float
    output_tokens: float
    duration_ms: float


def finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def feature_matrix(rows: list[FitInput]) -> np.ndarray:
    prefix = np.array([row.prefix_tokens for row in rows], dtype=np.float64) / 1000.0
    append = np.array([row.append_tokens for row in rows], dtype=np.float64) / 1000.0
    output = np.array([row.output_tokens for row in rows], dtype=np.float64) / 1000.0
    return np.column_stack(
        [
            np.ones(len(rows), dtype=np.float64),
            prefix,
            append,
            output,
            prefix * prefix,
            append * append,
            output * output,
            prefix * append,
            prefix * output,
            append * output,
        ]
    )


def duration_vector(rows: list[FitInput]) -> np.ndarray:
    return np.array([row.duration_ms for row in rows], dtype=np.float64)


def log_duration_vector(rows: list[FitInput]) -> np.ndarray:
    return np.log(duration_vector(rows))


def train_test_indices(n: int, seed: int, test_fraction: float) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    indices = np.arange(n)
    rng.shuffle(indices)
    test_size = max(1, int(round(n * test_fraction)))
    test = np.sort(indices[:test_size])
    train = np.sort(indices[test_size:])
    return train, test


def prediction_metrics(
    actual_ms: np.ndarray,
    predicted_ms: np.ndarray,
    target_actual: np.ndarray,
    target_predicted: np.ndarray,
    *,
    smooth_relative_threshold_ms: float,
) -> dict[str, float]:
    duration_err = predicted_ms - actual_ms
    abs_pct = np.abs(duration_err) / actual_ms
    smooth_abs_pct = np.abs(duration_err) / (actual_ms + smooth_relative_threshold_ms)
    ratio = predicted_ms / actual_ms
    log_abs_error = np.abs(np.log(predicted_ms) - np.log(actual_ms))
    target_err = target_predicted - target_actual
    centered = target_actual - float(np.mean(target_actual))
    ss_res = float(np.sum(target_err * target_err))
    ss_tot = float(np.sum(centered * centered))
    return {
        "rmse_ms": float(np.sqrt(np.mean(duration_err * duration_err))),
        "mae_ms": float(np.mean(np.abs(duration_err))),
        "median_abs_error_ms": float(np.median(np.abs(duration_err))),
        "mape": float(np.mean(abs_pct)),
        "median_absolute_percentage_error": float(np.median(abs_pct)),
        "p90_absolute_percentage_error": float(np.quantile(abs_pct, 0.90)),
        "smooth_relative_absolute_error": float(np.mean(smooth_abs_pct)),
        "median_smooth_relative_absolute_error": float(np.median(smooth_abs_pct)),
        "p90_smooth_relative_absolute_error": float(np.quantile(smooth_abs_pct, 0.90)),
        "mean_abs_log_error": float(np.mean(log_abs_error)),
        "median_abs_log_error": float(np.median(log_abs_error)),
        "multiplicative_mae": float(math.exp(np.mean(log_abs_error))),
        "multiplicative_median_error": float(math.exp(np.median(log_abs_error))),
        "prediction_ratio_p10": float(np.quantile(ratio, 0.10)),
        "prediction_ratio_p50": float(np.quantile(ratio, 0.50)),
        "prediction_ratio_p90": float(np.quantile(ratio, 0.90)),
        "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
    }


def fit_ols_target(
    rows: list[FitInput],
    *,
    train_idx: np.ndarray,
    test_idx: np.ndarray,
    target: str,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    x = feature_matrix(rows)
    actual_duration = duration_vector(rows)
    if target == "duration_ms":
        y = actual_duration
        prediction_to_duration = lambda pred: np.maximum(pred, 1e-9)
        coefficient_name = "coefficients_ms"
        weighted = False
    elif target == "log_duration_ms":
        y = np.log(actual_duration)
        prediction_to_duration = lambda pred: np.exp(pred)
        coefficient_name = "coefficients_log_ms"
        weighted = False
    elif target == "smooth_relative_duration_ms":
        y = actual_duration
        prediction_to_duration = lambda pred: np.maximum(pred, 1e-9)
        coefficient_name = "coefficients_ms"
        weighted = True
    else:
        raise ValueError(f"unsupported target: {target}")

    x_train = x[train_idx]
    y_train = y[train_idx]
    duration_train = actual_duration[train_idx]
    x_test = x[test_idx]
    y_test = y[test_idx]
    duration_test = actual_duration[test_idx]

    if weighted:
        # Weighted least squares approximates minimizing
        # abs(pred - y) / (y + T) with the corresponding squared loss.
        weights = 1.0 / np.square(duration_train + smooth_relative_threshold_ms)
        sqrt_weights = np.sqrt(weights)
        solve_x = x_train * sqrt_weights[:, None]
        solve_y = y_train * sqrt_weights
    else:
        solve_x = x_train
        solve_y = y_train

    coefficients, residuals, rank, singular_values = np.linalg.lstsq(
        solve_x,
        solve_y,
        rcond=None,
    )
    pred_train = x_train @ coefficients
    pred_test = x_test @ coefficients
    duration_pred_train = prediction_to_duration(pred_train)
    duration_pred_test = prediction_to_duration(pred_test)

    if target == "smooth_relative_duration_ms":
        target_actual_train = duration_train / (duration_train + smooth_relative_threshold_ms)
        target_pred_train = duration_pred_train / (duration_train + smooth_relative_threshold_ms)
        target_actual_test = duration_test / (duration_test + smooth_relative_threshold_ms)
        target_pred_test = duration_pred_test / (duration_test + smooth_relative_threshold_ms)
    else:
        target_actual_train = y_train
        target_pred_train = pred_train
        target_actual_test = y_test
        target_pred_test = pred_test

    return {
        "train_rows": len(train_idx),
        "test_rows": len(test_idx),
        "target": target,
        coefficient_name: {
            name: float(value) for name, value in zip(FEATURE_NAMES, coefficients)
        },
        "train_metrics": prediction_metrics(
            duration_train,
            duration_pred_train,
            target_actual_train,
            target_pred_train,
            smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        ),
        "test_metrics": prediction_metrics(
            duration_test,
            duration_pred_test,
            target_actual_test,
            target_pred_test,
            smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        ),
        "rank": int(rank),
        "condition_number": (
            float(singular_values[0] / singular_values[-1])
            if len(singular_values) and singular_values[-1] > 0
            else None
        ),
    }


def fit_ols(
    rows: list[FitInput],
    *,
    seed: int,
    test_fraction: float,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    train_idx, test_idx = train_test_indices(len(rows), seed, test_fraction)
    duration_fit = fit_ols_target(
        rows,
        train_idx=train_idx,
        test_idx=test_idx,
        target="duration_ms",
        smooth_relative_threshold_ms=smooth_relative_threshold_ms,
    )
    log_fit = fit_ols_target(
        rows,
        train_idx=train_idx,
        test_idx=test_idx,
        target="log_duration_ms",
        smooth_relative_threshold_ms=smooth_relative_threshold_ms,
    )
    smooth_relative_fit = fit_ols_target(
        rows,
        train_idx=train_idx,
        test_idx=test_idx,
        target="smooth_relative_duration_ms",
        smooth_relative_threshold_ms=smooth_relative_threshold_ms,
    )
    return {
        "rows": len(rows),
        "train_rows": len(train_idx),
        "test_rows": len(test_idx),
        "duration_ms": describe_values(duration_vector(rows)),
        "duration_ms_target": duration_fit,
        "log_duration_ms_target": log_fit,
        "smooth_relative_duration_ms_target": smooth_relative_fit,
    }


def describe_values(values: np.ndarray) -> dict[str, float]:
    if len(values) == 0:
        return {}
    quantiles = np.quantile(values, [0.5, 0.9, 0.95, 0.99])
    return {
        "min": float(np.min(values)),
        "p50": float(quantiles[0]),
        "p90": float(quantiles[1]),
        "p95": float(quantiles[2]),
        "p99": float(quantiles[3]),
        "max": float(np.max(values)),
        "mean": float(np.mean(values)),
    }


def fit_group_key(provider: str, model: str, segment_kind: str) -> str:
    provider_part = provider or "unknown_provider"
    model_part = model or "unknown_model"
    segment_part = segment_kind or "unknown_segment"
    return f"{provider_part}::{model_part}::{segment_part}"


def load_rows(input_path: Path) -> tuple[dict[str, list[FitInput]], Counter[str]]:
    grouped: dict[str, list[FitInput]] = defaultdict(list)
    stats: Counter[str] = Counter()
    with input_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stats["input_rows"] += 1
            segment_kind = row.get("segment_kind")
            if not segment_kind:
                stats["missing_segment_kind"] += 1
                continue
            provider = row.get("provider") or ""
            model = row.get("model") or ""
            prefix = finite_float(row.get("cached_tokens"))
            append = finite_float(row.get("append_tokens"))
            output = finite_float(row.get("segment_output_tokens"))
            duration = finite_float(row.get("duration_ms"))
            if prefix is None or append is None or output is None or duration is None:
                stats["missing_numeric_field"] += 1
                continue
            if prefix < 0 or append < 0 or output < 0 or duration <= 0:
                stats["invalid_numeric_field"] += 1
                continue
            grouped[fit_group_key(provider, model, segment_kind)].append(
                FitInput(
                    provider=provider,
                    model=model,
                    segment_kind=segment_kind,
                    prefix_tokens=prefix,
                    append_tokens=append,
                    output_tokens=output,
                    duration_ms=duration,
                )
            )
    return grouped, stats


def trim_rows(
    rows: list[FitInput],
    *,
    trim_quantile: float | None,
    max_duration_ms: float | None,
) -> tuple[list[FitInput], dict[str, Any]]:
    cutoff = max_duration_ms
    if trim_quantile is not None:
        durations = np.array([row.duration_ms for row in rows], dtype=np.float64)
        quantile_cutoff = float(np.quantile(durations, trim_quantile))
        cutoff = quantile_cutoff if cutoff is None else min(cutoff, quantile_cutoff)
    if cutoff is None:
        return rows, {"cutoff_ms": None, "removed_rows": 0}
    trimmed = [row for row in rows if row.duration_ms <= cutoff]
    return trimmed, {
        "cutoff_ms": cutoff,
        "removed_rows": len(rows) - len(trimmed),
    }


def fit_all(
    input_path: Path,
    output_dir: Path,
    *,
    min_rows: int,
    seed: int,
    test_fraction: float,
    trim_quantile: float | None,
    max_duration_ms: float | None,
    smooth_relative_threshold_ms: float,
) -> dict[str, Any]:
    grouped, load_stats = load_rows(input_path)
    result: dict[str, Any] = {
        "input": str(input_path),
        "feature_definition": {
            "token_unit": "1k tokens",
            "formula": "target ~ 1 + prefix + append + out + prefix^2 + append^2 + out^2 + prefix*append + prefix*out + append*out",
            "targets": {
                "duration_ms": "ordinary least squares on raw duration; optimized for absolute millisecond error",
                "log_duration_ms": "ordinary least squares on log(duration_ms); optimized for multiplicative/ratio-style error",
                "smooth_relative_duration_ms": "weighted least squares on raw duration with weights 1 / (duration_ms + T)^2; approximates smooth relative error",
            },
            "smooth_relative_threshold_ms": smooth_relative_threshold_ms,
            "prefix": "cached_tokens / 1000",
            "append": "append_tokens / 1000",
            "out": "segment_output_tokens / 1000",
        },
        "fit_config": {
            "min_rows": min_rows,
            "seed": seed,
            "test_fraction": test_fraction,
            "trim_quantile": trim_quantile,
            "max_duration_ms": max_duration_ms,
        },
        "load_stats": dict(load_stats),
        "segments": {},
    }

    coefficient_rows: list[dict[str, Any]] = []
    metric_rows: list[dict[str, Any]] = []
    for group_key, rows in sorted(grouped.items()):
        first = rows[0]
        segment_result: dict[str, Any] = {
            "provider": first.provider,
            "model": first.model,
            "segment_kind": first.segment_kind,
            "raw_rows": len(rows),
            "raw_duration_ms": describe_values(duration_vector(rows)),
        }
        fit_rows, trim_info = trim_rows(
            rows,
            trim_quantile=trim_quantile,
            max_duration_ms=max_duration_ms,
        )
        segment_result["trim"] = trim_info
        segment_result["fit_rows"] = len(fit_rows)
        if len(fit_rows) < min_rows:
            segment_result["skipped"] = f"fewer than {min_rows} rows after filtering"
            result["segments"][group_key] = segment_result
            metric_rows.append(
                {
                    "provider": first.provider,
                    "model": first.model,
                    "segment_kind": first.segment_kind,
                    "group_key": group_key,
                    "raw_rows": len(rows),
                    "fit_rows": len(fit_rows),
                    "removed_rows": trim_info["removed_rows"],
                    "skipped": segment_result["skipped"],
                    "duration_p50_ms": "",
                    "duration_p90_ms": "",
                    "duration_p99_ms": "",
                    "test_r2": "",
                    "test_mae_ms": "",
                    "test_rmse_ms": "",
                    "test_median_abs_error_ms": "",
                    "test_mape": "",
                    "test_median_absolute_percentage_error": "",
                    "test_p90_absolute_percentage_error": "",
                    "test_smooth_relative_absolute_error": "",
                    "test_median_smooth_relative_absolute_error": "",
                    "test_p90_smooth_relative_absolute_error": "",
                    "log_target_test_r2": "",
                    "log_target_test_mae_ms": "",
                    "log_target_test_rmse_ms": "",
                    "log_target_test_mape": "",
                    "log_target_test_median_absolute_percentage_error": "",
                    "log_target_test_p90_absolute_percentage_error": "",
                    "log_target_test_smooth_relative_absolute_error": "",
                    "log_target_test_median_smooth_relative_absolute_error": "",
                    "log_target_test_p90_smooth_relative_absolute_error": "",
                    "log_target_test_mean_abs_log_error": "",
                    "log_target_test_multiplicative_mae": "",
                    "smooth_target_test_r2": "",
                    "smooth_target_test_mae_ms": "",
                    "smooth_target_test_rmse_ms": "",
                    "smooth_target_test_mape": "",
                    "smooth_target_test_median_absolute_percentage_error": "",
                    "smooth_target_test_p90_absolute_percentage_error": "",
                    "smooth_target_test_smooth_relative_absolute_error": "",
                    "smooth_target_test_median_smooth_relative_absolute_error": "",
                    "smooth_target_test_p90_smooth_relative_absolute_error": "",
                    "smooth_target_test_mean_abs_log_error": "",
                    "smooth_target_test_multiplicative_mae": "",
                }
            )
            continue

        fit = fit_ols(
            fit_rows,
            seed=seed,
            test_fraction=test_fraction,
            smooth_relative_threshold_ms=smooth_relative_threshold_ms,
        )
        segment_result["fit"] = fit
        duration_fit = fit["duration_ms_target"]
        log_fit = fit["log_duration_ms_target"]
        smooth_fit = fit["smooth_relative_duration_ms_target"]
        duration_test = duration_fit["test_metrics"]
        log_test = log_fit["test_metrics"]
        smooth_test = smooth_fit["test_metrics"]
        result["segments"][group_key] = segment_result
        metric_rows.append(
            {
                "provider": first.provider,
                "model": first.model,
                "segment_kind": first.segment_kind,
                "group_key": group_key,
                "raw_rows": len(rows),
                "fit_rows": len(fit_rows),
                "removed_rows": trim_info["removed_rows"],
                "skipped": "",
                "duration_p50_ms": fit["duration_ms"]["p50"],
                "duration_p90_ms": fit["duration_ms"]["p90"],
                "duration_p99_ms": fit["duration_ms"]["p99"],
                "test_r2": duration_test["r2"],
                "test_mae_ms": duration_test["mae_ms"],
                "test_rmse_ms": duration_test["rmse_ms"],
                "test_median_abs_error_ms": duration_test["median_abs_error_ms"],
                "test_mape": duration_test["mape"],
                "test_median_absolute_percentage_error": duration_test[
                    "median_absolute_percentage_error"
                ],
                "test_p90_absolute_percentage_error": duration_test[
                    "p90_absolute_percentage_error"
                ],
                "test_smooth_relative_absolute_error": duration_test[
                    "smooth_relative_absolute_error"
                ],
                "test_median_smooth_relative_absolute_error": duration_test[
                    "median_smooth_relative_absolute_error"
                ],
                "test_p90_smooth_relative_absolute_error": duration_test[
                    "p90_smooth_relative_absolute_error"
                ],
                "log_target_test_r2": log_test["r2"],
                "log_target_test_mae_ms": log_test["mae_ms"],
                "log_target_test_rmse_ms": log_test["rmse_ms"],
                "log_target_test_mape": log_test["mape"],
                "log_target_test_median_absolute_percentage_error": log_test[
                    "median_absolute_percentage_error"
                ],
                "log_target_test_p90_absolute_percentage_error": log_test[
                    "p90_absolute_percentage_error"
                ],
                "log_target_test_smooth_relative_absolute_error": log_test[
                    "smooth_relative_absolute_error"
                ],
                "log_target_test_median_smooth_relative_absolute_error": log_test[
                    "median_smooth_relative_absolute_error"
                ],
                "log_target_test_p90_smooth_relative_absolute_error": log_test[
                    "p90_smooth_relative_absolute_error"
                ],
                "log_target_test_mean_abs_log_error": log_test["mean_abs_log_error"],
                "log_target_test_multiplicative_mae": log_test["multiplicative_mae"],
                "smooth_target_test_r2": smooth_test["r2"],
                "smooth_target_test_mae_ms": smooth_test["mae_ms"],
                "smooth_target_test_rmse_ms": smooth_test["rmse_ms"],
                "smooth_target_test_mape": smooth_test["mape"],
                "smooth_target_test_median_absolute_percentage_error": smooth_test[
                    "median_absolute_percentage_error"
                ],
                "smooth_target_test_p90_absolute_percentage_error": smooth_test[
                    "p90_absolute_percentage_error"
                ],
                "smooth_target_test_smooth_relative_absolute_error": smooth_test[
                    "smooth_relative_absolute_error"
                ],
                "smooth_target_test_median_smooth_relative_absolute_error": smooth_test[
                    "median_smooth_relative_absolute_error"
                ],
                "smooth_target_test_p90_smooth_relative_absolute_error": smooth_test[
                    "p90_smooth_relative_absolute_error"
                ],
                "smooth_target_test_mean_abs_log_error": smooth_test["mean_abs_log_error"],
                "smooth_target_test_multiplicative_mae": smooth_test["multiplicative_mae"],
            }
        )
        for target_name, target_fit in [
            ("duration_ms", duration_fit),
            ("log_duration_ms", log_fit),
            ("smooth_relative_duration_ms", smooth_fit),
        ]:
            coefficient_key = (
                "coefficients_log_ms"
                if target_name == "log_duration_ms"
                else "coefficients_ms"
            )
            for name, value in target_fit[coefficient_key].items():
                coefficient_rows.append(
                    {
                        "provider": first.provider,
                        "model": first.model,
                        "segment_kind": first.segment_kind,
                        "group_key": group_key,
                        "target": target_name,
                        "feature": name,
                        "coefficient": value,
                        "fit_rows": len(fit_rows),
                        "test_r2": target_fit["test_metrics"]["r2"],
                        "test_mae_ms": target_fit["test_metrics"]["mae_ms"],
                        "test_mape": target_fit["test_metrics"]["mape"],
                        "test_smooth_relative_absolute_error": target_fit[
                            "test_metrics"
                        ]["smooth_relative_absolute_error"],
                    }
                )

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "timing_fit_summary.json"
    coefficient_path = output_dir / "timing_fit_coefficients.csv"
    metrics_path = output_dir / "timing_fit_metrics.csv"
    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    with coefficient_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "provider",
                "model",
                "segment_kind",
                "group_key",
                "target",
                "feature",
                "coefficient",
                "fit_rows",
                "test_r2",
                "test_mae_ms",
                "test_mape",
                "test_smooth_relative_absolute_error",
            ],
        )
        writer.writeheader()
        writer.writerows(coefficient_rows)
    with metrics_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "provider",
                "model",
                "segment_kind",
                "group_key",
                "raw_rows",
                "fit_rows",
                "removed_rows",
                "skipped",
                "duration_p50_ms",
                "duration_p90_ms",
                "duration_p99_ms",
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
            ],
        )
        writer.writeheader()
        writer.writerows(metric_rows)
    result["outputs"] = {
        "summary_json": str(summary_path),
        "coefficients_csv": str(coefficient_path),
        "metrics_csv": str(metrics_path),
    }
    return result


def print_summary(result: dict[str, Any]) -> None:
    print(f"input={result['input']}")
    outputs = result.get("outputs", {})
    if outputs:
        print(f"summary_json={outputs.get('summary_json')}")
        print(f"coefficients_csv={outputs.get('coefficients_csv')}")
    print("segments:")
    for group_key, segment in result["segments"].items():
        fit = segment.get("fit")
        if not fit:
            print(f"  {group_key}: skipped ({segment.get('skipped')})")
            continue
        trim = segment["trim"]
        duration_test = fit["duration_ms_target"]["test_metrics"]
        log_test = fit["log_duration_ms_target"]["test_metrics"]
        smooth_test = fit["smooth_relative_duration_ms_target"]["test_metrics"]
        print(
            f"  {group_key}: rows={fit['rows']:,} "
            f"removed={trim['removed_rows']:,} "
            f"mae_target_r2={duration_test['r2']:.3f} "
            f"mae={duration_test['mae_ms']:.1f}ms "
            f"mape={duration_test['mape'] * 100:.1f}% "
            f"log_target_r2={log_test['r2']:.3f} "
            f"log_target_mape={log_test['mape'] * 100:.1f}% "
            f"smooth_target_loss={smooth_test['smooth_relative_absolute_error'] * 100:.1f}%"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"Input timing-fit CSV (default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--min-rows", type=int, default=100)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    parser.add_argument(
        "--trim-quantile",
        type=float,
        default=0.99,
        help="Drop rows above this per-segment duration quantile; set to 0 to disable.",
    )
    parser.add_argument(
        "--max-duration-ms",
        type=float,
        default=None,
        help="Optional absolute per-row duration cutoff.",
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

    trim_quantile = args.trim_quantile if args.trim_quantile > 0 else None
    result = fit_all(
        args.input,
        args.output_dir,
        min_rows=args.min_rows,
        seed=args.seed,
        test_fraction=args.test_fraction,
        trim_quantile=trim_quantile,
        max_duration_ms=args.max_duration_ms,
        smooth_relative_threshold_ms=args.smooth_relative_threshold_ms,
    )
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
