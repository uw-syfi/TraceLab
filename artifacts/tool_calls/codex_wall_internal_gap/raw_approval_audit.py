#!/usr/bin/env python3
"""Join Codex normalized tool latency rows with raw function-call approval arguments."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402

EXECUTION_LIKE_TOOLS = {"exec_command", "shell_command", "shell", "apply_patch"}
WALL_TIME_RE = re.compile(r"^Wall time:\s*([0-9]+(?:\.[0-9]+)?)\s*seconds\b", re.MULTILINE)


def parse_json_maybe(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def delta_ms(start: Any, end: Any) -> int | None:
    started = parse_ts(start)
    finished = parse_ts(end)
    if started is None or finished is None:
        return None
    return round((finished - started).total_seconds() * 1000)


def internal_ms_from_output(output: Any) -> int | None:
    if not isinstance(output, str):
        return None
    match = WALL_TIME_RE.search(output)
    if not match:
        return None
    return round(float(match.group(1)) * 1000)


def iter_session_roots(home_root: Path) -> list[Path]:
    roots: list[Path] = []
    for home in sorted(home_root.iterdir()):
        sessions = home / ".codex" / "sessions"
        try:
            if sessions.is_dir():
                roots.append(sessions)
        except OSError:
            continue
    return roots


def load_raw_calls(roots: list[Path]) -> dict[tuple[str, str], dict[str, Any]]:
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    duplicate_keys = 0
    files = 0
    records = 0
    for root in roots:
        for path in sorted(root.rglob("*.jsonl")):
            files += 1
            session_id: str | None = None
            try:
                fh = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line_no, line in enumerate(fh, start=1):
                    records += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    if obj.get("type") == "session_meta":
                        sid = payload.get("id")
                        if isinstance(sid, str):
                            session_id = sid
                        continue
                    if obj.get("type") != "response_item":
                        continue
                    payload_type = payload.get("type")
                    if payload_type not in {"function_call", "custom_tool_call"}:
                        continue
                    call_id = payload.get("call_id")
                    name = payload.get("name")
                    if not isinstance(session_id, str) or not isinstance(call_id, str):
                        continue
                    if name not in EXECUTION_LIKE_TOOLS and name != "write_stdin":
                        continue
                    raw_args = payload.get("arguments")
                    if raw_args is None:
                        raw_args = payload.get("input")
                    args = parse_json_maybe(raw_args)
                    key = (f"codex:{session_id}", call_id)
                    if key in calls:
                        duplicate_keys += 1
                    calls[key] = {
                        "raw_tool_name": name,
                        "raw_timestamp": obj.get("timestamp"),
                        "raw_path": str(path),
                        "raw_line": line_no,
                        "sandbox_permissions": args.get("sandbox_permissions"),
                        "has_prefix_rule": isinstance(args.get("prefix_rule"), list),
                        "has_justification": isinstance(args.get("justification"), str)
                        and bool(args.get("justification")),
                        "arg_keys": ";".join(sorted(str(k) for k in args)),
                    }
    calls[("__meta__", "__meta__")] = {
        "files": files,
        "records": records,
        "duplicate_keys": duplicate_keys,
    }
    return calls


def load_raw_completed_calls(roots: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    calls: dict[tuple[str, str], dict[str, Any]] = {}
    assessments: dict[str, list[dict[str, Any]]] = defaultdict(list)
    files = 0
    records = 0

    def finish_call(call_id: Any, timestamp: Any, output: Any, is_error: Any = None) -> None:
        if not isinstance(call_id, str):
            return
        candidates = [key for key in calls if key[1] == call_id]
        if not candidates:
            return
        key = candidates[-1]
        call = calls[key]
        if call.get("result_at") is not None:
            return
        call["result_at"] = timestamp
        call["wall_ms"] = delta_ms(call.get("emitted_at"), timestamp)
        call["internal_ms"] = internal_ms_from_output(output)
        call["is_error"] = is_error

    for root in roots:
        for path in sorted(root.rglob("*.jsonl")):
            files += 1
            session_id: str | None = None
            try:
                fh = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with fh:
                for line_no, line in enumerate(fh, start=1):
                    records += 1
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    timestamp = obj.get("timestamp")
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    top_type = obj.get("type")
                    payload_type = payload.get("type")
                    if top_type == "session_meta":
                        sid = payload.get("id")
                        if isinstance(sid, str):
                            session_id = f"codex:{sid}"
                        continue
                    if top_type == "response_item" and payload_type in {
                        "function_call",
                        "custom_tool_call",
                    }:
                        call_id = payload.get("call_id")
                        name = payload.get("name")
                        if (
                            not isinstance(session_id, str)
                            or not isinstance(call_id, str)
                            or name not in EXECUTION_LIKE_TOOLS | {"write_stdin", "request_user_input"}
                        ):
                            continue
                        raw_args = payload.get("arguments")
                        if raw_args is None:
                            raw_args = payload.get("input")
                        args = parse_json_maybe(raw_args)
                        calls[(session_id, call_id)] = {
                            "session_id": session_id,
                            "tool_call_id": call_id,
                            "tool_name": name,
                            "emitted_at": timestamp,
                            "result_at": None,
                            "raw_path": str(path),
                            "raw_line": line_no,
                            "sandbox_permissions": args.get("sandbox_permissions"),
                            "has_prefix_rule": isinstance(args.get("prefix_rule"), list),
                            "has_justification": isinstance(args.get("justification"), str)
                            and bool(args.get("justification")),
                            "arg_keys": ";".join(sorted(str(k) for k in args)),
                        }
                        continue
                    if top_type == "response_item" and payload_type in {
                        "function_call_output",
                        "custom_tool_call_output",
                    }:
                        finish_call(payload.get("call_id"), timestamp, payload.get("output"))
                        continue
                    if top_type == "event_msg" and payload_type == "exec_command_end":
                        exit_code = payload.get("exit_code")
                        is_error = exit_code != 0 if isinstance(exit_code, int) else None
                        finish_call(payload.get("call_id"), timestamp, payload.get("aggregated_output"), is_error)
                        continue
                    if top_type == "event_msg" and payload_type == "patch_apply_end":
                        success = payload.get("success")
                        output = (payload.get("stdout") or "") + (payload.get("stderr") or "")
                        is_error = not success if isinstance(success, bool) else None
                        finish_call(payload.get("call_id"), timestamp, output, is_error)
                        continue
                    if top_type == "event_msg" and payload_type == "guardian_assessment":
                        call_id = payload.get("target_item_id")
                        if isinstance(call_id, str):
                            assessments[call_id].append({"timestamp": timestamp, **payload})
                        continue

    for (_session_id, call_id), call in calls.items():
        call_assessments = assessments.get(call_id, [])
        if call_assessments:
            last = call_assessments[-1]
            call["guardian_status"] = last.get("status")
            call["guardian_decision_source"] = last.get("decision_source")
            approved = [item for item in call_assessments if item.get("status") == "approved"]
            call["guardian_approval_latency_ms"] = (
                delta_ms(call.get("emitted_at"), approved[-1].get("timestamp")) if approved else None
            )
        else:
            call["guardian_status"] = None
            call["guardian_decision_source"] = None
            call["guardian_approval_latency_ms"] = None
    completed = [call for call in calls.values() if call.get("wall_ms") is not None]
    return completed, {"files": files, "records": records, "calls": len(calls)}


def normalized_rows(con) -> list[dict[str, Any]]:
    query = """
        SELECT
          r.session_id,
          r.model,
          r.round_index,
          tc.tool_index,
          tc.tool_call_id,
          tc.tool_name,
          tc.tool_wall_latency_ms AS wall_ms,
          tc.tool_internal_latency_ms AS internal_ms,
          GREATEST(tc.tool_wall_latency_ms - tc.tool_internal_latency_ms, 0) AS gap_ms,
          tc.input_chars,
          tc.result_chars,
          tc.is_error
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE r.provider = 'codex'
          AND tc.tool_name IN ('exec_command', 'shell_command', 'shell', 'apply_patch', 'write_stdin')
          AND tc.tool_wall_latency_ms IS NOT NULL
          AND tc.tool_internal_latency_ms IS NOT NULL
          AND tc.tool_wall_latency_ms > 0
          AND tc.tool_internal_latency_ms >= 0
    """
    cur = con.execute(query)
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def bucket_for_gap(gap_ms: int | float) -> str:
    if gap_ms <= 1_000:
        return "<=1s"
    if gap_ms <= 10_000:
        return "1-10s"
    if gap_ms <= 60_000:
        return "10s-1m"
    if gap_ms <= 600_000:
        return "1-10m"
    if gap_ms <= 3_600_000:
        return "10m-1h"
    return ">1h"


def approval_class(raw: dict[str, Any] | None) -> str:
    if raw is None:
        return "raw_missing"
    if raw.get("sandbox_permissions") == "require_escalated":
        return "require_escalated"
    if raw.get("sandbox_permissions"):
        return f"sandbox_{raw['sandbox_permissions']}"
    return "no_escalation_arg"


def guardian_class(raw: dict[str, Any]) -> str:
    if raw.get("guardian_status") is not None:
        return f"guardian_{raw.get('guardian_status')}_{raw.get('guardian_decision_source')}"
    if raw.get("sandbox_permissions") != "require_escalated":
        return "no_escalation_arg"
    return "approval_requested_no_guardian_record"


def summarize_raw_only(
    rows: list[dict[str, Any]],
    *,
    class_func=approval_class,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        cls = class_func(row)
        internal = row.get("internal_ms")
        wall = row.get("wall_ms")
        gap = max((wall or 0) - (internal or 0), 0) if internal is not None else None
        for target, key in [
            (groups, (row["tool_name"], cls)),
            (buckets, (row["tool_name"], cls, bucket_for_gap(gap or 0) if gap is not None else "no_internal")),
        ]:
            entry = target.setdefault(
                key,
                {
                    "tool_name": row["tool_name"],
                    "approval_class": cls,
                    "gap_bucket": key[2] if len(key) > 2 else "all",
                    "calls": 0,
                    "both_timed_calls": 0,
                    "wall_total_s": 0.0,
                    "internal_total_s": 0.0,
                    "gap_total_s": 0.0,
                    "gap_gt_1m": 0,
                    "approval_latency_count": 0,
                    "approval_latency_total_s": 0.0,
                },
            )
            entry["calls"] += 1
            entry["wall_total_s"] += (wall or 0) / 1000.0
            approval_latency = row.get("guardian_approval_latency_ms")
            if approval_latency is not None:
                entry["approval_latency_count"] += 1
                entry["approval_latency_total_s"] += approval_latency / 1000.0
            if internal is not None:
                entry["both_timed_calls"] += 1
                entry["internal_total_s"] += internal / 1000.0
                entry["gap_total_s"] += (gap or 0) / 1000.0
                entry["gap_gt_1m"] += 1 if (gap or 0) > 60_000 else 0
    return list(groups.values()), list(buckets.values())


def summarize(rows: list[dict[str, Any]], raw_calls: dict[tuple[str, str], dict[str, Any]]):
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    bucket_groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    examples: list[dict[str, Any]] = []
    for row in rows:
        raw = raw_calls.get((row["session_id"], row["tool_call_id"]))
        cls = approval_class(raw)
        key = (row["tool_name"], cls, "all")
        bucket_key = (row["tool_name"], cls, bucket_for_gap(row["gap_ms"]))
        for target_key, target in [(key, groups), (bucket_key, bucket_groups)]:
            entry = target.setdefault(
                target_key,
                {
                    "tool_name": target_key[0],
                    "approval_class": target_key[1],
                    "gap_bucket": target_key[2],
                    "calls": 0,
                    "wall_total_s": 0.0,
                    "internal_total_s": 0.0,
                    "gap_total_s": 0.0,
                    "gap_gt_1m": 0,
                },
            )
            entry["calls"] += 1
            entry["wall_total_s"] += row["wall_ms"] / 1000.0
            entry["internal_total_s"] += row["internal_ms"] / 1000.0
            entry["gap_total_s"] += row["gap_ms"] / 1000.0
            entry["gap_gt_1m"] += 1 if row["gap_ms"] > 60_000 else 0
        if raw is not None and row["gap_ms"] > 60_000:
            examples.append(
                {
                    **{k: row[k] for k in row},
                    **{
                        "approval_class": cls,
                        "sandbox_permissions": raw.get("sandbox_permissions"),
                        "has_prefix_rule": raw.get("has_prefix_rule"),
                        "has_justification": raw.get("has_justification"),
                        "raw_path": raw.get("raw_path"),
                        "raw_line": raw.get("raw_line"),
                        "arg_keys": raw.get("arg_keys"),
                    },
                }
            )
    return list(groups.values()), list(bucket_groups.values()), examples


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = dict(row)
            for key in ("wall_total_s", "internal_total_s", "gap_total_s"):
                if key in out and isinstance(out[key], float) and math.isfinite(out[key]):
                    out[key] = round(out[key], 3)
            writer.writerow(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument("--home-root", type=Path, default=Path("/home"))
    parser.add_argument("--raw-root", action="append", type=Path, default=[])
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    roots = args.raw_root or iter_session_roots(args.home_root)
    raw_calls = load_raw_calls(roots)
    meta = raw_calls.pop(("__meta__", "__meta__"))
    raw_completed, raw_completed_meta = load_raw_completed_calls(roots)
    rows = normalized_rows(con)
    summary, buckets, examples = summarize(rows, raw_calls)
    raw_only_summary, raw_only_buckets = summarize_raw_only(raw_completed)
    raw_guardian_summary, raw_guardian_buckets = summarize_raw_only(
        raw_completed,
        class_func=guardian_class,
    )

    summary.sort(key=lambda r: (r["tool_name"], r["approval_class"]))
    buckets.sort(key=lambda r: (r["tool_name"], r["approval_class"], r["gap_bucket"]))
    examples.sort(key=lambda r: r["gap_ms"], reverse=True)
    raw_only_summary.sort(key=lambda r: (r["tool_name"], r["approval_class"]))
    raw_only_buckets.sort(key=lambda r: (r["tool_name"], r["approval_class"], r["gap_bucket"]))
    raw_guardian_summary.sort(key=lambda r: (r["tool_name"], r["approval_class"]))
    raw_guardian_buckets.sort(key=lambda r: (r["tool_name"], r["approval_class"], r["gap_bucket"]))

    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(
        out_dir / "codex_raw_approval_gap_by_tool.csv",
        summary,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    write_csv(
        out_dir / "codex_raw_approval_gap_buckets.csv",
        buckets,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    write_csv(
        out_dir / "codex_accessible_raw_guardian_by_tool.csv",
        raw_guardian_summary,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "both_timed_calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    write_csv(
        out_dir / "codex_accessible_raw_guardian_buckets.csv",
        raw_guardian_buckets,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "both_timed_calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    write_csv(
        out_dir / "codex_raw_approval_gap_examples.csv",
        examples[:200],
        [
            "session_id",
            "model",
            "round_index",
            "tool_index",
            "tool_call_id",
            "tool_name",
            "wall_ms",
            "internal_ms",
            "gap_ms",
            "input_chars",
            "result_chars",
            "is_error",
            "approval_class",
            "sandbox_permissions",
            "has_prefix_rule",
            "has_justification",
            "raw_path",
            "raw_line",
            "arg_keys",
        ],
    )
    write_csv(
        out_dir / "codex_accessible_raw_approval_by_tool.csv",
        raw_only_summary,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "both_timed_calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    write_csv(
        out_dir / "codex_accessible_raw_approval_buckets.csv",
        raw_only_buckets,
        [
            "tool_name",
            "approval_class",
            "gap_bucket",
            "calls",
            "both_timed_calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_gt_1m",
            "approval_latency_count",
            "approval_latency_total_s",
        ],
    )
    matched = sum(
        1 for row in rows if (row["session_id"], row["tool_call_id"]) in raw_calls
    )
    require = [r for r in summary if r["approval_class"] == "require_escalated"]
    normal = [r for r in summary if r["approval_class"] == "no_escalation_arg"]
    missing = [r for r in summary if r["approval_class"] == "raw_missing"]
    lines = [
        "# Raw approval argument audit",
        "",
        f"- Raw roots scanned: {len(roots)}",
        f"- Raw files scanned: {meta['files']:,}",
        f"- Raw records scanned: {meta['records']:,}",
        f"- Normalized rows considered: {len(rows):,}",
        f"- Matched raw calls: {matched:,}",
        f"- Unmatched normalized calls: {len(rows) - matched:,}",
        f"- Accessible raw completed calls: {len(raw_completed):,}",
        "",
        "Approval class uses raw function-call arguments: `require_escalated` means the model asked to run outside the sandbox; `no_escalation_arg` means the call had no such argument.",
        "",
        "## Totals",
        "",
        "| class | calls | residual hours | gap >1m calls |",
        "|---|---:|---:|---:|",
    ]
    for name, group in [
        ("require_escalated", require),
        ("no_escalation_arg", normal),
        ("raw_missing", missing),
    ]:
        calls = sum(int(r["calls"]) for r in group)
        gap_h = sum(float(r["gap_total_s"]) for r in group) / 3600.0
        gt_1m = sum(int(r["gap_gt_1m"]) for r in group)
        lines.append(f"| {name} | {calls:,} | {gap_h:.2f} | {gt_1m:,} |")
    lines.extend(
        [
            "",
            "## Accessible raw-only totals",
            "",
            "These are computed directly from currently accessible `/home/*/.codex/sessions` files. "
            "They do not join to the merged normalized trace above.",
            "",
            "| class | calls | both-timed calls | residual hours | gap >1m calls |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for name in ["require_escalated", "no_escalation_arg", "raw_missing"]:
        group = [r for r in raw_only_summary if r["approval_class"] == name]
        calls = sum(int(r["calls"]) for r in group)
        both = sum(int(r["both_timed_calls"]) for r in group)
        gap_h = sum(float(r["gap_total_s"]) for r in group) / 3600.0
        gt_1m = sum(int(r["gap_gt_1m"]) for r in group)
        lines.append(f"| {name} | {calls:,} | {both:,} | {gap_h:.2f} | {gt_1m:,} |")
    lines.extend(
        [
            "",
            "## Accessible raw-only guardian split",
            "",
            "`guardian_approved_agent` is an explicit auto-review approval record. "
            "`approval_requested_no_guardian_record` means the call requested escalation, "
            "but this raw file has no guardian/auto-review decision record.",
            "",
            "| class | calls | both-timed calls | residual hours | gap >1m calls | approval latency seconds |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name in [
        "guardian_approved_agent",
        "approval_requested_no_guardian_record",
        "no_escalation_arg",
    ]:
        group = [r for r in raw_guardian_summary if r["approval_class"] == name]
        calls = sum(int(r["calls"]) for r in group)
        both = sum(int(r["both_timed_calls"]) for r in group)
        gap_h = sum(float(r["gap_total_s"]) for r in group) / 3600.0
        gt_1m = sum(int(r["gap_gt_1m"]) for r in group)
        approval_s = sum(float(r["approval_latency_total_s"]) for r in group)
        lines.append(f"| {name} | {calls:,} | {both:,} | {gap_h:.2f} | {gt_1m:,} | {approval_s:.1f} |")
    (out_dir / "raw_approval_result_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote raw approval audit to {out_dir}")


if __name__ == "__main__":
    main()
