#!/usr/bin/env python3
"""Per user-initiated turn end-to-end response time summary, by provider.

Response time = response-triggering user_message -> last response-end event before the
next response-triggering user_message. See README.md for the exact definition.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from typing import Any  # noqa: E402
import csv  # noqa: E402
import numpy as np  # noqa: E402
from style import provider_order  # noqa: E402
from accumulators import sample_percentiles  # noqa: E402
import trace_db  # noqa: E402

# Same event-type partitions as the pre-DuckDB timing helper (artifacts/utils/timing.py).
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
# last_response_end_timestamp uses RESPONSE_END_EVENT_TYPES == MODEL_OUTPUT_EVENT_TYPES.
RESPONSE_END_EVENT_TYPES = MODEL_OUTPUT_EVENT_TYPES

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines. Rebuild the naive datetime here exactly
# (integer microseconds). The old path parsed ISO strings to tz-aware datetimes, but a difference
# between two same-tz datetimes equals the naive epoch_us difference to the microsecond, so the
# durations match the pre-DuckDB result bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def _response_trigger_user_message_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    """Reproduce timing.response_trigger_user_message_timestamp for one round's events.

    Collect this round's user_message timestamps and model-output (reasoning/text/tool_call)
    timestamps. If either list is empty -> None. Otherwise take the earliest output as
    first_output; keep only user inputs at-or-before first_output as candidates; if none -> None;
    else return the latest such candidate.
    """
    user_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event_type, ts in events:
        if ts is None:
            continue
        if event_type == "user_message":
            user_timestamps.append(ts)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(ts)
    if not user_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_users = [ts for ts in user_timestamps if ts <= first_output_at]
    if not candidate_users:
        return None
    return max(candidate_users)


def _last_response_end_timestamp(
    events: list[tuple[str | None, datetime | None]],
) -> datetime | None:
    """Reproduce timing.last_response_end_timestamp: latest model-output timestamp, or None."""
    timestamps = [
        ts
        for event_type, ts in events
        if event_type in RESPONSE_END_EVENT_TYPES and ts is not None
    ]
    return max(timestamps) if timestamps else None


def load_user_turn_response_seconds_by_provider(
    con: "duckdb.DuckDBPyConnection",
) -> dict[str, list[float]]:
    """``{"all": [...], provider: [...]}`` — per-session user-turn response times.

    Stateful, single-pass over rounds in ingestion order (``round_pk`` == file order), reproducing
    the old single-pass loader's user-turn state machine exactly. State is
    ``current_user_turn_by_session`` mapping ``session_id -> {provider, start_at, last_output_at}``.

    ``close_user_turn(session_id)`` pops the session's open turn; if it exists with both ``start_at``
    and ``last_output_at`` set and ``dur = (last_output_at - start_at).total_seconds() > 0``, that
    duration is appended to ``"all"`` and to the turn's provider bucket.

    For each round in order:
      1. ``start`` = response-trigger user_message timestamp (None if absent). If ``start`` is not
         None and the session_id is a str, close any open turn for that session, then open a fresh
         turn ``{provider, start_at: start, last_output_at: None}``.
      2. ``resp_end`` = last response-end (model-output) timestamp for the round. If the session_id
         is a str and ``resp_end`` is not None, and the session has an open turn, advance its
         ``last_output_at`` when it is unset or ``resp_end`` is strictly later.
    After all rounds, ``close_user_turn`` is called for every still-open session (end-of-stream
    flush) in dict-insertion order, matching the loader's ``for session_id in list(...)`` flush.
    The full list is kept per provider — no sampling — so the CDF/percentiles are exact.
    """
    # Per-round timing events (epoch-microsecond ints, rebuilt to naive datetimes). Event order
    # within a round is irrelevant: the per-round helpers only use min/max over the typed lists.
    events_by_round: dict[int, list[tuple[str | None, datetime | None]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        events_by_round.setdefault(round_pk, []).append(
            (event_type, _epoch_us_to_datetime(ts_us))
        )

    by_provider: dict[str, list[float]] = {"all": []}
    current_user_turn_by_session: dict[str, dict[str, Any]] = {}

    def close_user_turn(session_id: str) -> None:
        state = current_user_turn_by_session.pop(session_id, None)
        if not state:
            return
        start_at = state.get("start_at")
        last_output_at = state.get("last_output_at")
        provider = state.get("provider")
        if not isinstance(start_at, datetime) or not isinstance(last_output_at, datetime):
            return
        if not isinstance(provider, str):
            provider = "<unknown-provider>"
        duration_seconds = (last_output_at - start_at).total_seconds()
        if duration_seconds <= 0:
            return
        by_provider["all"].append(duration_seconds)
        by_provider.setdefault(provider, []).append(duration_seconds)

    # Per-round (session_id, provider) in ingestion order so the stateful walk and the per-provider
    # append order match the old line-by-line loader exactly.
    for round_pk, session_id, provider in con.execute(
        "SELECT round_pk, session_id, provider FROM rounds ORDER BY round_pk"
    ).fetchall():
        events = events_by_round.get(round_pk, [])
        # Matches the loader's `str(row.get("provider") or "<unknown-provider>")` (line 152).
        provider_key = str(provider) if provider else "<unknown-provider>"

        start = _response_trigger_user_message_timestamp(events)
        if start is not None and isinstance(session_id, str):
            close_user_turn(session_id)
            current_user_turn_by_session[session_id] = {
                "provider": provider_key,
                "start_at": start,
                "last_output_at": None,
            }

        resp_end = _last_response_end_timestamp(events)
        if isinstance(session_id, str) and resp_end is not None:
            user_turn = current_user_turn_by_session.get(session_id)
            if user_turn is not None:
                current_last_output_at = user_turn.get("last_output_at")
                if (
                    not isinstance(current_last_output_at, datetime)
                    or resp_end > current_last_output_at
                ):
                    user_turn["last_output_at"] = resp_end

    # End-of-stream flush, dict-insertion order (matches the loader's final close loop).
    for session_id in list(current_user_turn_by_session):
        close_user_turn(session_id)
    return by_provider


def ordered_provider_duration_items(
    seconds_by_provider: dict[str, list[float]],
) -> list[tuple[str, list[float]]]:
    items: list[tuple[str, list[float]]] = []
    all_values = seconds_by_provider.get("all", [])
    if all_values:
        items.append(("all", all_values))
    providers = {
        provider: values
        for provider, values in seconds_by_provider.items()
        if provider != "all" and values
    }
    for provider in provider_order(providers):
        items.append((provider, providers[provider]))
    return items


def duration_summary_row(label: str, values: list[float]) -> dict[str, Any]:
    arr = np.asarray(values, dtype=float)
    percentiles = sample_percentiles(values, [25, 50, 90, 99])
    return {
        "group": label,
        "count": int(arr.size),
        "mean_seconds": float(np.mean(arr)) if arr.size else None,
        "p25_seconds": percentiles["p25"],
        "p50_seconds": percentiles["p50"],
        "p90_seconds": percentiles["p90"],
        "p99_seconds": percentiles["p99"],
        "max_seconds": float(np.max(arr)) if arr.size else None,
    }


def write_user_turn_response_time_summary(
    response_seconds_by_provider: dict[str, list[float]],
    output_dir: Path,
) -> list[dict[str, Any]]:
    rows = [
        duration_summary_row(label, values)
        for label, values in ordered_provider_duration_items(
            response_seconds_by_provider
        )
    ]
    path = output_dir / "user_turn_response_time_summary.csv"
    fieldnames = [
        "group",
        "count",
        "mean_seconds",
        "p25_seconds",
        "p50_seconds",
        "p90_seconds",
        "p99_seconds",
        "max_seconds",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    response_seconds_by_provider = load_user_turn_response_seconds_by_provider(con)
    write_user_turn_response_time_summary(
        response_seconds_by_provider, args.output_dir
    )
    print(f"All outputs saved to {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
