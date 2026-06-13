#!/usr/bin/env python3
"""Decompose user-turn response time into generation/tool/residual totals.

Migrated onto the shared DuckDB layer (``artifacts/utils/trace_db.py``): instead of re-parsing the
normalized JSONL line by line, this reads round scalars + per-round timing events + per-round tool
latencies from the trace DB and replays the same stateful per-session decomposition over rounds in
ingestion order (``round_pk`` == file order). The result is byte-identical to the pre-DuckDB path.

This experiment is registered with ``style="global"`` in ``run_all.py`` and the web driver: the shim
imports this module, assigns ``module.INPUT = <path>``, then calls ``main()`` with no CLI flags. That
contract is preserved — ``INPUT`` stays a module-level default and ``main()`` falls back to it when no
``--db``/``-i`` is given — while the standard ``--db | -i/--input | -o`` CLI is also available.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402

# Module-level default input. The `style="global"` driver path assigns `module.INPUT = <path>` before
# calling `main()`; `main()` honors INPUT as the `-i` default when neither `--db` nor `-i` is passed.
INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
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


@dataclass
class ActiveTurn:
    provider: str
    start_at: datetime
    end_at: datetime | None = None
    last_model_output_at: datetime | None = None
    usage_report_at: datetime | None = None
    generation_seconds: float = 0.0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0
    rows: int = 0
    tool_calls: int = 0


@dataclass
class ProviderStats:
    turns: int = 0
    dropped_no_end: int = 0
    dropped_nonpositive: int = 0
    rows: int = 0
    tool_calls: int = 0
    e2e_seconds: float = 0.0
    generation_seconds: float = 0.0
    tool_effective_seconds: float = 0.0
    tool_wall_seconds: float = 0.0
    final_response_end_extra_seconds: float = 0.0
    usage_report_after_model_output_seconds: float = 0.0
    turns_with_usage_report_after_output: int = 0
    residual_effective_seconds: float = 0.0
    residual_wall_seconds: float = 0.0
    negative_residual_effective_turns: int = 0
    negative_residual_wall_turns: int = 0
    residual_effective_samples: list[float] = field(default_factory=list)
    residual_wall_samples: list[float] = field(default_factory=list)

    def add_turn(self, turn: ActiveTurn) -> None:
        if turn.end_at is None:
            self.dropped_no_end += 1
            return
        e2e = (turn.end_at - turn.start_at).total_seconds()
        if e2e <= 0:
            self.dropped_nonpositive += 1
            return
        final_extra = 0.0
        if turn.last_model_output_at is not None and turn.end_at > turn.last_model_output_at:
            final_extra = (turn.end_at - turn.last_model_output_at).total_seconds()
        usage_extra = 0.0
        if (
            turn.usage_report_at is not None
            and turn.last_model_output_at is not None
            and turn.usage_report_at > turn.last_model_output_at
        ):
            usage_extra = (turn.usage_report_at - turn.last_model_output_at).total_seconds()
        residual_effective = e2e - turn.generation_seconds - turn.tool_effective_seconds
        residual_wall = e2e - turn.generation_seconds - turn.tool_wall_seconds

        self.turns += 1
        self.rows += turn.rows
        self.tool_calls += turn.tool_calls
        self.e2e_seconds += e2e
        self.generation_seconds += turn.generation_seconds
        self.tool_effective_seconds += turn.tool_effective_seconds
        self.tool_wall_seconds += turn.tool_wall_seconds
        self.final_response_end_extra_seconds += final_extra
        self.residual_effective_seconds += residual_effective
        self.residual_wall_seconds += residual_wall
        self.usage_report_after_model_output_seconds += usage_extra
        if usage_extra > 0:
            self.turns_with_usage_report_after_output += 1
        self.residual_effective_samples.append(residual_effective)
        self.residual_wall_samples.append(residual_wall)
        if residual_effective < 0:
            self.negative_residual_effective_turns += 1
        if residual_wall < 0:
            self.negative_residual_wall_turns += 1


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def hours(seconds: float) -> float:
    return seconds / 3600


def fmt_h(seconds: float) -> str:
    return f"{hours(seconds):,.1f}h"


def fmt_s(seconds: float | None) -> str:
    return "" if seconds is None else f"{seconds:,.1f}s"


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
        if internal_ms is not None:
            effective = internal_ms / 1000
        elif wall_ms is not None:
            effective = wall_ms / 1000
        else:
            effective = None
        if effective is not None and effective > 0:
            agg.tool_effective_seconds += effective
        if wall_ms is not None:
            wall = wall_ms / 1000
            if wall > 0:
                agg.tool_wall_seconds += wall
    return aggregates


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Default `-i` to None so the module-level INPUT (which the `style="global"` driver assigns at
    # runtime, after import) is honored as the fallback inside main(); `add_db_args` still wires the
    # standard --db | -i/--input | -o surface.
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()
    if args.db is None and args.input == trace_db.DEFAULT_INPUT:
        # No explicit --db/-i: fall back to this module's INPUT (the global-driver contract).
        args.input = Path(INPUT)

    con = trace_db.open_from_args(args)

    events_by_round = load_timing_events(con)
    tools_by_round = load_round_tools(con)

    active_by_session: dict[str, ActiveTurn] = {}
    stats: defaultdict[str, ProviderStats] = defaultdict(ProviderStats)
    user_triggered: Counter[str] = Counter()

    def add_to_all(turn: ActiveTurn) -> None:
        stats[turn.provider].add_turn(turn)
        stats["all"].add_turn(turn)

    def close_turn(session_id: str) -> None:
        turn = active_by_session.pop(session_id, None)
        if turn is not None:
            add_to_all(turn)

    # Single pass over rounds in ingestion order (round_pk == file order), reproducing the old
    # line-by-line loader's per-session state machine exactly.
    for round_pk, provider_value, session_id_value in con.execute(
        "SELECT round_pk, provider, session_id FROM rounds ORDER BY round_pk"
    ).fetchall():
        events = events_by_round.get(round_pk, [])
        provider = str(provider_value) if provider_value else "<unknown-provider>"
        session_id = session_id_value if isinstance(session_id_value, str) else None

        user_start_at = response_trigger_user_message_timestamp(events)
        if user_start_at is not None and session_id is not None:
            close_turn(session_id)
            user_triggered[provider] += 1
            active_by_session[session_id] = ActiveTurn(
                provider=provider,
                start_at=user_start_at,
            )

        turn = active_by_session.get(session_id) if session_id is not None else None
        if turn is None:
            continue
        turn.rows += 1
        generation = input_to_last_output_span_seconds(events)
        if generation is not None:
            turn.generation_seconds += generation
        model_output_at = last_model_output_timestamp(events)
        if model_output_at is not None and (
            turn.last_model_output_at is None
            or model_output_at > turn.last_model_output_at
        ):
            turn.last_model_output_at = model_output_at
        response_end_at = last_response_end_timestamp(events)
        if response_end_at is not None and (
            turn.end_at is None or response_end_at > turn.end_at
        ):
            turn.end_at = response_end_at
        usage_report_at = timestamps_for(events, USAGE_REPORT_EVENT_TYPES)
        if usage_report_at:
            last_usage_report_at = max(usage_report_at)
            if (
                turn.usage_report_at is None
                or last_usage_report_at > turn.usage_report_at
            ):
                turn.usage_report_at = last_usage_report_at

        round_tools = tools_by_round.get(round_pk)
        if round_tools is not None:
            turn.tool_calls += round_tools.tool_calls
            turn.tool_effective_seconds += round_tools.tool_effective_seconds
            turn.tool_wall_seconds += round_tools.tool_wall_seconds

    for session_id in list(active_by_session):
        close_turn(session_id)

    out_md = Path(getattr(args, "output_dir", None) or EXP_DIR) / "result_analysis.md"
    input_label = str(args.db) if args.db is not None else str(Path(args.input).resolve())

    providers = ["all", *sorted(provider for provider in stats if provider != "all")]
    lines = [
        "# User Turn Decomposition Audit",
        "",
        f"Input: `{input_label}`",
        "",
        "This audit uses the same user-turn response-time definition as the summary:",
        "response-triggering `user_message` to the last response-end event before the next same-session response-triggering `user_message`.",
        "Response-end events are model output events (`reasoning`, `text`, or `tool_call`); Codex `usage_report` is tracked separately and is not part of e2e response time.",
        "",
        "The residual is a subtraction diagnostic, not a semantic state:",
        "`e2e - generation - tool_latency`. It is reported both with effective tool latency and wall tool latency.",
        "",
        "## Provider Totals",
        "",
        "| provider | response-triggering user rows | sampled turns | e2e | generation | tool effective | tool wall | residual effective | residual wall | usage-report-after-output | turns with usage gap |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for provider in providers:
        item = stats[provider]
        started = sum(user_triggered.values()) if provider == "all" else user_triggered[provider]
        lines.append(
            f"| {provider} | {started:,} | {item.turns:,} | {fmt_h(item.e2e_seconds)} | "
            f"{fmt_h(item.generation_seconds)} | {fmt_h(item.tool_effective_seconds)} | "
            f"{fmt_h(item.tool_wall_seconds)} | {fmt_h(item.residual_effective_seconds)} | "
            f"{fmt_h(item.residual_wall_seconds)} | "
            f"{fmt_h(item.usage_report_after_model_output_seconds)} | "
            f"{item.turns_with_usage_report_after_output:,} |"
        )

    lines.extend(
        [
            "",
            "## Per-Turn Residual Distribution",
            "",
            "| provider | residual kind | p25 | p50 | p90 | avg | negative turns |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for provider in providers:
        item = stats[provider]
        avg_eff = (
            item.residual_effective_seconds / item.turns if item.turns else None
        )
        avg_wall = item.residual_wall_seconds / item.turns if item.turns else None
        lines.append(
            f"| {provider} | effective | "
            f"{fmt_s(percentile(item.residual_effective_samples, 0.25))} | "
            f"{fmt_s(percentile(item.residual_effective_samples, 0.50))} | "
            f"{fmt_s(percentile(item.residual_effective_samples, 0.90))} | "
            f"{fmt_s(avg_eff)} | {item.negative_residual_effective_turns:,} |"
        )
        lines.append(
            f"| {provider} | wall | "
            f"{fmt_s(percentile(item.residual_wall_samples, 0.25))} | "
            f"{fmt_s(percentile(item.residual_wall_samples, 0.50))} | "
            f"{fmt_s(percentile(item.residual_wall_samples, 0.90))} | "
            f"{fmt_s(avg_wall)} | {item.negative_residual_wall_turns:,} |"
        )

    lines.extend(
        [
            "",
            "## Dropped Turns",
            "",
            "| provider | no response end | nonpositive duration |",
            "|---|---:|---:|",
        ]
    )
    for provider in providers:
        item = stats[provider]
        lines.append(
            f"| {provider} | {item.dropped_no_end:,} | {item.dropped_nonpositive:,} |"
        )

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(out_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
