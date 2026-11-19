from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "artifacts" / "utils"))

from token_policies import should_subtract_previous_output  # noqa: E402


@pytest.mark.parametrize("provider,model,expected", [
    ("claude", "claude-sonnet", True),
    ("claude", None, True),
    ("codex", "gpt-5.4", False),
    ("codex", "gpt-5.5", True),
    ("codex", "gpt-5.5-mini", False),
    ("codex", "gpt-5.5-2026-01-01", False),
    ("codex", None, False),
    ("other", "gpt-5.5", False),
])
def test_default_policy_requires_exact_supported_provider_model(provider, model, expected):
    assert should_subtract_previous_output(
        {"provider": provider, "model": model}, "claude-and-gpt55"
    ) is expected


def test_all_policy_does_not_require_provider_or_model():
    assert should_subtract_previous_output({}, "all") is True


def test_invalid_policy_preserves_error():
    with pytest.raises(ValueError, match="^Unsupported subtract policy: unsupported$"):
        should_subtract_previous_output({}, "unsupported")
