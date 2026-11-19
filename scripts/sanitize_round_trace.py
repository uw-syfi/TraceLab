#!/usr/bin/env python3
"""Sanitize normalized LLM round-trace JSONL rows for public sharing."""

from __future__ import annotations

import argparse
import json
import random
import re
import secrets
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

# Privacy rules (which keys are sensitive) live in trace_privacy so the sanitizer and the
# contribute gate share one definition. The script dir is on sys.path when run directly; add it
# explicitly so the import also works when imported as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trace_privacy import (  # noqa: E402,F401
    SENSITIVE_KEYS, USER_KEYS, LineageMarkerValidator, is_sensitive_key, row_has_leak,
)
from executable_facts import COMMAND_TOOL_NAMES, extract_executable_facts  # noqa: E402


DEFAULT_SEED = "coding-trace-sanitize-round-trace-v1"
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PREFIX_HEX_RE = re.compile(r"^(msg_|toolu_)([0-9a-fA-F]+)$")
PREFIX_BASE62_RE = re.compile(r"^(call_)([A-Za-z0-9]+)$")
PUBLIC_EXECUTABLE_RE = re.compile(r"^custom_[1-9][0-9]*$")
EXECUTABLE_WHITELIST = Path(__file__).resolve().with_name("public_common_executables.txt")
PUBLIC_TOOL_NAME_RE = re.compile(r"^custom_tool_[1-9][0-9]*$")
TOOL_NAME_WHITELIST = Path(__file__).resolve().with_name("public_common_tool_names.txt")


def load_executable_whitelist(path: Path = EXECUTABLE_WHITELIST) -> frozenset[str]:
    """Load the frozen exact-match list of executable labels safe for public release."""
    names = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if not names:
        raise ValueError(f"executable whitelist is empty: {path}")
    return frozenset(names)


PUBLIC_EXECUTABLES = load_executable_whitelist()
PUBLIC_TOOL_NAMES = load_executable_whitelist(TOOL_NAME_WHITELIST)
SKELETON_RESERVED = frozenset(
    {
        "|", "&&", "||", ";", "&", "!",
        "<loops>", "<substitution>", "<assign>", "<conditional>",
        "<subshell>", "<unknown>", "<function>",
    }
)


class StableIdSanitizer:
    def __init__(self, seed: str, *, already_sanitized: bool = False):
        self.random = random.Random(seed)
        self.maps: dict[str, dict[str, str]] = {}
        self.used: dict[str, set[str]] = {}
        self.identity_scope_id = str(uuid4())
        self.input_markers = LineageMarkerValidator()
        self.input_is_private: bool | None = None
        # Caller attestation for an established sanitized batch, never inferred from row contents.
        self.already_sanitized = already_sanitized

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

    def executable(self, original: str) -> str:
        """Keep frozen public/common names; pseudonymize every other exact label."""
        if original in PUBLIC_EXECUTABLES or PUBLIC_EXECUTABLE_RE.fullmatch(original):
            return original
        mapping = self.maps.setdefault("executable", {})
        if original not in mapping:
            mapping[original] = f"custom_{len(mapping) + 1}"
        return mapping[original]

    def tool_name(self, original: str) -> str:
        """Keep standard tool names; pseudonymize every custom/integration-specific name."""
        if original in PUBLIC_TOOL_NAMES or PUBLIC_TOOL_NAME_RE.fullmatch(original):
            return original
        mapping = self.maps.setdefault("tool_name", {})
        if original not in mapping:
            mapping[original] = f"custom_tool_{len(mapping) + 1}"
        return mapping[original]


def sanitize_executable_facts(facts: dict[str, Any], ids: StableIdSanitizer) -> dict[str, Any]:
    """Sanitize executable labels consistently in the list and structural skeleton."""
    labels = facts.get("executables")
    safe_labels = (
        [ids.executable(label) for label in labels if isinstance(label, str)]
        if isinstance(labels, list)
        else []
    )

    skeleton = facts.get("command_skeleton")
    if isinstance(skeleton, str):
        safe_skeleton = " ".join(
            token if token in SKELETON_RESERVED else ids.executable(token)
            for token in skeleton.split()
        )
    else:
        safe_skeleton = ""

    status = facts.get("executable_parse_status")
    if status not in {"success", "partial", "failed"}:
        status = "failed"
    reason = facts.get("executable_parse_reason")
    return {
        "executables": safe_labels,
        "executable_parse_status": status,
        "executable_parse_reason": reason if isinstance(reason, str) else None,
        "command_skeleton": safe_skeleton,
    }


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


def validate_sanitization_batch(
    rows: Iterable[dict[str, Any]], *, already_sanitized: bool = False,
) -> bool:
    """Return whether private fields establish scope eligibility, NOT whether to sanitize."""
    markers = LineageMarkerValidator()
    private: set[bool] = set()
    for row in rows:
        markers.add(row)
        has_private_fields = row_has_leak(row)
        if already_sanitized and (has_private_fields or not all(key in row for key in (
            "provider", "session_id", "round_index", "input_tokens_total",
        ))):
            raise ValueError("already-sanitized input must contain normalized rounds without private fields")
        private.add(has_private_fields)
    if len(private) > 1:
        raise ValueError("cannot mix private and ambiguous/already-sanitized rounds")
    is_private = True in private
    if markers.scope is not None and is_private:
        raise ValueError("identity_scope_id is only valid on already-sanitized rounds")
    return is_private


def sanitize_row(row: dict[str, Any], ids: StableIdSanitizer) -> dict[str, Any]:
    is_private = validate_sanitization_batch([row], already_sanitized=ids.already_sanitized)
    ids.input_markers.add(row)
    if ids.input_is_private is not None and ids.input_is_private != is_private:
        raise ValueError("cannot mix private and ambiguous/already-sanitized rounds")
    ids.input_is_private = is_private
    if ids.already_sanitized:
        return dict(row)
    original = row
    cleaned = sanitize_value(row, ids)
    if not isinstance(cleaned, dict):
        raise TypeError("sanitized row is not an object")

    provider = str(original.get("provider") or cleaned.get("provider") or "unknown")
    cleaned["session_origin"] = original.get("session_origin", "unknown")
    if is_private:
        cleaned["identity_scope_id"] = ids.identity_scope_id
    else:
        # Ambiguous input still needs privacy transforms, but cannot certify a legacy namespace.
        cleaned.pop("identity_scope_id", None)

    if "project" in original:
        cleaned["project"] = ids.project(original.get("project"))
    if "session_id" in original:
        cleaned["session_id"] = ids.session_id(provider, original.get("session_id"))
    for field in ("parent_session_id", "root_session_id"):
        if field in original:
            value = original[field]
            cleaned[field] = ids.session_id(provider, value) if isinstance(value, str) and value else None
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
            original_tool_name = (
                original_tool.get("tool_name") if isinstance(original_tool, dict) else None
            )
            if isinstance(original_tool_name, str):
                tool["tool_name"] = ids.tool_name(original_tool_name)
            if isinstance(original_tool, dict) and original_tool.get("report_session_ids") is not None:
                tool["report_session_ids"] = [
                    ids.session_id(provider, sid) for sid in original_tool["report_session_ids"]
                ]
            if isinstance(original_tool, dict) and "tool_call_id" in original_tool:
                tool["tool_call_id"] = ids.atomic_id(
                    "tool_call_id",
                    original_tool.get("tool_call_id"),
                    fallback_prefix="call_",
                )
            if (
                isinstance(original_tool, dict)
                and original_tool.get("continuation_of_tool_call_id") is not None
            ):
                tool["continuation_of_tool_call_id"] = ids.atomic_id(
                    "tool_call_id",
                    original_tool.get("continuation_of_tool_call_id"),
                    fallback_prefix="call_",
                )
            if isinstance(original_tool, dict) and original_tool_name in COMMAND_TOOL_NAMES:
                raw_input = original_tool.get("input")
                if raw_input is not None:
                    try:
                        facts = extract_executable_facts(raw_input)
                    except RuntimeError:
                        # Some constrained runtimes (notably the in-browser Pyodide ingest) cannot
                        # load tree-sitter-bash. Keep the public schema complete and report the
                        # limitation honestly; native collection installs the parser dependency.
                        facts = {
                            "executables": [],
                            "executable_parse_status": "failed",
                            "executable_parse_reason": "parser-unavailable",
                            "command_skeleton": "",
                        }
                else:
                    facts = {
                        key: original_tool.get(key)
                        for key in (
                            "executables",
                            "executable_parse_status",
                            "executable_parse_reason",
                            "command_skeleton",
                        )
                    }
                    if facts["executable_parse_status"] is None:
                        facts.update(
                            {
                                "executables": [],
                                "executable_parse_status": "failed",
                                "executable_parse_reason": "missing-input",
                                "command_skeleton": "",
                            }
                        )
                tool.update(sanitize_executable_facts(facts, ids))
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
            if isinstance(original_event, dict) and "turn_id" in original_event:
                event["turn_id"] = ids.atomic_id("turn_id", original_event["turn_id"], fallback_prefix="turn_")
            if isinstance(original_event, dict) and "tool_call_id" in original_event:
                event["tool_call_id"] = ids.atomic_id(
                    "tool_call_id",
                    original_event.get("tool_call_id"),
                    fallback_prefix="call_",
                )
            original_tool_name = (
                original_event.get("tool_name") if isinstance(original_event, dict) else None
            )
            if isinstance(original_tool_name, str):
                event["tool_name"] = ids.tool_name(original_tool_name)

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


def read_rows(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL row: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object row")
            yield row


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
    parser.add_argument(
        "--already-sanitized",
        action="store_true",
        help="Pass through an established already-sanitized batch; caller attests identifiers are safe.",
    )
    args = parser.parse_args()

    seed = secrets.token_hex(16) if args.random_seed else args.seed
    ids = StableIdSanitizer(seed, already_sanitized=args.already_sanitized)
    rows = 0
    tools = 0
    executable_statuses: Counter[str] = Counter()

    validate_sanitization_batch(read_rows(args.input), already_sanitized=args.already_sanitized)
    # Stage on disk so large batches cannot exhaust memory or truncate a good output on failure.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as staged:
        for row in read_rows(args.input):
            sanitized = sanitize_row(row, ids)
            rows += 1
            row_tools = sanitized.get("tools")
            if isinstance(row_tools, list):
                tools += len(row_tools)
                executable_statuses.update(
                    str(tool["executable_parse_status"])
                    for tool in row_tools
                    if isinstance(tool, dict) and "executable_parse_status" in tool
                )
            staged.write(json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")) + "\n")
        staged.seek(0)
        out, should_close = open_output(args.output)
        try:
            shutil.copyfileobj(staged, out)
        finally:
            if should_close:
                out.close()

    print(
        "sanitized "
        f"rows={rows} tools={tools} output={args.output} "
        f"executable_parse_status={dict(executable_statuses)} "
        f"seed={'<random>' if args.random_seed else seed}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
