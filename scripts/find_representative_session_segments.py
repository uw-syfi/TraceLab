#!/usr/bin/env python3
"""Find compact Claude Code and Codex session windows that illustrate raw formats.

This is intentionally a finder/export helper, not a normalized trace extractor.
It copies raw JSONL lines and optionally expands each line into pretty JSON for
human inspection.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Window:
    provider: str
    source: Path
    start_line: int
    end_line: int
    record_count: int
    byte_chars: int
    counts: dict[str, int]


@dataclass(frozen=True)
class RecordFlags:
    line_no: int
    chars: int
    counts: Counter[str]


COUNT_KEYS = [
    "direct_user_messages",
    "assistant_records",
    "tool_calls",
    "tool_results",
    "usage_records",
    "token_count_records",
    "reasoning_records",
    "assistant_event_messages",
    "user_message_events",
    "turn_context_records",
    "task_started_events",
    "task_complete_events",
    "file_history_snapshots",
    "assistant_text_blocks",
    "thinking_blocks",
]


def read_jsonl(path: Path) -> Iterable[tuple[int, str, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            raw = line.rstrip("\n")
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield line_no, raw, obj


def claude_counts(obj: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    typ = obj.get("type")
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")

    if typ == "user" and isinstance(content, str):
        counts["direct_user_messages"] += 1

    if typ == "user" and isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                counts["tool_results"] += 1

    if typ == "assistant" and msg.get("role") == "assistant" and msg.get("model") != "<synthetic>":
        counts["assistant_records"] += 1
        if msg.get("usage"):
            counts["usage_records"] += 1
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    counts["tool_calls"] += 1
                elif block.get("type") == "text":
                    counts["assistant_text_blocks"] += 1
                elif block.get("type") == "thinking":
                    counts["thinking_blocks"] += 1

    if typ == "file-history-snapshot":
        counts["file_history_snapshots"] += 1
    return counts


def codex_user_message_is_bootstrap(payload: dict[str, Any]) -> bool:
    parts: list[str] = []
    for block in payload.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    text = "\n".join(parts)
    return text.startswith("# AGENTS.md") or text.startswith("<environment_context>")


def codex_counts(obj: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    typ = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = payload.get("type")

    if (
        typ == "response_item"
        and payload_type == "message"
        and payload.get("role") == "user"
        and not codex_user_message_is_bootstrap(payload)
    ):
        counts["direct_user_messages"] += 1

    if typ == "event_msg" and payload_type == "user_message":
        counts["user_message_events"] += 1

    if typ == "response_item" and payload_type == "message" and payload.get("role") == "assistant":
        counts["assistant_records"] += 1

    if typ == "event_msg" and payload_type == "agent_message":
        counts["assistant_event_messages"] += 1

    if typ == "response_item" and payload_type in {"function_call", "custom_tool_call"}:
        counts["tool_calls"] += 1

    if typ == "response_item" and payload_type in {
        "function_call_output",
        "custom_tool_call_output",
    }:
        counts["tool_results"] += 1

    if (
        typ == "event_msg"
        and payload_type == "token_count"
        and isinstance(payload.get("info"), dict)
    ):
        counts["token_count_records"] += 1

    if typ == "response_item" and payload_type == "reasoning":
        counts["reasoning_records"] += 1

    if typ == "turn_context":
        counts["turn_context_records"] += 1

    if typ == "event_msg" and payload_type == "task_started":
        counts["task_started_events"] += 1

    if typ == "event_msg" and payload_type == "task_complete":
        counts["task_complete_events"] += 1

    return counts


def build_flags(provider: str, path: Path) -> list[RecordFlags]:
    flags: list[RecordFlags] = []
    counter_fn = claude_counts if provider == "claude" else codex_counts
    for line_no, raw, obj in read_jsonl(path):
        flags.append(RecordFlags(line_no=line_no, chars=len(raw), counts=counter_fn(obj)))
    return flags


def prefix_sums(flags: list[RecordFlags]) -> tuple[list[int], dict[str, list[int]]]:
    char_prefix = [0]
    count_prefix = {key: [0] for key in COUNT_KEYS}
    for item in flags:
        char_prefix.append(char_prefix[-1] + item.chars)
        for key in COUNT_KEYS:
            count_prefix[key].append(count_prefix[key][-1] + item.counts.get(key, 0))
    return char_prefix, count_prefix


def count_range(prefix: dict[str, list[int]], start: int, end: int) -> dict[str, int]:
    return {key: values[end] - values[start] for key, values in prefix.items()}


def window_matches(provider: str, counts: dict[str, int], args: argparse.Namespace) -> bool:
    if counts["direct_user_messages"] < args.min_user:
        return False
    if counts["tool_calls"] < args.min_tool_calls:
        return False
    if counts["tool_results"] < args.min_tool_results:
        return False
    if provider == "claude":
        return (
            counts["assistant_records"] >= args.min_assistant
            and counts["usage_records"] >= args.min_usage
        )
    return (
        counts["token_count_records"] >= args.min_token_count
        and (counts["assistant_records"] + counts["assistant_event_messages"]) >= args.min_assistant
    )


def find_windows_for_file(provider: str, path: Path, args: argparse.Namespace) -> list[Window]:
    flags = build_flags(provider, path)
    if not flags:
        return []
    char_prefix, count_prefix = prefix_sums(flags)
    windows: list[Window] = []

    for start in range(len(flags)):
        max_end = min(len(flags), start + args.max_records)
        min_end = min(len(flags), start + args.min_records)
        for end in range(min_end, max_end + 1):
            counts = count_range(count_prefix, start, end)
            if not window_matches(provider, counts, args):
                continue
            windows.append(
                Window(
                    provider=provider,
                    source=path,
                    start_line=flags[start].line_no,
                    end_line=flags[end - 1].line_no,
                    record_count=end - start,
                    byte_chars=char_prefix[end] - char_prefix[start],
                    counts={key: value for key, value in counts.items() if value},
                )
            )
            break
    return windows


def iter_claude_files(root: Path) -> list[Path]:
    projects_dir = root / "projects"
    if not projects_dir.exists():
        return []
    return sorted(projects_dir.rglob("*.jsonl"), key=lambda p: str(p))


def iter_codex_files(root: Path) -> list[Path]:
    sessions_dir = root / "sessions"
    if not sessions_dir.exists():
        return []
    return sorted(sessions_dir.rglob("*.jsonl"), key=lambda p: (p.stat().st_size, str(p)))


def within_size_limit(path: Path, max_file_size_mb: float | None) -> bool:
    if max_file_size_mb is None:
        return True
    return path.stat().st_size <= max_file_size_mb * 1024 * 1024


def find_windows(args: argparse.Namespace) -> list[Window]:
    all_windows: list[Window] = []
    providers = set(args.provider)

    if "claude" in providers:
        candidates: list[Window] = []
        for path in iter_claude_files(args.claude_root):
            if not within_size_limit(path, args.max_file_size_mb):
                continue
            candidates.extend(find_windows_for_file("claude", path, args))
        candidates.sort(
            key=lambda w: (w.byte_chars, w.record_count, str(w.source), w.start_line)
        )
        all_windows.extend(candidates[: args.limit])

    if "codex" in providers:
        candidates = []
        for path in iter_codex_files(args.codex_root):
            if not within_size_limit(path, args.max_file_size_mb):
                continue
            candidates.extend(find_windows_for_file("codex", path, args))
        candidates.sort(
            key=lambda w: (w.byte_chars, w.record_count, str(w.source), w.start_line)
        )
        all_windows.extend(candidates[: args.limit])

    all_windows.sort(
        key=lambda w: (
            w.provider,
            w.byte_chars,
            w.record_count,
            str(w.source),
            w.start_line,
        )
    )
    return all_windows


def window_rows(window: Window) -> list[tuple[int, str, dict[str, Any]]]:
    rows = []
    for line_no, raw, obj in read_jsonl(window.source):
        if line_no < window.start_line:
            continue
        if line_no > window.end_line:
            break
        rows.append((line_no, raw, obj))
    return rows


def export_windows(windows: list[Window], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"output_root": str(output_dir), "segments": []}

    for index, window in enumerate(windows, start=1):
        name = (
            f"{index:02d}_{window.provider}_{window.source.stem}_"
            f"{window.start_line}_{window.end_line}"
        )
        segment_dir = output_dir / name
        records_dir = segment_dir / "records"
        records_dir.mkdir(parents=True, exist_ok=True)

        raw_path = segment_dir / "segment.raw.jsonl"
        expanded_path = segment_dir / "segment.expanded.json"
        rows = window_rows(window)

        with (
            raw_path.open("w", encoding="utf-8") as raw,
            expanded_path.open("w", encoding="utf-8") as expanded,
        ):
            expanded.write(f"# {window.provider} representative raw session segment\n")
            expanded.write(f"# source: {window.source}\n")
            expanded.write(f"# source lines: {window.start_line}-{window.end_line}\n")
            expanded.write(
                "# This file has human-readable separators; the whole file is not JSON.\n\n"
            )
            for row_index, (line_no, raw_line, obj) in enumerate(rows, start=1):
                raw.write(raw_line + "\n")
                pretty = json.dumps(obj, ensure_ascii=False, indent=2)
                record_path = records_dir / f"record_{row_index:04d}_line_{line_no:06d}.json"
                record_path.write_text(pretty + "\n", encoding="utf-8")
                expanded.write(f"===== record {row_index:04d} | source_line {line_no} =====\n")
                expanded.write(pretty + "\n\n")

        manifest["segments"].append(
            {
                "provider": window.provider,
                "source": str(window.source),
                "source_line_start": window.start_line,
                "source_line_end": window.end_line,
                "record_count": window.record_count,
                "byte_chars": window.byte_chars,
                "counts": window.counts,
                "raw_jsonl": str(raw_path),
                "expanded_text": str(expanded_path),
                "per_record_json_dir": str(records_dir),
            }
        )

    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=["claude", "codex"],
        nargs="+",
        default=["claude", "codex"],
        help="Providers to scan.",
    )
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude")
    parser.add_argument("--codex-root", type=Path, default=Path.home() / ".codex")
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of compact windows per provider.",
    )
    parser.add_argument("--min-user", type=int, default=2)
    parser.add_argument("--min-assistant", type=int, default=1)
    parser.add_argument("--min-tool-calls", type=int, default=3)
    parser.add_argument("--min-tool-results", type=int, default=3)
    parser.add_argument(
        "--min-usage",
        type=int,
        default=3,
        help="Claude usage-bearing assistant records.",
    )
    parser.add_argument("--min-token-count", type=int, default=3, help="Codex token_count records.")
    parser.add_argument("--min-records", type=int, default=8)
    parser.add_argument("--max-records", type=int, default=120)
    parser.add_argument(
        "--max-file-size-mb",
        type=float,
        default=10.0,
        help="Skip larger raw session files by default to keep scans quick. Use 0 to disable.",
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        help="Optional directory for raw JSONL, expanded text, per-record JSON, and manifest.",
    )
    args = parser.parse_args()
    if args.max_file_size_mb == 0:
        args.max_file_size_mb = None
    return args


def main() -> int:
    args = parse_args()
    windows = find_windows(args)
    for idx, window in enumerate(windows, start=1):
        print(
            f"{idx:02d} {window.provider:6s} chars={window.byte_chars:7d} "
            f"records={window.record_count:3d} "
            f"{window.source}:{window.start_line}-{window.end_line} "
            f"counts={json.dumps(window.counts, sort_keys=True)}"
        )
    if args.export_dir:
        export_windows(windows, args.export_dir)
        print(f"export_dir={args.export_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
