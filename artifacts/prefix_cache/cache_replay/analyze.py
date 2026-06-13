#!/usr/bin/env python3
"""Analyze prompt-cache replay and adjusted append-token accounting.

This script turns the cache-replay checks from the notebook/ad-hoc analysis
into a repeatable report. It uses the normalized round trace for provider-wide
adjacent-round metrics and, when available, Claude's normalized
`claude_cache_creation_input_tokens`/`claude_uncached_input_tokens` fields.
Reachable raw Claude session files remain an optional fallback/debug source.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402

DEFAULT_OUTPUT_DIR = SCRIPT_DIR

PROVIDERS = ("claude", "codex")
ABS_THRESHOLDS = (0, 1, 2, 8, 32, 128, 512, 2048)
INPUT_CHAR_THRESHOLDS = (100, 500, 1_000)
LONG_TOOL_THRESHOLDS = (10_000, 50_000, 100_000, 500_000)
LONG_USER_THRESHOLDS = (1_000, 5_000, 10_000, 50_000)
LONG_OUTPUT_THRESHOLDS = (1_000, 4_000, 8_000, 16_000)


def int_field(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) else 0


def optional_int_field(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    return value if isinstance(value, int) else None


def first_present_int(row: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = optional_int_field(row, key)
        if value is not None:
            return value
    return None


def content_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def numeric_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "p10": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "min": None,
            "max": None,
        }
    return {
        "count": len(values),
        "mean": mean(values),
        "median": median(values),
        "p10": percentile(values, 0.10),
        "p90": percentile(values, 0.90),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "min": min(values),
        "max": max(values),
    }


def fraction(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "count": numerator,
        "total": denominator,
        "fraction": numerator / denominator if denominator else None,
    }


def fmt(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        if not math.isfinite(value):
            return "n/a"
        if abs(value) >= 100:
            return f"{value:,.0f}"
        return f"{value:,.{digits}f}"
    return str(value)


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def stringify_counter(counter: Counter[Any]) -> dict[str, int]:
    return {str(key): value for key, value in counter.most_common()}


def first_input_event_and_chars(row: dict[str, Any]) -> tuple[str | None, int, int, int, int]:
    top_level_user_chars = optional_int_field(row, "current_user_message_chars")
    top_level_tool_chars = optional_int_field(row, "current_tool_result_chars")
    top_level_user_count = optional_int_field(row, "current_user_message_count")
    top_level_tool_count = optional_int_field(row, "current_tool_result_count")
    top_level_first_event = row.get("first_input_event_type")
    if (
        top_level_user_chars is not None
        or top_level_tool_chars is not None
        or top_level_user_count is not None
        or top_level_tool_count is not None
        or isinstance(top_level_first_event, str)
    ):
        return (
            top_level_first_event if isinstance(top_level_first_event, str) else None,
            top_level_user_chars or 0,
            top_level_tool_chars or 0,
            top_level_user_count or 0,
            top_level_tool_count or 0,
        )

    first_event: str | None = None
    user_chars = 0
    tool_chars = 0
    user_count = 0
    tool_count = 0
    events = row.get("timing_events")
    if not isinstance(events, list):
        return first_event, user_chars, tool_chars, user_count, tool_count
    for event in events:
        if not isinstance(event, dict):
            continue
        event_type = event.get("event_type")
        if first_event is None and isinstance(event_type, str):
            first_event = event_type
        if event_type == "user_message":
            user_chars += int_field(event, "content_chars")
            user_count += 1
        elif event_type == "tool_result":
            tool_chars += int_field(event, "result_chars")
            tool_count += 1
    return first_event, user_chars, tool_chars, user_count, tool_count


def visible_output_tokens(row: dict[str, Any]) -> int:
    output_tokens = int_field(row, "output_tokens")
    if row.get("provider") == "codex":
        return max(0, output_tokens - int_field(row, "reasoning_output_tokens"))
    return output_tokens


# Round-level columns pulled from the DB, in file order (``ingest_seq``). These are exactly the
# keys the legacy JSONL loader read off each ``raw`` row, so a per-row dict assembled from them can
# be fed unchanged to the same helpers (``first_input_event_and_chars``, ``int_field``,
# ``optional_int_field``, ``first_present_int``, ``visible_output_tokens``) for byte-identical rows.
_ROUND_COLUMNS = (
    "provider",
    "session_id",
    "round_index",
    "model",
    "session_file",
    "prefix_tokens",
    "newly_append_tokens",
    "input_tokens_total",
    "output_tokens",
    "reasoning_output_tokens",
    "claude_uncached_input_tokens",
    "claude_cache_creation_input_tokens",
    "claude_cache_read_input_tokens",
    "current_user_message_chars",
    "current_tool_result_chars",
    "current_user_message_count",
    "current_tool_result_count",
    "first_input_event_type",
)


def _db_row_to_raw(record: tuple[Any, ...]) -> dict[str, Any]:
    """Rebuild the subset of a normalized ``raw`` dict the loader/helpers read, from a DB tuple.

    DuckDB returns BIGINT cells as Python ``int`` and NULL as ``None`` — the same shapes the JSON
    loader saw — so ``int_field``/``optional_int_field``/``first_present_int`` (which gate on
    ``isinstance(value, int)``) behave identically. The legacy ``timing_events`` fallback inside
    ``first_input_event_and_chars`` is intentionally not wired up: it only fires when ALL of the
    top-level ``current_*`` count/char fields are non-int AND ``first_input_event_type`` is not a
    string, which never occurs in the normalized trace (those columns are pinned BIGINT and always
    present), so the top-level branch is taken for every row exactly as before.
    """
    return dict(zip(_ROUND_COLUMNS, record))


def load_normalized_rows_from_db(con: "duckdb.DuckDBPyConnection") -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """DB-backed equivalent of the old ``load_normalized_rows`` JSONL parse.

    Rows are pulled ``ORDER BY ingest_seq`` (== file order), then grouped by ``(provider,
    session_id)`` and stably sorted by ``round_index`` in Python — reproducing the legacy
    first-appearance session-visitation order and per-session adjacency exactly. Each DB tuple is
    turned back into a ``raw``-like dict so the unchanged row-building/pairing logic (and the same
    helpers) produce identical ``rows`` / ``pairs`` / ``meta`` (incl. ``claude_source_files``).
    """
    rows_by_session: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    all_rows: list[dict[str, Any]] = []
    source_files: set[str] = set()
    stats: Counter[str] = Counter()

    # The DB's `rounds` schema is inferred from the trace's keys, so optional columns (e.g.
    # `session_file`, present in some traces, absent in others) vary by trace. Project each wanted
    # column only if it exists, else NULL — keeps the positional zip stable and yields None for an
    # absent column, exactly as the JSONL loader's `raw.get(...)` did (the helpers tolerate None).
    present = {row[0] for row in con.execute("DESCRIBE rounds").fetchall()}
    select_list = ", ".join(col if col in present else f"NULL AS {col}" for col in _ROUND_COLUMNS)
    records = con.execute(
        f"SELECT {select_list} FROM rounds ORDER BY ingest_seq"
    ).fetchall()
    for line_no, record in enumerate(records, start=1):
        raw = _db_row_to_raw(record)

        provider = raw.get("provider")
        session_id = raw.get("session_id")
        round_index = raw.get("round_index")
        if provider not in PROVIDERS or not isinstance(session_id, str) or not isinstance(round_index, int):
            stats["skipped_missing_identity"] += 1
            continue

        first_event, user_chars, tool_chars, user_count, tool_count = first_input_event_and_chars(raw)
        row = {
            "provider": provider,
            "session_id": session_id,
            "round_index": round_index,
            "model": raw.get("model"),
            "session_file": raw.get("session_file"),
            "prefix_tokens": int_field(raw, "prefix_tokens"),
            "newly_append_tokens": int_field(raw, "newly_append_tokens"),
            "input_tokens_total": int_field(raw, "input_tokens_total"),
            "output_tokens": int_field(raw, "output_tokens"),
            "reasoning_output_tokens": int_field(raw, "reasoning_output_tokens"),
            "visible_output_tokens": visible_output_tokens(raw),
            "claude_uncached_input_tokens": optional_int_field(raw, "claude_uncached_input_tokens"),
            "claude_cache_creation_input_tokens": first_present_int(
                raw,
                (
                    "claude_cache_creation_input_tokens",
                    "claude_cache_write_input_tokens",
                ),
            ),
            "claude_cache_read_input_tokens": optional_int_field(raw, "claude_cache_read_input_tokens"),
            "first_event": first_event,
            "user_chars": user_chars,
            "tool_chars": tool_chars,
            "raw_input_chars": user_chars + tool_chars,
            "user_event_count": user_count,
            "tool_result_count": tool_count,
            "line_no": line_no,
        }
        all_rows.append(row)
        rows_by_session[(provider, session_id)].append(row)
        if (
            provider == "claude"
            and row["claude_uncached_input_tokens"] is not None
            and row["claude_cache_creation_input_tokens"] is not None
            and row["claude_cache_read_input_tokens"] is not None
        ):
            stats["claude_rows_with_normalized_cache_fields"] += 1
        if provider == "claude" and isinstance(raw.get("session_file"), str):
            source_files.add(raw["session_file"])
        stats["rows"] += 1

    pairs: list[dict[str, Any]] = []
    for (_provider, _session_id), session_rows in rows_by_session.items():
        session_rows.sort(key=lambda item: item["round_index"])
        for previous, current in zip(session_rows, session_rows[1:]):
            if current["round_index"] != previous["round_index"] + 1:
                stats["skipped_non_adjacent_pair"] += 1
                continue
            signed_adjusted = current["newly_append_tokens"] - previous["output_tokens"]
            visible_signed_adjusted = current["newly_append_tokens"] - previous["visible_output_tokens"]
            pairs.append(
                {
                    "provider": current["provider"],
                    "session_id": current["session_id"],
                    "previous_round_index": previous["round_index"],
                    "round_index": current["round_index"],
                    "model": current["model"],
                    "next_prefix_minus_last_total": current["prefix_tokens"] - previous["input_tokens_total"],
                    "next_prefix_minus_last_total_plus_output": (
                        current["prefix_tokens"]
                        - (previous["input_tokens_total"] + previous["output_tokens"])
                    ),
                    "next_prefix_minus_last_total_plus_visible": (
                        current["prefix_tokens"]
                        - (previous["input_tokens_total"] + previous["visible_output_tokens"])
                    ),
                    "raw_append": current["newly_append_tokens"],
                    "previous_output_tokens": previous["output_tokens"],
                    "previous_visible_output_tokens": previous["visible_output_tokens"],
                    "previous_reasoning_output_tokens": previous["reasoning_output_tokens"],
                    "signed_adjusted_append": signed_adjusted,
                    "adjusted_append": max(0, signed_adjusted),
                    "visible_signed_adjusted_append": visible_signed_adjusted,
                    "visible_adjusted_append": max(0, visible_signed_adjusted),
                    "first_event": current["first_event"],
                    "user_chars": current["user_chars"],
                    "tool_chars": current["tool_chars"],
                    "raw_input_chars": current["raw_input_chars"],
                    "prefix_tokens": current["prefix_tokens"],
                    "input_tokens_total": current["input_tokens_total"],
                }
            )
            stats["pairs"] += 1

    return all_rows, pairs, {"stats": dict(stats), "claude_source_files": sorted(source_files)}


def build_normalized_claude_cache_detail(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stats: Counter[str] = Counter()
    for row in rows:
        if row["provider"] != "claude":
            continue
        uncached = row.get("claude_uncached_input_tokens")
        cache_creation = row.get("claude_cache_creation_input_tokens")
        cache_read = row.get("claude_cache_read_input_tokens")
        if not (
            isinstance(uncached, int)
            and isinstance(cache_creation, int)
            and isinstance(cache_read, int)
        ):
            stats["skipped_missing_normalized_cache_fields"] += 1
            continue
        item = {
            "session_id": row["session_id"],
            "round_index": row["round_index"],
            "first_event": row["first_event"],
            "raw_input_chars": row["raw_input_chars"],
            "user_chars": row["user_chars"],
            "tool_chars": row["tool_chars"],
            "input_tokens": uncached,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "input_tokens_total": row["input_tokens_total"],
            "output_tokens": row["output_tokens"],
        }
        rows_by_session[row["session_id"]].append(item)
        stats["normalized_cache_rounds"] += 1

    if not rows_by_session:
        return None

    normalized_rounds: list[dict[str, Any]] = []
    normalized_pairs: list[dict[str, Any]] = []
    for session_rows in rows_by_session.values():
        session_rows.sort(key=lambda item: item["round_index"])
        normalized_rounds.extend(session_rows)
        for previous, current in zip(session_rows, session_rows[1:]):
            if current["round_index"] != previous["round_index"] + 1:
                stats["skipped_non_adjacent_normalized_cache_pair"] += 1
                continue
            no_subtract = current["input_tokens"] + current["cache_creation_input_tokens"]
            signed_subtract = no_subtract - previous["output_tokens"]
            normalized_pairs.append(
                {
                    "first_event": current["first_event"],
                    "raw_input_chars": current["raw_input_chars"],
                    "user_chars": current["user_chars"],
                    "tool_chars": current["tool_chars"],
                    "input_tokens": current["input_tokens"],
                    "cache_creation_input_tokens": current["cache_creation_input_tokens"],
                    "cache_read_input_tokens": current["cache_read_input_tokens"],
                    "input_tokens_total": current["input_tokens_total"],
                    "output_tokens": current["output_tokens"],
                    "previous_output_tokens": previous["output_tokens"],
                    "previous_input_tokens_total": previous["input_tokens_total"],
                    "no_subtract": no_subtract,
                    "signed_subtract_previous_output": signed_subtract,
                    "subtract_previous_output_clamped": max(0, signed_subtract),
                    "next_cache_read_minus_previous_total": (
                        current["cache_read_input_tokens"] - previous["input_tokens_total"]
                    ),
                    "next_cache_creation_minus_previous_output": (
                        current["cache_creation_input_tokens"] - previous["output_tokens"]
                    ),
                }
            )
            stats["normalized_cache_pairs"] += 1

    return {
        "stats": {
            "source": "normalized_trace",
            **dict(stats),
            "raw_rounds": stats["normalized_cache_rounds"],
            "raw_pairs": stats["normalized_cache_pairs"],
        },
        "rounds": normalized_rounds,
        "pairs": normalized_pairs,
    }


def summarize_prefix_relationship(pairs: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    provider_pairs = [pair for pair in pairs if pair["provider"] == provider]
    errors = [float(pair["next_prefix_minus_last_total"]) for pair in provider_pairs]
    abs_errors = [abs(value) for value in errors]
    expected = [
        pair["input_tokens_total"] - pair["raw_append"]
        for pair in provider_pairs
    ]
    # The current row total is not the previous expected denominator. Use the
    # previous total implied by prefix error instead: next.prefix - error.
    relative_abs_errors = []
    for pair in provider_pairs:
        previous_total = pair["prefix_tokens"] - pair["next_prefix_minus_last_total"]
        if previous_total > 0:
            relative_abs_errors.append(abs(pair["next_prefix_minus_last_total"]) / previous_total)
    plus_output_errors = [
        float(pair["next_prefix_minus_last_total_plus_output"])
        for pair in provider_pairs
    ]
    plus_output_abs = [abs(value) for value in plus_output_errors]
    return {
        "pairs": len(provider_pairs),
        "error": numeric_summary(errors),
        "absolute_error": numeric_summary(abs_errors),
        "relative_absolute_error": numeric_summary(relative_abs_errors),
        "within_absolute": {
            str(threshold): fraction(sum(value <= threshold for value in abs_errors), len(abs_errors))
            for threshold in ABS_THRESHOLDS
        },
        "within_relative": {
            "1pct": fraction(sum(value <= 0.01 for value in relative_abs_errors), len(relative_abs_errors)),
            "5pct": fraction(sum(value <= 0.05 for value in relative_abs_errors), len(relative_abs_errors)),
        },
        "with_prior_output_added_error": numeric_summary(plus_output_errors),
        "with_prior_output_added_absolute_error": numeric_summary(plus_output_abs),
        "with_prior_output_added_within_absolute_8": fraction(
            sum(value <= 8 for value in plus_output_abs),
            len(plus_output_abs),
        ),
    }


def summarize_adjusted_append(pairs: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    provider_pairs = [pair for pair in pairs if pair["provider"] == provider]
    signed = [float(pair["signed_adjusted_append"]) for pair in provider_pairs]
    adjusted = [float(pair["adjusted_append"]) for pair in provider_pairs]
    visible_signed = [float(pair["visible_signed_adjusted_append"]) for pair in provider_pairs]
    visible_adjusted = [float(pair["visible_adjusted_append"]) for pair in provider_pairs]
    starts = Counter(pair["first_event"] for pair in provider_pairs)
    by_start: dict[str, dict[str, Any]] = {}
    for start_event, count in sorted(starts.items(), key=lambda item: str(item[0])):
        if count < 20:
            continue
        subset = [pair for pair in provider_pairs if pair["first_event"] == start_event]
        subset_signed = [float(pair["signed_adjusted_append"]) for pair in subset]
        by_start[str(start_event)] = {
            "pairs": len(subset),
            "signed_adjusted_append": numeric_summary(subset_signed),
            "adjusted_zero": fraction(
                sum(pair["adjusted_append"] == 0 for pair in subset),
                len(subset),
            ),
            "signed_negative": fraction(
                sum(pair["signed_adjusted_append"] < 0 for pair in subset),
                len(subset),
            ),
        }
    return {
        "pairs": len(provider_pairs),
        "raw_append": numeric_summary([float(pair["raw_append"]) for pair in provider_pairs]),
        "previous_output_tokens": numeric_summary(
            [float(pair["previous_output_tokens"]) for pair in provider_pairs]
        ),
        "previous_visible_output_tokens": numeric_summary(
            [float(pair["previous_visible_output_tokens"]) for pair in provider_pairs]
        ),
        "signed_adjusted_append": numeric_summary(signed),
        "adjusted_append": numeric_summary(adjusted),
        "visible_signed_adjusted_append": numeric_summary(visible_signed),
        "visible_adjusted_append": numeric_summary(visible_adjusted),
        "adjusted_zero": fraction(
            sum(pair["adjusted_append"] == 0 for pair in provider_pairs),
            len(provider_pairs),
        ),
        "signed_negative": fraction(
            sum(pair["signed_adjusted_append"] < 0 for pair in provider_pairs),
            len(provider_pairs),
        ),
        "signed_exact_zero": fraction(
            sum(pair["signed_adjusted_append"] == 0 for pair in provider_pairs),
            len(provider_pairs),
        ),
        "next_append_ge_previous_output": fraction(
            sum(pair["raw_append"] >= pair["previous_output_tokens"] for pair in provider_pairs),
            len(provider_pairs),
        ),
        "next_append_ge_previous_visible_output": fraction(
            sum(pair["raw_append"] >= pair["previous_visible_output_tokens"] for pair in provider_pairs),
            len(provider_pairs),
        ),
        "by_start_event": by_start,
    }


def summarize_clamped_cases(pairs: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    provider_pairs = [pair for pair in pairs if pair["provider"] == provider]
    clamped = [pair for pair in provider_pairs if pair["signed_adjusted_append"] < 0]
    not_clamped = [pair for pair in provider_pairs if pair["signed_adjusted_append"] >= 0]

    def summarize_subset(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(subset),
            "raw_input_chars": numeric_summary([float(pair["raw_input_chars"]) for pair in subset]),
            "user_chars": numeric_summary([float(pair["user_chars"]) for pair in subset]),
            "tool_chars": numeric_summary([float(pair["tool_chars"]) for pair in subset]),
            "raw_append": numeric_summary([float(pair["raw_append"]) for pair in subset]),
            "previous_output_tokens": numeric_summary(
                [float(pair["previous_output_tokens"]) for pair in subset]
            ),
            "previous_visible_output_tokens": numeric_summary(
                [float(pair["previous_visible_output_tokens"]) for pair in subset]
            ),
            "previous_reasoning_output_tokens": numeric_summary(
                [float(pair["previous_reasoning_output_tokens"]) for pair in subset]
            ),
            "deficit_tokens": numeric_summary(
                [float(-pair["signed_adjusted_append"]) for pair in subset if pair["signed_adjusted_append"] < 0]
            ),
            "raw_input_char_buckets": {
                f"<= {threshold}": fraction(
                    sum(pair["raw_input_chars"] <= threshold for pair in subset),
                    len(subset),
                )
                for threshold in INPUT_CHAR_THRESHOLDS
            },
            "first_event": stringify_counter(Counter(pair["first_event"] for pair in subset)),
        }

    return {
        "pairs": len(provider_pairs),
        "clamped": summarize_subset(clamped),
        "not_clamped": summarize_subset(not_clamped),
    }


def summarize_long_current_inputs(rows: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    provider_rows = [row for row in rows if row["provider"] == provider]

    def summarize(field: str, thresholds: tuple[int, ...]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for threshold in thresholds:
            subset = [row for row in provider_rows if row[field] >= threshold]
            denom = sum(1 for row in subset if row["raw_input_chars"] > 0)
            data[str(threshold)] = {
                "rows": len(subset),
                "append_tokens": numeric_summary(
                    [float(row["newly_append_tokens"]) for row in subset]
                ),
                "prefix_tokens": numeric_summary(
                    [float(row["prefix_tokens"]) for row in subset]
                ),
                "input_tokens_total": numeric_summary(
                    [float(row["input_tokens_total"]) for row in subset]
                ),
                "raw_input_chars": numeric_summary(
                    [float(row["raw_input_chars"]) for row in subset]
                ),
                "append_ge_raw_chars_div_4": fraction(
                    sum(
                        row["newly_append_tokens"] >= row["raw_input_chars"] / 4
                        for row in subset
                        if row["raw_input_chars"] > 0
                    ),
                    denom,
                ),
                "first_event": stringify_counter(Counter(row["first_event"] for row in subset)),
            }
        return data

    return {
        "tool_chars": summarize("tool_chars", LONG_TOOL_THRESHOLDS),
        "user_chars": summarize("user_chars", LONG_USER_THRESHOLDS),
        "raw_input_chars": summarize("raw_input_chars", LONG_TOOL_THRESHOLDS),
    }


def summarize_long_prior_outputs(pairs: list[dict[str, Any]], provider: str) -> dict[str, Any]:
    provider_pairs = [pair for pair in pairs if pair["provider"] == provider]
    data: dict[str, Any] = {}
    for threshold in LONG_OUTPUT_THRESHOLDS:
        subset = [
            pair
            for pair in provider_pairs
            if pair["previous_visible_output_tokens"] >= threshold
        ]
        data[str(threshold)] = {
            "pairs": len(subset),
            "previous_output_tokens": numeric_summary(
                [float(pair["previous_output_tokens"]) for pair in subset]
            ),
            "previous_visible_output_tokens": numeric_summary(
                [float(pair["previous_visible_output_tokens"]) for pair in subset]
            ),
            "previous_reasoning_output_tokens": numeric_summary(
                [float(pair["previous_reasoning_output_tokens"]) for pair in subset]
            ),
            "next_append": numeric_summary([float(pair["raw_append"]) for pair in subset]),
            "next_prefix_minus_last_total": numeric_summary(
                [float(pair["next_prefix_minus_last_total"]) for pair in subset]
            ),
            "next_append_ge_previous_output": fraction(
                sum(pair["raw_append"] >= pair["previous_output_tokens"] for pair in subset),
                len(subset),
            ),
            "next_append_ge_previous_visible_output": fraction(
                sum(pair["raw_append"] >= pair["previous_visible_output_tokens"] for pair in subset),
                len(subset),
            ),
        }
    return data


def claude_usage_int(usage: dict[str, Any] | None, key: str) -> int:
    if not isinstance(usage, dict):
        return 0
    value = usage.get(key, 0)
    return value if isinstance(value, int) else 0


def claude_has_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(
        isinstance(usage.get(key), int)
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def claude_usage_score(message: dict[str, Any]) -> tuple[int, int, int]:
    usage = message.get("usage") if isinstance(message, dict) else None
    return (
        claude_usage_int(usage, "output_tokens"),
        claude_usage_int(usage, "input_tokens")
        + claude_usage_int(usage, "cache_creation_input_tokens")
        + claude_usage_int(usage, "cache_read_input_tokens"),
        int(message.get("stop_reason") is not None),
    )


def summarize_claude_user_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content")
    if isinstance(content, list):
        tool_chars = 0
        user_chars = 0
        tool_count = 0
        user_count = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_count += 1
                tool_chars += content_chars(block.get("content"))
            else:
                user_count += 1
                user_chars += content_chars(block)
        return {
            "first_event": "tool_result" if tool_count else "user_message",
            "tool_chars": tool_chars,
            "user_chars": user_chars,
            "raw_input_chars": content_chars(content),
            "tool_count": tool_count,
            "user_count": user_count,
        }
    return {
        "first_event": "user_message",
        "tool_chars": 0,
        "user_chars": content_chars(content),
        "raw_input_chars": content_chars(content),
        "tool_count": 0,
        "user_count": 1,
    }


def extract_raw_claude_rounds(session_file: Path) -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    by_message_id: dict[str, dict[str, Any]] = {}
    pending_inputs: list[dict[str, Any]] = []

    with session_file.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "user":
                item = summarize_claude_user_message(message)
                item["line_no"] = line_no
                pending_inputs.append(item)
                continue
            if record.get("type") != "assistant" or role != "assistant":
                continue
            message_id = message.get("id")
            usage = message.get("usage")
            if (
                not isinstance(message_id, str)
                or message_id == "<synthetic>"
                or message.get("model") == "<synthetic>"
                or not claude_has_usage(usage)
            ):
                continue

            score = claude_usage_score(message)
            if message_id not in by_message_id:
                item = {
                    "message_id": message_id,
                    "line_no": line_no,
                    "score": score,
                    "first_event": pending_inputs[0]["first_event"] if pending_inputs else None,
                    "raw_input_chars": sum(row["raw_input_chars"] for row in pending_inputs),
                    "user_chars": sum(row["user_chars"] for row in pending_inputs),
                    "tool_chars": sum(row["tool_chars"] for row in pending_inputs),
                    "tool_count": sum(row["tool_count"] for row in pending_inputs),
                    "user_count": sum(row["user_count"] for row in pending_inputs),
                    "model": message.get("model"),
                    "stop_reason": message.get("stop_reason"),
                }
                pending_inputs = []
                by_message_id[message_id] = item
                rounds.append(item)
            else:
                item = by_message_id[message_id]
                if score < item["score"]:
                    continue
                item.update(
                    {
                        "line_no": line_no,
                        "score": score,
                        "model": message.get("model"),
                        "stop_reason": message.get("stop_reason"),
                    }
                )

            assert isinstance(usage, dict)
            input_tokens = claude_usage_int(usage, "input_tokens")
            cache_creation = claude_usage_int(usage, "cache_creation_input_tokens")
            cache_read = claude_usage_int(usage, "cache_read_input_tokens")
            output_tokens = claude_usage_int(usage, "output_tokens")
            item.update(
                {
                    "input_tokens": input_tokens,
                    "cache_creation_input_tokens": cache_creation,
                    "cache_read_input_tokens": cache_read,
                    "output_tokens": output_tokens,
                    "input_tokens_total": input_tokens + cache_creation + cache_read,
                }
            )

    return [row for row in rounds if "input_tokens_total" in row]


def load_raw_claude(source_files: list[str], max_files: int | None) -> dict[str, Any]:
    raw_rounds: list[dict[str, Any]] = []
    raw_pairs: list[dict[str, Any]] = []
    stats: Counter[str] = Counter()

    for index, source_file in enumerate(source_files):
        if max_files is not None and index >= max_files:
            break
        path = Path(source_file)
        try:
            if not path.exists() or not os.access(path, os.R_OK):
                stats["skipped_unreadable_or_missing"] += 1
                continue
            rounds = extract_raw_claude_rounds(path)
        except OSError:
            stats["skipped_permission_or_os_error"] += 1
            continue
        stats["readable_files"] += 1
        raw_rounds.extend(rounds)
        for previous, current in zip(rounds, rounds[1:]):
            no_subtract = current["input_tokens"] + current["cache_creation_input_tokens"]
            signed_subtract = no_subtract - previous["output_tokens"]
            raw_pairs.append(
                {
                    "first_event": current["first_event"],
                    "raw_input_chars": current["raw_input_chars"],
                    "user_chars": current["user_chars"],
                    "tool_chars": current["tool_chars"],
                    "input_tokens": current["input_tokens"],
                    "cache_creation_input_tokens": current["cache_creation_input_tokens"],
                    "cache_read_input_tokens": current["cache_read_input_tokens"],
                    "input_tokens_total": current["input_tokens_total"],
                    "output_tokens": current["output_tokens"],
                    "previous_output_tokens": previous["output_tokens"],
                    "previous_input_tokens_total": previous["input_tokens_total"],
                    "no_subtract": no_subtract,
                    "signed_subtract_previous_output": signed_subtract,
                    "subtract_previous_output_clamped": max(0, signed_subtract),
                    "next_cache_read_minus_previous_total": (
                        current["cache_read_input_tokens"] - previous["input_tokens_total"]
                    ),
                    "next_cache_creation_minus_previous_output": (
                        current["cache_creation_input_tokens"] - previous["output_tokens"]
                    ),
                }
            )
        stats["raw_rounds"] += len(rounds)
        stats["raw_pairs"] += max(0, len(rounds) - 1)

    return {
        "stats": dict(stats),
        "rounds": raw_rounds,
        "pairs": raw_pairs,
    }


def summarize_raw_claude(raw: dict[str, Any]) -> dict[str, Any]:
    rounds: list[dict[str, Any]] = raw["rounds"]
    pairs: list[dict[str, Any]] = raw["pairs"]

    by_start: dict[str, Any] = {}
    starts = Counter(pair["first_event"] for pair in pairs)
    for start_event, count in sorted(starts.items(), key=lambda item: str(item[0])):
        if count < 20:
            continue
        subset = [pair for pair in pairs if pair["first_event"] == start_event]
        by_start[str(start_event)] = {
            "pairs": len(subset),
            "next_cache_creation_ge_previous_output": fraction(
                sum(
                    pair["cache_creation_input_tokens"] >= pair["previous_output_tokens"]
                    for pair in subset
                ),
                len(subset),
            ),
            "next_cache_creation_minus_previous_output": numeric_summary(
                [
                    float(pair["next_cache_creation_minus_previous_output"])
                    for pair in subset
                ]
            ),
        }

    def long_current(field: str, thresholds: tuple[int, ...]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for threshold in thresholds:
            subset = [round_obj for round_obj in rounds if round_obj[field] >= threshold]
            data[str(threshold)] = {
                "rounds": len(subset),
                "cache_creation_input_tokens": numeric_summary(
                    [float(row["cache_creation_input_tokens"]) for row in subset]
                ),
                "input_tokens": numeric_summary(
                    [float(row["input_tokens"]) for row in subset]
                ),
                "cache_read_input_tokens": numeric_summary(
                    [float(row["cache_read_input_tokens"]) for row in subset]
                ),
                "raw_input_chars": numeric_summary(
                    [float(row["raw_input_chars"]) for row in subset]
                ),
            }
        return data

    def short_current(threshold: int, first_event: str | None = None) -> dict[str, Any]:
        subset = [pair for pair in pairs if pair["raw_input_chars"] <= threshold]
        if first_event is not None:
            subset = [pair for pair in subset if pair["first_event"] == first_event]
        return {
            "pairs": len(subset),
            "raw_input_chars": numeric_summary([float(pair["raw_input_chars"]) for pair in subset]),
            "previous_output_tokens": numeric_summary(
                [float(pair["previous_output_tokens"]) for pair in subset]
            ),
            "no_subtract": numeric_summary([float(pair["no_subtract"]) for pair in subset]),
            "subtract_previous_output": numeric_summary(
                [float(pair["signed_subtract_previous_output"]) for pair in subset]
            ),
            "subtract_previous_output_clamped": numeric_summary(
                [float(pair["subtract_previous_output_clamped"]) for pair in subset]
            ),
            "negative_after_subtract": fraction(
                sum(pair["signed_subtract_previous_output"] < 0 for pair in subset),
                len(subset),
            ),
        }

    return {
        "stats": raw["stats"],
        "next_cache_read_minus_previous_total": numeric_summary(
            [float(pair["next_cache_read_minus_previous_total"]) for pair in pairs]
        ),
        "next_cache_creation_minus_previous_output": numeric_summary(
            [float(pair["next_cache_creation_minus_previous_output"]) for pair in pairs]
        ),
        "next_cache_creation_ge_previous_output": fraction(
            sum(pair["cache_creation_input_tokens"] >= pair["previous_output_tokens"] for pair in pairs),
            len(pairs),
        ),
        "by_start_event": by_start,
        "long_current_inputs": {
            "tool_chars": long_current("tool_chars", LONG_TOOL_THRESHOLDS),
            "user_chars": long_current("user_chars", LONG_USER_THRESHOLDS),
        },
        "short_current_inputs": {
            "<=100": short_current(100),
            "user_message<=50": short_current(50, "user_message"),
            "tool_result<=100": short_current(100, "tool_result"),
        },
    }


def build_report(
    normalized_rows: list[dict[str, Any]],
    normalized_pairs: list[dict[str, Any]],
    normalized_meta: dict[str, Any],
    raw_claude: dict[str, Any] | None,
) -> dict[str, Any]:
    normalized_claude_cache = build_normalized_claude_cache_detail(normalized_rows)
    report: dict[str, Any] = {
        "normalized_trace": normalized_meta,
        "providers": {},
    }
    for provider in PROVIDERS:
        report["providers"][provider] = {
            "prefix_relationship": summarize_prefix_relationship(normalized_pairs, provider),
            "adjusted_append": summarize_adjusted_append(normalized_pairs, provider),
            "clamped_cases": summarize_clamped_cases(normalized_pairs, provider),
            "long_current_inputs": summarize_long_current_inputs(normalized_rows, provider),
            "long_prior_visible_outputs": summarize_long_prior_outputs(normalized_pairs, provider),
        }
    if normalized_claude_cache is not None:
        report["claude_cache_detail"] = summarize_raw_claude(normalized_claude_cache)
    elif raw_claude is not None:
        report["claude_cache_detail"] = summarize_raw_claude(raw_claude)
    if raw_claude is not None:
        report["raw_claude_debug"] = summarize_raw_claude(raw_claude)
    return report


def md_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = [
        "# Cache Replay Analysis",
        "",
        "Generated from normalized adjacent LLM rounds. The Claude cache-creation section uses normalized Claude cache fields when present; raw Claude parsing is only a fallback/debug path.",
        "",
        "## Normalized Trace",
        "",
    ]
    stats = report["normalized_trace"]["stats"]
    lines.extend(
        md_table(
            ["metric", "value"],
            [
                ["rows", fmt(stats.get("rows"))],
                ["adjacent pairs", fmt(stats.get("pairs"))],
                ["skipped non-adjacent pairs", fmt(stats.get("skipped_non_adjacent_pair"))],
                ["bad json", fmt(stats.get("bad_json", 0))],
            ],
        )
    )

    lines.extend(["", "## Prefix Relationship", ""])
    rows = []
    for provider in PROVIDERS:
        section = report["providers"][provider]["prefix_relationship"]
        rows.append(
            [
                provider,
                fmt(section["pairs"]),
                fmt(section["absolute_error"]["median"]),
                fmt_pct(section["within_absolute"]["8"]["fraction"]),
                fmt_pct(section["within_relative"]["1pct"]["fraction"]),
                fmt(section["with_prior_output_added_error"]["median"]),
                fmt_pct(section["with_prior_output_added_within_absolute_8"]["fraction"]),
            ]
        )
    lines.extend(
        md_table(
            [
                "provider",
                "pairs",
                "median abs err",
                "within 8 tok",
                "within 1%",
                "median err if add output",
                "within 8 if add output",
            ],
            rows,
        )
    )

    lines.extend(["", "## Adjusted Append", ""])
    rows = []
    for provider in PROVIDERS:
        section = report["providers"][provider]["adjusted_append"]
        rows.append(
            [
                provider,
                fmt(section["raw_append"]["median"]),
                fmt(section["adjusted_append"]["median"]),
                fmt(section["previous_output_tokens"]["median"]),
                fmt_pct(section["adjusted_zero"]["fraction"]),
                fmt_pct(section["signed_negative"]["fraction"]),
                fmt_pct(section["next_append_ge_previous_output"]["fraction"]),
                fmt_pct(section["next_append_ge_previous_visible_output"]["fraction"]),
            ]
        )
    lines.extend(
        md_table(
            [
                "provider",
                "raw append median",
                "adjusted append median",
                "prev output median",
                "adjusted zero",
                "signed negative",
                "append >= prev output",
                "append >= prev visible",
            ],
            rows,
        )
    )

    lines.extend(["", "## Clamped Cases", ""])
    rows = []
    for provider in PROVIDERS:
        section = report["providers"][provider]["clamped_cases"]
        clamped = section["clamped"]
        rows.append(
            [
                provider,
                fmt(clamped["count"]),
                fmt_pct(clamped["count"] / section["pairs"] if section["pairs"] else None),
                fmt(clamped["raw_input_chars"]["median"]),
                fmt_pct(clamped["raw_input_char_buckets"]["<= 500"]["fraction"]),
                fmt(clamped["raw_append"]["median"]),
                fmt(clamped["previous_output_tokens"]["median"]),
                fmt(clamped["deficit_tokens"]["median"]),
            ]
        )
    lines.extend(
        md_table(
            [
                "provider",
                "clamped pairs",
                "share",
                "raw chars median",
                "raw <=500",
                "append median",
                "prev output median",
                "deficit median",
            ],
            rows,
        )
    )

    lines.extend(["", "## Long Current Inputs", ""])
    rows = []
    for provider in PROVIDERS:
        section = report["providers"][provider]["long_current_inputs"]
        rows.extend(
            [
                [
                    provider,
                    "tool_chars>=50k",
                    fmt(section["tool_chars"]["50000"]["rows"]),
                    fmt(section["tool_chars"]["50000"]["append_tokens"]["median"]),
                    fmt_pct(section["tool_chars"]["50000"]["append_ge_raw_chars_div_4"]["fraction"]),
                ],
                [
                    provider,
                    "tool_chars>=100k",
                    fmt(section["tool_chars"]["100000"]["rows"]),
                    fmt(section["tool_chars"]["100000"]["append_tokens"]["median"]),
                    fmt_pct(section["tool_chars"]["100000"]["append_ge_raw_chars_div_4"]["fraction"]),
                ],
                [
                    provider,
                    "user_chars>=10k",
                    fmt(section["user_chars"]["10000"]["rows"]),
                    fmt(section["user_chars"]["10000"]["append_tokens"]["median"]),
                    fmt_pct(section["user_chars"]["10000"]["append_ge_raw_chars_div_4"]["fraction"]),
                ],
                [
                    provider,
                    "user_chars>=50k",
                    fmt(section["user_chars"]["50000"]["rows"]),
                    fmt(section["user_chars"]["50000"]["append_tokens"]["median"]),
                    fmt_pct(section["user_chars"]["50000"]["append_ge_raw_chars_div_4"]["fraction"]),
                ],
            ]
        )
    lines.extend(
        md_table(
            ["provider", "condition", "rows", "append median", "append >= raw/4"],
            rows,
        )
    )

    claude_cache = report.get("claude_cache_detail")
    if isinstance(claude_cache, dict):
        lines.extend(["", "## Claude Cache Creation", ""])
        cache_stats = claude_cache["stats"]
        lines.extend(
            md_table(
                ["metric", "value"],
                [
                    ["source", cache_stats.get("source", "raw_claude")],
                    ["cache rounds", fmt(cache_stats.get("raw_rounds", 0))],
                    ["cache adjacent pairs", fmt(cache_stats.get("raw_pairs", 0))],
                    ["readable files", fmt(cache_stats.get("readable_files", 0))],
                    ["skipped unreadable/missing", fmt(cache_stats.get("skipped_unreadable_or_missing", 0))],
                    ["skipped permission/os error", fmt(cache_stats.get("skipped_permission_or_os_error", 0))],
                    ["skipped missing normalized fields", fmt(cache_stats.get("skipped_missing_normalized_cache_fields", 0))],
                ],
            )
        )
        lines.extend(["", "### Prior Output Into Next Cache Write", ""])
        rows = [
            [
                "all",
                fmt(claude_cache["stats"].get("raw_pairs", 0)),
                fmt(claude_cache["next_cache_creation_minus_previous_output"]["median"]),
                fmt_pct(claude_cache["next_cache_creation_ge_previous_output"]["fraction"]),
            ]
        ]
        for start_event, section in claude_cache["by_start_event"].items():
            rows.append(
                [
                    start_event,
                    fmt(section["pairs"]),
                    fmt(section["next_cache_creation_minus_previous_output"]["median"]),
                    fmt_pct(section["next_cache_creation_ge_previous_output"]["fraction"]),
                ]
            )
        lines.extend(
            md_table(
                ["next start", "pairs", "median create-prev output", "create >= prev output"],
                rows,
            )
        )
        lines.extend(["", "### Short Inputs Before/After Subtracting Prior Output", ""])
        short = claude_cache["short_current_inputs"]
        rows = []
        for label, section in short.items():
            rows.append(
                [
                    label,
                    fmt(section["pairs"]),
                    fmt(section["no_subtract"]["median"]),
                    fmt(section["subtract_previous_output"]["median"]),
                    fmt(section["subtract_previous_output_clamped"]["median"]),
                    fmt_pct(section["negative_after_subtract"]["fraction"]),
                ]
            )
        lines.extend(
            md_table(
                [
                    "condition",
                    "pairs",
                    "no subtract median",
                    "subtract median",
                    "clamped subtract median",
                    "negative share",
                ],
                rows,
            )
        )
        lines.extend(["", "### Long Current Inputs And Cache Creation", ""])
        long = claude_cache["long_current_inputs"]
        rows = [
            [
                "tool_chars>=50k",
                fmt(long["tool_chars"]["50000"]["rounds"]),
                fmt(long["tool_chars"]["50000"]["cache_creation_input_tokens"]["median"]),
            ],
            [
                "tool_chars>=100k",
                fmt(long["tool_chars"]["100000"]["rounds"]),
                fmt(long["tool_chars"]["100000"]["cache_creation_input_tokens"]["median"]),
            ],
            [
                "user_chars>=10k",
                fmt(long["user_chars"]["10000"]["rounds"]),
                fmt(long["user_chars"]["10000"]["cache_creation_input_tokens"]["median"]),
            ],
            [
                "user_chars>=50k",
                fmt(long["user_chars"]["50000"]["rounds"]),
                fmt(long["user_chars"]["50000"]["cache_creation_input_tokens"]["median"]),
            ],
        ]
        lines.extend(
            md_table(
                ["condition", "rounds", "cache_creation median"],
                rows,
            )
        )

    lines.append("")
    return "\n".join(lines)


def write_outputs(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "cache_replay_analysis.json"
    md_path = output_dir / "cache_replay_analysis.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # --db | -i/--input | -o/--output-dir. ``--output-dir`` stays valid as the long form of -o, and
    # ``--input`` as the long form of -i, so existing callers (e.g. run_all.py) keep working.
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--raw-claude",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Read reachable raw Claude session files as a fallback/debug source "
            "for cache_creation_input_tokens metrics."
        ),
    )
    parser.add_argument(
        "--max-raw-claude-files",
        type=int,
        default=None,
        help="Optional cap for debugging raw Claude parsing.",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    rows, pairs, meta = load_normalized_rows_from_db(con)
    raw_claude = None
    if args.raw_claude:
        raw_claude = load_raw_claude(
            meta["claude_source_files"],
            args.max_raw_claude_files,
        )
    report = build_report(rows, pairs, meta, raw_claude)
    json_path, md_path = write_outputs(report, args.output_dir)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
