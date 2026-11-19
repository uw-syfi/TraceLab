"""Shared raw-call accounting must preserve each consumer's grouping and output policy."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402
from tool_metrics import tool_aggregate_sql  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(f"tool_metrics_test_{name}", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


counts = load("counts", "artifacts/tool_calls/tool_call_counts/plot.py")
time = load("time", "artifacts/tool_calls/tool_time_by_kind/plot.py")
latency = load("latency", "artifacts/tool_calls/tool_latency_distribution/plot.py")
category = load("category", "artifacts/tool_calls/tool_category_distribution/analyze.py")
subagents = load("subagents", "artifacts/subagents/tool_usage/plot.py")


@pytest.fixture
def con():
    db = duckdb.connect()
    db.execute("""
        CREATE TABLE sessions (provider VARCHAR, session_id VARCHAR, session_origin VARCHAR,
                               root_status VARCHAR, resolved_root_session_id VARCHAR);
        CREATE TABLE rounds (round_pk BIGINT, provider VARCHAR, session_id VARCHAR);
        CREATE TABLE tool_calls (round_pk BIGINT, tool_index BIGINT, tool_name VARCHAR,
                                is_error BOOLEAN, tool_internal_latency_ms BIGINT,
                                tool_wall_latency_ms BIGINT);
        INSERT INTO rounds VALUES (1, 'claude', 'main'), (2, 'claude', 'child'),
            (3, 'codex', 'main'), (4, 'codex', 'child'), (5, '', 'unknown'), (6, NULL, 'unknown');
        INSERT INTO sessions VALUES
            ('claude', 'main', 'claude_top_level_layout', 'resolved_explicit', 'main'),
            ('claude', 'child', 'unknown', 'resolved_explicit', 'main'),
            ('codex', 'main', 'codex_top_level_source', 'resolved_explicit', 'main'),
            ('codex', 'child', 'unknown', 'resolved_explicit', 'main');
        INSERT INTO tool_calls VALUES
            (1, 0, 'shell', TRUE, 100, 900), (1, 1, 'shell', FALSE, 0, 800),
            (1, 2, 'shell', NULL, NULL, 200), (1, 3, ' shell ', FALSE, -5, 700),
            (1, 4, 'mcp_one', NULL, NULL, 10), (1, 5, 'mcp_two', FALSE, NULL, 20),
            (1, 6, 'missing', NULL, NULL, NULL), (1, 7, '', NULL, NULL, NULL),
            (1, 8, '   ', FALSE, 0, 80), (1, 9, NULL, TRUE, NULL, 30),
            (2, 0, 'shell', FALSE, NULL, 40), (3, 0, 'shell', FALSE, NULL, 1000),
            (4, 0, 'shell', TRUE, 20, 2000), (5, 0, 'Read', FALSE, NULL, 0),
            (6, 0, 'Bash', TRUE, NULL, 50);
    """)
    yield db
    db.close()


def test_raw_call_partitions_and_sum_availability(con):
    agg = tool_aggregate_sql(latency=f"({trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL})")
    expressions = ", ".join(agg.values())
    assert con.execute(f"SELECT {expressions} FROM tool_calls").fetchone() == (
        15, 4, 7, 4, 9, 2, 4, 1470, 1470,
    )
    assert con.execute(
        f"SELECT {expressions} FROM tool_calls WHERE tool_name = 'missing'"
    ).fetchone() == (1, 0, 0, 1, 0, 1, 0, None, 0)
    assert con.execute(f"SELECT {expressions} FROM tool_calls WHERE FALSE").fetchone() == (
        0, 0, 0, 0, 0, 0, 0, None, None,
    )


def test_consumers_keep_names_providers_and_missing_time_policies(con):
    by_provider = counts.load_tool_counts_by_provider(con, min_calls=1)
    assert by_provider["claude"]["shell"].calls == 4  # Counts preserve surrounding spaces.
    assert by_provider["claude"][" shell "].calls == 1
    assert by_provider["claude"]["mcp"].calls == 2
    assert by_provider["claude"][None].error_calls == 1
    assert "" in by_provider and None in by_provider

    totals = time.load_tool_time(con)
    assert totals["shell"].calls == 7  # Time/latency trim names and pool providers.
    assert totals["shell"].latency_sum == 1360
    assert totals["missing"].latency_sum == 0
    stats = latency.load_tool_stats(con)
    assert stats["shell"].summary() == {
        "calls": 7, "latency_count": 5, "missing_latency": 0, "nonpositive_latency": 2,
        "error_calls": 2, "mean_ms": 272, "min_ms": 20, "max_ms": 1000,
        "p50_ms": 100, "p90_ms": 680, "p99_ms": 968,
        "sample_count": 5, "sampled": False, "providers": {"claude": 5, "codex": 2},
    }
    assert stats["missing"].latency_sum == 0 and stats["missing"].summary()["p50_ms"] is None
    aggregates = category.load_tool_aggregates(con)
    assert aggregates["claude", "shell"].calls == 5
    assert aggregates["claude", "shell"].nonpositive_latency_calls == 2
    assert aggregates["claude", "missing"].total_latency_ms == 0
    assert ("<unknown-provider>", "Read") in aggregates
    assert ("<unknown-provider>", "Bash") in aggregates

    groups, tools = subagents.load_metrics(con)
    lookup = {(r["provider"], r["role"], r["tool_name"]): r for r in tools}
    shell = lookup["claude", "main", "shell"]
    assert shell["calls"] == 3 and shell["latency_ms"] == 300
    assert shell["error_rate"] == 0.5 and shell["known_outcome_coverage"] == pytest.approx(2 / 3)
    assert lookup["claude", "main", "missing"]["latency_ms"] is None
    assert lookup["claude", "main", None]["calls"] == 3
    assert lookup["claude", "main", " shell "]["calls"] == 1
    assert lookup["claude", "main", "mcp_one"]["calls"] == 1
    assert lookup["claude", "main", "mcp_two"]["calls"] == 1
    assert len(groups) == 4


def test_existing_ranking_collapse_and_export_contracts(con, tmp_path):
    count_groups = counts.load_tool_counts_by_provider(con, min_calls=2)
    count_rows = counts.write_tool_call_counts_by_provider(
        {provider: count_groups[provider] for provider in ("claude", "codex")}, tmp_path, 30,
    )
    assert [r["tool_name"] for r in count_rows if r["provider"] == "claude"] == [
        "Other (<2 calls/tool)", "shell", "mcp",
    ]
    shell = next(r for r in count_rows if r["provider"] == "claude" and r["tool_name"] == "shell")
    assert shell["error_rate"] == 0.25  # The older page divides by all calls.
    counts.write_tool_call_counts_by_provider(
        {"claude": {name: counts.ToolCounts(1, 0, first) for name, first in (("z", 1), ("a", 2))}},
        tmp_path, 30,
    )
    with (tmp_path / "tool_call_counts_by_provider.csv").open() as fh:
        assert [r["tool_name"] for r in csv.DictReader(fh)] == ["z", "a"]

    for loader in (time.load_tool_time_by_provider, latency.load_tool_stats_by_provider):
        grouped = loader(con, min_calls=2)["claude"]
        assert grouped["mcp"].calls == 2 and grouped["shell"].calls == 5
        assert grouped["Other (<2 calls/tool)"].calls == 1
    time_rows = time.write_tool_total_time_by_kind(time.load_tool_time(con), tmp_path)
    assert all(r["tool_name"] != "missing" for r in time_rows)
    latency.write_tool_summary(latency.load_tool_stats(con), tmp_path)
    groups, tools = subagents.load_metrics(con)
    subagents.write_tables(groups, tools, tmp_path)
    assert "| Error rate | 50% | 0% |" in (tmp_path / "tool_usage.md").read_text()
    with (tmp_path / "tool_usage.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    missing = next(r for r in rows if r["row_type"] == "tool" and r["tool_name"] == "missing")
    assert missing["latency_ms"] == "" and missing["missing_latency_calls"] == "1"
