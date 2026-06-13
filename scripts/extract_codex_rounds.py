#!/usr/bin/env python3
"""Extract Codex LLM rounds with normalized token and tool-call metadata."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from extract_claude_rounds import (
    _raw_text,
    append_timing_event,
    apply_input_event_summary,
    content_chars,
    parse_ts,
    round_key,
    write_rounds_jsonl,
)


EXIT_CODE_RE = re.compile(r"(?:Exit code|Process exited with code)[: ]+(-?\d+)")
WALL_TIME_RE = re.compile(r"^Wall time:\s*([0-9]+(?:\.[0-9]+)?)\s*seconds\b", re.MULTILINE)


def usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) else 0


def timestamp_delta_ms(start: str | None, end: str | None) -> int | None:
    started = parse_ts(start)
    finished = parse_ts(end)
    if started is None or finished is None:
        return None
    return round((finished - started).total_seconds() * 1000)


def wall_time_ms_from_output(output: Any) -> int | None:
    if not isinstance(output, str):
        return None
    match = WALL_TIME_RE.search(output)
    if not match:
        return None
    return round(float(match.group(1)) * 1000)


def infer_error_from_output(output: Any) -> bool | None:
    if not isinstance(output, str):
        return None
    match = EXIT_CODE_RE.search(output)
    if match:
        return int(match.group(1)) != 0
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    metadata = parsed.get("metadata")
    if isinstance(metadata, dict):
        exit_code = metadata.get("exit_code")
        if isinstance(exit_code, int):
            return exit_code != 0
        success = metadata.get("success")
        if isinstance(success, bool):
            return not success
    return None


def add_tool(
    *,
    tools: list[dict[str, Any]],
    tool_by_id: dict[str, dict[str, Any]],
    call_id: str,
    tool_name: str | None,
    emitted_at: str | None,
    input_value: Any,
) -> dict[str, Any]:
    existing = tool_by_id.get(call_id)
    if existing is not None:
        return existing
    tool = {
        "tool_index": len(tools),
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "emitted_at": emitted_at,
        "result_at": None,
        "input": input_value,
        "input_chars": content_chars(input_value),
        "result_chars": None,
        "tool_wall_latency_ms": None,
        "tool_internal_latency_ms": None,
        "is_error": None,
    }
    tools.append(tool)
    tool_by_id[call_id] = tool
    return tool


def apply_tool_output(
    tool: dict[str, Any],
    timestamp: str | None,
    output: Any,
    is_error: bool | None,
) -> bool:
    first_result = tool.get("result_at") is None
    if first_result:
        tool["result_at"] = timestamp
    if tool.get("result_chars") is None:
        tool["result_chars"] = content_chars(output)
    if tool.get("tool_wall_latency_ms") is None:
        tool["tool_wall_latency_ms"] = timestamp_delta_ms(tool.get("emitted_at"), timestamp)
    if tool.get("tool_internal_latency_ms") is None:
        tool["tool_internal_latency_ms"] = wall_time_ms_from_output(output)
    if is_error is not None:
        tool["is_error"] = is_error
    return first_result


def _add_raw_tool(
    raw_tools: list[dict[str, Any]],
    raw_by_id: dict[str, dict[str, Any]],
    call_id: str,
    name: Any,
    input_value: Any,
) -> None:
    """Append a local-only raw tool entry and index it by call_id for later result back-fill."""
    raw_tool = {
        "name": name,
        "input": _raw_text(input_value),
        "result": "",
        "error": False,
    }
    raw_tools.append(raw_tool)
    raw_by_id[call_id] = raw_tool


def _fill_raw_result(
    raw_by_id: dict[str, dict[str, Any]],
    call_id: str | None,
    output: Any,
    is_error: Any,
) -> None:
    """Back-fill a raw tool's result text. The dict is shared with an already-emitted raw entry, so
    this updates the round's raw view even when the output lands in a later segment."""
    if not isinstance(call_id, str):
        return
    raw_tool = raw_by_id.get(call_id)
    if raw_tool is not None:
        raw_tool["result"] = _raw_text(output)
        raw_tool["error"] = bool(is_error)


def extract_codex_session(
    session_file: Path,
    *,
    raw_sink: dict[str, Any] | None = None,
    title_sink: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract one Codex session's rounds.

    ``raw_sink`` is an **opt-in, local-only** side channel (default ``None``): when given, per-round
    original text is collected keyed by ``trace_key``, never written into the round dict, so output
    is byte-identical to the sink-off path. ``title_sink`` is accepted for call-site symmetry with
    the Claude extractor but Codex carries no explicit conversation title, so it stays empty. The
    raw map is local-only and must NEVER enter a sanitized/contributed/uploadable artifact.
    """
    rounds: list[dict[str, Any]] = []
    session_meta_id: str | None = None
    file_session_meta_id: str | None = None
    parent_session_meta_id: str | None = None
    is_subagent_file = False
    current_turn_id: str | None = None
    current_model: str | None = None
    current_cwd: str | None = None
    last_total_sig: str | None = None
    turn_round_counts: Counter[str] = Counter()
    session_round_counts: Counter[str] = Counter()

    pending_input_events: list[dict[str, Any]] = []
    segment_timing_events: list[dict[str, Any]] = []
    segment_tools: list[dict[str, Any]] = []
    tool_by_id: dict[str, dict[str, Any]] = {}
    # Opt-in raw capture state (parallel to the round-building state above; never merged into it).
    # raw_tool_by_id is NOT reset per round (mirrors tool_by_id) so a tool whose output lands in a
    # later segment still back-fills the same dict referenced by the already-emitted raw entry.
    raw_pending_in: list[dict[str, str]] = []
    raw_segment_out: list[str] = []
    raw_segment_tools: list[dict[str, Any]] = []
    raw_tool_by_id: dict[str, dict[str, Any]] = {}

    with session_file.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            timestamp = record.get("timestamp")
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            payload_type = payload.get("type")
            top_type = record.get("type")

            if top_type == "session_meta":
                meta_id = payload.get("id")
                if isinstance(meta_id, str) and meta_id:
                    if file_session_meta_id is None:
                        file_session_meta_id = meta_id
                        session_meta_id = meta_id
                        source = payload.get("source")
                        if isinstance(source, dict):
                            spawn = (
                                source.get("subagent", {})
                                .get("thread_spawn", {})
                                if isinstance(source.get("subagent"), dict)
                                else {}
                            )
                            parent_id = spawn.get("parent_thread_id")
                            if isinstance(parent_id, str) and parent_id:
                                parent_session_meta_id = parent_id
                                is_subagent_file = True
                    elif is_subagent_file and meta_id != file_session_meta_id:
                        # Subagent files replay parent history after the child
                        # session metadata. Keep both identities: replayed
                        # parent turns should dedup against the parent session,
                        # while live child turns should remain in the child
                        # session.
                        parent_session_meta_id = meta_id
                    elif session_meta_id is None:
                        session_meta_id = meta_id
                if session_meta_id is None and isinstance(meta_id, str) and meta_id:
                    session_meta_id = meta_id
                current_cwd = payload.get("cwd") or current_cwd
                continue

            if top_type == "turn_context":
                current_turn_id = payload.get("turn_id") or current_turn_id
                current_model = payload.get("model") or current_model
                current_cwd = payload.get("cwd") or current_cwd
                continue

            if payload_type == "task_started":
                current_turn_id = payload.get("turn_id") or current_turn_id
                continue

            if top_type == "event_msg" and payload_type == "user_message":
                append_timing_event(
                    pending_input_events,
                    "user_message",
                    timestamp,
                    "event_msg.user_message",
                    content_chars=content_chars(payload.get("message")),
                )
                if raw_sink is not None:
                    raw_pending_in.append(
                        {"kind": "user", "text": _raw_text(payload.get("message"))}
                    )
                continue

            if top_type == "response_item" and payload_type == "reasoning":
                append_timing_event(
                    segment_timing_events,
                    "reasoning",
                    timestamp,
                    "response_item.reasoning",
                )
                continue

            if (
                top_type == "response_item"
                and payload_type == "message"
                and payload.get("role") == "assistant"
            ):
                append_timing_event(
                    segment_timing_events,
                    "text",
                    timestamp,
                    "response_item.message",
                    content_chars=content_chars(payload.get("content")),
                )
                if raw_sink is not None:
                    text = _raw_text(payload.get("content"))
                    if text:
                        raw_segment_out.append(text)
                continue

            if top_type == "event_msg" and payload_type == "agent_message":
                continue

            if top_type == "response_item" and payload_type == "function_call":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    tool_count = len(segment_tools)
                    add_tool(
                        tools=segment_tools,
                        tool_by_id=tool_by_id,
                        call_id=call_id,
                        tool_name=payload.get("name"),
                        emitted_at=timestamp,
                        input_value=payload.get("arguments"),
                    )
                    if len(segment_tools) > tool_count:
                        append_timing_event(
                            segment_timing_events,
                            "tool_call",
                            timestamp,
                            "response_item.function_call",
                            tool_call_id=call_id,
                            tool_name=payload.get("name"),
                            tool_index=len(segment_tools) - 1,
                        )
                        if raw_sink is not None:
                            _add_raw_tool(
                                raw_segment_tools,
                                raw_tool_by_id,
                                call_id,
                                payload.get("name"),
                                payload.get("arguments"),
                            )
                continue

            if top_type == "response_item" and payload_type == "custom_tool_call":
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    tool_count = len(segment_tools)
                    add_tool(
                        tools=segment_tools,
                        tool_by_id=tool_by_id,
                        call_id=call_id,
                        tool_name=payload.get("name"),
                        emitted_at=timestamp,
                        input_value=payload.get("input"),
                    )
                    if len(segment_tools) > tool_count:
                        append_timing_event(
                            segment_timing_events,
                            "tool_call",
                            timestamp,
                            "response_item.custom_tool_call",
                            tool_call_id=call_id,
                            tool_name=payload.get("name"),
                            tool_index=len(segment_tools) - 1,
                        )
                        if raw_sink is not None:
                            _add_raw_tool(
                                raw_segment_tools,
                                raw_tool_by_id,
                                call_id,
                                payload.get("name"),
                                payload.get("input"),
                            )
                continue

            if top_type == "response_item" and payload_type == "function_call_output":
                call_id = payload.get("call_id")
                tool = tool_by_id.get(call_id) if isinstance(call_id, str) else None
                if tool is not None:
                    output = payload.get("output")
                    is_error = infer_error_from_output(output)
                    if apply_tool_output(tool, timestamp, output, is_error):
                        append_timing_event(
                            pending_input_events,
                            "tool_result",
                            timestamp,
                            "response_item.function_call_output",
                            tool_call_id=call_id,
                            result_chars=tool.get("result_chars"),
                            is_error=tool.get("is_error"),
                        )
                        if raw_sink is not None:
                            _fill_raw_result(raw_tool_by_id, call_id, output, tool.get("is_error"))
                            raw_pending_in.append({"kind": "tool", "text": _raw_text(output)})
                continue

            if top_type == "response_item" and payload_type == "custom_tool_call_output":
                call_id = payload.get("call_id")
                tool = tool_by_id.get(call_id) if isinstance(call_id, str) else None
                if tool is not None:
                    output = payload.get("output")
                    is_error = infer_error_from_output(output)
                    if apply_tool_output(tool, timestamp, output, is_error):
                        append_timing_event(
                            pending_input_events,
                            "tool_result",
                            timestamp,
                            "response_item.custom_tool_call_output",
                            tool_call_id=call_id,
                            result_chars=tool.get("result_chars"),
                            is_error=tool.get("is_error"),
                        )
                        if raw_sink is not None:
                            _fill_raw_result(raw_tool_by_id, call_id, output, tool.get("is_error"))
                            raw_pending_in.append({"kind": "tool", "text": _raw_text(output)})
                continue

            if top_type == "event_msg" and payload_type == "exec_command_end":
                call_id = payload.get("call_id")
                tool = tool_by_id.get(call_id) if isinstance(call_id, str) else None
                if tool is not None:
                    exit_code = payload.get("exit_code")
                    is_error = exit_code != 0 if isinstance(exit_code, int) else None
                    output = payload.get("aggregated_output")
                    if apply_tool_output(
                        tool,
                        timestamp,
                        output,
                        is_error,
                    ):
                        append_timing_event(
                            pending_input_events,
                            "tool_result",
                            timestamp,
                            "event_msg.exec_command_end",
                            tool_call_id=call_id,
                            result_chars=tool.get("result_chars"),
                            is_error=tool.get("is_error"),
                        )
                        if raw_sink is not None:
                            _fill_raw_result(raw_tool_by_id, call_id, output, tool.get("is_error"))
                            raw_pending_in.append({"kind": "tool", "text": _raw_text(output)})
                continue

            if top_type == "event_msg" and payload_type == "patch_apply_end":
                call_id = payload.get("call_id")
                tool = tool_by_id.get(call_id) if isinstance(call_id, str) else None
                if tool is not None:
                    success = payload.get("success")
                    output = (payload.get("stdout") or "") + (payload.get("stderr") or "")
                    is_error = not success if isinstance(success, bool) else None
                    if apply_tool_output(
                        tool,
                        timestamp,
                        output,
                        is_error,
                    ):
                        append_timing_event(
                            pending_input_events,
                            "tool_result",
                            timestamp,
                            "event_msg.patch_apply_end",
                            tool_call_id=call_id,
                            result_chars=tool.get("result_chars"),
                            is_error=tool.get("is_error"),
                        )
                        if raw_sink is not None:
                            _fill_raw_result(raw_tool_by_id, call_id, output, tool.get("is_error"))
                            raw_pending_in.append({"kind": "tool", "text": _raw_text(output)})
                continue

            if top_type == "event_msg" and payload_type == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                last_usage = info.get("last_token_usage")
                total_usage = info.get("total_token_usage")
                if not isinstance(last_usage, dict) or not isinstance(total_usage, dict):
                    continue
                total_sig = json.dumps(total_usage, sort_keys=True)
                if total_sig == last_total_sig:
                    continue
                last_total_sig = total_sig

                turn_id = current_turn_id or session_file.stem
                turn_round_index = turn_round_counts[turn_id]
                turn_round_counts[turn_id] += 1

                input_tokens = usage_int(last_usage, "input_tokens")
                cached_tokens = usage_int(last_usage, "cached_input_tokens")
                round_session_meta_id = session_meta_id or session_file.stem
                if is_subagent_file:
                    if (
                        isinstance(current_turn_id, str)
                        and isinstance(file_session_meta_id, str)
                        and current_turn_id >= file_session_meta_id
                    ):
                        round_session_meta_id = file_session_meta_id
                    elif parent_session_meta_id is not None:
                        round_session_meta_id = parent_session_meta_id
                session_id = f"codex:{round_session_meta_id}"
                round_index = session_round_counts[session_id]
                session_round_counts[session_id] += 1
                timing_events = [
                    *pending_input_events,
                    *segment_timing_events,
                ]
                append_timing_event(
                    timing_events,
                    "usage_report",
                    timestamp,
                    "event_msg.token_count",
                )
                round_obj = {
                    "provider": "codex",
                    "session_id": session_id,
                    "session_file": str(session_file),
                    "round_index": round_index,
                    "round_id": f"{turn_id}:{turn_round_index}",
                    "turn_id": current_turn_id,
                    "cwd": current_cwd,
                    "model": current_model,
                    "input_tokens_total": input_tokens,
                    "prefix_tokens": cached_tokens,
                    "newly_append_tokens": max(0, input_tokens - cached_tokens),
                    "claude_uncached_input_tokens": None,
                    "claude_cache_creation_input_tokens": None,
                    "claude_cache_read_input_tokens": None,
                    "output_tokens": usage_int(last_usage, "output_tokens"),
                    "reasoning_output_tokens": usage_int(last_usage, "reasoning_output_tokens"),
                    "timing_events": timing_events,
                    "tools": list(segment_tools),
                }
                apply_input_event_summary(round_obj)
                for tool_index, tool in enumerate(round_obj["tools"]):
                    tool["tool_index"] = tool_index
                rounds.append(round_obj)

                if raw_sink is not None:
                    kinds = {item["kind"] for item in raw_pending_in}
                    # Keep the first occurrence by trace_key (matching write_rounds_jsonl's dedup).
                    # tools stores the shared dicts so later outputs still back-fill this entry.
                    raw_sink.setdefault(
                        round_key(round_obj),
                        {
                            "input": "\n".join(
                                item["text"] for item in raw_pending_in if item["text"]
                            ),
                            "inputKind": "user" if "user" in kinds else "tool",
                            "output": "\n".join(t for t in raw_segment_out if t),
                            "tools": list(raw_segment_tools),
                        },
                    )
                    raw_pending_in = []
                    raw_segment_out = []
                    raw_segment_tools = []

                pending_input_events = []
                segment_timing_events = []
                segment_tools = []

    return rounds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions_dir", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output JSONL path. Each line is one deduped Codex LLM round.",
    )
    parser.add_argument(
        "--append-dedup",
        action="store_true",
        help="Append only rounds whose stable trace_key is not already present in the output file.",
    )
    args = parser.parse_args()

    session_files = sorted(args.sessions_dir.rglob("*.jsonl"))
    all_rounds: list[dict[str, Any]] = []
    for session_file in session_files:
        all_rounds.extend(extract_codex_session(session_file))

    stats = write_rounds_jsonl(args.output, all_rounds, append_dedup=args.append_dedup)
    tool_calls = sum(len(r["tools"]) for r in all_rounds)
    tool_results = sum(1 for r in all_rounds for t in r["tools"] if t.get("result_at") is not None)
    print(f"sessions={len(session_files)}")
    print(f"rounds={len(all_rounds)}")
    print(f"tool_calls={tool_calls}")
    print(f"tool_results={tool_results}")
    print(f"written_rounds={stats['written_rounds']}")
    print(f"skipped_duplicate_rounds={stats['skipped_duplicate_rounds']}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
