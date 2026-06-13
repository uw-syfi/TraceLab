#!/usr/bin/env python3
"""Print general aggregate stats for a normalized LLM round trace."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402
from growth import (  # noqa: E402
    InputGrowthStats,
    MAJOR_COMPACT_MIN_TOKENS,
    MICRO_REDUCTION_MAX_TOKENS,
    first_timing_event_type,
)

DEFAULT_INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"

# Timing-event and tool timestamps were originally ISO8601 strings on the JSONL rows
# (`YYYY-MM-DDTHH:MM:SS.mmmZ`, uniformly UTC millisecond precision); the summary parses them
# back into aware datetimes. The trace DB stores them as a naive microsecond TIMESTAMP (T/Z
# stripped by trace_db._ts), so we pull them as integer epoch-microseconds (native/wasm-identical
# marshalling) and rebuild the exact canonical ISO string here. Feeding the reconstructed string
# back through the unchanged JSON-path helpers (`parse_ts`) yields bit-for-bit identical results.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    moment = _EPOCH + timedelta(microseconds=value)
    millis = moment.microsecond // 1000
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"

INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
NON_REASONING_MODEL_OUTPUT_EVENT_TYPES = {"text", "tool_call"}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def int_field(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) else 0


def output_tokens_including_reasoning(row: dict[str, Any]) -> int:
    """Return inclusive output tokens.

    In the Codex token accounting rows observed here, `reasoning_output_tokens`
    is a subset of `output_tokens`, not an additional count. Claude rows also
    report inclusive assistant output in `output_tokens`.
    """
    return int_field(row, "output_tokens")


def visible_or_structured_output_tokens(row: dict[str, Any]) -> int:
    """Return output tokens after removing exact reasoning tokens when known."""
    output_tokens = output_tokens_including_reasoning(row)
    reasoning_tokens = row.get("reasoning_output_tokens")
    if isinstance(reasoning_tokens, int):
        return max(0, output_tokens - reasoning_tokens)
    return output_tokens


def event_types(row: dict[str, Any]) -> set[str]:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return set()
    return {
        event.get("event_type")
        for event in events
        if isinstance(event, dict) and isinstance(event.get("event_type"), str)
    }


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if quantile <= 0:
        return min(values)
    if quantile >= 1:
        return max(values)

    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    if lower_index == upper_index:
        return ordered[lower_index]

    lower_weight = upper_index - index
    upper_weight = index - lower_index
    return (
        ordered[lower_index] * lower_weight
        + ordered[upper_index] * upper_weight
    )


def user_message_start_timestamp(row: dict[str, Any]) -> datetime | None:
    """Return the first user-message timestamp if the round starts from the user."""
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
    """Return the latest user message before this row's first model output."""
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
    candidate_inputs = [timestamp for timestamp in input_timestamps if timestamp <= first_output_at]
    if not candidate_inputs:
        return None
    input_ready_at = max(candidate_inputs)
    duration = (max(output_timestamps) - input_ready_at).total_seconds()
    return duration if duration > 0 else None


def input_to_reasoning_end_span_seconds(row: dict[str, Any]) -> float | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    input_timestamps: list[datetime] = []
    reasoning_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is None:
            continue
        if event_type in INPUT_EVENT_TYPES:
            input_timestamps.append(timestamp)
        elif event_type == "reasoning":
            reasoning_timestamps.append(timestamp)

    if not input_timestamps or not reasoning_timestamps:
        return None
    reasoning_end_at = max(reasoning_timestamps)
    candidate_inputs = [timestamp for timestamp in input_timestamps if timestamp <= reasoning_end_at]
    if not candidate_inputs:
        return None
    duration = (reasoning_end_at - max(candidate_inputs)).total_seconds()
    return duration if duration > 0 else None


def post_reasoning_output_span_seconds(row: dict[str, Any]) -> float | None:
    """Return the trace span from reasoning marker/end to final visible/tool output.

    If a round has no reasoning marker, fall back to the span from first to last
    non-reasoning model output. The fallback keeps ordinary non-reasoning rounds
    usable for output-speed summaries while keeping reasoning rounds anchored at
    the reasoning marker/end.
    """
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None

    reasoning_timestamps: list[datetime] = []
    non_reasoning_output_timestamps: list[datetime] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is None:
            continue
        if event_type == "reasoning":
            reasoning_timestamps.append(timestamp)
        elif event_type in NON_REASONING_MODEL_OUTPUT_EVENT_TYPES:
            non_reasoning_output_timestamps.append(timestamp)

    if reasoning_timestamps and non_reasoning_output_timestamps:
        reasoning_end_at = max(reasoning_timestamps)
        later_outputs = [
            timestamp
            for timestamp in non_reasoning_output_timestamps
            if timestamp >= reasoning_end_at
        ]
        if later_outputs:
            duration = (max(later_outputs) - reasoning_end_at).total_seconds()
            return duration if duration > 0 else None

    if len(non_reasoning_output_timestamps) < 2:
        return None
    duration = (
        max(non_reasoning_output_timestamps) - min(non_reasoning_output_timestamps)
    ).total_seconds()
    return duration if duration > 0 else None


def iter_observed_timestamps(row: dict[str, Any]) -> list[datetime]:
    """Return trace-observed timestamps from timing events and tool metadata."""
    timestamps: list[datetime] = []

    events = row.get("timing_events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            timestamp = parse_ts(event.get("timestamp"))
            if timestamp is not None:
                timestamps.append(timestamp)

    tools = row.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            for key in ("emitted_at", "result_at"):
                timestamp = parse_ts(tool.get(key))
                if timestamp is not None:
                    timestamps.append(timestamp)

    return timestamps


def numeric_field(row: dict[str, Any], key: str) -> float | None:
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def isoformat_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


@dataclass
class Summary:
    rounds: int = 0
    user_input_rounds: int = 0
    tool_result_rounds: int = 0
    total_new_tokens: int = 0
    total_cached_read_tokens: int = 0
    total_output_tokens_including_reasoning: int = 0
    total_reasoning_output_tokens: int = 0
    rounds_with_observed_reasoning: int = 0
    rounds_with_positive_reasoning_output_tokens: int = 0
    total_observable_generation_time_seconds: float = 0.0
    observable_generation_time_rounds: int = 0
    observable_generation_time_seconds: list[float] = field(default_factory=list)
    generation_speed_rounds: int = 0
    generation_speed_token_seconds: float = 0.0
    generation_speed_tokens: int = 0
    input_to_reasoning_end_rounds: int = 0
    total_input_to_reasoning_end_seconds: float = 0.0
    user_started_input_token_rounds: int = 0
    tool_result_started_input_token_rounds: int = 0
    total_input_tokens_user_started: int = 0
    total_input_tokens_tool_result_started: int = 0
    total_new_tokens_user_started: int = 0
    total_new_tokens_tool_result_started: int = 0
    user_context_delta_rounds: int = 0
    total_user_context_delta_tokens: int = 0
    user_context_delta_tokens: list[float] = field(default_factory=list)
    tool_result_context_delta_rounds: int = 0
    total_tool_result_context_delta_tokens: int = 0
    tool_result_context_delta_tokens: list[float] = field(default_factory=list)
    user_started_total_input_growth: InputGrowthStats = field(
        default_factory=InputGrowthStats
    )
    tool_result_started_total_input_growth: InputGrowthStats = field(
        default_factory=InputGrowthStats
    )
    human_input_wait_candidate_rounds: int = 0
    human_input_wait_rounds: int = 0
    total_human_input_wait_seconds: float = 0.0
    human_input_wait_seconds: list[float] = field(default_factory=list)
    exact_reasoning_ttft_rounds: int = 0
    exact_reasoning_input_to_reasoning_end_seconds: float = 0.0
    exact_reasoning_tokens_for_ttft: int = 0
    exact_reasoning_decode_speed_rounds: int = 0
    exact_reasoning_decode_token_seconds: float = 0.0
    exact_reasoning_decode_tokens: int = 0
    tool_calls: int = 0
    rounds_with_tool_calls: int = 0
    tool_wall_latency_count: int = 0
    tool_wall_latency_missing: int = 0
    total_tool_wall_latency_ms: float = 0.0
    tool_internal_latency_count: int = 0
    total_tool_internal_latency_ms: float = 0.0
    tool_effective_latency_count: int = 0
    tool_effective_latency_missing: int = 0
    tool_effective_latency_nonpositive: int = 0
    tool_effective_latency_from_internal_count: int = 0
    tool_effective_latency_from_wall_count: int = 0
    tool_effective_latency_from_legacy_count: int = 0
    total_tool_effective_latency_ms: float = 0.0
    tool_effective_latency_ms: list[float] = field(default_factory=list)
    earliest_timestamp: datetime | None = None
    latest_timestamp: datetime | None = None
    session_ids: set[str] = field(default_factory=set)
    users: set[str] = field(default_factory=set)
    providers: Counter[str] = field(default_factory=Counter)
    models: Counter[str] = field(default_factory=Counter)
    event_type_counts: Counter[str] = field(default_factory=Counter)
    last_model_output_by_session: dict[str, datetime] = field(default_factory=dict)
    last_total_input_tokens_by_session: dict[str, int] = field(default_factory=dict)

    def add(self, row: dict[str, Any]) -> None:
        self.rounds += 1

        provider = row.get("provider")
        if isinstance(provider, str):
            self.providers[provider] += 1

        model = row.get("model")
        if isinstance(model, str):
            self.models[model] += 1

        session_id = row.get("session_id")
        if isinstance(session_id, str):
            self.session_ids.add(session_id)

        user_message_start_at = response_trigger_user_message_timestamp(row)
        if user_message_start_at is not None:
            self.human_input_wait_candidate_rounds += 1
            if isinstance(session_id, str):
                previous_model_output_at = self.last_model_output_by_session.get(
                    session_id
                )
                if previous_model_output_at is not None:
                    wait_seconds = (
                        user_message_start_at - previous_model_output_at
                    ).total_seconds()
                    if wait_seconds > 0:
                        self.human_input_wait_rounds += 1
                        self.total_human_input_wait_seconds += wait_seconds
                        self.human_input_wait_seconds.append(wait_seconds)

        user = row.get("user")
        if isinstance(user, str):
            self.users.add(user)

        types = event_types(row)
        self.event_type_counts.update(types)
        if "user_message" in types:
            self.user_input_rounds += 1
        if "tool_result" in types:
            self.tool_result_rounds += 1

        newly_append_tokens = int_field(row, "newly_append_tokens")
        prefix_tokens = int_field(row, "prefix_tokens")
        row_total_input_tokens = newly_append_tokens + prefix_tokens
        self.total_new_tokens += newly_append_tokens
        self.total_cached_read_tokens += prefix_tokens
        first_event_type = first_timing_event_type(row)
        if first_event_type == "user_message":
            self.user_started_input_token_rounds += 1
            self.total_input_tokens_user_started += row_total_input_tokens
            self.total_new_tokens_user_started += newly_append_tokens
            if isinstance(session_id, str):
                previous_total_input_tokens = self.last_total_input_tokens_by_session.get(
                    session_id
                )
                if previous_total_input_tokens is not None:
                    raw_delta_tokens = row_total_input_tokens - previous_total_input_tokens
                    context_delta_tokens = max(0, raw_delta_tokens)
                    self.user_context_delta_rounds += 1
                    self.total_user_context_delta_tokens += context_delta_tokens
                    self.user_context_delta_tokens.append(float(context_delta_tokens))
                    self.user_started_total_input_growth.add(raw_delta_tokens)
        elif first_event_type == "tool_result":
            self.tool_result_started_input_token_rounds += 1
            self.total_input_tokens_tool_result_started += row_total_input_tokens
            self.total_new_tokens_tool_result_started += newly_append_tokens
            if isinstance(session_id, str):
                previous_total_input_tokens = self.last_total_input_tokens_by_session.get(
                    session_id
                )
                if previous_total_input_tokens is not None:
                    raw_delta_tokens = row_total_input_tokens - previous_total_input_tokens
                    context_delta_tokens = max(0, raw_delta_tokens)
                    self.tool_result_context_delta_rounds += 1
                    self.total_tool_result_context_delta_tokens += context_delta_tokens
                    self.tool_result_context_delta_tokens.append(
                        float(context_delta_tokens)
                    )
                    self.tool_result_started_total_input_growth.add(raw_delta_tokens)
        output_tokens = output_tokens_including_reasoning(row)
        visible_tokens = visible_or_structured_output_tokens(row)
        self.total_output_tokens_including_reasoning += output_tokens
        reasoning_output_tokens = int_field(row, "reasoning_output_tokens")
        self.total_reasoning_output_tokens += reasoning_output_tokens
        if "reasoning" in types or reasoning_output_tokens > 0:
            self.rounds_with_observed_reasoning += 1
        if reasoning_output_tokens > 0:
            self.rounds_with_positive_reasoning_output_tokens += 1

        span_seconds = input_to_last_output_span_seconds(row)
        if span_seconds is not None:
            self.observable_generation_time_rounds += 1
            self.total_observable_generation_time_seconds += span_seconds
            self.observable_generation_time_seconds.append(span_seconds)
            if output_tokens > 0:
                self.generation_speed_rounds += 1
                self.generation_speed_token_seconds += span_seconds
                self.generation_speed_tokens += output_tokens

        input_to_reasoning_end_seconds = input_to_reasoning_end_span_seconds(row)
        if input_to_reasoning_end_seconds is not None:
            self.input_to_reasoning_end_rounds += 1
            self.total_input_to_reasoning_end_seconds += input_to_reasoning_end_seconds
            if reasoning_output_tokens > 0:
                self.exact_reasoning_ttft_rounds += 1
                self.exact_reasoning_input_to_reasoning_end_seconds += (
                    input_to_reasoning_end_seconds
                )
                self.exact_reasoning_tokens_for_ttft += reasoning_output_tokens

        output_decoding_span_seconds = post_reasoning_output_span_seconds(row)
        if output_decoding_span_seconds is not None and visible_tokens > 0:
            if reasoning_output_tokens > 0:
                self.exact_reasoning_decode_speed_rounds += 1
                self.exact_reasoning_decode_token_seconds += output_decoding_span_seconds
                self.exact_reasoning_decode_tokens += visible_tokens

        for timestamp in iter_observed_timestamps(row):
            if self.earliest_timestamp is None or timestamp < self.earliest_timestamp:
                self.earliest_timestamp = timestamp
            if self.latest_timestamp is None or timestamp > self.latest_timestamp:
                self.latest_timestamp = timestamp

        tools = row.get("tools")
        if isinstance(tools, list):
            row_tool_calls = 0
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                self.tool_calls += 1
                row_tool_calls += 1
                wall_latency_ms = numeric_field(tool, "tool_wall_latency_ms")
                if wall_latency_ms is None:
                    self.tool_wall_latency_missing += 1
                else:
                    self.tool_wall_latency_count += 1
                    self.total_tool_wall_latency_ms += wall_latency_ms

                internal_latency_ms = numeric_field(tool, "tool_internal_latency_ms")
                legacy_latency_ms = numeric_field(tool, "latency_ms")
                effective_latency_ms: float | None
                effective_source: str | None
                if internal_latency_ms is not None:
                    effective_latency_ms = internal_latency_ms
                    effective_source = "internal"
                elif wall_latency_ms is not None:
                    effective_latency_ms = wall_latency_ms
                    effective_source = "wall"
                elif legacy_latency_ms is not None:
                    effective_latency_ms = legacy_latency_ms
                    effective_source = "legacy"
                else:
                    effective_latency_ms = None
                    effective_source = None

                if effective_latency_ms is None:
                    self.tool_effective_latency_missing += 1
                elif effective_latency_ms <= 0:
                    self.tool_effective_latency_nonpositive += 1
                else:
                    self.tool_effective_latency_count += 1
                    self.total_tool_effective_latency_ms += effective_latency_ms
                    self.tool_effective_latency_ms.append(effective_latency_ms)
                    if effective_source == "internal":
                        self.tool_effective_latency_from_internal_count += 1
                    elif effective_source == "wall":
                        self.tool_effective_latency_from_wall_count += 1
                    elif effective_source == "legacy":
                        self.tool_effective_latency_from_legacy_count += 1

                if internal_latency_ms is not None:
                    self.tool_internal_latency_count += 1
                    self.total_tool_internal_latency_ms += internal_latency_ms
            if row_tool_calls:
                self.rounds_with_tool_calls += 1

        last_output_at = last_model_output_timestamp(row)
        if isinstance(session_id, str) and last_output_at is not None:
            self.last_model_output_by_session[session_id] = last_output_at
        if isinstance(session_id, str):
            self.last_total_input_tokens_by_session[session_id] = row_total_input_tokens

    @property
    def average_output_tokens(self) -> float | None:
        if self.rounds == 0:
            return None
        return self.total_output_tokens_including_reasoning / self.rounds

    @property
    def average_generation_speed_tokens_per_second(self) -> float | None:
        if self.generation_speed_token_seconds <= 0:
            return None
        return self.generation_speed_tokens / self.generation_speed_token_seconds

    @property
    def p50_observable_generation_time_seconds(self) -> float | None:
        return percentile(self.observable_generation_time_seconds, 0.50)

    @property
    def p90_observable_generation_time_seconds(self) -> float | None:
        return percentile(self.observable_generation_time_seconds, 0.90)

    @property
    def p50_tool_effective_latency_seconds(self) -> float | None:
        value_ms = percentile(self.tool_effective_latency_ms, 0.50)
        return value_ms / 1000 if value_ms is not None else None

    @property
    def p90_tool_effective_latency_seconds(self) -> float | None:
        value_ms = percentile(self.tool_effective_latency_ms, 0.90)
        return value_ms / 1000 if value_ms is not None else None

    @property
    def exact_reasoning_decode_latency_seconds_per_token(self) -> float | None:
        if self.exact_reasoning_decode_tokens <= 0:
            return None
        return self.exact_reasoning_decode_token_seconds / self.exact_reasoning_decode_tokens

    @property
    def exact_reasoning_decode_speed_tokens_per_second(self) -> float | None:
        latency = self.exact_reasoning_decode_latency_seconds_per_token
        if latency is None or latency <= 0:
            return None
        return 1 / latency

    @property
    def average_human_input_wait_seconds(self) -> float | None:
        if self.human_input_wait_rounds == 0:
            return None
        return self.total_human_input_wait_seconds / self.human_input_wait_rounds

    @property
    def p90_human_input_wait_seconds(self) -> float | None:
        return percentile(self.human_input_wait_seconds, 0.90)

    @property
    def median_human_input_wait_seconds(self) -> float | None:
        return percentile(self.human_input_wait_seconds, 0.50)

    @property
    def average_total_input_tokens_user_started(self) -> float | None:
        if self.user_started_input_token_rounds == 0:
            return None
        return self.total_input_tokens_user_started / self.user_started_input_token_rounds

    @property
    def average_total_input_tokens_tool_result_started(self) -> float | None:
        if self.tool_result_started_input_token_rounds == 0:
            return None
        return (
            self.total_input_tokens_tool_result_started
            / self.tool_result_started_input_token_rounds
        )

    @property
    def average_new_input_tokens_user_started(self) -> float | None:
        if self.user_started_input_token_rounds == 0:
            return None
        return self.total_new_tokens_user_started / self.user_started_input_token_rounds

    @property
    def average_new_input_tokens_tool_result_started(self) -> float | None:
        if self.tool_result_started_input_token_rounds == 0:
            return None
        return (
            self.total_new_tokens_tool_result_started
            / self.tool_result_started_input_token_rounds
        )

    @property
    def average_user_context_delta_tokens(self) -> float | None:
        if self.user_context_delta_rounds == 0:
            return None
        return self.total_user_context_delta_tokens / self.user_context_delta_rounds

    @property
    def median_user_context_delta_tokens(self) -> float | None:
        return percentile(self.user_context_delta_tokens, 0.50)

    @property
    def p90_user_context_delta_tokens(self) -> float | None:
        return percentile(self.user_context_delta_tokens, 0.90)

    @property
    def average_tool_result_context_delta_tokens(self) -> float | None:
        if self.tool_result_context_delta_rounds == 0:
            return None
        return (
            self.total_tool_result_context_delta_tokens
            / self.tool_result_context_delta_rounds
        )

    @property
    def median_tool_result_context_delta_tokens(self) -> float | None:
        return percentile(self.tool_result_context_delta_tokens, 0.50)

    @property
    def p90_tool_result_context_delta_tokens(self) -> float | None:
        return percentile(self.tool_result_context_delta_tokens, 0.90)

    @property
    def prefix_hit_rate_user_started(self) -> float | None:
        if self.total_input_tokens_user_started == 0:
            return None
        cached_tokens = (
            self.total_input_tokens_user_started - self.total_new_tokens_user_started
        )
        return cached_tokens / self.total_input_tokens_user_started

    @property
    def prefix_hit_rate_tool_result_started(self) -> float | None:
        if self.total_input_tokens_tool_result_started == 0:
            return None
        cached_tokens = (
            self.total_input_tokens_tool_result_started
            - self.total_new_tokens_tool_result_started
        )
        return cached_tokens / self.total_input_tokens_tool_result_started

    @property
    def tool_calls_per_visible_user_message_round(self) -> float | None:
        if self.user_input_rounds == 0:
            return None
        return self.tool_calls / self.user_input_rounds

    def as_dict(self) -> dict[str, Any]:
        total_input_tokens = self.total_new_tokens + self.total_cached_read_tokens
        non_reasoning_output_tokens = (
            self.total_output_tokens_including_reasoning - self.total_reasoning_output_tokens
        )
        exact_reasoning_decode_latency = (
            self.exact_reasoning_decode_latency_seconds_per_token
        )
        exact_reasoning_decode_speed = (
            self.exact_reasoning_decode_speed_tokens_per_second
        )
        exact_reasoning_estimated_ttft_total = None
        if (
            exact_reasoning_decode_latency is not None
            and self.exact_reasoning_ttft_rounds
        ):
            exact_reasoning_estimated_ttft_total = (
                self.exact_reasoning_input_to_reasoning_end_seconds
                - self.exact_reasoning_tokens_for_ttft * exact_reasoning_decode_latency
            )
        return {
            "scope": {
                "total_sessions": len(self.session_ids),
                "distinct_users": len(self.users) if self.users else None,
                "llm_rounds_total": self.rounds,
                "rounds_with_visible_user_message": self.user_input_rounds,
                "rounds_started_from_tool_result": self.tool_result_rounds,
                "earliest_observed_timestamp": isoformat_z(self.earliest_timestamp),
                "latest_observed_timestamp": isoformat_z(self.latest_timestamp),
            },
            "tokens": {
                "input": {
                    "direct_new_input_token_definition": (
                        "Direct new input tokens are provider-reported "
                        "`newly_append_tokens`. Trigger-conditioned "
                        "average_new_input_tokens_* fields use this direct "
                        "new-append/cache-miss accounting."
                    ),
                    "total_input_tokens": total_input_tokens,
                    "new_input_tokens": self.total_new_tokens,
                    "cached_read_input_tokens": self.total_cached_read_tokens,
                    "average_total_input_tokens_per_round": (
                        total_input_tokens / self.rounds if self.rounds else None
                    ),
                    "average_new_input_tokens_per_round": (
                        self.total_new_tokens / self.rounds if self.rounds else None
                    ),
                    "average_cached_read_input_tokens_per_round": (
                        self.total_cached_read_tokens / self.rounds if self.rounds else None
                    ),
                    "rounds_started_with_user_message_for_input_token_average": (
                        self.user_started_input_token_rounds
                    ),
                    "average_total_input_tokens_when_started_with_user_message": (
                        self.average_total_input_tokens_user_started
                    ),
                    "average_new_input_tokens_when_started_with_user_message": (
                        self.average_new_input_tokens_user_started
                    ),
                    "total_new_input_tokens_when_started_with_user_message": (
                        self.total_new_tokens_user_started
                    ),
                    "prefix_hit_rate_when_started_with_user_message": (
                        self.prefix_hit_rate_user_started
                    ),
                    "user_context_delta_definition": (
                        "For user-message-started rounds with a previous round in the "
                        "same session: max(0, current total input tokens - previous "
                        "total input tokens). This estimates prompt growth rather than "
                        "provider cache-miss accounting."
                    ),
                    "user_context_delta_rounds": self.user_context_delta_rounds,
                    "average_user_context_delta_tokens": (
                        self.average_user_context_delta_tokens
                    ),
                    "median_user_context_delta_tokens": (
                        self.median_user_context_delta_tokens
                    ),
                    "p90_user_context_delta_tokens": (
                        self.p90_user_context_delta_tokens
                    ),
                    "rounds_started_with_tool_result_for_input_token_average": (
                        self.tool_result_started_input_token_rounds
                    ),
                    "average_total_input_tokens_when_started_with_tool_result": (
                        self.average_total_input_tokens_tool_result_started
                    ),
                    "average_new_input_tokens_when_started_with_tool_result": (
                        self.average_new_input_tokens_tool_result_started
                    ),
                    "total_new_input_tokens_when_started_with_tool_result": (
                        self.total_new_tokens_tool_result_started
                    ),
                    "prefix_hit_rate_when_started_with_tool_result": (
                        self.prefix_hit_rate_tool_result_started
                    ),
                    "tool_result_context_delta_definition": (
                        "For tool-result-started rounds with a previous round in the "
                        "same session: max(0, current total input tokens - previous "
                        "total input tokens). This estimates prompt growth rather than "
                        "provider cache-miss accounting."
                    ),
                    "tool_result_context_delta_rounds": (
                        self.tool_result_context_delta_rounds
                    ),
                    "average_tool_result_context_delta_tokens": (
                        self.average_tool_result_context_delta_tokens
                    ),
                    "median_tool_result_context_delta_tokens": (
                        self.median_tool_result_context_delta_tokens
                    ),
                    "p90_tool_result_context_delta_tokens": (
                        self.p90_tool_result_context_delta_tokens
                    ),
                    "total_input_growth_definition": (
                        "For rounds with a previous round in the same session: "
                        "current total input tokens - previous total input tokens, "
                        "before clipping. total input tokens are "
                        "prefix_tokens + newly_append_tokens. First rounds in a "
                        "session are excluded."
                    ),
                    "total_context_increase_definition": (
                        "Sum of positive same-session total-input deltas, before "
                        "netting out zero or negative deltas. This counts total "
                        "observed context growth across user- and tool-result-started "
                        "rounds that have a previous round in the same session."
                    ),
                    "total_context_increase_tokens": (
                        self.user_started_total_input_growth.total_positive_growth_tokens
                        + self.tool_result_started_total_input_growth.total_positive_growth_tokens
                    ),
                    "total_input_growth_reduction_buckets": {
                        "micro_reduction": (
                            f"raw delta is negative and the reduction is <= "
                            f"{MICRO_REDUCTION_MAX_TOKENS} tokens"
                        ),
                        "ordinary_reduction": (
                            f"raw delta is negative, the reduction is > "
                            f"{MICRO_REDUCTION_MAX_TOKENS} tokens, and the "
                            f"reduction is < {MAJOR_COMPACT_MIN_TOKENS} tokens"
                        ),
                        "major_compact": (
                            f"raw delta is negative and the reduction is >= "
                            f"{MAJOR_COMPACT_MIN_TOKENS} tokens"
                        ),
                    },
                    "total_input_growth_when_started_with_user_message": (
                        self.user_started_total_input_growth.as_dict()
                    ),
                    "total_input_growth_when_started_with_tool_result": (
                        self.tool_result_started_total_input_growth.as_dict()
                    ),
                    "prefix_hit_rate": (
                        self.total_cached_read_tokens / total_input_tokens
                        if total_input_tokens
                        else None
                    ),
                },
                "output": {
                    "total_output_tokens_including_reasoning": (
                        self.total_output_tokens_including_reasoning
                    ),
                    "visible_or_structured_output_tokens_estimate": non_reasoning_output_tokens,
                    "rounds_with_observed_reasoning": self.rounds_with_observed_reasoning,
                    "reasoning_output_tokens_subset": self.total_reasoning_output_tokens,
                    "rounds_with_positive_reasoning_output_tokens": (
                        self.rounds_with_positive_reasoning_output_tokens
                    ),
                    "average_output_tokens_including_reasoning_per_round": (
                        self.average_output_tokens
                    ),
                },
            },
            "generation_timing": {
                "definition": (
                    "Sum over rounds from latest input event to last model-output event; "
                    "this is observable trace time, not serving-engine internal decode time."
                ),
                "total_observable_generation_time_seconds": (
                    self.total_observable_generation_time_seconds
                ),
                "rounds_with_observable_generation_time": (
                    self.observable_generation_time_rounds
                ),
                "p50_observable_generation_time_seconds": (
                    self.p50_observable_generation_time_seconds
                ),
                "p90_observable_generation_time_seconds": (
                    self.p90_observable_generation_time_seconds
                ),
                "average_normalized_decoding_speed_tokens_per_second": (
                    self.average_generation_speed_tokens_per_second
                ),
                "rounds_used_for_normalized_decoding_speed": self.generation_speed_rounds,
                "input_to_reasoning_end_definition": (
                    "Latest input event (`user_message` or `tool_result`) to the "
                    "reasoning marker/end timestamp."
                ),
                "total_input_to_reasoning_end_time_seconds": (
                    self.total_input_to_reasoning_end_seconds
                ),
                "rounds_with_input_to_reasoning_end_time": (
                    self.input_to_reasoning_end_rounds
                ),
                "waiting_for_human_input_definition": (
                    "For rows with a `user_message` before the first model-output event, "
                    "subtract the previous last model-output event (`reasoning`, `text`, "
                    "or `tool_call`) in the same session from the latest such "
                    "`user_message` timestamp."
                ),
                "rounds_with_user_message_before_model_output": (
                    self.human_input_wait_candidate_rounds
                ),
                "rounds_with_waiting_for_human_input_time": (
                    self.human_input_wait_rounds
                ),
                "total_waiting_for_human_input_seconds": (
                    self.total_human_input_wait_seconds
                ),
                "average_waiting_for_human_input_seconds": (
                    self.average_human_input_wait_seconds
                ),
                "median_waiting_for_human_input_seconds": (
                    self.median_human_input_wait_seconds
                ),
                "p90_waiting_for_human_input_seconds": (
                    self.p90_human_input_wait_seconds
                ),
                "post_reasoning_tpot_estimate": {
                    "definition": (
                        "TPOT-style decode estimate for rounds with positive exact "
                        "`reasoning_output_tokens`: reasoning marker/end to last "
                        "non-reasoning model-output event (`text` or `tool_call`), "
                        "divided by visible/structured output tokens "
                        "(`output_tokens - reasoning_output_tokens`). Values are null "
                        "when exact reasoning-token accounting is unavailable."
                    ),
                    "rounds": self.exact_reasoning_decode_speed_rounds,
                    "visible_or_structured_output_tokens": (
                        self.exact_reasoning_decode_tokens
                    ),
                    "post_reasoning_output_decode_time_seconds": (
                        self.exact_reasoning_decode_token_seconds
                        if exact_reasoning_decode_latency is not None
                        else None
                    ),
                    "average_decode_speed_tokens_per_second": (
                        exact_reasoning_decode_speed
                    ),
                    "average_decode_latency_seconds_per_token": (
                        exact_reasoning_decode_latency
                    ),
                },
                "estimated_ttft_from_exact_reasoning_tokens": {
                    "definition": (
                        "Residual estimate for rounds with positive exact "
                        "`reasoning_output_tokens`: "
                        "input_to_reasoning_end_time - reasoning_output_tokens * "
                        "post_reasoning_tpot_estimate.average_decode_latency_seconds_per_token."
                    ),
                    "rounds": self.exact_reasoning_ttft_rounds,
                    "input_to_reasoning_end_total_seconds": (
                        self.exact_reasoning_input_to_reasoning_end_seconds
                    ),
                    "reasoning_tokens": self.exact_reasoning_tokens_for_ttft,
                    "decode_latency_seconds_per_token_used": (
                        exact_reasoning_decode_latency
                    ),
                    "decode_speed_tokens_per_second_used": exact_reasoning_decode_speed,
                    "rounds_used_to_estimate_decode_latency": (
                        self.exact_reasoning_decode_speed_rounds
                    ),
                    "estimated_total_seconds": exact_reasoning_estimated_ttft_total,
                    "estimated_average_seconds": (
                        exact_reasoning_estimated_ttft_total
                        / self.exact_reasoning_ttft_rounds
                        if exact_reasoning_estimated_ttft_total is not None
                        and self.exact_reasoning_ttft_rounds
                        else None
                    ),
                },
            },
            "tools": {
                "definition": (
                    "Tool-call totals are counted from tools[] entries. Effective latency "
                    "uses tool_internal_latency_ms when available, otherwise "
                    "tool_wall_latency_ms, otherwise latency_ms. Nonpositive latencies "
                    "are counted separately and excluded from latency percentiles/totals "
                    "to match the tool-latency distribution experiment. Totals are summed "
                    "over tool calls, so parallel calls are additive."
                ),
                "total_tool_calls": self.tool_calls,
                "rounds_with_tool_calls": self.rounds_with_tool_calls,
                "tool_calls_per_visible_user_message_round": (
                    self.tool_calls_per_visible_user_message_round
                ),
                "effective_latency": {
                    "total_seconds": self.total_tool_effective_latency_ms / 1000,
                    "tool_calls_with_latency": self.tool_effective_latency_count,
                    "tool_calls_missing_latency": self.tool_effective_latency_missing,
                    "tool_calls_nonpositive_latency": (
                        self.tool_effective_latency_nonpositive
                    ),
                    "p50_seconds": self.p50_tool_effective_latency_seconds,
                    "p90_seconds": self.p90_tool_effective_latency_seconds,
                    "tool_calls_using_internal_latency": (
                        self.tool_effective_latency_from_internal_count
                    ),
                    "tool_calls_using_wall_latency_fallback": (
                        self.tool_effective_latency_from_wall_count
                    ),
                    "tool_calls_using_legacy_latency_fallback": (
                        self.tool_effective_latency_from_legacy_count
                    ),
                },
            },
            "rounds_by_provider": dict(sorted(self.providers.items())),
            "rounds_by_model": dict(self.models.most_common()),
        }


@dataclass
class SummaryBundle:
    merged: Summary = field(default_factory=Summary)
    by_provider: dict[str, Summary] = field(default_factory=dict)

    def add(self, row: dict[str, Any]) -> None:
        self.merged.add(row)

        provider = row.get("provider")
        if not isinstance(provider, str):
            return
        if provider not in self.by_provider:
            self.by_provider[provider] = Summary()
        self.by_provider[provider].add(row)

    def as_dict(self) -> dict[str, Any]:
        data = {"merged": self.merged.as_dict()}
        for provider in sorted(self.by_provider):
            data[provider] = self.by_provider[provider].as_dict()
        return data


def fmt_int(value: int | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:,}"


def fmt_float(value: float | None, digits: int = 2) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:,.{digits}f}"


def fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{fmt_float(value * 100, digits=digits)}%"


def print_input_growth(label: str, growth: dict[str, Any]) -> None:
    print(f"  total-input growth / {label}:")
    print(
        "    rounds with previous same-session total: "
        f"{fmt_int(growth['rounds'])}"
    )
    print(
        "    positive / zero / negative rounds: "
        f"{fmt_int(growth['positive_growth_rounds'])} "
        f"({fmt_pct(growth['positive_growth_share'])}) / "
        f"{fmt_int(growth['zero_growth_rounds'])} "
        f"({fmt_pct(growth['zero_growth_share'])}) / "
        f"{fmt_int(growth['negative_growth_rounds'])} "
        f"({fmt_pct(growth['negative_growth_share'])})"
    )
    print(
        "    negative buckets, micro / ordinary / major compact: "
        f"{fmt_int(growth['micro_reduction_rounds'])} "
        f"({fmt_pct(growth['micro_reduction_share'])}) / "
        f"{fmt_int(growth['ordinary_reduction_rounds'])} "
        f"({fmt_pct(growth['ordinary_reduction_share'])}) / "
        f"{fmt_int(growth['major_compact_rounds'])} "
        f"({fmt_pct(growth['major_compact_share'])})"
    )
    print(
        "    total context increase tokens: "
        f"{fmt_int(growth['total_context_increase_tokens'])}"
    )
    print(
        "    raw delta avg / p10 / median / p90: "
        f"{fmt_float(growth['average_raw_delta_tokens'])} / "
        f"{fmt_float(growth['p10_raw_delta_tokens'])} / "
        f"{fmt_float(growth['median_raw_delta_tokens'])} / "
        f"{fmt_float(growth['p90_raw_delta_tokens'])}"
    )
    print(
        "    positive-growth avg / clipped-growth avg / reduction avg / max reduction: "
        f"{fmt_float(growth['average_positive_growth_tokens'])} / "
        f"{fmt_float(growth['average_clipped_growth_tokens'])} / "
        f"{fmt_float(growth['average_reduction_tokens'])} / "
        f"{fmt_int(growth['max_reduction_tokens'])}"
    )


def _rows_from_db(con) -> list[dict[str, Any]]:
    """Reconstruct per-round dicts from the trace DB in file order (``ORDER BY round_pk``).

    Each dict carries exactly the keys ``Summary.add`` and its helpers read (round scalars plus
    rebuilt ``timing_events`` / ``tools`` lists), with the same field names and Python types the
    JSONL rows had. Timestamps are rebuilt to the canonical ISO string from integer
    epoch-microseconds so the unchanged datetime-parsing helpers behave identically. ``NULL``
    columns pass straight through (``None``), matching missing/null JSON values.
    """
    # Per-round timing events, ordered by event_index (event_index = 1 is the round's FIRST event,
    # which `growth.first_timing_event_type` relies on). Only event_type + timestamp are read.
    timing_by_round: dict[int, list[dict[str, Any]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        timing_by_round.setdefault(round_pk, []).append(
            {"event_type": event_type, "timestamp": _epoch_us_to_iso(ts_us)}
        )

    # Per-round tool calls, ordered by tool_index. The summary reads wall/internal latency plus
    # emitted_at/result_at (for the observed-timestamp min/max). Legacy `latency_ms` is absent in
    # the normalized schema, so it is intentionally not reconstructed (the legacy fallback never
    # fired on the JSONL path either).
    tools_by_round: dict[int, list[dict[str, Any]]] = {}
    for (
        round_pk,
        tool_wall_latency_ms,
        tool_internal_latency_ms,
        emitted_us,
        result_us,
    ) in con.execute(
        "SELECT round_pk, tool_wall_latency_ms, tool_internal_latency_ms, "
        "CAST(epoch_us(emitted_at) AS BIGINT) AS emitted_us, "
        "CAST(epoch_us(result_at) AS BIGINT) AS result_us "
        "FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        tools_by_round.setdefault(round_pk, []).append(
            {
                "tool_wall_latency_ms": tool_wall_latency_ms,
                "tool_internal_latency_ms": tool_internal_latency_ms,
                "emitted_at": _epoch_us_to_iso(emitted_us),
                "result_at": _epoch_us_to_iso(result_us),
            }
        )

    rows: list[dict[str, Any]] = []
    for (
        round_pk,
        provider,
        model,
        session_id,
        user,
        prefix_tokens,
        newly_append_tokens,
        output_tokens,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, model, session_id, \"user\", "
        "prefix_tokens, newly_append_tokens, output_tokens, reasoning_output_tokens "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        rows.append(
            {
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "user": user,
                "prefix_tokens": prefix_tokens,
                "newly_append_tokens": newly_append_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "timing_events": timing_by_round.get(round_pk, []),
                "tools": tools_by_round.get(round_pk, []),
            }
        )
    return rows


def read_summary_from_db(con) -> SummaryBundle:
    """Build the summary bundle from an open trace DB connection (the core computation path)."""
    summary = SummaryBundle()
    for row in _rows_from_db(con):
        summary.add(row)
    return summary


def read_summary(path: Path) -> SummaryBundle:
    """Build the summary bundle from a JSONL trace path (preserved public/in-process API).

    Materializes the path into a temp trace DB via ``trace_db`` (reused across runs), then defers
    to :func:`read_summary_from_db`. The driver and ``run_all.py`` keep calling this unchanged.
    """
    cache = trace_db._cache_db_path(Path(path))
    trace = Path(path)
    fresh = cache.exists() and cache.stat().st_mtime >= trace.stat().st_mtime
    if not fresh:
        trace_db.materialize(trace, cache)
    con = trace_db.connect(cache, read_only=True)
    try:
        return read_summary_from_db(con)
    finally:
        con.close()


def print_one_summary(title: str, data: dict[str, Any]) -> None:
    scope = data["scope"]
    tokens = data["tokens"]
    generation = data["generation_timing"]
    tools = data["tools"]

    print(title)
    print("scope:")
    print(f"  total sessions: {fmt_int(scope['total_sessions'])}")
    print(f"  distinct users: {fmt_int(scope['distinct_users'])}")
    print(f"  LLM rounds total: {fmt_int(scope['llm_rounds_total'])}")
    print(
        "  rounds with visible user message: "
        f"{fmt_int(scope['rounds_with_visible_user_message'])}"
    )
    print(
        "  rounds started from tool result: "
        f"{fmt_int(scope['rounds_started_from_tool_result'])}"
    )
    print(f"  earliest observed timestamp: {scope['earliest_observed_timestamp'] or 'n/a'}")
    print(f"  latest observed timestamp: {scope['latest_observed_timestamp'] or 'n/a'}")
    print()

    print("tokens:")
    print(f"  total input tokens: {fmt_int(tokens['input']['total_input_tokens'])}")
    print(f"  new input tokens: {fmt_int(tokens['input']['new_input_tokens'])}")
    print(
        "  cached-read input tokens: "
        f"{fmt_int(tokens['input']['cached_read_input_tokens'])}"
    )
    print(
        "  average total input tokens / round: "
        f"{fmt_float(tokens['input']['average_total_input_tokens_per_round'])}"
    )
    print(
        "  average new input tokens / round: "
        f"{fmt_float(tokens['input']['average_new_input_tokens_per_round'])}"
    )
    print(
        "  average cached-read input tokens / round: "
        f"{fmt_float(tokens['input']['average_cached_read_input_tokens_per_round'])}"
    )
    print(
        "  average total input tokens / user-started round: "
        f"{fmt_float(tokens['input']['average_total_input_tokens_when_started_with_user_message'])} "
        f"over {fmt_int(tokens['input']['rounds_started_with_user_message_for_input_token_average'])} "
        "rounds"
    )
    print(
        "  average direct new-append input tokens / user-started round: "
        f"{fmt_float(tokens['input']['average_new_input_tokens_when_started_with_user_message'])} "
        f"over {fmt_int(tokens['input']['rounds_started_with_user_message_for_input_token_average'])} "
        "rounds"
    )
    print(
        "  total direct new-append input tokens / user-started rounds: "
        f"{fmt_int(tokens['input']['total_new_input_tokens_when_started_with_user_message'])}"
    )
    print(
        "  prefix hit rate / user-started round: "
        f"{fmt_float(tokens['input']['prefix_hit_rate_when_started_with_user_message'], digits=4)}"
    )
    print(
        "  average user-started context delta tokens: "
        f"{fmt_float(tokens['input']['average_user_context_delta_tokens'])} "
        f"over {fmt_int(tokens['input']['user_context_delta_rounds'])} rounds"
    )
    print(
        "  median / p90 user-started context delta tokens: "
        f"{fmt_float(tokens['input']['median_user_context_delta_tokens'])} / "
        f"{fmt_float(tokens['input']['p90_user_context_delta_tokens'])}"
    )
    print(
        "  average total input tokens / tool-result-started round: "
        f"{fmt_float(tokens['input']['average_total_input_tokens_when_started_with_tool_result'])} "
        f"over {fmt_int(tokens['input']['rounds_started_with_tool_result_for_input_token_average'])} "
        "rounds"
    )
    print(
        "  average direct new-append input tokens / tool-result-started round: "
        f"{fmt_float(tokens['input']['average_new_input_tokens_when_started_with_tool_result'])} "
        f"over {fmt_int(tokens['input']['rounds_started_with_tool_result_for_input_token_average'])} "
        "rounds"
    )
    print(
        "  total direct new-append input tokens / tool-result-started rounds: "
        f"{fmt_int(tokens['input']['total_new_input_tokens_when_started_with_tool_result'])}"
    )
    print(
        "  prefix hit rate / tool-result-started round: "
        f"{fmt_float(tokens['input']['prefix_hit_rate_when_started_with_tool_result'], digits=4)}"
    )
    print(
        "  average tool-result-started context delta tokens: "
        f"{fmt_float(tokens['input']['average_tool_result_context_delta_tokens'])} "
        f"over {fmt_int(tokens['input']['tool_result_context_delta_rounds'])} rounds"
    )
    print(
        "  median / p90 tool-result-started context delta tokens: "
        f"{fmt_float(tokens['input']['median_tool_result_context_delta_tokens'])} / "
        f"{fmt_float(tokens['input']['p90_tool_result_context_delta_tokens'])}"
    )
    print_input_growth(
        "user-started rounds",
        tokens["input"]["total_input_growth_when_started_with_user_message"],
    )
    print_input_growth(
        "tool-result-started rounds",
        tokens["input"]["total_input_growth_when_started_with_tool_result"],
    )
    print(
        "  prefix hit rate: "
        f"{fmt_float(tokens['input']['prefix_hit_rate'], digits=4)}"
    )
    print(
        "  total output tokens, including reasoning: "
        f"{fmt_int(tokens['output']['total_output_tokens_including_reasoning'])}"
    )
    print(
        "  visible/structured output tokens estimate: "
        f"{fmt_int(tokens['output']['visible_or_structured_output_tokens_estimate'])}"
    )
    print(
        "  reasoning output tokens subset: "
        f"{fmt_int(tokens['output']['reasoning_output_tokens_subset'])}"
    )
    print(
        "  rounds with observed reasoning: "
        f"{fmt_int(tokens['output']['rounds_with_observed_reasoning'])}"
    )
    print(
        "  rounds with positive reasoning tokens: "
        f"{fmt_int(tokens['output']['rounds_with_positive_reasoning_output_tokens'])}"
    )
    print(
        "  average output tokens / round: "
        f"{fmt_float(tokens['output']['average_output_tokens_including_reasoning_per_round'])}"
    )
    print()

    print("generation timing:")
    print("  definition: summed latest-input to last-model-output trace time")
    print(
        "  total observable generation time: "
        f"{fmt_float(generation['total_observable_generation_time_seconds'])} s"
    )
    print(
        "  rounds with observable generation time: "
        f"{fmt_int(generation['rounds_with_observable_generation_time'])}"
    )
    print(
        "  p50 / p90 observable generation time: "
        f"{fmt_float(generation['p50_observable_generation_time_seconds'])} s / "
        f"{fmt_float(generation['p90_observable_generation_time_seconds'])} s"
    )
    print(
        "  average normalized decoding speed: "
        f"{fmt_float(generation['average_normalized_decoding_speed_tokens_per_second'])} "
        "tok/s"
    )
    print(
        "  rounds used for speed: "
        f"{fmt_int(generation['rounds_used_for_normalized_decoding_speed'])}"
    )
    print(
        "  total input-to-reasoning-end time: "
        f"{fmt_float(generation['total_input_to_reasoning_end_time_seconds'])} s"
    )
    print(
        "  rounds with input-to-reasoning-end time: "
        f"{fmt_int(generation['rounds_with_input_to_reasoning_end_time'])}"
    )
    print(
        "  average waiting for human input: "
        f"{fmt_float(generation['average_waiting_for_human_input_seconds'])} s "
        f"over {fmt_int(generation['rounds_with_waiting_for_human_input_time'])} "
        "user-started rounds"
    )
    print(
        "  median waiting for human input: "
        f"{fmt_float(generation['median_waiting_for_human_input_seconds'])} s"
    )
    print(
        "  p90 waiting for human input: "
        f"{fmt_float(generation['p90_waiting_for_human_input_seconds'])} s"
    )
    tpot = generation["post_reasoning_tpot_estimate"]
    print(
        "  post-reasoning TPOT estimate: "
        f"{fmt_float(tpot['average_decode_latency_seconds_per_token'], digits=4)} "
        "s/token "
        f"over {fmt_int(tpot['rounds'])} exact-reasoning rounds"
    )
    print(
        "  post-reasoning TPOT decode speed: "
        f"{fmt_float(tpot['average_decode_speed_tokens_per_second'])} tok/s"
    )
    ttft = generation["estimated_ttft_from_exact_reasoning_tokens"]
    print(
        "  estimated TTFT residual, exact-reasoning rounds: "
        f"{fmt_float(ttft['estimated_average_seconds'], digits=4)} s avg "
        f"over {fmt_int(ttft['rounds'])} rounds"
    )
    print(
        "  TTFT decode latency used: "
        f"{fmt_float(ttft['decode_latency_seconds_per_token_used'], digits=4)} "
        "s/token"
    )
    print()

    print("tools:")
    print(f"  total tool calls: {fmt_int(tools['total_tool_calls'])}")
    print(f"  rounds with tool calls: {fmt_int(tools['rounds_with_tool_calls'])}")
    print(
        "  tool calls / visible user-message round: "
        f"{fmt_float(tools['tool_calls_per_visible_user_message_round'])}"
    )
    print(
        "  total effective latency: "
        f"{fmt_float(tools['effective_latency']['total_seconds'])} s "
        f"({fmt_int(tools['effective_latency']['tool_calls_with_latency'])} calls with "
        f"latency, {fmt_int(tools['effective_latency']['tool_calls_missing_latency'])} "
        "missing, "
        f"{fmt_int(tools['effective_latency']['tool_calls_nonpositive_latency'])} "
        "nonpositive)"
    )
    print(
        "  p50 / p90 effective latency: "
        f"{fmt_float(tools['effective_latency']['p50_seconds'])} s / "
        f"{fmt_float(tools['effective_latency']['p90_seconds'])} s"
    )
    print(
        "  effective latency sources: "
        f"{fmt_int(tools['effective_latency']['tool_calls_using_internal_latency'])} "
        "internal, "
        f"{fmt_int(tools['effective_latency']['tool_calls_using_wall_latency_fallback'])} "
        "wall fallback"
    )
    print()
    print("rounds by provider:")
    for provider, count in data["rounds_by_provider"].items():
        print(f"  {provider}: {fmt_int(count)}")


def print_text(summary: SummaryBundle, path: Path) -> None:
    data = summary.as_dict()
    print(f"input: {path}")
    for index, key in enumerate(data):
        if index:
            print()
        print_one_summary(f"[{key}]", data[key])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    # `-i/--input` (JSONL, materialized to a temp DuckDB) and `--db` (prebuilt DuckDB) come from the
    # shared trace-db I/O surface; `-o/--output-dir` is added too but unused here (no files written).
    trace_db.add_db_args(parser, default_output_dir=SCRIPT_DIR)
    parser.add_argument("--json", action="store_true", help="Emit JSON summary.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # --db opens the prebuilt DuckDB directly; otherwise -i/--input materializes to a temp cache.
    # Both feed the identical DB-backed computation, so output is the same on every entry path.
    con = trace_db.open_from_args(args)
    try:
        summary = read_summary_from_db(con)
    finally:
        con.close()
    source = args.db if getattr(args, "db", None) is not None else args.input
    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
    else:
        print_text(summary, Path(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
