from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from extract_codex_rounds import extract_codex_session  # noqa: E402
from merge_round_traces import round_exclusion_reason  # noqa: E402


SESSION = "019a0000-0000-7000-8000-000000000001"
TURN = "019a0000-0000-7000-8000-00000000000a"


def _rec(ts: str, top: str, payload: dict) -> dict:
    return {"timestamp": f"2026-09-01T10:00:{ts}Z", "type": top, "payload": payload}


def _meta(version: str = "0.150.0") -> list[dict]:
    return [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": version, "source": "cli", "cwd": "/repo"}),
        _rec("00.100", "turn_context", {"turn_id": TURN, "model": "gpt-5.6-sol"}),
    ]


def _usage(ts: str, n: int) -> dict:
    last = {"input_tokens": 1000 + n, "cached_input_tokens": 800, "output_tokens": 50, "reasoning_output_tokens": 10}
    total = {**last, "total_tokens": 5000 + n}
    return _rec(ts, "event_msg", {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total}})


def _prompt_new(ts: str, text: str) -> list[dict]:
    """How Codex CLI >= 0.147 writes a prompt: a role=user response item, then item_completed/UserMessage."""
    return [
        _rec(ts, "response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}),
        _rec(ts, "event_msg", {"type": "item_completed",
                                "item": {"type": "UserMessage", "id": "i1", "content": [{"type": "text", "text": text}]}}),
    ]


def _call(ts: str, call_id: str) -> dict:
    return _rec(ts, "response_item", {"type": "function_call", "name": "shell", "call_id": call_id,
                                       "arguments": json.dumps({"command": ["bash", "-lc", "true"]})})


def _output(ts: str, call_id: str) -> dict:
    return _rec(ts, "response_item", {"type": "function_call_output", "call_id": call_id, "output": "ok"})


def _answer(ts: str) -> dict:
    return _rec(ts, "response_item", {"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": "done"}]})


def _write(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / f"rollout-2026-09-01T10-00-00-{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _inputs(round_obj: dict) -> list[str]:
    return [e["event_type"] for e in round_obj["timing_events"] if e["event_type"] in ("user_message", "tool_result")]


# --------------------------------------------------------------------------- round boundaries


def _late_usage_rollout() -> list[dict]:
    """prompt → call c1 → output c1 → token_count (of the call that requested c1) → answer → token_count."""
    return [
        *_meta(),
        *_prompt_new("01.000", "run the tests"),
        _rec("02.000", "response_item", {"type": "reasoning"}),
        _call("03.000", "c1"),
        _output("08.000", "c1"),
        _usage("08.100", 1),
        _answer("10.000"),
        _usage("10.100", 2),
    ]


def test_tool_result_after_the_call_belongs_to_the_next_round(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, _late_usage_rollout()))
    assert _inputs(r1) == ["user_message"]           # the result of c1 is not an input of the call that asked for it
    assert [t["tool_call_id"] for t in r1["tools"]] == ["c1"]   # the call itself stays with the round that made it
    assert _inputs(r2) == ["tool_result"]
    assert r2["first_input_event_type"] == "tool_result"   # the final answer has a trigger (it was None)


def test_rounds_and_tokens_are_unchanged_by_the_boundary_fix(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, _late_usage_rollout()))
    assert (r1["input_tokens_total"], r2["input_tokens_total"]) == (1001, 1002)
    assert (r1["round_id"], r2["round_id"]) == (f"{TURN}:0", f"{TURN}:1")


def test_each_round_has_its_input_before_its_output(tmp_path: Path) -> None:
    for r in extract_codex_session(_write(tmp_path, _late_usage_rollout())):
        ins = [e["timestamp"] for e in r["timing_events"] if e["event_type"] in ("user_message", "tool_result")]
        outs = [e["timestamp"] for e in r["timing_events"] if e["event_type"] in ("reasoning", "text", "tool_call")]
        assert ins and outs and max(ins) <= min(outs)   # so a generation span can be measured


def test_token_count_before_the_tool_output_keeps_the_old_assignment(tmp_path: Path) -> None:
    # usage-first ordering (occurs per call in any version): the call's usage is written first; the result then waits for the next round as before
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta("0.140.0"),
        _rec("01.000", "event_msg", {"type": "user_message", "message": "go"}),
        _call("03.000", "c1"),
        _usage("03.100", 1),
        _output("08.000", "c1"),
        _answer("10.000"),
        _usage("10.100", 2),
    ]))
    assert _inputs(r1) == ["user_message"] and _inputs(r2) == ["tool_result"]


def test_parallel_tool_results_all_move_to_the_next_round(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "two things"),
        _call("03.000", "c1"),
        _call("03.100", "c2"),
        _output("06.000", "c1"),
        _output("07.000", "c2"),
        _usage("07.100", 1),
        _answer("09.000"),
        _usage("09.100", 2),
    ]))
    assert _inputs(r1) == ["user_message"]
    assert _inputs(r2) == ["tool_result", "tool_result"]
    assert len(r1["tools"]) == 2


def test_a_trailing_tool_result_is_input_to_no_round_but_the_tool_keeps_its_result(tmp_path: Path) -> None:
    (r1,) = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("03.000", "c1"),
        _output("08.000", "c1"),
        _usage("08.100", 1),
    ]))
    assert _inputs(r1) == ["user_message"]
    assert r1["tools"][0]["result_at"] is not None       # the tool record is complete


def test_raw_sink_follows_the_same_boundary(tmp_path: Path) -> None:
    sink: dict = {}
    _r1, _r2 = extract_codex_session(_write(tmp_path, _late_usage_rollout()), raw_sink=sink)
    entries = list(sink.values())
    assert entries[0]["inputKind"] == "user" and "ok" not in entries[0]["input"]
    assert entries[1]["inputKind"] == "tool" and entries[1]["input"] == "ok"


# --------------------------------------------------------------------------- turn boundaries and result paths

TURN_B = "019a0000-0000-7000-8000-00000000000b"


def _turn_ends_after_a_tool_result(end: str) -> list[dict]:
    """Turn A: prompt → call c1 → output c1 → late token_count, then the turn ends with no further call.
    Turn B: a new prompt → answer. ``end`` is how Codex closes turn A (task_complete or turn_aborted)."""
    return [
        *_meta(),
        _rec("00.200", "event_msg", {"type": "task_started", "turn_id": TURN}),
        *_prompt_new("01.000", "run the tests"),
        _call("03.000", "c1"),
        _output("08.000", "c1"),
        _usage("08.100", 1),
        _rec("08.200", "event_msg", {"type": end, "turn_id": TURN}),
        _rec("20.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("20.001", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        *_prompt_new("21.000", "now the docs"),
        _answer("23.000"),
        _usage("23.100", 2),
    ]


def test_a_result_left_at_the_end_of_a_turn_does_not_start_the_next_prompt_round(tmp_path: Path) -> None:
    for end in ("task_complete", "turn_aborted"):
        r1, r2 = extract_codex_session(_write(tmp_path, _turn_ends_after_a_tool_result(end)))
        assert _inputs(r1) == ["user_message"]
        assert r2["round_id"] == f"{TURN_B}:0"
        assert _inputs(r2) == ["user_message"], end       # the new request's first step is user-initiated
        assert r2["first_input_event_type"] == "user_message"
        assert r1["tools"][0]["result_at"] is not None     # the tool record keeps its result


def test_raw_sink_drops_the_left_result_at_the_turn_boundary(tmp_path: Path) -> None:
    sink: dict = {}
    extract_codex_session(_write(tmp_path, _turn_ends_after_a_tool_result("task_complete")), raw_sink=sink)
    second = list(sink.values())[1]
    assert second["inputKind"] == "user" and second["input"] == "now the docs"


def test_results_inside_one_turn_are_kept_across_repeated_turn_context(tmp_path: Path) -> None:
    # a turn_context that repeats the same turn id is not a boundary
    records = _late_usage_rollout()
    records.insert(-2, _rec("09.000", "turn_context", {"turn_id": TURN, "model": "gpt-5.6-sol"}))
    _r1, r2 = extract_codex_session(_write(tmp_path, records))
    assert _inputs(r2) == ["tool_result"]


def test_custom_tool_and_exec_results_move_to_the_next_round(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "edit and run"),
        _rec("03.000", "response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "p1",
                                          "input": "*** Begin Patch"}),
        _call("03.100", "c2"),
        _rec("05.000", "response_item", {"type": "custom_tool_call_output", "call_id": "p1", "output": "Done"}),
        _rec("06.000", "event_msg", {"type": "exec_command_end", "call_id": "c2", "exit_code": 0,
                                      "aggregated_output": "ok"}),
        _usage("06.100", 1),
        _answer("09.000"),
        _usage("09.100", 2),
    ]))
    assert _inputs(r1) == ["user_message"]
    assert _inputs(r2) == ["tool_result", "tool_result"]


# --------------------------------------------------------------------------- rollouts without turn ids, round identity


def _old_rollout() -> list[dict]:
    """A rollout from a CLI that records no turn ids: turn_context / task_started / task_complete without turn_id.
    Request 1 ends with a tool result after its call's token_count; request 2 follows minutes later."""
    return [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": "0.40.0", "source": "cli", "cwd": "/repo"}),
        _rec("00.100", "turn_context", {"model": "gpt-5-codex"}),
        _rec("00.200", "event_msg", {"type": "task_started"}),
        _rec("01.000", "event_msg", {"type": "user_message", "message": "go"}),
        _call("02.000", "c1"),
        _output("03.000", "c1"),
        _usage("03.100", 1),
        _rec("03.200", "event_msg", {"type": "task_complete"}),
        _rec("40.000", "turn_context", {"model": "gpt-5-codex"}),
        _rec("40.100", "event_msg", {"type": "task_started"}),
        _rec("40.200", "event_msg", {"type": "user_message", "message": "next"}),
        _answer("45.000"),
        _usage("45.100", 2),
    ]


def test_rollouts_without_turn_ids_are_extracted_as_before(tmp_path: Path) -> None:
    # without turn ids a new request cannot be told from a message sent mid-turn: keep the upstream assignment
    r1, r2 = extract_codex_session(_write(tmp_path, _old_rollout()))
    assert _inputs(r1) == ["user_message", "tool_result"]
    assert _inputs(r2) == ["user_message"] and r2["first_input_event_type"] == "user_message"


def test_a_call_without_recognized_output_after_a_moved_result_is_still_skipped(tmp_path: Path) -> None:
    # upstream skips a token_count with no new input, output or tool as accounting replay; a result that was
    # moved forward must not turn it into a round (round ids and tokens stay as before this change)
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _output("03.000", "c1"),
        _usage("03.100", 1),
        _rec("04.000", "response_item", {"type": "web_search_call", "status": "completed"}),
        _usage("04.100", 2),
        _answer("05.000"),
        _usage("05.100", 3),
    ]))
    assert (r1["input_tokens_total"], r2["input_tokens_total"]) == (1001, 1003)
    assert (r1["round_id"], r2["round_id"]) == (f"{TURN}:0", f"{TURN}:1")
    assert _inputs(r2) == ["tool_result"]


def _waiting_result_then_new_turn(new_turn: list[dict], first_call: list[dict]) -> list[dict]:
    """Usage-first call whose result waits; then a new turn whose first call (``first_call`` before its
    token_count) comes before the turn's prompt."""
    return [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _output("03.000", "c1"),
        *new_turn,
        *first_call,
        _usage("05.500", 2),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 3),
    ]


_TASK_TURN_B = [
    _rec("03.200", "event_msg", {"type": "task_complete", "turn_id": TURN}),
    _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
    _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
]


def test_a_call_with_neither_output_nor_prompt_after_a_turn_change_keeps_its_result(tmp_path: Path) -> None:
    # the new turn starts with a call that has no recognized output (e.g. a compaction) before its prompt: the
    # waiting result is all that triggered it, so it stays its input (as upstream), and the merge keeps the row
    sink: dict = {}
    rounds = extract_codex_session(_write(tmp_path, _waiting_result_then_new_turn(_TASK_TURN_B, [])), raw_sink=sink)
    assert [r["input_tokens_total"] for r in rounds] == [1001, 1002, 1003]
    assert [r["round_id"] for r in rounds] == [f"{TURN}:0", f"{TURN_B}:0", f"{TURN_B}:1"]
    assert _inputs(rounds[1]) == ["tool_result"] and round_exclusion_reason(rounds[1]) is None
    assert rounds[2]["first_input_event_type"] == "user_message" and _inputs(rounds[2]) == ["user_message"]
    raw = list(sink.values())[1]
    assert raw["inputKind"] == "tool" and raw["input"] == "ok"


def test_a_call_with_output_after_a_turn_change_does_not_get_the_result_back(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, _waiting_result_then_new_turn(_TASK_TURN_B, [_answer("05.400")])))
    assert _inputs(rounds[1]) == []


def test_a_prompt_round_without_output_does_not_get_the_result_back(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _output("03.000", "c1"),
        *_TASK_TURN_B,
        *_prompt_new("05.200", "search"),
        _rec("05.300", "response_item", {"type": "web_search_call", "status": "completed"}),
        _usage("05.500", 2),
        _answer("07.000"),
        _usage("07.100", 3),
    ]))
    assert _inputs(rounds[1]) == ["user_message"]


def test_a_result_carried_over_from_the_previous_round_is_not_given_back(tmp_path: Path) -> None:
    # c1's result arrives before the round's token_count (carried to the next round), c2's after it; the turn
    # ends; the new turn's first call has neither output nor prompt: it gets back c2's result only, as upstream
    # assigned it (c1's result belonged to the earlier round there)
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _call("02.100", "c2"),
        _output("02.500", "c1"),
        _usage("02.600", 1),
        _output("03.000", "c2"),
        *_TASK_TURN_B,
        _usage("05.500", 2),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 3),
    ]))
    assert _inputs(rounds[1]) == ["tool_result"]
    assert [e.get("tool_call_id") for e in rounds[1]["timing_events"] if e["event_type"] == "tool_result"] == ["c2"]


def test_a_result_dropped_before_an_earlier_round_is_not_given_back_later(tmp_path: Path) -> None:
    turn_c = "019a0000-0000-7000-8000-00000000000c"
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _output("03.000", "c1"),
        *_TASK_TURN_B,                                       # c1's result is dropped here
        _call("05.200", "c2"),
        _usage("05.500", 2),                                 # a round with output: it does not get c1's result
        _output("06.000", "c2"),
        _rec("06.200", "event_msg", {"type": "task_complete", "turn_id": TURN_B}),
        _rec("08.000", "event_msg", {"type": "task_started", "turn_id": turn_c}),
        _rec("08.100", "turn_context", {"turn_id": turn_c, "model": "gpt-5.6-sol"}),
        _usage("08.500", 3),                                 # neither output nor prompt: c2's result only
        *_prompt_new("09.000", "next"),
        _answer("10.000"),
        _usage("10.100", 4),
    ]))
    assert [r["round_id"] for r in rounds] == [f"{TURN}:0", f"{TURN_B}:0", f"{turn_c}:0", f"{turn_c}:1"]
    assert _inputs(rounds[1]) == []
    assert [e.get("tool_call_id") for e in rounds[2]["timing_events"] if e["event_type"] == "tool_result"] == ["c2"]


def _result_ids(round_obj: dict) -> list[str]:
    return [e.get("tool_call_id") for e in round_obj["timing_events"] if e["event_type"] == "tool_result"]


def test_given_back_results_keep_their_order_before_a_result_written_after_the_turn_began(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _call("02.100", "c2"),
        _usage("02.200", 1),
        _output("03.000", "c1"),                             # waiting at the turn change: dropped, then given back
        *_TASK_TURN_B,
        _output("05.200", "c2"),                             # written after turn B began: never dropped
        _usage("05.500", 2),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 3),
    ]))
    assert _result_ids(rounds[1]) == ["c1", "c2"]


def test_a_second_turn_change_before_the_round_closes_is_handled_like_the_first(tmp_path: Path) -> None:
    # turn B starts and is aborted without a token_count; c2's result, written in B, waits at the change to C
    turn_c = "019a0000-0000-7000-8000-00000000000c"
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _call("02.100", "c2"),
        _output("02.500", "c1"),                             # carried to the next round
        _usage("02.600", 1),
        *_TASK_TURN_B,                                       # c1's carried result is dropped here
        _output("05.200", "c2"),
        _rec("05.300", "event_msg", {"type": "turn_aborted", "turn_id": TURN_B}),
        _rec("06.000", "event_msg", {"type": "task_started", "turn_id": turn_c}),
        _rec("06.100", "turn_context", {"turn_id": turn_c, "model": "gpt-5.6-sol"}),
        _usage("06.500", 2),                                 # neither output nor prompt: c2's result only
        *_prompt_new("07.000", "next"),
        _answer("08.000"),
        _usage("08.100", 3),
    ]))
    assert [r["round_id"] for r in rounds] == [f"{TURN}:0", f"{turn_c}:0", f"{turn_c}:1"]
    assert _result_ids(rounds[1]) == ["c2"]


def test_a_result_written_after_the_new_turn_began_does_not_start_its_prompt_round(tmp_path: Path) -> None:
    # usage-first call; its result is written only after the next turn has started, before that turn's prompt
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _rec("03.000", "event_msg", {"type": "turn_aborted", "turn_id": TURN}),
        _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        _output("05.500", "c1"),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 2),
    ]))
    assert _inputs(r2) == ["user_message"] and r2["first_input_event_type"] == "user_message"
    assert r1["tools"][0]["result_at"] is not None


def test_a_message_sent_mid_turn_still_follows_the_result(tmp_path: Path) -> None:
    # paper, Table 2 footnote †(i): a message sent mid-turn arrives with the next tool result -> tool-initiated
    _r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _output("03.000", "c1"),
        _usage("03.100", 1),
        *_prompt_new("03.200", "also lint"),
        _answer("05.000"),
        _usage("05.100", 2),
    ]))
    assert _inputs(r2) == ["tool_result", "user_message"] and r2["first_input_event_type"] == "tool_result"


# --------------------------------------------------------------------------- behaviour pinned by mutation testing


def test_patch_apply_end_results_move_to_the_next_round(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "patch it"),
        _rec("02.000", "response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "p1", "input": "x"}),
        _rec("03.000", "event_msg", {"type": "patch_apply_end", "call_id": "p1", "success": True, "stdout": "done"}),
        _usage("03.100", 1),
        _answer("05.000"),
        _usage("05.100", 2),
    ]))
    assert _inputs(r1) == ["user_message"] and _inputs(r2) == ["tool_result"]


def test_replayed_history_before_the_first_turn_context_leaves_no_activity_behind(tmp_path: Path) -> None:
    # a replayed prompt before any turn_context is discarded with its token_count; the next token_count that
    # has no activity of its own stays an accounting replay, as before this change
    (r,) = extract_codex_session(_write(tmp_path, [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": "0.150.0", "source": "cli"}),
        _rec("00.010", "event_msg", {"type": "user_message", "message": "replayed"}),
        _usage("00.020", 1),
        _rec("00.100", "turn_context", {"turn_id": TURN, "model": "gpt-5.6-sol"}),
        _usage("00.200", 2),
        *_prompt_new("01.000", "go"),
        _answer("02.000"),
        _usage("02.100", 3),
    ]))
    assert r["input_tokens_total"] == 1003 and _inputs(r) == ["user_message"]


def test_a_call_after_a_prompt_with_no_recognized_output_is_still_a_round(tmp_path: Path) -> None:
    r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "search"),
        _rec("02.000", "response_item", {"type": "web_search_call", "status": "completed"}),
        _usage("02.100", 1),
        _answer("04.000"),
        _usage("04.100", 2),
    ]))
    assert (r1["input_tokens_total"], r2["input_tokens_total"]) == (1001, 1002)
    assert _inputs(r1) == ["user_message"]


def test_a_new_turn_announced_only_by_turn_context_is_a_boundary(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path, _waiting_result_then_new_turn(
        [_rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"})], [_answer("05.400")])))
    assert [r["round_id"] for r in rounds] == [f"{TURN}:0", f"{TURN_B}:0", f"{TURN_B}:1"]
    assert _inputs(rounds[1]) == []


def test_raw_sink_drops_a_waiting_result_at_the_turn_boundary(tmp_path: Path) -> None:
    sink: dict = {}
    extract_codex_session(_write(tmp_path, _waiting_result_then_new_turn([
        _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"})], [_answer("05.400")])),
        raw_sink=sink)
    assert list(sink.values())[1]["input"] == ""


def test_a_result_carried_by_an_aborted_call_does_not_leak_into_the_next_turn(tmp_path: Path) -> None:
    # the call's output is written, its result arrives, the turn is aborted before its token_count
    rounds = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _output("03.000", "c1"),
        _rec("03.200", "event_msg", {"type": "turn_aborted", "turn_id": TURN}),
        _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 1),
        _answer("08.000"),
        _usage("08.100", 2),
    ]))
    assert _inputs(rounds[-1]) == []                    # the aborted call's result is not an input later on


def test_a_prompt_waiting_at_a_turn_boundary_is_kept(tmp_path: Path) -> None:
    # only tool results are dropped at a boundary; a user message stays an input, as before this change
    _r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _answer("02.000"),
        _usage("02.100", 1),
        *_prompt_new("03.000", "queued"),
        _rec("03.500", "event_msg", {"type": "task_complete", "turn_id": TURN}),
        _rec("04.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("04.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        _answer("05.000"),
        _usage("05.100", 2),
    ]))
    assert _inputs(r2) == ["user_message"]


def test_without_turn_ids_a_waiting_result_stays_in_front_of_the_next_prompt_as_before(tmp_path: Path) -> None:
    _r1, r2 = extract_codex_session(_write(tmp_path, [
        _rec("00.000", "session_meta", {"id": SESSION, "cli_version": "0.40.0", "source": "cli"}),
        _rec("00.100", "turn_context", {"model": "gpt-5-codex"}),
        _rec("01.000", "event_msg", {"type": "user_message", "message": "go"}),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _output("03.000", "c1"),
        _rec("03.200", "event_msg", {"type": "task_complete"}),
        _rec("40.000", "turn_context", {"model": "gpt-5-codex"}),
        _rec("40.200", "event_msg", {"type": "user_message", "message": "next"}),
        _answer("45.000"),
        _usage("45.100", 2),
    ]))
    assert _inputs(r2) == ["tool_result", "user_message"]


def test_a_mid_call_message_in_the_first_round_of_a_turn_keeps_the_results_before_it(tmp_path: Path) -> None:
    # the new turn's prompt, then an earlier call's result, then output, then a message sent mid-call:
    # only a prompt BEFORE any output drops waiting results
    _r1, r2 = extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _rec("02.500", "event_msg", {"type": "task_complete", "turn_id": TURN}),
        _rec("04.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("04.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        *_prompt_new("05.000", "next"),
        _output("05.500", "c1"),
        _rec("06.000", "response_item", {"type": "reasoning"}),
        *_prompt_new("06.500", "also"),
        _answer("07.000"),
        _usage("07.100", 2),
    ]))
    assert _inputs(r2) == ["user_message", "tool_result", "user_message"]


def test_raw_sink_drops_a_result_written_after_the_new_turn_began(tmp_path: Path) -> None:
    sink: dict = {}
    extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _usage("02.100", 1),
        _rec("03.000", "event_msg", {"type": "turn_aborted", "turn_id": TURN}),
        _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        _output("05.500", "c1"),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 2),
    ]), raw_sink=sink)
    second = list(sink.values())[1]
    assert second["inputKind"] == "user" and second["input"] == "next"


def test_raw_sink_drops_a_result_carried_by_an_aborted_call_at_the_turn_boundary(tmp_path: Path) -> None:
    sink: dict = {}
    extract_codex_session(_write(tmp_path, [
        *_meta(),
        *_prompt_new("01.000", "go"),
        _call("02.000", "c1"),
        _output("03.000", "c1"),
        _rec("03.200", "event_msg", {"type": "turn_aborted", "turn_id": TURN}),
        _rec("05.000", "event_msg", {"type": "task_started", "turn_id": TURN_B}),
        _rec("05.100", "turn_context", {"turn_id": TURN_B, "model": "gpt-5.6-sol"}),
        *_prompt_new("06.000", "next"),
        _answer("07.000"),
        _usage("07.100", 1),
        _answer("08.000"),
        _usage("08.100", 2),
    ]), raw_sink=sink)
    assert list(sink.values())[-1]["input"] == ""
