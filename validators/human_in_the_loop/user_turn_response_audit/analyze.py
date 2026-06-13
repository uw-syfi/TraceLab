#!/usr/bin/env python3
"""Audit user-message-triggered rows that do not become response-time samples."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]  # experiment -> category -> artifacts -> repo root
INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
OUT_MD = Path(__file__).with_name("result_analysis.md")

MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_types(row: dict[str, Any]) -> list[str]:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return []
    out = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if isinstance(event_type, str):
            out.append(event_type)
    return out


def response_trigger_user_message_timestamp(row: dict[str, Any]) -> datetime | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            continue
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


def visible_user_nontrigger_reason(row: dict[str, Any]) -> str | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return "no_timing_events"
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    saw_user = False
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if event_type == "user_message":
            saw_user = True
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is None:
            continue
        if event_type == "user_message":
            user_timestamps.append(timestamp)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(timestamp)
    if not saw_user:
        return None
    if not user_timestamps:
        return "no_parseable_user_timestamp"
    if not output_timestamps:
        return "no_model_output"
    first_output_at = min(output_timestamps)
    if not any(timestamp <= first_output_at for timestamp in user_timestamps):
        return "user_after_first_model_output"
    return None


def last_model_output_timestamp(row: dict[str, Any]) -> datetime | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None
    timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") not in MODEL_OUTPUT_EVENT_TYPES:
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is not None:
            timestamps.append(timestamp)
    return max(timestamps) if timestamps else None


@dataclass
class Turn:
    session_id: str
    provider: str
    start_at: datetime
    line_no: int
    row_id: str
    event_types: list[str]
    last_output_at: datetime | None = None
    output_line_no: int | None = None


def row_identifier(row: dict[str, Any], line_no: int) -> str:
    for key in ("round_id", "request_id", "message_id", "id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return f"line:{line_no}"


def format_sample(turn: Turn, reason: str) -> str:
    return (
        f"- reason={reason} provider={turn.provider} session={turn.session_id} "
        f"line={turn.line_no} row={turn.row_id} "
        f"start={turn.start_at.isoformat()} events={','.join(turn.event_types)}"
    )


def main() -> int:
    user_triggered = Counter()
    response_samples = Counter()
    dropped_no_output = Counter()
    dropped_nonpositive = Counter()
    closed_by_next_user = Counter()
    closed_by_eof = Counter()
    visible_user_any = Counter()
    visible_user_not_trigger = Counter()
    first_event_counts = Counter()
    dropped_samples: list[str] = []

    active_by_session: dict[str, Turn] = {}

    def close_turn(session_id: str, reason: str) -> None:
        turn = active_by_session.pop(session_id, None)
        if turn is None:
            return
        if turn.last_output_at is None:
            dropped_no_output[turn.provider] += 1
            if len(dropped_samples) < 20:
                dropped_samples.append(format_sample(turn, reason))
            return
        duration = (turn.last_output_at - turn.start_at).total_seconds()
        if duration <= 0:
            dropped_nonpositive[turn.provider] += 1
            if len(dropped_samples) < 20:
                dropped_samples.append(format_sample(turn, f"{reason}:nonpositive"))
            return
        response_samples[turn.provider] += 1
        if reason == "next_user_message":
            closed_by_next_user[turn.provider] += 1
        elif reason == "eof":
            closed_by_eof[turn.provider] += 1

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
            types = event_types(row)
            user_start_at = response_trigger_user_message_timestamp(row)
            if "user_message" in types:
                visible_user_any[provider] += 1
                nontrigger_reason = visible_user_nontrigger_reason(row)
                if nontrigger_reason is not None:
                    visible_user_not_trigger[(provider, nontrigger_reason)] += 1
                elif session_id is None:
                    visible_user_not_trigger[(provider, "missing_session")] += 1
            if types:
                first_event_counts[(provider, types[0])] += 1

            if user_start_at is not None and session_id is not None:
                close_turn(session_id, "next_user_message")
                user_triggered[provider] += 1
                active_by_session[session_id] = Turn(
                    session_id=session_id,
                    provider=provider,
                    start_at=user_start_at,
                    line_no=line_no,
                    row_id=row_identifier(row, line_no),
                    event_types=types,
                )

            output_at = last_model_output_timestamp(row)
            if output_at is not None and session_id is not None:
                turn = active_by_session.get(session_id)
                if turn is not None and (
                    turn.last_output_at is None or output_at > turn.last_output_at
                ):
                    turn.last_output_at = output_at
                    turn.output_line_no = line_no

    for session_id in list(active_by_session):
        close_turn(session_id, "eof")

    providers = sorted(set(user_triggered) | set(response_samples) | set(dropped_no_output))
    lines = [
        "# User Turn Response Time Audit",
        "",
        f"Input: `{INPUT}`",
        "",
        "A response-time sample is counted only when the latest `user_message` before a row's first model-output event has a later same-session model-output event before the next such `user_message` or EOF.",
        "",
        "## Counts",
        "",
        "| provider | response-triggering user rows | response samples | dropped no output | dropped nonpositive | closed by next user | closed by EOF |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    total_user_started = 0
    total_response_samples = 0
    total_dropped_no_output = 0
    total_dropped_nonpositive = 0
    total_closed_next = 0
    total_closed_eof = 0
    for provider in providers:
        total_user_started += user_triggered[provider]
        total_response_samples += response_samples[provider]
        total_dropped_no_output += dropped_no_output[provider]
        total_dropped_nonpositive += dropped_nonpositive[provider]
        total_closed_next += closed_by_next_user[provider]
        total_closed_eof += closed_by_eof[provider]
        lines.append(
            f"| {provider} | {user_triggered[provider]:,} | {response_samples[provider]:,} | "
            f"{dropped_no_output[provider]:,} | {dropped_nonpositive[provider]:,} | "
            f"{closed_by_next_user[provider]:,} | {closed_by_eof[provider]:,} |"
        )
    lines.append(
        f"| all | {total_user_started:,} | {total_response_samples:,} | "
        f"{total_dropped_no_output:,} | {total_dropped_nonpositive:,} | "
        f"{total_closed_next:,} | {total_closed_eof:,} |"
    )
    lines.extend(
        [
            "",
            "## Visible User Messages",
            "",
            "| provider | rows containing user_message | response-triggering user rows |",
            "|---|---:|---:|",
        ]
    )
    for provider in sorted(visible_user_any):
        started = user_triggered[provider]
        lines.append(f"| {provider} | {visible_user_any[provider]:,} | {started:,} |")
    lines.extend(
        [
            "",
            "## Visible User Rows Not Used As Response Triggers",
            "",
            "| provider | reason | rows |",
            "|---|---|---:|",
        ]
    )
    for (provider, reason), count in sorted(visible_user_not_trigger.items()):
        lines.append(f"| {provider} | {reason} | {count:,} |")
    lines.extend(
        [
            "",
            "## First Event Type Counts",
            "",
            "| provider | first event | rows |",
            "|---|---|---:|",
        ]
    )
    for (provider, first_event), count in sorted(first_event_counts.items()):
        lines.append(f"| {provider} | {first_event} | {count:,} |")

    if dropped_samples:
        lines.extend(["", "## Dropped Samples", ""])
        lines.extend(dropped_samples)

    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
