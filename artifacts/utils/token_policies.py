"""Shared provider/model policy for prior-round output subtraction."""

from __future__ import annotations

from typing import Any


def should_subtract_previous_output(previous: dict[str, Any], policy: str) -> bool:
    if policy == "all":
        return True
    if policy == "claude-and-gpt55":
        if previous["provider"] == "claude":
            return True
        return previous["provider"] == "codex" and previous.get("model") == "gpt-5.5"
    raise ValueError(f"Unsupported subtract policy: {policy}")
