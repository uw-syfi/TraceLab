from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("subagent_tool_plot", ROOT / "artifacts/subagents/tool_usage/plot.py")
assert SPEC is not None and SPEC.loader is not None
plot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plot)


@pytest.fixture
def con():
    db = duckdb.connect()
    db.execute("""
        CREATE TABLE sessions (provider VARCHAR, session_id VARCHAR, session_origin VARCHAR,
                               root_status VARCHAR, resolved_root_session_id VARCHAR);
        CREATE TABLE rounds (round_pk BIGINT, provider VARCHAR, session_id VARCHAR);
        CREATE TABLE tool_calls (round_pk BIGINT, tool_name VARCHAR, is_error BOOLEAN,
                                tool_internal_latency_ms BIGINT, tool_wall_latency_ms BIGINT);
    """)
    yield db
    db.close()


def family(con, provider, start=0):
    # Identical IDs across providers deliberately catch joins missing provider identity.
    origin = "claude_top_level_layout" if provider == "claude" else "codex_top_level_source"
    for i, sid in enumerate(("private-main", "private-child", "private-empty")):
        con.execute("INSERT INTO sessions VALUES (?, ?, ?, 'resolved_explicit', 'private-main')",
                    [provider, sid, origin if i == 0 else "unknown"])
        con.execute("INSERT INTO rounds VALUES (?, ?, ?)", [start + i, provider, sid])


def index(groups, tools):
    return ({(r["provider"], r["role"]): r for r in groups},
            {(r["provider"], r["role"], r["tool_name"]): r for r in tools})


def test_metrics_provider_identity_fanout_outcomes_and_latency(con):
    family(con, "claude")
    family(con, "codex", 10)
    con.execute("""
        INSERT INTO tool_calls VALUES
        (0, 'shell', TRUE, 100, 900), (0, 'shell', FALSE, 0, 800),
        (0, 'shell', NULL, NULL, 200), (0, 'Read', NULL, -5, 700),
        (1, 'shell', FALSE, NULL, NULL), (1, '', NULL, NULL, 50),
        (1, NULL, NULL, NULL, NULL), (1, '   ', NULL, NULL, NULL),
        (10, 'shell', FALSE, NULL, 1000), (11, 'shell', TRUE, 20, 2000);
    """)
    groups, tools = plot.load_metrics(con)
    g, t = index(groups, tools)
    main = g["claude", "main"]
    assert main["diversity_n"] == 1  # Not duplicated by two children.
    assert main["calls"] == 4
    assert main["latency_ms"] == 300
    shell = t["claude", "main", "shell"]
    assert shell["error_rate"] == 0.5
    assert shell["known_outcome_coverage"] == pytest.approx(2 / 3)
    assert shell["call_share"] == 0.75
    assert shell["positive_latency_calls"] == 2
    assert shell["nonpositive_latency_calls"] == 1
    assert t["claude", "main", "Read"]["latency_ms"] is None
    assert t["claude", "main", "Read"]["nonpositive_latency_calls"] == 1
    assert t["claude", "main", "Read"]["error_rate"] is None
    assert t["claude", "main", "Read"]["known_outcome_coverage"] == 0
    assert t["claude", "descendant", None]["calls"] == 3
    assert g["claude", "descendant"]["diversity_n"] == 2
    assert g["claude", "descendant"]["diversity_avg"] == 0.5
    assert g["claude", "descendant"]["diversity_p50"] == 0.5
    assert g["codex", "main"]["calls"] == 1
    assert g["codex", "main"]["latency_ms"] == 1000
    assert g["codex", "descendant"]["latency_ms"] == 20
    for group in groups:
        rows = [r for r in tools if (r["provider"], r["role"]) == (group["provider"], group["role"])]
        assert sum(r["call_share"] for r in rows) == pytest.approx(1)
        assert sum(r["latency_share"] for r in rows) == pytest.approx(1)
        assert group["calls"] == sum(group[k] for k in ("errors", "successes", "unknown_outcomes"))
        assert group["calls"] == sum(group[k] for k in ("positive_latency_calls", "missing_latency_calls", "nonpositive_latency_calls"))


def test_empty_cohort_is_not_zero_diversity(con):
    family(con, "claude")
    # Unlinked main and unresolved family are not part of the matched population.
    con.execute("""
        INSERT INTO sessions VALUES
        ('claude', 'private-no-round', 'unknown', 'resolved_explicit', 'private-main'),
        ('codex', 'solo', 'codex_top_level_source', 'resolved_explicit', 'solo'),
        ('codex', 'orphan', 'unknown', 'missing', NULL);
        INSERT INTO rounds VALUES (20, 'codex', 'solo'), (21, 'codex', 'orphan');
        INSERT INTO tool_calls VALUES (20, 'excluded', TRUE, 5, 5), (21, 'excluded', TRUE, 5, 5);
    """)
    groups, tools = plot.load_metrics(con)
    g, _ = index(groups, tools)
    assert tools == []
    assert g["claude", "main"]["diversity_avg"] == 0
    assert g["claude", "descendant"]["diversity_n"] == 3
    assert g["codex", "main"]["diversity_avg"] is None
    assert g["codex", "main"]["diversity_n"] == 0
    for row in groups:
        assert row["call_share"] is None
        assert row["error_rate"] is None
        assert row["known_outcome_coverage"] is None
        assert row["latency_ms"] is None


def test_exact_names_and_full_tail(con, tmp_path):
    family(con, "claude")
    names = ["shell", "Shell", " shell ", "mcp_one", "mcp_two", "a|b<c>"] + [f"tool-{i}" for i in range(12)]
    con.executemany("INSERT INTO tool_calls VALUES (0, ?, NULL, NULL, NULL)", [(n,) for n in names])
    groups, tools = plot.load_metrics(con)
    assert groups[0]["diversity_avg"] == len(names)
    assert len(plot.selected_names(tools)) == 12
    assert {r["tool_name"] for r in tools} == set(names)
    plot.write_tables(groups, tools, tmp_path)
    with (tmp_path / "tool_usage.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len([r for r in rows if r["row_type"] == "tool"]) == len(names)
    text = (tmp_path / "tool_usage.md").read_text()
    assert "a&#124;b&lt;c&gt;" in text
    assert "Tools outside the figure" in text
    assert "Codex: no linked data" in text
    assert text.count("| Provider | Avg | P25 | P50 | P90 | P99 |") == 2
    assert "#### <code> shell </code>" in text
    assert "| Outcome coverage | 0% | N/A |" in text
    assert "<br>" not in text and "; mean" not in text


def test_display_aligns_asymmetric_tool_names_and_missing_latency(con):
    family(con, "claude")
    family(con, "codex", 10)
    con.execute("""
        INSERT INTO tool_calls VALUES
        (0, 'shared', TRUE, 100, NULL), (10, 'shared', FALSE, NULL, NULL),
        (0, 'Claude-only', NULL, NULL, NULL), (10, 'Codex-only', FALSE, 0, 10);
    """)
    page = plot.render_md(*plot.load_metrics(con))
    shared = page.split("#### <code>shared</code>")[1]
    assert "| Calls | 1 | 1 |" in shared and "| Call share | 50% | 50% |" in shared
    assert "| Known errors | 1 | 0 |" in shared and "| Known successes | 0 | 1 |" in shared
    assert "| Error rate | 100% | 0% |" in shared and "| Outcome coverage | 100% | 100% |" in shared
    assert "| Summed tool time (ms) | 100 | N/A |" in shared
    assert "| Calls with missing time | 0 | 1 |" in shared
    codex_only = page.split("#### <code>Codex-only</code>")[1].split("####")[0]
    assert "| Calls | N/A | 1 |" in codex_only
    assert "<br>" not in page and "; mean" not in page


def test_time_selection_includes_rare_slow_tool_and_keeps_exact_csv_names(con, tmp_path, monkeypatch):
    family(con, "claude")
    con.execute("""
        INSERT INTO tool_calls VALUES
        (0, 'mcp_one', FALSE, 10, 500), (0, 'mcp_one', FALSE, 10, 500),
        (0, 'mcp_two', FALSE, 10, 500), (0, 'mcp_two', FALSE, 10, 500),
        (0, 'rare-slow', FALSE, 1000, 9999);
    """)
    monkeypatch.setattr(plot, "TOP_TOOLS", 1)
    groups, tools = plot.load_metrics(con)
    calls = plot.chart_rows(tools, "calls")
    times = plot.chart_rows(tools, "latency_ms")
    assert plot.selected_names(calls, "calls") == ["mcp"]
    assert plot.selected_names(times, "latency_ms") == ["rare-slow"]
    assert next(r for r in times if r["tool_name"] == "rare-slow")["latency_ms"] == 1000
    saved = []
    monkeypatch.setattr(plot, "save_plot", lambda fig, path: saved.append(fig))
    plot.plot_usage(groups, tools, tmp_path)
    figure = saved[0]
    assert [tick.get_text() for tick in figure.axes[0].get_yticklabels()] == ["mcp"]
    assert [tick.get_text() for tick in figure.axes[1].get_yticklabels()] == ["rare-slow"]
    assert "Main omitted: 3.8%" in figure.axes[1].get_title(loc="left")
    plot.write_tables(groups, tools, tmp_path)
    with (tmp_path / "tool_usage.csv").open() as fh:
        assert {r["tool_name"] for r in csv.DictReader(fh) if r["row_type"] == "tool"} == {
            "mcp_one", "mcp_two", "rare-slow",
        }
    assert "Positive time (ms)" in (tmp_path / "tool_usage.md").read_text()


def test_hidden_time_category_cannot_set_displayed_axis(con, tmp_path, monkeypatch):
    family(con, "claude")
    family(con, "codex", 10)
    con.execute("""
        INSERT INTO tool_calls VALUES
        (0, 'displayed', FALSE, 100, NULL), (10, 'displayed', FALSE, 100, NULL),
        (0, 'hidden', FALSE, 150, NULL);
    """)
    monkeypatch.setattr(plot, "TOP_TOOLS", 1)
    saved = []
    monkeypatch.setattr(plot, "save_plot", lambda fig, path: saved.append(fig))
    groups, tools = plot.load_metrics(con)
    plot.plot_usage(groups, tools, tmp_path)
    assert saved[0].axes[1].get_xlim()[1] == pytest.approx(100 * 1.08 / 3_600_000)
    assert saved[0].axes[3].get_xlim() == saved[0].axes[1].get_xlim()
    assert "| Claude | Positive time (ms) | 1 | 150 | 60% |" in plot.render_md(groups, tools)


@pytest.mark.parametrize("population", ["empty", "zero", "calls"])
def test_main_synthetic_plot_smoke(con, tmp_path, monkeypatch, population):
    if population != "empty":
        for provider, start in (("claude", 0), ("codex", 10)):
            family(con, provider, start)
            if population == "calls":
                con.executemany("INSERT INTO tool_calls VALUES (?, ?, ?, ?, ?)", [
                    (start, "shell", False, 100, 200), (start, "Read", None, 0, 800),
                    (start + 1, "shell", True, None, 400), (start + 1, "patch", False, 200, 300),
                ])
    # Existing unrelated exports must not enter the PNG or get rewritten.
    (tmp_path / "unrelated.csv").write_text("private-sentinel")
    monkeypatch.setattr(plot.trace_db, "open_from_args", lambda args: con)
    monkeypatch.setattr(sys, "argv", ["plot.py", "--output-dir", str(tmp_path)])
    assert plot.main() == 0
    assert {p.name for p in tmp_path.iterdir()} == {
        "unrelated.csv", "tool_usage.png", "tool_usage.md", "tool_usage.csv"}
    assert (tmp_path / "tool_usage.png").stat().st_size > 1000
    payload = plot.png_sidecar.extract_from_png(tmp_path / "tool_usage.png")
    assert [key for key in payload if key.startswith("sidecar:data/")] == ["sidecar:data/tool_usage.csv"]
    assert payload["sidecar:data/tool_usage.csv"] == (tmp_path / "tool_usage.csv").read_text()
    assert "private-sentinel" not in str(payload)
    assert "private-main" not in str(payload)
    assert "private-child" not in str(payload)
    assert not plot.plt.get_fignums()
    with pytest.raises(duckdb.ConnectionException):
        con.execute("SELECT 1")
