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
PROCESS_RUNNING_RE = re.compile(r"Process running with session ID\s+([A-Za-z0-9_-]+)")
CONTINUATION_FAILURE_RE = re.compile(
    r"(?:write_stdin failed|no running session|stdin is closed for this session|"
    r"session(?: ID)? [A-Za-z0-9_-]+ (?:was )?not found)",
    re.IGNORECASE,
)
COMMAND_ABORTED_RE = re.compile(r"^aborted by user(?: after [0-9.]+s)?\s*$", re.IGNORECASE)
COMMAND_FAILURE_RE = re.compile(
    r"^(?:exec_command failed\b|failed to parse function arguments\b)",
    re.IGNORECASE,
)
WRAPPED_OUTPUT_MARKER = "\nOutput:\n"
PROCESS_ROOT_TOOLS = frozenset(("exec_command",))
PROCESS_CONTINUATION_TOOLS = frozenset(("write_stdin",))
TERMINAL_COMMAND_STATUSES = frozenset(("finished", "aborted", "failed", "session_error"))
ERROR_COMMAND_STATUSES = TERMINAL_COMMAND_STATUSES - {"finished"}


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


def output_metadata(output: Any) -> dict[str, Any] | None:
    if not isinstance(output, str):
        return None
    candidates = [output]
    if WRAPPED_OUTPUT_MARKER in output:
        candidates.append(output.rsplit(WRAPPED_OUTPUT_MARKER, 1)[1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("metadata"), dict):
            return parsed["metadata"]
    return None


def exit_code_from_output(output: Any) -> int | None:
    if not isinstance(output, str):
        return None
    match = EXIT_CODE_RE.search(output)
    if match:
        return int(match.group(1))
    metadata = output_metadata(output)
    if metadata is not None and isinstance(metadata.get("exit_code"), int):
        return metadata["exit_code"]
    return None


def infer_error_from_output(output: Any) -> bool | None:
    exit_code = exit_code_from_output(output)
    if exit_code is not None:
        return exit_code != 0
    metadata = output_metadata(output)
    if metadata is not None:
        success = metadata.get("success")
        if isinstance(success, bool):
            return not success
    if isinstance(output, str) and (
        COMMAND_ABORTED_RE.match(output) or COMMAND_FAILURE_RE.match(output)
    ):
        return True
    return None


def tool_input_dict(input_value: Any) -> dict[str, Any] | None:
    if isinstance(input_value, dict):
        return input_value
    if not isinstance(input_value, str):
        return None
    try:
        parsed = json.loads(input_value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def process_session_from_input(input_value: Any) -> str | None:
    parsed = tool_input_dict(input_value)
    if parsed is None or parsed.get("session_id") is None:
        return None
    return str(parsed["session_id"])


def command_observation(
    output: Any,
    tool_name: str | None,
    *,
    explicit_exit_code: int | None = None,
) -> tuple[str | None, str | None, int | None]:
    """Return ``(status, runner_session_id, exit_code)`` from one command result."""
    if tool_name not in PROCESS_ROOT_TOOLS | PROCESS_CONTINUATION_TOOLS:
        return None, None, None
    if not isinstance(output, str):
        output = ""
    if COMMAND_ABORTED_RE.match(output):
        return "aborted", None, None
    if COMMAND_FAILURE_RE.match(output):
        return "failed", None, None
    running = PROCESS_RUNNING_RE.search(output)
    if running:
        return "running", running.group(1), None

    exit_code = explicit_exit_code
    if exit_code is None:
        exit_code = exit_code_from_output(output)
    if exit_code is not None:
        return "finished", None, exit_code

    metadata = output_metadata(output)
    if metadata is not None and isinstance(metadata.get("success"), bool):
        return "finished", None, None

    if tool_name in PROCESS_CONTINUATION_TOOLS and CONTINUATION_FAILURE_RE.search(output):
        return "session_error", None, None
    return None, None, None


def add_tool(
    *,
    tools: list[dict[str, Any]],
    tool_by_id: dict[str, dict[str, Any]],
    call_id: str,
    tool_name: str | None,
    emitted_at: str | None,
    input_value: Any,
    process_roots: dict[str, dict[str, Any]],
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
    if tool_name in PROCESS_CONTINUATION_TOOLS:
        process_session_id = process_session_from_input(input_value)
        tool["_process_session_id"] = process_session_id
        root = process_roots.get(process_session_id) if process_session_id is not None else None
        if root is not None:
            tool["continuation_of_tool_call_id"] = root.get("tool_call_id")
    tools.append(tool)
    tool_by_id[call_id] = tool
    return tool


def apply_tool_output(
    tool: dict[str, Any],
    timestamp: str | None,
    output: Any,
    is_error: bool | None,
    process_roots: dict[str, dict[str, Any]],
    *,
    explicit_exit_code: int | None = None,
    observation_source: str,
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

    status, observed_session_id, exit_code = command_observation(
        output,
        tool.get("tool_name"),
        explicit_exit_code=explicit_exit_code,
    )
    if status in ERROR_COMMAND_STATUSES and is_error is None:
        tool["is_error"] = True

    if observed_session_id is not None:
        tool["_process_session_id"] = observed_session_id
    process_session_id = tool.get("_process_session_id")

    if tool.get("tool_name") in PROCESS_ROOT_TOOLS and process_session_id is not None:
        process_roots[process_session_id] = tool
    root = (
        process_roots.get(process_session_id)
        if isinstance(process_session_id, str)
        else None
    )
    if root is not None and tool.get("tool_name") in PROCESS_CONTINUATION_TOOLS:
        tool["continuation_of_tool_call_id"] = root.get("tool_call_id")

    # A model-facing function result has higher priority than the runner's exec_command_end event.
    # This resolves the race where exec_command_end says the OS process has finished, but the result
    # returned to the model says it must continue through write_stdin. Within the same priority,
    # a terminal status is monotonic so a replayed "running" result cannot undo it.
    if status is not None:
        source_priority = 2 if observation_source.endswith("function_call_output") else 1
        current_source_priority = tool.get("_command_status_source_priority", -1)
        current_status = tool.get("command_status")
        should_replace = (
            current_status is None
            or source_priority > current_source_priority
            or (
                source_priority == current_source_priority
                and current_status not in TERMINAL_COMMAND_STATUSES
            )
        )
        if should_replace:
            tool["command_status"] = status
            tool["_command_status_source_priority"] = source_priority
            if exit_code is None:
                tool.pop("command_exit_code", None)
            else:
                tool["command_exit_code"] = exit_code
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
    # Hierarchy of a spawned child, read once from the child's own session_meta. Kept apart from
    # parent_session_meta_id, which is re-assigned below when the file replays its parent's history.
    spawn_parent_id: str | None = None
    spawn_role: str | None = None
    # Codex's automatic review runs as its own rollout (source.subagent.other == "guardian"). It
    # is not spawned by the user's thread, but its session_meta names the thread it reviewed
    # (parent_thread_id); that links the review to the reviewed session. It replays no history.
    is_guardian_file = False
    review_parent_id: str | None = None
    # Other sub-sessions Codex starts on its own (source.subagent = "review" / "compact", or
    # {"other": <name>}): delegated work with no recorded parent. Holds the variant name.
    other_subagent_role: str | None = None
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
    # Runner process-session id -> originating exec_command tool. Tool dicts remain shared with
    # already-built round objects, so a terminal write_stdin can back-fill its root call later.
    process_roots: dict[str, dict[str, Any]] = {}
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
                                spawn_parent_id = parent_id
                                role = spawn.get("agent_role")
                                spawn_role = role if isinstance(role, str) and role else None
                            subagent = source.get("subagent")
                            other = subagent.get("other") if isinstance(subagent, dict) else subagent
                            if other == "guardian":
                                is_guardian_file = True
                            elif isinstance(other, str) and other:
                                other_subagent_role = other
                        if payload.get("thread_source") == "guardian_review":
                            is_guardian_file = True
                        reviewed = payload.get("parent_thread_id")
                        if is_guardian_file and isinstance(reviewed, str) and reviewed:
                            review_parent_id = reviewed
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
                        process_roots=process_roots,
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
                        process_roots=process_roots,
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
                    if apply_tool_output(
                        tool,
                        timestamp,
                        output,
                        is_error,
                        process_roots,
                        observation_source="function_call_output",
                    ):
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
                    if apply_tool_output(
                        tool,
                        timestamp,
                        output,
                        is_error,
                        process_roots,
                        observation_source="custom_tool_call_output",
                    ):
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
                        process_roots,
                        explicit_exit_code=exit_code if isinstance(exit_code, int) else None,
                        observation_source="exec_command_end",
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
                        process_roots,
                        observation_source="patch_apply_end",
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
                if current_model is None:
                    # Subagent rollout files can replay a parent session before
                    # the child's first turn_context. Those token_count records
                    # have no attributable model and are not new child LLM
                    # invocations. Discard their accumulated replay state so it
                    # cannot leak into the first live child round.
                    pending_input_events = []
                    segment_timing_events = []
                    segment_tools = []
                    tool_by_id.clear()
                    process_roots.clear()
                    if raw_sink is not None:
                        raw_pending_in = []
                        raw_segment_out = []
                        raw_segment_tools = []
                        raw_tool_by_id.clear()
                    continue
                if (
                    not pending_input_events
                    and not segment_timing_events
                    and not segment_tools
                ):
                    # Forked/resumed rollout files can replay historical
                    # token_count events at startup after a turn_context has
                    # already set current_model. With no input, output, or tool
                    # activity, this is accounting replay rather than a new LLM
                    # invocation. Do not advance last_total_sig: a later live
                    # segment with the same cumulative total must remain
                    # eligible for extraction.
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
                # Live child turns belong to the spawned child; replayed parent turns keep the parent.
                is_live_child = bool(
                    is_subagent_file
                    and spawn_parent_id is not None
                    and round_session_meta_id == file_session_meta_id
                )
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
                    "thread_kind": (
                        "subagent" if is_live_child or other_subagent_role
                        else "guardian" if is_guardian_file else "main"
                    ),
                    "parent_session_id": (
                        f"codex:{spawn_parent_id}" if is_live_child
                        else f"codex:{review_parent_id}"
                        if is_guardian_file and review_parent_id and not other_subagent_role
                        else None
                    ),
                    "workflow_id": None,
                    "agent_role": (  # same precedence as thread_kind above
                        spawn_role if is_live_child
                        else other_subagent_role or ("guardian" if is_guardian_file else None)
                    ),
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

    # Private extraction-only runner ids/provenance must not enter the normalized schema.
    for tool in tool_by_id.values():
        tool.pop("_process_session_id", None)
        tool.pop("_command_status_source_priority", None)
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
