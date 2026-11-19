from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import collect_llm_traces as collector
import extract_claude_rounds as claude
from trace_privacy import row_has_leak
from web.payload import ingest


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def assistant(mid, second, content):
    return {"type": "assistant", "timestamp": f"2026-01-01T00:00:{second:02d}Z",
            "message": {"role": "assistant", "id": mid, "model": "claude-test",
                        "content": content, "usage": {"input_tokens": 10, "output_tokens": 2,
                        "cache_creation_input_tokens": 3,
                        "cache_creation": {"ephemeral_1h_input_tokens": 3}}}}


def family(tmp_path, *, name="Agent", parent=None):
    project = tmp_path / "projects" / "project"
    launch = assistant("launch", 1, [{"type": "tool_use", "id": "private-call",
                                      "name": name, "input": {"prompt": "private task"}}])
    result = {"type": "user", "timestamp": "2026-01-01T00:00:03Z",
              "message": {"role": "user", "content": [{"type": "tool_result",
                          "tool_use_id": "private-call", "content": "reply"}]},
              "toolUseResult": {"agentId": "child", "status": "completed"}}
    owner = write(project / "root.jsonl", [launch, result])
    child = write(project / "root/subagents/agent-child.jsonl",
                  [assistant("child-round", 2, [])])
    metadata = {"toolUseId": "private-call", "description": "private description"}
    if parent is not None:
        metadata["parentAgentId"] = parent
    child.with_suffix(".meta.json").write_text(json.dumps(metadata))
    return owner, child, launch, result


def extract(paths):
    batches = [(path, ingest._extract_one_file(path)[0]) for path in paths]
    claude.resolve_parent_links(batches)
    return {rows[0]["session_id"]: rows for _, rows in batches if rows}


@pytest.mark.parametrize("name", ["Agent", "Task"])
def test_exact_native_ids_recover_parent_without_changing_usage(tmp_path, name):
    owner, child, _, _ = family(tmp_path, name=name)
    assert ingest._extract_one_file(child)[0][0]["parent_session_id"] is None
    rows = extract([child, owner, owner, child])  # Repeated paths neither invent calls nor lose updates.
    row = rows["claude:root/subagents/agent-child"][0]
    assert row["parent_session_id"] == row["root_session_id"] == "claude:root"
    assert row["claude_cache_creation_1h_input_tokens"] == 3
    assert "toolUseId" not in json.dumps(row) and "agentId" not in json.dumps(row)


@pytest.mark.parametrize("failure", [
    "duplicate_call", "duplicate_result", "wrong_agent", "missing_agent", "wrong_tool",
    "result_before_call", "child_before_call", "missing_child_time", "missing_result_time",
    "unretained_call", "error_result", "two_result_blocks", "cross_family", "cross_project",
    "two_owners", "duplicate_claim",
])
def test_incomplete_or_ambiguous_evidence_does_not_infer_parent(tmp_path, failure):
    owner, child, launch, result = family(tmp_path)
    records = [launch, result]
    if failure == "duplicate_call":
        launch["message"]["content"] *= 2
    elif failure == "duplicate_result":
        records.append(result)
    elif failure == "wrong_agent":
        result["toolUseResult"]["agentId"] = "other"
    elif failure == "missing_agent":
        result["toolUseResult"] = {}
    elif failure == "wrong_tool":
        launch["message"]["content"][0]["name"] = "Bash"
    elif failure == "result_before_call":
        result["timestamp"] = "2026-01-01T00:00:00Z"
    elif failure == "child_before_call":
        write(child, [assistant("child-round", 0, [])])
    elif failure == "missing_child_time":
        record = assistant("child-round", 2, [])
        record.pop("timestamp")
        write(child, [record])
    elif failure == "missing_result_time":
        result.pop("timestamp")
    elif failure == "unretained_call":
        launch["message"]["model"] = "<synthetic>"
        records.append(assistant("retained-other", 4, []))
    elif failure == "error_result":
        result["message"]["content"][0]["is_error"] = True
    elif failure == "two_result_blocks":
        result["message"]["content"].append({"type": "tool_result", "tool_use_id": "other"})
    elif failure == "cross_family":
        owner = owner.with_name("other-root.jsonl")
    elif failure == "cross_project":
        owner = tmp_path / "elsewhere/projects/project/root.jsonl"
    write(owner, records)
    files = [owner, child]
    if failure == "two_owners":
        files.append(write(child.with_name("agent-other.jsonl"), records))
    elif failure == "duplicate_claim":
        other = write(child.with_name("agent-other.jsonl"), [assistant("other-child", 2, [])])
        other.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": "private-call"}))
        files.append(other)
    row = extract(files)["claude:root/subagents/agent-child"][0]
    assert row["parent_session_id"] is None


def test_nested_owner_and_explicit_parent_agreement(tmp_path):
    owner, child, launch, result = family(tmp_path, parent="parent")
    owner.unlink()
    parent = write(child.with_name("agent-parent.jsonl"), [launch, result])
    rows = extract([parent, child])
    assert rows["claude:root/subagents/agent-child"][0]["parent_session_id"] == "claude:root/subagents/agent-parent"


def test_conflicting_exact_and_explicit_parents_are_not_overwritten(tmp_path):
    owner, child, _, _ = family(tmp_path, parent="other")
    assert ingest._extract_one_file(child)[0][0]["parent_session_id"].endswith("agent-other")
    row = extract([owner, child])["claude:root/subagents/agent-child"][0]
    assert row["parent_session_id"] is None
    assert row["root_session_id"] == "claude:root"


def test_spawn_depth_and_unsupplied_native_files_do_not_supply_edges(tmp_path):
    owner, child, _, _ = family(tmp_path)
    child.with_suffix(".meta.json").write_text(json.dumps({"spawnDepth": 1}))
    assert extract([owner, child])["claude:root/subagents/agent-child"][0]["parent_session_id"] is None
    child.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": "private-call"}))
    assert extract([child])["claude:root/subagents/agent-child"][0]["parent_session_id"] is None


def test_cli_collector_and_serial_parallel_ingest_agree(tmp_path, monkeypatch):
    owner, child, _, _ = family(tmp_path)
    files = [child, owner]
    serial, _ = ingest._extract_normalized(files, [], lambda _: None, jobs=1)
    monkeypatch.setattr(ingest, "_PARALLEL_MIN_FILES", 1)
    parallel_warnings = []
    parallel, _ = ingest._extract_normalized(files, parallel_warnings, lambda _: None, jobs=2)
    assert parallel_warnings == []
    assert serial == parallel
    output = tmp_path / "cli.jsonl"
    monkeypatch.setattr(sys, "argv", ["extract_claude_rounds.py", str(owner.parent), "-o", str(output)])
    assert claude.main() == 0
    expected = [{**row, "trace_key": claude.round_key(row)} for row in serial]
    assert [json.loads(line) for line in output.read_text().splitlines()] == expected
    collected = tmp_path / "collected.jsonl"
    collector.append_claude_round_trace(
        output_path=collected,
        sources=[{"project_dir": owner.parent, "home": "/private", "user": "private", "store": ".claude"}],
        project_filters=[], progress_enabled=False,
    )
    rows = [json.loads(line) for line in collected.read_text().splitlines()]
    for row in rows:
        for key in ("home", "user", "store"):
            row.pop(key)
    assert rows == expected
    meta = ingest.prepare(owner, tmp_path / "prepared", files=files)
    public = Path(meta["files"]["sanitized_jsonl"]).read_text()
    public_rows = [json.loads(line) for line in public.splitlines()]
    assert not any(row_has_leak(row) for row in public_rows)
    assert all(secret not in public for secret in ("private-call", "private description", "private task", "agent-child", "toolUseId", "agentId"))
    main = next(row for row in public_rows if row["session_origin"] == "claude_top_level_layout")
    subagent = next(row for row in public_rows if row["session_origin"] == "claude_subagent_layout")
    assert subagent["parent_session_id"] == subagent["root_session_id"] == main["session_id"]


def test_uploaded_normalized_paths_do_not_supply_native_call_owners(tmp_path):
    owner, child, _, _ = family(tmp_path)
    normalized = ingest._extract_one_file(owner)[0]
    uploaded = write(tmp_path / "uploaded.jsonl", normalized)
    rows, _ = ingest._extract_normalized([uploaded, child], [], lambda _: None)
    subagent = next(row for row in rows if row["session_origin"] == "claude_subagent_layout")
    assert subagent["parent_session_id"] is None
