#!/usr/bin/env python3
"""Per-round observable LLM generation-time CDFs by provider (count + total).

Generation time is the observable span from the latest input event
(user_message/tool_result) to the last model-output event (reasoning/text/
tool_call) in a round. See README.md for the full definition and caveats.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from cdf import (
    plot_count_cdf_by_provider,
    plot_cumulative_duration_cdf_by_provider,
    write_count_cdf_by_provider,
    write_cumulative_duration_cdf_by_provider,
)  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

# Same event-type partitions as the pre-DuckDB timing helper (artifacts/utils/timing.py).
INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}

# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string*; the int round-trips identically on both engines. Rebuild the naive datetime here exactly
# (integer microseconds). The old path parsed ISO strings to tz-aware datetimes, but a span between
# two same-tz datetimes equals the naive epoch_us span to the microsecond, so durations match the
# pre-DuckDB result bit-for-bit.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)


def _input_to_last_output_span_seconds(
    input_timestamps: list[datetime], output_timestamps: list[datetime]
) -> float | None:
    """Reproduce timing.input_to_last_output_span_seconds for one round's events.

    Per round: if either the input or output timestamp list is empty -> None. Otherwise take the
    earliest output as first_output; keep only inputs at-or-before first_output as candidate inputs;
    if none -> None. The span is (latest output - latest candidate input) in seconds, kept only when
    strictly positive.
    """
    if not input_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_inputs = [ts for ts in input_timestamps if ts <= first_output_at]
    if not candidate_inputs:
        return None
    duration = (max(output_timestamps) - max(candidate_inputs)).total_seconds()
    return duration if duration > 0 else None


def load_generation_seconds_by_provider(
    con: "duckdb.DuckDBPyConnection",
) -> dict[str, list[float]]:
    """``{provider: [generation_seconds, ...]}`` — one entry per round with a positive span.

    Mirrors the old single-pass loader exactly: for every round the per-round observable span is
    ``input_to_last_output_span_seconds`` over its ``timing_events`` and, when not None, is appended
    to that round's provider bucket (``str(provider) or "<unknown-provider>"``, matching the old
    truthy fallback). The full list is kept per provider — no sampling — so the CDF is exact.
    """
    # Per-round input/output timestamps (epoch-microsecond ints, rebuilt to naive datetimes below).
    # Event order within a round is irrelevant: only min/max over the two lists is used.
    inputs_by_round: dict[int, list[datetime]] = {}
    outputs_by_round: dict[int, list[datetime]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk"
    ).fetchall():
        ts = _epoch_us_to_datetime(ts_us)
        if ts is None:
            continue
        if event_type in INPUT_EVENT_TYPES:
            inputs_by_round.setdefault(round_pk, []).append(ts)
        elif event_type in MODEL_OUTPUT_EVENT_TYPES:
            outputs_by_round.setdefault(round_pk, []).append(ts)

    # Per-round provider, in ingestion order (round_pk == file order) so the per-provider value
    # lists are appended in exactly the order the old line-by-line loader produced.
    by_provider: dict[str, list[float]] = {}
    for round_pk, provider in con.execute(
        "SELECT round_pk, provider FROM rounds ORDER BY round_pk"
    ).fetchall():
        seconds = _input_to_last_output_span_seconds(
            inputs_by_round.get(round_pk, []),
            outputs_by_round.get(round_pk, []),
        )
        if seconds is None:
            continue
        key = str(provider) if provider else "<unknown-provider>"
        by_provider.setdefault(key, []).append(seconds)
    return by_provider


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    gen_by_provider = load_generation_seconds_by_provider(con)

    plot_count_cdf_by_provider(
        gen_by_provider,
        out,
        out_name="llm_generation_time_count_cdf_by_provider.png",
        title="LLM Generation Time Count CDF by Provider",
        x_label="Per-round observable generation-time threshold",
        table_title="single-round generation time",
        edge_kind="duration_seconds",
        unit_label="round",
    )
    plot_cumulative_duration_cdf_by_provider(
        gen_by_provider,
        out,
        out_name="llm_generation_time_total_cdf_by_provider.png",
        title="LLM Generation Time Total CDF by Provider",
        x_label="Per-round observable generation-time threshold",
        table_title="single-round generation time",
    )
    write_count_cdf_by_provider(
        gen_by_provider,
        out,
        out_name="llm_generation_time_count_cdf_by_provider.csv",
        edge_kind="duration_seconds",
    )
    write_cumulative_duration_cdf_by_provider(
        gen_by_provider,
        out,
        out_name="llm_generation_time_total_cdf_by_provider.csv",
    )

    # Final step: fuse README + CSV data + plotting code into each PNG.
    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
