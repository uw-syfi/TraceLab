"""Synthetic source-derived fixtures, NOT real captures or report-token ground truth.

Codex wait shape: fe439afb81c1ba9999a29e9e996ac6fca561ae4a. Claude fixtures exercise
paired Agent/Task candidates only; toolUseResult metadata is deliberately not evidence.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "artifacts" / "utils"))

from extract_claude_rounds import extract_session_with_key, write_rounds_jsonl
from extract_codex_rounds import extract_codex_session
from merge_round_traces import merge_round_traces
from sanitize_round_trace import StableIdSanitizer, sanitize_row
from trace_privacy import validate_lineage_markers
from trace_db import _TOOL_FIELDS, _child_projection, connect, materialize

PARENT = "10000000-0000-4000-8000-000000000001"
CHILD = "20000000-0000-4000-8000-000000000002"
OTHER = "30000000-0000-4000-8000-000000000003"
PRIVATE = "PRIVATE_REPORT_SENTINEL alice@example.com /clients/acme"


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def record(top, payload, second=0):
    return {"type": top, "timestamp": f"2026-01-01T00:00:{second:02d}Z", "payload": payload}


def usage(sequence):
    tokens = {"input_tokens": 100 + sequence, "output_tokens": 10, "total_tokens": 110 + sequence}
    return record("event_msg", {"type": "token_count", "info": {
        "last_token_usage": tokens, "total_token_usage": tokens,
    }}, sequence * 10)


def completed(*children, timed_out=False):
    return {"status": {child: {"completed": PRIVATE} for child in children}, "timed_out": timed_out}


def codex_records(outputs, *, name="wait", consume=True, output_call="call_wait"):
    rows = [
        record("session_meta", {"id": PARENT, "source": "cli"}),
        record("turn_context", {"turn_id": "turn-1", "model": "gpt-5.5"}),
        record("response_item", {"type": "function_call", "name": "spawn_agent",
                                 "call_id": "call_spawn", "arguments": "{}"}),
        record("response_item", {"type": "function_call", "name": name,
                                 "call_id": "call_wait", "arguments": json.dumps({"ids": [CHILD]})}),
        usage(1),
        record("response_item", {"type": "function_call_output", "call_id": "call_spawn",
                                 "output": json.dumps({"agent_id": CHILD})}, 11),
    ]
    rows.extend(record("response_item", {
        "type": "function_call_output", "call_id": output_call,
        "output": output if isinstance(output, str) else json.dumps(output),
    }, 12 + index) for index, output in enumerate(outputs))
    if consume:
        rows.extend([
            record("turn_context", {"turn_id": "turn-1", "model": "gpt-5.5"}, 19),
            record("response_item", {"type": "message", "role": "assistant", "content": []}, 19),
            usage(2),
        ])
    return rows


def normalized(provider="codex", **extra):
    return {"provider": provider, "session_id": f"{provider}:{PARENT}", "round_id": "round-1",
            "round_index": 0, "input_tokens_total": 100, "tools": [], **extra}


@pytest.mark.parametrize("consume", [False, True])
@pytest.mark.parametrize("timed_out", [False, True])
def test_codex_receipt_owner_consumer_and_sanitized_child_link(tmp_path, consume, timed_out):
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", codex_records(
        [completed(CHILD, OTHER, timed_out=timed_out)], consume=consume,
    )))
    spawn, wait = rows[0]["tools"]
    assert spawn["report_source"] is spawn["report_session_ids"] is None
    assert wait["tool_call_id"] == "call_wait"
    assert wait["report_source"] == "codex_wait_completed"
    assert wait["report_session_ids"] == [f"codex:{CHILD}", f"codex:{OTHER}"]
    assert rows[0]["session_id"] == f"codex:{PARENT}"
    assert wait["result_at"] == "2026-01-01T00:00:12Z"
    assert len(rows) == (2 if consume else 1)
    assert not any(e.get("tool_call_id") == "call_wait" and e["event_type"] == "tool_result"
                   for e in rows[0]["timing_events"])
    if consume:
        assert rows[1]["session_id"] == rows[0]["session_id"]
        assert [(e["tool_call_id"], e["timestamp"]) for e in rows[1]["timing_events"]
                if e["event_type"] == "tool_result"] == [
            ("call_spawn", "2026-01-01T00:00:11Z"), ("call_wait", "2026-01-01T00:00:12Z"),
        ]
    assert PRIVATE not in json.dumps(rows)
    rows.append(normalized(session_id=f"codex:{CHILD}", parent_session_id=f"codex:{PARENT}",
                           session_file="/private/child.jsonl"))
    ids = StableIdSanitizer("reports")
    safe = [sanitize_row(row, ids) for row in rows]
    safe_wait = safe[0]["tools"][1]
    assert safe_wait["report_session_ids"][0] == safe[-1]["session_id"]
    assert safe[-1]["parent_session_id"] == safe[0]["session_id"]
    assert CHILD not in json.dumps(safe) and PRIVATE not in json.dumps(safe)
    assert validate_lineage_markers(safe)
    if consume:
        assert safe_wait["tool_call_id"] in [e.get("tool_call_id") for e in safe[1]["timing_events"]]

    db = materialize(write(tmp_path / "safe.jsonl", safe), tmp_path / "reports.duckdb")
    with connect(db) as con:
        assert con.execute("SELECT report_source, report_session_ids FROM tool_calls WHERE report_source IS NOT NULL").fetchall() == [
            ("codex_wait_completed", safe_wait["report_session_ids"]),
        ]
        assert con.execute("""
            SELECT owner.session_id, consumer.round_index
            FROM tool_calls t JOIN rounds owner ON owner.round_pk = t.round_pk
            JOIN timing_events e ON e.tool_call_id = t.tool_call_id AND e.event_type = 'tool_result'
            JOIN rounds consumer ON consumer.round_pk = e.round_pk AND consumer.session_id = owner.session_id
            WHERE t.report_source = 'codex_wait_completed'
        """).fetchall() == ([(safe[0]["session_id"], 1)] if consume else [])


def test_codex_receipt_survives_turn_change_without_a_consuming_round(tmp_path):
    records = codex_records([completed(CHILD)])
    records[-3]["payload"]["turn_id"] = "turn-2"
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", records))
    assert len(rows) == 2
    wait = rows[0]["tools"][1]
    assert wait["report_source"] == "codex_wait_completed"
    assert wait["report_session_ids"] == [f"codex:{CHILD}"]
    assert wait["result_at"] == "2026-01-01T00:00:12Z"
    assert not any(event["event_type"] == "tool_result"
                   for row in rows for event in row["timing_events"])
    assert PRIVATE not in json.dumps(rows)

    ids = StableIdSanitizer("cross-turn-receipt")
    safe = [sanitize_row(row, ids) for row in rows]
    db = materialize(write(tmp_path / "safe.jsonl", safe), tmp_path / "reports.duckdb")
    with connect(db) as con:
        assert con.execute("SELECT report_source, report_session_ids FROM tool_calls WHERE report_source IS NOT NULL").fetchall() == [
            ("codex_wait_completed", safe[0]["tools"][1]["report_session_ids"]),
        ]
        assert con.execute("SELECT count(*) FROM timing_events WHERE event_type = 'tool_result'").fetchone() == (0,)


@pytest.mark.parametrize("output", [
    {"status": {CHILD: {"completed": None}}, "timed_out": False},
    {"status": {CHILD: {"completed": True}}, "timed_out": False},
    {"status": {CHILD: {"completed": PRIVATE, "errored": PRIVATE}}, "timed_out": False},
    {"status": {CHILD: "running"}, "timed_out": False},
    {"status": {CHILD: {"errored": PRIVATE}}, "timed_out": False},
    {"status": {}, "timed_out": True},
    {"status": [], "timed_out": False},
    {"status": {CHILD: {"completed": PRIVATE}}, "timed_out": 1},
    {"status": {CHILD: {"completed": PRIVATE}}},
    completed(""), completed("not-a-thread"), completed(f"codex:{CHILD}"),
    completed(CHILD, "invalid"), {"agent_id": CHILD}, {"ready": True},
    [], None, "not JSON " + PRIVATE,
])
def test_codex_non_reports(tmp_path, output):
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", codex_records([output])))
    wait = rows[0]["tools"][1]
    assert wait["report_source"] is wait["report_session_ids"] is None
    assert PRIVATE not in json.dumps(rows)


@pytest.mark.parametrize("text", ["", "Process exited with code 1\n" + PRIVATE])
def test_codex_completed_string_is_evidence_not_its_prose(tmp_path, text):
    output = {"status": {CHILD: {"completed": text}, OTHER: "running"}, "timed_out": False}
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", codex_records([output])))
    wait = rows[0]["tools"][1]
    assert wait["report_source"] == "codex_wait_completed"
    assert wait["report_session_ids"] == [f"codex:{CHILD}"]
    assert wait["is_error"] is None
    assert PRIVATE not in json.dumps(rows)


@pytest.mark.parametrize("name", ["spawn_agent", "wait_agent", "task_complete"])
def test_codex_unsupported_tools_and_generic_completion_are_not_receipts(tmp_path, name):
    records = codex_records([completed(CHILD)], name=name)
    records.append(record("event_msg", {"type": "task_complete", "last_agent_message": PRIVATE}))
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", records))
    assert all(tool["report_source"] is None for row in rows for tool in row["tools"])


@pytest.mark.parametrize("output_call", ["call_spawn", "missing"])
def test_codex_output_must_match_wait_call_id(tmp_path, output_call):
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", codex_records(
        [completed(CHILD)], output_call=output_call,
    )))
    assert all(tool["report_source"] is None for tool in rows[0]["tools"])


@pytest.mark.parametrize("outputs", [
    [completed(CHILD), completed(CHILD)], [completed(CHILD), completed(OTHER)],
    [completed(CHILD), {"status": {}, "timed_out": True}],
    [{"status": {}, "timed_out": True}, completed(CHILD)],
    [completed(CHILD), completed(OTHER), completed(CHILD)],
])
@pytest.mark.parametrize("timestamps", [False, True])
def test_codex_repeated_outputs_fail_closed(tmp_path, outputs, timestamps):
    records = codex_records(outputs)
    if not timestamps:
        for item in records:
            item.pop("timestamp", None)
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", records))
    assert rows[0]["tools"][1]["report_source"] is None
    assert rows[0]["tools"][1]["report_session_ids"] is None


def claude_records(name="Agent", *, result=True, consume=True, is_error=False, paired=True):
    def assistant(msg_id, content):
        return {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "message": {
            "role": "assistant", "id": msg_id, "model": "claude-test", "content": content,
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }}
    records = [assistant("msg_1", [{"type": "tool_use", "id": "toolu_123", "name": name, "input": {}}])]
    if result:
        records.append({"type": "user", "timestamp": "2026-01-01T00:00:01Z", "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_123" if paired else "missing",
                                         "content": PRIVATE, "is_error": is_error}],
        }, "toolUseResult": {"agentId": "fabricated-child", "status": "completed"}})
    if consume:
        records.append(assistant("msg_2", []))
    return records


@pytest.mark.parametrize("name", ["Agent", "Task"])
@pytest.mark.parametrize("consume", [False, True])
@pytest.mark.parametrize("is_error", [False, True])
def test_claude_paired_candidates_never_fabricate_child_or_completion(tmp_path, name, consume, is_error):
    rows = extract_session_with_key(write(tmp_path / "claude.jsonl", claude_records(
        name, consume=consume, is_error=is_error,
    )), "project", "parent")
    tool = rows[0]["tools"][0]
    assert tool["report_source"] == "claude_agent_task_result"
    assert tool["report_session_ids"] is None
    assert tool["is_error"] is is_error
    assert len(rows) == (2 if consume else 1)
    if consume:
        assert rows[1]["timing_events"][0]["tool_call_id"] == tool["tool_call_id"]
        assert rows[1]["timing_events"][0]["event_type"] == "tool_result"
    assert PRIVATE not in json.dumps(rows) and "fabricated-child" not in json.dumps(rows)
    safe = sanitize_row(rows[0], StableIdSanitizer("claude"))
    assert safe["tools"][0]["report_source"] == tool["report_source"]
    assert safe["tools"][0]["report_session_ids"] is None
    assert PRIVATE not in json.dumps(safe)


@pytest.mark.parametrize("kwargs", [
    {"name": "Read"}, {"name": "agent"}, {"name": "TaskOutput"}, {"result": False}, {"paired": False},
])
def test_claude_requires_exact_paired_tool_result(tmp_path, kwargs):
    rows = extract_session_with_key(write(tmp_path / "claude.jsonl", claude_records(**kwargs)), "project", "parent")
    assert rows[0]["tools"][0]["report_source"] is None


def test_claude_repeated_results_clear_candidate(tmp_path):
    records = claude_records()
    records.append(records[1])
    rows = extract_session_with_key(write(tmp_path / "claude.jsonl", records), "project", "parent")
    assert rows[0]["tools"][0]["report_source"] is None


@pytest.mark.parametrize("provider,source,session_ids", [
    ("codex", "private-source", None), ("codex", True, None), ("codex", [], None),
    ("codex", {}, None), ("claude", "codex_wait_completed", [f"codex:{CHILD}"]),
    ("codex", "claude_agent_task_result", None),
    ("codex", "codex_wait_completed", None), ("codex", "codex_wait_completed", []),
    ("codex", "codex_wait_completed", True), ("codex", "codex_wait_completed", CHILD),
    ("codex", "codex_wait_completed", [True]), ("codex", "codex_wait_completed", [{}]),
    ("codex", "codex_wait_completed", [f"codex:{CHILD}", f"codex:{CHILD}"]),
    ("codex", "codex_wait_completed", [f"claude:{CHILD}"]),
    ("codex", "codex_wait_completed", ["codex:"]),
    ("codex", "codex_wait_completed", ["codex:private-name"]),
    ("codex", None, [f"codex:{CHILD}"]),
    ("claude", "claude_agent_task_result", ["claude:child"]),
])
def test_report_contract_rejected_at_shared_boundaries(tmp_path, provider, source, session_ids):
    from fastapi import HTTPException
    from web.server.app import _parse_and_validate

    row = normalized(provider, tools=[{"tool_name": "wait", "tool_call_id": "call_wait",
                                      "report_source": source, "report_session_ids": session_ids}])
    for already_sanitized in (False, True):
        with pytest.raises(ValueError, match="report_"):
            sanitize_row(row, StableIdSanitizer("invalid", already_sanitized=already_sanitized))
    output = tmp_path / "out.jsonl"
    output.write_text("keep me")
    # Same identity: the malformed candidate must not disappear through dedup.
    rows = [row, normalized(provider)]
    with pytest.raises(ValueError, match="report_"):
        write_rounds_jsonl(output, rows, append_dedup=False)
    with pytest.raises(ValueError, match="report_"):
        merge_round_traces([write(tmp_path / "input.jsonl", rows)], output)
    assert output.read_text() == "keep me"
    with pytest.raises(HTTPException) as exc:
        _parse_and_validate(gzip.compress((json.dumps(row) + "\n").encode()))
    assert exc.value.status_code == 422 and "report_" in str(exc.value.detail)


@pytest.mark.parametrize("tools", [None, [{"tool_name": "Read"}], [{"report_source": None, "report_session_ids": None}]])
def test_legacy_and_null_reports_materialize_as_typed_null(tmp_path, tools):
    row = normalized()
    if tools is None:
        row.pop("tools")
    else:
        row["tools"] = tools
    db = materialize(write(tmp_path / "legacy.jsonl", [row]), tmp_path / "legacy.duckdb")
    with connect(db) as con:
        types = {r[0]: r[1] for r in con.execute("DESCRIBE tool_calls").fetchall()}
        assert types["report_source"] == "VARCHAR" and types["report_session_ids"] == "VARCHAR[]"
        assert con.execute("SELECT report_source, report_session_ids FROM tool_calls").fetchall() == (
            [] if tools is None else [(None, None)]
        )


@pytest.mark.parametrize("tool", [{}, {"report_source": None, "report_session_ids": None}, {
    "report_source": "codex_wait_completed", "report_session_ids": [f"codex:{CHILD}", f"codex:{OTHER}"],
}])
def test_json_fallback_projection_pins_report_types(tool):
    specs = [spec for spec in _TOOL_FIELDS if spec[0].startswith("report_")]
    with duckdb.connect() as con:
        con.execute("CREATE TABLE projected AS SELECT " + ", ".join(_child_projection("tc", None, specs))
                    + " FROM (SELECT ?::JSON AS tc)", [json.dumps(tool)])
        assert con.execute("SELECT * FROM projected").fetchone() == (
            tool.get("report_source"), tool.get("report_session_ids"),
        )
        assert [r[1] for r in con.execute("DESCRIBE projected").fetchall()] == ["VARCHAR", "VARCHAR[]"]


@pytest.mark.parametrize("existing_reports", [False, True])
@pytest.mark.parametrize("old_runner_fields", [False, True])
def test_readonly_compat_reports_share_existing_view_without_shadowing(tmp_path, existing_reports, old_runner_fields):
    db = tmp_path / "prebuilt.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE rounds AS SELECT 1 AS round_pk, 'codex' AS provider, 'codex:legacy' AS session_id")
        con.execute("CREATE TABLE timing_events AS SELECT 1 AS round_pk")
        fields = ["1 AS round_pk", "'wait' AS tool_name"]
        if old_runner_fields:
            fields += ["'exited' AS process_state", "0 AS process_exit_code", "'call_root' AS root_tool_call_id"]
        else:
            fields += [f"CAST(NULL AS {kind}) AS {name}" for name, kind, _ in _TOOL_FIELDS
                       if name != "tool_name" and not name.startswith("report_")]
        if existing_reports:
            fields += ["'codex_wait_completed'::VARCHAR AS report_source",
                       f"['codex:{CHILD}']::VARCHAR[] AS report_session_ids"]
        con.execute("CREATE TABLE tool_calls AS SELECT " + ", ".join(fields))
    before, mtime = db.read_bytes(), db.stat().st_mtime_ns
    with connect(db) as con:
        assert con.execute("SELECT report_source, report_session_ids FROM tool_calls").fetchone() == (
            ("codex_wait_completed", [f"codex:{CHILD}"]) if existing_reports else (None, None)
        )
        assert con.execute("SELECT command_status, command_exit_code FROM tool_calls").fetchone() == (
            ("finished", 0) if old_runner_fields else (None, None)
        )
        types = {r[0]: r[1] for r in con.execute("DESCRIBE tool_calls").fetchall()}
        assert types["report_source"] == "VARCHAR" and types["report_session_ids"] == "VARCHAR[]"
        assert con.execute("SELECT event_index FROM timing_events").fetchone() == (1,)
    assert db.read_bytes() == before and db.stat().st_mtime_ns == mtime
