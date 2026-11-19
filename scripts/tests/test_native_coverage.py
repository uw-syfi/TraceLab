"""Native lifecycle/tool observations must not invent usage, clocks, identities, or private text."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts"), str(ROOT / "artifacts/utils")]

from extract_claude_rounds import extract_session_with_key
from extract_codex_rounds import extract_codex_session
from sanitize_round_trace import StableIdSanitizer, sanitize_row
from trace_db import connect, materialize
from trace_privacy import validate_lineage_markers
from web.payload import ingest

SESSION = "019a0000-0000-7000-8000-000000000001"
PARENT = "019a0000-0000-7000-8000-000000000000"
TURN = "019a0000-0000-7000-8000-000000000002"
SECRET = "PRIVATE_QUERY alice@example.com /private/repo"


def rec(top, second, **payload):
    return {"type": top, "timestamp": f"2026-01-01T00:00:{second:02d}Z", "payload": payload}


def usage(second, n=1):
    tokens = {"input_tokens": 100 + n, "output_tokens": n, "total_tokens": 101 + n}
    return rec("event_msg", second, type="token_count", info={
        "last_token_usage": tokens, "total_token_usage": tokens,
    })


def prelude(source="cli"):
    return [rec("session_meta", 0, id=SESSION, source=source),
            rec("turn_context", 1, turn_id=TURN, model="gpt-test"),
            rec("event_msg", 2, type="task_started", turn_id=TURN),
            rec("event_msg", 3, type="user_message", message="go")]


def answer(second=6):
    return rec("response_item", second, type="message", role="assistant", content=[])


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


@pytest.mark.parametrize("end,event", [("task_complete", "request_end"), ("turn_aborted", "request_aborted")])
def test_lifecycle_backfills_existing_usage_row_and_deduplicates(tmp_path, end, event):
    marker = rec("event_msg", 8, type=end, turn_id=TURN, last_agent_message=SECRET)
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [*prelude(), answer(), usage(7), marker, marker]))
    assert len(rows) == 1
    row = rows[0]
    assert row["round_id"] == f"{TURN}:0" and row["input_tokens_total"] == 101
    assert row["session_kind"] == "main"
    events = [e for e in row["timing_events"] if e["event_type"].startswith("request_")]
    assert [(e["event_type"], e["timestamp"], e["turn_id"]) for e in events] == [
        ("request_start", "2026-01-01T00:00:02Z", TURN), (event, "2026-01-01T00:00:08Z", TURN),
    ]
    assert SECRET not in json.dumps(rows)


@pytest.mark.parametrize("source,expected", [
    ({"subagent": {"other": "guardian"}}, "guardian"),
    ({"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}, "worker"),
    ({"subagent": {"other": "unproved"}}, "unknown"),
])
def test_child_kind_uses_matching_metadata_not_replayed_parent(tmp_path, source, expected):
    records = prelude(source)
    records.insert(1, rec("session_meta", 0, id=PARENT, source="vscode"))
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [*records, answer(), usage(7)]))
    assert rows[0]["session_id"] == f"codex:{SESSION}"
    assert rows[0]["session_kind"] == expected


def test_conflicting_kind_stays_unknown(tmp_path):
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [
        *prelude({"subagent": {"other": "guardian"}}),
        rec("session_meta", 4, id=SESSION, source="cli"), answer(), usage(7),
    ]))
    assert rows[0]["session_kind"] == "unknown"


def test_partial_kind_with_conflicting_origin_stays_unknown(tmp_path):
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [
        *prelude(), rec("session_meta", 4, id=SESSION, source={"subagent": "review"}),
        answer(), usage(7),
    ]))
    assert rows[0]["session_origin"] == rows[0]["session_kind"] == "unknown"


@pytest.mark.parametrize("top,kind,counter", [
    ("event_msg", "task_started", "incomplete_lifecycle_records"),
    ("turn_context", None, "incomplete_records"),
])
def test_malformed_turn_identity_does_not_corrupt_current_request(tmp_path, top, kind, counter):
    diagnostics = {}
    malformed = rec(top, 4, turn_id=["invalid"], **({"type": kind} if kind else {}))
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [*prelude(), malformed, answer(), usage(7)]),
                                 diagnostics_sink=diagnostics)
    assert len(rows) == 1 and rows[0]["turn_id"] == TURN
    assert diagnostics == {counter: 1}


def test_web_end_without_start_and_duplicate_representations(tmp_path):
    end = rec("event_msg", 5, type="web_search_end", call_id="call_search", query=SECRET)
    anonymous = rec("response_item", 5, type="web_search_call", id="item_search", status="completed", action={"query": SECRET})
    diagnostics = {}
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [
        *prelude(), end, end, anonymous, anonymous, answer(), usage(7),
    ]), diagnostics_sink=diagnostics)
    tool, = rows[0]["tools"]
    assert tool["tool_name"] == "web_search" and tool["tool_call_id"] == "call_search"
    assert tool["emitted_at"] is tool["tool_wall_latency_ms"] is tool["tool_internal_latency_ms"] is None
    assert tool["result_at"] == "2026-01-01T00:00:05Z"
    observations = [e for e in rows[0]["timing_events"] if e["event_type"] == "web_search_observation"]
    assert len(observations) == 2 and sum("tool_call_id" in e for e in observations) == 1
    assert not any(e["event_type"] == "tool_result" for e in rows[0]["timing_events"])
    assert rows[0]["current_user_message_count"] == 1 and rows[0]["current_tool_result_count"] == 0
    assert diagnostics == {"duplicate_web_search_observations": 2,
                           "web_search_observations_without_call_id": 1, "web_search_calls_missing_start": 1}
    assert SECRET not in json.dumps(rows)


def test_native_web_begin_end_keeps_only_observed_clock(tmp_path):
    rows = extract_codex_session(write(tmp_path / "raw.jsonl", [
        *prelude(), rec("event_msg", 4, type="web_search_begin", call_id="call_search"),
        rec("event_msg", 5, type="web_search_end", call_id="call_search"), answer(), usage(7),
    ]))
    tool, = rows[0]["tools"]
    assert tool["tool_wall_latency_ms"] == 1000 and tool["tool_internal_latency_ms"] is None


def test_trailing_output_and_malformed_records_are_diagnostics_not_rounds(tmp_path):
    path = write(tmp_path / "raw.jsonl", [*prelude(), answer(), usage(7),
        rec("response_item", 8, type="reasoning"), answer(9),
        {"type": "event_msg", "payload": None},
    ])
    with path.open("a") as out:
        out.write('{"incomplete":\n')
    diagnostics = {}
    rows = extract_codex_session(path, diagnostics_sink=diagnostics)
    assert len(rows) == 1 and rows[0]["output_tokens"] == 1
    assert diagnostics == {"incomplete_records": 1, "malformed_json_records": 1,
                           "unaccounted_output_records": 2}
    empty = {}
    assert extract_codex_session(write(tmp_path / "no-usage.jsonl", [*prelude(), answer()]), diagnostics_sink=empty) == []
    assert empty["unaccounted_output_records"] == 1 and empty["unattached_lifecycle_events"] == 1


@pytest.mark.parametrize("stop,ended", [("end_turn", True), ("stop_sequence", True), ("tool_use", False), (None, False)])
def test_claude_completion_requires_native_final_reason(tmp_path, stop, ended):
    row = {"type": "assistant", "timestamp": "2026-01-01T00:00:04Z", "message": {
        "role": "assistant", "id": "msg_example", "model": "claude-test", "content": [],
        "stop_reason": stop, "usage": {"input_tokens": 10, "output_tokens": 2},
    }}
    rows = extract_session_with_key(write(tmp_path / "raw.jsonl", [row, row]), "project", "root", project_layout=True)
    assert len(rows) == 1 and rows[0]["session_kind"] == "main"
    assert sum(e["event_type"] == "request_end" for e in rows[0]["timing_events"]) == int(ended)


def test_ingest_sanitize_database_and_legacy_contract(tmp_path):
    raw = write(tmp_path / "raw.jsonl", [*prelude(),
        rec("event_msg", 5, type="web_search_end", call_id="call_search", query=SECRET),
        answer(), usage(7), rec("event_msg", 8, type="task_complete", turn_id=TURN),
        answer(9),
    ])
    meta = ingest.prepare(raw, tmp_path / "out")
    assert meta["coverage_diagnostics"]["codex"]["unaccounted_output_records"] == 1
    safe = [json.loads(s) for s in Path(meta["files"]["sanitized_jsonl"]).read_text().splitlines()]
    assert SECRET not in json.dumps(safe) and TURN not in json.dumps(safe)
    assert safe[0]["turn_id"] != TURN
    assert {e["turn_id"] for e in safe[0]["timing_events"] if "turn_id" in e} == {safe[0]["turn_id"]}
    assert safe[0]["tools"][0]["tool_name"] == "web_search"
    db = materialize(meta["files"]["sanitized_jsonl"], tmp_path / "new.duckdb")
    with connect(db) as con:
        assert con.execute("SELECT session_kind FROM rounds").fetchone() == ("main",)
        assert con.execute("SELECT count(*) FROM timing_events WHERE event_type='request_end' AND turn_id IS NOT NULL").fetchone() == (1,)
        assert con.execute("SELECT emitted_at,tool_wall_latency_ms FROM tool_calls").fetchone() == (None, None)

    legacy = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(legacy)) as con:
        con.execute("CREATE TABLE rounds AS SELECT 1 round_pk, 'codex' provider, 'codex:old' session_id")
        con.execute("CREATE TABLE timing_events AS SELECT 1 round_pk, 'text' event_type")
        con.execute("CREATE TABLE tool_calls AS SELECT 1 round_pk, 'shell' tool_name")
    with connect(legacy) as con:
        assert con.execute("SELECT session_kind FROM rounds").fetchone() == ("unknown",)
        assert con.execute("SELECT turn_id,event_index FROM timing_events").fetchone() == (None, 1)


def test_claude_message_snapshots_have_one_latest_completion(tmp_path):
    thinking = {"type": "assistant", "timestamp": "2026-01-01T00:00:04Z", "message": {
        "role": "assistant", "id": "msg_example", "model": "claude-test",
        "content": [{"type": "thinking", "thinking": "work"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }}
    text = {**thinking, "timestamp": "2026-01-01T00:00:05Z", "message": {
        **thinking["message"], "content": [{"type": "text", "text": "done"}],
    }}
    rows = extract_session_with_key(write(tmp_path / "raw.jsonl", [thinking, text, thinking]), "project", "root")
    ends = [e for e in rows[0]["timing_events"] if e["event_type"] == "request_end"]
    assert len(ends) == 1 and ends[0]["timestamp"] == text["timestamp"]
    assert rows[0]["input_tokens_total"] == 10 and rows[0]["output_tokens"] == 2


def test_session_kind_validation_does_not_publish_arbitrary_metadata():
    row = {"provider": "codex", "session_id": "codex:x", "round_index": 0,
           "input_tokens_total": 1, "session_kind": SECRET}
    with pytest.raises(ValueError, match="session_kind"):
        validate_lineage_markers([row])
    with pytest.raises(ValueError, match="session_kind"):
        sanitize_row(row, StableIdSanitizer("test"))
