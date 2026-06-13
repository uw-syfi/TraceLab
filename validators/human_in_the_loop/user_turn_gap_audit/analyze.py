#!/usr/bin/env python3
"""Deep audit of unclassified elapsed time inside user-turn response windows."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]  # experiment -> category -> artifacts -> repo root
INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
OUT_MD = Path(__file__).with_name("result_analysis.md")

INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
USAGE_REPORT_EVENT_TYPES = {"usage_report"}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def events(row: dict[str, Any]) -> list[dict[str, Any]]:
    value = row.get("timing_events")
    return [event for event in value if isinstance(event, dict)] if isinstance(value, list) else []


def row_id(row: dict[str, Any], line_no: int) -> str:
    for key in ("round_id", "request_id", "message_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return f"line:{line_no}"


def response_trigger_user_message_timestamp(row: dict[str, Any]) -> datetime | None:
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events(row):
        event_type = event.get("event_type")
        timestamp = parse_ts(event.get("timestamp"))
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


def timestamps_for(row: dict[str, Any], event_types: set[str]) -> list[tuple[str, datetime]]:
    out: list[tuple[str, datetime]] = []
    for event in events(row):
        event_type = event.get("event_type")
        if event_type not in event_types:
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is not None:
            out.append((str(event_type), timestamp))
    return out


def model_output_timestamps(row: dict[str, Any]) -> list[tuple[str, datetime]]:
    return timestamps_for(row, MODEL_OUTPUT_EVENT_TYPES)


def last_model_output_timestamp(row: dict[str, Any]) -> tuple[str, datetime] | None:
    values = model_output_timestamps(row)
    return max(values, key=lambda item: item[1]) if values else None


def usage_report_timestamp(row: dict[str, Any]) -> datetime | None:
    values = timestamps_for(row, USAGE_REPORT_EVENT_TYPES)
    return max((timestamp for _kind, timestamp in values), default=None)


def generation_interval(row: dict[str, Any]) -> tuple[datetime, datetime] | None:
    input_timestamps = [timestamp for _kind, timestamp in timestamps_for(row, INPUT_EVENT_TYPES)]
    output_timestamps = [timestamp for _kind, timestamp in model_output_timestamps(row)]
    if not input_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_inputs = [
        timestamp for timestamp in input_timestamps if timestamp <= first_output_at
    ]
    if not candidate_inputs:
        return None
    start = max(candidate_inputs)
    end = max(output_timestamps)
    if end <= start:
        return None
    return start, end


def tool_wall_interval(tool: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = parse_ts(tool.get("emitted_at"))
    end = parse_ts(tool.get("result_at"))
    if start is None or end is None or end <= start:
        return None
    return start, end


def tool_latency_seconds(tool: dict[str, Any], key: str) -> float | None:
    value = safe_float(tool.get(key))
    return value / 1000 if value is not None and value > 0 else None


@dataclass
class Interval:
    start: datetime
    end: datetime
    kind: str
    label: str

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class TimelineEvent:
    timestamp: datetime
    kind: str
    label: str


@dataclass
class Turn:
    provider: str
    session_id: str
    start_at: datetime
    start_line: int
    start_row: str
    end_at: datetime | None = None
    end_kind: str | None = None
    end_line: int | None = None
    generation_intervals: list[Interval] = field(default_factory=list)
    tool_intervals: list[Interval] = field(default_factory=list)
    events: list[TimelineEvent] = field(default_factory=list)
    rows: int = 0
    tool_calls: int = 0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0
    usage_report_after_output_seconds: float = 0.0


@dataclass
class TurnResult:
    provider: str
    session_id: str
    start_at: datetime
    end_at: datetime
    e2e_seconds: float
    generation_seconds: float
    tool_wall_seconds: float
    tool_effective_seconds: float
    union_covered_seconds: float
    uncovered_seconds: float
    overlap_seconds: float
    residual_wall_seconds: float
    residual_effective_seconds: float
    rows: int
    tool_calls: int
    largest_gaps: list[tuple[float, str, str, datetime, datetime]]


def clip_interval(interval: Interval, start: datetime, end: datetime) -> Interval | None:
    clipped_start = max(interval.start, start)
    clipped_end = min(interval.end, end)
    if clipped_end <= clipped_start:
        return None
    return Interval(clipped_start, clipped_end, interval.kind, interval.label)


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: (item.start, item.end))
    merged = [ordered[0]]
    for interval in ordered[1:]:
        current = merged[-1]
        if interval.start <= current.end:
            if interval.end > current.end:
                current.end = interval.end
                current.label = f"{current.label}+{interval.label}"
            continue
        merged.append(Interval(interval.start, interval.end, "covered", interval.label))
    return merged


def nearest_labels(
    turn: Turn,
    gap_start: datetime,
    gap_end: datetime,
) -> tuple[str, str]:
    ordered = sorted(turn.events, key=lambda item: item.timestamp)
    before = "start"
    after = "end"
    for event in ordered:
        if event.timestamp <= gap_start:
            before = event.label
        if event.timestamp >= gap_end:
            after = event.label
            break
    return before, after


def analyze_turn(turn: Turn) -> TurnResult | None:
    if turn.end_at is None:
        return None
    e2e_seconds = (turn.end_at - turn.start_at).total_seconds()
    if e2e_seconds <= 0:
        return None
    raw_intervals = [
        *(clip_interval(interval, turn.start_at, turn.end_at) for interval in turn.generation_intervals),
        *(clip_interval(interval, turn.start_at, turn.end_at) for interval in turn.tool_intervals),
    ]
    intervals = [interval for interval in raw_intervals if interval is not None]
    generation_seconds = sum(
        interval.seconds
        for interval in intervals
        if interval.kind == "generation"
    )
    tool_wall_interval_seconds = sum(
        interval.seconds
        for interval in intervals
        if interval.kind == "tool_wall"
    )
    merged = merge_intervals(intervals)
    union_covered_seconds = sum(interval.seconds for interval in merged)
    overlap_seconds = max(0.0, generation_seconds + tool_wall_interval_seconds - union_covered_seconds)
    gaps: list[tuple[float, str, str, datetime, datetime]] = []
    cursor = turn.start_at
    for interval in merged:
        if interval.start > cursor:
            before, after = nearest_labels(turn, cursor, interval.start)
            gaps.append(((interval.start - cursor).total_seconds(), before, after, cursor, interval.start))
        cursor = max(cursor, interval.end)
    if turn.end_at > cursor:
        before, after = nearest_labels(turn, cursor, turn.end_at)
        gaps.append(((turn.end_at - cursor).total_seconds(), before, after, cursor, turn.end_at))
    gaps.sort(reverse=True, key=lambda item: item[0])
    uncovered_seconds = sum(item[0] for item in gaps)
    residual_wall_seconds = e2e_seconds - generation_seconds - tool_wall_interval_seconds
    residual_effective_seconds = e2e_seconds - generation_seconds - turn.tool_effective_seconds
    return TurnResult(
        provider=turn.provider,
        session_id=turn.session_id,
        start_at=turn.start_at,
        end_at=turn.end_at,
        e2e_seconds=e2e_seconds,
        generation_seconds=generation_seconds,
        tool_wall_seconds=tool_wall_interval_seconds,
        tool_effective_seconds=turn.tool_effective_seconds,
        union_covered_seconds=union_covered_seconds,
        uncovered_seconds=uncovered_seconds,
        overlap_seconds=overlap_seconds,
        residual_wall_seconds=residual_wall_seconds,
        residual_effective_seconds=residual_effective_seconds,
        rows=turn.rows,
        tool_calls=turn.tool_calls,
        largest_gaps=gaps[:5],
    )


def hours(seconds: float) -> float:
    return seconds / 3600


def fmt_h(seconds: float) -> str:
    return f"{hours(seconds):,.1f}h"


def fmt_dur(seconds: float) -> str:
    if abs(seconds) < 60:
        return f"{seconds:.1f}s"
    if abs(seconds) < 3600:
        return f"{seconds / 60:.1f}m"
    if abs(seconds) < 86400:
        return f"{seconds / 3600:.2f}h"
    return f"{seconds / 86400:.2f}d"


def main() -> int:
    active_by_session: dict[str, Turn] = {}
    results: list[TurnResult] = []
    dropped = Counter()
    usage_gap_by_provider = Counter()
    usage_gap_turns_by_provider = Counter()
    gap_pattern_seconds: defaultdict[tuple[str, str, str], float] = defaultdict(float)
    gap_pattern_counts: Counter[tuple[str, str, str]] = Counter()

    def close_turn(session_id: str) -> None:
        turn = active_by_session.pop(session_id, None)
        if turn is None:
            return
        result = analyze_turn(turn)
        if result is None:
            if turn.end_at is None:
                dropped[(turn.provider, "no_end")] += 1
            else:
                dropped[(turn.provider, "nonpositive")] += 1
            return
        results.append(result)
        usage_gap_by_provider[turn.provider] += turn.usage_report_after_output_seconds
        if turn.usage_report_after_output_seconds > 0:
            usage_gap_turns_by_provider[turn.provider] += 1
        for seconds, before, after, _start, _end in result.largest_gaps:
            before_kind = before.split(":", 1)[0]
            after_kind = after.split(":", 1)[0]
            key = (turn.provider, before_kind, after_kind)
            gap_pattern_seconds[key] += seconds
            gap_pattern_counts[key] += 1

    with INPUT.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            provider = str(row.get("provider") or "<unknown-provider>")
            session_id_value = row.get("session_id")
            session_id = session_id_value if isinstance(session_id_value, str) else None
            rid = row_id(row, line_no)

            user_start_at = response_trigger_user_message_timestamp(row)
            if user_start_at is not None and session_id is not None:
                close_turn(session_id)
                active_by_session[session_id] = Turn(
                    provider=provider,
                    session_id=session_id,
                    start_at=user_start_at,
                    start_line=line_no,
                    start_row=rid,
                    events=[
                        TimelineEvent(
                            user_start_at,
                            "user_message",
                            f"user_message:line{line_no}",
                        )
                    ],
                )

            turn = active_by_session.get(session_id) if session_id is not None else None
            if turn is None:
                continue
            turn.rows += 1

            for event in events(row):
                event_type = event.get("event_type")
                timestamp = parse_ts(event.get("timestamp"))
                if isinstance(event_type, str) and timestamp is not None:
                    turn.events.append(
                        TimelineEvent(timestamp, event_type, f"{event_type}:line{line_no}")
                    )

            gen = generation_interval(row)
            if gen is not None:
                start, end = gen
                turn.generation_intervals.append(
                    Interval(start, end, "generation", f"generation:line{line_no}")
                )
            last_output = last_model_output_timestamp(row)
            if last_output is not None:
                end_kind, end_at = last_output
                if turn.end_at is None or end_at > turn.end_at:
                    turn.end_at = end_at
                    turn.end_kind = end_kind
                    turn.end_line = line_no
            usage_at = usage_report_timestamp(row)
            if usage_at is not None and last_output is not None:
                _kind, output_at = last_output
                if usage_at > output_at:
                    turn.usage_report_after_output_seconds += (
                        usage_at - output_at
                    ).total_seconds()

            tools = row.get("tools")
            if isinstance(tools, list):
                for index, tool in enumerate(tools):
                    if not isinstance(tool, dict):
                        continue
                    turn.tool_calls += 1
                    effective = tool_latency_seconds(tool, "tool_internal_latency_ms")
                    if effective is None:
                        effective = tool_latency_seconds(tool, "tool_wall_latency_ms")
                    if effective is None:
                        effective = tool_latency_seconds(tool, "latency_ms")
                    if effective is not None:
                        turn.tool_effective_seconds += effective
                    interval = tool_wall_interval(tool)
                    if interval is not None:
                        start, end = interval
                        turn.tool_wall_seconds += (end - start).total_seconds()
                        name = str(tool.get("tool_name") or "tool")
                        turn.tool_intervals.append(
                            Interval(start, end, "tool_wall", f"tool:{name}:line{line_no}:{index}")
                        )
                        turn.events.append(TimelineEvent(start, "tool_emit", f"tool_emit:{name}:line{line_no}"))
                        turn.events.append(TimelineEvent(end, "tool_result", f"tool_result:{name}:line{line_no}"))

    for session_id in list(active_by_session):
        close_turn(session_id)

    providers = ["all", "claude", "codex"]
    by_provider: dict[str, list[TurnResult]] = {
        provider: [
            result for result in results if provider == "all" or result.provider == provider
        ]
        for provider in providers
    }

    lines = [
        "# Deep User Turn Gap Audit",
        "",
        f"Input: `{INPUT}`",
        "",
        "Turn window: response-triggering `user_message` to final model output (`reasoning`, `text`, or `tool_call`) before the next same-session response-triggering `user_message`.",
        "",
        "Coverage intervals are row-level generation intervals plus tool wall intervals, clipped to the turn window and merged before computing uncovered wall-clock gaps.",
        "",
        "## Provider Totals",
        "",
        "| provider | turns | e2e | generation | tool wall sum | covered union | uncovered wall gaps | overlap | residual wall | residual effective |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in providers:
        items = by_provider[provider]
        totals = {
            "e2e": sum(item.e2e_seconds for item in items),
            "generation": sum(item.generation_seconds for item in items),
            "tool_wall": sum(item.tool_wall_seconds for item in items),
            "covered": sum(item.union_covered_seconds for item in items),
            "uncovered": sum(item.uncovered_seconds for item in items),
            "overlap": sum(item.overlap_seconds for item in items),
            "residual_wall": sum(item.residual_wall_seconds for item in items),
            "residual_effective": sum(item.residual_effective_seconds for item in items),
        }
        lines.append(
            f"| {provider} | {len(items):,} | {fmt_h(totals['e2e'])} | "
            f"{fmt_h(totals['generation'])} | {fmt_h(totals['tool_wall'])} | "
            f"{fmt_h(totals['covered'])} | {fmt_h(totals['uncovered'])} | "
            f"{fmt_h(totals['overlap'])} | {fmt_h(totals['residual_wall'])} | "
            f"{fmt_h(totals['residual_effective'])} |"
        )

    lines.extend(
        [
            "",
            "Identity check: `residual wall = uncovered wall gaps - overlap`. Large residual means large unmerged uncovered gaps, not only overlap/double-counting.",
            "",
            "## Usage Report Gap Diagnostic",
            "",
            "| provider | usage-report-after-output | turns with usage gap |",
            "|---|---:|---:|",
        ]
    )
    for provider in providers:
        if provider == "all":
            seconds = sum(usage_gap_by_provider.values())
            count = sum(usage_gap_turns_by_provider.values())
        else:
            seconds = usage_gap_by_provider[provider]
            count = usage_gap_turns_by_provider[provider]
        lines.append(f"| {provider} | {fmt_h(seconds)} | {count:,} |")

    lines.extend(
        [
            "",
            "## Largest Uncovered Gap Patterns",
            "",
            "This groups only each turn's five largest uncovered gaps, so totals here are diagnostic rather than exhaustive.",
            "",
            "| provider | before kind | after kind | count | gap total |",
            "|---|---|---|---:|---:|",
        ]
    )
    pattern_rows = sorted(
        gap_pattern_seconds.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:20]
    for (provider, before, after), seconds in pattern_rows:
        lines.append(
            f"| {provider} | {before} | {after} | "
            f"{gap_pattern_counts[(provider, before, after)]:,} | {fmt_h(seconds)} |"
        )

    lines.extend(
        [
            "",
            "## Top Residual Turns",
            "",
            "| provider | residual wall | uncovered | overlap | e2e | gen | tool wall | rows | tools | session | start | top gaps |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
        ]
    )
    for item in sorted(results, key=lambda result: result.residual_wall_seconds, reverse=True)[:25]:
        gap_text = "; ".join(
            f"{fmt_dur(seconds)} {before}->{after}"
            for seconds, before, after, _start, _end in item.largest_gaps[:3]
        )
        lines.append(
            f"| {item.provider} | {fmt_dur(item.residual_wall_seconds)} | "
            f"{fmt_dur(item.uncovered_seconds)} | {fmt_dur(item.overlap_seconds)} | "
            f"{fmt_dur(item.e2e_seconds)} | {fmt_dur(item.generation_seconds)} | "
            f"{fmt_dur(item.tool_wall_seconds)} | {item.rows:,} | {item.tool_calls:,} | "
            f"`{item.session_id}` | {item.start_at.isoformat()} | {gap_text} |"
        )

    lines.extend(
        [
            "",
            "## Dropped Turns",
            "",
            "| provider | reason | count |",
            "|---|---|---:|",
        ]
    )
    for (provider, reason), count in sorted(dropped.items()):
        lines.append(f"| {provider} | {reason} | {count:,} |")

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
