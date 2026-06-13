#!/usr/bin/env python3
"""Sanitize normalized LLM round-trace JSONL rows for public sharing."""

from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import sys
from pathlib import Path
from typing import Any, TextIO

# Privacy rules (which keys are sensitive) live in trace_privacy so the sanitizer and the
# contribute gate share one definition. The script dir is on sys.path when run directly; add it
# explicitly so the import also works when imported as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_privacy import SENSITIVE_KEYS, USER_KEYS, is_sensitive_key  # noqa: E402,F401


DEFAULT_SEED = "coding-trace-sanitize-round-trace-v1"
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PREFIX_HEX_RE = re.compile(r"^(msg_|toolu_)([0-9a-fA-F]+)$")
PREFIX_BASE62_RE = re.compile(r"^(call_)([A-Za-z0-9]+)$")


class StableIdSanitizer:
    def __init__(self, seed: str):
        self.random = random.Random(seed)
        self.maps: dict[str, dict[str, str]] = {}
        self.used: dict[str, set[str]] = {}

    def rand_hex(self, length: int) -> str:
        return "".join(self.random.choice("0123456789abcdef") for _ in range(length))

    def rand_base62(self, length: int) -> str:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        return "".join(self.random.choice(alphabet) for _ in range(length))

    def rand_uuid_like(self) -> str:
        return "-".join(
            [
                self.rand_hex(8),
                self.rand_hex(4),
                self.rand_hex(4),
                self.rand_hex(4),
                self.rand_hex(12),
            ]
        )

    def unique(self, kind: str, make_value) -> str:
        used = self.used.setdefault(kind, set())
        while True:
            value = make_value()
            if value not in used:
                used.add(value)
                return value

    def map_value(self, kind: str, original: Any, make_value) -> Any:
        if not isinstance(original, str):
            return original
        mapping = self.maps.setdefault(kind, {})
        if original not in mapping:
            mapping[original] = self.unique(kind, make_value)
        return mapping[original]

    def atomic_id(self, kind: str, original: Any, *, fallback_prefix: str = "id_") -> Any:
        if not isinstance(original, str):
            return original

        def make_value() -> str:
            match = PREFIX_HEX_RE.match(original)
            if match:
                return f"{match.group(1)}{self.rand_hex(len(match.group(2)))}"
            match = PREFIX_BASE62_RE.match(original)
            if match:
                return f"{match.group(1)}{self.rand_base62(len(match.group(2)))}"
            if UUID_RE.match(original):
                return self.rand_uuid_like()
            if HEX_RE.match(original) and len(original) >= 12:
                return self.rand_hex(len(original))
            return f"{fallback_prefix}{self.rand_hex(16)}"

        return self.map_value(kind, original, make_value)

    def session_id(self, provider: str, original: Any) -> Any:
        if not isinstance(original, str):
            return original

        def make_value() -> str:
            return f"{provider}:{self.rand_uuid_like()}"

        return self.map_value("session_id", original, make_value)

    def round_id(self, provider: str, original: Any) -> Any:
        if not isinstance(original, str):
            return original
        if provider == "codex" and ":" in original:
            base, suffix = original.rsplit(":", 1)
            if suffix.isdigit():
                return f"{self.atomic_id('turn_id', base, fallback_prefix='turn_')}:{suffix}"
        return self.atomic_id("round_id", original, fallback_prefix="round_")

    def project(self, original: Any) -> Any:
        return self.map_value("project", original, lambda: f"project_{self.rand_hex(8)}")

    def user(self, original: Any) -> Any:
        return self.map_value("user", original, lambda: f"user_{self.rand_hex(8)}")


def sanitize_value(value: Any, ids: StableIdSanitizer) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, child in value.items():
            if isinstance(key, str):
                normalized = key.lower().replace("-", "_")
                if normalized in USER_KEYS:
                    cleaned[key] = (
                        ids.user(child) if isinstance(child, str) else sanitize_value(child, ids)
                    )
                    continue
                if is_sensitive_key(key):
                    continue
            cleaned[key] = sanitize_value(child, ids)
        return cleaned
    if isinstance(value, list):
        return [sanitize_value(item, ids) for item in value]
    return value


def sanitize_row(row: dict[str, Any], ids: StableIdSanitizer) -> dict[str, Any]:
    original = row
    cleaned = sanitize_value(row, ids)
    if not isinstance(cleaned, dict):
        raise TypeError("sanitized row is not an object")

    provider = str(original.get("provider") or cleaned.get("provider") or "unknown")

    if "project" in original:
        cleaned["project"] = ids.project(original.get("project"))
    if "session_id" in original:
        cleaned["session_id"] = ids.session_id(provider, original.get("session_id"))
    if "turn_id" in original:
        cleaned["turn_id"] = ids.atomic_id(
            "turn_id",
            original.get("turn_id"),
            fallback_prefix="turn_",
        )
    if "round_id" in original:
        cleaned["round_id"] = ids.round_id(provider, original.get("round_id"))

    tools = cleaned.get("tools")
    original_tools = original.get("tools")
    if isinstance(tools, list) and isinstance(original_tools, list):
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                continue
            original_tool = original_tools[index] if index < len(original_tools) else {}
            if isinstance(original_tool, dict) and "tool_call_id" in original_tool:
                tool["tool_call_id"] = ids.atomic_id(
                    "tool_call_id",
                    original_tool.get("tool_call_id"),
                    fallback_prefix="call_",
                )
            tool.pop("input", None)
            if isinstance(original_tool, dict) and "_assistant_uuid" in original_tool:
                tool["_assistant_uuid"] = ids.atomic_id(
                    "assistant_uuid",
                    original_tool.get("_assistant_uuid"),
                    fallback_prefix="assistant_",
                )

    timing_events = cleaned.get("timing_events")
    original_timing_events = original.get("timing_events")
    if isinstance(timing_events, list) and isinstance(original_timing_events, list):
        for index, event in enumerate(timing_events):
            if not isinstance(event, dict):
                continue
            original_event = (
                original_timing_events[index] if index < len(original_timing_events) else {}
            )
            if isinstance(original_event, dict) and "tool_call_id" in original_event:
                event["tool_call_id"] = ids.atomic_id(
                    "tool_call_id",
                    original_event.get("tool_call_id"),
                    fallback_prefix="call_",
                )

    if "trace_key" in cleaned:
        session_id = cleaned.get("session_id")
        round_id = cleaned.get("round_id")
        if session_id is not None and round_id is not None:
            cleaned["trace_key"] = f"{provider}:{session_id}:{round_id}"
        else:
            cleaned["trace_key"] = ids.atomic_id(
                "trace_key",
                original.get("trace_key"),
                fallback_prefix="trace_",
            )

    return cleaned


def open_output(path: str | None) -> tuple[TextIO, bool]:
    if path is None or path == "-":
        return sys.stdout, False
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return output_path.open("w", encoding="utf-8"), True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Normalized round-trace JSONL input.")
    parser.add_argument(
        "-o",
        "--output",
        default="-",
        help="Sanitized JSONL output path. Defaults to stdout.",
    )
    parser.add_argument(
        "--seed",
        default=DEFAULT_SEED,
        help="Seed for stable pseudorandom id generation.",
    )
    parser.add_argument(
        "--random-seed",
        action="store_true",
        help=(
            "Use a fresh random seed for this run. Relationships are still "
            "preserved within the output."
        ),
    )
    args = parser.parse_args()

    seed = secrets.token_hex(16) if args.random_seed else args.seed
    ids = StableIdSanitizer(seed)
    rows = 0
    tools = 0

    out, should_close = open_output(args.output)
    try:
        with args.input.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SystemExit(f"{args.input}:{line_no}: invalid JSONL row: {exc}") from exc
                if not isinstance(row, dict):
                    raise SystemExit(f"{args.input}:{line_no}: expected JSON object row")
                sanitized = sanitize_row(row, ids)
                rows += 1
                row_tools = sanitized.get("tools")
                if isinstance(row_tools, list):
                    tools += len(row_tools)
                out.write(
                    json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
    finally:
        if should_close:
            out.close()

    print(
        "sanitized "
        f"rows={rows} tools={tools} output={args.output} "
        f"seed={'<random>' if args.random_seed else seed}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
