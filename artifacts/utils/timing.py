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


def human_waits_from_event_pairs(
    event_pairs: Any, previous_event_at: datetime | None
) -> tuple[list[float], int, datetime | None]:
    """Provider-agnostic human-thinking waits for one round's timing events.

    ``event_pairs`` is an iterable of ``(event_type, timestamp | None)``. Using
    ``previous_event_at`` (the session's last event timestamp *before* this round, or ``None``),
    return ``(waits, user_message_count, last_event_at)``:

      * ``waits`` -- the strictly-positive gap from the immediately preceding event of **any**
        type (including a prior round via ``previous_event_at``, and non-output events such as
        Codex ``usage_report``) to each ``user_message`` event, in event time order. This counts
        **every** user message (turn-triggering or not), so it captures all human idle -- unlike
        the older "previous model output -> response-triggering user message" definition, which
        dropped non-trigger messages and the post-output ``usage_report`` tail into an unattributed
        residual.
      * ``user_message_count`` -- number of ``user_message`` events in this round (candidates).
      * ``last_event_at`` -- the latest event timestamp in this round, else ``previous_event_at``
        (carry forward as the next round's ``previous_event_at``).
    """
    items = sorted(
        ((ts, et) for et, ts in event_pairs if ts is not None),
        key=lambda pair: pair[0],
    )
    waits: list[float] = []
    user_message_count = 0
    last = previous_event_at
    for ts, event_type in items:
        if event_type == "user_message":
            user_message_count += 1
            if last is not None:
                wait = (ts - last).total_seconds()
                if wait > 0:
                    waits.append(wait)
        last = ts
    return waits, user_message_count, last


def human_input_wait_seconds_for_row(
    row: dict[str, Any], previous_event_at: datetime | None
) -> tuple[list[float], int, datetime | None]:
    """Row-dict wrapper over :func:`human_waits_from_event_pairs` (see it for semantics)."""
    events = row.get("timing_events")
    pairs: list[tuple[Any, datetime | None]] = []
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                pairs.append((event.get("event_type"), parse_ts(event.get("timestamp"))))
    return human_waits_from_event_pairs(pairs, previous_event_at)
