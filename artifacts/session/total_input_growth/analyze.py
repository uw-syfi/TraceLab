#!/usr/bin/env python3
"""Analyze same-session total input length growth and reductions."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402
from growth import (  # noqa: E402
    EVENT_COLUMNS,
    MAJOR_REDUCTION_MIN_TOKENS,
    MICRO_REDUCTION_MAX_TOKENS,
    TRIGGER_LABELS,
    build_growth_stats,
    reduction_bucket,
    write_events_csv,
    write_summary_csv,
)

DEFAULT_OUTPUT_DIR = SCRIPT_DIR
DEFAULT_SUMMARY_NAME = "total_input_growth_summary.csv"

# Timing-event timestamps were originally read from the JSONL as raw ISO8601 strings
# (`YYYY-MM-DDTHH:MM:SS.mmmZ`, uniformly UTC millisecond precision). The trace DB stores them as a
# naive microsecond TIMESTAMP (T/Z stripped by trace_db._ts), so we pull them back as integer
# epoch-microseconds (native/wasm-identical marshalling) and rebuild the canonical ISO string here,
# bit-for-bit with the pre-DuckDB path.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    moment = _EPOCH + timedelta(microseconds=value)
    millis = moment.microsecond // 1000
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{millis:03d}Z"


def _int_or_zero(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _round_snapshot(
    *,
    round_pk: int,
    round_index,
    prefix_tokens,
    newly_append_tokens,
    first_event_type,
    model,
    timestamp_iso,
    trace_key,
) -> dict:
    prefix = _int_or_zero(prefix_tokens)
    append = _int_or_zero(newly_append_tokens)
    return {
        "line_number": round_pk,
        "round_index": round_index,
        "total_input_tokens": prefix + append,
        "prefix_tokens": prefix,
        "newly_append_tokens": append,
        "first_event_type": first_event_type,
        "model": model,
        "timestamp": timestamp_iso,
        "trace_key": trace_key,
    }


def iter_growth_events_from_db(con) -> list[dict]:
    """Reproduce ``growth.iter_growth_events`` from the trace DB (hybrid: SQL rows, Python sequencing).

    Rounds come back in ingestion order (``round_pk`` == file order), so walking them reproduces the
    line-by-line scan the JSONL path used: ``previous`` for a session is whatever row was last seen
    for it in file order, and ``current_line_number`` == ``round_pk`` (no blank lines, so file line
    number equals the ingest ordinal). The per-round FIRST timing event (``event_index = 1``) supplies
    ``first_event_type`` / ``timestamp`` exactly as ``first_timing_event_*`` did over the raw list.
    """
    # First timing event per round (event_index = 1): event_type and timestamp as epoch-microseconds.
    first_event: dict[int, tuple[str | None, int | None]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events WHERE event_index = 1 ORDER BY round_pk"
    ).fetchall():
        first_event[round_pk] = (event_type, ts_us)

    events: list[dict] = []
    last_by_session: dict[str, dict] = {}
    for (
        round_pk,
        session_id,
        provider,
        model,
        round_index,
        prefix_tokens,
        newly_append_tokens,
        trace_key,
    ) in con.execute(
        "SELECT round_pk, session_id, provider, model, round_index, "
        "prefix_tokens, newly_append_tokens, trace_key "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        first_event_type, ts_us = first_event.get(round_pk, (None, None))
        current_timestamp = _epoch_us_to_iso(ts_us)
        prefix = _int_or_zero(prefix_tokens)
        append = _int_or_zero(newly_append_tokens)
        current_total_input_tokens = prefix + append

        if (
            isinstance(session_id, str)
            and session_id in last_by_session
            and first_event_type in TRIGGER_LABELS
        ):
            previous = last_by_session[session_id]
            raw_delta_tokens = (
                current_total_input_tokens - previous["total_input_tokens"]
            )
            bucket = reduction_bucket(raw_delta_tokens)
            events.append(
                {
                    "provider": provider or "unknown",
                    "trigger": TRIGGER_LABELS[first_event_type],
                    "bucket": bucket,
                    "raw_delta_tokens": raw_delta_tokens,
                    "reduction_tokens": (
                        -raw_delta_tokens if raw_delta_tokens < 0 else 0
                    ),
                    "session_id": session_id,
                    "previous_round_index": previous["round_index"],
                    "current_round_index": round_index,
                    "previous_total_input_tokens": previous["total_input_tokens"],
                    "current_total_input_tokens": current_total_input_tokens,
                    "previous_prefix_tokens": previous["prefix_tokens"],
                    "current_prefix_tokens": prefix,
                    "prefix_delta_tokens": prefix - previous["prefix_tokens"],
                    "previous_newly_append_tokens": previous["newly_append_tokens"],
                    "current_newly_append_tokens": append,
                    "append_delta_tokens": append - previous["newly_append_tokens"],
                    "previous_first_event_type": previous["first_event_type"],
                    "current_first_event_type": first_event_type,
                    "previous_model": previous["model"],
                    "current_model": model,
                    "previous_timestamp": previous["timestamp"],
                    "current_timestamp": current_timestamp,
                    "previous_trace_key": previous["trace_key"],
                    "current_trace_key": trace_key,
                    "previous_line_number": previous["line_number"],
                    "current_line_number": round_pk,
                }
            )

        if isinstance(session_id, str):
            last_by_session[session_id] = _round_snapshot(
                round_pk=round_pk,
                round_index=round_index,
                prefix_tokens=prefix_tokens,
                newly_append_tokens=newly_append_tokens,
                first_event_type=first_event_type,
                model=model,
                timestamp_iso=current_timestamp,
                trace_key=trace_key,
            )
    return events


def write_filtered_events_csv(
    path: Path,
    events: list[dict],
    *,
    buckets: set[str],
    limit: int | None = None,
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    selected = [event for event in events if event.get("bucket") in buckets]
    selected.sort(
        key=lambda event: (
            str(event.get("provider") or ""),
            str(event.get("trigger") or ""),
            str(event.get("session_id") or ""),
            int(event.get("current_line_number") or 0),
        )
    )
    if limit is not None:
        selected = selected[:limit]

    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(selected)
    return len(selected)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--summary-csv",
        type=Path,
        default=None,
        help="Output aggregate CSV with trigger=all,user,tool_result. "
        "Defaults to total_input_growth_summary.csv under --output-dir.",
    )
    parser.add_argument(
        "--events-csv",
        type=Path,
        default=None,
        help="Optional path for every same-session growth event.",
    )
    parser.add_argument(
        "--micro-csv",
        type=Path,
        default=None,
        help="Optional path for micro-reduction events. Defaults under --output-dir.",
    )
    parser.add_argument(
        "--reductions-csv",
        type=Path,
        default=None,
        help="Optional path for all negative-growth events. Defaults under --output-dir.",
    )
    parser.add_argument(
        "--no-drilldowns",
        action="store_true",
        help="Only write the aggregate summary CSV.",
    )
    parser.add_argument(
        "--limit-events",
        type=int,
        default=None,
        help="Optional max rows for each drilldown CSV after stable sorting.",
    )
    parser.add_argument(
        "--micro-reduction-max-tokens",
        type=int,
        default=MICRO_REDUCTION_MAX_TOKENS,
        help="Maximum absolute drop size counted as micro_reduction.",
    )
    parser.add_argument(
        "--major-reduction-min-tokens",
        type=int,
        default=MAJOR_REDUCTION_MIN_TOKENS,
        help="Minimum absolute drop size counted as major_reduction.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.micro_reduction_max_tokens < 1:
        raise SystemExit("--micro-reduction-max-tokens must be positive")
    if args.major_reduction_min_tokens <= args.micro_reduction_max_tokens:
        raise SystemExit(
            "--major-reduction-min-tokens must be greater than "
            "--micro-reduction-max-tokens"
        )
    if args.limit_events is not None and args.limit_events < 0:
        raise SystemExit("--limit-events must be nonnegative")

    output_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    summary_csv = args.summary_csv or output_dir / DEFAULT_SUMMARY_NAME

    con = trace_db.open_from_args(args)
    events = iter_growth_events_from_db(con)
    if (
        args.micro_reduction_max_tokens != MICRO_REDUCTION_MAX_TOKENS
        or args.major_reduction_min_tokens != MAJOR_REDUCTION_MIN_TOKENS
    ):
        for event in events:
            raw_delta_tokens = int(event.get("raw_delta_tokens") or 0)
            event["bucket"] = reduction_bucket(
                raw_delta_tokens,
                micro_reduction_max_tokens=args.micro_reduction_max_tokens,
                major_reduction_min_tokens=args.major_reduction_min_tokens,
            )
            event["reduction_tokens"] = (
                -raw_delta_tokens if raw_delta_tokens < 0 else 0
            )

    stats = build_growth_stats(
        events,
        micro_reduction_max_tokens=args.micro_reduction_max_tokens,
        major_reduction_min_tokens=args.major_reduction_min_tokens,
    )
    write_summary_csv(summary_csv, stats)
    print(f"summary_csv={summary_csv}")

    if not args.no_drilldowns:
        events_csv = args.events_csv or output_dir / "total_input_growth_events.csv"
        reductions_csv = (
            args.reductions_csv or output_dir / "total_input_reductions.csv"
        )
        micro_csv = args.micro_csv or output_dir / "total_input_micro_reductions.csv"

        event_rows = events
        if args.limit_events is not None:
            event_rows = sorted(
                events,
                key=lambda event: int(event.get("current_line_number") or 0),
            )[: args.limit_events]
        event_count = write_events_csv(events_csv, event_rows)
        reduction_count = write_filtered_events_csv(
            reductions_csv,
            events,
            buckets={"micro_reduction", "ordinary_reduction", "major_reduction"},
            limit=args.limit_events,
        )
        micro_count = write_filtered_events_csv(
            micro_csv,
            events,
            buckets={"micro_reduction"},
            limit=args.limit_events,
        )
        print(f"events_csv={events_csv} rows={event_count}")
        print(f"reductions_csv={reductions_csv} rows={reduction_count}")
        print(f"micro_csv={micro_csv} rows={micro_count}")

    print(f"events={len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
