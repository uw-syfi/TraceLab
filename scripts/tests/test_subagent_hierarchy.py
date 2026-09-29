from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
for _sub in ("scripts", "artifacts/utils", "artifacts/web_analytics", "web/payload"):
    sys.path.insert(0, str(REPO_ROOT / _sub))

from extract_claude_rounds import claude_hierarchy_from_key, extract_session_with_key, round_key  # noqa: E402
from extract_codex_rounds import extract_codex_session  # noqa: E402
from sanitize_round_trace import StableIdSanitizer, sanitize_row  # noqa: E402
from trace_privacy import find_sensitive  # noqa: E402
from trace_db import connect, materialize  # noqa: E402
from rounds import load_rounds  # noqa: E402
from sessions import build_session_detail, build_sessions  # noqa: E402


HIERARCHY_FIELDS = ("thread_kind", "parent_session_id", "workflow_id", "agent_role")

PARENT = "11111111-1111-4111-8111-111111111111"
AGENT = "a30192fffacf16baf"
AGENT_B = "b40192fffacf16bae"
AGENT_C = "c50192fffacf16bad"


# --------------------------------------------------------------------------- fixtures


def _claude_assistant(msg_id: str, *, sidechain: bool, session: str, agent: str | None = None,
                      role: str | None = None) -> dict:
    record = {
        "type": "assistant",
        "timestamp": "2026-01-01T00:00:01.000Z",
        "sessionId": session,
        "isSidechain": sidechain,
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "claude-test",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                      "cache_read_input_tokens": 100, "output_tokens": 3},
        },
    }
    if agent is not None:
        record["agentId"] = agent
    if role is not None:
        record["attributionAgent"] = role
    return record


def _claude_user(*, sidechain: bool, session: str, agent: str | None = None) -> dict:
    record = {"type": "user", "timestamp": "2026-01-01T00:00:00.000Z", "sessionId": session,
              "isSidechain": sidechain, "message": {"role": "user", "content": "do the task"}}
    if agent is not None:
        record["agentId"] = agent
    return record


def _write(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _child_records(msg_id: str = "msg_02child", agent: str = AGENT, role: str = "workflow-subagent") -> list[dict]:
    return [
        _claude_user(sidechain=True, session=PARENT, agent=agent),
        _claude_assistant(msg_id, sidechain=True, session=PARENT, agent=agent, role=role),
    ]


def _stamp(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["trace_key"] = round_key(r)  # stamped by write_rounds_jsonl in the real pipeline
    return rows


def _db(tmp_path: Path, rows: list[dict], name: str = "trace"):
    trace = tmp_path / f"{name}.jsonl"
    trace.write_text("".join(json.dumps(r) + "\n" for r in rows))
    db = tmp_path / f"{name}.duckdb"
    materialize(trace, db)
    return connect(db, read_only=True)


def _tree_rounds(tmp_path: Path) -> list[dict]:
    """One main session with a workflow of two agents and one directly launched agent."""
    project = tmp_path / "projects" / "demo"
    main = _write(project / f"{PARENT}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
        _claude_assistant("msg_02main", sidechain=False, session=PARENT),
    ])
    out = extract_session_with_key(main, "demo", PARENT)
    for agent, sub, msg in ((AGENT, "workflows/wf_one", "msg_03"), (AGENT_B, "workflows/wf_one", "msg_04"),
                            (AGENT_C, "", "msg_05")):
        path = project / PARENT / "subagents" / (f"{sub}/" if sub else "") / f"agent-{agent}.jsonl"
        _write(path, _child_records(msg, agent, "workflow-subagent" if sub else "Explore"))
        out += extract_session_with_key(path, "demo", path.relative_to(project).with_suffix("").as_posix())
    return _stamp(out)


# --------------------------------------------------------------------------- Claude extraction


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (PARENT, {"thread_kind": "main", "parent_session_id": None, "workflow_id": None}),
        (f"{PARENT}/subagents/agent-{AGENT}",
         {"thread_kind": "subagent", "parent_session_id": f"claude:{PARENT}", "workflow_id": None}),
        (f"{PARENT}/subagents/workflows/wf_22bfd098-533/agent-{AGENT}",
         {"thread_kind": "subagent", "parent_session_id": f"claude:{PARENT}", "workflow_id": "wf_22bfd098-533"}),
        (f"{PARENT}\\subagents\\workflows\\wf_22bfd098-533\\agent-{AGENT}",
         {"thread_kind": "subagent", "parent_session_id": f"claude:{PARENT}", "workflow_id": "wf_22bfd098-533"}),
    ],
)
def test_claude_hierarchy_follows_from_the_session_path(key: str, expected: dict) -> None:
    assert claude_hierarchy_from_key(key) == expected


def test_claude_child_file_links_to_the_main_session(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    main = _write(project / f"{PARENT}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
    ])
    child = _write(project / PARENT / "subagents" / "workflows" / "wf_x" / f"agent-{AGENT}.jsonl", _child_records())
    (m,) = extract_session_with_key(main, "demo", PARENT)
    (c,) = extract_session_with_key(child, "demo", child.relative_to(project).with_suffix("").as_posix())
    assert (m["thread_kind"], m["parent_session_id"]) == ("main", None)
    assert c["parent_session_id"] == m["session_id"]
    assert (c["thread_kind"], c["workflow_id"], c["agent_role"]) == ("subagent", "wf_x", "workflow-subagent")
    for dropped in ("is_subagent", "agent_id", "agent_depth"):
        assert dropped not in c


def test_claude_child_read_without_its_directory_recovers_the_parent(tmp_path: Path) -> None:
    child = _write(tmp_path / f"agent-{AGENT}.jsonl", _child_records())
    (c,) = extract_session_with_key(child, "upload", child.stem)
    assert (c["thread_kind"], c["parent_session_id"]) == ("subagent", f"claude:{PARENT}")


def _inline_session(tmp_path: Path) -> Path:
    return _write(tmp_path / f"{PARENT}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
        _claude_assistant("msg_02side", sidechain=True, session=PARENT, role="general-purpose"),
    ])


def test_claude_inline_sidechain_is_tagged_at_round_level(tmp_path: Path) -> None:
    rounds = {r["round_id"]: r for r in extract_session_with_key(_inline_session(tmp_path), "demo", PARENT)}
    assert rounds["msg_01main"]["thread_kind"] == "main"
    side = rounds["msg_02side"]
    assert side["thread_kind"] == "subagent"
    assert side["parent_session_id"] == side["session_id"] == f"claude:{PARENT}"


def test_inline_sidechains_keep_their_own_roles(tmp_path: Path) -> None:
    path = _write(tmp_path / f"{PARENT}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
        _claude_assistant("msg_02gp", sidechain=True, session=PARENT, role="general-purpose"),
        _claude_assistant("msg_03main", sidechain=False, session=PARENT),
        _claude_assistant("msg_04explore", sidechain=True, session=PARENT, role="Explore"),
    ])
    roles = {r["round_id"]: r["agent_role"] for r in extract_session_with_key(path, "demo", PARENT)}
    assert roles == {"msg_01main": None, "msg_02gp": "general-purpose", "msg_03main": None,
                     "msg_04explore": "Explore"}


def test_inline_sidechain_is_not_a_child_session_end_to_end(tmp_path: Path) -> None:
    """Extractor -> sanitizer -> DuckDB -> sessions/topology: an inline sidechain must not make its
    session its own parent or child."""
    rows = _stamp(extract_session_with_key(_inline_session(tmp_path), "demo", PARENT))
    ids = StableIdSanitizer("seed-a")
    con = _db(tmp_path, [sanitize_row(r, ids) for r in rows])
    (s,) = build_sessions(con, load_rounds(con))
    assert (s["threadKind"], s["childSessions"]) == ("main", 0)
    topo = build_session_detail(con, s["sessionId"])["topology"]
    assert topo["parentSessionId"] is None and topo["children"] == []


def test_session_keys_are_posix_at_every_entry_point(tmp_path: Path) -> None:
    from ingest import _claude_project_and_key
    import extract_claude_rounds as ecr

    project = tmp_path / "projects" / "demo"
    child = _write(project / PARENT / "subagents" / "workflows" / "wf_x" / f"agent-{AGENT}.jsonl", _child_records())
    _write(project / f"{PARENT}.jsonl", [_claude_user(sidechain=False, session=PARENT),
                                          _claude_assistant("msg_01main", sidechain=False, session=PARENT)])
    # browser / sidecar ingest
    _project, key = _claude_project_and_key(child)
    assert "\\" not in key and key.endswith(f"subagents/workflows/wf_x/agent-{AGENT}")
    # direct CLI
    out = tmp_path / "cli.jsonl"
    argv = sys.argv
    sys.argv = ["extract_claude_rounds.py", str(project), "-o", str(out)]
    try:
        ecr.main()
    finally:
        sys.argv = argv
    sids = {json.loads(line)["session_id"] for line in out.read_text().splitlines()}
    assert sids and all("\\" not in s for s in sids)
    # collector: both of its session-key expressions are POSIX
    import collect_llm_traces as clt
    source = Path(clt.__file__).read_text(encoding="utf-8")
    assert source.count('.with_suffix("").as_posix()') == 2
    assert 'str(path.relative_to(projects_dir).with_suffix(""))' not in source


# --------------------------------------------------------------------------- Codex extraction


def _record(timestamp: str, top_type: str, payload: dict) -> dict:
    return {"timestamp": timestamp, "type": top_type, "payload": payload}


def _token_count(timestamp: str, sequence: int) -> dict:
    usage = {"input_tokens": 100 + sequence, "cached_input_tokens": 80, "output_tokens": 10,
             "reasoning_output_tokens": 2, "total_tokens": 110 + sequence}
    return _record(timestamp, "event_msg", {
        "type": "token_count",
        "info": {"last_token_usage": usage, "total_token_usage": {**usage, "total_tokens": 1_000 + sequence}},
    })


CODEX_PARENT = "11111111-1111-7111-8111-111111111111"
CODEX_CHILD = "22222222-2222-7222-8222-222222222222"


def _codex_child_records() -> list[dict]:
    spawn = {"parent_thread_id": CODEX_PARENT, "depth": 1, "agent_path": "/root/private_task_name",
             "agent_nickname": "Private Nickname", "agent_role": None}
    return [
        _record("2026-01-01T00:00:00.000Z", "session_meta",
                {"id": CODEX_CHILD, "cwd": "/private/repo", "source": {"subagent": {"thread_spawn": spawn}}}),
        # The child file replays its parent's history first ...
        _record("2026-01-01T00:00:00.001Z", "session_meta", {"id": CODEX_PARENT, "cwd": "/private/repo"}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "11111111-1111-7111-8111-aaaaaaaaaaaa", "model": "codex-test"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "parent input"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
        # ... then runs its own turn.
        _record("2026-01-01T00:00:01.000Z", "turn_context",
                {"turn_id": "33333333-3333-7333-8333-333333333333", "model": "codex-test"}),
        _record("2026-01-01T00:00:01.010Z", "event_msg", {"type": "user_message", "message": "child input"}),
        _token_count("2026-01-01T00:00:01.100Z", 2),
    ]


def test_codex_live_child_turns_link_to_the_spawn_parent(tmp_path: Path) -> None:
    rounds = extract_codex_session(_write(tmp_path / "rollout.jsonl", _codex_child_records()))
    replayed = [r for r in rounds if r["session_id"] == f"codex:{CODEX_PARENT}"]
    live = [r for r in rounds if r["session_id"] == f"codex:{CODEX_CHILD}"]
    assert live, "the child's own turn must be extracted"
    assert replayed, "the replayed parent turn must be extracted too"
    for r in live:
        # the spawn parent, not the replayed-parent id parent_session_meta_id is re-assigned to
        assert (r["thread_kind"], r["parent_session_id"]) == ("subagent", f"codex:{CODEX_PARENT}")
    for r in replayed:
        assert (r["thread_kind"], r["parent_session_id"]) == ("main", None)
    text = json.dumps(rounds)
    assert "private_task_name" not in text and "Private Nickname" not in text


def _codex_guardian_records(reviewed: str | None = CODEX_PARENT) -> list[dict]:
    meta = {"id": CODEX_CHILD, "source": {"subagent": {"other": "guardian"}}}
    if reviewed:
        meta["parent_thread_id"] = reviewed  # the thread it reviewed
    return [
        _record("2026-01-01T00:00:00.000Z", "session_meta", meta),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "33333333-3333-7333-8333-333333333333", "model": "codex-auto-review"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "review"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ]


def test_codex_guardian_review_links_the_session_it_reviewed(tmp_path: Path) -> None:
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", _codex_guardian_records()))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == (
        "guardian", "guardian", f"codex:{CODEX_PARENT}"
    )
    assert r["session_id"] == f"codex:{CODEX_CHILD}"


def test_codex_guardian_review_without_a_recorded_parent_stays_unlinked(tmp_path: Path) -> None:
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", _codex_guardian_records(None)))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == ("guardian", "guardian", None)


def test_codex_guardian_review_is_recognized_from_thread_source_alone(tmp_path: Path) -> None:
    records = [
        _record("2026-01-01T00:00:00.000Z", "session_meta",
                {"id": CODEX_CHILD, "source": "cli", "thread_source": "guardian_review"}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "33333333-3333-7333-8333-333333333333", "model": "codex-auto-review"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "review"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ]
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", records))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == ("guardian", "guardian", None)


@pytest.mark.parametrize(
    ("source", "role"),
    [({"subagent": "review"}, "review"), ({"subagent": "compact"}, "compact"),
     ({"subagent": {"other": "some-new-kind"}}, "some-new-kind")],
)
def test_codex_other_sub_sessions_are_delegated_not_main(tmp_path: Path, source: dict, role: str) -> None:
    records = [
        _record("2026-01-01T00:00:00.000Z", "session_meta", {"id": CODEX_CHILD, "source": source}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "33333333-3333-7333-8333-333333333333", "model": "codex-test"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "go"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ]
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", records))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == ("subagent", role, None)
    clean = sanitize_row(r, StableIdSanitizer("seed-a"))
    # Codex's own sub-session kinds stay readable; an unknown name is pseudonymized like a custom role.
    assert clean["agent_role"] == (role if role in ("review", "compact") else "custom_role_1")


def test_codex_cli_session_is_main(tmp_path: Path) -> None:
    records = [
        _record("2026-01-01T00:00:00.000Z", "session_meta", {"id": CODEX_CHILD, "source": "cli"}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "33333333-3333-7333-8333-333333333333", "model": "codex-test"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "go"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ]
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", records))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == ("main", None, None)


# --------------------------------------------------------------------------- sanitization


def _pair(tmp_path: Path) -> tuple[dict, dict]:
    project = tmp_path / "projects" / "demo"
    main = _write(project / f"{PARENT}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
    ])
    child = _write(project / PARENT / "subagents" / "workflows" / "wf_22bfd098-533" / f"agent-{AGENT}.jsonl",
                   _child_records())
    (m,) = extract_session_with_key(main, "demo", PARENT)
    (c,) = extract_session_with_key(child, "demo", child.relative_to(project).with_suffix("").as_posix())
    return m, c


def test_parent_link_survives_sanitization(tmp_path: Path) -> None:
    m, c = _pair(tmp_path)
    ids = StableIdSanitizer("seed-a")
    sm, sc = sanitize_row(m, ids), sanitize_row(c, ids)
    assert sc["parent_session_id"] == sm["session_id"] != sc["session_id"]


def test_hierarchy_ids_do_not_link_across_seeds(tmp_path: Path) -> None:
    _, c = _pair(tmp_path)
    a = sanitize_row(c, StableIdSanitizer("seed-a"))
    b = sanitize_row(c, StableIdSanitizer("seed-b"))
    for field in ("parent_session_id", "workflow_id"):
        assert a[field] != b[field]


def test_no_raw_hierarchy_identifier_survives_sanitization(tmp_path: Path) -> None:
    m, c = _pair(tmp_path)
    ids = StableIdSanitizer("seed-a")
    rows = [sanitize_row(m, ids), sanitize_row(c, ids)]
    text = json.dumps(rows)
    for fragment in (PARENT, AGENT, "wf_22bfd098", "subagents", "demo"):
        assert fragment not in text, fragment
    assert all(find_sensitive(r) == [] for r in rows)
    assert all(field in rows[1] for field in HIERARCHY_FIELDS)


def test_agent_roles_keep_builtins_and_pseudonymize_custom_names() -> None:
    ids = StableIdSanitizer("seed-a")
    base = {"provider": "claude", "session_id": "claude:s", "round_id": "msg_01"}
    assert sanitize_row({**base, "agent_role": "Explore"}, ids)["agent_role"] == "Explore"
    assert sanitize_row({**base, "agent_role": "workflow-subagent"}, ids)["agent_role"] == "workflow-subagent"
    first = sanitize_row({**base, "agent_role": "my-secret-reviewer"}, ids)["agent_role"]
    again = sanitize_row({**base, "agent_role": "my-secret-reviewer"}, ids)["agent_role"]
    other = sanitize_row({**base, "agent_role": "another-agent"}, ids)["agent_role"]
    assert first == again == "custom_role_1" and other == "custom_role_2"
    assert sanitize_row({**base, "agent_role": None}, ids)["agent_role"] is None


def test_thread_kind_is_a_closed_vocabulary() -> None:
    ids = StableIdSanitizer("seed-a")
    base = {"provider": "claude", "session_id": "claude:s", "round_id": "msg_01"}
    for kind in ("main", "subagent", "guardian"):
        assert sanitize_row({**base, "thread_kind": kind}, ids)["thread_kind"] == kind
    assert sanitize_row({**base, "thread_kind": "private-client-project"}, ids)["thread_kind"] is None


def test_main_rounds_keep_null_hierarchy_after_sanitization(tmp_path: Path) -> None:
    m, _ = _pair(tmp_path)
    s = sanitize_row(m, StableIdSanitizer("seed-a"))
    assert tuple(s[f] for f in HIERARCHY_FIELDS) == ("main", None, None, None)


# --------------------------------------------------------------------------- analytics


def test_session_list_and_topology_expose_the_tree(tmp_path: Path) -> None:
    ids = StableIdSanitizer("seed-a")
    con = _db(tmp_path, [sanitize_row(r, ids) for r in _tree_rounds(tmp_path)])
    sessions = {s["sessionId"]: s for s in build_sessions(con, load_rounds(con))}
    main_id = next(s for s, v in sessions.items() if v["threadKind"] == "main")
    assert sessions[main_id]["childSessions"] == 3 and sessions[main_id]["agentRole"] is None
    for dropped in ("parentSessionId", "workflowId", "agentDepth"):
        assert dropped not in sessions[main_id]
    children = [v for v in sessions.values() if v["threadKind"] == "subagent"]
    assert len(children) == 3 and all(c["childSessions"] == 0 for c in children)
    assert all(build_session_detail(con, c["sessionId"])["topology"]["parentSessionId"] == main_id for c in children)
    assert {c["agentRole"] for c in children} == {"workflow-subagent", "Explore"}

    topo = build_session_detail(con, main_id)["topology"]
    assert topo["known"] is True and topo["threadKind"] == "main" and len(topo["children"]) == 3
    assert sorted(w["agents"] for w in topo["workflows"]) == [1, 2]  # wf_one (2) + direct (1)
    assert 0 < topo["children"][0]["prefixShare"] < 1

    child_topo = build_session_detail(con, children[0]["sessionId"])["topology"]
    assert child_topo["threadKind"] == "subagent" and child_topo["parentSessionId"] == main_id
    assert child_topo["children"] == [] and child_topo["reviews"] == []


def test_automatic_reviews_link_to_the_reviewed_session_but_are_not_children(tmp_path: Path) -> None:
    main_file = _write(tmp_path / "main.jsonl", [
        _record("2026-01-01T00:00:00.000Z", "session_meta", {"id": CODEX_PARENT, "source": "cli"}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "11111111-1111-7111-8111-aaaaaaaaaaaa", "model": "codex-test"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "work"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ])
    review_file = _write(tmp_path / "review.jsonl", _codex_guardian_records())
    rows = _stamp(extract_codex_session(main_file) + extract_codex_session(review_file))
    ids = StableIdSanitizer("seed-a")
    con = _db(tmp_path, [sanitize_row(r, ids) for r in rows], "reviews")
    sessions = {s["threadKind"]: s for s in build_sessions(con, load_rounds(con))}
    main, review = sessions["main"], sessions["guardian"]
    assert main["childSessions"] == 0  # a review is not delegated work
    main_topo = build_session_detail(con, main["sessionId"])["topology"]
    assert main_topo["children"] == [] and main_topo["workflows"] == []
    assert [r["sessionId"] for r in main_topo["reviews"]] == [review["sessionId"]]
    review_topo = build_session_detail(con, review["sessionId"])["topology"]
    assert (review_topo["threadKind"], review_topo["parentSessionId"]) == ("guardian", main["sessionId"])


def test_reviews_of_a_session_are_listed_in_start_order(tmp_path: Path) -> None:
    main_file = _write(tmp_path / "main.jsonl", [
        _record("2026-01-01T00:00:00.000Z", "session_meta", {"id": CODEX_PARENT, "source": "cli"}),
        _record("2026-01-01T00:00:00.002Z", "turn_context",
                {"turn_id": "11111111-1111-7111-8111-aaaaaaaaaaaa", "model": "codex-test"}),
        _record("2026-01-01T00:00:00.003Z", "event_msg", {"type": "user_message", "message": "work"}),
        _token_count("2026-01-01T00:00:00.100Z", 1),
    ])
    rows = extract_codex_session(main_file)
    starts = {"33333333-3333-7333-8333-000000000002": "2026-01-01T00:02:00",
              "33333333-3333-7333-8333-000000000001": "2026-01-01T00:01:00"}
    for review_id, start in starts.items():  # written in the opposite order of their start
        records = _codex_guardian_records()
        records[0]["payload"]["id"] = review_id
        for r in records:
            r["timestamp"] = r["timestamp"].replace("2026-01-01T00:00", start[:16])
        rows += extract_codex_session(_write(tmp_path / f"{review_id}.jsonl", records))
    ids = StableIdSanitizer("seed-a")
    con = _db(tmp_path, [sanitize_row(r, ids) for r in _stamp(rows)], "ordered")
    sessions = build_sessions(con, load_rounds(con))
    main = next(s for s in sessions if s["threadKind"] == "main")
    reviews = build_session_detail(con, main["sessionId"])["topology"]["reviews"]
    assert [r["firstTsUs"] for r in reviews] == sorted(r["firstTsUs"] for r in reviews) and len(reviews) == 2


def test_a_review_flag_on_another_sub_session_kind_does_not_link_a_reviewed_session(tmp_path: Path) -> None:
    records = _codex_guardian_records()
    meta = records[0]["payload"]
    meta["source"] = {"subagent": "review"}  # Codex's own review sub-session ...
    meta["thread_source"] = "guardian_review"  # ... that also carries the guardian marker
    (r,) = extract_codex_session(_write(tmp_path / "rollout.jsonl", records))
    assert (r["thread_kind"], r["agent_role"], r["parent_session_id"]) == ("subagent", "review", None)


def test_traces_without_hierarchy_columns_report_unknown_not_main(tmp_path: Path) -> None:
    rows = _tree_rounds(tmp_path)
    for r in rows:
        for field in HIERARCHY_FIELDS:
            r.pop(field, None)
    con = _db(tmp_path, rows, "old")
    sessions = build_sessions(con, load_rounds(con))
    assert sessions and all(s["threadKind"] is None and s["childSessions"] is None for s in sessions)
    topo = build_session_detail(con, sessions[0]["sessionId"])["topology"]
    assert topo["known"] is False and topo["children"] == [] and topo["reviews"] == []


@pytest.mark.parametrize(
    "keep",
    [("thread_kind",), ("thread_kind", "parent_session_id"), ("thread_kind", "agent_role"),
     ("thread_kind", "workflow_id", "agent_role"), ("parent_session_id",)],
)
def test_partial_hierarchy_schemas_degrade_instead_of_failing(tmp_path: Path, keep: tuple) -> None:
    rows = _tree_rounds(tmp_path)
    for r in rows:
        for field in HIERARCHY_FIELDS:
            if field not in keep:
                r.pop(field, None)
    con = _db(tmp_path, rows, "partial")
    sessions = build_sessions(con, load_rounds(con))
    for s in sessions:
        topo = build_session_detail(con, s["sessionId"])["topology"]  # must not raise
        assert isinstance(topo["children"], list)
    if "parent_session_id" in keep and "thread_kind" in keep:
        assert max(s["childSessions"] for s in sessions) == 3


def test_mixed_old_and_new_trace_keeps_old_sessions_unknown(tmp_path: Path) -> None:
    new = _tree_rounds(tmp_path)
    old_file = _write(tmp_path / "old" / "22222222-2222-4222-8222-222222222222.jsonl", [
        _claude_user(sidechain=False, session="22222222-2222-4222-8222-222222222222"),
        _claude_assistant("msg_99old", sidechain=False, session="22222222-2222-4222-8222-222222222222"),
    ])
    old = _stamp(extract_session_with_key(old_file, "old", old_file.stem))
    for r in old:
        for field in HIERARCHY_FIELDS:
            r.pop(field, None)
    con = _db(tmp_path, new + old, "mixed")
    sessions = {s["sessionId"]: s for s in build_sessions(con, load_rounds(con))}
    old_s = sessions[f"claude:{old_file.stem}"]
    assert old_s["threadKind"] is None
    assert old_s["childSessions"] is None  # unknown per session, not 0 because other sessions are tagged
    assert build_session_detail(con, old_s["sessionId"])["topology"]["known"] is False
    assert sum(1 for s in sessions.values() if s["threadKind"] == "main") == 1


def test_child_loaded_without_its_parent_names_a_parent_that_is_absent(tmp_path: Path) -> None:
    child = _write(tmp_path / f"agent-{AGENT}.jsonl", _child_records())
    con = _db(tmp_path, _stamp(extract_session_with_key(child, "upload", child.stem)), "orphan")
    (s,) = build_sessions(con, load_rounds(con))
    parent = build_session_detail(con, s["sessionId"])["topology"]["parentSessionId"]
    assert s["threadKind"] == "subagent" and parent == f"claude:{PARENT}"
    # the UI offers "open parent" only when the parent is among the loaded sessions
    assert parent not in {x["sessionId"] for x in build_sessions(con, load_rounds(con))}


def test_all_main_trace_with_null_hierarchy_columns_loads(tmp_path: Path) -> None:
    con = _db(tmp_path, [r for r in _tree_rounds(tmp_path) if r["thread_kind"] == "main"], "mainonly")
    loaded = load_rounds(con)
    assert loaded and all(r["thread_kind"] == "main" and r["parent_session_id"] is None for r in loaded)
    (s,) = build_sessions(con, loaded)
    assert s["childSessions"] == 0
    assert build_session_detail(con, s["sessionId"])["topology"]["children"] == []


# --------------------------------------------------------------------------- browser / sidecar ingest


def test_ingest_skips_workflow_journals_quietly(tmp_path: Path) -> None:
    from ingest import _extract_one_file

    journal = _write(tmp_path / "projects" / "demo" / PARENT / "subagents" / "workflows" / "wf_x" / "journal.jsonl",
                     [{"type": "started", "agentId": AGENT}, {"type": "result", "agentId": AGENT}])
    rows, _raw, _titles, warning, kind = _extract_one_file(journal)
    assert (rows, warning, kind) == ([], None, "skip")
    other = _write(tmp_path / "notes.jsonl", [{"hello": "world"}])
    assert _extract_one_file(other)[3] is not None
    # only a journal inside a subagent workflow folder is skipped quietly, not one in a folder that happens to be
    # called "workflows"
    elsewhere = _write(tmp_path / "workflows" / "journal.jsonl", [{"hello": "world"}])
    assert _extract_one_file(elsewhere)[3] is not None


# --------------------------------------------------------------------------- review follow-ups


def test_parent_keeps_the_key_prefix_so_it_matches_the_main_session_id() -> None:
    # every entry point builds the main transcript's key with the same prefix as its children's keys
    for prefix in ("", "-home-u-demo/", "home/u/.claude/projects/demo/"):
        key = f"{prefix}{PARENT}/subagents/workflows/wf_1/agent-{AGENT}"
        assert claude_hierarchy_from_key(key) == {
            "thread_kind": "subagent",
            "parent_session_id": f"claude:{prefix}{PARENT}",
            "workflow_id": "wf_1",
        }


@pytest.mark.parametrize(
    ("member", "expected"),
    [
        # a home-directory archive with its own projects/ folder above .claude/projects/
        (f"home/u/projects/cfg/.claude/projects/demo/{PARENT}.jsonl", ("demo", PARENT)),
        (f"home/u/projects/cfg/.claude/projects/demo/{PARENT}/subagents/workflows/wf_1/agent-{AGENT}.jsonl",
         ("demo", f"{PARENT}/subagents/workflows/wf_1/agent-{AGENT}")),
        # an archive without any projects/ segment keeps parent and child in one project
        (f"export/{PARENT}.jsonl", ("export", PARENT)),
        (f"export/{PARENT}/subagents/workflows/wf_1/agent-{AGENT}.jsonl",
         ("export", f"{PARENT}/subagents/workflows/wf_1/agent-{AGENT}")),
    ],
)
def test_ingest_keys_match_the_collector_for_awkward_archives(member: str, expected: tuple) -> None:
    from ingest import _claude_project_and_key

    assert _claude_project_and_key(Path(member)) == expected


def test_rows_from_an_older_windows_run_are_not_collected_twice(tmp_path: Path) -> None:
    from collect_llm_traces import count_round_keys
    from extract_claude_rounds import load_existing_round_keys
    from merge_round_traces import stable_round_identity

    old_sid = "\\".join((f"claude:{PARENT}", "subagents", f"agent-{AGENT}"))
    old = {"provider": "claude", "session_id": old_sid, "round_id": "msg_1"}
    trace = _write(tmp_path / "trace.jsonl", [{**old, "trace_key": round_key(old)}])   # the key as that run wrote it
    new = {**old, "session_id": f"claude:{PARENT}/subagents/agent-{AGENT}"}
    keys = load_existing_round_keys(trace)
    assert round_key(new) in keys
    assert count_round_keys(keys) == 1          # the collector's existing_rounds counts that row once
    assert stable_round_identity(old) == stable_round_identity(new)


def test_session_with_only_inline_sidechains_is_a_main_session(tmp_path: Path) -> None:
    main = _write(tmp_path / "projects" / "demo" / f"{PARENT}.jsonl", [
        _claude_user(sidechain=True, session=PARENT),
        _claude_assistant("msg_01side", sidechain=True, session=PARENT, role="Explore"),
    ])
    rows = _stamp(extract_session_with_key(main, "demo", PARENT))
    assert [(r["thread_kind"], r["parent_session_id"]) for r in rows] == [("subagent", f"claude:{PARENT}")]
    con = _db(tmp_path, rows)
    (s,) = build_sessions(con, load_rounds(con))
    assert (s["threadKind"], s["agentRole"]) == ("main", None)
    topology = build_session_detail(con, s["sessionId"])["topology"]
    assert (topology["threadKind"], topology["parentSessionId"], topology["children"]) == ("main", None, [])


@pytest.mark.parametrize("bad", [["main"], {"k": 1}, 7, True, ""])
def test_sanitizer_drops_non_string_hierarchy_values(bad) -> None:
    row = {"provider": "claude", "session_id": "claude:s", "round_id": "msg_01",
           "thread_kind": bad, "parent_session_id": bad, "workflow_id": bad, "agent_role": bad}
    clean = sanitize_row(row, StableIdSanitizer("seed-a"))
    assert all(clean[field] is None for field in HIERARCHY_FIELDS)


def test_hierarchy_columns_are_typed_even_when_all_null_or_absent(tmp_path: Path) -> None:
    main_only = [r for r in _tree_rounds(tmp_path) if r["thread_kind"] == "main"]
    typed = "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'rounds'"
    con = _db(tmp_path, main_only, "allnull")  # parent/workflow/role are null on every row
    assert {c: t for c, t in con.execute(typed).fetchall() if c in HIERARCHY_FIELDS} == dict.fromkeys(
        HIERARCHY_FIELDS, "VARCHAR")
    for r in main_only:
        for field in HIERARCHY_FIELDS:
            del r[field]
    old = _db(tmp_path, main_only, "absent")  # a DB built before the fields existed, opened via connect()
    assert {c: t for c, t in old.execute(typed).fetchall() if c in HIERARCHY_FIELDS} == dict.fromkeys(
        HIERARCHY_FIELDS, "VARCHAR")
    assert old.execute("SELECT count(*) FROM rounds WHERE thread_kind IS NOT NULL").fetchone()[0] == 0


# --------------------------------------------------------------------------- behaviour pinned by mutation testing


def test_hierarchy_from_key_edge_paths() -> None:
    # a file named after the folder is not a child; an agent directly under workflows/ has no workflow id
    assert claude_hierarchy_from_key(f"{PARENT}/subagents")["thread_kind"] == "main"
    direct = claude_hierarchy_from_key(f"{PARENT}/subagents/workflows/agent-{AGENT}")
    assert (direct["thread_kind"], direct["workflow_id"]) == ("subagent", None)
    assert direct["parent_session_id"] == f"claude:{PARENT}"
    # a "workflows" segment before subagents/ is not the child's workflow
    before = claude_hierarchy_from_key(f"workflows/{PARENT}/subagents/agent-{AGENT}")
    assert (before["thread_kind"], before["workflow_id"]) == ("subagent", None)


def test_a_child_file_fills_a_missing_round_role_from_the_file(tmp_path: Path) -> None:
    # the role is only on the child's first record; the round without its own role takes the file's
    user = {**_claude_user(sidechain=True, session=PARENT, agent=AGENT), "attributionAgent": "Explore"}
    child = _write(tmp_path / "projects" / "demo" / PARENT / "subagents" / f"agent-{AGENT}.jsonl",
                   [user, _claude_assistant("msg_02child", sidechain=True, session=PARENT, agent=AGENT)])
    (row,) = extract_session_with_key(child, "demo", f"{PARENT}/subagents/agent-{AGENT}")
    assert (row["thread_kind"], row["agent_role"]) == ("subagent", "Explore")


def test_inline_sidechains_do_not_share_a_file_wide_role(tmp_path: Path) -> None:
    # older main transcripts hold inline sidechains of different agents: a round without its own role
    # must not inherit another round's role
    main = _write(tmp_path / "projects" / "demo" / f"{PARENT}.jsonl", [
        _claude_user(sidechain=True, session=PARENT),
        _claude_assistant("msg_01side", sidechain=True, session=PARENT, role="Explore"),
        _claude_assistant("msg_02side", sidechain=True, session=PARENT),
    ])
    rows = extract_session_with_key(main, "demo", PARENT)
    assert [(r["thread_kind"], r["agent_role"]) for r in rows] == [("subagent", "Explore"), ("subagent", None)]


def test_a_main_file_naming_another_session_is_not_a_detached_child(tmp_path: Path) -> None:
    # only a file whose EVERY round is a sidechain is read as a child uploaded on its own
    other = "33333333-3333-4333-8333-333333333333"
    main = _write(tmp_path / "projects" / "demo" / f"{other}.jsonl", [
        _claude_user(sidechain=False, session=PARENT),
        _claude_assistant("msg_01main", sidechain=False, session=PARENT),
        _claude_assistant("msg_02side", sidechain=True, session=PARENT),
    ])
    rows = extract_session_with_key(main, "demo", other)
    assert rows[0]["thread_kind"] == "main" and rows[0]["parent_session_id"] is None


def test_sanitizing_an_already_sanitized_role_keeps_it() -> None:
    ids = StableIdSanitizer("seed-a")
    base = {"provider": "claude", "session_id": "claude:s", "round_id": "msg_01"}
    assert sanitize_row({**base, "agent_role": "custom_role_7"}, ids)["agent_role"] == "custom_role_7"
