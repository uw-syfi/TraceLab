#!/usr/bin/env python3
"""Check whether low prefix-cache hit rounds follow long waits or tool durations.

Reads the shared trace DuckDB (``rounds`` / ``tool_calls`` / ``timing_events``) instead of
re-parsing the normalized JSONL. The gap/hit relationship is stateful per session, so the
session walk is reproduced in Python exactly as the pre-DuckDB loader did; only the input
mechanism (JSONL -> DB) changed. All metrics, grouping and CSV output are unchanged.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402


DEFAULT_OUTPUT_DIR = SCRIPT_DIR

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines (DB_SCHEMA gotcha). Rebuild the naive
# datetime here exactly. The old path parsed ISO strings to tz-aware datetimes, but a difference
# between two same-tz datetimes equals the naive epoch_us difference to the microsecond, so every
# measured gap matches the pre-DuckDB result bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


@dataclass
class RoundData:
    """The per-round fields the session walk needs, in file order within a session."""

    round_index: int
    prefix_tokens: int | None
    append_tokens: int | None
    first_event_type: str | None
    first_activity_us: int | None
    last_activity_us: int | None
    # Ordered leading tool_result timing-event call ids (until the first non-tool_result event).
    leading_tool_result_call_ids: list[str] = field(default_factory=list)
    # Every tool this round emits: tool_call_id -> duration seconds (result_at - emitted_at) when
    # both times exist and the gap is >= 0, else None. Stored for *all* emitted tools (mirroring the
    # old remember_emitted_tools that kept every tool dict); the None entries reproduce the old
    # overwrite-on-re-emit behavior, and the tool_result lookup skips them.
    emitted_tool_durations: dict[str, float | None] = field(default_factory=dict)


@dataclass
class GapGroup:
    rounds: int = 0
    low_hit_rounds: int = 0
    all_with_gap: int = 0
    all_gt_idle: int = 0
    nonlow_with_gap: int = 0
    nonlow_gt_idle: int = 0
    low_with_gap: int = 0
    low_gt_idle: int = 0
    all_gaps: list[float] = field(default_factory=list)
    low_gaps: list[float] = field(default_factory=list)


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


def format_pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return ""
    return f"{numerator / denominator * 100:.2f}%"


def format_float(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6g}"


def load_rounds_by_session(con) -> dict[tuple[str, str], list[RoundData]]:
    """Build ``{(provider, session_id): [RoundData, ...]}`` from the trace DB.

    Reproduces the old ``read_rows_by_session`` exactly:
      * keep only rounds with string ``provider`` / ``session_id`` and an int ``round_index``;
      * group by ``(provider, session_id)``;
      * sort each session by ``(round_index, first_activity_ts)`` — ``first_activity_ts`` being the
        first timing event's timestamp (or, when no timing event carries a timestamp, the earliest
        activity timestamp across timing events + tool emitted/result times). Epoch-microsecond
        integers preserve that instant ordering, so the line-order tie-break is reproduced.
    """
    # Per-round timing events in event_index order: first/ordered event recovery, the activity span,
    # and the leading tool_result call ids all derive from this ordered list.
    timing_by_round: dict[int, list[tuple[str | None, int | None, str | None]]] = defaultdict(list)
    for round_pk, event_type, ts_us, tool_call_id in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us, tool_call_id "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        timing_by_round[round_pk].append((event_type, ts_us, tool_call_id))

    # Per-round tools in tool_index order: emitted/result times feed the activity span and, keyed by
    # call id, the tool durations a later tool_result round looks up.
    tools_by_round: dict[int, list[tuple[str | None, int | None, int | None]]] = defaultdict(list)
    for round_pk, tool_call_id, emitted_us, result_us in con.execute(
        "SELECT round_pk, tool_call_id, "
        "       CAST(epoch_us(emitted_at) AS BIGINT) AS emitted_us, "
        "       CAST(epoch_us(result_at) AS BIGINT) AS result_us "
        "FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        tools_by_round[round_pk].append((tool_call_id, emitted_us, result_us))

    sessions: dict[tuple[str, str], list[tuple[int, int, RoundData]]] = defaultdict(list)
    for round_pk, provider, session_id, round_index, prefix_tokens, append_tokens in con.execute(
        "SELECT round_pk, provider, session_id, round_index, prefix_tokens, newly_append_tokens "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        if not isinstance(provider, str) or not isinstance(session_id, str):
            continue
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            continue

        events = timing_by_round.get(round_pk, [])
        tools = tools_by_round.get(round_pk, [])

        # first_timing_event_type: the first timing event's event_type (event_index = 1).
        first_event_type = events[0][0] if events else None

        # Activity timestamps: every timing-event timestamp + every tool emitted/result time.
        activity_us: list[int] = [ts for _etype, ts, _cid in events if ts is not None]
        for _cid, emitted_us, result_us in tools:
            if emitted_us is not None:
                activity_us.append(emitted_us)
            if result_us is not None:
                activity_us.append(result_us)

        # first_activity_ts: first timing event carrying a timestamp; else earliest activity ts.
        first_activity_us: int | None = None
        for _etype, ts, _cid in events:
            if ts is not None:
                first_activity_us = ts
                break
        if first_activity_us is None:
            first_activity_us = min(activity_us) if activity_us else None

        last_activity_us = max(activity_us) if activity_us else None

        # Leading tool_result call ids: the run of tool_result events at the head of the round
        # (until the first non-tool_result event), keeping non-empty string ids.
        leading_call_ids: list[str] = []
        for etype, _ts, cid in events:
            if etype != "tool_result":
                break
            if isinstance(cid, str) and cid:
                leading_call_ids.append(cid)

        # Every emitted tool keyed by call id (mirroring remember_emitted_tools storing every tool):
        # duration seconds when both times exist and the gap is >= 0, else None.
        emitted_durations: dict[str, float | None] = {}
        for cid, emitted_us, result_us in tools:
            if not isinstance(cid, str) or not cid:
                continue
            if emitted_us is None or result_us is None:
                emitted_durations[cid] = None
                continue
            duration = (result_us - emitted_us) / 1e6
            emitted_durations[cid] = duration if duration >= 0 else None

        data = RoundData(
            round_index=round_index,
            prefix_tokens=prefix_tokens,
            append_tokens=append_tokens,
            first_event_type=first_event_type if isinstance(first_event_type, str) else None,
            first_activity_us=first_activity_us,
            last_activity_us=last_activity_us,
            leading_tool_result_call_ids=leading_call_ids,
            emitted_tool_durations=emitted_durations,
        )
        sessions[(provider, session_id)].append((round_index, round_pk, data))

    # Sort each session by (round_index, first_activity instant), matching the old
    # sortable_activity_ts(row) = first_activity_ts().timestamp() or float("-inf"). Epoch-microsecond
    # integers preserve that instant ordering. round_pk is carried only for clarity, not as a key.
    result: dict[tuple[str, str], list[RoundData]] = {}
    for key, triples in sessions.items():
        triples.sort(
            key=lambda triple: (
                triple[2].round_index,
                triple[2].first_activity_us
                if triple[2].first_activity_us is not None
                else float("-inf"),
            )
        )
        result[key] = [data for _ri, _pk, data in triples]
    return result


def update_group(
    group: GapGroup,
    *,
    hit_ratio: float,
    gap_seconds: float | None,
    low_hit_ratio: float,
    idle_seconds: float,
) -> None:
    group.rounds += 1
    is_low = hit_ratio < low_hit_ratio
    if is_low:
        group.low_hit_rounds += 1

    if gap_seconds is None:
        return
    group.all_with_gap += 1
    group.all_gaps.append(gap_seconds)
    if gap_seconds > idle_seconds:
        group.all_gt_idle += 1

    if is_low:
        group.low_with_gap += 1
        group.low_gaps.append(gap_seconds)
        if gap_seconds > idle_seconds:
            group.low_gt_idle += 1
    else:
        group.nonlow_with_gap += 1
        if gap_seconds > idle_seconds:
            group.nonlow_gt_idle += 1


def analyze(
    con,
    *,
    low_hit_ratio: float,
    idle_seconds: float,
) -> dict[tuple[str, str], GapGroup]:
    groups: dict[tuple[str, str], GapGroup] = defaultdict(GapGroup)
    rounds_by_session = load_rounds_by_session(con)
    for (provider, _session_id), rounds in rounds_by_session.items():
        # Session-scoped accumulator of emitted tool durations by call id (call ids are unique
        # within a session). Tools are "remembered" only after a round is processed, so a
        # tool_result round looks up durations emitted by *previous* rounds — reproduced below.
        durations_by_call_id: dict[str, float] = {}
        for row_offset, current in enumerate(rounds):
            event_type = current.first_event_type
            if event_type not in ("user_message", "tool_result"):
                durations_by_call_id.update(current.emitted_tool_durations)
                continue
            prefix_tokens = current.prefix_tokens
            append_tokens = current.append_tokens
            if prefix_tokens is None or append_tokens is None:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue
            total_tokens = prefix_tokens + append_tokens
            if total_tokens <= 0:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue

            trigger = "user" if event_type == "user_message" else "tool_result"
            hit_ratio = prefix_tokens / total_tokens
            measured_seconds = None
            if trigger == "user" and row_offset > 0:
                previous_last = rounds[row_offset - 1].last_activity_us
                current_first = current.first_activity_us
                if previous_last is not None and current_first is not None:
                    delta = (current_first - previous_last) / 1e6
                    if delta >= 0:
                        measured_seconds = delta
            elif trigger == "tool_result":
                durations = [
                    duration
                    for tool_call_id in current.leading_tool_result_call_ids
                    if (duration := durations_by_call_id.get(tool_call_id)) is not None
                ]
                if durations:
                    measured_seconds = max(durations)

            for scope in ("merged", provider):
                update_group(
                    groups[(scope, "all")],
                    hit_ratio=hit_ratio,
                    gap_seconds=measured_seconds,
                    low_hit_ratio=low_hit_ratio,
                    idle_seconds=idle_seconds,
                )
                update_group(
                    groups[(scope, trigger)],
                    hit_ratio=hit_ratio,
                    gap_seconds=measured_seconds,
                    low_hit_ratio=low_hit_ratio,
                    idle_seconds=idle_seconds,
                )
            durations_by_call_id.update(current.emitted_tool_durations)
    return dict(groups)


def write_summary(
    groups: dict[tuple[str, str], GapGroup],
    output_path: Path,
    *,
    low_hit_ratio: float,
    idle_seconds: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str | int | float]] = []
    time_measures = {
        "all": "mixed_user_idle_wait_or_tool_duration",
        "user": "user_idle_wait_since_previous_activity",
        "tool_result": "tool_duration_result_at_minus_emitted_at",
    }
    for scope in ("merged", "claude", "codex"):
        for trigger in ("all", "user", "tool_result"):
            group = groups.get((scope, trigger), GapGroup())
            rows.append(
                {
                    "scope": scope,
                    "trigger": trigger,
                    "time_measure": time_measures[trigger],
                    "rounds": group.rounds,
                    "low_hit_threshold": low_hit_ratio,
                    "idle_threshold_seconds": idle_seconds,
                    "low_hit_rounds": group.low_hit_rounds,
                    "low_with_gap": group.low_with_gap,
                    "low_gt_idle": group.low_gt_idle,
                    "low_gt_idle_share": format_pct(group.low_gt_idle, group.low_with_gap),
                    "all_with_gap": group.all_with_gap,
                    "all_gt_idle_share": format_pct(group.all_gt_idle, group.all_with_gap),
                    "nonlow_gt_idle_share": format_pct(
                        group.nonlow_gt_idle, group.nonlow_with_gap
                    ),
                    "low_gap_median_s": format_float(percentile(group.low_gaps, 0.50)),
                    "low_gap_p90_s": format_float(percentile(group.low_gaps, 0.90)),
                    "all_gap_median_s": format_float(percentile(group.all_gaps, 0.50)),
                    "all_gap_p90_s": format_float(percentile(group.all_gaps, 0.90)),
                }
            )
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--low-hit-ratio", type=float, default=0.10)
    parser.add_argument("--idle-seconds", type=float, default=300.0)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    groups = analyze(
        con,
        low_hit_ratio=args.low_hit_ratio,
        idle_seconds=args.idle_seconds,
    )
    output_path = args.output_dir / "cache_hit_idle_gap_summary.csv"
    write_summary(
        groups,
        output_path,
        low_hit_ratio=args.low_hit_ratio,
        idle_seconds=args.idle_seconds,
    )
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
