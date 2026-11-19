#!/usr/bin/env python3
"""Per user-initiated turn end-to-end response time summary, by provider.

Response time requires a recorded request start and native completion. Legacy, open,
aborted, invalid, and compressed captures have no response duration.
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
from style import provider_order  # noqa: E402
from accumulators import sample_percentiles  # noqa: E402
import trace_db  # noqa: E402

# Same event-type partitions as the pre-DuckDB timing helper (artifacts/utils/timing.py).
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
# last_response_end_timestamp uses RESPONSE_END_EVENT_TYPES == MODEL_OUTPUT_EVENT_TYPES.
RESPONSE_END_EVENT_TYPES = MODEL_OUTPUT_EVENT_TYPES

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines. Rebuild the naive datetime here exactly
# (integer microseconds). The old path parsed ISO strings to tz-aware datetimes, but a difference
# between two same-tz datetimes equals the naive epoch_us difference to the microsecond, so the
# durations match the pre-DuckDB result bit-for-bit.
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


def _last_response_end_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    """Reproduce timing.last_response_end_timestamp: latest model-output timestamp, or None."""
    timestamps = [
        ts
        for event_type, ts in events
        if event_type in RESPONSE_END_EVENT_TYPES and ts is not None
    ]
    return max(timestamps) if timestamps else None


def load_user_turn_response_seconds_by_provider(con):
    """Native completed lifecycle durations only; legacy/open/aborted captures stay unavailable."""
    import request_timing as rt
    events = rt.load_events(con)
    tools = rt.load_tools(con)
    by_provider = {"all": []}
    for request in rt.collect_requests(con, events):
        duration = request.elapsed_seconds
        measured = rt.timing_summary(request.round_pks, events, tools)
        if duration is None or measured["compressed_timestamps"]:
            continue
        by_provider["all"].append(duration)
        by_provider.setdefault(request.provider, []).append(duration)
    return by_provider


def ordered_provider_duration_items(
    seconds_by_provider: dict[str, list[float]],
) -> list[tuple[str, list[float]]]:
    items: list[tuple[str, list[float]]] = []
    all_values = seconds_by_provider.get("all", [])
    if all_values:
        items.append(("all", all_values))
    providers = {
        provider: values
        for provider, values in seconds_by_provider.items()
        if provider != "all" and values
    }
    for provider in provider_order(providers):
        items.append((provider, providers[provider]))
    return items


def duration_summary_row(label: str, values: list[float]) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    percentiles = sample_percentiles(values, [25, 50, 90, 99])
    return {
        "group": label,
        "count": int(arr.size),
        "mean_seconds": float(np.mean(arr)) if arr.size else None,
        "p25_seconds": percentiles["p25"],
        "p50_seconds": percentiles["p50"],
        "p90_seconds": percentiles["p90"],
        "p99_seconds": percentiles["p99"],
        "max_seconds": float(np.max(arr)) if arr.size else None,
    }


def write_user_turn_response_time_summary(
    response_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    rows = [
        duration_summary_row(label, values)
        for label, values in ordered_provider_duration_items(
            response_seconds_by_provider
        )
    ]
    path = output_dir / "user_turn_response_time_summary.csv"
    fieldnames = [
        "group",
        "count",
        "mean_seconds",
        "p25_seconds",
        "p50_seconds",
        "p90_seconds",
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
    from request_timing import request_ownership_coverage, render_ownership_coverage
    try:
        response_seconds_by_provider = load_user_turn_response_seconds_by_provider(con)
        coverage = request_ownership_coverage(con)
    finally:
        con.close()
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    (Path(args.output_dir) / "request_ownership_coverage.md").write_text(
        render_ownership_coverage(coverage) + "\n", encoding="utf-8")
    write_user_turn_response_time_summary(
        response_seconds_by_provider, args.output_dir
    )
    print(f"All outputs saved to {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
