#!/usr/bin/env python3
"""How long the model waits for the human between responses (idle wait CDFs).

Human input wait = previous model-output event -> the next user_message, in the same
session. See README.md for the definition and caveats.
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
import numpy as np  # noqa: E402
from style import (
    TEXT_COLOR,
    mticker,
    plot_color,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
)  # noqa: E402
from accumulators import sample_percentiles  # noqa: E402
from formatters import CDF_REFERENCE_SECONDS, format_duration_seconds_tick  # noqa: E402
from cdf import (
    plot_count_cdf_by_provider,
    plot_cumulative_duration_cdf_by_provider,
    write_count_cdf_by_provider,
    write_cumulative_duration_cdf_by_provider,
)  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

ONE_HOUR_SECONDS = 60 * 60

# Same event-type partitions as the pre-DuckDB timing helper (artifacts/utils/timing.py).
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines. Rebuild the naive datetime here exactly
# (integer microseconds). The old path parsed ISO strings to tz-aware datetimes, but a difference
# between two same-tz datetimes equals the naive epoch_us difference to the microsecond, so the waits
# match the pre-DuckDB result bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def _response_trigger_user_message_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    """Reproduce timing.response_trigger_user_message_timestamp for one round's events.

    Collect this round's user_message timestamps and model-output (reasoning/text/tool_call)
    timestamps. If either list is empty -> None. Otherwise take the earliest output as
    first_output; keep only user inputs at-or-before first_output as candidates; if none -> None;
    else return the latest such candidate.
    """
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event_type, ts in events:
        if ts is None:
            continue
        if event_type == "user_message":
            user_timestamps.append(ts)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(ts)
    if not user_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_users = [ts for ts in user_timestamps if ts <= first_output_at]
    if not candidate_users:
        return None
    return max(candidate_users)


def _last_model_output_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    """Reproduce timing.last_model_output_timestamp: latest model-output timestamp, or None."""
    output_timestamps = [
        ts for event_type, ts in events if event_type in MODEL_OUTPUT_EVENT_TYPES and ts is not None
    ]
    return max(output_timestamps) if output_timestamps else None


def load_human_input_wait_seconds_by_provider(
    con: "duckdb.DuckDBPyConnection",
) -> dict[str, list[float]]:
    """``{"all": [...], provider: [...]}`` — per-session idle waits between turns.

    Stateful, single-pass over rounds in ingestion order (``round_pk`` == file order), reproducing
    the old single-pass loader exactly. State is ``last_model_output_by_session``. For each round in
    order:
      1. ``start`` = response-trigger user_message timestamp for the round (None if absent).
      2. If ``start`` is not None and the session_id is a non-empty str, and a previous model-output
         timestamp exists for that session, the wait is ``(start - prev).total_seconds()``; when
         strictly positive it is appended to ``"all"`` and to that round's provider bucket.
      3. Update ``last_model_output_by_session[session_id]`` with this round's last model-output
         timestamp (when present).
    The full list is kept per provider — no sampling — so the CDF is exact.
    """
    # Per-round timing events (epoch-microsecond ints, rebuilt to naive datetimes). Event order
    # within a round is irrelevant: the per-round helpers only use min/max over the typed lists.
    events_by_round: dict[int, list[tuple[str | None, datetime | None]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk"
    ).fetchall():
        events_by_round.setdefault(round_pk, []).append(
            (event_type, _epoch_us_to_datetime(ts_us))
        )

    by_provider: dict[str, list[float]] = {"all": []}
    last_model_output_by_session: dict[str, datetime] = {}
    # Per-round (session_id, provider) in ingestion order so the stateful walk and the per-provider
    # append order match the old line-by-line loader exactly.
    for round_pk, session_id, provider in con.execute(
        "SELECT round_pk, session_id, provider FROM rounds ORDER BY round_pk"
    ).fetchall():
        events = events_by_round.get(round_pk, [])
        provider_key = str(provider) if provider else "<unknown-provider>"

        start = _response_trigger_user_message_timestamp(events)
        if start is not None and isinstance(session_id, str) and session_id:
            prev = last_model_output_by_session.get(session_id)
            if prev is not None:
                wait_seconds = (start - prev).total_seconds()
                if wait_seconds > 0:
                    by_provider["all"].append(wait_seconds)
                    by_provider.setdefault(provider_key, []).append(wait_seconds)

        out = _last_model_output_timestamp(events)
        if isinstance(session_id, str) and session_id and out is not None:
            last_model_output_by_session[session_id] = out
    return by_provider


def ordered_human_wait_items(
    wait_seconds_by_provider: dict[str, list[float]],
) -> list[tuple[str, list[float]]]:
    items: list[tuple[str, list[float]]] = []
    all_values = wait_seconds_by_provider.get("all", [])
    if all_values:
        items.append(("all", all_values))
    providers = {
        provider: values
        for provider, values in wait_seconds_by_provider.items()
        if provider != "all" and values
    }
    for provider in provider_order(providers):
        items.append((provider, providers[provider]))
    return items


def human_wait_summary_row(label: str, values: list[float]) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    percentiles = sample_percentiles(values, [50, 90, 95, 99])
    return {
        "group": label,
        "count": int(arr.size),
        "mean_seconds": float(np.mean(arr)) if arr.size else None,
        "p50_seconds": percentiles["p50"],
        "p90_seconds": percentiles["p90"],
        "p95_seconds": percentiles["p95"],
        "p99_seconds": percentiles["p99"],
        "max_seconds": float(np.max(arr)) if arr.size else None,
    }


def plot_human_input_wait_cdf(
    wait_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> None:
    items = ordered_human_wait_items(wait_seconds_by_provider)
    if not items:
        return

    all_values = [value for _label, values in items for value in values if value > 0]
    if not all_values:
        return

    fig, ax = plt.subplots(figsize=(9.5, 6.0))
    ax.set_title("Human Input Wait Time CDF")
    ax.set_xlabel("Time from previous model output to next user message")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 100)
    ax.set_xscale("log")
    ax.yaxis.set_major_formatter(mticker.PercentFormatter())
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(format_duration_seconds_tick))
    ticks = [
        1,
        10,
        60,
        5 * 60,
        10 * 60,
        30 * 60,
        60 * 60,
        2 * 60 * 60,
        6 * 60 * 60,
        12 * 60 * 60,
        24 * 60 * 60,
        7 * 24 * 60 * 60,
    ]
    max_value = max(all_values)
    visible_ticks = [tick for tick in ticks if tick <= max_value * 1.05]
    if visible_ticks:
        ax.set_xticks(visible_ticks)
    polish_axes(ax, grid_axis="both")

    for index, (label, values) in enumerate(items):
        arr = np.sort(np.asarray([value for value in values if value > 0], dtype=float))
        if arr.size == 0:
            continue
        y = np.arange(1, arr.size + 1) / arr.size * 100
        summary = human_wait_summary_row(label, values)
        ax.plot(
            arr,
            y,
            linewidth=2.0,
            color=TEXT_COLOR if label == "all" else plot_color(label, index),
            label=(
                f"{provider_title(label) if label != 'all' else 'All'} "
                f"(n={summary['count']:,}, "
                f"p50={format_duration_seconds_tick(summary['p50_seconds'], 0)}, "
                f"p90={format_duration_seconds_tick(summary['p90_seconds'], 0)})"
            ),
        )

    ax.legend(fontsize=9)
    fig.tight_layout()
    out = output_dir / "human_input_wait_cdf.png"
    save_plot(fig, out)


def write_human_input_wait_summary(
    wait_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    rows = [
        human_wait_summary_row(label, values)
        for label, values in ordered_human_wait_items(wait_seconds_by_provider)
    ]
    path = output_dir / "human_input_wait_summary.csv"
    fieldnames = [
        "group",
        "count",
        "mean_seconds",
        "p50_seconds",
        "p90_seconds",
        "p95_seconds",
        "p99_seconds",
        "max_seconds",
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
    wait = load_human_input_wait_seconds_by_provider(con)

    plot_human_input_wait_cdf(wait, out)
    plot_count_cdf_by_provider(
        wait,
        out,
        out_name="human_input_wait_count_cdf_by_provider.png",
        title="Human Input Wait Count CDF by Provider",
        x_label="Wait from previous model output to next user message",
        table_title="human input wait time",
        edge_kind="duration_seconds",
        unit_label="wait",
        x_max=ONE_HOUR_SECONDS,
        x_max_label="1h",
    )
    plot_cumulative_duration_cdf_by_provider(
        wait,
        out,
        out_name="human_input_wait_total_cdf_by_provider.png",
        title="Human Input Wait Total CDF by Provider",
        x_label="Wait from previous model output to next user message",
        table_title="human input wait time",
        x_max=ONE_HOUR_SECONDS,
        x_max_label="1h",
        reference_seconds=CDF_REFERENCE_SECONDS,
        reference_label="5m",
    )
    write_count_cdf_by_provider(
        wait,
        out,
        out_name="human_input_wait_count_cdf_by_provider.csv",
        edge_kind="duration_seconds",
    )
    write_cumulative_duration_cdf_by_provider(
        wait,
        out,
        out_name="human_input_wait_total_cdf_by_provider.csv",
    )
    write_human_input_wait_summary(wait, out)

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
