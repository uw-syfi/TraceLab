from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402
from subagent_cohorts import summarize  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "subagent_timing", REPO_ROOT / "artifacts" / "subagents" / "timing" / "analyze.py"
)
timing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timing)


def _timestamp(seconds):
    if seconds is None or isinstance(seconds, str):
        return seconds
    return (datetime(2026, 1, 1) + timedelta(seconds=seconds)).isoformat()


def _row(provider, sid, *, events=(), intervals=(), origin="unknown", root="root", parent=None):
    return {
        "provider": provider, "session_id": f"{provider}:{sid}",
        "identity_scope_id": "10000000-0000-4000-8000-000000000001",
        "session_origin": origin,
        "root_session_id": f"{provider}:{root}" if root else None,
        "parent_session_id": f"{provider}:{parent}" if parent else None,
        "round_id": "duplicate", "trace_key": "duplicate", "round_index": 1,
        "timing_events": [{"timestamp": _timestamp(t)} for t in events],
        "tools": [{"tool_index": index, "tool_call_id": "duplicate", "tool_name": "Read",
                   "emitted_at": _timestamp(start), "result_at": _timestamp(end),
                   "tool_internal_latency_ms": None,
                   "tool_wall_latency_ms": round((end - start) * 1000) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None}
                  for index, (start, end) in enumerate(intervals)],
    }


def _family(*children):
    origins = {"claude": "claude_top_level_layout", "codex": "codex_top_level_source"}
    return [
        _row(provider, "root", origin=origins[provider], root=None,
             events=(0, 1000), intervals=((0, 999),))
        for provider in sorted({row["provider"] for row in children})
    ] + list(children)


def _write_trace(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def db_for(tmp_path):
    connections = []

    def build(rows):
        trace = tmp_path / f"trace-{len(connections)}.jsonl"
        _write_trace(trace, rows)
        db = trace.with_suffix(".duckdb")
        trace_db.materialize(trace, db)
        con = trace_db.connect(db, read_only=False)
        connections.append(con)
        # JSON timestamp inference can normalize infinity to 1900; test actual DuckDB infinities.
        for round_pk, row in enumerate(rows, 1):
            for event_index, event in enumerate(row["timing_events"], 1):
                if event["timestamp"] in ("infinity", "-infinity"):
                    con.execute("UPDATE timing_events SET timestamp = ?::TIMESTAMP WHERE round_pk = ? AND event_index = ?",
                                [event["timestamp"], round_pk, event_index])
            for tool in row["tools"]:
                for column in ("emitted_at", "result_at"):
                    if tool[column] in ("infinity", "-infinity"):
                        con.execute(f"UPDATE tool_calls SET {column} = ?::TIMESTAMP WHERE round_pk = ? AND tool_index = ?",
                                    [tool[column], round_pk, tool["tool_index"]])
        return con

    yield build
    for con in connections:
        con.close()


@pytest.mark.parametrize("intervals,expected", [
    ([], 0), ([(5, 8), (1, 3)], 5), ([(1, 5), (3, 7)], 6),
    ([(0, 10), (1, 2), (3, 8)], 10), ([(0, 2), (2, 4)], 4),
    ([(0, 4), (0, 4)], 4), ([(2, 2)], 0),
])
def test_union_disjoint_overlap_nested_touching_duplicate_zero(intervals, expected):
    assert timing.union_duration(intervals) == expected


def test_envelope_retained_rounds_and_same_ids_are_provider_isolated(db_for):
    con = db_for(_family(
        _row("claude", "child", events=(10, 5, None, "infinity"),
             intervals=((0, 8), (4, 12), (6, 7), (12, 15), (20, 25), (20, 25), (30, 30))),
        _row("claude", "child", intervals=((27, 28),)),
        _row("codex", "child", events=(10, 0), intervals=((1, 2),)),
    ))
    # Deliberately collide full IDs across providers; no query may rely on the usual ID prefix.
    # The compatibility view can shadow rounds when newer optional count fields are absent.
    database = trace_db._ident(trace_db._primary_database_name(con))
    con.execute(f"UPDATE {database}.main.rounds SET session_id = split_part(session_id, ':', 2)")
    con.execute("""UPDATE sessions SET session_id = split_part(session_id, ':', 2),
                   resolved_root_session_id = split_part(resolved_root_session_id, ':', 2)""")
    data = timing.collect(con)
    for provider, elapsed, tools, outstanding in (("claude", 30, 31, 21), ("codex", 10, 1, 1)):
        assert data[provider]["sessions"] == 1
        for metric, expected in (("elapsed", elapsed), ("tools", tools), ("outstanding", outstanding)):
            assert data[provider][metric] == pytest.approx(summarize([expected]))
    md = timing.render_md(data)
    assert "Work-ratio eligible" in md
    assert "| Claude | 1 | 30.00 | 30.00 | 30.00 | 30.00 | 30.00 |" in md


def test_missing_invalid_and_zero_timing_keep_distinct_denominators(db_for):
    rows = [
        _row("claude", "no-tools", events=(10, 0)),
        _row("claude", "empty"),
        _row("claude", "zero", events=(5,), intervals=((5, 5),)),
        _row("claude", "invalid-events", events=(None, "infinity", "-infinity")),
        _row("claude", "tool-only", events=(None, "infinity"), intervals=((2, 6),)),
        _row("claude", "bad-only", intervals=((5, None),)),
    ]
    for index, bad in enumerate(((2, None), (None, 2), (20, 15), (1, "infinity"), ("-infinity", 2))):
        rows.append(_row("claude", f"bad-{index}", events=(None, "infinity", 10, 0, "-infinity"),
                         intervals=((-5, 3), bad)))
    con = db_for(_family(*rows))
    assert con.execute("SELECT count(*) FROM timing_events WHERE isinf(timestamp)").fetchone()[0] > 0
    data = timing.collect(con)
    d = data["claude"]
    assert d["sessions"] == 11
    assert d["elapsed"] == pytest.approx(summarize([10, None, 0, None, 4, None] + [15] * 5))
    assert d["outstanding"] == pytest.approx(summarize([0, 0, 0, 0, 4, None] + [None] * 5))
    assert d["invalid_endpoints"] == 6
    assert d["tools"]["n"] == 11  # known positive work remains counted with partial coverage
    md = timing.render_md(data)
    assert "Timing coverage" in md
    assert "N/A" in md
    assert "Unknown legacy kinds stay unknown" in md


def test_report_pools_same_definition_ratios_as_overall_sessions(db_for):
    data = timing.collect(db_for(_family(
        _row("claude", "short", events=(10, 0), intervals=((0, 10),)),
        _row("claude", "long", events=(100, 0)),
    )))["claude"]
    assert data["elapsed"] == pytest.approx(summarize([10, 100]))
    assert data["share"] == pytest.approx(summarize([100, 0]))
    assert data["pooled_share"] == pytest.approx(100 * 10 / 110)
    assert data["share"]["avg"] != pytest.approx(data["pooled_share"])


def test_shared_cohort_uses_root_membership_not_parent_and_ignores_other_sessions(db_for):
    data = timing.collect(db_for(_family(
        _row("claude", "root-linked", events=(0, 2)),
        _row("claude", "indirect", root=None, parent="root-linked", events=(0, 4)),
        _row("claude", "unrelated", origin="claude_top_level_layout", root=None, events=(0, 100)),
        _row("claude", "unknown-anchor", root=None, events=(0, 100)),
        _row("claude", "outside", root="unknown-anchor", events=(0, 100)),
        _row("claude", "detached", root=None, parent="absent", events=(0, 100)),
    )))
    assert data["claude"]["sessions"] == 2
    assert data["claude"]["elapsed"] == pytest.approx(summarize([2, 4]))
    assert data["codex"]["sessions"] == 0


@pytest.mark.parametrize("source", ["db", "input", "empty", "unscoped", "legacy"])
def test_cli_outputs_are_aggregate_only_and_empty_cohorts_are_unavailable(tmp_path, source, monkeypatch, capsys):
    trace, db, out = tmp_path / "trace.jsonl", tmp_path / "trace.duckdb", tmp_path / "output"
    rows = _family(_row("claude", "private-session", events=(0, 10), intervals=((2, 4),)))
    if source == "unscoped":
        for row in rows:
            row.pop("identity_scope_id")
    if source == "legacy":
        with duckdb.connect(str(db)) as con:
            con.execute("CREATE TABLE rounds AS SELECT 1 AS round_pk, 'claude' AS provider, 'claude:old' AS session_id")
            con.execute("CREATE TABLE tool_calls AS SELECT 1 AS round_pk")
            con.execute("CREATE TABLE timing_events AS SELECT 1 AS round_pk")
    else:
        _write_trace(trace, rows)
        trace_db.materialize(trace, db)
        if source == "empty":
            with duckdb.connect(str(db)) as con:
                for table in ("sessions", "rounds", "tool_calls", "timing_events"):
                    con.execute(f"DELETE FROM {table}")
    before = db.read_bytes()
    monkeypatch.setattr(trace_db, "_cache_db_path", lambda path: tmp_path / "cached.duckdb")
    args = ["--input", str(trace)] if source == "input" else ["--db", str(db)]
    assert timing.main([*args, "--output-dir", str(out)]) == 0
    assert {path.name for path in out.iterdir()} == {"timing.md", "headline.json"}
    md = (out / "timing.md").read_text(encoding="utf-8")
    headline = json.loads((out / "headline.json").read_text(encoding="utf-8"))
    assert headline == [
        {"label": "Claude median observed subagent elapsed time (seconds)",
         "value": "10.00" if source in ("db", "input") else "N/A"},
        {"label": "Codex median observed subagent elapsed time (seconds)", "value": "N/A"},
    ]
    assert "private-session" not in md + json.dumps(headline)
    assert "claude:" not in md + json.dumps(headline)
    assert md.count("| Provider | N | Avg | P25 | P50 | P90 | P99 |") == 3
    assert "## Time with tools outstanding (seconds; extra wall metric)" in md
    assert "## Canonical attributed tool work (seconds)" in md
    assert md in capsys.readouterr().out
    assert db.read_bytes() == before
