from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from trace_db import connect, materialize  # noqa: E402

SCOPE = "10000000-0000-4000-8000-000000000001"


def _row(sid, *, provider="claude", origin: str | None = "unknown", parent=None, root=None, **extra):
    def qualified(value):
        return f"{provider}:{value}" if isinstance(value, str) and value and ":" not in value else value

    return {
        "provider": provider,
        "session_id": qualified(sid),
        "identity_scope_id": SCOPE,
        "session_origin": origin,
        "parent_session_id": qualified(parent),
        "root_session_id": qualified(root),
        **extra,
    }


@pytest.fixture
def db_for(tmp_path):
    connections = []

    def build(rows):
        trace = tmp_path / f"trace-{len(connections)}.jsonl"
        trace.write_text("".join(json.dumps(row) + "\n" for row in rows))
        db = trace.with_suffix(".duckdb")
        materialize(trace, db)
        con = connect(db)
        connections.append(con)
        return con

    yield build
    for con in connections:
        con.close()


def _sessions(con):
    result = con.execute("SELECT * FROM sessions")
    columns = [column[0] for column in result.description]
    return {row[2]: dict(zip(columns, row)) for row in result.fetchall()}


def _link(session):
    return tuple(session[name] for name in (
        "parent_status", "root_status", "resolved_root_session_id", "depth",
    ))


@pytest.mark.parametrize("provider,top,child", [
    ("claude", "claude_top_level_layout", "claude_subagent_layout"),
    ("codex", "codex_top_level_source", "codex_subagent_source"),
])
def test_known_chain_and_unrelated_unknown(db_for, provider, top, child):
    con = db_for([
        _row("grandchild", provider=provider, origin=child, parent="child"),
        _row("unknown", provider=provider),
        _row("child", provider=provider, origin=child, parent="root"),
        _row("root", provider=provider, origin=top),
    ])
    sessions = _sessions(con)
    root = f"{provider}:root"
    assert _link(sessions[root]) == ("unknown", "resolved_explicit", root, 0)
    assert _link(sessions[f"{provider}:child"]) == ("resolved", "resolved_chain", root, 1)
    assert _link(sessions[f"{provider}:grandchild"]) == ("resolved", "resolved_chain", root, 2)
    assert _link(sessions[f"{provider}:unknown"]) == ("unknown", "unknown", None, None)


@pytest.mark.parametrize("origin", ["unknown", "claude_top_level_layout"])
def test_root_only_and_nested_membership_does_not_invent_depth_or_origin(db_for, origin):
    sessions = _sessions(db_for([
        _row("nested", parent="child", origin="claude_subagent_layout"),
        _row("child", root="root", origin="claude_subagent_layout"),
        _row("root", origin=origin),
        _row("plain-child", parent="root"),
    ]))
    assert _link(sessions["claude:child"]) == ("unknown", "resolved_explicit", "claude:root", None)
    assert _link(sessions["claude:nested"]) == ("resolved", "resolved_chain", "claude:root", None)
    assert sessions["claude:root"]["session_origin"] == origin
    if origin == "unknown":
        assert _link(sessions["claude:root"]) == ("unknown", "unknown", None, None)
        assert _link(sessions["claude:plain-child"]) == ("resolved", "unknown", None, None)


def test_missing_targets_and_explicit_root_with_missing_parent(db_for):
    sessions = _sessions(db_for([
        _row("root", origin="claude_top_level_layout"),
        _row("missing-parent", parent="absent-parent"),
        _row("missing-root", root="absent-root"),
        _row("member", parent="absent-parent", root="root"),
        _row("nested", parent="member"),
        _row("missing-descendant", parent="missing-root"),
    ]))
    assert len(sessions) == 6
    assert _link(sessions["claude:missing-parent"]) == ("missing", "missing", None, None)
    assert _link(sessions["claude:missing-root"]) == ("unknown", "missing", None, None)
    assert _link(sessions["claude:member"]) == ("missing", "resolved_explicit", "claude:root", None)
    assert _link(sessions["claude:nested"]) == ("resolved", "resolved_chain", "claude:root", None)
    assert _link(sessions["claude:missing-descendant"]) == ("resolved", "missing", None, None)


def test_cycles_and_downstream_explicit_root_cannot_override_cycle(db_for):
    sessions = _sessions(db_for([
        _row("root", origin="claude_top_level_layout"),
        _row("descendant", parent="a", root="root"),
        _row("a", parent="b", root="root"),
        _row("b", parent="c"),
        _row("c", parent="a"),
    ]))
    for sid in ("a", "b", "c"):
        assert _link(sessions[f"claude:{sid}"]) == ("cycle", "cycle", None, None)
    assert _link(sessions["claude:descendant"]) == ("resolved", "cycle", None, None)


def test_long_chain_is_iterative(db_for):
    rows = [_row(str(i), parent=str(i - 1)) for i in range(1200, 0, -1)]
    rows.append(_row("0", origin="claude_top_level_layout"))
    sessions = _sessions(db_for(rows))
    assert _link(sessions["claude:1200"]) == ("resolved", "resolved_chain", "claude:0", 1200)


@pytest.mark.parametrize("changes,parent_status,root_status", [
    ({"parent": "codex:root"}, "invalid", "invalid"),
    ({"root": "codex:root"}, "unknown", "invalid"),
    ({"parent": "child"}, "invalid", "invalid"),
    ({"root": "child", "origin": "claude_subagent_layout"}, "unknown", "invalid"),
    ({"root": "child", "parent": "root"}, "resolved", "invalid"),
    ({"parent": 42}, "invalid", "invalid"),
    ({"root": ["root"]}, "unknown", "invalid"),
    ({"origin": "custom_origin"}, "invalid", "invalid"),
    ({"origin": "codex_top_level_source"}, "invalid", "invalid"),
    ({"origin": "claude_top_level_layout", "parent": "root"}, "conflict", "conflict"),
    ({"origin": "claude_top_level_layout", "root": "root"}, "unknown", "conflict"),
])
def test_invalid_links_and_top_level_contradictions(db_for, changes, parent_status, root_status):
    sessions = _sessions(db_for([
        _row("child", **changes),
        _row("descendant", parent="child", root="root"),
        _row("root", origin="claude_top_level_layout"),
        _row("root", provider="codex", origin="codex_top_level_source"),
    ]))
    assert _link(sessions["claude:child"]) == (parent_status, root_status, None, None)
    assert _link(sessions["claude:descendant"]) == ("resolved", root_status, None, None)


@pytest.mark.parametrize("target,status", [
    ({"origin": "claude_subagent_layout"}, "invalid"),
    ({"parent": "absent"}, "invalid"),
    ({"origin": "claude_top_level_layout", "parent": "absent"}, "conflict"),
])
def test_explicit_root_target_cannot_be_a_child(db_for, target, status):
    sessions = _sessions(db_for([_row("member", root="target"), _row("target", **target)]))
    assert _link(sessions["claude:member"]) == ("unknown", status, None, None)


def test_unknown_self_root_is_not_positive_root_evidence(db_for):
    sessions = _sessions(db_for([
        _row("unknown", root="unknown"),
        _row("member", root="unknown"),
        _row("child", root="child", parent="absent"),
        _row("top", root="top", origin="claude_top_level_layout"),
    ]))
    assert _link(sessions["claude:unknown"]) == ("unknown", "unknown", None, None)
    assert _link(sessions["claude:member"]) == ("unknown", "resolved_explicit", "claude:unknown", None)
    assert _link(sessions["claude:child"]) == ("missing", "invalid", None, None)
    assert _link(sessions["claude:top"]) == ("unknown", "resolved_explicit", "claude:top", 0)


@pytest.mark.parametrize("reverse", [False, True])
def test_cyclic_explicit_root_target_is_never_resolved(db_for, reverse):
    rows = [_row("member", root="a"), _row("a", parent="b"), _row("b", parent="a")]
    sessions = _sessions(db_for(rows[::-1] if reverse else rows))
    assert _link(sessions["claude:member"]) == ("unknown", "cycle", None, None)


@pytest.mark.parametrize("reverse", [False, True])
def test_contradictory_root_target_is_order_independent(db_for, reverse):
    rows = [
        _row("member", root="target"), _row("target", root="child"),
        _row("child", origin="claude_subagent_layout"),
    ]
    sessions = _sessions(db_for(rows[::-1] if reverse else rows))
    assert _link(sessions["claude:member"]) == ("unknown", "conflict", None, None)
    assert _link(sessions["claude:target"]) == ("unknown", "invalid", None, None)


@pytest.mark.parametrize("field,values,canonical,parent_status", [
    ("parent_session_id", ["claude:root", "claude:other"], None, "conflict"),
    ("root_session_id", ["claude:root", "claude:other"], None, "unknown"),
    ("session_origin", ["claude_subagent_layout", "claude_top_level_layout"], "unknown", "conflict"),
])
def test_conflicting_round_evidence_is_retained(db_for, field, values, canonical, parent_status):
    con = db_for([
        _row("child", **{field: values[0]}), _row("child", **{field: values[1]}),
        _row("descendant", parent="child", root="root"),
        _row("root", origin="claude_top_level_layout"),
        _row("other", origin="claude_top_level_layout"),
    ])
    sessions = _sessions(con)
    column = field if field == "session_origin" else "observed_" + field
    assert sessions["claude:child"][column] == canonical
    assert _link(sessions["claude:child"]) == (parent_status, "conflict", None, None)
    assert _link(sessions["claude:descendant"]) == ("resolved", "conflict", None, None)
    assert [r[0] for r in con.execute(
        f"SELECT {field} FROM rounds WHERE session_id = 'claude:child' ORDER BY round_pk"
    ).fetchall()] == values


def test_null_and_nonnull_evidence_and_repeated_rounds_keep_keys_and_joins(db_for):
    rows = [
        _row("child", origin=None),
        _row("root", origin="claude_top_level_layout"),
        _row("child", parent="root", root="root", origin="claude_subagent_layout"),
        _row("child", parent="root", root="root", origin="claude_subagent_layout"),
    ]
    for index, row in enumerate(rows):
        row.update(round_id="duplicate", trace_key="duplicate", round_index=1,
                   tools=[{"tool_call_id": "duplicate", "tool_index": index}],
                   timing_events=[{"event_type": "message", "content_chars": index},
                                  {"event_type": "tool", "tool_index": index}])
    con = db_for(rows)
    sessions = _sessions(con)
    assert len(sessions) == 2
    child = sessions["claude:child"]
    assert child["session_origin"] == "claude_subagent_layout"
    assert child["observed_parent_session_id"] == child["observed_root_session_id"] == "claude:root"
    assert _link(child) == ("resolved", "resolved_explicit", "claude:root", 1)
    assert con.execute("SELECT round_pk, ingest_seq, session_id FROM rounds ORDER BY round_pk").fetchall() == [
        (i + 1, i + 1, row["session_id"]) for i, row in enumerate(rows)
    ]
    assert con.execute("SELECT count(*) FROM tool_calls JOIN rounds USING (round_pk)").fetchone() == (4,)
    assert con.execute("SELECT count(*) FROM timing_events JOIN rounds USING (round_pk)").fetchone() == (8,)
    assert con.execute("SELECT round_pk, tool_index FROM tool_calls ORDER BY round_pk").fetchall() == [
        (i + 1, i) for i in range(4)
    ]
    assert con.execute("SELECT round_pk, event_index FROM timing_events ORDER BY 1, 2").fetchall() == [
        (i + 1, event) for i in range(4) for event in (1, 2)
    ]


@pytest.mark.parametrize("parent_root", ["other", "absent"])
def test_explicit_root_disagrees_with_ancestor_claim(db_for, parent_root):
    sessions = _sessions(db_for([
        _row("root", origin="claude_top_level_layout"),
        _row("other", origin="claude_top_level_layout"),
        _row("ancestor", root=parent_root),
        _row("child", parent="ancestor", root="root"),
        _row("descendant", parent="child"),
    ]))
    assert _link(sessions["claude:child"]) == ("resolved", "conflict", None, None)
    assert _link(sessions["claude:descendant"]) == ("resolved", "conflict", None, None)


def test_missing_origin_scope_and_columns(db_for):
    con = db_for([{"provider": "claude", "session_id": "claude:legacy"}])
    session = _sessions(con)["claude:legacy"]
    assert session["session_origin"] == "unknown"
    assert session["identity_scope_id"] is None
    assert _link(session) == ("unavailable", "unavailable", None, None)
    assert con.execute("SELECT identity_scope_id, session_origin, parent_session_id, root_session_id FROM rounds").fetchone() == (None,) * 4
    assert not _sessions(db_for([{"round_id": "minimal"}]))
    assert not _sessions(db_for([
        _row(None), _row(""), _row("codex:wrong"), _row("x", provider="other"),
        _row(" "), _row("claude:"), _row(42),
    ]))


@pytest.mark.parametrize("missing", [False, True])
def test_unscoped_rows_never_resolve_even_positive_top_level(db_for, missing):
    rows = [_row("root", origin="claude_top_level_layout"), _row("child", parent="root")]
    for row in rows:
        if missing:
            row.pop("identity_scope_id")
        else:
            row["identity_scope_id"] = None
    sessions = _sessions(db_for(rows))
    for session in sessions.values():
        assert _link(session) == ("unavailable", "unavailable", None, None)


@pytest.mark.parametrize("scopes", [
    [SCOPE, "10000000-0000-4000-8000-000000000002"], [SCOPE, None], [SCOPE, ""], [""], ["  "], [42],
    [SCOPE, 42], [{"scope": "one"}], [True], [["one"]],
    ["42", 42], ["true", True],
    ["not-a-uuid"], ["A0000000-0000-4000-8000-000000000001"],
    ["2026-01-01T00:00:00.000Z", "2026-01-01T00:00:00.000000Z"],
])
def test_bad_scopes_preserve_existing_database_before_replacement(tmp_path, scopes):
    trace = tmp_path / "bad.jsonl"
    trace.write_text("".join(json.dumps(_row(str(i), identity_scope_id=scope)) + "\n" for i, scope in enumerate(scopes)))
    db = tmp_path / "sentinel.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE sentinel AS SELECT 123 AS value")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="identity_scope_id"):
        materialize(trace, db)
    assert db.read_bytes() == before
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT value FROM sentinel").fetchone() == (123,)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bad.jsonl", "sentinel.duckdb"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("contradictory_incoming", [False, True])
def test_incoming_explicit_root_claim_constrains_descendants(db_for, reverse, contradictory_incoming):
    rows = [
        _row("r", origin="claude_top_level_layout"), _row("a"),
        _row("b", origin="claude_subagent_layout", parent="a", root="r"),
        _row("c", origin="claude_subagent_layout", root="a", parent="r" if contradictory_incoming else None),
        _row("descendant", parent="b"),
        _row("plain-child", parent="a"),
    ]
    sessions = _sessions(db_for(rows[::-1] if reverse else rows))
    if contradictory_incoming:
        assert _link(sessions["claude:b"]) == ("resolved", "resolved_explicit", "claude:r", None)
        assert _link(sessions["claude:descendant"]) == ("resolved", "resolved_chain", "claude:r", None)
        assert _link(sessions["claude:c"]) == ("resolved", "conflict", None, None)
    else:
        assert _link(sessions["claude:b"]) == ("resolved", "conflict", None, None)
        assert _link(sessions["claude:descendant"]) == ("resolved", "conflict", None, None)
        assert _link(sessions["claude:c"]) == ("unknown", "resolved_explicit", "claude:a", None)
    assert _link(sessions["claude:a"]) == ("unknown", "unknown", None, None)
    assert sessions["claude:a"]["session_origin"] == "unknown"
    assert _link(sessions["claude:plain-child"]) == ("resolved", "unknown", None, None)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("origins", [
    ["codex_top_level_source"], ["claude_top_level_layout", 17],
    ["claude_top_level_layout", "claude_subagent_layout"],
])
def test_bad_origin_evidence_always_has_unknown_canonical_origin(db_for, scoped, origins):
    rows = [_row("session", session_origin=origin) for origin in origins]
    if not scoped:
        for row in rows:
            row.pop("identity_scope_id")
    session = _sessions(db_for(rows))["claude:session"]
    assert session["session_origin"] == "unknown"
    assert session["resolved_root_session_id"] is None
    assert session["depth"] is None


@pytest.mark.parametrize("alias", [False, True])
def test_materialize_rejects_same_source_and_destination(tmp_path, alias):
    trace = tmp_path / "source.jsonl"
    trace.write_text(json.dumps(_row("root")) + "\n")
    before = trace.read_bytes()
    destination = trace
    if alias:
        destination = tmp_path / "alias.duckdb"
        destination.symlink_to(trace)
    with pytest.raises(ValueError, match="same"):
        materialize(trace, destination)
    assert trace.read_bytes() == before
    assert destination.read_bytes() == before
    if alias:
        assert destination.is_symlink()


@pytest.mark.parametrize("identity_columns", [False, True])
def test_readonly_legacy_compatibility_is_temporary_and_typed(tmp_path, identity_columns):
    db = tmp_path / "old.duckdb"
    with duckdb.connect(str(db)) as con:
        if identity_columns:
            con.execute("CREATE TABLE rounds AS SELECT 'claude' AS provider, 'claude:legacy' AS session_id FROM range(2)")
        else:
            con.execute("CREATE TABLE rounds AS SELECT 1 AS round_pk")
        con.execute("CREATE TABLE tool_calls AS SELECT 1 AS round_pk, 'Read' AS tool_name")
        con.execute("CREATE TABLE timing_events AS SELECT 1 AS round_pk")
    before, mtime = db.read_bytes(), db.stat().st_mtime_ns
    con = connect(db)
    try:
        sessions = _sessions(con)
        assert len(sessions) == int(identity_columns)
        if identity_columns:
            assert _link(sessions["claude:legacy"]) == ("unavailable", "unavailable", None, None)
            assert sessions["claude:legacy"]["session_origin"] == "unknown"
        types = {row[0]: row[1] for row in con.execute("DESCRIBE sessions").fetchall()}
        assert types["depth"] == "BIGINT"
        assert all(kind == "VARCHAR" for name, kind in types.items() if name != "depth")
        assert con.execute("SELECT count(*) FROM rounds").fetchone() == (2 if identity_columns else 1,)
        assert con.execute("SELECT event_index FROM timing_events").fetchone() == (1,)
    finally:
        con.close()
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime
    with duckdb.connect(str(db), read_only=True) as con:
        assert con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'sessions'").fetchone() == (0,)
