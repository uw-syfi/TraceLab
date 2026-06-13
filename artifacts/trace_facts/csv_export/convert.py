#!/usr/bin/env python3
"""Convert the normalized coding-trace into a multi-round CSV trace.

Source rows are LLM rounds in the shared trace DuckDB (one round per ``rounds``
row; see ``artifacts/utils/DB_SCHEMA.md``). Output uses the canonical multi-round
trace columns:

    id,input_len,output_len,arrival_time,round_idx,tool_wait_after_ms,prefix_len

Mapping:

    input_len          = max(newly_append_tokens, 1)
    prefix_len         = max(prefix_tokens, 0)
    output_len         = max(output_tokens, 1)
    round_idx          = contiguous 0..N within each emitted session
    arrival_time       = synthetic session arrival time in milliseconds
    tool_wait_after_ms = summed tool latency after the round, 0 on final round

By default, tool wait uses trace-observed wall latency (`tool_wall_latency_ms`).

I/O is the shared layer (`trace_db.add_db_args`): pass a prebuilt `--db`, or `-i`
a normalized JSONL trace (materialized to a temp DuckDB). `-o` is the output CSV.
Rounds are pulled in file order (`ORDER BY ingest_seq`) so the seeded synthetic
arrival times reproduce the pre-DuckDB JSONL path byte-for-byte.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402


TRACE_FIELDS = [
    "id",
    "input_len",
    "output_len",
    "arrival_time",
    "round_idx",
    "tool_wait_after_ms",
    "prefix_len",
]


class HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter,
    argparse.RawDescriptionHelpFormatter,
):
    pass


@dataclass
class ConvertStats:
    input_rows: int = 0
    skipped_rows: int = 0
    sessions_seen: int = 0
    sessions_emitted: int = 0
    rounds_emitted: int = 0
    compaction_sessions: int = 0
    tool_calls_seen: int = 0
    tool_latency_used: int = 0
    tool_latency_missing: int = 0
    tool_latency_invalid: int = 0
    total_tool_wait_ms: float = 0.0


def finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def int_at_least(value: Any, minimum: int, field_name: str) -> int:
    number = finite_float(value)
    if number is None:
        raise ValueError(f"missing or invalid {field_name}: {value!r}")
    return max(int(number), minimum)


def poisson_arrivals(n: int, rate_rps: float, seed: int) -> list[float]:
    if n <= 0:
        return []
    if rate_rps <= 0:
        raise ValueError(f"arrival rate must be positive, got {rate_rps}")
    if n == 1:
        return [0.0]
    rng = np.random.default_rng(seed)
    rate_per_ms = rate_rps / 1000.0
    inter_arrivals = rng.exponential(1.0 / rate_per_ms, size=n - 1)
    times = np.empty(n)
    times[0] = 0.0
    np.cumsum(inter_arrivals, out=times[1:])
    return times.tolist()


def constant_arrivals(n: int, rate_rps: float) -> list[float]:
    if n <= 0:
        return []
    if rate_rps <= 0:
        raise ValueError(f"arrival rate must be positive, got {rate_rps}")
    interval_ms = 1000.0 / rate_rps
    return [index * interval_ms for index in range(n)]


# Per-round tool wait, aggregated in SQL so we don't fetch one Python row per tool.
# We keep `wall`/`internal` as separate sums (+ missing/invalid/seen counts) so the
# Python side can apply the per-row stats and the final-round skip exactly as the
# pre-DuckDB JSONL loader did (which only iterated tools on non-final rounds).
_LATENCY_COLUMN = {
    "wall": "tool_wall_latency_ms",
    "internal": "tool_internal_latency_ms",
}


@dataclass
class ToolWaitAgg:
    wait_ms: float = 0.0
    seen: int = 0
    used: int = 0
    missing: int = 0
    invalid: int = 0


def load_tool_wait_by_round(con, latency_source: str) -> dict[int, ToolWaitAgg]:
    """Per-round tool-wait aggregate (keyed by ``round_pk``) for the chosen source.

    Reproduces the old per-tool loop: each tool contributes to ``seen``; its latency
    is null/non-finite -> ``missing``; negative -> ``invalid``; else summed and
    ``used``. ``finite_float`` mirrors the JSONL path (the column is BIGINT, so the
    only non-finite case is NULL). The final-round skip is applied later in Python.
    """
    if latency_source not in _LATENCY_COLUMN:
        raise ValueError(f"unsupported latency source: {latency_source}")
    column = _LATENCY_COLUMN[latency_source]
    agg: dict[int, ToolWaitAgg] = {}
    for round_pk, latency in con.execute(
        f"SELECT round_pk, {column} AS latency FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        bucket = agg.get(round_pk)
        if bucket is None:
            bucket = ToolWaitAgg()
            agg[round_pk] = bucket
        bucket.seen += 1
        value = finite_float(latency)
        if value is None:
            bucket.missing += 1
            continue
        if value < 0:
            bucket.invalid += 1
            continue
        bucket.used += 1
        bucket.wait_ms += value
    return agg


def load_sessions(
    con,
    provider: str,
    stats: ConvertStats,
) -> "OrderedDict[str, list[tuple[int, dict[str, Any]]]]":
    """Group rounds by session_id, preserving first-seen (file-order) session order.

    Rounds are pulled ``ORDER BY ingest_seq`` (== file order == old line order), so the
    first-appearance session order and the per-session row order both match the JSONL
    loader byte-for-byte. ``round_pk`` plays the role of the old 1-based line number
    (the trace has no blank lines, and it is only ever used as a sort tie-break, where
    only the relative order — identical to file order — matters).
    """
    sessions: "OrderedDict[str, list[tuple[int, dict[str, Any]]]]" = OrderedDict()
    for (
        round_pk,
        row_provider,
        session_id,
        round_index,
        newly_append_tokens,
        prefix_tokens,
        output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, session_id, round_index, "
        "newly_append_tokens, prefix_tokens, output_tokens "
        "FROM rounds ORDER BY ingest_seq"
    ).fetchall():
        stats.input_rows += 1
        if provider != "all" and row_provider != provider:
            continue
        if not isinstance(session_id, str) or not session_id:
            stats.skipped_rows += 1
            continue
        row = {
            "provider": row_provider,
            "session_id": session_id,
            "round_index": round_index,
            "newly_append_tokens": newly_append_tokens,
            "prefix_tokens": prefix_tokens,
            "output_tokens": output_tokens,
            "round_pk": round_pk,
        }
        sessions.setdefault(session_id, []).append((round_pk, row))
    stats.sessions_seen = len(sessions)
    return sessions


def round_sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
    line_no, row = item
    round_index = row.get("round_index")
    if isinstance(round_index, int) and not isinstance(round_index, bool):
        return round_index, line_no
    parsed = finite_float(round_index)
    if parsed is not None:
        return int(parsed), line_no
    return line_no, line_no


def tool_wait_after_ms(
    row: dict[str, Any],
    *,
    is_final_round: bool,
    tool_wait_by_round: dict[int, ToolWaitAgg],
    stats: ConvertStats,
) -> float:
    if is_final_round:
        return 0.0

    bucket = tool_wait_by_round.get(row["round_pk"])
    if bucket is None:
        return 0.0

    stats.tool_calls_seen += bucket.seen
    stats.tool_latency_missing += bucket.missing
    stats.tool_latency_invalid += bucket.invalid
    stats.tool_latency_used += bucket.used
    return bucket.wait_ms


def build_session_rounds(
    rows: list[tuple[int, dict[str, Any]]],
    *,
    tool_wait_by_round: dict[int, ToolWaitAgg],
    stats: ConvertStats,
) -> list[dict[str, Any]]:
    sorted_rows = sorted(rows, key=round_sort_key)
    rounds: list[dict[str, Any]] = []
    for index, (_line_no, row) in enumerate(sorted_rows):
        input_len = int_at_least(row.get("newly_append_tokens"), 1, "newly_append_tokens")
        output_len = int_at_least(row.get("output_tokens"), 1, "output_tokens")
        prefix_len = int_at_least(row.get("prefix_tokens"), 0, "prefix_tokens")
        tool_wait = tool_wait_after_ms(
            row,
            is_final_round=index == len(sorted_rows) - 1,
            tool_wait_by_round=tool_wait_by_round,
            stats=stats,
        )
        rounds.append(
            {
                "input_len": input_len,
                "output_len": output_len,
                "prefix_len": prefix_len,
                "tool_wait_after_ms": tool_wait,
            }
        )
    return rounds


def ordered_items(
    items: list[Any],
    *,
    seed: int,
    max_sessions: int | None,
    order: str,
) -> list[Any]:
    if order == "stable":
        ordered = list(items)
    elif order == "shuffle":
        rng = np.random.default_rng(seed)
        perm = rng.permutation(len(items))
        ordered = [items[int(index)] for index in perm]
    else:
        raise ValueError(f"unsupported session order: {order}")
    return ordered[:max_sessions] if max_sessions is not None else ordered


def generate_trace(
    con,
    output_path: Path,
    *,
    arrival_rate: float,
    arrival_pattern: str,
    seed: int,
    provider: str,
    max_sessions: int | None,
    session_order: str,
    latency_source: str,
) -> ConvertStats:
    stats = ConvertStats()
    tool_wait_by_round = load_tool_wait_by_round(con, latency_source)
    raw_sessions = load_sessions(con, provider, stats)

    emitted_raw_sessions = ordered_items(
        list(raw_sessions.values()),
        seed=seed,
        max_sessions=max_sessions,
        order=session_order,
    )

    built_sessions: list[list[dict[str, Any]]] = []
    for rows in emitted_raw_sessions:
        rounds = build_session_rounds(
            rows, tool_wait_by_round=tool_wait_by_round, stats=stats
        )
        if rounds:
            built_sessions.append(rounds)

    if not built_sessions:
        raise SystemExit("No sessions to emit. Check --provider and input file.")

    if arrival_pattern == "poisson":
        arrivals = poisson_arrivals(len(built_sessions), arrival_rate, seed + 1)
    elif arrival_pattern == "constant":
        arrivals = constant_arrivals(len(built_sessions), arrival_rate)
    else:
        raise ValueError(f"unsupported arrival pattern: {arrival_pattern}")

    trace_rows: list[dict[str, Any]] = []
    for session_id, (arrival_ms, rounds) in enumerate(zip(arrivals, built_sessions)):
        logical_prefix = 0
        had_compaction = False
        for round_idx, round_ in enumerate(rounds):
            if round_["prefix_len"] < logical_prefix:
                had_compaction = True
            logical_prefix = (
                round_["prefix_len"] + round_["input_len"] + round_["output_len"]
            )
            stats.total_tool_wait_ms += round_["tool_wait_after_ms"]
            trace_rows.append(
                {
                    "id": session_id,
                    "input_len": round_["input_len"],
                    "output_len": round_["output_len"],
                    "arrival_time": f"{arrival_ms:.6f}",
                    "round_idx": round_idx,
                    "tool_wait_after_ms": f"{round_['tool_wait_after_ms']:.6f}",
                    "prefix_len": round_["prefix_len"],
                }
            )
        if had_compaction:
            stats.compaction_sessions += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=TRACE_FIELDS)
        writer.writeheader()
        writer.writerows(trace_rows)

    stats.sessions_emitted = len(built_sessions)
    stats.rounds_emitted = len(trace_rows)
    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=HelpFormatter,
    )
    # Shared I/O surface: --db | -i/--input. We register these directly (mirroring
    # trace_db.add_db_args) instead of calling it, because add_db_args also binds
    # `-o` to --output-dir (a *directory* it mkdir's), which collides with this
    # experiment's `-o`/--output CSV *file* — run_all drives csv_export via the `io`
    # style (`-i <jsonl> -o <coding_trace.csv>`), so `-o` must stay the output file.
    # trace_db.open_from_args reads only args.db / args.input here (output_dir absent).
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=trace_db.DEFAULT_INPUT,
        help="normalized JSONL trace (materialized to a temp DuckDB if --db is not given)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="prebuilt DuckDB (from trace_db.materialize / run_all's build-db); skips materialize",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output trace CSV path.",
    )
    parser.add_argument(
        "--arrival-rate",
        type=float,
        default=1.0,
        help="Synthetic session arrival rate in sessions/s.",
    )
    parser.add_argument(
        "--arrival-pattern",
        choices=["poisson", "constant"],
        default="poisson",
        help="Synthetic session arrival process.",
    )
    parser.add_argument("--seed", type=int, default=0, help="Random seed.")
    parser.add_argument(
        "--provider",
        choices=["claude", "codex", "all"],
        default="all",
        help="Keep only sessions from this provider.",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=None,
        help="Cap the number of sessions emitted.",
    )
    parser.add_argument(
        "--session-order",
        choices=["shuffle", "stable"],
        default="shuffle",
        help="Shuffle sessions before assigning synthetic arrivals, or preserve input order.",
    )
    parser.add_argument(
        "--tool-latency-source",
        choices=["wall", "internal"],
        default="wall",
        help=(
            "Tool wait latency source. 'wall' uses tool_wall_latency_ms; "
            "'internal' uses tool_internal_latency_ms."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.arrival_rate <= 0:
        raise SystemExit(f"--arrival-rate must be positive, got {args.arrival_rate}")
    if args.max_sessions is not None and args.max_sessions <= 0:
        raise SystemExit(f"--max-sessions must be positive, got {args.max_sessions}")

    con = trace_db.open_from_args(args)
    stats = generate_trace(
        con,
        output_path=args.output,
        arrival_rate=args.arrival_rate,
        arrival_pattern=args.arrival_pattern,
        seed=args.seed,
        provider=args.provider,
        max_sessions=args.max_sessions,
        session_order=args.session_order,
        latency_source=args.tool_latency_source,
    )
    print(
        "Generated coding trace CSV:\n"
        f"  input rows:             {stats.input_rows}\n"
        f"  sessions seen:          {stats.sessions_seen}\n"
        f"  sessions emitted:       {stats.sessions_emitted}\n"
        f"  rounds emitted:         {stats.rounds_emitted}\n"
        f"  compaction sessions:    {stats.compaction_sessions}\n"
        f"  total tool wait:        {stats.total_tool_wait_ms / 1000.0:.1f} s\n"
        f"  tool latencies used:    {stats.tool_latency_used}\n"
        f"  tool latencies missing: {stats.tool_latency_missing}\n"
        f"  tool latencies invalid: {stats.tool_latency_invalid}\n"
        f"  output:                 {args.output}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
