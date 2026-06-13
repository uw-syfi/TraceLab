"""Shared helpers for same-session total-input length growth analysis."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


MICRO_REDUCTION_MAX_TOKENS = 1_024
MAJOR_COMPACT_MIN_TOKENS = 50_000
TRIGGER_LABELS = {
    "user_message": "user",
    "tool_result": "tool_result",
}
SUMMARY_TRIGGERS = ("all", "user", "tool_result")
SUMMARY_COLUMNS = [
    "scope",
    "trigger",
    "rounds",
    "positive",
    "positive_pct",
    "zero",
    "zero_pct",
    "negative",
    "negative_pct",
    "micro_reduction",
    "micro_pct",
    "ordinary_reduction",
    "ordinary_pct",
    "major_compact",
    "major_pct",
    "total_context_increase",
    "avg_raw_delta",
    "p10_raw_delta",
    "median_raw_delta",
    "p90_raw_delta",
    "avg_positive_growth",
    "avg_clipped_growth",
    "avg_reduction",
    "max_reduction",
]
EVENT_COLUMNS = [
    "provider",
    "trigger",
    "bucket",
    "raw_delta_tokens",
    "reduction_tokens",
    "session_id",
    "previous_round_index",
    "current_round_index",
    "previous_total_input_tokens",
    "current_total_input_tokens",
    "previous_prefix_tokens",
    "current_prefix_tokens",
    "prefix_delta_tokens",
    "previous_newly_append_tokens",
    "current_newly_append_tokens",
    "append_delta_tokens",
    "previous_first_event_type",
    "current_first_event_type",
    "previous_model",
    "current_model",
    "previous_timestamp",
    "current_timestamp",
    "previous_trace_key",
    "current_trace_key",
    "previous_line_number",
    "current_line_number",
]


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


def int_field(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def first_timing_event_type(row: dict[str, Any]) -> str | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        return event_type if isinstance(event_type, str) else None
    return None


def first_timing_event_timestamp(row: dict[str, Any]) -> str | None:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return None
    for event in events:
        if not isinstance(event, dict):
            continue
        timestamp = event.get("timestamp")
        return timestamp if isinstance(timestamp, str) else None
    return None


def row_total_input_tokens(row: dict[str, Any]) -> int:
    return int_field(row, "prefix_tokens") + int_field(row, "newly_append_tokens")


def reduction_bucket(
    raw_delta_tokens: int,
    *,
    micro_reduction_max_tokens: int = MICRO_REDUCTION_MAX_TOKENS,
    major_compact_min_tokens: int = MAJOR_COMPACT_MIN_TOKENS,
) -> str:
    if raw_delta_tokens > 0:
        return "positive"
    if raw_delta_tokens == 0:
        return "zero"

    reduction_tokens = -raw_delta_tokens
    if reduction_tokens <= micro_reduction_max_tokens:
        return "micro_reduction"
    if reduction_tokens >= major_compact_min_tokens:
        return "major_compact"
    return "ordinary_reduction"


@dataclass
class InputGrowthStats:
    micro_reduction_max_tokens: int = MICRO_REDUCTION_MAX_TOKENS
    major_compact_min_tokens: int = MAJOR_COMPACT_MIN_TOKENS
    rounds: int = 0
    positive_growth_rounds: int = 0
    zero_growth_rounds: int = 0
    micro_reduction_rounds: int = 0
    ordinary_reduction_rounds: int = 0
    major_compact_rounds: int = 0
    total_raw_delta_tokens: int = 0
    total_positive_growth_tokens: int = 0
    total_reduction_tokens: int = 0
    max_reduction_tokens: int = 0
    raw_delta_tokens: list[float] = field(default_factory=list)

    def add(self, raw_delta_tokens: int) -> None:
        self.rounds += 1
        self.total_raw_delta_tokens += raw_delta_tokens
        self.raw_delta_tokens.append(float(raw_delta_tokens))

        bucket = reduction_bucket(
            raw_delta_tokens,
            micro_reduction_max_tokens=self.micro_reduction_max_tokens,
            major_compact_min_tokens=self.major_compact_min_tokens,
        )
        if bucket == "positive":
            self.positive_growth_rounds += 1
            self.total_positive_growth_tokens += raw_delta_tokens
            return
        if bucket == "zero":
            self.zero_growth_rounds += 1
            return

        reduction_tokens = -raw_delta_tokens
        self.total_reduction_tokens += reduction_tokens
        self.max_reduction_tokens = max(self.max_reduction_tokens, reduction_tokens)
        if bucket == "micro_reduction":
            self.micro_reduction_rounds += 1
        elif bucket == "major_compact":
            self.major_compact_rounds += 1
        else:
            self.ordinary_reduction_rounds += 1

    @property
    def negative_growth_rounds(self) -> int:
        return (
            self.micro_reduction_rounds
            + self.ordinary_reduction_rounds
            + self.major_compact_rounds
        )

    def share(self, count: int) -> float | None:
        return count / self.rounds if self.rounds else None

    def average(self, total: int, count: int) -> float | None:
        return total / count if count else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "positive_growth_rounds": self.positive_growth_rounds,
            "zero_growth_rounds": self.zero_growth_rounds,
            "negative_growth_rounds": self.negative_growth_rounds,
            "micro_reduction_rounds": self.micro_reduction_rounds,
            "ordinary_reduction_rounds": self.ordinary_reduction_rounds,
            "major_compact_rounds": self.major_compact_rounds,
            "positive_growth_share": self.share(self.positive_growth_rounds),
            "zero_growth_share": self.share(self.zero_growth_rounds),
            "negative_growth_share": self.share(self.negative_growth_rounds),
            "micro_reduction_share": self.share(self.micro_reduction_rounds),
            "ordinary_reduction_share": self.share(self.ordinary_reduction_rounds),
            "major_compact_share": self.share(self.major_compact_rounds),
            "total_context_increase_tokens": self.total_positive_growth_tokens,
            "total_raw_delta_tokens": self.total_raw_delta_tokens,
            "total_reduction_tokens": (
                self.total_reduction_tokens if self.negative_growth_rounds else None
            ),
            "average_raw_delta_tokens": self.average(
                self.total_raw_delta_tokens, self.rounds
            ),
            "median_raw_delta_tokens": percentile(self.raw_delta_tokens, 0.50),
            "p10_raw_delta_tokens": percentile(self.raw_delta_tokens, 0.10),
            "p90_raw_delta_tokens": percentile(self.raw_delta_tokens, 0.90),
            "average_positive_growth_tokens": self.average(
                self.total_positive_growth_tokens, self.positive_growth_rounds
            ),
            "average_clipped_growth_tokens": self.average(
                self.total_positive_growth_tokens, self.rounds
            ),
            "average_reduction_tokens": self.average(
                self.total_reduction_tokens, self.negative_growth_rounds
            ),
            "max_reduction_tokens": (
                self.max_reduction_tokens if self.negative_growth_rounds else None
            ),
        }


def _round_snapshot(row: dict[str, Any], line_number: int) -> dict[str, Any]:
    return {
        "line_number": line_number,
        "round_index": row.get("round_index"),
        "total_input_tokens": row_total_input_tokens(row),
        "prefix_tokens": int_field(row, "prefix_tokens"),
        "newly_append_tokens": int_field(row, "newly_append_tokens"),
        "first_event_type": first_timing_event_type(row),
        "model": row.get("model"),
        "timestamp": first_timing_event_timestamp(row),
        "trace_key": row.get("trace_key"),
    }


def iter_growth_events(path: Path) -> Iterator[dict[str, Any]]:
    last_by_session: dict[str, dict[str, Any]] = {}

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue

            session_id = row.get("session_id")
            current_first_event_type = first_timing_event_type(row)
            current_total_input_tokens = row_total_input_tokens(row)
            if (
                isinstance(session_id, str)
                and session_id in last_by_session
                and current_first_event_type in TRIGGER_LABELS
            ):
                previous = last_by_session[session_id]
                raw_delta_tokens = (
                    current_total_input_tokens - previous["total_input_tokens"]
                )
                bucket = reduction_bucket(raw_delta_tokens)
                yield {
                    "provider": row.get("provider") or "unknown",
                    "trigger": TRIGGER_LABELS[current_first_event_type],
                    "bucket": bucket,
                    "raw_delta_tokens": raw_delta_tokens,
                    "reduction_tokens": (
                        -raw_delta_tokens if raw_delta_tokens < 0 else 0
                    ),
                    "session_id": session_id,
                    "previous_round_index": previous["round_index"],
                    "current_round_index": row.get("round_index"),
                    "previous_total_input_tokens": previous["total_input_tokens"],
                    "current_total_input_tokens": current_total_input_tokens,
                    "previous_prefix_tokens": previous["prefix_tokens"],
                    "current_prefix_tokens": int_field(row, "prefix_tokens"),
                    "prefix_delta_tokens": (
                        int_field(row, "prefix_tokens") - previous["prefix_tokens"]
                    ),
                    "previous_newly_append_tokens": previous["newly_append_tokens"],
                    "current_newly_append_tokens": int_field(
                        row, "newly_append_tokens"
                    ),
                    "append_delta_tokens": (
                        int_field(row, "newly_append_tokens")
                        - previous["newly_append_tokens"]
                    ),
                    "previous_first_event_type": previous["first_event_type"],
                    "current_first_event_type": current_first_event_type,
                    "previous_model": previous["model"],
                    "current_model": row.get("model"),
                    "previous_timestamp": previous["timestamp"],
                    "current_timestamp": first_timing_event_timestamp(row),
                    "previous_trace_key": previous["trace_key"],
                    "current_trace_key": row.get("trace_key"),
                    "previous_line_number": previous["line_number"],
                    "current_line_number": line_number,
                }

            if isinstance(session_id, str):
                last_by_session[session_id] = _round_snapshot(row, line_number)


def build_growth_stats(
    events: Iterable[dict[str, Any]],
    *,
    micro_reduction_max_tokens: int = MICRO_REDUCTION_MAX_TOKENS,
    major_compact_min_tokens: int = MAJOR_COMPACT_MIN_TOKENS,
) -> dict[tuple[str, str], InputGrowthStats]:
    stats: dict[tuple[str, str], InputGrowthStats] = defaultdict(
        lambda: InputGrowthStats(
            micro_reduction_max_tokens=micro_reduction_max_tokens,
            major_compact_min_tokens=major_compact_min_tokens,
        )
    )
    for event in events:
        provider = str(event.get("provider") or "unknown")
        trigger = str(event.get("trigger") or "unknown")
        raw_delta_tokens = int(event.get("raw_delta_tokens") or 0)
        for scope in ("merged", provider):
            stats[(scope, trigger)].add(raw_delta_tokens)
            stats[(scope, "all")].add(raw_delta_tokens)
    return dict(stats)


def pct_string(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.2f}%"


def blank_if_none(value: Any) -> Any:
    return "" if value is None else value


def summary_row(scope: str, trigger: str, stats: InputGrowthStats) -> dict[str, Any]:
    data = stats.as_dict()
    return {
        "scope": scope,
        "trigger": trigger,
        "rounds": data["rounds"],
        "positive": data["positive_growth_rounds"],
        "positive_pct": pct_string(data["positive_growth_share"]),
        "zero": data["zero_growth_rounds"],
        "zero_pct": pct_string(data["zero_growth_share"]),
        "negative": data["negative_growth_rounds"],
        "negative_pct": pct_string(data["negative_growth_share"]),
        "micro_reduction": data["micro_reduction_rounds"],
        "micro_pct": pct_string(data["micro_reduction_share"]),
        "ordinary_reduction": data["ordinary_reduction_rounds"],
        "ordinary_pct": pct_string(data["ordinary_reduction_share"]),
        "major_compact": data["major_compact_rounds"],
        "major_pct": pct_string(data["major_compact_share"]),
        "total_context_increase": data["total_context_increase_tokens"],
        "avg_raw_delta": blank_if_none(data["average_raw_delta_tokens"]),
        "p10_raw_delta": blank_if_none(data["p10_raw_delta_tokens"]),
        "median_raw_delta": blank_if_none(data["median_raw_delta_tokens"]),
        "p90_raw_delta": blank_if_none(data["p90_raw_delta_tokens"]),
        "avg_positive_growth": blank_if_none(data["average_positive_growth_tokens"]),
        "avg_clipped_growth": blank_if_none(data["average_clipped_growth_tokens"]),
        "avg_reduction": blank_if_none(data["average_reduction_tokens"]),
        "max_reduction": blank_if_none(data["max_reduction_tokens"]),
    }


def write_summary_csv(
    path: Path,
    stats: dict[tuple[str, str], InputGrowthStats],
    *,
    scopes: Iterable[str] | None = None,
) -> None:
    if scopes is None:
        providers = sorted({scope for scope, _ in stats if scope != "merged"})
        scopes = ["merged", *providers]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for scope in scopes:
            for trigger in SUMMARY_TRIGGERS:
                row_stats = stats.get((scope, trigger), InputGrowthStats())
                writer.writerow(summary_row(scope, trigger, row_stats))


def write_events_csv(path: Path, events: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for event in events:
            writer.writerow(event)
            count += 1
    return count
