#!/usr/bin/env python3
"""Fraction of useful generation time vs cache-eviction timeout (KV cache liveness).

For each eviction timeout T, computes how much of the wall time the KV cache would
still be "active" (worth keeping). See README.md for the exact formula.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from typing import Any  # noqa: E402
import csv  # noqa: E402
import math  # noqa: E402
import numpy as np  # noqa: E402
from style import (
    AXIS_COLOR,
    TEXT_COLOR,
    mticker,
    plot_color,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
)  # noqa: E402
from formatters import (
    KV_CACHE_TIMEOUT_LANDMARKS_SECONDS,
    KV_CACHE_TIMEOUT_TICK_SECONDS,
    cumulative_values_at_thresholds_seconds,
    format_duration_compact,
    format_duration_seconds_tick,
    format_seconds_as_hours_compact,
    kv_cache_timeout_thresholds_seconds,
)  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402
from timing import human_waits_from_event_pairs  # noqa: E402

# Event-type sets (mirror artifacts/utils/timing.py exactly).
_INPUT_EVENT_TYPES = ("user_message", "tool_result")
_MODEL_OUTPUT_EVENT_TYPES = ("reasoning", "text", "tool_call")

_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def _in_set_sql(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"


def load_llm_generation_seconds_by_provider(con) -> dict[str, list[float]]:
    """Per-round ``input_to_last_output_span_seconds`` grouped by provider.

    Replicates ``timing.input_to_last_output_span_seconds``: with ``first_output =
    min(output_ts)`` and ``last_input = max(input_ts <= first_output)``, the span is
    ``max(output_ts) - last_input``, kept only when strictly positive. Timestamps are
    fetched as integer microseconds (``epoch_us``) per the DB_SCHEMA gotcha, then divided
    by 1e6 — identical to the old ``timedelta.total_seconds()`` on UTC datetimes.
    """
    input_in = _in_set_sql("event_type", _INPUT_EVENT_TYPES)
    output_in = _in_set_sql("event_type", _MODEL_OUTPUT_EVENT_TYPES)
    rows = con.execute(
        f"""
        WITH ev AS (
            SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us
            FROM timing_events
            WHERE timestamp IS NOT NULL
              AND ({input_in} OR {output_in})
        ),
        bounds AS (
            SELECT
                round_pk,
                min(CASE WHEN {output_in} THEN ts_us END) AS first_output_us,
                max(CASE WHEN {output_in} THEN ts_us END) AS last_output_us
            FROM ev
            GROUP BY round_pk
        ),
        agg AS (
            SELECT
                b.round_pk,
                b.first_output_us,
                b.last_output_us,
                max(CASE WHEN {input_in} AND ev.ts_us <= b.first_output_us
                         THEN ev.ts_us END) AS last_input_us
            FROM bounds b
            JOIN ev USING (round_pk)
            WHERE b.first_output_us IS NOT NULL
            GROUP BY b.round_pk, b.first_output_us, b.last_output_us
        )
        SELECT
            COALESCE(r.provider, '<unknown-provider>') AS provider,
            (a.last_output_us - a.last_input_us) / 1e6 AS span_seconds
        FROM agg a
        JOIN rounds r USING (round_pk)
        WHERE a.last_input_us IS NOT NULL
          AND (a.last_output_us - a.last_input_us) > 0
        """
    ).fetchall()

    by_provider: dict[str, list[float]] = {}
    for provider, span_seconds in rows:
        by_provider.setdefault(provider, []).append(float(span_seconds))
    return by_provider


def load_tool_latency_values_by_provider(con) -> dict[str, list[float]]:
    """Effective tool latency (ms) per call, grouped by provider, positive only.

    Mirrors the old ``tool_latency_ms`` (internal else wall; legacy ``latency_ms`` is
    absent from the normalized schema) with the ``latency > 0`` filter.
    """
    latency_sql = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL
    rows = con.execute(
        f"""
        SELECT COALESCE(r.provider, '<unknown-provider>') AS provider,
               CAST({latency_sql} AS DOUBLE) AS latency_ms
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE ({latency_sql}) IS NOT NULL AND ({latency_sql}) > 0
        """
    ).fetchall()

    by_provider: dict[str, list[float]] = {}
    for provider, latency_ms in rows:
        by_provider.setdefault(provider, []).append(float(latency_ms))
    return by_provider


def load_human_input_wait_seconds_by_provider(con) -> dict[str, list[float]]:
    """Per-session human-wait gaps grouped by provider, plus an ``all`` group.

    Provider-agnostic definition (see ``timing.human_waits_from_event_pairs``): the wait before a
    ``user_message`` is the gap from the **previous event of any type** in the same session
    (including non-output events such as Codex ``usage_report``), counting every user message. For
    the cache-eviction view this is the right idle: the KV cache sits unused from the session's
    last event until the next user prompt. Stateful single pass over rounds in ingestion order
    (``round_pk`` == file order), carrying ``last_event_at`` per session.
    """
    events_by_round: dict[int, list[tuple[str | None, datetime | None]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        events_by_round.setdefault(round_pk, []).append(
            (event_type, _epoch_us_to_datetime(ts_us))
        )

    human_by_provider: dict[str, list[float]] = {"all": []}
    last_event_at_by_session: dict[str, datetime] = {}
    for round_pk, session_id, provider in con.execute(
        "SELECT round_pk, session_id, COALESCE(provider, '<unknown-provider>') AS provider "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        if not (isinstance(session_id, str) and session_id):
            continue
        events = events_by_round.get(round_pk, [])
        waits, _n_user, last = human_waits_from_event_pairs(
            events, last_event_at_by_session.get(session_id)
        )
        for wait_seconds in waits:
            human_by_provider["all"].append(wait_seconds)
            human_by_provider.setdefault(provider, []).append(wait_seconds)
        if last is not None:
            last_event_at_by_session[session_id] = last
    return human_by_provider


def plot_kv_cache_active_ratio_by_provider(
    generation_seconds_by_provider: dict[str, list[float]],
    tool_latency_ms_by_provider: dict[str, list[float]],
    human_wait_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> None:
    providers = (
        {provider for provider in generation_seconds_by_provider if provider != "all"}
        | {provider for provider in tool_latency_ms_by_provider if provider != "all"}
        | {provider for provider in human_wait_seconds_by_provider if provider != "all"}
    )
    providers = {
        provider
        for provider in providers
        if generation_seconds_by_provider.get(provider)
    }
    if not providers:
        return

    thresholds = kv_cache_timeout_thresholds_seconds()
    if thresholds.size == 0:
        return

    fig, ax = plt.subplots(figsize=(10.8, 6.2))
    ax.set_title("KV Cache Active Ratio by Eviction Timeout")
    ax.set_xlabel("Cache eviction timeout")
    ax.set_ylabel("Generation time share")
    ax.set_xscale("log")
    ax.set_xticks(KV_CACHE_TIMEOUT_TICK_SECONDS)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_duration_seconds_tick))
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    polish_axes(ax, grid_axis="both")

    for timeout in KV_CACHE_TIMEOUT_LANDMARKS_SECONDS:
        ax.axvline(
            timeout,
            color=AXIS_COLOR,
            linestyle=(0, (4, 3)),
            linewidth=0.9,
            alpha=0.78,
            zorder=0,
        )

    table_rows: list[tuple[str, float, dict[int, float]]] = []
    min_ratio = 1.0
    max_ratio = 0.0
    for index, provider in enumerate(provider_order(providers)):
        generation_values = [
            value
            for value in generation_seconds_by_provider.get(provider, [])
            if value > 0 and math.isfinite(value)
        ]
        if not generation_values:
            continue
        generation_total_seconds = float(np.sum(generation_values))
        tool_values_seconds = [
            value / 1000
            for value in tool_latency_ms_by_provider.get(provider, [])
            if value > 0 and math.isfinite(value)
        ]
        human_values_seconds = human_wait_seconds_by_provider.get(provider, [])
        tool_cumulative_seconds = cumulative_values_at_thresholds_seconds(
            tool_values_seconds,
            thresholds,
        )
        human_cumulative_seconds = cumulative_values_at_thresholds_seconds(
            human_values_seconds,
            thresholds,
        )
        denominator_seconds = (
            generation_total_seconds
            + tool_cumulative_seconds
            + human_cumulative_seconds
        )
        ratios = np.divide(
            generation_total_seconds,
            denominator_seconds,
            out=np.zeros_like(denominator_seconds, dtype=float),
            where=denominator_seconds > 0,
        )
        if ratios.size:
            min_ratio = min(min_ratio, float(np.min(ratios)))
            max_ratio = max(max_ratio, float(np.max(ratios)))
        landmark_ratios: dict[int, float] = {}
        for timeout in KV_CACHE_TIMEOUT_LANDMARKS_SECONDS:
            timeout_index = int(np.searchsorted(thresholds, timeout, side="left"))
            if timeout_index < thresholds.size and thresholds[timeout_index] == timeout:
                landmark_ratios[timeout] = float(ratios[timeout_index])
        table_rows.append(
            (provider_title(provider), generation_total_seconds, landmark_ratios)
        )
        ax.plot(
            thresholds,
            ratios,
            linewidth=2.35,
            color=plot_color(provider, index),
            label=(
                f"{provider_title(provider)} "
                f"(gen={format_seconds_as_hours_compact(generation_total_seconds)})"
            ),
        )

    ax.set_xlim(thresholds[0], thresholds[-1])
    lower = max(0.0, min_ratio - 0.04)
    upper = min(1.0, max_ratio + 0.04)
    if upper - lower < 0.08:
        upper = min(1.0, lower + 0.08)
    ax.set_ylim(lower, upper)
    ax.legend(fontsize=9.5, loc="upper right")

    if table_rows:
        headings = ["provider", "gen", "1m", "5m", "10m", "30m", "1h"]
        stats_lines = [
            "KV active ratio",
            f"{headings[0]:<8} {headings[1]:>8} "
            f"{headings[2]:>6} {headings[3]:>6} {headings[4]:>6} "
            f"{headings[5]:>6} {headings[6]:>6}",
        ]
        for provider, generation_total_seconds, landmark_ratios in table_rows:
            ratio_values = [
                landmark_ratios.get(timeout, 0.0)
                for timeout in KV_CACHE_TIMEOUT_LANDMARKS_SECONDS
            ]
            stats_lines.append(
                f"{provider:<8} "
                f"{format_seconds_as_hours_compact(generation_total_seconds):>8} "
                f"{ratio_values[0] * 100:>5.1f}% "
                f"{ratio_values[1] * 100:>5.1f}% "
                f"{ratio_values[2] * 100:>5.1f}% "
                f"{ratio_values[3] * 100:>5.1f}% "
                f"{ratio_values[4] * 100:>5.1f}%"
            )
        ax.text(
            0.012,
            0.035,
            "\n".join(stats_lines),
            transform=ax.transAxes,
            ha="left",
            va="bottom",
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
    out = output_dir / "kv_cache_active_ratio_by_provider.png"
    save_plot(fig, out)


def write_kv_cache_active_ratio_by_provider(
    generation_seconds_by_provider: dict[str, list[float]],
    tool_latency_ms_by_provider: dict[str, list[float]],
    human_wait_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    providers = (
        {provider for provider in generation_seconds_by_provider if provider != "all"}
        | {provider for provider in tool_latency_ms_by_provider if provider != "all"}
        | {provider for provider in human_wait_seconds_by_provider if provider != "all"}
    )
    providers = {
        provider
        for provider in providers
        if generation_seconds_by_provider.get(provider)
    }
    thresholds = kv_cache_timeout_thresholds_seconds()
    landmark_set = {float(value) for value in KV_CACHE_TIMEOUT_LANDMARKS_SECONDS}
    rows: list[dict[str, Any]] = []
    for provider in provider_order(providers):
        generation_values = [
            value
            for value in generation_seconds_by_provider.get(provider, [])
            if value > 0 and math.isfinite(value)
        ]
        if not generation_values:
            continue
        generation_total_seconds = float(np.sum(generation_values))
        tool_values_seconds = [
            value / 1000
            for value in tool_latency_ms_by_provider.get(provider, [])
            if value > 0 and math.isfinite(value)
        ]
        human_values_seconds = human_wait_seconds_by_provider.get(provider, [])
        tool_cumulative_seconds = cumulative_values_at_thresholds_seconds(
            tool_values_seconds,
            thresholds,
        )
        human_cumulative_seconds = cumulative_values_at_thresholds_seconds(
            human_values_seconds,
            thresholds,
        )
        for timeout, tool_seconds, human_seconds in zip(
            thresholds,
            tool_cumulative_seconds,
            human_cumulative_seconds,
            strict=True,
        ):
            denominator_seconds = (
                generation_total_seconds + float(tool_seconds) + float(human_seconds)
            )
            active_ratio = (
                generation_total_seconds / denominator_seconds
                if denominator_seconds > 0
                else 0.0
            )
            rows.append(
                {
                    "provider": provider,
                    "cache_eviction_timeout_seconds": float(timeout),
                    "cache_eviction_timeout_label": format_duration_compact(
                        float(timeout)
                    ),
                    "landmark_timeout": float(timeout) in landmark_set,
                    "generation_seconds": generation_total_seconds,
                    "generation_hours": generation_total_seconds / 3600,
                    "tool_cumulative_seconds": float(tool_seconds),
                    "tool_cumulative_hours": float(tool_seconds) / 3600,
                    "human_cumulative_seconds": float(human_seconds),
                    "human_cumulative_hours": float(human_seconds) / 3600,
                    "denominator_seconds": denominator_seconds,
                    "denominator_hours": denominator_seconds / 3600,
                    "kv_cache_active_ratio": active_ratio,
                }
            )

    path = output_dir / "kv_cache_active_ratio_by_provider.csv"
    fieldnames = [
        "provider",
        "cache_eviction_timeout_seconds",
        "cache_eviction_timeout_label",
        "landmark_timeout",
        "generation_seconds",
        "generation_hours",
        "tool_cumulative_seconds",
        "tool_cumulative_hours",
        "human_cumulative_seconds",
        "human_cumulative_hours",
        "denominator_seconds",
        "denominator_hours",
        "kv_cache_active_ratio",
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
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    gen = load_llm_generation_seconds_by_provider(con)
    tool = load_tool_latency_values_by_provider(con)
    human = load_human_input_wait_seconds_by_provider(con)

    plot_kv_cache_active_ratio_by_provider(gen, tool, human, out)
    write_kv_cache_active_ratio_by_provider(gen, tool, human, out)

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
