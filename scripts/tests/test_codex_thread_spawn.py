from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from extract_codex_rounds import extract_codex_session  # noqa: E402


SESSION = "019a0000-0000-7000-8000-000000000001"
PARENT = "019a0000-0000-7000-8000-000000000000"
# Older than SESSION, so in a subagent file this turn is a replay of the parent's history
# and its round is credited to the parent; in a normal file it stays with SESSION.
TURN = "019a0000-0000-6000-8000-00000000000a"


def _rec(ts: str, top: str, payload: dict) -> dict:
    return {"timestamp": f"2026-09-01T10:00:{ts}Z", "type": top, "payload": payload}


def _rollout(thread_spawn: object) -> list[dict]:
    """One complete round, in a session whose source.subagent.thread_spawn is the given value."""
    source = {"subagent": {"thread_spawn": thread_spawn}}
    usage = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50, "reasoning_output_tokens": 10}
    return [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": "0.150.0", "source": source, "cwd": "/repo"}),
        _rec("00.100", "turn_context", {"turn_id": TURN, "model": "gpt-5.6-sol"}),
        _rec("01.000", "event_msg", {"type": "user_message", "message": "go"}),
        _rec("02.000", "response_item", {"type": "message", "role": "assistant",
                                          "content": [{"type": "output_text", "text": "done"}]}),
        _rec("02.100", "event_msg", {"type": "token_count",
                                      "info": {"last_token_usage": usage,
                                               "total_token_usage": {**usage, "total_tokens": 5000}}}),
    ]


def _write(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / f"rollout-2026-09-01T10-00-00-{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.mark.parametrize("bad", [None, "not-an-object", ["a", "list"], 42])
def test_non_object_thread_spawn_is_treated_as_absent(tmp_path: Path, bad: object) -> None:
    rounds = extract_codex_session(_write(tmp_path, _rollout(bad)))
    assert len(rounds) == 1                                   # no AttributeError, and the round is kept
    assert rounds[0]["session_id"] == f"codex:{SESSION}"      # not treated as a subagent of anything


def test_object_thread_spawn_still_marks_a_subagent(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, _rollout({"parent_thread_id": PARENT})))
    assert len(rounds) == 1
    assert rounds[0]["session_id"] == f"codex:{PARENT}"       # replayed turn is credited to the parent