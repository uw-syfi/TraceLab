"""Timing-event helpers: timestamp parsing and input/output span extraction."""

from __future__ import annotations

from typing import Any
from datetime import datetime

INPUT_EVENT_TYPES = {"user_message", "tool_result"}


MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}


RESPONSE_END_EVENT_TYPES = MODEL_OUTPUT_EVENT_TYPES


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def user_message_start_timestamp(row: dict[str, Any]) -> datetime | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            continue
        if event_type != "user_message":
            return None
        return parse_ts(event.get("timestamp"))
    return None


def response_trigger_user_message_timestamp(row: dict[str, Any]) -> datetime | None:
    """Return the latest user input that can trigger this row's model output."""
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is None:
            continue
        event_type = event.get("event_type")
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
    if not candidate_users:
        return None
    return max(candidate_users)


def last_model_output_timestamp(row: dict[str, Any]) -> datetime | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    output_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") not in MODEL_OUTPUT_EVENT_TYPES:
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is not None:
            output_timestamps.append(timestamp)
    if not output_timestamps:
        return None
    return max(output_timestamps)


def last_response_end_timestamp(row: dict[str, Any]) -> datetime | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("event_type") not in RESPONSE_END_EVENT_TYPES:
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is not None:
            timestamps.append(timestamp)
    if not timestamps:
        return None
    return max(timestamps)


def input_to_last_output_span_seconds(row: dict[str, Any]) -> float | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    input_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is None:
            continue
        event_type = event.get("event_type")
        if event_type in INPUT_EVENT_TYPES:
            input_timestamps.append(timestamp)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(timestamp)

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
