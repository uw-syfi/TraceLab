#!/usr/bin/env python3
"""Plot whether prior output tracks the next round's append tokens."""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import png_sidecar
from style import TEXT_COLOR, plot_color, save_plot  # noqa: E402
from formatters import (
    apply_binary_token_axis,
    format_token_label,
    infer_first_token_tick,
    token_axis_value,
    token_axis_values,
)  # noqa: E402
import trace_db  # noqa: E402

import matplotlib.pyplot as plt


DEFAULT_OUTPUT_DIR = SCRIPT_DIR
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a raw
# TIMESTAMP: native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a *string*,
# while the int round-trips identically in both engines. We rebuild the naive datetime here so the
# gap-second arithmetic matches the pre-DuckDB ISO-parse path bit-for-bit. The trace is uniformly
# UTC, so the naive UTC microsecond timestamp the DB pins differs from the old tz-aware datetime only
# by the (constant) offset, which cancels in every timestamp *difference* the old code computed.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


@dataclass(frozen=True)
class Pair:
    provider: str
    model: str
    next_input: str
    prev_output: int
    next_append: int
    prefix_delta: int
    residual: int
    prev_reasoning: int
    gap_seconds: float


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    predicate: Callable[[Pair], bool]


def first_timestamp(events: list[tuple[str | None, datetime | None]]) -> datetime | None:
    """First event (in list / event_index order) with a non-null timestamp.

    Mirrors the old loader, which walked a round's ``timing_events`` list in order and returned the
    first parseable timestamp. The DB pins ``timestamp`` and we pull events ``ORDER BY event_index``,
    so list order == the round's original JSON order.
    """
    for _event_type, timestamp in events:
        if timestamp is not None:
            return timestamp
    return None


def last_model_output_timestamp(
    events: list[tuple[str | None, datetime | None]]
) -> datetime | None:
    """Latest timestamp among model-output events, falling back to the first observed timestamp."""
    timestamps: list[datetime] = []
    for event_type, timestamp in events:
        if event_type not in MODEL_OUTPUT_EVENT_TYPES:
            continue
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else first_timestamp(events)


def int_field(row: dict[str, Any], key: str) -> int:
    """Match the old int_field: keep ints, treat everything else (incl. None) as 0.

    DuckDB returns BIGINT columns as Python ints and NULL as None; booleans are excluded to mirror
    ``isinstance(value, int)`` rejecting bools the JSON loader never produced for these columns.
    """
    value = row.get(key)
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0


def load_pairs(
    con: "duckdb.DuckDBPyConnection", *, max_gap_seconds: float | None
) -> list[Pair]:
    """Reconstruct adjacent (previous, current) round pairs per session from the trace DB.

    The old JSONL loader grouped rows by ``(provider, session_id)`` in **file order** (the
    per-session list appears in first-appearance order), then did a *stable* sort by
    ``(round_index, first_timestamp)`` — so within a session, equal sort keys kept file order.
    ``ingest_seq`` (== ``round_pk``) is exactly that file order, so pulling ``ORDER BY ingest_seq``
    and grouping into ``rows_by_session`` reproduces BOTH the per-session row order AND the
    first-appearance session-visitation order byte-for-byte. The visitation order matters because the
    scatter subsample's stable sort by ``prev_output`` keeps the pair-append order on ties, and the
    pair-append order is driven by session-visitation order. The summary CSV is order-independent,
    but the scatter PNG is not. Rows missing a string ``provider``/``session_id`` or an integer
    ``round_index`` are skipped exactly as before; the DB pins those columns, so the skips are the
    NULL rows.
    """
    stats: Counter[str] = Counter()

    # Per-round timing events in original list order (event_index). Timestamps come back as
    # epoch-microseconds (int) for native/wasm-identical marshalling, rebuilt to naive datetimes.
    timing_by_round: dict[int, list[tuple[str | None, datetime | None]]] = defaultdict(list)
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        timing_by_round[round_pk].append((event_type, _epoch_us_to_datetime(ts_us)))

    rows_by_session: dict[
        tuple[str, str], list[tuple[int, datetime, dict[str, Any]]]
    ] = defaultdict(list)
    for (
        round_pk,
        provider,
        session_id,
        round_index,
        model,
        first_input_event_type,
        output_tokens,
        newly_append_tokens,
        prefix_tokens,
        input_tokens_total,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, session_id, round_index, model, "
        "first_input_event_type, output_tokens, newly_append_tokens, "
        "prefix_tokens, input_tokens_total, reasoning_output_tokens "
        "FROM rounds ORDER BY ingest_seq"
    ).fetchall():
        if not isinstance(provider, str) or not isinstance(session_id, str):
            stats["missing_provider_or_session"] += 1
            continue
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            stats["missing_index_or_timestamp"] += 1
            continue
        events = timing_by_round.get(round_pk, [])
        timestamp = first_timestamp(events)
        if timestamp is None:
            stats["missing_index_or_timestamp"] += 1
            continue
        row = {
            "provider": provider,
            "model": model,
            "first_input_event_type": first_input_event_type,
            "output_tokens": output_tokens,
            "newly_append_tokens": newly_append_tokens,
            "prefix_tokens": prefix_tokens,
            "input_tokens_total": input_tokens_total,
            "reasoning_output_tokens": reasoning_output_tokens,
            "timing_events": events,
        }
        rows_by_session[(provider, session_id)].append((round_index, timestamp, row))

    pairs: list[Pair] = []
    for rows in rows_by_session.values():
        rows.sort(key=lambda item: (item[0], item[1]))
        for _prev_index, _prev_ts, previous in rows:
            if int_field(previous, "input_tokens_total") <= 0:
                stats["skipped_missing_prev_total"] += 1

        for (_prev_index, _prev_ts, previous), (
            _current_index,
            current_ts,
            current,
        ) in zip(rows, rows[1:]):
            prev_total = int_field(previous, "input_tokens_total")
            prev_output = int_field(previous, "output_tokens")
            if prev_total <= 0 or prev_output <= 0:
                stats["skipped_missing_prev_tokens"] += 1
                continue
            output_at = last_model_output_timestamp(previous["timing_events"])
            if output_at is None:
                stats["skipped_missing_output_ts"] += 1
                continue
            gap_seconds = (current_ts - output_at).total_seconds()
            if gap_seconds < 0:
                stats["skipped_negative_gap"] += 1
                continue
            if max_gap_seconds is not None and gap_seconds > max_gap_seconds:
                stats["skipped_large_gap"] += 1
                continue
            next_append = int_field(current, "newly_append_tokens")
            prefix_delta = int_field(current, "prefix_tokens") - prev_total
            pairs.append(
                Pair(
                    provider=str(previous.get("provider")),
                    model=str(previous.get("model") or "unknown"),
                    next_input=str(current.get("first_input_event_type") or "unknown"),
                    prev_output=prev_output,
                    next_append=next_append,
                    prefix_delta=prefix_delta,
                    residual=next_append - prev_output,
                    prev_reasoning=int_field(previous, "reasoning_output_tokens"),
                    gap_seconds=gap_seconds,
                )
            )
            stats["pairs"] += 1

    print(f"Loaded {len(pairs):,} adjacent pairs", file=sys.stderr)
    if stats:
        print(f"Pair stats: {dict(stats)}", file=sys.stderr)
    return pairs


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6g}"


def tolerance_for_output(prev_output: int) -> float:
    return max(512.0, 0.10 * float(prev_output))


def prefix_close(pair: Pair) -> bool:
    tolerance = tolerance_for_output(pair.prev_output)
    return abs(pair.prefix_delta - pair.prev_output) <= tolerance


def prefix_rejects_output(pair: Pair) -> bool:
    tolerance = tolerance_for_output(pair.prev_output)
    return pair.prefix_delta < pair.prev_output - tolerance


def append_can_contain_output(pair: Pair) -> bool:
    tolerance = tolerance_for_output(pair.prev_output)
    return pair.next_append >= pair.prev_output - tolerance


def append_side_pair(pair: Pair) -> bool:
    return prefix_rejects_output(pair) and append_can_contain_output(pair)


def assignment_label(
    *,
    count: int,
    prefix_close_pct: float,
    append_side_pair_pct: float,
) -> tuple[str, str]:
    if count < 50:
        return "not_sure", "weak"
    if prefix_close_pct >= 70.0 and append_side_pair_pct <= 20.0:
        strength = (
            "very_strong" if count >= 300 and prefix_close_pct >= 80.0 else "strong"
        )
        return "prefix_side", strength
    if append_side_pair_pct >= 70.0 and prefix_close_pct <= 20.0:
        strength = (
            "very_strong" if count >= 300 and append_side_pair_pct >= 90.0 else "strong"
        )
        return "append_side", strength
    if prefix_close_pct >= 30.0 and append_side_pair_pct >= 30.0:
        strength = "strong" if count >= 100 else "moderate"
        return "mixed", strength
    return "not_sure", "weak"


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    xarr = np.asarray(xs, dtype=float)
    yarr = np.asarray(ys, dtype=float)
    if np.all(xarr == xarr[0]) or np.all(yarr == yarr[0]):
        return None
    return float(np.corrcoef(xarr, yarr)[0, 1])


def sampled_pairs(pairs: list[Pair], max_points: int) -> list[Pair]:
    if len(pairs) <= max_points:
        return pairs
    # Deterministic rank-stratified sampling preserves the output-token sweep.
    ordered = sorted(pairs, key=lambda pair: pair.prev_output)
    indexes = np.linspace(0, len(ordered) - 1, max_points, dtype=int)
    return [ordered[int(index)] for index in indexes]


def scenario_groups() -> list[Scenario]:
    return [
        Scenario(
            "claude_tool_result",
            "Claude -> tool result",
            lambda pair: pair.provider == "claude" and pair.next_input == "tool_result",
        ),
        Scenario(
            "claude_user_message",
            "Claude -> user message",
            lambda pair: (
                pair.provider == "claude" and pair.next_input == "user_message"
            ),
        ),
        Scenario(
            "gpt55_tool_result",
            "gpt-5.5 -> tool result",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.5"
                and pair.next_input == "tool_result"
            ),
        ),
        Scenario(
            "gpt55_user_message",
            "gpt-5.5 -> user message",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.5"
                and pair.next_input == "user_message"
            ),
        ),
        Scenario(
            "gpt54_tool_result",
            "gpt-5.4 -> tool result",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.4"
                and pair.next_input == "tool_result"
            ),
        ),
        Scenario(
            "gpt54_user_message",
            "gpt-5.4 -> user message",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.4"
                and pair.next_input == "user_message"
            ),
        ),
        Scenario(
            "gpt53_codex_tool_result",
            "gpt-5.3-codex -> tool result",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.3-codex"
                and pair.next_input == "tool_result"
            ),
        ),
        Scenario(
            "gpt52_codex_tool_result",
            "gpt-5.2-codex -> tool result",
            lambda pair: (
                pair.provider == "codex"
                and pair.model == "gpt-5.2-codex"
                and pair.next_input == "tool_result"
            ),
        ),
    ]


def output_axis_bounds(values: list[int], *, minimum: int) -> tuple[float, float]:
    positives = [value for value in values if value > 0]
    if not positives:
        return float(minimum), float(max(minimum * 2, 1))
    low = max(float(minimum), float(min(positives)))
    high = float(max(positives))
    if high <= low:
        high = low * 2
    return max(1.0, low / 1.15), high * 1.15


def apply_binary_token_axis_window(
    ax: plt.Axes,
    *,
    min_value: float,
    max_value: float,
    first_tick: float,
    max_ticks: int = 9,
) -> None:
    lower = token_axis_value(max(min_value, 1.0), first_tick)
    upper = token_axis_value(max(max_value, min_value, first_tick), first_tick)
    if upper <= lower:
        upper = lower + 1.0
    margin = max(0.03, (upper - lower) * 0.03)

    tick = 2 ** math.floor(math.log2(max(min_value, 1.0)))
    ticks: list[float] = []
    while tick <= max_value * 1.000001:
        if tick >= min_value / 1.000001:
            ticks.append(float(tick))
        tick *= 2
    if not ticks:
        ticks = [min_value, max_value]
    if len(ticks) > max_ticks:
        step = math.ceil(len(ticks) / max_ticks)
        ticks = ticks[::step]
        if ticks[-1] < max_value:
            ticks.append(max_value)

    ax.set_xlim(lower - margin, upper + margin)
    ax.set_xticks([token_axis_value(value, first_tick) for value in ticks])
    ax.set_xticklabels([format_token_label(value) for value in ticks])


def plot_scatter_grid(
    scenario_pairs: list[tuple[Scenario, list[Pair]]],
    output_dir: Path,
    *,
    min_output_tokens: int,
    max_points_per_scenario: int,
) -> Path | None:
    nonempty = [(scenario, pairs) for scenario, pairs in scenario_pairs if pairs]
    if not nonempty:
        return None

    columns = 2
    rows = math.ceil(len(nonempty) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(13, 4.2 * rows), squeeze=False)
    fig.suptitle(
        f"Previous Output vs Next Append (prev.output >= {min_output_tokens:,})",
        fontsize=15,
        fontweight="semibold",
        color=TEXT_COLOR,
    )

    y_values = [
        value
        for _scenario, pairs in nonempty
        for pair in pairs
        for value in (pair.prev_output, pair.next_append)
        if value > 0
    ]
    x_values = [
        pair.prev_output
        for _scenario, pairs in nonempty
        for pair in pairs
        if pair.prev_output > 0
    ]
    first_tick = infer_first_token_tick(y_values)
    y_max_value = max(y_values) if y_values else 1.0
    x_min_value, x_max_value = output_axis_bounds(x_values, minimum=min_output_tokens)

    for index, (scenario, pairs) in enumerate(nonempty):
        ax = axes[index // columns][index % columns]
        sample = sampled_pairs(pairs, max_points_per_scenario)
        xs = [pair.prev_output for pair in sample]
        ys = [pair.next_append for pair in sample]
        residuals = [pair.residual for pair in pairs]
        corr = correlation(
            [math.log2(max(1.0, pair.prev_output)) for pair in pairs],
            [math.log2(max(1.0, pair.next_append)) for pair in pairs],
        )
        ax.scatter(
            token_axis_values(xs, first_tick),
            token_axis_values(ys, first_tick),
            s=10,
            alpha=0.25,
            linewidths=0,
            color=plot_color(scenario.key, index),
        )
        line = np.linspace(x_min_value, x_max_value, 128)
        ax.plot(
            token_axis_values(line, first_tick),
            token_axis_values(line, first_tick),
            color="#111827",
            linewidth=1.0,
            alpha=0.65,
            label="y = prev.output",
        )
        title = f"{scenario.title}\nn={len(pairs):,}, corr(log)={fmt(corr)}"
        ax.set_title(title)
        ax.set_xlabel("Previous output tokens")
        ax.set_ylabel("Next append tokens")
        ax.grid(True, alpha=0.25)
        apply_binary_token_axis_window(
            ax,
            min_value=x_min_value,
            max_value=x_max_value,
            first_tick=first_tick,
        )
        apply_binary_token_axis(
            ax, axis="y", max_value=y_max_value, first_tick=first_tick
        )
        ax.text(
            0.02,
            0.98,
            f"median append-output: {fmt(percentile(residuals, 0.50))}",
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=8.5,
            color="#526070",
        )

    for index in range(len(nonempty), rows * columns):
        axes[index // columns][index % columns].axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = output_dir / f"output_vs_next_append_scatter_min{min_output_tokens}.png"
    save_plot(fig, out)
    return out


def plot_rank_grid(
    scenario_pairs: list[tuple[Scenario, list[Pair]]],
    output_dir: Path,
    *,
    min_output_tokens: int,
    max_points_per_scenario: int,
) -> Path | None:
    nonempty = [(scenario, pairs) for scenario, pairs in scenario_pairs if pairs]
    if not nonempty:
        return None

    columns = 2
    rows = math.ceil(len(nonempty) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(13, 4.2 * rows), squeeze=False)
    fig.suptitle(
        f"Ranked Previous Output and Next Append (prev.output >= {min_output_tokens:,})",
        fontsize=15,
        fontweight="semibold",
        color=TEXT_COLOR,
    )

    all_values = [
        value
        for _scenario, pairs in nonempty
        for pair in pairs
        for value in (pair.prev_output, pair.next_append)
        if value > 0
    ]
    first_tick = infer_first_token_tick(all_values)
    max_value = max(all_values) if all_values else 1.0

    for index, (scenario, pairs) in enumerate(nonempty):
        ax = axes[index // columns][index % columns]
        ordered = sorted(pairs, key=lambda pair: pair.prev_output)
        sample = sampled_pairs(ordered, max_points_per_scenario)
        ranks = [100.0 * i / max(1, len(sample) - 1) for i in range(len(sample))]
        output_values = [pair.prev_output for pair in sample]
        append_values = [pair.next_append for pair in sample]
        ax.scatter(
            ranks,
            token_axis_values(append_values, first_tick),
            s=10,
            alpha=0.25,
            linewidths=0,
            color=plot_color(scenario.key, index),
            label="next append",
        )
        ax.plot(
            ranks,
            token_axis_values(output_values, first_tick),
            color="#111827",
            linewidth=1.2,
            alpha=0.7,
            label="prev output rank curve",
        )
        ax.set_title(f"{scenario.title}\nn={len(pairs):,}")
        ax.set_xlabel("Rank by previous output tokens (%)")
        ax.set_ylabel("Tokens (binary scale)")
        ax.grid(True, alpha=0.25)
        ax.set_xlim(-1, 101)
        apply_binary_token_axis(
            ax, axis="y", max_value=max_value, first_tick=first_tick
        )
        ax.legend(fontsize=8.5)

    for index in range(len(nonempty), rows * columns):
        axes[index // columns][index % columns].axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = output_dir / f"ranked_output_vs_next_append_min{min_output_tokens}.png"
    save_plot(fig, out)
    return out


def positive_prefix_gain(pair: Pair) -> int:
    return max(0, pair.prefix_delta)


def plot_prefix_gain_scatter_grid(
    scenario_pairs: list[tuple[Scenario, list[Pair]]],
    output_dir: Path,
    *,
    min_output_tokens: int,
    max_points_per_scenario: int,
) -> Path | None:
    nonempty = [(scenario, pairs) for scenario, pairs in scenario_pairs if pairs]
    if not nonempty:
        return None

    columns = 2
    rows = math.ceil(len(nonempty) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(13, 4.2 * rows), squeeze=False)
    fig.suptitle(
        f"Previous Output vs Next Prefix Gain (prev.output >= {min_output_tokens:,})",
        fontsize=15,
        fontweight="semibold",
        color=TEXT_COLOR,
    )

    y_values = [
        value
        for _scenario, pairs in nonempty
        for pair in pairs
        for value in (pair.prev_output, positive_prefix_gain(pair))
        if value > 0
    ]
    x_values = [
        pair.prev_output
        for _scenario, pairs in nonempty
        for pair in pairs
        if pair.prev_output > 0
    ]
    first_tick = infer_first_token_tick(y_values)
    y_max_value = max(y_values) if y_values else 1.0
    x_min_value, x_max_value = output_axis_bounds(x_values, minimum=min_output_tokens)

    for index, (scenario, pairs) in enumerate(nonempty):
        ax = axes[index // columns][index % columns]
        sample = sampled_pairs(pairs, max_points_per_scenario)
        xs = [pair.prev_output for pair in sample]
        ys = [positive_prefix_gain(pair) for pair in sample]
        residuals = [pair.prefix_delta - pair.prev_output for pair in pairs]
        corr = correlation(
            [math.log2(max(1.0, pair.prev_output)) for pair in pairs],
            [math.log2(max(1.0, positive_prefix_gain(pair))) for pair in pairs],
        )
        ax.scatter(
            token_axis_values(xs, first_tick),
            token_axis_values(ys, first_tick),
            s=10,
            alpha=0.25,
            linewidths=0,
            color=plot_color(scenario.key, index),
        )
        line = np.linspace(x_min_value, x_max_value, 128)
        ax.plot(
            token_axis_values(line, first_tick),
            token_axis_values(line, first_tick),
            color="#111827",
            linewidth=1.0,
            alpha=0.65,
            label="y = prev.output",
        )
        title = f"{scenario.title}\nn={len(pairs):,}, corr(log)={fmt(corr)}"
        ax.set_title(title)
        ax.set_xlabel("Previous output tokens")
        ax.set_ylabel("Next prefix gain tokens")
        ax.grid(True, alpha=0.25)
        apply_binary_token_axis_window(
            ax,
            min_value=x_min_value,
            max_value=x_max_value,
            first_tick=first_tick,
        )
        apply_binary_token_axis(
            ax, axis="y", max_value=y_max_value, first_tick=first_tick
        )
        ax.text(
            0.02,
            0.98,
            f"median prefix_gain-output: {fmt(percentile(residuals, 0.50))}\n"
            "nonpositive gains shown at 0",
            transform=ax.transAxes,
            va="top",
            ha="left",
            fontsize=8.5,
            color="#526070",
        )

    for index in range(len(nonempty), rows * columns):
        axes[index // columns][index % columns].axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = output_dir / f"output_vs_prefix_gain_scatter_min{min_output_tokens}.png"
    save_plot(fig, out)
    return out


def plot_prefix_gain_rank_grid(
    scenario_pairs: list[tuple[Scenario, list[Pair]]],
    output_dir: Path,
    *,
    min_output_tokens: int,
    max_points_per_scenario: int,
) -> Path | None:
    nonempty = [(scenario, pairs) for scenario, pairs in scenario_pairs if pairs]
    if not nonempty:
        return None

    columns = 2
    rows = math.ceil(len(nonempty) / columns)
    fig, axes = plt.subplots(rows, columns, figsize=(13, 4.2 * rows), squeeze=False)
    fig.suptitle(
        f"Ranked Previous Output and Next Prefix Gain (prev.output >= {min_output_tokens:,})",
        fontsize=15,
        fontweight="semibold",
        color=TEXT_COLOR,
    )

    all_values = [
        value
        for _scenario, pairs in nonempty
        for pair in pairs
        for value in (pair.prev_output, positive_prefix_gain(pair))
        if value > 0
    ]
    first_tick = infer_first_token_tick(all_values)
    max_value = max(all_values) if all_values else 1.0

    for index, (scenario, pairs) in enumerate(nonempty):
        ax = axes[index // columns][index % columns]
        ordered = sorted(pairs, key=lambda pair: pair.prev_output)
        sample = sampled_pairs(ordered, max_points_per_scenario)
        ranks = [100.0 * i / max(1, len(sample) - 1) for i in range(len(sample))]
        output_values = [pair.prev_output for pair in sample]
        prefix_gain_values = [positive_prefix_gain(pair) for pair in sample]
        ax.scatter(
            ranks,
            token_axis_values(prefix_gain_values, first_tick),
            s=10,
            alpha=0.25,
            linewidths=0,
            color=plot_color(scenario.key, index),
            label="next prefix gain",
        )
        ax.plot(
            ranks,
            token_axis_values(output_values, first_tick),
            color="#111827",
            linewidth=1.2,
            alpha=0.7,
            label="prev output rank curve",
        )
        ax.set_title(f"{scenario.title}\nn={len(pairs):,}")
        ax.set_xlabel("Rank by previous output tokens (%)")
        ax.set_ylabel("Tokens (binary scale)")
        ax.grid(True, alpha=0.25)
        ax.set_xlim(-1, 101)
        apply_binary_token_axis(
            ax, axis="y", max_value=max_value, first_tick=first_tick
        )
        ax.legend(fontsize=8.5)

    for index in range(len(nonempty), rows * columns):
        axes[index // columns][index % columns].axis("off")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = output_dir / f"ranked_output_vs_prefix_gain_min{min_output_tokens}.png"
    save_plot(fig, out)
    return out


def write_summary(
    scenario_pairs: list[tuple[Scenario, list[Pair]]],
    output_dir: Path,
    *,
    min_output_tokens: int,
) -> Path:
    out = output_dir / f"output_append_assignment_summary_min{min_output_tokens}.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "scenario",
                "count",
                "decision",
                "decision_strength",
                "corr_log_output_append",
                "corr_log_output_prefix_gain",
                "prefix_close_pct",
                "prefix_reject_pct",
                "append_can_contain_output_pct",
                "append_side_pair_pct",
                "prefix_close_and_append_can_pct",
                "unassigned_pair_pct",
                "median_prev_output",
                "median_next_append",
                "median_prefix_gain",
                "median_append_minus_output",
                "median_prefix_gain_minus_output",
                "p10_append_minus_output",
                "p90_append_minus_output",
            ],
        )
        writer.writeheader()
        for scenario, pairs in scenario_pairs:
            if not pairs:
                continue
            append_can_count = 0
            prefix_close_count = 0
            prefix_reject_count = 0
            append_side_pair_count = 0
            prefix_close_and_append_can_count = 0
            unassigned_pair_count = 0
            residuals: list[float] = []
            for pair in pairs:
                is_prefix_close = prefix_close(pair)
                is_prefix_reject = prefix_rejects_output(pair)
                is_append_can = append_can_contain_output(pair)
                is_append_side = is_prefix_reject and is_append_can
                append_can_count += is_append_can
                prefix_close_count += is_prefix_close
                prefix_reject_count += is_prefix_reject
                append_side_pair_count += is_append_side
                prefix_close_and_append_can_count += is_prefix_close and is_append_can
                unassigned_pair_count += not is_prefix_close and not is_append_side
                residuals.append(float(pair.residual))
            prefix_close_pct = 100 * prefix_close_count / len(pairs)
            append_side_pair_pct = 100 * append_side_pair_count / len(pairs)
            decision, decision_strength = assignment_label(
                count=len(pairs),
                prefix_close_pct=prefix_close_pct,
                append_side_pair_pct=append_side_pair_pct,
            )
            corr = correlation(
                [math.log2(max(1.0, pair.prev_output)) for pair in pairs],
                [math.log2(max(1.0, pair.next_append)) for pair in pairs],
            )
            prefix_corr = correlation(
                [math.log2(max(1.0, pair.prev_output)) for pair in pairs],
                [math.log2(max(1.0, positive_prefix_gain(pair))) for pair in pairs],
            )
            writer.writerow(
                {
                    "scenario": scenario.key,
                    "count": len(pairs),
                    "decision": decision,
                    "decision_strength": decision_strength,
                    "corr_log_output_append": fmt(corr),
                    "corr_log_output_prefix_gain": fmt(prefix_corr),
                    "prefix_close_pct": fmt(prefix_close_pct),
                    "prefix_reject_pct": fmt(100 * prefix_reject_count / len(pairs)),
                    "append_can_contain_output_pct": fmt(
                        100 * append_can_count / len(pairs)
                    ),
                    "append_side_pair_pct": fmt(append_side_pair_pct),
                    "prefix_close_and_append_can_pct": fmt(
                        100 * prefix_close_and_append_can_count / len(pairs)
                    ),
                    "unassigned_pair_pct": fmt(
                        100 * unassigned_pair_count / len(pairs)
                    ),
                    "median_prev_output": fmt(
                        percentile([pair.prev_output for pair in pairs], 0.50)
                    ),
                    "median_next_append": fmt(
                        percentile([pair.next_append for pair in pairs], 0.50)
                    ),
                    "median_prefix_gain": fmt(
                        percentile([positive_prefix_gain(pair) for pair in pairs], 0.50)
                    ),
                    "median_append_minus_output": fmt(percentile(residuals, 0.50)),
                    "median_prefix_gain_minus_output": fmt(
                        percentile(
                            [pair.prefix_delta - pair.prev_output for pair in pairs],
                            0.50,
                        )
                    ),
                    "p10_append_minus_output": fmt(percentile(residuals, 0.10)),
                    "p90_append_minus_output": fmt(percentile(residuals, 0.90)),
                }
            )
    print(f"Saved {out}", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-gap-seconds", type=float, default=240.0)
    parser.add_argument(
        "--min-output-tokens", type=int, nargs="+", default=[2_000, 4_000]
    )
    parser.add_argument("--max-points-per-scenario", type=int, default=6_000)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    pairs = load_pairs(con, max_gap_seconds=args.max_gap_seconds)
    scenarios = scenario_groups()

    outputs: list[Path] = []
    for minimum in args.min_output_tokens:
        filtered = [
            (
                scenario,
                [
                    pair
                    for pair in pairs
                    if pair.prev_output >= minimum and scenario.predicate(pair)
                ],
            )
            for scenario in scenarios
        ]
        outputs.append(
            write_summary(filtered, args.output_dir, min_output_tokens=minimum)
        )
        scatter = plot_scatter_grid(
            filtered,
            args.output_dir,
            min_output_tokens=minimum,
            max_points_per_scenario=args.max_points_per_scenario,
        )
        if scatter is not None:
            outputs.append(scatter)
        rank = plot_rank_grid(
            filtered,
            args.output_dir,
            min_output_tokens=minimum,
            max_points_per_scenario=args.max_points_per_scenario,
        )
        if rank is not None:
            outputs.append(rank)
        prefix_scatter = plot_prefix_gain_scatter_grid(
            filtered,
            args.output_dir,
            min_output_tokens=minimum,
            max_points_per_scenario=args.max_points_per_scenario,
        )
        if prefix_scatter is not None:
            outputs.append(prefix_scatter)
        prefix_rank = plot_prefix_gain_rank_grid(
            filtered,
            args.output_dir,
            min_output_tokens=minimum,
            max_points_per_scenario=args.max_points_per_scenario,
        )
        if prefix_rank is not None:
            outputs.append(prefix_rank)

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
