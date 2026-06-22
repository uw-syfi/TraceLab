#!/usr/bin/env python3
"""Per-session / per-request / per-step count distributions for the paper's session table.

Populates ``tab:session_internal_counts`` (``src/04_SessionContext.tex``): how many
requests, steps, and tool calls a coding session contains; how many tool-initiated steps and
tool calls a single request drives; and how many tool calls a single step issues. Reported as
avg / p25 / p50 / p90 / p99.

Definitions (reused from the canonical experiments so the numbers reconcile):

* A **session** is a non-empty ``session_id`` (the grouping used by ``session_token_steps`` and
  the overview; 4,265 sessions in the public trace).
* A **request** is one user turn -- a response-triggering ``user_message`` to the next one in
  the same session -- replaying the exact turn state machine from
  ``human_in_the_loop/user_turn_decomposition`` (identical boundaries to
  ``user_turn_response_time``). Turns with no response-end event or non-positive duration are
  dropped, matching those experiments.
* A **step** is one LLM round. ``user-initiated`` / ``tool-initiated`` split on the round's
  ``first_input_event_type`` (``user_message`` vs ``tool_result``), the same trigger field the
  loader's ``is_user_input`` uses.
* **Tool calls** are ``tool_calls`` rows; per session = all of a session's calls, per request =
  the calls inside one turn, per step = the calls inside one round.

Run with the standard trace-db CLI (``--db`` | ``-i/--input`` | ``-o/--output-dir``).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # session_internal_counts -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

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
    sessions: dict[str, dict[str, Any]] = {}

    def session(sid: str) -> dict[str, Any]:
        return sessions.setdefault(
            sid,
            {"provider": None, "user_steps": 0, "tool_steps": 0, "tool_calls": 0, "turns": 0},
        )

    for sid, first_event, count in con.execute(
        "SELECT session_id, first_input_event_type, count(*) FROM rounds "
        "WHERE session_id IS NOT NULL AND session_id <> '' GROUP BY 1, 2"
    ).fetchall():
        record = session(sid)
        if first_event == "user_message":
            record["user_steps"] += count
        elif first_event == "tool_result":
            record["tool_steps"] += count

    for sid, provider in con.execute(
        "SELECT DISTINCT session_id, provider FROM rounds "
        "WHERE session_id IS NOT NULL AND session_id <> ''"
    ).fetchall():
        session(sid)["provider"] = provider

    for sid, calls in con.execute(
        "SELECT r.session_id, count(*) FROM tool_calls tc JOIN rounds r USING (round_pk) "
        "WHERE r.session_id IS NOT NULL AND r.session_id <> '' GROUP BY 1"
    ).fetchall():
        session(sid)["tool_calls"] = int(calls)

    # --- per-turn / per-step counts (turn state machine + per-round tool counts) ---
    events_by_round = DEC.load_timing_events(con)
    tools_by_round = DEC.load_round_tools(con)
    active: dict[str, dict[str, Any]] = {}
    per_request: dict[str, dict[str, list[float]]] = {
        scope: {"tool_calls": [], "tool_steps": [], "user_steps": []} for scope in SCOPES
    }
    per_step: dict[str, list[float]] = {scope: [] for scope in SCOPES}

    def close(sid: str) -> None:
        turn = active.pop(sid, None)
        if turn is None or turn["end"] is None:
            return
        if (turn["end"] - turn["start"]).total_seconds() <= 0:
            return
        if sid in sessions:
            sessions[sid]["turns"] += 1
        for scope in ("merged", turn["provider"]):
            bucket = per_request.get(scope)
            if bucket is not None:
                bucket["tool_calls"].append(turn["tools"])
                bucket["tool_steps"].append(turn["tool_steps"])
                bucket["user_steps"].append(turn["user_steps"])

    for round_pk, provider, sid, first_event in con.execute(
        "SELECT round_pk, provider, session_id, first_input_event_type FROM rounds ORDER BY round_pk"
    ).fetchall():
        # per-step tool calls: one value per round (0 for text-only rounds), all rounds.
        round_tools = tools_by_round.get(round_pk)
        n_tools = round_tools.tool_calls if round_tools is not None else 0
        per_step["merged"].append(n_tools)
        if provider in per_step:
            per_step[provider].append(n_tools)

        events = events_by_round.get(round_pk, [])
        sid = sid if isinstance(sid, str) and sid else None
        start = DEC.response_trigger_user_message_timestamp(events)
        if start is not None and sid is not None:
            close(sid)
            active[sid] = {
                "provider": provider, "start": start, "end": None,
                "tools": 0, "tool_steps": 0, "user_steps": 0,
            }
        turn = active.get(sid) if sid is not None else None
        if turn is None:
            continue
        if first_event == "user_message":
            turn["user_steps"] += 1
        elif first_event == "tool_result":
            turn["tool_steps"] += 1
        response_end = DEC.last_response_end_timestamp(events)
        if response_end is not None and (turn["end"] is None or response_end > turn["end"]):
            turn["end"] = response_end
        if round_tools is not None:
            turn["tools"] += round_tools.tool_calls
    for sid in list(active):
        close(sid)

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
    finally:
        con.close()

    data = {scope: {key: summarize(vals) for key, vals in metrics.items()} for scope, metrics in raw.items()}

    out_path = Path(args.output_dir) / "session_internal_counts.tex"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_tex(data["merged"]), encoding="utf-8")

    print(render_table(data))
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
