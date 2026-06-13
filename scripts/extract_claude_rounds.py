#!/usr/bin/env python3
"""Extract Claude Code LLM rounds with nested tool-call metadata."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def content_chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def usage_int(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    return value if isinstance(value, int) else 0


def event_int(event: dict[str, Any], key: str) -> int:
    value = event.get(key, 0)
    return value if isinstance(value, int) else 0


def has_usage_tokens(usage: Any) -> bool:
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


def apply_usage(round_obj: dict[str, Any], msg: dict[str, Any]) -> None:
    """Refresh token accounting from the latest usage-bearing Claude record.

    Claude Code can emit multiple assistant records with the same message id.
    Usage is message-level accounting, not per-content-block accounting, and it
    is often repeated on thinking/text/tool_use records. Earlier records may
    contain partial placeholder usage, while later records for the same message
    include completed text/tool-use accounting. Rarely, a duplicate later record
    can carry zero placeholder usage. Keep the best complete-looking snapshot
    rather than blindly trusting the last record.
    """
    usage = msg.get("usage")
    if not has_usage_tokens(usage):
        return
    assert isinstance(usage, dict)
    uncached = usage_int(usage, "input_tokens")
    cache_write = usage_int(usage, "cache_creation_input_tokens")
    cache_read = usage_int(usage, "cache_read_input_tokens")
    output_tokens = usage_int(usage, "output_tokens")
    usage_score = (
        output_tokens,
        uncached + cache_write + cache_read,
        int(msg.get("stop_reason") is not None),
    )
    existing_score = round_obj.get("_usage_score")
    if isinstance(existing_score, tuple) and usage_score < existing_score:
        return

    round_obj["input_tokens_total"] = uncached + cache_write + cache_read
    round_obj["prefix_tokens"] = cache_read
    round_obj["newly_append_tokens"] = uncached + cache_write
    round_obj["claude_uncached_input_tokens"] = uncached
    round_obj["claude_cache_creation_input_tokens"] = cache_write
    round_obj["claude_cache_read_input_tokens"] = cache_read
    round_obj["output_tokens"] = output_tokens
    round_obj["_usage_score"] = usage_score


def duration_ms_from_mapping(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in ("durationMs", "duration_ms"):
        duration = value.get(key)
        if isinstance(duration, (int, float)) and duration >= 0:
            return round(float(duration))
    for key in ("durationSeconds", "duration_seconds"):
        duration = value.get(key)
        if isinstance(duration, (int, float)) and duration >= 0:
            return round(float(duration) * 1000)
    return None


def make_timing_event(
    event_type: str,
    timestamp: str | None,
    source: str,
    **attrs: Any,
) -> dict[str, Any]:
    event = {
        "event_type": event_type,
        "timestamp": timestamp,
        "source": source,
    }
    event.update({key: value for key, value in attrs.items() if value is not None})
    return event


def append_timing_event(
    events: list[dict[str, Any]],
    event_type: str,
    timestamp: str | None,
    source: str,
    **attrs: Any,
) -> None:
    events.append(make_timing_event(event_type, timestamp, source, **attrs))


def input_event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    user_chars = 0
    tool_chars = 0
    user_count = 0
    tool_count = 0
    first_input_event_type: str | None = None

    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"user_message", "tool_result"}:
            continue
        if first_input_event_type is None and isinstance(event_type, str):
            first_input_event_type = event_type
        if event_type == "user_message":
            user_count += 1
            user_chars += event_int(event, "content_chars")
        elif event_type == "tool_result":
            tool_count += 1
            tool_chars += event_int(event, "result_chars")

    return {
        "current_input_event_count": user_count + tool_count,
        "current_user_message_count": user_count,
        "current_tool_result_count": tool_count,
        "current_user_message_chars": user_chars,
        "current_tool_result_chars": tool_chars,
        "current_input_chars": user_chars + tool_chars,
        "first_input_event_type": first_input_event_type,
    }


def apply_input_event_summary(round_obj: dict[str, Any]) -> None:
    events = round_obj.get("timing_events")
    if not isinstance(events, list):
        events = []
    round_obj.update(input_event_summary(events))


def round_key(round_obj: dict[str, Any]) -> str:
    provider = round_obj.get("provider") or "claude"
    return f"{provider}:{round_obj.get('session_id')}:{round_obj.get('round_id')}"


def load_existing_round_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if isinstance(row.get("trace_key"), str):
                keys.add(row["trace_key"])
            elif row.get("session_id") and row.get("round_id"):
                keys.add(round_key(row))
    return keys


def write_rounds_jsonl(
    path: Path,
    rounds: list[dict[str, Any]],
    *,
    append_dedup: bool,
    written_sink: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    # When ``written_sink`` is given, each row actually written (after the trace_key stamp + de-dup) is
    # appended to it, so a caller can reuse the exact deduped rows without re-parsing the file it just
    # wrote. The CLIs pass nothing (unaffected); ingest.prepare uses it to skip a ~3.7 s reload.
    existing_keys = load_existing_round_keys(path) if append_dedup else set()
    mode = "a" if append_dedup else "w"
    written = 0
    skipped_duplicates = 0
    with path.open(mode, encoding="utf-8") as out:
        for round_obj in rounds:
            key = round_key(round_obj)
            round_obj["trace_key"] = key
            if key in existing_keys:
                skipped_duplicates += 1
                continue
            existing_keys.add(key)
            out.write(json.dumps(round_obj, ensure_ascii=False, separators=(",", ":")) + "\n")
            written += 1
            if written_sink is not None:
                written_sink.append(round_obj)
    return {
        "candidate_rounds": len(rounds),
        "written_rounds": written,
        "skipped_duplicate_rounds": skipped_duplicates,
    }


def make_round(
    *,
    project: str,
    session_id: str,
    session_file: Path,
    msg_id: str,
    msg: dict[str, Any],
    timing_events: list[dict[str, Any]],
) -> dict[str, Any]:
    round_obj = {
        "provider": "claude",
        "project": project,
        "session_id": session_id,
        "session_file": str(session_file),
        "round_index": None,
        "round_id": msg_id,
        "model": msg.get("model"),
        "input_tokens_total": 0,
        "prefix_tokens": 0,
        "newly_append_tokens": 0,
        "claude_uncached_input_tokens": 0,
        "claude_cache_creation_input_tokens": 0,
        "claude_cache_read_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": None,
        "timing_events": list(timing_events),
        "tools": [],
    }
    apply_usage(round_obj, msg)
    apply_input_event_summary(round_obj)
    return round_obj


def add_tool_use(
    *,
    round_obj: dict[str, Any],
    block: dict[str, Any],
    assistant_uuid: str | None,
    timestamp: str | None,
) -> dict[str, Any] | None:
    tool_id = block.get("id")
    if not isinstance(tool_id, str):
        return None
    for existing in round_obj["tools"]:
        if existing.get("tool_call_id") == tool_id:
            return existing

    tool = {
        "tool_index": len(round_obj["tools"]),
        "tool_name": block.get("name"),
        "tool_call_id": tool_id,
        "_assistant_uuid": assistant_uuid,
        "emitted_at": timestamp,
        "input": block.get("input"),
        "input_chars": content_chars(block.get("input")),
        "result_chars": None,
        "tool_wall_latency_ms": None,
        "tool_internal_latency_ms": None,
        "is_error": None,
        "result_at": None,
    }
    round_obj["tools"].append(tool)
    return tool


def apply_tool_result(
    *,
    tool: dict[str, Any],
    block: dict[str, Any],
    record: dict[str, Any],
) -> None:
    result_at = record.get("timestamp")
    emitted = parse_ts(tool.get("emitted_at"))
    finished = parse_ts(result_at)
    wall_latency_ms = None
    if emitted is not None and finished is not None:
        wall_latency_ms = round((finished - emitted).total_seconds() * 1000)
    internal_latency_ms = duration_ms_from_mapping(record.get("toolUseResult"))

    tool["result_chars"] = content_chars(block.get("content"))
    tool["tool_wall_latency_ms"] = wall_latency_ms
    tool["tool_internal_latency_ms"] = internal_latency_ms
    tool["is_error"] = bool(block.get("is_error", False))
    tool["result_at"] = result_at


def _raw_text(value: Any) -> str:
    """Best-effort flatten of a content value (str / dict / list of blocks) to display text.

    Used ONLY by the opt-in ``raw_sink`` (local-only per-round originals); never touches the round
    dict, so normalized/sanitized output is byte-identical whether or not a sink is supplied.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for el in value:
            if isinstance(el, str):
                parts.append(el)
            elif isinstance(el, dict):
                if isinstance(el.get("text"), str):
                    parts.append(el["text"])
                elif el.get("type") == "image":
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(el, ensure_ascii=False))
            else:
                parts.append(str(el))
        return "\n".join(parts)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def extract_session(session_file: Path, project: str) -> list[dict[str, Any]]:
    return extract_session_with_key(session_file, project, session_file.stem)


def extract_session_with_key(
    session_file: Path,
    project: str,
    session_key: str,
    *,
    raw_sink: dict[str, Any] | None = None,
    title_sink: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Extract one session's rounds.

    ``raw_sink`` / ``title_sink`` are **opt-in, local-only** side channels (default ``None``):
    when given, per-round original text is collected into ``raw_sink`` keyed by ``trace_key`` and
    conversation titles into ``title_sink`` keyed by ``session_id``. Neither is ever written back
    into the round dict, so with both ``None`` (the CLI / contribute default) behavior — and the
    normalized/sanitized output — is unchanged. The raw map stays on the local machine and must
    NEVER enter a sanitized/contributed/uploadable artifact.
    """
    rounds: list[dict[str, Any]] = []
    by_msg_id: dict[str, dict[str, Any]] = {}
    tool_by_id: dict[str, dict[str, Any]] = {}
    pending_input_events: list[dict[str, Any]] = []
    session_id = f"claude:{session_key}"
    # Opt-in raw capture state (parallel to the round-building state above; never merged into it).
    raw_by_msg_id: dict[str, dict[str, Any]] = {}
    raw_tool_by_id: dict[str, dict[str, Any]] = {}
    pending_raw_in: list[dict[str, str]] = []

    with session_file.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            msg = record.get("message")
            if not isinstance(msg, dict):
                # Title records carry no `message`. They're local-only (titles are user text), so
                # capture them here before skipping — keyed by this session's id so they line up
                # with the rounds. custom-title (explicit) wins over agent-name (fallback).
                if title_sink is not None:
                    rtype = record.get("type")
                    if rtype == "custom-title":
                        title = record.get("customTitle")
                        if isinstance(title, str) and title.strip():
                            title_sink[session_id] = title.strip()
                    elif rtype == "agent-name":
                        name = record.get("agentName")
                        if isinstance(name, str) and name.strip():
                            title_sink.setdefault(session_id, name.strip())
                continue

            timestamp = record.get("timestamp")
            record_type = record.get("type")
            role = msg.get("role")

            if record_type == "assistant" and role == "assistant":
                msg_id = msg.get("id")
                if not isinstance(msg_id, str) or msg_id == "<synthetic>":
                    continue
                if msg.get("model") == "<synthetic>":
                    continue

                round_obj = by_msg_id.get(msg_id)
                if round_obj is None:
                    round_obj = make_round(
                        project=project,
                        session_id=session_id,
                        session_file=session_file,
                        msg_id=msg_id,
                        msg=msg,
                        timing_events=pending_input_events,
                    )
                    pending_input_events = []
                    by_msg_id[msg_id] = round_obj
                    rounds.append(round_obj)
                    if raw_sink is not None:
                        # This round consumes whatever input text accrued since the last round.
                        kinds = {item["kind"] for item in pending_raw_in}
                        raw_by_msg_id[msg_id] = {
                            "input": "\n".join(
                                item["text"] for item in pending_raw_in if item["text"]
                            ),
                            "inputKind": "user" if "user" in kinds else "tool",
                            "_output": [],
                            "_tools": [],
                        }
                        pending_raw_in = []
                else:
                    if round_obj.get("model") is None:
                        round_obj["model"] = msg.get("model")
                    apply_usage(round_obj, msg)

                assistant_uuid = record.get("uuid")

                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if block_type == "thinking":
                        append_timing_event(
                            round_obj["timing_events"],
                            "reasoning",
                            timestamp,
                            "assistant.content.thinking",
                            content_chars=content_chars(block.get("thinking")),
                        )
                    elif block_type == "text":
                        append_timing_event(
                            round_obj["timing_events"],
                            "text",
                            timestamp,
                            "assistant.content.text",
                            content_chars=content_chars(block.get("text")),
                        )
                        if raw_sink is not None and msg_id in raw_by_msg_id:
                            text = block.get("text")
                            if isinstance(text, str) and text:
                                raw_by_msg_id[msg_id]["_output"].append(text)
                    elif block_type == "tool_use":
                        tool_count = len(round_obj["tools"])
                        tool = add_tool_use(
                            round_obj=round_obj,
                            block=block,
                            assistant_uuid=(
                                assistant_uuid if isinstance(assistant_uuid, str) else None
                            ),
                            timestamp=timestamp,
                        )
                        if tool is not None:
                            tool_by_id[tool["tool_call_id"]] = tool
                            if len(round_obj["tools"]) > tool_count:
                                append_timing_event(
                                    round_obj["timing_events"],
                                    "tool_call",
                                    timestamp,
                                    "assistant.content.tool_use",
                                    tool_call_id=tool["tool_call_id"],
                                    tool_name=tool.get("tool_name"),
                                    tool_index=tool.get("tool_index"),
                                )
                                if raw_sink is not None and msg_id in raw_by_msg_id:
                                    raw_tool = {
                                        "name": tool.get("tool_name"),
                                        "input": _raw_text(block.get("input")),
                                        "result": "",
                                        "error": False,
                                    }
                                    raw_by_msg_id[msg_id]["_tools"].append(raw_tool)
                                    raw_tool_by_id[tool["tool_call_id"]] = raw_tool

            elif role == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    append_timing_event(
                        pending_input_events,
                        "user_message",
                        timestamp,
                        "user.message",
                        content_chars=content_chars(content),
                    )
                    if raw_sink is not None:
                        pending_raw_in.append({"kind": "user", "text": content})
                    continue
                if not isinstance(content, list):
                    continue
                saw_tool_result = False
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    saw_tool_result = True
                    tool_id = block.get("tool_use_id")
                    if not isinstance(tool_id, str):
                        continue
                    append_timing_event(
                        pending_input_events,
                        "tool_result",
                        timestamp,
                        "user.content.tool_result",
                        tool_call_id=tool_id,
                        result_chars=content_chars(block.get("content")),
                        is_error=bool(block.get("is_error", False)),
                    )
                    if raw_sink is not None:
                        result_text = _raw_text(block.get("content"))
                        pending_raw_in.append({"kind": "tool", "text": result_text})
                        # The result also belongs to the tool call (in an earlier round) that
                        # emitted it; back-fill that round's raw tool entry.
                        raw_tool = raw_tool_by_id.get(tool_id)
                        if raw_tool is not None:
                            raw_tool["result"] = result_text
                            raw_tool["error"] = bool(block.get("is_error", False))
                    tool = tool_by_id.get(tool_id)
                    if tool is None:
                        continue
                    apply_tool_result(tool=tool, block=block, record=record)
                if not saw_tool_result:
                    append_timing_event(
                        pending_input_events,
                        "user_message",
                        timestamp,
                        "user.message",
                        content_chars=content_chars(content),
                    )
                    if raw_sink is not None:
                        pending_raw_in.append({"kind": "user", "text": _raw_text(content)})

    for index, round_obj in enumerate(rounds):
        round_obj["round_index"] = index
        apply_input_event_summary(round_obj)
        round_obj.pop("_usage_score", None)
        for tool_index, tool in enumerate(round_obj["tools"]):
            tool["tool_index"] = tool_index
            tool.pop("_assistant_uuid", None)
        if raw_sink is not None:
            entry = raw_by_msg_id.get(round_obj["round_id"])
            if entry is not None:
                # Key by the same trace_key write_rounds_jsonl will stamp; keep the first occurrence
                # (matching its de-dup) so the raw map lines up with the written rounds.
                raw_sink.setdefault(
                    round_key(round_obj),
                    {
                        "input": entry["input"],
                        "inputKind": entry["inputKind"],
                        "output": "\n".join(t for t in entry["_output"] if t),
                        "tools": entry["_tools"],
                    },
                )
    return rounds


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_dir", type=Path)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Output JSONL path. Each line is one deduped LLM round.",
    )
    parser.add_argument(
        "--append-dedup",
        action="store_true",
        help="Append only rounds whose stable trace_key is not already present in the output file.",
    )
    args = parser.parse_args()

    project_dir = args.project_dir
    session_files = sorted(project_dir.rglob("*.jsonl"))
    project = project_dir.name

    all_rounds: list[dict[str, Any]] = []
    for session_file in session_files:
        session_key = str(session_file.relative_to(project_dir).with_suffix(""))
        all_rounds.extend(extract_session_with_key(session_file, project, session_key))

    write_stats = write_rounds_jsonl(args.output, all_rounds, append_dedup=args.append_dedup)

    sessions = len(session_files)
    tool_calls = sum(len(r["tools"]) for r in all_rounds)
    tool_results = sum(1 for r in all_rounds for t in r["tools"] if t.get("result_at") is not None)
    print(f"sessions={sessions}")
    print(f"rounds={len(all_rounds)}")
    print(f"tool_calls={tool_calls}")
    print(f"tool_results={tool_results}")
    print(f"written_rounds={write_stats['written_rounds']}")
    print(f"skipped_duplicate_rounds={write_stats['skipped_duplicate_rounds']}")
    print(f"output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
