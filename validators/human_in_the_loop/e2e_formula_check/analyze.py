#!/usr/bin/env python3
"""Check whether user-turn e2e is explained by average generation/tool cost."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]  # experiment -> category -> artifacts -> repo root
INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
OUT_MD = Path(__file__).with_name("result_analysis.md")

INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}


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


def response_trigger_user_message_timestamp(row: dict[str, Any]) -> datetime | None:
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events(row):
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
    return max(candidate_users) if candidate_users else None


def timestamps_for(row: dict[str, Any], event_types: set[str]) -> list[datetime]:
    out: list[datetime] = []
    for event in events(row):
        if event.get("event_type") not in event_types:
            continue
        timestamp = parse_ts(event.get("timestamp"))
        if timestamp is not None:
            out.append(timestamp)
    return out


def last_response_end_timestamp(row: dict[str, Any]) -> datetime | None:
    values = timestamps_for(row, MODEL_OUTPUT_EVENT_TYPES)
    return max(values) if values else None


def input_to_last_output_span_seconds(row: dict[str, Any]) -> float | None:
    input_timestamps = timestamps_for(row, INPUT_EVENT_TYPES)
    output_timestamps = timestamps_for(row, MODEL_OUTPUT_EVENT_TYPES)
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


def tool_effective_latency_seconds(tool: dict[str, Any]) -> float | None:
    internal = safe_float(tool.get("tool_internal_latency_ms"))
    if internal is not None:
        return internal / 1000
    wall = safe_float(tool.get("tool_wall_latency_ms"))
    if wall is not None:
        return wall / 1000
    fallback = safe_float(tool.get("latency_ms"))
    return fallback / 1000 if fallback is not None else None


def tool_wall_latency_seconds(tool: dict[str, Any]) -> float | None:
    wall = safe_float(tool.get("tool_wall_latency_ms"))
    return wall / 1000 if wall is not None else None


@dataclass
class ActiveTurn:
    provider: str
    start_at: datetime
    end_at: datetime | None = None
    rows: int = 0
    generation_rows: int = 0
    generation_seconds: float = 0.0
    tool_calls: int = 0
    tool_effective_calls: int = 0
    tool_wall_calls: int = 0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0


@dataclass
class Stats:
    triggers: int = 0
    turns: int = 0
    dropped_nonpositive: int = 0
    rows: int = 0
    generation_rows: int = 0
    tool_calls: int = 0
    tool_effective_calls: int = 0
    tool_wall_calls: int = 0
    e2e_seconds: float = 0.0
    generation_seconds: float = 0.0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0

    def add_turn(self, turn: ActiveTurn) -> None:
        if turn.end_at is None:
            return
        e2e = (turn.end_at - turn.start_at).total_seconds()
        if e2e <= 0:
            self.dropped_nonpositive += 1
            return
        self.turns += 1
        self.rows += turn.rows
        self.generation_rows += turn.generation_rows
        self.tool_calls += turn.tool_calls
        self.tool_effective_calls += turn.tool_effective_calls
        self.tool_wall_calls += turn.tool_wall_calls
        self.e2e_seconds += e2e
        self.generation_seconds += turn.generation_seconds
        self.tool_effective_seconds += turn.tool_effective_seconds
        self.tool_wall_seconds += turn.tool_wall_seconds


def div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else float("nan")


def fmt_seconds(value: float) -> str:
    if not math.isfinite(value):
        return ""
    if abs(value) < 60:
        return f"{value:.1f}s"
    if abs(value) < 3600:
        return f"{value / 60:.2f}m"
    return f"{value / 3600:.2f}h"


def fmt_float(value: float) -> str:
    return "" if not math.isfinite(value) else f"{value:.2f}"


def main() -> int:
    active_by_session: dict[str, ActiveTurn] = {}
    stats: defaultdict[str, Stats] = defaultdict(Stats)

    def close_turn(session_id: str) -> None:
        turn = active_by_session.pop(session_id, None)
        if turn is None:
            return
        stats[turn.provider].add_turn(turn)
        stats["all"].add_turn(turn)

    with INPUT.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            provider = str(row.get("provider") or "<unknown-provider>")
            session_id_value = row.get("session_id")
            session_id = session_id_value if isinstance(session_id_value, str) else None
            trigger_at = response_trigger_user_message_timestamp(row)
            if trigger_at is not None and session_id is not None:
                close_turn(session_id)
                stats[provider].triggers += 1
                stats["all"].triggers += 1
                active_by_session[session_id] = ActiveTurn(provider=provider, start_at=trigger_at)

            turn = active_by_session.get(session_id) if session_id is not None else None
            if turn is None:
                continue
            turn.rows += 1
            generation_seconds = input_to_last_output_span_seconds(row)
            if generation_seconds is not None:
                turn.generation_rows += 1
                turn.generation_seconds += generation_seconds
            response_end = last_response_end_timestamp(row)
            if response_end is not None and (turn.end_at is None or response_end > turn.end_at):
                turn.end_at = response_end

            tools = row.get("tools")
            if isinstance(tools, list):
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    turn.tool_calls += 1
                    effective = tool_effective_latency_seconds(tool)
                    if effective is not None and effective > 0:
                        turn.tool_effective_calls += 1
                        turn.tool_effective_seconds += effective
                    wall = tool_wall_latency_seconds(tool)
                    if wall is not None and wall > 0:
                        turn.tool_wall_calls += 1
                        turn.tool_wall_seconds += wall

    for session_id in list(active_by_session):
        close_turn(session_id)

    providers = ["all", "claude", "codex"]
    lines = [
        "# E2E Average Formula Check",
        "",
        f"Input: `{INPUT}`",
        "",
        "Corrected user-turn window: latest `user_message` before first model output, through final model output before the next such user message.",
        "",
        "The row formula is `(generation per in-turn LLM row + effective tool latency per in-turn LLM row) * rows per sampled turn`.",
        "The component formula is `generation per generation row * generation rows per turn + effective tool latency per tool call * tool calls per turn`.",
        "",
        "## Effective Tool Latency",
        "",
        "| provider | turns | avg e2e | rows/turn | gen/row | tool eff/row | row formula | row error | gen rows/turn | gen/gen-row | tool calls/turn | tool eff/call | component formula | component error |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in providers:
        item = stats[provider]
        avg_e2e = div(item.e2e_seconds, item.turns)
        rows_per_turn = div(item.rows, item.turns)
        gen_per_row = div(item.generation_seconds, item.rows)
        tool_eff_per_row = div(item.tool_effective_seconds, item.rows)
        row_formula = (gen_per_row + tool_eff_per_row) * rows_per_turn
        gen_rows_per_turn = div(item.generation_rows, item.turns)
        gen_per_gen_row = div(item.generation_seconds, item.generation_rows)
        tool_calls_per_turn = div(item.tool_effective_calls, item.turns)
        tool_eff_per_call = div(item.tool_effective_seconds, item.tool_effective_calls)
        component_formula = (
            gen_per_gen_row * gen_rows_per_turn
            + tool_eff_per_call * tool_calls_per_turn
        )
        lines.append(
            f"| {provider} | {item.turns:,} | {fmt_seconds(avg_e2e)} | "
            f"{fmt_float(rows_per_turn)} | {fmt_seconds(gen_per_row)} | "
            f"{fmt_seconds(tool_eff_per_row)} | {fmt_seconds(row_formula)} | "
            f"{fmt_seconds(row_formula - avg_e2e)} | {fmt_float(gen_rows_per_turn)} | "
            f"{fmt_seconds(gen_per_gen_row)} | {fmt_float(tool_calls_per_turn)} | "
            f"{fmt_seconds(tool_eff_per_call)} | {fmt_seconds(component_formula)} | "
            f"{fmt_seconds(component_formula - avg_e2e)} |"
        )

    lines.extend(
        [
            "",
            "## Wall Tool Latency",
            "",
            "| provider | turns | avg e2e | rows/turn | gen/row | tool wall/row | row formula | row error | tool wall calls/turn | tool wall/call | component formula | component error |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for provider in providers:
        item = stats[provider]
        avg_e2e = div(item.e2e_seconds, item.turns)
        rows_per_turn = div(item.rows, item.turns)
        gen_per_row = div(item.generation_seconds, item.rows)
        tool_wall_per_row = div(item.tool_wall_seconds, item.rows)
        row_formula = (gen_per_row + tool_wall_per_row) * rows_per_turn
        gen_rows_per_turn = div(item.generation_rows, item.turns)
        gen_per_gen_row = div(item.generation_seconds, item.generation_rows)
        tool_wall_calls_per_turn = div(item.tool_wall_calls, item.turns)
        tool_wall_per_call = div(item.tool_wall_seconds, item.tool_wall_calls)
        component_formula = (
            gen_per_gen_row * gen_rows_per_turn
            + tool_wall_per_call * tool_wall_calls_per_turn
        )
        lines.append(
            f"| {provider} | {item.turns:,} | {fmt_seconds(avg_e2e)} | "
            f"{fmt_float(rows_per_turn)} | {fmt_seconds(gen_per_row)} | "
            f"{fmt_seconds(tool_wall_per_row)} | {fmt_seconds(row_formula)} | "
            f"{fmt_seconds(row_formula - avg_e2e)} | {fmt_float(tool_wall_calls_per_turn)} | "
            f"{fmt_seconds(tool_wall_per_call)} | {fmt_seconds(component_formula)} | "
            f"{fmt_seconds(component_formula - avg_e2e)} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- With effective tool latency, the aggregate average is explained closely by generation plus tool latency. The all-provider row overshoots actual e2e by about 2 seconds per sampled turn.",
            "- With wall tool latency, the formula overshoots more because parallel/overlapping tool waits are summed additively.",
            "- Do not use `average tool latency per tool call + average generation latency per LLM row`, multiplied by rows per turn. Those averages have different denominators; the tool term must be multiplied by tool calls per turn, or normalized to per in-turn LLM row first.",
        ]
    )
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
