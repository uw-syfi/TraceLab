from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "artifacts" / "utils"))

import sanitize_round_trace as san
import collect_llm_traces as collector
from extract_claude_rounds import extract_session_with_key, write_rounds_jsonl
from extract_codex_rounds import extract_codex_session
from merge_round_traces import merge_round_traces
from subagent_cohorts import COHORT_SQL
from trace_db import connect, materialize
from trace_privacy import validate_lineage_markers
from web.payload import ingest


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def read(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def private_row(sid="claude:root", **extra):
    return {
        "provider": "claude", "session_id": sid, "round_id": "msg_1234567890ab",
        "round_index": 0, "input_tokens_total": 100, "session_file": "/private/raw.jsonl",
        "tools": [], **extra,
    }


def claude_file(path):
    return write(path, [{"type": "assistant", "message": {
        "role": "assistant", "id": "msg_1234567890ab", "model": "claude-test",
        "content": [], "usage": {"input_tokens": 100, "output_tokens": 10},
    }}])


def test_claude_layout_and_detached(tmp_path):
    retained = claude_file(tmp_path / "projects" / "project" / "root.jsonl")
    detached = claude_file(tmp_path / "root.jsonl")
    truncated = claude_file(tmp_path / "projects" / "root.jsonl")
    assert ingest._extract_one_file(retained)[0][0]["session_origin"] == "claude_top_level_layout"
    assert ingest._extract_one_file(detached)[0][0]["session_origin"] == "unknown"
    row = ingest._extract_one_file(truncated)[0][0]
    assert row["session_origin"] == "unknown"
    assert row["project"] != truncated.name
    assert row["parent_session_id"] is None and row["root_session_id"] is None
    assert extract_session_with_key(detached, "project", "root")[0]["session_origin"] == "unknown"
    child = claude_file(retained.parent / "root" / "subagents" / "agent-child.jsonl")
    child.with_suffix(".meta.json").write_text(json.dumps({"parentAgentId": "parent"}))
    row = ingest._extract_one_file(child)[0][0]
    assert row["session_origin"] == "claude_subagent_layout"
    assert row["root_session_id"] == "claude:root"
    assert row["parent_session_id"] == "claude:root/subagents/agent-parent"
    child.with_suffix(".meta.json").unlink()
    row = ingest._extract_one_file(child)[0][0]
    assert row["root_session_id"] == "claude:root" and row["parent_session_id"] is None
    assert ingest._extract_one_file(claude_file(tmp_path / "agent-child.jsonl"))[0][0]["session_origin"] == "unknown"


@pytest.mark.parametrize("metadata", [None, {
    "parentAgentId": "parent", "spawnDepth": 2, "workflowPhase": "implementation",
}])
def test_claude_workflow_layout(tmp_path, metadata):
    for workflow in ("wf_12345678-abc", "wf_two"):
        key = f"root/subagents/workflows/{workflow}/agent-child"
        path = claude_file(tmp_path / "projects" / "project" / f"{key}.jsonl")
        if metadata is not None:
            path.with_suffix(".meta.json").write_text(json.dumps(metadata))
        row = ingest._extract_one_file(path)[0][0]
        assert row["session_id"] == f"claude:{key}"
        assert row["session_origin"] == "claude_subagent_layout"
        assert row["root_session_id"] == "claude:root"
        assert row["parent_session_id"] is None
        assert extract_session_with_key(path, "project", key) == [row]


@pytest.mark.parametrize("key", [
    "root/subagents/workflows/wf_one/journal",
    "root/subagents/workflows/wf_one/nested/agent-child",
    "root/subagents/workflows/wf_one/agent-child/nested",
    "root/subagents/workflows/wf_/agent-child",
    "root/subagents/workflows/wf-one/agent-child",
    "root/subagents/workflows/other/agent-child",
    "root/subagents/workflows/wf_one/agent-",
    "root/subagents/workflows/wf_one/child",
    "root/subagents/workflows/agent-child",
    "extra/root/subagents/workflows/wf_one/agent-child",
    "root/subagents/nested/agent-child",
])
def test_claude_unsupported_subagent_layouts(tmp_path, key):
    path = claude_file(tmp_path / "projects" / "project" / f"{key}.jsonl")
    row = ingest._extract_one_file(path)[0][0]
    assert row["session_id"] == f"claude:{key}"
    assert row["session_origin"] == "unknown"
    assert row["root_session_id"] is None and row["parent_session_id"] is None


@pytest.mark.parametrize("observed_root", [True, False])
def test_claude_workflow_cohort(tmp_path, observed_root):
    keys = [f"root/subagents/workflows/{workflow}/agent-child" for workflow in ("wf_one", "wf_two")]
    if observed_root:
        keys.append("root")
    ids = san.StableIdSanitizer("workflow")
    rows = [
        san.sanitize_row(row, ids)
        for key in keys
        for row in ingest._extract_one_file(
            claude_file(tmp_path / "projects" / "project" / f"{key}.jsonl")
        )[0]
    ]
    assert validate_lineage_markers(rows) == ids.identity_scope_id
    root = ids.session_id("claude", "claude:root")
    children = {ids.session_id("claude", f"claude:{key}") for key in keys[:2]}
    trace = write(tmp_path / "sanitized.jsonl", rows)
    db = trace.with_suffix(".duckdb")
    materialize(trace, db)
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM sessions").fetchone() == (len(keys),)
        assert set(con.execute("""
            SELECT session_id, observed_parent_session_id, parent_status,
                   root_status, resolved_root_session_id, depth
            FROM sessions WHERE session_origin = 'claude_subagent_layout'
        """).fetchall()) == {
            (child, None, "unknown", "resolved_explicit" if observed_root else "missing",
             root if observed_root else None, None)
            for child in children
        }
        cohort = con.execute(f"WITH cohort AS ({COHORT_SQL}) SELECT * FROM cohort").fetchall()
        expected = ({("claude", root, root, "main")} | {
            ("claude", child, root, "descendant") for child in children
        }) if observed_root else set()
        assert len(cohort) == len(expected)
        assert set(cohort) == expected
        assert con.execute(f"""
            WITH cohort AS ({COHORT_SQL})
            SELECT c.role, count(*) FROM cohort c
            JOIN rounds r ON r.provider = c.provider AND r.session_id = c.session_id
            GROUP BY c.role ORDER BY c.role
        """).fetchall() == ([("descendant", 2), ("main", 1)] if observed_root else [])


def codex_turn(turn="300", sequence=1):
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": sequence}
    return [
        {"type": "turn_context", "payload": {"turn_id": turn, "model": "codex-test"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": []}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": usage, "total_token_usage": usage,
        }}},
    ]


@pytest.mark.parametrize("source,expected", [
    ("cli", "codex_top_level_source"), ("vscode", "codex_top_level_source"),
    ("exec", "codex_top_level_source"), ("mcp", "unknown"), (None, "unknown"),
    ("unknown", "unknown"), ("custom", "unknown"), ("internal", "unknown"),
    ({"subagent": "review"}, "codex_subagent_source"),
    ({"subagent": "compact"}, "codex_subagent_source"),
    ({"subagent": "memory_consolidation"}, "codex_subagent_source"),
    ({"subagent": {"other": "custom"}}, "codex_subagent_source"),
    ({"subagent": {"thread_spawn": {}}}, "codex_subagent_source"),
    ({"subagent": "unrecognized"}, "unknown"), ({"subagent": {}}, "unknown"),
])
def test_codex_native_source_without_parent(tmp_path, source, expected):
    # Provider-native hierarchy says nothing about external launch orchestrators.
    path = write(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "200", "source": source}}, *codex_turn(),
    ])
    row = extract_codex_session(path)[0]
    assert row["session_origin"] == expected
    assert row["parent_session_id"] is None


@pytest.mark.parametrize("evidence", [{"parent_thread_id": "100"}, {"subagent": "review"}])
def test_codex_contradictory_top_level(tmp_path, evidence):
    path = write(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "200", "source": "cli", **evidence}},
        *codex_turn(),
    ])
    assert extract_codex_session(path)[0]["session_origin"] == "unknown"


@pytest.mark.parametrize("evidence", [{"parent_thread_id": "100"}, {"subagent": "review"}])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("interleaved", [False, True])
def test_codex_reconciles_contradictory_top_level(tmp_path, evidence, reverse, interleaved):
    payloads = [{"id": "200", "source": "cli"}, {"id": "200", "source": "cli", **evidence}]
    if reverse:
        payloads.reverse()
    records = []
    for index, payload in enumerate(payloads):
        records.append({"type": "session_meta", "payload": payload})
        if interleaved:
            records.extend(codex_turn(str(300 + index), index + 1))
    if not interleaved:
        records.extend(codex_turn())
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", records))
    assert rows and {row["session_origin"] for row in rows} == {"unknown"}


def test_codex_replayed_parent_does_not_inherit_child_origin(tmp_path):
    path = write(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "200", "source": {
            "subagent": {"thread_spawn": {"parent_thread_id": "100"}},
        }}},
        {"type": "session_meta", "payload": {"id": "100"}},
        *codex_turn("150", 1), *codex_turn("300", 2),
    ])
    parent, child = extract_codex_session(path)
    assert (parent["session_id"], parent["session_origin"]) == ("codex:100", "unknown")
    assert parent["parent_session_id"] is None
    assert child["session_origin"] == "codex_subagent_source"
    assert child["parent_session_id"] == parent["session_id"]


def test_sanitizer_scope_links_and_fresh_batches():
    raw = [private_row(session_origin="claude_top_level_layout"), private_row(
        "claude:child", parent_session_id="claude:root", root_session_id="claude:root",
    )]
    ids = san.StableIdSanitizer("same")
    rows = [san.sanitize_row(row, ids) for row in raw]
    scope = validate_lineage_markers(rows)
    assert str(UUID(scope)) == scope
    assert rows[1]["parent_session_id"] == rows[0]["session_id"] == rows[1]["root_session_id"]
    assert rows[0]["session_origin"] == "claude_top_level_layout"
    assert rows[1]["session_origin"] == "unknown"
    fresh = san.sanitize_row(raw[0], san.StableIdSanitizer("same"))
    assert fresh["identity_scope_id"] != scope
    assert fresh["session_id"] == rows[0]["session_id"]


def test_raw_tool_payload_is_not_lineage_contract():
    raw = private_row(tools=[{"tool_name": "Read", "tool_call_id": "call_x", "input": {
        "session_origin": "/private/user/text", "identity_scope_id": {"arbitrary": True},
    }}])
    row = san.sanitize_row(raw, san.StableIdSanitizer("same"))
    assert "input" not in row["tools"][0]


@pytest.mark.parametrize("legacy", [False, True])
def test_scoped_and_legacy_passthrough(tmp_path, monkeypatch, legacy):
    row = san.sanitize_row(private_row(), san.StableIdSanitizer("same"))
    if legacy:
        row.pop("identity_scope_id")
        row.pop("session_origin")
    assert san.sanitize_row(row, san.StableIdSanitizer("different", already_sanitized=True)) == row
    source = write(tmp_path / "input.jsonl", [row])
    output = tmp_path / "out.jsonl"
    monkeypatch.setattr(sys, "argv", ["sanitize", str(source), "-o", str(output), "--already-sanitized"])
    assert san.main() == 0
    assert read(output) == [row]
    meta = ingest.prepare(source, tmp_path / "prepared", already_sanitized=True)
    result = read(meta["files"]["sanitized_jsonl"])[0]
    assert meta["kind"] == "sanitized"
    assert result.get("identity_scope_id") == row.get("identity_scope_id")
    assert ("identity_scope_id" in result) is not legacy


@pytest.mark.parametrize("claimed_scope", [False, True])
def test_ambiguous_normalized_input_is_sanitized_without_certifying_scope(tmp_path, monkeypatch, claimed_scope):
    raw = {
        "provider": "claude", "session_id": "claude:customer-project", "round_index": 0,
        "input_tokens_total": 100, "user": "alice@example.com", "project": "/clients/acme",
    }
    if claimed_scope:
        raw.update(identity_scope_id=str(uuid4()), session_origin="unknown")
    row = san.sanitize_row(raw, san.StableIdSanitizer("same"))
    source = write(tmp_path / "input.jsonl", [raw])
    output = tmp_path / "out.jsonl"
    monkeypatch.setattr(sys, "argv", ["sanitize", str(source), "-o", str(output)])
    assert san.main() == 0
    meta = ingest.prepare(source, tmp_path / "prepared", trusted=True)
    assert meta["kind"] == "normalized" and meta["produced"]["sanitized"]
    for result in (row, read(output)[0], read(meta["files"]["sanitized_jsonl"])[0]):
        assert "identity_scope_id" not in result
        assert result["input_tokens_total"] == 100
        for field in ("session_id", "user", "project"):
            assert result[field] != raw[field]
            assert raw[field] not in json.dumps(result)


def test_explicit_passthrough_still_rejects_private_fields(tmp_path, monkeypatch):
    row = private_row()
    with pytest.raises(ValueError, match="already-sanitized"):
        san.sanitize_row(row, san.StableIdSanitizer("same", already_sanitized=True))
    source = write(tmp_path / "input.jsonl", [row])
    output = tmp_path / "out.jsonl"
    output.write_text("keep me")
    monkeypatch.setattr(sys, "argv", ["sanitize", str(source), "-o", str(output), "--already-sanitized"])
    with pytest.raises(ValueError, match="already-sanitized"):
        san.main()
    assert output.read_text() == "keep me"
    with pytest.raises(ingest.IngestError, match="already-sanitized"):
        ingest.prepare(source, tmp_path / "prepared", already_sanitized=True)


def test_explicit_passthrough_requires_normalized_shape():
    with pytest.raises(ValueError, match="normalized rounds"):
        san.sanitize_row({"provider": "claude"}, san.StableIdSanitizer("same", already_sanitized=True))


@pytest.mark.parametrize("second_scope", [None, "different"])
@pytest.mark.parametrize("reverse", [False, True])
def test_scope_conflicts_before_dedup_and_output(tmp_path, monkeypatch, second_scope, reverse):
    first = san.sanitize_row(private_row(), san.StableIdSanitizer("same"))
    second = dict(first)
    if second_scope is None:
        second.pop("identity_scope_id")
    else:
        second["identity_scope_id"] = str(uuid4())
    rows = [second, first] if reverse else [first, second]
    source = write(tmp_path / "input.jsonl", rows)
    output = tmp_path / "output.jsonl"
    output.write_text("keep me")
    with pytest.raises(ValueError, match="identity_scope_id"):
        write_rounds_jsonl(output, rows, append_dedup=False)
    assert output.read_text() == "keep me"
    with pytest.raises(ValueError, match="identity_scope_id"):
        merge_round_traces([source], output)
    assert output.read_text() == "keep me"
    monkeypatch.setattr(sys, "argv", ["sanitize", str(source), "-o", str(output)])
    with pytest.raises(ValueError, match="identity_scope_id"):
        san.main()
    assert output.read_text() == "keep me"
    work = tmp_path / "prepared"
    work.mkdir()
    for name in ("normalized.jsonl", "sanitized.jsonl"):
        (work / name).write_text("keep me")
    with pytest.raises(ingest.IngestError, match="identity_scope_id"):
        ingest.prepare(source, work)
    assert (work / "normalized.jsonl").read_text() == "keep me"
    assert (work / "sanitized.jsonl").read_text() == "keep me"
    ids = san.StableIdSanitizer("same")
    san.sanitize_row(rows[0], ids)
    with pytest.raises(ValueError, match="identity_scope_id"):
        san.sanitize_row(rows[1], ids)


@pytest.mark.parametrize("field,value", [
    ("identity_scope_id", ""), ("identity_scope_id", "private-name"),
    ("identity_scope_id", None), ("identity_scope_id", 12), ("identity_scope_id", {}),
    ("session_origin", "/private/name"), ("session_origin", None), ("session_origin", []),
])
def test_invalid_markers_fail_safe(tmp_path, field, value):
    row = private_row(**{field: value})
    output = tmp_path / "output.jsonl"
    output.write_text("keep me")
    with pytest.raises(ValueError, match=field):
        write_rounds_jsonl(output, [row], append_dedup=False)
    with pytest.raises(ValueError, match=field):
        san.sanitize_row(row, san.StableIdSanitizer("same"))
    source = write(tmp_path / "source.jsonl", [row])
    with pytest.raises(ingest.IngestError, match=field):
        ingest.prepare(source, tmp_path / "prepared")
    assert output.read_text() == "keep me"


def test_mixed_private_and_legacy_cannot_be_certified(tmp_path):
    raw = private_row()
    legacy = {k: v for k, v in raw.items() if k != "session_file"}
    # Arbitrary raw-looking IDs alone are not evidence of a controlled private batch.
    assert "identity_scope_id" not in san.sanitize_row(legacy, san.StableIdSanitizer("s"))
    source = write(tmp_path / "source.jsonl", [raw, legacy])
    with pytest.raises(ingest.IngestError, match="already-sanitized"):
        ingest.prepare(source, tmp_path / "prepared")


def test_append_checks_existing_scope_and_same_scope_merge(tmp_path, monkeypatch):
    row = san.sanitize_row(private_row(), san.StableIdSanitizer("s"))
    output = write(tmp_path / "output.jsonl", [row])
    before = output.read_bytes()
    with pytest.raises(ValueError, match="identity_scope_id"):
        write_rounds_jsonl(output, [{**row, "identity_scope_id": str(uuid4())}], append_dedup=True)
    assert output.read_bytes() == before
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    merged = tmp_path / "merged.jsonl"
    assert merge_round_traces([output, output], merged)["unique_rounds"] == 1
    assert read(merged) == [row]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("case", ["scoped_destination", "scoped_candidate", "private"])
def test_collector_append_validates_before_dedup(tmp_path, monkeypatch, provider, case):
    session_dir = tmp_path / "sessions"
    write(session_dir / "session.jsonl", [{}])
    row = private_row(f"{provider}:root", provider=provider)
    existing = dict(row)
    candidate = dict(row)
    if case == "scoped_destination":
        existing["identity_scope_id"] = str(uuid4())
    elif case == "scoped_candidate":
        candidate["identity_scope_id"] = str(uuid4())
    output = write(tmp_path / "output.jsonl", [existing])
    before = output.read_bytes()
    calls = []

    def extract(*args, **kwargs):
        calls.append(args)
        # A valid new row must not be written before the invalid duplicate is checked.
        return [{**row, "round_id": "new"}, candidate]

    source = {"home": "/home/test", "user": "test", "store": ".claude"}
    if provider == "claude":
        source["project_dir"] = session_dir
        monkeypatch.setattr(collector, "extract_session_with_key", extract)
        append = collector.append_claude_round_trace
        kwargs = {"project_filters": []}
    else:
        source["sessions_dir"] = session_dir
        monkeypatch.setattr(collector, "extract_codex_session", extract)
        append = collector.append_codex_round_trace
        kwargs = {}
    kwargs.update(output_path=output, sources=[source], progress_enabled=False)
    if case == "private":
        stats = append(**kwargs)
        assert stats["written_rounds"] == stats["skipped_duplicate_rounds"] == 1
        assert len(read(output)) == 2
    else:
        with pytest.raises(ValueError, match="identity_scope_id"):
            append(**kwargs)
        assert output.read_bytes() == before
        assert bool(calls) is (case != "scoped_destination")


@pytest.mark.parametrize("sub", ["review", "compact", "memory_consolidation", {"other": "custom"}])
def test_codex_parentless_child_does_not_activate_replay_routing(tmp_path, sub):
    path = write(tmp_path / "codex.jsonl", [
        {"type": "session_meta", "payload": {"id": "200", "source": {"subagent": sub}}},
        {"type": "session_meta", "payload": {"id": "100"}},
        *codex_turn("150", 1), *codex_turn("250", 2),
    ])
    rows = extract_codex_session(path)
    assert [(r["session_id"], r["round_id"], r["round_index"]) for r in rows] == [
        ("codex:200", "150:0", 0), ("codex:200", "250:0", 1),
    ]
    assert all(r["session_origin"] == "codex_subagent_source" for r in rows)
    assert all(r["parent_session_id"] is None for r in rows)


@pytest.mark.parametrize("sources,expected", [
    (["cli", {"subagent": "review"}], "unknown"),
    (["cli", None], "codex_top_level_source"),
    ([{"subagent": "review"}, None], "codex_subagent_source"),
])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("interleaved", [False, True])
def test_codex_reconciles_all_origin_evidence(tmp_path, sources, expected, reverse, interleaved):
    sources = list(reversed(sources)) if reverse else sources
    records = []
    for index, source in enumerate(sources):
        records.append({"type": "session_meta", "payload": {"id": "200", "source": source}})
        if interleaved:
            records.extend(codex_turn(str(250 + index), index + 1))
    if not interleaved:
        records.extend(codex_turn("250", 1))
    rows = extract_codex_session(write(tmp_path / "codex.jsonl", records))
    assert all(r["session_id"] == "codex:200" for r in rows)
    assert {r["session_origin"] for r in rows} == {expected}
