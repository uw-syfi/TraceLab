#!/usr/bin/env python3
"""Test whether long-append rounds are slower than matched prefix-heavy rounds.

The hypothesis is not just "append-heavy rows are slower on average".  The
strong version is: after matching rows with similar provider, model, segment
kind, total input length, and output length, append-heavy rows should separate
cleanly from prefix-heavy rows.

This script reports both:

* effect size: how often an append-heavy row is slower than a matched
  prefix-heavy row;
* separation quality: whether a duration threshold can distinguish the two
  classes after normalizing by the local prefix-heavy median latency.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import random
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any


def configure_matplotlib_cache() -> None:
    """Keep Matplotlib usable when the launching user's config dir is read-only."""
    if "MPLCONFIGDIR" in os.environ:
        return

    config_home = os.environ.get("XDG_CONFIG_HOME")
    config_base = Path(config_home) if config_home else Path.home() / ".config"
    matplotlib_dir = config_base / "matplotlib"
    if matplotlib_dir.exists() and os.access(matplotlib_dir, os.W_OK):
        return
    if not matplotlib_dir.exists() and config_base.exists() and os.access(config_base, os.W_OK):
        return

    fallback_dir = Path(tempfile.gettempdir()) / "coding-trace-matplotlib"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(fallback_dir)


configure_matplotlib_cache()

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import png_sidecar  # noqa: E402

DEFAULT_INPUT = SCRIPT_DIR.parent / "timing_fit" / "timing_fit_trace.csv"
DEFAULT_OUTPUT_DIR = SCRIPT_DIR

TEXT_COLOR = "#172033"
MUTED_TEXT = "#526070"
GRID_COLOR = "#e6eaf0"
PREFIX_BLUE = "#2563eb"
APPEND_ORANGE = "#d97706"

FULL_TURN_INPUT_EVENTS = {"tool_result", "user_message"}


@dataclass(frozen=True)
class TimingRow:
    provider: str
    model: str
    segment_kind: str
    start_event: str
    source_line: int
    total_tokens: int
    prefix_tokens: int
    append_tokens: int
    output_tokens: int
    duration_ms: float

    @property
    def append_share(self) -> float:
        if self.total_tokens <= 0:
            return 0.0
        return self.append_tokens / self.total_tokens


@dataclass(frozen=True)
class BucketKey:
    provider: str
    model: str
    segment_kind: str
    total_bin: int
    output_bin: int

    def label(self) -> str:
        return "::".join(
            [
                self.provider,
                self.model,
                self.segment_kind,
                str(self.total_bin),
                str(self.output_bin),
            ]
        )


@dataclass
class BucketSummary:
    key: BucketKey
    prefix_rows: int
    append_rows: int
    pair_count: int
    total_tokens_median: float
    output_tokens_median: float
    prefix_append_share_median: float
    append_append_share_median: float
    prefix_duration_median_ms: float
    append_duration_median_ms: float
    median_ratio: float
    append_slower_probability: float
    cliffs_delta: float
    best_normalized_threshold: float
    best_balanced_accuracy: float
    prefix_recall: float
    append_recall: float


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


def fmt_number(value: float | int | None, digits: int = 2) -> str:
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


def token_bin(value: int, step: float) -> int:
    if value <= 0:
        return -1_000_000
    return int(math.floor(math.log2(value) / step))


def duration_label(ms: float) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def row_group(
    row: TimingRow,
    *,
    append_heavy_share: float,
    prefix_heavy_max_append_share: float,
    min_append_tokens: int,
    min_prefix_tokens: int,
) -> str | None:
    if row.append_share >= append_heavy_share and row.append_tokens >= min_append_tokens:
        return "append_heavy"
    if (
        row.append_share <= prefix_heavy_max_append_share
        and row.prefix_tokens >= min_prefix_tokens
    ):
        return "prefix_heavy"
    return None


def load_rows(
    input_path: Path,
    *,
    include_split_segments: bool,
    min_total_tokens: int,
    min_duration_ms: float,
    max_duration_ms: float | None,
) -> tuple[list[TimingRow], Counter[str]]:
    rows: list[TimingRow] = []
    stats: Counter[str] = Counter()
    with input_path.open("r", encoding="utf-8", newline="") as fh:
        for source_line, raw in enumerate(csv.DictReader(fh), start=2):
            stats["input_rows"] += 1
            start_event = raw.get("segment_start_event") or ""
            end_event = raw.get("segment_end_event") or ""
            if not include_split_segments and (
                start_event not in FULL_TURN_INPUT_EVENTS or end_event != "tool_call"
            ):
                stats["skipped_split_or_non_tool_call_segment"] += 1
                continue

            total = intish(raw.get("input_tokens_total"))
            prefix = intish(raw.get("cached_tokens"))
            append = intish(raw.get("append_tokens"))
            output = intish(raw.get("segment_output_tokens"))
            duration = finite_float(raw.get("duration_ms"))
            if total is None or prefix is None or append is None or output is None or duration is None:
                stats["skipped_missing_numeric"] += 1
                continue
            if total < min_total_tokens:
                stats["skipped_small_total"] += 1
                continue
            if prefix < 0 or append < 0 or output < 0 or duration <= 0:
                stats["skipped_invalid_numeric"] += 1
                continue
            if duration < min_duration_ms:
                stats["skipped_below_min_duration"] += 1
                continue
            if max_duration_ms is not None and duration > max_duration_ms:
                stats["skipped_above_max_duration"] += 1
                continue

            rows.append(
                TimingRow(
                    provider=raw.get("provider") or "",
                    model=raw.get("model") or "",
                    segment_kind=raw.get("segment_kind") or "",
                    start_event=start_event,
                    source_line=source_line,
                    total_tokens=total,
                    prefix_tokens=prefix,
                    append_tokens=append,
                    output_tokens=output,
                    duration_ms=duration,
                )
            )
    stats["usable_rows"] = len(rows)
    return rows, stats


def trim_by_group(
    rows: list[TimingRow],
    *,
    trim_quantile: float | None,
) -> tuple[list[TimingRow], Counter[str]]:
    stats: Counter[str] = Counter()
    if trim_quantile is None:
        stats["rows_after_trim"] = len(rows)
        return rows, stats

    grouped: dict[tuple[str, str, str], list[TimingRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.provider, row.model, row.segment_kind)].append(row)

    kept: list[TimingRow] = []
    for group_rows in grouped.values():
        durations = np.array([row.duration_ms for row in group_rows], dtype=np.float64)
        cutoff = float(np.quantile(durations, trim_quantile))
        for row in group_rows:
            if row.duration_ms <= cutoff:
                kept.append(row)
            else:
                stats["trimmed_rows"] += 1
    stats["rows_after_trim"] = len(kept)
    return kept, stats


def dominance_probability(append_durations: list[float], prefix_durations: list[float]) -> float:
    prefix_sorted = sorted(prefix_durations)
    wins = 0
    ties = 0
    for value in append_durations:
        wins += bisect.bisect_left(prefix_sorted, value)
        ties += bisect.bisect_right(prefix_sorted, value) - bisect.bisect_left(
            prefix_sorted,
            value,
        )
    pairs = len(append_durations) * len(prefix_durations)
    if pairs == 0:
        return float("nan")
    return (wins + 0.5 * ties) / pairs


def best_threshold(
    prefix_values: list[float],
    append_values: list[float],
) -> tuple[float, float, float, float]:
    """Return threshold, balanced accuracy, prefix recall, append recall.

    Classification rule: value > threshold means append-heavy.
    """
    if not prefix_values or not append_values:
        return float("nan"), float("nan"), float("nan"), float("nan")

    combined = [(value, 0) for value in prefix_values] + [
        (value, 1) for value in append_values
    ]
    combined.sort(key=lambda item: item[0])

    prefix_below = 0
    append_below = 0
    prefix_total = len(prefix_values)
    append_total = len(append_values)
    best = (float("-inf"), 0.5, 0.0, 1.0)

    index = 0
    while index < len(combined):
        threshold = combined[index][0]
        while index < len(combined) and combined[index][0] == threshold:
            if combined[index][1] == 0:
                prefix_below += 1
            else:
                append_below += 1
            index += 1
        prefix_recall = prefix_below / prefix_total
        append_recall = (append_total - append_below) / append_total
        balanced_accuracy = 0.5 * (prefix_recall + append_recall)
        if balanced_accuracy > best[1]:
            best = (threshold, balanced_accuracy, prefix_recall, append_recall)
    return best


def summarize_bucket(
    key: BucketKey,
    prefix_rows: list[TimingRow],
    append_rows: list[TimingRow],
) -> BucketSummary:
    prefix_durations = [row.duration_ms for row in prefix_rows]
    append_durations = [row.duration_ms for row in append_rows]
    prefix_median = median(prefix_durations)
    append_median = median(append_durations)
    prefix_norm = [value / prefix_median for value in prefix_durations]
    append_norm = [value / prefix_median for value in append_durations]
    threshold, balanced_accuracy, prefix_recall, append_recall = best_threshold(
        prefix_norm,
        append_norm,
    )
    dominance = dominance_probability(append_durations, prefix_durations)
    return BucketSummary(
        key=key,
        prefix_rows=len(prefix_rows),
        append_rows=len(append_rows),
        pair_count=len(prefix_rows) * len(append_rows),
        total_tokens_median=median(
            [row.total_tokens for row in prefix_rows] + [row.total_tokens for row in append_rows]
        ),
        output_tokens_median=median(
            [row.output_tokens for row in prefix_rows] + [row.output_tokens for row in append_rows]
        ),
        prefix_append_share_median=median(row.append_share for row in prefix_rows),
        append_append_share_median=median(row.append_share for row in append_rows),
        prefix_duration_median_ms=prefix_median,
        append_duration_median_ms=append_median,
        median_ratio=append_median / prefix_median if prefix_median > 0 else float("nan"),
        append_slower_probability=dominance,
        cliffs_delta=2 * dominance - 1,
        best_normalized_threshold=threshold,
        best_balanced_accuracy=balanced_accuracy,
        prefix_recall=prefix_recall,
        append_recall=append_recall,
    )


def analyze_rows(
    rows: list[TimingRow],
    *,
    append_heavy_share: float,
    prefix_heavy_max_append_share: float,
    min_append_tokens: int,
    min_prefix_tokens: int,
    min_bucket_per_side: int,
    total_bin_step: float,
    output_bin_step: float,
) -> tuple[list[BucketSummary], dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[BucketKey, dict[str, list[TimingRow]]] = defaultdict(
        lambda: {"prefix_heavy": [], "append_heavy": []}
    )
    class_counts: Counter[str] = Counter()

    for row in rows:
        group = row_group(
            row,
            append_heavy_share=append_heavy_share,
            prefix_heavy_max_append_share=prefix_heavy_max_append_share,
            min_append_tokens=min_append_tokens,
            min_prefix_tokens=min_prefix_tokens,
        )
        if group is None:
            class_counts["middle_rows"] += 1
            continue
        class_counts[group] += 1
        key = BucketKey(
            provider=row.provider,
            model=row.model,
            segment_kind=row.segment_kind,
            total_bin=token_bin(row.total_tokens, total_bin_step),
            output_bin=token_bin(row.output_tokens, output_bin_step),
        )
        grouped[key][group].append(row)

    summaries: list[BucketSummary] = []
    normalized_rows: list[dict[str, Any]] = []
    for key, parts in grouped.items():
        prefix_rows = parts["prefix_heavy"]
        append_rows = parts["append_heavy"]
        if len(prefix_rows) < min_bucket_per_side or len(append_rows) < min_bucket_per_side:
            continue
        summary = summarize_bucket(key, prefix_rows, append_rows)
        summaries.append(summary)
        prefix_median = summary.prefix_duration_median_ms
        for group_name, bucket_rows in [
            ("prefix_heavy", prefix_rows),
            ("append_heavy", append_rows),
        ]:
            for row in bucket_rows:
                normalized_rows.append(
                    {
                        "group": group_name,
                        "provider": row.provider,
                        "model": row.model,
                        "segment_kind": row.segment_kind,
                        "source_line": row.source_line,
                        "total_tokens": row.total_tokens,
                        "prefix_tokens": row.prefix_tokens,
                        "append_tokens": row.append_tokens,
                        "output_tokens": row.output_tokens,
                        "append_share": row.append_share,
                        "duration_ms": row.duration_ms,
                        "normalized_duration": row.duration_ms / prefix_median,
                        "bucket_key": key.label(),
                    }
                )

    summaries.sort(key=lambda item: item.pair_count, reverse=True)

    global_stats = aggregate_stats(summaries, normalized_rows, class_counts)
    return summaries, global_stats, normalized_rows


def aggregate_stats(
    summaries: list[BucketSummary],
    normalized_rows: list[dict[str, Any]],
    class_counts: Counter[str],
) -> dict[str, Any]:
    matched_prefix_rows = sum(item.prefix_rows for item in summaries)
    matched_append_rows = sum(item.append_rows for item in summaries)
    total_pairs = sum(item.pair_count for item in summaries)
    weighted_dominance = (
        sum(item.append_slower_probability * item.pair_count for item in summaries)
        / total_pairs
        if total_pairs
        else None
    )
    weighted_balanced_accuracy = (
        sum(
            item.best_balanced_accuracy * (item.prefix_rows + item.append_rows)
            for item in summaries
        )
        / (matched_prefix_rows + matched_append_rows)
        if matched_prefix_rows + matched_append_rows
        else None
    )
    median_ratio = (
        median(item.median_ratio for item in summaries) if summaries else None
    )

    prefix_norm = [
        row["normalized_duration"]
        for row in normalized_rows
        if row["group"] == "prefix_heavy"
    ]
    append_norm = [
        row["normalized_duration"]
        for row in normalized_rows
        if row["group"] == "append_heavy"
    ]
    global_threshold, global_ba, global_prefix_recall, global_append_recall = best_threshold(
        prefix_norm,
        append_norm,
    )
    global_norm_dominance = dominance_probability(append_norm, prefix_norm)

    bucket_append_median_slower = sum(
        1 for item in summaries if item.append_duration_median_ms > item.prefix_duration_median_ms
    )
    bucket_count = len(summaries)

    verdict = "inconclusive"
    if bucket_count == 0 or matched_prefix_rows == 0 or matched_append_rows == 0:
        verdict = "inconclusive: no matched buckets with both classes"
    elif (
        weighted_dominance is not None
        and weighted_dominance >= 0.80
        and global_ba >= 0.75
        and median_ratio is not None
        and median_ratio >= 1.50
    ):
        verdict = "supports clear separation"
    elif weighted_dominance is not None and weighted_dominance > 0.60:
        verdict = "supports append-slower effect, rejects clear no-mixing separation"
    else:
        verdict = "rejects append-slower separation"

    return {
        "verdict": verdict,
        "class_counts": dict(class_counts),
        "matched_buckets": bucket_count,
        "matched_prefix_rows": matched_prefix_rows,
        "matched_append_rows": matched_append_rows,
        "matched_pair_count": total_pairs,
        "pair_weighted_append_slower_probability": weighted_dominance,
        "pair_weighted_cliffs_delta": (
            2 * weighted_dominance - 1 if weighted_dominance is not None else None
        ),
        "median_bucket_median_duration_ratio": median_ratio,
        "row_weighted_bucket_threshold_balanced_accuracy": weighted_balanced_accuracy,
        "global_normalized_append_slower_probability": global_norm_dominance,
        "global_normalized_best_threshold": global_threshold,
        "global_normalized_best_balanced_accuracy": global_ba,
        "global_normalized_prefix_recall": global_prefix_recall,
        "global_normalized_append_recall": global_append_recall,
        "buckets_where_append_median_slower": bucket_append_median_slower,
        "bucket_append_median_slower_fraction": (
            bucket_append_median_slower / bucket_count if bucket_count else None
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def bucket_rows(summaries: list[BucketSummary]) -> list[dict[str, Any]]:
    return [
        {
            "provider": item.key.provider,
            "model": item.key.model,
            "segment_kind": item.key.segment_kind,
            "total_bin": item.key.total_bin,
            "output_bin": item.key.output_bin,
            "prefix_rows": item.prefix_rows,
            "append_rows": item.append_rows,
            "pair_count": item.pair_count,
            "total_tokens_median": item.total_tokens_median,
            "output_tokens_median": item.output_tokens_median,
            "prefix_append_share_median": item.prefix_append_share_median,
            "append_append_share_median": item.append_append_share_median,
            "prefix_duration_median_ms": item.prefix_duration_median_ms,
            "append_duration_median_ms": item.append_duration_median_ms,
            "median_ratio": item.median_ratio,
            "append_slower_probability": item.append_slower_probability,
            "cliffs_delta": item.cliffs_delta,
            "best_normalized_threshold": item.best_normalized_threshold,
            "best_balanced_accuracy": item.best_balanced_accuracy,
            "prefix_recall": item.prefix_recall,
            "append_recall": item.append_recall,
        }
        for item in summaries
    ]


def plot_bucket_effects(path: Path, summaries: list[BucketSummary], top_n: int) -> None:
    if not summaries:
        return
    selected = sorted(summaries[:top_n], key=lambda item: item.total_tokens_median)
    x = np.arange(len(selected))
    ratios = np.array([item.median_ratio for item in selected], dtype=np.float64)
    sizes = np.array(
        [max(20.0, math.sqrt(item.prefix_rows + item.append_rows) * 10) for item in selected],
        dtype=np.float64,
    )
    colors = [
        APPEND_ORANGE if item.key.provider == "codex" else PREFIX_BLUE
        for item in selected
    ]

    fig, ax = plt.subplots(figsize=(13.5, 6.2), constrained_layout=True)
    ax.scatter(x, ratios, s=sizes, c=colors, alpha=0.78, edgecolor="white", linewidth=0.8)
    ax.axhline(1.0, color="#111827", lw=1.2, alpha=0.75)
    ax.axhline(1.5, color="#b45309", lw=1.1, ls="--", alpha=0.75)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}x"))
    ax.set_ylabel("append-heavy median latency / prefix-heavy median latency")
    ax.set_xlabel("matched buckets, sorted by median total input tokens")
    ax.set_title("Matched Append-Heavy vs Prefix-Heavy Latency Ratios")
    ax.grid(True, axis="y", color=GRID_COLOR, lw=0.8)
    tick_positions = np.linspace(0, len(selected) - 1, min(8, len(selected)), dtype=int)
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(
        [fmt_number(selected[index].total_tokens_median, 0) for index in tick_positions],
        rotation=0,
    )
    legend_items = [
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=PREFIX_BLUE, label="Claude bucket", markersize=8),
        plt.Line2D([0], [0], marker="o", color="w", markerfacecolor=APPEND_ORANGE, label="Codex bucket", markersize=8),
        plt.Line2D([0], [0], color="#111827", label="equal medians", lw=1.2),
        plt.Line2D([0], [0], color="#b45309", label="1.5x slower", lw=1.1, ls="--"),
    ]
    ax.legend(handles=legend_items, loc="best")
    fig.savefig(path, dpi=240)
    plt.close(fig)


def plot_normalized_overlap(
    path: Path,
    normalized_rows: list[dict[str, Any]],
    *,
    sample_rows: int,
    seed: int,
) -> None:
    if not normalized_rows:
        return
    rng = random.Random(seed)
    sample = normalized_rows
    if len(sample) > sample_rows:
        sample = rng.sample(sample, sample_rows)

    prefix = [row for row in sample if row["group"] == "prefix_heavy"]
    append = [row for row in sample if row["group"] == "append_heavy"]

    fig, ax = plt.subplots(figsize=(12.5, 7.2), constrained_layout=True)
    ax.scatter(
        [row["append_share"] for row in prefix],
        [row["normalized_duration"] for row in prefix],
        s=13,
        alpha=0.28,
        color=PREFIX_BLUE,
        label=f"prefix-heavy sample n={len(prefix):,}",
    )
    ax.scatter(
        [row["append_share"] for row in append],
        [row["normalized_duration"] for row in append],
        s=13,
        alpha=0.32,
        color=APPEND_ORANGE,
        label=f"append-heavy sample n={len(append):,}",
    )
    ax.axhline(1.0, color="#111827", lw=1.2, alpha=0.8)
    ax.set_yscale("log")
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _: f"{v:g}x"))
    ax.xaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax.set_xlabel("append tokens / total input tokens")
    ax.set_ylabel("duration / matched bucket prefix-heavy median duration")
    ax.set_title("Normalized Latency Overlap in Matched Buckets")
    ax.grid(True, color=GRID_COLOR, lw=0.8)
    ax.legend(loc="best")
    fig.savefig(path, dpi=240)
    plt.close(fig)


def make_report(
    *,
    input_path: Path,
    output_dir: Path,
    config: dict[str, Any],
    load_stats: Counter[str],
    trim_stats: Counter[str],
    global_stats: dict[str, Any],
    summaries: list[BucketSummary],
) -> str:
    verdict = global_stats["verdict"]
    lines = [
        "# Append vs Prefix Latency",
        "",
        "## Hypothesis",
        "",
        (
            "Long-append rounds should be substantially slower than prefix-heavy "
            "rounds even when `prefix + append` is similar.  The strong version "
            "requires more than a positive average effect: the two groups should "
            "not mix much after matching."
        ),
        "",
        "## Matching Design",
        "",
        f"- Input CSV: `{input_path}`",
        f"- Rows scanned: {load_stats['input_rows']:,}",
        f"- Usable rows before trim: {load_stats['usable_rows']:,}",
        f"- Rows after trim: {trim_stats['rows_after_trim']:,}",
        f"- Minimum observed segment duration: {duration_label(config['min_duration_ms'])}",
        (
            "- Rows included by default are full-turn rows only: "
            "`user_message/tool_result -> tool_call`. Split Codex rows are excluded "
            "unless `--include-split-segments` is used."
        ),
        (
            "- Matched buckets use provider, model, segment kind, log-binned total "
            "input tokens, and log-binned output tokens."
        ),
        (
            f"- Append-heavy: append share >= {fmt_percent(config['append_heavy_share'])} "
            f"and append tokens >= {config['min_append_tokens']:,}."
        ),
        (
            f"- Prefix-heavy: append share <= {fmt_percent(config['prefix_heavy_max_append_share'])} "
            f"and prefix tokens >= {config['min_prefix_tokens']:,}."
        ),
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        (
            "The key effect-size statistic is `P(append-heavy duration > "
            "prefix-heavy duration)` over matched pairs.  A value near 0.5 means "
            "the dots mix; a value near 1 means append-heavy rows are almost always "
            "slower."
        ),
        "",
        f"- Matched buckets: {global_stats['matched_buckets']:,}",
        f"- Matched prefix-heavy rows: {global_stats['matched_prefix_rows']:,}",
        f"- Matched append-heavy rows: {global_stats['matched_append_rows']:,}",
        f"- Matched pair count: {global_stats['matched_pair_count']:,}",
        (
            "- Pair-weighted append-slower probability: "
            f"**{fmt_percent(global_stats['pair_weighted_append_slower_probability'])}**"
        ),
        (
            "- Pair-weighted Cliff's delta: "
            f"**{fmt_number(global_stats['pair_weighted_cliffs_delta'], 3)}** "
            "(`0` means no ordering, `1` means perfect append-slower ordering)."
        ),
        (
            "- Median bucket-level append/prefix median-latency ratio: "
            f"**{fmt_number(global_stats['median_bucket_median_duration_ratio'], 2)}x**"
        ),
        (
            "- Best global normalized duration threshold balanced accuracy: "
            f"**{fmt_percent(global_stats['global_normalized_best_balanced_accuracy'])}** "
            f"at threshold {fmt_number(global_stats['global_normalized_best_threshold'], 2)}x."
        ),
        (
            "- Buckets where append-heavy median is slower: "
            f"{global_stats['buckets_where_append_median_slower']:,} / "
            f"{global_stats['matched_buckets']:,} "
            f"({fmt_percent(global_stats['bucket_append_median_slower_fraction'])})."
        ),
        "",
        "## Interpretation",
        "",
    ]

    probability = global_stats["pair_weighted_append_slower_probability"]
    balanced_accuracy = global_stats["global_normalized_best_balanced_accuracy"]
    median_ratio = global_stats["median_bucket_median_duration_ratio"]
    if probability is not None and probability > 0.60:
        lines.append(
            "There is evidence that append-heavy rows are slower on average after "
            "matching.  However, the strong visual-separation hypothesis should only "
            "be accepted if the append-slower probability and threshold balanced "
            "accuracy are both high."
        )
    else:
        lines.append(
            "The matched data does not show a robust append-slower ordering.  Under "
            "these controls, append-heavy and prefix-heavy durations mix too much to "
            "support the hypothesis."
        )
    lines.append("")
    if (
        probability is not None
        and balanced_accuracy is not None
        and median_ratio is not None
        and (probability < 0.80 or balanced_accuracy < 0.75 or median_ratio < 1.50)
    ):
        lines.append(
            "For the strong no-mixing version, this is a rejection: at least one of "
            "the practical separation checks falls short of the default threshold "
            "(`80%` pair ordering, `75%` balanced threshold accuracy, and `1.5x` "
            "median bucket ratio)."
        )
    lines.extend(
        [
            "",
            "## Largest Matched Buckets",
            "",
            "| group | total tok | out tok | prefix n | append n | prefix median | append median | ratio | P(append slower) | balanced acc |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for item in summaries[:25]:
        lines.append(
            f"| {item.key.provider} / {item.key.model} / {item.key.segment_kind} "
            f"| {fmt_number(item.total_tokens_median, 0)} "
            f"| {fmt_number(item.output_tokens_median, 0)} "
            f"| {item.prefix_rows:,} "
            f"| {item.append_rows:,} "
            f"| {duration_label(item.prefix_duration_median_ms)} "
            f"| {duration_label(item.append_duration_median_ms)} "
            f"| {fmt_number(item.median_ratio, 2)}x "
            f"| {fmt_percent(item.append_slower_probability)} "
            f"| {fmt_percent(item.best_balanced_accuracy)} |"
        )

    lines.extend(
        [
            "",
            "## Outputs",
            "",
            f"- JSON summary: `{output_dir / 'append_vs_prefix_latency.json'}`",
            f"- Matched bucket CSV: `{output_dir / 'append_vs_prefix_matched_buckets.csv'}`",
            f"- Normalized row sample/source CSV: `{output_dir / 'append_vs_prefix_normalized_rows.csv'}`",
            f"- Bucket effect plot: `{output_dir / 'append_vs_prefix_bucket_effects.png'}`",
            f"- Normalized overlap plot: `{output_dir / 'append_vs_prefix_normalized_overlap.png'}`",
            "",
            "## Caveats",
            "",
            "- This is an observational trace analysis, not a controlled serving benchmark.",
            "- Matching controls token lengths and model/segment identity, but not queueing, batch composition, backend placement, cache residency, retries, or transient load.",
            "- A positive effect does not imply clean separation.  The latter is tested by the dominance probability and threshold balanced accuracy.",
            "",
        ]
    )
    return "\n".join(lines)


def analyze(
    *,
    input_path: Path,
    output_dir: Path,
    include_split_segments: bool,
    min_total_tokens: int,
    append_heavy_share: float,
    prefix_heavy_max_append_share: float,
    min_append_tokens: int,
    min_prefix_tokens: int,
    min_bucket_per_side: int,
    total_bin_step: float,
    output_bin_step: float,
    trim_quantile: float | None,
    min_duration_ms: float,
    max_duration_ms: float | None,
    plot_sample_rows: int,
    seed: int,
) -> dict[str, Any]:
    rows, load_stats = load_rows(
        input_path,
        include_split_segments=include_split_segments,
        min_total_tokens=min_total_tokens,
        min_duration_ms=min_duration_ms,
        max_duration_ms=max_duration_ms,
    )
    rows, trim_stats = trim_by_group(rows, trim_quantile=trim_quantile)
    summaries, global_stats, normalized_rows = analyze_rows(
        rows,
        append_heavy_share=append_heavy_share,
        prefix_heavy_max_append_share=prefix_heavy_max_append_share,
        min_append_tokens=min_append_tokens,
        min_prefix_tokens=min_prefix_tokens,
        min_bucket_per_side=min_bucket_per_side,
        total_bin_step=total_bin_step,
        output_bin_step=output_bin_step,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "include_split_segments": include_split_segments,
        "min_total_tokens": min_total_tokens,
        "append_heavy_share": append_heavy_share,
        "prefix_heavy_max_append_share": prefix_heavy_max_append_share,
        "min_append_tokens": min_append_tokens,
        "min_prefix_tokens": min_prefix_tokens,
        "min_bucket_per_side": min_bucket_per_side,
        "total_bin_step": total_bin_step,
        "output_bin_step": output_bin_step,
        "trim_quantile": trim_quantile,
        "min_duration_ms": min_duration_ms,
        "max_duration_ms": max_duration_ms,
        "plot_sample_rows": plot_sample_rows,
        "seed": seed,
    }
    result = {
        "input": str(input_path),
        "config": config,
        "load_stats": dict(load_stats),
        "trim_stats": dict(trim_stats),
        "summary": global_stats,
    }

    summary_path = output_dir / "append_vs_prefix_latency.json"
    md_path = output_dir / "append_vs_prefix_latency.md"
    bucket_csv_path = output_dir / "append_vs_prefix_matched_buckets.csv"
    normalized_csv_path = output_dir / "append_vs_prefix_normalized_rows.csv"
    bucket_plot_path = output_dir / "append_vs_prefix_bucket_effects.png"
    overlap_plot_path = output_dir / "append_vs_prefix_normalized_overlap.png"

    summary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(
        bucket_csv_path,
        bucket_rows(summaries),
        [
            "provider",
            "model",
            "segment_kind",
            "total_bin",
            "output_bin",
            "prefix_rows",
            "append_rows",
            "pair_count",
            "total_tokens_median",
            "output_tokens_median",
            "prefix_append_share_median",
            "append_append_share_median",
            "prefix_duration_median_ms",
            "append_duration_median_ms",
            "median_ratio",
            "append_slower_probability",
            "cliffs_delta",
            "best_normalized_threshold",
            "best_balanced_accuracy",
            "prefix_recall",
            "append_recall",
        ],
    )
    write_csv(
        normalized_csv_path,
        normalized_rows,
        [
            "group",
            "provider",
            "model",
            "segment_kind",
            "source_line",
            "total_tokens",
            "prefix_tokens",
            "append_tokens",
            "output_tokens",
            "append_share",
            "duration_ms",
            "normalized_duration",
            "bucket_key",
        ],
    )
    plot_bucket_effects(bucket_plot_path, summaries, top_n=200)
    plot_normalized_overlap(
        overlap_plot_path,
        normalized_rows,
        sample_rows=plot_sample_rows,
        seed=seed,
    )
    md_path.write_text(
        make_report(
            input_path=input_path,
            output_dir=output_dir,
            config=config,
            load_stats=load_stats,
            trim_stats=trim_stats,
            global_stats=global_stats,
            summaries=summaries,
        ),
        encoding="utf-8",
    )

    result["outputs"] = {
        "markdown": str(md_path),
        "json": str(summary_path),
        "matched_buckets_csv": str(bucket_csv_path),
        "normalized_rows_csv": str(normalized_csv_path),
        "bucket_effect_plot": str(bucket_plot_path),
        "normalized_overlap_plot": str(overlap_plot_path),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--include-split-segments", action="store_true")
    parser.add_argument("--min-total-tokens", type=int, default=4096)
    parser.add_argument("--append-heavy-share", type=float, default=0.25)
    parser.add_argument("--prefix-heavy-max-append-share", type=float, default=0.10)
    parser.add_argument("--min-append-tokens", type=int, default=1024)
    parser.add_argument("--min-prefix-tokens", type=int, default=4096)
    parser.add_argument("--min-bucket-per-side", type=int, default=5)
    parser.add_argument("--total-bin-step", type=float, default=0.25)
    parser.add_argument("--output-bin-step", type=float, default=0.50)
    parser.add_argument(
        "--trim-quantile",
        type=float,
        default=0.99,
        help="Per provider/model/segment duration trim. Use --trim-quantile 0 to disable.",
    )
    parser.add_argument(
        "--min-duration-ms",
        type=float,
        default=50.0,
        help="Drop implausibly tiny observed spans before matching. Default: 50ms.",
    )
    parser.add_argument("--max-duration-ms", type=float, default=None)
    parser.add_argument("--plot-sample-rows", type=int, default=80_000)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    trim_quantile = args.trim_quantile
    if trim_quantile == 0:
        trim_quantile = None
    elif trim_quantile is not None and not (0 < trim_quantile <= 1):
        parser.error("--trim-quantile must be in (0, 1], or 0 to disable")

    result = analyze(
        input_path=args.input,
        output_dir=args.output_dir,
        include_split_segments=args.include_split_segments,
        min_total_tokens=args.min_total_tokens,
        append_heavy_share=args.append_heavy_share,
        prefix_heavy_max_append_share=args.prefix_heavy_max_append_share,
        min_append_tokens=args.min_append_tokens,
        min_prefix_tokens=args.min_prefix_tokens,
        min_bucket_per_side=args.min_bucket_per_side,
        total_bin_step=args.total_bin_step,
        output_bin_step=args.output_bin_step,
        trim_quantile=trim_quantile,
        min_duration_ms=args.min_duration_ms,
        max_duration_ms=args.max_duration_ms,
        plot_sample_rows=args.plot_sample_rows,
        seed=args.seed,
    )

    summary = result["summary"]
    print(f"input={result['input']}")
    print(f"verdict={summary['verdict']}")
    print(f"matched_buckets={summary['matched_buckets']}")
    print(f"matched_prefix_rows={summary['matched_prefix_rows']}")
    print(f"matched_append_rows={summary['matched_append_rows']}")
    print(
        "pair_weighted_append_slower_probability="
        f"{summary['pair_weighted_append_slower_probability']}"
    )
    print(
        "global_normalized_best_balanced_accuracy="
        f"{summary['global_normalized_best_balanced_accuracy']}"
    )
    for name, path in result["outputs"].items():
        print(f"{name}={path}")
    png_sidecar.make_self_contained(
        args.output_dir,
        code_files=[Path(__file__)],
        readme_path=SCRIPT_DIR / "README.md",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
