#!/usr/bin/env python3
"""Audit Codex wall-vs-internal tool latency gaps.

Codex traces often have two latency notions:

* ``tool_wall_latency_ms``: timestamp span from function-call emission to function-call output.
* ``tool_internal_latency_ms``: runner-reported ``Wall time: ... seconds`` parsed from output.

This experiment quantifies the residual ``max(wall - internal, 0)``. The residual is the only signal
available for approval/user wait in the normalized trace, because tool inputs, outputs, and explicit
approval events are not retained.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Any

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> tool_calls -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402


DIRECT_HUMAN_TOOLS = {"request_user_input"}
EXECUTION_LIKE_TOOLS = {"exec_command", "shell_command", "shell", "apply_patch"}


def _connect(args: argparse.Namespace):
    return trace_db.open_from_args(args)


def _round_or_none(value: Any, digits: int = 3) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return round(number, digits)


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _fetch_dicts(con, query: str) -> list[dict[str, Any]]:
    columns = [col[0] for col in con.execute(query).description]
    return [dict(zip(columns, row)) for row in con.fetchall()]


def coverage_rows(con) -> list[dict[str, Any]]:
    query = """
        SELECT
          tc.tool_name,
          COUNT(*) AS calls,
          COUNT(tc.tool_wall_latency_ms) AS wall_count,
          COUNT(tc.tool_internal_latency_ms) AS internal_count,
          SUM(CASE
              WHEN tc.tool_wall_latency_ms IS NOT NULL
               AND tc.tool_internal_latency_ms IS NOT NULL THEN 1 ELSE 0 END) AS both_count,
          SUM(CASE
              WHEN tc.tool_wall_latency_ms IS NOT NULL
               AND tc.tool_internal_latency_ms IS NULL THEN 1 ELSE 0 END) AS wall_only_count,
          ROUND(SUM(tc.tool_wall_latency_ms) / 1000.0, 3) AS wall_total_s,
          ROUND(SUM(tc.tool_internal_latency_ms) / 1000.0, 3) AS internal_total_s
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE r.provider = 'codex'
        GROUP BY tc.tool_name
        ORDER BY calls DESC, tc.tool_name
    """
    return _fetch_dicts(con, query)


def residual_rows(con) -> list[dict[str, Any]]:
    query = """
        WITH base AS (
          SELECT
            tc.tool_name,
            tc.tool_wall_latency_ms AS wall_ms,
            tc.tool_internal_latency_ms AS internal_ms,
            GREATEST(tc.tool_wall_latency_ms - tc.tool_internal_latency_ms, 0) AS gap_ms
          FROM tool_calls tc
          JOIN rounds r USING (round_pk)
          WHERE r.provider = 'codex'
            AND tc.tool_wall_latency_ms IS NOT NULL
            AND tc.tool_internal_latency_ms IS NOT NULL
            AND tc.tool_wall_latency_ms > 0
            AND tc.tool_internal_latency_ms >= 0
        )
        SELECT
          tool_name,
          COUNT(*) AS calls,
          ROUND(SUM(wall_ms) / 1000.0, 3) AS wall_total_s,
          ROUND(SUM(internal_ms) / 1000.0, 3) AS internal_total_s,
          ROUND(SUM(gap_ms) / 1000.0, 3) AS gap_total_s,
          ROUND(100.0 * SUM(gap_ms) / NULLIF(SUM(wall_ms), 0), 3) AS gap_share_of_wall_pct,
          ROUND(AVG(gap_ms) / 1000.0, 3) AS mean_gap_s,
          ROUND(QUANTILE_CONT(gap_ms / 1000.0, 0.50), 3) AS p50_gap_s,
          ROUND(QUANTILE_CONT(gap_ms / 1000.0, 0.90), 3) AS p90_gap_s,
          ROUND(QUANTILE_CONT(gap_ms / 1000.0, 0.99), 3) AS p99_gap_s,
          SUM(CASE WHEN gap_ms > 1000 THEN 1 ELSE 0 END) AS gap_gt_1s,
          SUM(CASE WHEN gap_ms > 10000 THEN 1 ELSE 0 END) AS gap_gt_10s,
          SUM(CASE WHEN gap_ms > 60000 THEN 1 ELSE 0 END) AS gap_gt_1m,
          SUM(CASE WHEN gap_ms > 600000 THEN 1 ELSE 0 END) AS gap_gt_10m,
          SUM(CASE WHEN gap_ms > 3600000 THEN 1 ELSE 0 END) AS gap_gt_1h
        FROM base
        GROUP BY tool_name
        ORDER BY gap_total_s DESC, calls DESC, tool_name
    """
    return _fetch_dicts(con, query)


def paper_table_rows(con) -> list[dict[str, Any]]:
    query = """
        WITH base AS (
          SELECT
            tc.tool_name,
            tc.tool_wall_latency_ms / 1000.0 AS e2e_s,
            tc.tool_internal_latency_ms / 1000.0 AS internal_s,
            GREATEST(tc.tool_wall_latency_ms - tc.tool_internal_latency_ms, 0) / 1000.0
              AS residual_s
          FROM tool_calls tc
          JOIN rounds r USING (round_pk)
          WHERE r.provider = 'codex'
            AND tc.tool_wall_latency_ms IS NOT NULL
            AND tc.tool_internal_latency_ms IS NOT NULL
            AND tc.tool_wall_latency_ms > 0
            AND tc.tool_internal_latency_ms >= 0
        ),
        grouped AS (
          SELECT
            CASE
              WHEN tool_name IN ('exec_command', 'write_stdin', 'shell_command', 'apply_patch')
                THEN tool_name
              ELSE 'other'
            END AS tool_group,
            e2e_s,
            internal_s,
            residual_s
          FROM base
        ),
        agg_all AS (
          SELECT
            0 AS sort_key,
            'All timed' AS tool_group,
            COUNT(*) AS calls,
            SUM(e2e_s) / 3600.0 AS e2e_h,
            SUM(internal_s) / 3600.0 AS internal_h,
            SUM(residual_s) / 3600.0 AS residual_h,
            AVG(residual_s) AS avg_residual_s,
            QUANTILE_CONT(residual_s, 0.50) AS p50_residual_s,
            QUANTILE_CONT(residual_s, 0.90) AS p90_residual_s,
            QUANTILE_CONT(residual_s, 0.99) AS p99_residual_s
          FROM grouped
        ),
        agg_group AS (
          SELECT
            CASE tool_group
              WHEN 'exec_command' THEN 1
              WHEN 'write_stdin' THEN 2
              WHEN 'shell_command' THEN 3
              WHEN 'apply_patch' THEN 4
              ELSE 5
            END AS sort_key,
            tool_group,
            COUNT(*) AS calls,
            SUM(e2e_s) / 3600.0 AS e2e_h,
            SUM(internal_s) / 3600.0 AS internal_h,
            SUM(residual_s) / 3600.0 AS residual_h,
            AVG(residual_s) AS avg_residual_s,
            QUANTILE_CONT(residual_s, 0.50) AS p50_residual_s,
            QUANTILE_CONT(residual_s, 0.90) AS p90_residual_s,
            QUANTILE_CONT(residual_s, 0.99) AS p99_residual_s
          FROM grouped
          WHERE tool_group IN ('exec_command', 'write_stdin', 'shell_command', 'apply_patch')
          GROUP BY tool_group
        )
        SELECT * FROM agg_all
        UNION ALL
        SELECT * FROM agg_group
        ORDER BY sort_key
    """
    return _fetch_dicts(con, query)


def _fmt_calls(value: Any) -> str:
    calls = int(value)
    if calls >= 10000:
        return f"{round(calls / 1000):,}k"
    return f"{calls / 1000:.1f}k"


def _fmt_hours(value: Any, digits: int = 1) -> str:
    return f"{float(value):,.{digits}f}"


def _fmt_seconds(value: Any, digits: int = 2) -> str:
    return f"{float(value):.{digits}f}"


def _tex_tool_label(label: str) -> str:
    if label == "All timed":
        return label
    return "\\texttt{" + label.replace("_", "\\_") + "}"


def render_paper_tex(rows: list[dict[str, Any]]) -> str:
    lines = [
        "% AUTO-GENERATED by artifacts/tool_calls/codex_wall_internal_gap/analyze.py -- do not edit",
        "% by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Codex end-to-end and internal tool latency, with positive residual statistics.}",
        "\\label{tab:codex_tool_e2e_internal}",
        "\\small",
        "\\setlength{\\tabcolsep}{1.3pt}",
        "\\renewcommand{\\arraystretch}{1.10}",
        "\\begin{tabular*}{\\columnwidth}{@{\\extracolsep{\\fill}}l r r r r r r@{}}",
        "\\toprule",
        '\\textbf{Tool} & \\textbf{Calls} & \\textbf{E2E} & \\textbf{Int.} & \\textbf{Res.} & \\textbf{Avg} & \\textbf{P50/90/99} \\\\',
        "\\midrule",
    ]
    for row in rows:
        percentiles = (
            f"{_fmt_seconds(row["p50_residual_s"])}/"
            f"{_fmt_seconds(row["p90_residual_s"])}/"
            f"{_fmt_seconds(row["p99_residual_s"], 1)}s"
        )
        residual_h = _fmt_hours(row["residual_h"], 2 if float(row["residual_h"]) < 1 else 1)
        lines.append(
            f"{_tex_tool_label(row['tool_group'])} & {_fmt_calls(row['calls'])} & "
            f"{_fmt_hours(row['e2e_h'])}h & {_fmt_hours(row['internal_h'])}h & "
            f"{residual_h}h & {_fmt_seconds(row['avg_residual_s'])}s & {percentiles} \\\\"
        )
    lines += [
        "\\bottomrule",
        "\\end{tabular*}",
        "\\end{table}",
    ]
    return "\n".join(lines) + "\n"


def direct_human_rows(con) -> list[dict[str, Any]]:
    query = """
        SELECT
          tc.tool_name,
          COUNT(*) AS calls,
          COUNT(tc.tool_wall_latency_ms) AS wall_count,
          ROUND(SUM(tc.tool_wall_latency_ms) / 1000.0, 3) AS wall_total_s,
          ROUND(AVG(tc.tool_wall_latency_ms) / 1000.0, 3) AS mean_wall_s,
          ROUND(QUANTILE_CONT(tc.tool_wall_latency_ms / 1000.0, 0.50), 3) AS p50_wall_s,
          ROUND(QUANTILE_CONT(tc.tool_wall_latency_ms / 1000.0, 0.90), 3) AS p90_wall_s,
          ROUND(QUANTILE_CONT(tc.tool_wall_latency_ms / 1000.0, 0.99), 3) AS p99_wall_s,
          SUM(CASE WHEN tc.tool_wall_latency_ms > 60000 THEN 1 ELSE 0 END) AS wall_gt_1m,
          SUM(CASE WHEN tc.tool_wall_latency_ms > 3600000 THEN 1 ELSE 0 END) AS wall_gt_1h
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE r.provider = 'codex'
          AND tc.tool_wall_latency_ms IS NOT NULL
          AND tc.tool_name IN ('request_user_input', 'wait_agent', 'send_input', 'update_plan')
        GROUP BY tc.tool_name
        ORDER BY wall_total_s DESC, calls DESC, tc.tool_name
    """
    return _fetch_dicts(con, query)


def top_gap_examples(con, limit: int) -> list[dict[str, Any]]:
    query = f"""
        SELECT
          tc.tool_name,
          tc.round_pk,
          tc.tool_index,
          tc.tool_call_id,
          CAST(tc.emitted_at AS VARCHAR) AS emitted_at,
          CAST(tc.result_at AS VARCHAR) AS result_at,
          tc.tool_wall_latency_ms AS wall_ms,
          tc.tool_internal_latency_ms AS internal_ms,
          GREATEST(tc.tool_wall_latency_ms - tc.tool_internal_latency_ms, 0) AS gap_ms,
          tc.input_chars,
          tc.result_chars,
          tc.is_error,
          r.model,
          r.session_id,
          r.round_index,
          r.trace_key
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE r.provider = 'codex'
          AND tc.tool_wall_latency_ms IS NOT NULL
          AND tc.tool_internal_latency_ms IS NOT NULL
          AND tc.tool_wall_latency_ms > 0
          AND tc.tool_internal_latency_ms >= 0
        ORDER BY gap_ms DESC
        LIMIT {int(limit)}
    """
    return _fetch_dicts(con, query)


def residual_bucket_rows(con) -> list[dict[str, Any]]:
    query = """
        WITH base AS (
          SELECT
            CASE
              WHEN tc.tool_name IN ('exec_command', 'shell_command', 'shell', 'apply_patch')
                THEN 'execution_like'
              WHEN tc.tool_name = 'write_stdin'
                THEN 'write_stdin'
              ELSE 'other_both_timed'
            END AS category,
            GREATEST(tc.tool_wall_latency_ms - tc.tool_internal_latency_ms, 0) AS gap_ms
          FROM tool_calls tc
          JOIN rounds r USING (round_pk)
          WHERE r.provider = 'codex'
            AND tc.tool_wall_latency_ms IS NOT NULL
            AND tc.tool_internal_latency_ms IS NOT NULL
            AND tc.tool_wall_latency_ms > 0
            AND tc.tool_internal_latency_ms >= 0
        ),
        bucketed AS (
          SELECT
            category,
            CASE
              WHEN gap_ms <= 1000 THEN '<=1s'
              WHEN gap_ms <= 10000 THEN '1-10s'
              WHEN gap_ms <= 60000 THEN '10s-1m'
              WHEN gap_ms <= 600000 THEN '1-10m'
              WHEN gap_ms <= 3600000 THEN '10m-1h'
              ELSE '>1h'
            END AS gap_bucket,
            CASE
              WHEN gap_ms <= 1000 THEN 1
              WHEN gap_ms <= 10000 THEN 2
              WHEN gap_ms <= 60000 THEN 3
              WHEN gap_ms <= 600000 THEN 4
              WHEN gap_ms <= 3600000 THEN 5
              ELSE 6
            END AS bucket_order,
            gap_ms
          FROM base
        ),
        agg AS (
          SELECT
            category,
            gap_bucket,
            bucket_order,
            COUNT(*) AS calls,
            ROUND(SUM(gap_ms) / 1000.0, 3) AS gap_total_s
          FROM bucketed
          GROUP BY category, gap_bucket, bucket_order
        )
        SELECT
          category,
          gap_bucket,
          calls,
          gap_total_s,
          ROUND(100.0 * gap_total_s / NULLIF(SUM(gap_total_s) OVER (PARTITION BY category), 0), 3)
            AS category_gap_share_pct
        FROM agg
        ORDER BY category, bucket_order
    """
    return _fetch_dicts(con, query)


def category_rows(coverage: list[dict[str, Any]], residuals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coverage_by_tool = {row["tool_name"]: row for row in coverage}
    residual_by_tool = {row["tool_name"]: row for row in residuals}
    categories = [
        ("direct_human", DIRECT_HUMAN_TOOLS),
        ("execution_like", EXECUTION_LIKE_TOOLS),
        ("other", set(coverage_by_tool) - DIRECT_HUMAN_TOOLS - EXECUTION_LIKE_TOOLS),
    ]
    rows: list[dict[str, Any]] = []
    for category, tools in categories:
        cov = [coverage_by_tool[t] for t in tools if t in coverage_by_tool]
        res = [residual_by_tool[t] for t in tools if t in residual_by_tool]
        wall_total = sum(float(row["wall_total_s"] or 0) for row in cov)
        internal_total = sum(float(row["internal_total_s"] or 0) for row in cov)
        gap_total = sum(float(row["gap_total_s"] or 0) for row in res)
        rows.append(
            {
                "category": category,
                "tools": ";".join(sorted(tools & set(coverage_by_tool))),
                "calls": sum(int(row["calls"]) for row in cov),
                "wall_count": sum(int(row["wall_count"]) for row in cov),
                "internal_count": sum(int(row["internal_count"]) for row in cov),
                "both_count": sum(int(row["both_count"]) for row in cov),
                "wall_only_count": sum(int(row["wall_only_count"]) for row in cov),
                "wall_total_s": round(wall_total, 3),
                "internal_total_s": round(internal_total, 3),
                "gap_total_s": round(gap_total, 3),
                "gap_share_of_wall_pct": _round_or_none(100.0 * gap_total / wall_total if wall_total else None),
            }
        )
    return rows


def write_analysis(
    path: Path,
    coverage: list[dict[str, Any]],
    residuals: list[dict[str, Any]],
    direct_human: list[dict[str, Any]],
    categories: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
) -> None:
    by_tool = {row["tool_name"]: row for row in residuals}
    coverage_by_tool = {row["tool_name"]: row for row in coverage}
    direct_by_tool = {row["tool_name"]: row for row in direct_human}
    exec_row = by_tool.get("exec_command", {})
    write_row = by_tool.get("write_stdin", {})
    request_row = direct_by_tool.get("request_user_input", {})
    execution_cat = next((row for row in categories if row["category"] == "execution_like"), {})
    execution_buckets = [row for row in buckets if row["category"] == "execution_like"]
    execution_gt_1m_calls = sum(
        int(row["calls"]) for row in execution_buckets if row["gap_bucket"] in {"1-10m", "10m-1h", ">1h"}
    )
    execution_gt_1m_gap_s = sum(
        float(row["gap_total_s"]) for row in execution_buckets if row["gap_bucket"] in {"1-10m", "10m-1h", ">1h"}
    )
    execution_gap_s = float(execution_cat.get("gap_total_s") or 0)

    def hours(seconds: Any) -> float:
        return float(seconds or 0) / 3600.0

    lines = [
        "# Codex wall/internal tool-latency gap",
        "",
        "This audit compares Codex trace-observed wall time with runner-reported internal time.",
        "`gap = max(tool_wall_latency_ms - tool_internal_latency_ms, 0)`.",
        "",
        "## Main numbers",
        "",
        (
            f"- `exec_command`: {int(exec_row.get('calls', 0)):,} calls with both timings, "
            f"{hours(exec_row.get('gap_total_s')):.2f} h residual gap, "
            f"{exec_row.get('gap_share_of_wall_pct')}% of its wall time; median gap "
            f"{exec_row.get('p50_gap_s')} s, p90 {exec_row.get('p90_gap_s')} s, "
            f"p99 {exec_row.get('p99_gap_s')} s."
        ),
        (
            f"- execution-like tools (`exec_command`, `shell_command`, `shell`, `apply_patch`): "
            f"{int(execution_cat.get('calls', 0)):,} calls total, "
            f"{hours(execution_cat.get('gap_total_s')):.2f} h residual gap. "
            f"Only tools with both timings can contribute to this residual."
        ),
        (
            f"- Execution-like residuals above 1 minute: {execution_gt_1m_calls:,} calls, "
            f"{hours(execution_gt_1m_gap_s):.2f} h, "
            f"{_round_or_none(100.0 * execution_gt_1m_gap_s / execution_gap_s if execution_gap_s else None)}% "
            "of the execution-like residual gap."
        ),
        (
            f"- `request_user_input`: {int(request_row.get('wall_count', 0)):,} wall-timed calls, "
            f"{hours(request_row.get('wall_total_s')):.2f} h trace-observed wait; median "
            f"{request_row.get('p50_wall_s')} s, p90 {request_row.get('p90_wall_s')} s."
        ),
        (
            f"- `write_stdin`: {int(write_row.get('calls', 0)):,} calls with both timings, "
            f"{hours(write_row.get('gap_total_s')):.2f} h residual gap, "
            f"{write_row.get('gap_share_of_wall_pct')}% of its wall time."
        ),
        "",
        "## Interpretability limits",
        "",
        "- The normalized trace keeps `input_chars` and `result_chars`, but not actual tool inputs, "
        "tool outputs, or explicit approval events.",
        "- There are generic `tool_call` and `tool_result` timing events, but no approval-start, "
        "approval-end, or auto-approval marker.",
        "- Therefore the residual is a strong upper-bound signal for client-side waiting around the "
        "tool call. It is not a direct measurement of human approval time.",
        "- Auto-approval latency is not separately identifiable in this trace. At best, one could "
        "infer that ordinary low-residual calls include auto-approved or no-approval cases.",
        "",
        "## Coverage",
        "",
        (
            f"- `exec_command`: {int(coverage_by_tool.get('exec_command', {}).get('both_count', 0)):,} "
            f"both-timed calls out of {int(coverage_by_tool.get('exec_command', {}).get('calls', 0)):,}."
        ),
        (
            f"- `request_user_input`: {int(coverage_by_tool.get('request_user_input', {}).get('wall_count', 0)):,} "
            f"wall-timed calls and {int(coverage_by_tool.get('request_user_input', {}).get('internal_count', 0)):,} "
            "internal-timed calls."
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument("--top-gap-examples", type=int, default=50)
    args = parser.parse_args()

    con = _connect(args)
    out_dir = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    coverage = coverage_rows(con)
    residuals = residual_rows(con)
    direct_human = direct_human_rows(con)
    examples = top_gap_examples(con, args.top_gap_examples)
    categories = category_rows(coverage, residuals)
    buckets = residual_bucket_rows(con)
    paper_rows = paper_table_rows(con)

    _write_csv(
        out_dir / "codex_tool_timing_coverage.csv",
        coverage,
        [
            "tool_name",
            "calls",
            "wall_count",
            "internal_count",
            "both_count",
            "wall_only_count",
            "wall_total_s",
            "internal_total_s",
        ],
    )
    _write_csv(
        out_dir / "codex_wall_internal_gap_by_tool.csv",
        residuals,
        [
            "tool_name",
            "calls",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_share_of_wall_pct",
            "mean_gap_s",
            "p50_gap_s",
            "p90_gap_s",
            "p99_gap_s",
            "gap_gt_1s",
            "gap_gt_10s",
            "gap_gt_1m",
            "gap_gt_10m",
            "gap_gt_1h",
        ],
    )
    _write_csv(
        out_dir / "codex_direct_human_wall_time.csv",
        direct_human,
        [
            "tool_name",
            "calls",
            "wall_count",
            "wall_total_s",
            "mean_wall_s",
            "p50_wall_s",
            "p90_wall_s",
            "p99_wall_s",
            "wall_gt_1m",
            "wall_gt_1h",
        ],
    )
    _write_csv(
        out_dir / "codex_wall_internal_gap_by_category.csv",
        categories,
        [
            "category",
            "tools",
            "calls",
            "wall_count",
            "internal_count",
            "both_count",
            "wall_only_count",
            "wall_total_s",
            "internal_total_s",
            "gap_total_s",
            "gap_share_of_wall_pct",
        ],
    )
    _write_csv(
        out_dir / "codex_wall_internal_gap_buckets.csv",
        buckets,
        [
            "category",
            "gap_bucket",
            "calls",
            "gap_total_s",
            "category_gap_share_pct",
        ],
    )
    _write_csv(
        out_dir / "codex_top_wall_internal_gap_examples.csv",
        examples,
        [
            "tool_name",
            "round_pk",
            "tool_index",
            "tool_call_id",
            "emitted_at",
            "result_at",
            "wall_ms",
            "internal_ms",
            "gap_ms",
            "input_chars",
            "result_chars",
            "is_error",
            "model",
            "session_id",
            "round_index",
            "trace_key",
        ],
    )
    write_analysis(out_dir / "result_analysis.md", coverage, residuals, direct_human, categories, buckets)
    (out_dir / "codex_tool_e2e_internal.tex").write_text(
        render_paper_tex(paper_rows), encoding="utf-8"
    )

    print(f"Wrote Codex wall/internal gap audit to {out_dir}")


if __name__ == "__main__":
    main()
