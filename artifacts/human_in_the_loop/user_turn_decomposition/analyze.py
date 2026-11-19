#!/usr/bin/env python3
"""Audit shared request lifecycle and timing coverage.

Native completion determines response duration. Canonical attributed tool work is
not a wall-clock partition. Small per-round loading helpers serve count consumers."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402
from timing import effective_tool_seconds

OUT_MD = EXP_DIR / "result_analysis.md"

INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
RESPONSE_END_EVENT_TYPES = MODEL_OUTPUT_EVENT_TYPES
USAGE_REPORT_EVENT_TYPES = {"usage_report"}

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines. We rebuild the naive datetime here
# exactly (integer microseconds). The old path parsed ISO strings to tz-aware datetimes, but a
# difference between two same-tz datetimes equals the naive epoch_us difference to the microsecond,
# so every span matches the pre-DuckDB result bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


# --- Per-round timing helpers (reproduce artifacts/utils/timing.py over DB timing_events) ---------
#
# Each takes the round's events as a list of (event_type, timestamp) tuples. The pre-DuckDB code
# dropped events whose timestamp failed to parse; here the timestamp is already null when unparseable
# (TRY_CAST in materialize()), so the `ts is None` guards reproduce that filtering exactly.


def response_trigger_user_message_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event_type, timestamp in events:
        if timestamp is None:
            continue
        if event_type == "user_message":
            user_timestamps.append(timestamp)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(timestamp)
    if not user_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_users = [
        timestamp for timestamp in user_timestamps if timestamp <= first_output_at
    ]
    return max(candidate_users) if candidate_users else None


def timestamps_for(
    events: list[tuple[str | None, datetime | None]], event_types: set[str]
) -> list[datetime]:
    out: list[datetime] = []
    for event_type, timestamp in events:
        if event_type not in event_types:
            continue
        if timestamp is not None:
            out.append(timestamp)
    return out


def last_model_output_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    values = timestamps_for(events, MODEL_OUTPUT_EVENT_TYPES)
    return max(values) if values else None


def last_response_end_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    values = timestamps_for(events, RESPONSE_END_EVENT_TYPES)
    return max(values) if values else None


def input_to_last_output_span_seconds(
    events: list[tuple[str | None, datetime | None]],
) -> float | None:
    input_timestamps = timestamps_for(events, INPUT_EVENT_TYPES)
    output_timestamps = timestamps_for(events, MODEL_OUTPUT_EVENT_TYPES)
    if not input_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_inputs = [
        timestamp for timestamp in input_timestamps if timestamp <= first_output_at
    ]
    if not candidate_inputs:
        return None
    duration = (max(output_timestamps) - max(candidate_inputs)).total_seconds()
    return duration if duration > 0 else None


@dataclass
class RoundTools:
    """Per-round tool aggregates, computed from the DB ``tool_calls`` rows the same way the old loop
    aggregated each round's ``tools`` list: count every tool, sum only positive effective/wall."""

    tool_calls: int = 0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0


def load_timing_events(
    con: "duckdb.DuckDBPyConnection",
) -> dict[int, list[tuple[str | None, datetime | None]]]:
    """Per-round ``(event_type, timestamp)`` lists in event order (``event_index``).

    Event order within a round does not affect the per-round helpers (they use min/max/<=), but we
    order by ``event_index`` anyway to mirror the JSONL list order deterministically. Timestamps come
    back as epoch-microsecond ints (native/wasm-identical), rebuilt to naive datetimes here.
    """
    events_by_round: dict[int, list[tuple[str | None, datetime | None]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        events_by_round.setdefault(round_pk, []).append(
            (event_type, _epoch_us_to_datetime(ts_us))
        )
    return events_by_round


def load_round_tools(con: "duckdb.DuckDBPyConnection") -> dict[int, RoundTools]:
    """Per-round tool aggregates from ``tool_calls``, matching the old per-``tools``-list reduction.

    The old loop counted *every* tool dict and summed effective/wall latency only when present and
    strictly positive. Effective latency precedence is internal-else-wall (legacy ``latency_ms`` is
    not in the normalized schema, so its fallback branch was already dead). We aggregate per round in
    Python from the typed tool rows so the per-turn sums match the JSONL path exactly.
    """
    aggregates: dict[int, RoundTools] = {}
    for round_pk, internal_ms, wall_ms in con.execute(
        "SELECT round_pk, tool_internal_latency_ms, tool_wall_latency_ms "
        "FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        agg = aggregates.setdefault(round_pk, RoundTools())
        agg.tool_calls += 1
        # Effective tool latency: internal if present, else wall (seconds).
        effective = effective_tool_seconds(internal_ms, wall_ms)
        if effective is not None and effective > 0:
            agg.tool_effective_seconds += effective
        if wall_ms is not None:
            wall = wall_ms / 1000
            if wall > 0:
                agg.tool_wall_seconds += wall
    return aggregates


def main() -> int:
    import request_timing as rt
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=Path(__file__).resolve().parent)
    args = parser.parse_args()
    con = trace_db.open_from_args(args)
    try:
        report = rt.render_request_audit(con)
    finally:
        con.close()
    output = Path(args.output_dir) / "result_analysis.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
