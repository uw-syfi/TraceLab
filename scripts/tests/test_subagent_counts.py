from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, session_kinds_sql, summarize  # noqa: E402

SCOPE = "10000000-0000-4000-8000-000000000001"
ORIGINS = {
    "claude": ("claude_top_level_layout", "claude_subagent_layout"),
    "codex": ("codex_top_level_source", "codex_subagent_source"),
}


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"subagent_counts_{name}", REPO_ROOT / "artifacts" / "subagents" / name / "analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adoption = _load("adoption")
workload = _load("workload")


def _row(provider, sid, origin="unknown", *, parent=None, root=None, tools=0):
    return {
        "provider": provider,
        "session_id": f"{provider}:{sid}",
        "identity_scope_id": SCOPE,
        "session_origin": origin,
        "parent_session_id": f"{provider}:{parent}" if parent else None,
        "root_session_id": f"{provider}:{root}" if root else None,
        "round_id": "duplicate",
        "trace_key": "duplicate",
        "round_index": 1,
        "tools": [{"tool_index": i, "tool_name": "Read", "tool_call_id": "duplicate"} for i in range(tools)],
    }


@pytest.fixture
def family_rows():
    rows = []
    for provider, (main, child) in ORIGINS.items():
        rows += [
            _row(provider, "root", main, tools=2),
            _row(provider, "root", main, tools=4 if provider == "codex" else 0),
            _row(provider, "child", child, parent="root", tools=3),
            _row(provider, "grandchild", child, parent="child"),
            _row(provider, "flat", main),
            _row(provider, "flat-child", child, parent="flat"),
            _row(provider, "root-only", main),
            _row(provider, "member", child, root="root-only"),
            _row(provider, "unrelated", main, tools=7),
            _row(provider, "anchor", tools=9),
            _row(provider, "anchored", child, root="anchor", tools=11),
            _row(provider, "unknown", tools=13),
            _row(provider, "bad-main", main, root="unrelated", tools=17),
            _row(provider, "detached", child, parent="absent", tools=19),
        ]
        if provider == "codex":
            rows += [_row(provider, "root", main), _row(provider, "child", child, parent="root", tools=1)]
    return rows


@pytest.fixture
def db_for(tmp_path):
    connections = []

    def build(rows):
        trace = tmp_path / f"trace-{len(connections)}.jsonl"
        trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        db = trace.with_suffix(".duckdb")
        trace_db.materialize(trace, db)
        con = trace_db.connect(db)
        connections.append(con)
        return con

    yield build
    for con in connections:
        con.close()


def test_shared_summary_empty_zero_missing_and_signed():
    empty = {"n": 0, "avg": None, "p25": None, "p50": None, "p90": None, "p99": None}
    assert summarize([]) == empty
    assert summarize([None, float("nan"), float("inf")]) == empty
    assert summarize([0, None]) == {"n": 1, "avg": 0, "p25": 0, "p50": 0, "p90": 0, "p99": 0}
    assert summarize([-2, 0, 4]) == pytest.approx({"n": 3, "avg": 2 / 3, "p25": -1, "p50": 0, "p90": 3.2, "p99": 3.92})


def test_shared_cohort_is_unique_provider_partitioned_and_excludes_unknown_anchors(db_for, family_rows):
    con = db_for(family_rows)
    result = con.execute(f"WITH cohort AS ({COHORT_SQL}) SELECT * FROM cohort")
    assert [column[0] for column in result.description] == ["provider", "session_id", "root_session_id", "role"]
    rows = result.fetchall()
    assert not COHORT_SQL.rstrip().endswith(";")
    assert len(rows) == len(set(rows)) == 14
    for provider in ORIGINS:
        actual = {(sid, root, role) for prov, sid, root, role in rows if prov == provider}
        expected = {
            ("root", "root", "main"), ("child", "root", "descendant"),
            ("grandchild", "root", "descendant"), ("flat", "flat", "main"),
            ("flat-child", "flat", "descendant"), ("root-only", "root-only", "main"),
            ("member", "root-only", "descendant"),
        }
        assert actual == {(f"{provider}:{sid}", f"{provider}:{root}", role) for sid, root, role in expected}


def test_adoption_denominators_topology_and_depth_coverage(db_for, family_rows):
    data = adoption.collect(db_for(family_rows))
    for provider, excluded in (("claude", 1), ("codex", 0)):
        d = data[provider]
        assert {key: d[key] for key in (
            "observed_sessions", "main_origins", "subagent_origins", "unknown_origins",
            "eligible_mains", "delegating_mains", "matched_descendants", "known_parent_edges",
            "nested_families", "flat_families", "unknown_families", "unavailable_lineage",
        )} == dict(zip((
            "observed_sessions", "main_origins", "subagent_origins", "unknown_origins",
            "eligible_mains", "delegating_mains", "matched_descendants", "known_parent_edges",
            "nested_families", "flat_families", "unknown_families", "unavailable_lineage",
        ), (13, 5, 6, 2, 4, 3, 4, 3, 1, 1, 1, 0)))
        assert d["resolved_descendants"] == 4 + excluded
        assert d["excluded_resolved_descendants"] == excluded
        assert d["descendants_per_main"] == pytest.approx(summarize([2, 1, 1]))
        assert d["direct_children_per_main"] == pytest.approx(summarize([1, 1, 0]))
        assert d["depth"] == pytest.approx(summarize([1, 2, 1]))

    md = adoption.render_md(data)
    assert "Rates and coverage" not in md and "<br>" not in md and "; mean" not in md
    assert "| Main sessions with confirmed roots | 4 | 4 |" in md
    assert "| Main sessions with subagents | 3 | 3 |" in md
    assert "| Subagents with a known parent in the family | 3 | 3 |" in md
    assert "| Linked sessions outside these families | 1 | 0 |" in md
    assert "| Provider | Avg | P25 | P50 | P90 | P99 |" in md
    depth = md.split("### Subagent depth\n")[1]
    assert "| Provider | Count | Avg | P25 | P50 | P90 | P99 |" in depth
    assert "| Claude | 3 | 1.33 | 1.00 | 1.00 | 1.80 | 1.98 |" in depth
    assert md.count("| Provider |") == 3
    structure = md.split("## Family structure\n")[1].split("## Family size and depth\n")[0]
    assert structure.count("```text\n") == 3
    assert "synthetic examples, not reconstructions of actual data" in structure
    assert "direct launch links**, not just family membership" in structure
    assert "even if other parent links are unknown" in structure
    unknown_diagram = structure.split("**Unknown structure:**")[1].split("```text\n")[1].split("```")[0]
    assert "Main  A  B  C" in unknown_diagram and "→" not in unknown_diagram


@pytest.mark.parametrize("provider", ORIGINS)
def test_nested_evidence_does_not_require_known_depth(db_for, provider):
    main, child = ORIGINS[provider]
    data = adoption.collect(db_for([
        _row(provider, "root", main), _row(provider, "child", child, root="root"),
        _row(provider, "grandchild", child, parent="child"),
    ]))[provider]
    assert data["nested_families"] == 1
    assert data["flat_families"] == data["unknown_families"] == 0
    assert data["known_parent_edges"] == 1
    assert data["direct_children_per_main"]["avg"] == 0
    assert data["depth"]["n"] == 0
    assert data["depth"]["avg"] is None


@pytest.mark.parametrize("provider", ORIGINS)
def test_parent_outside_matched_family_does_not_establish_nestedness(db_for, provider):
    main, child = ORIGINS[provider]
    data = adoption.collect(db_for([
        _row(provider, "root", main), _row(provider, "unknown"),
        _row(provider, "child", child, root="root", parent="unknown"),
    ]))[provider]
    assert data["matched_descendants"] == data["unknown_families"] == 1
    assert data["nested_families"] == data["flat_families"] == data["known_parent_edges"] == 0


def test_workload_no_round_tool_or_main_child_fanout(db_for, family_rows):
    data = workload.collect(db_for(family_rows))
    for provider, main_rounds, main_tools, child_rounds, child_tools in (
        ("claude", [2, 1, 1], [2, 0, 0], [1, 1, 1, 1], [3, 0, 0, 0]),
        ("codex", [3, 1, 1], [6, 0, 0], [2, 1, 1, 1], [4, 0, 0, 0]),
    ):
        d = data[provider]
        assert d["observed_sessions"] == 13
        for role, rounds, tools in (("main", main_rounds, main_tools), ("descendant", child_rounds, child_tools)):
            s = d[role]
            assert s["sessions"] == len(rounds)
            assert s["rounds"] == sum(rounds)
            assert s["tools"] == sum(tools)
            assert s["rounds_per_session"] == pytest.approx(summarize(rounds))
            assert s["tools_per_session"] == pytest.approx(summarize(tools))
            assert s["tools_per_round"]["n"] == sum(rounds)
            assert s["tools_per_round"]["avg"] == pytest.approx(sum(tools) / sum(rounds))
            assert s["tools_per_round"]["p50"] == 0
            assert s["zero_tool_sessions"] == len(tools) - 1
            assert s["one_round_sessions"] == sum(n == 1 for n in rounds)
        assert d["excluded_resolved_descendants"] == (1 if provider == "claude" else 0)
    md = workload.render_md(data)
    assert "| Sessions with no tool calls | 2 (66.7%) | 2 (66.7%) |" in md
    assert "| Sessions with one round | 4 (100.0%) | 3 (75.0%) |" in md
    assert md.count("| Provider | Avg | P25 | P50 | P90 | P99 |") == 6
    assert md.count("### Rounds per session") == md.count("### Tool calls per session") == 2
    assert "| Claude | 1.33 | 1.00 | 1.00 | 1.80 | 1.98 |" in md
    assert "Count" not in md and "<br>" not in md and "; mean" not in md


def test_empty_cohort_is_not_zero_workload_or_unknown_adoption(db_for):
    con = db_for([_row("claude", "unrelated", ORIGINS["claude"][0])])
    a, w = adoption.collect(con), workload.collect(con)
    assert a["claude"]["eligible_mains"] == 1
    assert a["claude"]["delegating_mains"] == 0
    assert adoption.render_headline(a)[0]["value"] == "0.0%"
    assert adoption.render_headline(a)[1]["value"] == "N/A"
    for provider in ORIGINS:
        assert a[provider]["descendants_per_main"]["avg"] is None
        for role in ("main", "descendant"):
            assert w[provider][role]["sessions"] == 0
            assert w[provider][role]["tools_per_session"] == summarize([])
            assert w[provider][role]["tools_per_round"] == summarize([])
    assert all(item["value"] == "N/A" for item in workload.render_headline(w))
    assert "| Main sessions with confirmed roots | 1 | 0 |" in adoption.render_md(a)
    assert "| Claude | 0 | N/A | N/A | N/A | N/A | N/A |" in adoption.render_md(a)
    assert "| Claude | N/A | N/A | N/A | N/A | N/A |" in workload.render_md(w)


def test_unscoped_known_origins_keep_classification_not_linkage(db_for, family_rows):
    for row in family_rows:
        row.pop("identity_scope_id")
    con = db_for(family_rows)
    a, w = adoption.collect(con), workload.collect(con)
    for provider in ORIGINS:
        assert a[provider]["main_origins"] == 5
        assert a[provider]["subagent_origins"] == 6
        assert a[provider]["unavailable_lineage"] == 13
        assert a[provider]["eligible_mains"] == a[provider]["delegating_mains"] == 0
        assert w[provider]["main"]["rounds_per_session"]["avg"] is None
    assert all(item["value"] == "N/A" for item in adoption.render_headline(a))


@pytest.mark.parametrize("name,module,expected", [
    ("adoption", adoption, [
        {"label": "Claude main sessions with subagents", "value": "75.0%"},
        {"label": "Codex main sessions with subagents", "value": "75.0%"},
        {"label": "Claude average subagents per main session", "value": "1.33"},
        {"label": "Codex average subagents per main session", "value": "1.33"},
    ]),
    ("workload", workload, [
        {"label": "Claude median rounds per main session", "value": "1.00"},
        {"label": "Claude median rounds per subagent", "value": "1.00"},
        {"label": "Codex median rounds per main session", "value": "1.00"},
        {"label": "Codex median rounds per subagent", "value": "1.00"},
    ]),
])
@pytest.mark.parametrize("source", ["db", "input"])
def test_main_emits_only_expected_artifacts_with_computed_provider_values(
    tmp_path, family_rows, name, module, expected, source, monkeypatch, capsys,
):
    trace = tmp_path / "synthetic.jsonl"
    trace.write_text("".join(json.dumps(row) + "\n" for row in family_rows), encoding="utf-8")
    db = tmp_path / "synthetic.duckdb"
    trace_db.materialize(trace, db)
    before = db.read_bytes()
    if source == "input":
        monkeypatch.setattr(trace_db, "_cache_db_path", lambda path: tmp_path / "cached.duckdb")
    out = tmp_path / "output"
    assert module.main([f"--{source}", str(db if source == "db" else trace), "--output-dir", str(out)]) == 0
    assert {path.name for path in out.iterdir()} == {f"{name}.md", "headline.json"}
    md = (out / f"{name}.md").read_text(encoding="utf-8")
    assert "| Claude | Codex |" in md
    assert "not complete real execution" in md
    assert "claude:root" not in md and "codex:root" not in md
    assert json.loads((out / "headline.json").read_text(encoding="utf-8")) == expected
    assert db.read_bytes() == before
    stdout = capsys.readouterr().out
    assert "| Claude | Codex |" in stdout


def test_population_baselines_outlier_and_zero_round_coverage(db_for):
    main, child = ORIGINS["claude"]
    rows = [
        *[_row("claude", "long", main, tools=2) for _ in range(10)],
        _row("claude", "short", main),
        *[_row("claude", f"long-child-{i}", child, parent="long") for i in range(3)],
        _row("claude", "short-child", child, parent="short", tools=3),
        _row("claude", "solo", main),
    ]
    data = workload.collect(db_for(rows))
    d = data["claude"]
    assert {key: d["populations"][key]["sessions"] for key in ("all", "all_main", "other_main", "main", "descendant")} == {
        "all": 7, "all_main": 3, "other_main": 1, "main": 2, "descendant": 4,
    }
    assert d["populations"]["all"]["rounds_per_session"]["avg"] == pytest.approx(16 / 7)
    assert d["populations"]["all_main"]["rounds_per_session"]["avg"] == 4
    assert d["populations"]["descendant"]["rounds_per_session"]["p50"] == 1
    assert d["main"]["rounds"] == 11  # Children stay separate, regardless of family sizes.
    assert d["descendant"]["rounds"] == 4
    assert d["main"]["largest_session_rounds"] == 10
    assert d["main"]["rounds_without_largest_mean"] == 1
    assert d["main"]["zero_tool_rounds"] == 1
    assert d["main"]["multi_tool_rounds"] == 10
    md = workload.render_md(data)
    assert "| Largest session's rounds | 10 (90.9%) | N/A |" in md
    assert "a main never includes its children's rows" in md
    assert "unrelated mains are excluded only from matched tables" in md
    assert "do not measure task difficulty or efficiency" in md


def test_kinds_partition_children_without_guessing_legacy_or_conflicting_evidence(db_for):
    main, child = ORIGINS["codex"]
    rows = [_row("codex", "root", main)]
    for sid, kinds in (("worker", ["worker"]), ("guardian", ["guardian"]),
                       ("mixed", ["worker", "guardian"]), ("legacy", [None])):
        for kind in kinds:
            row = _row("codex", sid, child, parent="root")
            row["session_kind"] = kind
            rows.append(row)
    con = db_for(rows)
    kinds = dict((sid.rsplit(":", 1)[-1], kind) for _, sid, kind in con.execute(session_kinds_sql(con)).fetchall())
    assert kinds == {"root": "unknown", "worker": "worker", "guardian": "guardian", "mixed": "unknown", "legacy": "unknown"}
    d = workload.collect(con)["codex"]["populations"]
    assert [d[key]["sessions"] for key in ("worker", "guardian", "unknown_kind")] == [1, 1, 2]
    assert adoption.collect(con)["codex"]["linked_kinds"] == {"worker": 1, "guardian": 1, "unknown": 2}


@pytest.mark.parametrize("name,module", [("adoption", adoption), ("workload", workload)])
def test_legacy_without_sessions_emits_unavailable_not_zero(tmp_path, name, module):
    db = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("""
            CREATE TABLE rounds AS SELECT * FROM (VALUES
                (1, 'claude', 'claude:old'), (2, 'codex', 'codex:old')
            ) AS r(round_pk, provider, session_id)
        """)
        con.execute("CREATE TABLE tool_calls AS SELECT 1 AS round_pk, 'Read' AS tool_name")
        con.execute("CREATE TABLE timing_events AS SELECT 1 AS round_pk")
    before = db.read_bytes()
    out = tmp_path / "output"
    assert module.main(["--db", str(db), "-o", str(out)]) == 0
    md = (out / f"{name}.md").read_text(encoding="utf-8")
    assert "| Sessions with unavailable family links | 1 | 1 |" in md
    assert "| Claude | Codex |" in md
    assert all(item["value"] == "N/A" for item in json.loads((out / "headline.json").read_text()))
    assert db.read_bytes() == before
