#!/usr/bin/env python3
"""Collect simplified timing-fit observations from normalized round traces.

The input is the normalized round trace, read from the shared DuckDB layer
(``artifacts/utils/trace_db.py``): one round per ``rounds`` row, with per-round
timing events in ``timing_events``. The output is a long-form CSV where each row
is one timing segment suitable for regression/model-fitting experiments.

This is the ROOT of the timing sub-chain — the CSV is consumed byte-for-byte by
the downstream timing experiments, so the per-segment values must match the
legacy JSONL collector exactly.

Segment definitions:

* Claude: latest input event -> final tool_call emission in the next assistant
  round. Claude usage only gives total message-level output tokens here, so the
  segment uses total output length. The input event can be a visible user
  message or a tool result, and is recorded in `segment_start_event`.
* Codex: latest input event -> final tool_call emission, mirroring the Claude
  full-turn segment. When a reasoning marker is available, the script also emits
  the split latest input event -> reasoning marker/end and reasoning marker/end
  -> final tool_call emission rows. Codex has exact reasoning-token accounting in
  the derived trace, so the input-to-reasoning segment uses reasoning tokens and
  the post-reasoning segment uses output_tokens - reasoning_output_tokens. The
  full-turn Codex segment uses total output tokens.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # timing_fit -> llm_generation -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
DEFAULT_OUTPUT = SCRIPT_DIR / "timing_fit_trace.csv"

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a raw
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string* — the int round-trips identically on both engines. We rebuild a naive datetime here,
# exactly (integer microseconds). The legacy JSONL path parsed the ISO strings into tz-aware
# (UTC) datetimes; both represent the same UTC instant, so durations and the rendered ISO strings
# (always serialized with a trailing ``Z``) match the pre-DuckDB path bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


CSV_FIELDS = [
    "provider",
    "model",
    "user",
    "session_id",
    "round_index",
    "round_id",
    "source_line",
    "segment_kind",
    "segment_start_event",
    "segment_end_event",
    "segment_start_at",
    "segment_end_at",
    "duration_ms",
    "cached_tokens",
    "append_tokens",
    "input_tokens_total",
    "prefix_hit_rate",
    "output_tokens_total",
    "reasoning_output_tokens",
    "visible_output_tokens",
    "segment_output_tokens",
    "tool_calls_in_round",
    "tool_errors_in_round",
]


@dataclass(frozen=True)
class TimedEvent:
    event_type: str
    timestamp: datetime


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def isoformat_z(value: datetime) -> str:
    # The DB rebuilds naive datetimes, so the legacy ``+00:00`` -> ``Z`` swap no longer applies;
    # the instant is UTC, so append ``Z`` directly. ``datetime.isoformat()`` already renders the
    # 6-digit microseconds (and omits the fractional part when microseconds == 0), matching the
    # legacy tz-aware ``isoformat().replace("+00:00", "Z")`` output exactly.
    return value.isoformat() + "Z"


def int_field(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return 0


def optional_int_field(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return None


def timing_events(row: dict[str, Any]) -> list[TimedEvent]:
    events = row.get("timing_events")
    if not isinstance(events, list):
        return []

    parsed: list[TimedEvent] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if not isinstance(event_type, str):
            continue
        timestamp = event.get("timestamp")
        if timestamp is None:
            continue
        parsed.append(TimedEvent(event_type=event_type, timestamp=timestamp))
    return parsed


def latest_event(
    events: list[TimedEvent],
    event_type: str,
    *,
    before_or_at: datetime | None = None,
) -> TimedEvent | None:
    candidates = [
        event
        for event in events
        if event.event_type == event_type
        and (before_or_at is None or event.timestamp <= before_or_at)
    ]
    return max(candidates, key=lambda event: event.timestamp) if candidates else None


def latest_event_after(
    events: list[TimedEvent],
    event_type: str,
    *,
    after_or_at: datetime,
) -> TimedEvent | None:
    candidates = [
        event
        for event in events
        if event.event_type == event_type and event.timestamp >= after_or_at
    ]
    return max(candidates, key=lambda event: event.timestamp) if candidates else None


def latest_input_event(
    events: list[TimedEvent],
    *,
    before_or_at: datetime,
) -> TimedEvent | None:
    candidates = [
        event
        for event in events
        if event.event_type in {"user_message", "tool_result"}
        and event.timestamp <= before_or_at
    ]
    return max(candidates, key=lambda event: event.timestamp) if candidates else None


def duration_ms(start: datetime, end: datetime) -> float | None:
    value = (end - start).total_seconds() * 1000
    return value if value > 0 else None


def tool_counts(row: dict[str, Any]) -> tuple[int, int]:
    tools = row.get("tools")
    if not isinstance(tools, list):
        return 0, 0
    calls = 0
    errors = 0
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        calls += 1
        if tool.get("is_error") is True:
            errors += 1
    return calls, errors


def base_output_fields(row: dict[str, Any]) -> dict[str, int | str]:
    cached_tokens = int_field(row, "prefix_tokens")
    append_tokens = int_field(row, "newly_append_tokens")
    input_tokens_total = int_field(row, "input_tokens_total")
    if input_tokens_total <= 0:
        input_tokens_total = cached_tokens + append_tokens

    output_tokens = int_field(row, "output_tokens")
    reasoning_tokens = optional_int_field(row, "reasoning_output_tokens")
    visible_tokens: int | str = ""
    if reasoning_tokens is not None:
        visible_tokens = max(0, output_tokens - reasoning_tokens)

    prefix_hit_rate: float | str = ""
    if input_tokens_total > 0:
        prefix_hit_rate = cached_tokens / input_tokens_total

    return {
        "cached_tokens": cached_tokens,
        "append_tokens": append_tokens,
        "input_tokens_total": input_tokens_total,
        "prefix_hit_rate": prefix_hit_rate,
        "output_tokens_total": output_tokens,
        "reasoning_output_tokens": "" if reasoning_tokens is None else reasoning_tokens,
        "visible_output_tokens": visible_tokens,
    }


def make_segment_row(
    row: dict[str, Any],
    *,
    source_line: int,
    segment_kind: str,
    start_event: TimedEvent,
    end_event: TimedEvent,
    segment_output_tokens: int,
) -> dict[str, Any] | None:
    elapsed_ms = duration_ms(start_event.timestamp, end_event.timestamp)
    if elapsed_ms is None:
        return None

    calls, errors = tool_counts(row)
    result: dict[str, Any] = {
        "provider": row.get("provider") or "",
        "model": row.get("model") or "",
        "user": row.get("user") or "",
        "session_id": row.get("session_id") or "",
        "round_index": row.get("round_index") if row.get("round_index") is not None else "",
        "round_id": row.get("round_id") or "",
        "source_line": source_line,
        "segment_kind": segment_kind,
        "segment_start_event": start_event.event_type,
        "segment_end_event": end_event.event_type,
        "segment_start_at": isoformat_z(start_event.timestamp),
        "segment_end_at": isoformat_z(end_event.timestamp),
        "duration_ms": round(elapsed_ms, 3),
        "segment_output_tokens": segment_output_tokens,
        "tool_calls_in_round": calls,
        "tool_errors_in_round": errors,
    }
    result.update(base_output_fields(row))
    return result


def iter_segment_rows(
    row: dict[str, Any],
    *,
    source_line: int,
) -> list[dict[str, Any]]:
    provider = row.get("provider")
    events = timing_events(row)
    if not events:
        return []

    output_tokens = int_field(row, "output_tokens")
    reasoning_tokens = optional_int_field(row, "reasoning_output_tokens")
    emitted: list[dict[str, Any]] = []

    if provider == "claude":
        tool_call = latest_event(events, "tool_call")
        if tool_call is None:
            return []
        input_event = latest_input_event(events, before_or_at=tool_call.timestamp)
        if input_event is None:
            return []
        segment = make_segment_row(
            row,
            source_line=source_line,
            segment_kind=f"claude_{input_event.event_type}_to_tool_call",
            start_event=input_event,
            end_event=tool_call,
            segment_output_tokens=output_tokens,
        )
        if segment is not None:
            emitted.append(segment)
        return emitted

    if provider == "codex":
        tool_call = latest_event(events, "tool_call")
        if tool_call is not None:
            input_event = latest_input_event(events, before_or_at=tool_call.timestamp)
            if input_event is not None:
                segment = make_segment_row(
                    row,
                    source_line=source_line,
                    segment_kind=f"codex_{input_event.event_type}_to_tool_call",
                    start_event=input_event,
                    end_event=tool_call,
                    segment_output_tokens=output_tokens,
                )
                if segment is not None:
                    emitted.append(segment)

        reasoning = latest_event(events, "reasoning")
        if reasoning is None:
            return emitted
        input_event = latest_input_event(events, before_or_at=reasoning.timestamp)
        if input_event is not None:
            segment = make_segment_row(
                row,
                source_line=source_line,
                segment_kind=f"codex_{input_event.event_type}_to_reasoning_end",
                start_event=input_event,
                end_event=reasoning,
                segment_output_tokens=max(0, reasoning_tokens or 0),
            )
            if segment is not None:
                emitted.append(segment)

        post_reasoning_tool_call = latest_event_after(
            events,
            "tool_call",
            after_or_at=reasoning.timestamp,
        )
        if post_reasoning_tool_call is not None:
            segment = make_segment_row(
                row,
                source_line=source_line,
                segment_kind="codex_reasoning_end_to_tool_call",
                start_event=reasoning,
                end_event=post_reasoning_tool_call,
                segment_output_tokens=max(0, output_tokens - (reasoning_tokens or 0)),
            )
            if segment is not None:
                emitted.append(segment)
        return emitted

    return []


def _load_rounds_from_db(con: "duckdb.DuckDBPyConnection") -> list[dict[str, Any]]:
    """Rebuild the per-round JSONL row shape the segment extractor consumes, from the trace DB.

    ``round_pk`` is the ingestion ordinal in file order, so it reproduces the legacy line-number
    ``source_line`` (the trace is one JSON object per line, no blank/garbage lines). Per-round
    timing events are pulled in list order (``event_index``) so ``max(...)``'s first-maximum
    tie-break over equal timestamps matches the legacy in-memory list exactly. Timestamps come
    back as epoch-microseconds (int) for native/wasm-identical marshalling, rebuilt to naive
    datetimes here. Tool calls are reduced to per-round (count, error-count) — the only thing
    ``tool_counts`` reads — keeping the legacy ``tools`` list-of-dicts shape that helper expects.
    """
    # Per-round timing events in list order. Skip events with a null event_type or null timestamp,
    # mirroring the legacy parser (which dropped non-str event_type / unparseable timestamps).
    timing_by_round: dict[int, list[dict[str, Any]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        if not isinstance(event_type, str) or ts_us is None:
            continue
        timing_by_round.setdefault(round_pk, []).append(
            {"event_type": event_type, "timestamp": _epoch_us_to_datetime(ts_us)}
        )

    # Per-round tool calls reduced to the (is_error is True) flags ``tool_counts`` inspects. The
    # tool_calls table is the UNNEST of each round's ``tools`` list, so one row per call; we
    # rebuild a minimal list-of-dicts so the legacy helper runs unchanged.
    tools_by_round: dict[int, list[dict[str, Any]]] = {}
    for round_pk, is_error in con.execute(
        "SELECT round_pk, is_error FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        tools_by_round.setdefault(round_pk, []).append({"is_error": is_error})

    rows: list[dict[str, Any]] = []
    for (
        round_pk,
        provider,
        model,
        user,
        session_id,
        round_index,
        round_id,
        prefix_tokens,
        newly_append_tokens,
        input_tokens_total,
        output_tokens,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, model, user, session_id, round_index, round_id, "
        "prefix_tokens, newly_append_tokens, input_tokens_total, output_tokens, "
        "reasoning_output_tokens "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        rows.append(
            {
                "source_line": round_pk,
                "provider": provider,
                "model": model,
                "user": user,
                "session_id": session_id,
                "round_index": round_index,
                "round_id": round_id,
                "prefix_tokens": prefix_tokens,
                "newly_append_tokens": newly_append_tokens,
                "input_tokens_total": input_tokens_total,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "timing_events": timing_by_round.get(round_pk, []),
                "tools": tools_by_round.get(round_pk, []),
            }
        )
    return rows


def collect_timing_fit_trace(con: "duckdb.DuckDBPyConnection", output_path: Path) -> Counter[str]:
    stats: Counter[str] = Counter()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = _load_rounds_from_db(con)

    with output_path.open("w", encoding="utf-8", newline="") as dst:
        writer = csv.DictWriter(dst, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for row in rows:
            stats["input_rows"] += 1
            source_line = row["source_line"]

            segment_rows = iter_segment_rows(row, source_line=source_line)
            if not segment_rows:
                stats[f"rounds_without_fit_segment.{row.get('provider') or 'unknown'}"] += 1
                continue

            stats[f"rounds_with_fit_segment.{row.get('provider') or 'unknown'}"] += 1
            for segment_row in segment_rows:
                writer.writerow(segment_row)
                stats["segments_written"] += 1
                stats[f"segments.{segment_row['segment_kind']}"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # This collector emits a single CSV FILE, so it keeps its own ``-o/--output`` (a file path)
    # rather than the shared ``-o/--output-dir``. From the trace_db surface we add only ``--db``
    # (prebuilt DuckDB, used by run_all's build-db) and ``-i/--input`` (a JSONL trace materialized
    # to a temp cache). run_all's timing-build invokes this as ``-i <jsonl> -o <timing_csv>``.
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="prebuilt DuckDB (from trace_db.materialize / run_all's build-db); skips materialize",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=DEFAULT_INPUT,
        help=f"normalized JSONL trace (materialized to a temp DuckDB if --db is not given; default: {DEFAULT_INPUT})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output timing-fit CSV file (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    stats = collect_timing_fit_trace(con, args.output)
    print(f"db={args.db}" if args.db is not None else f"input={args.input}")
    print(f"output={args.output}")
    for key in sorted(stats):
        print(f"{key}={stats[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
