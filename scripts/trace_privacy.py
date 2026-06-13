#!/usr/bin/env python3
"""Shared privacy rules for normalized LLM round-trace rows.

Single source of truth for *which* keys are public-release-sensitive, used by two consumers:

- ``sanitize_round_trace.py`` — **removes** sensitive keys (and drops ``tools[].input``) while
  rewriting identifiers, producing a shareable trace.
- the contribute endpoint (``web/server``) — a **read-only gate** that *rejects* an upload if any
  sensitive key or ``tools[].input`` still survives. It never mutates; it reports the offending
  paths so the rejection is explainable.

Keep the two in lock-step by importing the constants/predicate here rather than re-declaring them.
``USER_KEYS`` are intentionally **not** rejected by the gate: the sanitizer pseudonymizes (not
drops) them, so a sanitized row legitimately still carries a pseudonymous ``user`` value.
"""

from __future__ import annotations

from typing import Any

USER_KEYS = {
    "user",
    "user_name",
    "username",
}

SENSITIVE_KEYS = {
    "cwd",
    "home",
    "host",
    "hostname",
    "file_path",
    "filepath",
    "repo_url",
    "repository_url",
    "session_file",
    "workdir",
}


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return (
        normalized in SENSITIVE_KEYS
        or normalized.endswith("_path")
        or normalized.endswith("_filepath")
        or normalized.endswith("filepath")
    )


def _scan(value: Any, path: str, out: list[str]) -> None:
    """Walk dict/list/scalar, appending the breadcrumb path of every sensitive *key* found.

    Mirrors the descent in ``sanitize_round_trace.sanitize_value`` but is read-only and records
    paths instead of filtering. A sensitive subtree is recorded once (we don't descend into it).
    """
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and is_sensitive_key(key):
                out.append(child_path)
                continue
            _scan(child, child_path, out)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan(item, f"{path}[{index}]", out)


def _has_sensitive_key(value: Any) -> bool:
    """Fast boolean twin of :func:`_scan` — no path strings. Used on the validation hot path."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and is_sensitive_key(key):
                return True
            if _has_sensitive_key(child):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_sensitive_key(item):
                return True
    return False


def row_has_leak(row: Any) -> bool:
    """Cheap pre-check: does the row contain any sensitive key or ``tools[].input``?

    Avoids the breadcrumb-string allocation of :func:`find_sensitive` — the common (clean) case is
    a pure structural walk. Only call :func:`find_sensitive` to build the offending paths once this
    returns ``True``.
    """
    if _has_sensitive_key(row):
        return True
    if isinstance(row, dict):
        tools = row.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and "input" in tool:
                    return True
    return False


def find_sensitive(row: Any) -> list[str]:
    """Return the breadcrumb paths of anything a sanitized row must not contain.

    Two classes of violation:
    - any key matching :func:`is_sensitive_key`, anywhere in the structure (e.g. ``"cwd"``,
      ``"tools[0].file_path"``);
    - a surviving ``tools[i].input`` — ``input`` is dropped by name (not by key-pattern), so it is
      checked explicitly rather than via :func:`is_sensitive_key`.

    An empty list means the row is clean.
    """
    out: list[str] = []
    _scan(row, "", out)
    if isinstance(row, dict):
        tools = row.get("tools")
        if isinstance(tools, list):
            for index, tool in enumerate(tools):
                if isinstance(tool, dict) and "input" in tool:
                    out.append(f"tools[{index}].input")
    return out
