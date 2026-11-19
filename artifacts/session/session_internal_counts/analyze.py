#!/usr/bin/env python3
"""Per-session, reconstructed-request, and per-round count distributions.

Request ownership comes from request_timing.py, shared with timing and cost.
Native turn identity takes priority; legacy groups remain countable without a
measurable duration. Missing completion, zero duration, and compressed timestamps
do not erase workload counts."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # session_internal_counts -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402  (gfm_table / section_tables for the web detail page)
import trace_db  # noqa: E402

SCOPES = ("merged", "claude", "codex")
PERCENTILES = (25, 50, 90, 99)


def _load_module(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DEC = _load_module(
    "sic_turn_decomposition",
    "artifacts/human_in_the_loop/user_turn_decomposition/analyze.py",
)


def collect(con) -> dict[str, dict[str, list[float]]]:
    """Return ``{scope: {metric: [values...]}}`` for the table rows.

    Per-session metrics are one value per session; ``*_per_request`` are one value per turn;
    ``tool_calls_per_step`` is one value per round.
    """
    # --- per-session step-trigger counts + tool calls (SQL) ---
    sessions: dict[tuple[str, str], dict[str, Any]] = {}

    def session(provider: str, sid: str) -> dict[str, Any]:
        return sessions.setdefault(
            (provider, sid),
            {"provider": provider, "user_steps": 0, "tool_steps": 0, "tool_calls": 0, "turns": 0},
        )

    for provider, sid, first_event, count in con.execute(
        "SELECT provider, session_id, first_input_event_type, count(*) FROM rounds "
        "WHERE session_id IS NOT NULL AND session_id <> '' GROUP BY 1, 2, 3"
    ).fetchall():
        record = session(provider, sid)
        if first_event == "user_message":
            record["user_steps"] += count
        elif first_event == "tool_result":
            record["tool_steps"] += count

    for sid, provider in con.execute(
        "SELECT DISTINCT session_id, provider FROM rounds "
        "WHERE session_id IS NOT NULL AND session_id <> ''"
    ).fetchall():
        session(provider, sid)

    for provider, sid, calls in con.execute(
        "SELECT r.provider, r.session_id, count(*) FROM tool_calls tc JOIN rounds r USING (round_pk) "
        "WHERE r.session_id IS NOT NULL AND r.session_id <> '' GROUP BY 1, 2"
    ).fetchall():
        session(provider, sid)["tool_calls"] = int(calls)

    # Request ownership is shared with timing/cost. Legacy groups stay countable without a duration.
    from request_timing import collect_requests
    tools_by_round = DEC.load_round_tools(con)
    requests = collect_requests(con)
    request_by_round = {rpk: index for index, request in enumerate(requests) for rpk in request.round_pks}
    request_counts = [{"tool_calls": 0, "tool_steps": 0, "user_steps": 0} for _ in requests]
    per_request = {scope: {"tool_calls": [], "tool_steps": [], "user_steps": []} for scope in SCOPES}
    per_step = {scope: [] for scope in SCOPES}
    for rpk, provider, sid, first_event in con.execute(
        "SELECT round_pk, provider, session_id, first_input_event_type FROM rounds ORDER BY round_pk"
    ).fetchall():
        n_tools = tools_by_round[rpk].tool_calls if rpk in tools_by_round else 0
        for scope in ("merged", provider):
            if scope in per_step:
                per_step[scope].append(n_tools)
        index = request_by_round.get(rpk)
        if index is not None:
            record = request_counts[index]
            record["tool_calls"] += n_tools
            record["user_steps"] += first_event == "user_message"
            record["tool_steps"] += first_event == "tool_result"
    for request, record in zip(requests, request_counts):
        if not request.round_pks:
            continue
        key = (request.provider, request.session_id)
        if key in sessions:
            sessions[key]["turns"] += 1
        for scope in ("merged", request.provider):
            if scope in per_request:
                for key, value in record.items():
                    per_request[scope][key].append(value)

    # --- assemble per-scope arrays ---
    out: dict[str, dict[str, list[float]]] = {}
    for scope in SCOPES:
        members = [
            record
            for record in sessions.values()
            if scope == "merged" or record["provider"] == scope
        ]
        out[scope] = {
            "requests": [r["turns"] for r in members],
            "user_steps": [r["user_steps"] for r in members],
            "tool_steps": [r["tool_steps"] for r in members],
            "tool_calls": [r["tool_calls"] for r in members],
            "user_steps_per_request": per_request[scope]["user_steps"],
            "tool_steps_per_request": per_request[scope]["tool_steps"],
            "tool_calls_per_request": per_request[scope]["tool_calls"],
            "tool_calls_per_step": per_step[scope],
        }
    return out


def summarize(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    row = {"n": int(arr.size), "avg": float(arr.mean()) if arr.size else 0.0}
    for q, v in zip(PERCENTILES, np.percentile(arr, PERCENTILES) if arr.size else [0] * 4):
        row[f"p{q}"] = float(v)
    return row


# Display order: (metric key, row label, group). Group drives the sub-header bands.
ROWS = [
    ("requests", "Requests", "session"),
    ("user_steps", "User-initiated steps", "session"),
    ("tool_steps", "Tool-initiated steps", "session"),
    ("tool_calls", "Tool calls", "session"),
    ("user_steps_per_request", "User-initiated steps", "request"),
    ("tool_steps_per_request", "Tool-initiated steps", "request"),
    ("tool_calls_per_request", "Tool calls", "request"),
    ("tool_calls_per_step", "Tool calls", "step"),
]
GROUP_LABEL = {"session": "Per session", "request": "Per request", "step": "Per step"}
GROUP_UNIT = {"session": "/session", "request": "/request", "step": "/step"}


def _avg(v: float) -> str:
    return f"{v:,.1f}"


def _pct(v: float) -> str:
    return f"{round(v):,}"


def render_tex(merged: dict[str, dict[str, float]]) -> str:
    lines = [
        "% AUTO-GENERATED by artifacts/session/session_internal_counts/analyze.py -- do not edit",
        "% by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Per-session, per-request, and per-step count distributions across the coding-agent trace.}",
        "\\label{tab:session_internal_counts}",
        "\\small",
        "\\setlength{\\tabcolsep}{6pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Avg} & \\textbf{P25} & \\textbf{P50} & \\textbf{P90} & \\textbf{P99} \\\\",
        "\\midrule",
    ]
    current_group = None
    for key, label, group in ROWS:
        if group != current_group:
            if current_group is not None:
                lines.append("\\addlinespace")
            lines.append(f"\\multicolumn{{6}}{{@{{}}l}}{{\\emph{{{GROUP_LABEL[group]}}}}} \\\\")
            current_group = group
        marker = "$^{\\dagger}$" if key == "user_steps" else ""
        s = merged[key]
        lines.append(
            f"\\quad {label}{marker} & {_avg(s['avg'])} & {_pct(s['p25'])} & {_pct(s['p50'])} "
            f"& {_pct(s['p90'])} & {_pct(s['p99'])} \\\\"
        )
    lines += [
        "\\bottomrule",
        "\\end{tabular}",
        "\\smallskip",
        "{\\footnotesize\\raggedright $^{\\dagger}$ A \\emph{user-initiated step} is a step "
        "whose first input event is a user message. This need not equal the number of user "
        "messages: (i)~a message the user sends while the agent is still working is delivered "
        "together with the next tool result, so that step is counted as tool-initiated; and "
        "(ii)~a message that never triggers another model call is not counted as a step at "
        "all.\\par}",
        "\\end{table}",
    ]
    return "\n".join(lines) + "\n"


def render_md(merged: dict[str, dict[str, float]]) -> str:
    """GFM mirror of :func:`render_tex` for the web detail page (table only — the page adds a caption).

    Each LaTeX ``\\multicolumn`` band (Per session / Per request / Per step) becomes its own GFM
    table under a bold heading, since GFM cannot span columns.
    """
    headers = ["Metric", "Avg", "P25", "P50", "P90", "P99"]
    sections: list[tuple[str, list[list[str]]]] = []
    current_group = None
    rows: list[list[str]] = []
    for key, label, group in ROWS:
        if group != current_group:
            if current_group is not None:
                sections.append((GROUP_LABEL[current_group], rows))
            rows = []
            current_group = group
        marker = "†" if key == "user_steps" else ""
        s = merged[key]
        rows.append(
            [f"{label}{marker}", _avg(s["avg"]), _pct(s["p25"]), _pct(s["p50"]), _pct(s["p90"]), _pct(s["p99"])]
        )
    if current_group is not None:
        sections.append((GROUP_LABEL[current_group], rows))
    return md_table.section_tables(headers, sections, ["l", "r", "r", "r", "r", "r"])


def render_headline(merged: dict[str, dict[str, float]]) -> list[dict[str, str]]:
    """The 2–4 most important numbers for the Overview gallery card (label + formatted value).

    Derived from the same merged summaries as the table so the card never drifts. Consumed by
    web/scripts/build-payload.mjs (aggregated into public/data/headlines.json by experiment slug).
    """
    return [
        {"label": "Requests / session", "value": _avg(merged["requests"]["avg"])},
        {"label": "Tool calls / request", "value": _avg(merged["tool_calls_per_request"]["avg"])},
        {"label": "Tool calls / step", "value": _avg(merged["tool_calls_per_step"]["avg"])},
    ]


def render_table(data: dict[str, dict[str, dict[str, float]]]) -> str:
    out: list[str] = []
    for scope in SCOPES:
        n_sessions = data[scope]["requests"]["n"]
        out.append(f"[{scope}]  ({n_sessions:,} sessions)")
        out.append(f"  {'metric':24s} {'unit':9s} {'avg':>10s} {'p25':>8s} {'p50':>8s} {'p90':>8s} {'p99':>8s}")
        for key, label, group in ROWS:
            s = data[scope][key]
            out.append(
                f"  {label:24s} {GROUP_UNIT[group]:9s} {s['avg']:>10.2f} {s['p25']:>8.1f} "
                f"{s['p50']:>8.1f} {s['p90']:>8.1f} {s['p99']:>8.1f}"
            )
        out.append("")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    try:
        raw = collect(con)
        from request_timing import request_ownership_coverage
        coverage = request_ownership_coverage(con)
    finally:
        con.close()

    data = {scope: {key: summarize(vals) for key, vals in metrics.items()} for scope, metrics in raw.items()}

    from request_timing import ownership_coverage_note, render_ownership_coverage
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "session_internal_counts.tex"
    tex_path.write_text(render_tex(data["merged"]) + "\n% " + ownership_coverage_note(coverage) + "\n", encoding="utf-8")
    md_path = out_dir / "session_internal_counts.md"
    md_path.write_text(render_md(data["merged"]) + "\n\n" + render_ownership_coverage(coverage), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data["merged"]), indent=2) + "\n", encoding="utf-8")

    print(render_table(data))
    print(f"Wrote {tex_path}, {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
