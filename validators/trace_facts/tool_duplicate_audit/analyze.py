#!/usr/bin/env python3
"""Audit duplicate tool-call rows in the merged normalized trace."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]  # experiment -> category -> artifacts -> repo root
DEFAULT_INPUT = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
DEFAULT_OUT_DIR = Path(__file__).resolve().parent


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def effective_latency_ms(tool: dict[str, Any]) -> tuple[float | None, str]:
    internal = safe_float(tool.get("tool_internal_latency_ms"))
    if internal is not None:
        return internal, "internal"
    wall = safe_float(tool.get("tool_wall_latency_ms"))
    if wall is not None:
        return wall, "wall_fallback"
    legacy = safe_float(tool.get("latency_ms"))
    if legacy is not None:
        return legacy, "legacy_latency_ms"
    return None, "missing"


def base_session_id(provider: str, session_id: str) -> str:
    if provider == "claude" and "/subagents/" in session_id:
        return session_id.split("/subagents/", 1)[0]
    return session_id


def tool_identity(row: dict[str, Any], tool: dict[str, Any]) -> tuple[Any, ...]:
    """Signature for one logical tool call, collapsing Claude subagent copies."""
    provider = str(row.get("provider") or "<unknown-provider>")
    session_id = str(row.get("session_id") or "")
    latency_ms, source = effective_latency_ms(tool)
    return (
        provider,
        base_session_id(provider, session_id),
        str(row.get("round_id") or ""),
        str(tool.get("tool_call_id") or ""),
        str(tool.get("tool_name") or "<unknown-tool>"),
        str(tool.get("emitted_at") or ""),
        str(tool.get("result_at") or ""),
        tool.get("input_chars"),
        tool.get("result_chars"),
        latency_ms,
        source,
    )


def exact_row_identity(row: dict[str, Any], tool: dict[str, Any]) -> tuple[Any, ...]:
    """Signature for an exact duplicate normalized row/tool entry."""
    return (
        str(row.get("trace_key") or ""),
        tool.get("tool_index"),
        str(tool.get("tool_call_id") or ""),
        str(tool.get("tool_name") or "<unknown-tool>"),
        str(tool.get("emitted_at") or ""),
        str(tool.get("result_at") or ""),
        tool.get("input_chars"),
        tool.get("result_chars"),
        safe_float(tool.get("tool_internal_latency_ms")),
        safe_float(tool.get("tool_wall_latency_ms")),
    )


def add_metric(metrics: dict[str, Any], key: str, value: int | float) -> None:
    metrics[key] = metrics.get(key, 0) + value


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}"


def fmt_hours(ms: float) -> str:
    return f"{ms / 3_600_000:.2f}h"


def summarize_groups(groups: dict[tuple[Any, ...], dict[str, Any]]) -> dict[str, Any]:
    duplicate_groups = 0
    raw_rows_in_duplicate_groups = 0
    extra_rows = 0
    raw_latency_ms = 0.0
    dedup_latency_ms = 0.0
    overcount_latency_ms = 0.0
    duplicate_latency_ms = 0.0
    duplicate_groups_with_multiple_sessions = 0
    duplicate_groups_with_subagent = 0
    duplicate_groups_with_exact_same_trace = 0
    extra_rows_with_subagent = 0
    provider_extra_rows: Counter[str] = Counter()
    provider_overcount_ms: Counter[str] = Counter()
    tool_extra_rows: Counter[str] = Counter()
    tool_overcount_ms: Counter[str] = Counter()

    for signature, info in groups.items():
        count = info["count"]
        latency_ms = info["latency_ms"]
        raw_latency_ms += latency_ms * count
        dedup_latency_ms += latency_ms
        if count <= 1:
            continue

        provider = signature[0]
        tool_name = signature[4]
        extra = count - 1
        overcount = latency_ms * extra
        duplicate_groups += 1
        raw_rows_in_duplicate_groups += count
        extra_rows += extra
        overcount_latency_ms += overcount
        duplicate_latency_ms += latency_ms * count
        provider_extra_rows[provider] += extra
        provider_overcount_ms[provider] += overcount
        tool_extra_rows[tool_name] += extra
        tool_overcount_ms[tool_name] += overcount
        if len(info["sessions"]) > 1:
            duplicate_groups_with_multiple_sessions += 1
        if any("/subagents/" in session for session in info["sessions"]):
            duplicate_groups_with_subagent += 1
            extra_rows_with_subagent += extra
        if len(info["trace_keys"]) == 1:
            duplicate_groups_with_exact_same_trace += 1

    return {
        "duplicate_groups": duplicate_groups,
        "raw_rows_in_duplicate_groups": raw_rows_in_duplicate_groups,
        "extra_rows": extra_rows,
        "raw_latency_ms": raw_latency_ms,
        "dedup_latency_ms": dedup_latency_ms,
        "overcount_latency_ms": overcount_latency_ms,
        "duplicate_latency_ms": duplicate_latency_ms,
        "duplicate_groups_with_multiple_sessions": duplicate_groups_with_multiple_sessions,
        "duplicate_groups_with_subagent": duplicate_groups_with_subagent,
        "duplicate_groups_with_exact_same_trace": duplicate_groups_with_exact_same_trace,
        "extra_rows_with_subagent": extra_rows_with_subagent,
        "provider_extra_rows": provider_extra_rows,
        "provider_overcount_ms": provider_overcount_ms,
        "tool_extra_rows": tool_extra_rows,
        "tool_overcount_ms": tool_overcount_ms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--top-groups", type=int, default=200)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    raw_tool_calls = 0
    calls_with_latency = 0
    calls_missing_latency = 0
    provider_calls: Counter[str] = Counter()
    provider_calls_with_latency: Counter[str] = Counter()
    exact_groups: Counter[tuple[Any, ...]] = Counter()
    physical_groups: dict[tuple[Any, ...], dict[str, Any]] = {}

    with args.input.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                continue
            provider = str(row.get("provider") or "<unknown-provider>")
            tools = row.get("tools")
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                raw_tool_calls += 1
                provider_calls[provider] += 1
                latency_ms, source = effective_latency_ms(tool)
                if latency_ms is None:
                    calls_missing_latency += 1
                    latency_for_sum = 0.0
                else:
                    calls_with_latency += 1
                    provider_calls_with_latency[provider] += 1
                    latency_for_sum = latency_ms

                exact_groups[exact_row_identity(row, tool)] += 1
                signature = tool_identity(row, tool)
                info = physical_groups.setdefault(
                    signature,
                    {
                        "count": 0,
                        "latency_ms": latency_for_sum,
                        "sessions": set(),
                        "trace_keys": set(),
                        "line_numbers": [],
                        "source": source,
                    },
                )
                info["count"] += 1
                if isinstance(row.get("session_id"), str):
                    info["sessions"].add(row["session_id"])
                if isinstance(row.get("trace_key"), str):
                    info["trace_keys"].add(row["trace_key"])
                if len(info["line_numbers"]) < 8:
                    info["line_numbers"].append(line_no)

    metrics = summarize_groups(physical_groups)
    exact_duplicate_groups = sum(1 for count in exact_groups.values() if count > 1)
    exact_extra_rows = sum(count - 1 for count in exact_groups.values() if count > 1)

    top_rows = []
    for signature, info in physical_groups.items():
        count = info["count"]
        if count <= 1:
            continue
        provider, base_session, round_id, tool_call_id, tool_name, emitted_at, result_at, input_chars, result_chars, latency_ms, source = signature
        extra = count - 1
        top_rows.append(
            {
                "provider": provider,
                "base_session_id": base_session,
                "round_id": round_id,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "emitted_at": emitted_at,
                "result_at": result_at,
                "input_chars": input_chars,
                "result_chars": result_chars,
                "latency_source": source,
                "latency_ms": latency_ms,
                "latency_hours": latency_ms / 3_600_000 if latency_ms is not None else "",
                "raw_rows": count,
                "extra_rows": extra,
                "overcount_latency_ms": (latency_ms or 0.0) * extra,
                "overcount_latency_hours": (latency_ms or 0.0) * extra / 3_600_000,
                "session_count": len(info["sessions"]),
                "trace_key_count": len(info["trace_keys"]),
                "has_subagent_session": any("/subagents/" in s for s in info["sessions"]),
                "sample_sessions": ";".join(sorted(info["sessions"])[:8]),
                "sample_trace_keys": ";".join(sorted(info["trace_keys"])[:8]),
                "sample_line_numbers": ";".join(str(n) for n in info["line_numbers"]),
            }
        )
    top_rows.sort(
        key=lambda row: (row["overcount_latency_ms"], row["extra_rows"]),
        reverse=True,
    )

    detail_path = args.output_dir / "duplicate_tool_groups.csv"
    fieldnames = [
        "provider",
        "base_session_id",
        "round_id",
        "tool_call_id",
        "tool_name",
        "emitted_at",
        "result_at",
        "input_chars",
        "result_chars",
        "latency_source",
        "latency_ms",
        "latency_hours",
        "raw_rows",
        "extra_rows",
        "overcount_latency_ms",
        "overcount_latency_hours",
        "session_count",
        "trace_key_count",
        "has_subagent_session",
        "sample_sessions",
        "sample_trace_keys",
        "sample_line_numbers",
    ]
    with detail_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(top_rows[: args.top_groups])

    provider_lines = [
        f"- {provider}: {fmt_int(extra)} extra rows, "
        f"{fmt_hours(metrics['provider_overcount_ms'][provider])} latency"
        for provider, extra in metrics["provider_extra_rows"].most_common()
    ]
    tool_lines = [
        f"- {tool}: {fmt_int(extra)} extra rows, "
        f"{fmt_hours(metrics['tool_overcount_ms'][tool])} latency"
        for tool, extra in metrics["tool_extra_rows"].most_common(20)
    ]
    top_group_lines = [
        "- "
        f"{row['raw_rows']}x {row['tool_name']} provider={row['provider']} "
        f"extra={row['extra_rows']} overcount={row['overcount_latency_hours']:.2f}h "
        f"round={row['round_id']} tool={row['tool_call_id']} "
        f"subagent={row['has_subagent_session']}"
        for row in top_rows[:15]
    ]

    analysis_path = args.output_dir / "result_analysis.md"
    analysis_path.write_text(
        "\n".join(
            [
                "# Tool duplicate audit",
                "",
                f"Input: `{args.input}`",
                "",
                "## Definitions",
                "",
                "- Exact duplicate normalized row: same `trace_key`, `tool_index`, `tool_call_id`, timestamps, sizes, and latencies.",
                "- Physical-call signature: same provider, base session, round id, tool id, tool name, timestamps, sizes, effective latency, and latency source. For Claude, `/subagents/...` is stripped from the session id before grouping.",
                "",
                "## Summary",
                "",
                f"- Raw tool rows: {fmt_int(raw_tool_calls)}",
                f"- Raw tool rows with effective latency: {fmt_int(calls_with_latency)}",
                f"- Raw tool rows missing effective latency: {fmt_int(calls_missing_latency)}",
                f"- Physical-call duplicate groups: {fmt_int(metrics['duplicate_groups'])}",
                f"- Raw rows inside duplicate physical-call groups: {fmt_int(metrics['raw_rows_in_duplicate_groups'])}",
                f"- Extra duplicate physical-call rows: {fmt_int(metrics['extra_rows'])}",
                f"- Deduped physical tool calls: {fmt_int(raw_tool_calls - metrics['extra_rows'])}",
                f"- Raw effective latency: {fmt_hours(metrics['raw_latency_ms'])}",
                f"- Signature-deduped effective latency: {fmt_hours(metrics['dedup_latency_ms'])}",
                f"- Effective-latency overcount from duplicate physical-call rows: {fmt_hours(metrics['overcount_latency_ms'])}",
                f"- Duplicate groups spanning multiple sessions: {fmt_int(metrics['duplicate_groups_with_multiple_sessions'])}",
                f"- Duplicate groups involving Claude subagent session rows: {fmt_int(metrics['duplicate_groups_with_subagent'])}",
                f"- Extra duplicate rows involving Claude subagent session rows: {fmt_int(metrics['extra_rows_with_subagent'])}",
                f"- Exact duplicate normalized-row groups: {fmt_int(exact_duplicate_groups)}",
                f"- Extra exact duplicate normalized rows: {fmt_int(exact_extra_rows)}",
                "",
                "## Provider duplicate overcount",
                "",
                "\n".join(provider_lines) if provider_lines else "- none",
                "",
                "## Tool-name duplicate overcount",
                "",
                "\n".join(tool_lines) if tool_lines else "- none",
                "",
                "## Top duplicate groups by latency overcount",
                "",
                "\n".join(top_group_lines) if top_group_lines else "- none",
                "",
                f"Detail CSV: `{detail_path}`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"wrote {detail_path}")
    print(f"wrote {analysis_path}")
    print(f"raw_tool_rows={raw_tool_calls}")
    print(f"extra_duplicate_physical_rows={metrics['extra_rows']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
