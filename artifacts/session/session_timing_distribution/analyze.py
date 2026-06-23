#!/usr/bin/env python3
"""Per-session / per-request / per-step *time* distributions for the paper's Timing
Distribution table (``tab:timing_distribution`` in ``src/04_SessionContext.tex``).

This is the time-domain sibling of ``session_cost_distribution`` and answers: of the wall-clock
time a coding agent consumes, how much is the human thinking, the LLM generating, and the tools
executing? For each granularity we report avg / p50 / p90 / p99 per unit plus each category's
share of the total (the same Avg/P50/P90/P99 + %% layout as the cost table).

The category set differs by granularity, because **human thinking is a between-request quantity**:

* **Per session** -- ``Total elapsed`` = wall-clock span (first to last timing event of the
  session), with ``Human thinking`` + ``LLM generation`` + ``Tool execution`` shares of it.
* **Per session, human capped (1h)** -- the same session units, but human idle is re-summed with
  each gap clamped to one hour (a prompt-cache TTL horizon) and the block total is
  ``capped human + generation + tool``. This drops the multi-day abandoned-session tail so the
  engaged-time split is visible (and shares partition cleanly).
* **Per request** -- ``Total (response time)`` = turn e2e (response-trigger user message to last
  response-end output), with ``LLM generation`` + ``Tool execution`` shares. No human term: human
  wait sits *between* requests, never inside one.
* **Per step** -- ``LLM generation`` vs ``Tool execution`` only; one LLM round has no human term
  and no clean e2e total.
* **Per individual latency** -- the event-level distributions behind the CDF/summary views:
  strictly-positive human-input waits, positive per-round generation spans, and positive per-tool
  effective latencies.

Shares need not sum to 100%: summed per-round generation and per-tool effective latency can
overlap (concurrent tools, generation streaming during a tool call), so they can slightly exceed
or fall short of the measured total.

Definitions reuse the canonical timing experiments so the numbers reconcile:

* **LLM generation** (per step) -- observable generation span, latest qualifying input event to
  last model-output event; identical to ``llm_generation/generation_time_cdf`` and the per-round
  generation in ``human_in_the_loop/user_turn_decomposition``.
* **Tool execution** (per step) -- sum of effective tool latency (``tool_internal_latency_ms``
  else ``tool_wall_latency_ms``), only strictly-positive; identical to
  ``tool_calls/tool_latency_distribution``.
* **Human thinking** (per session) -- sum of human-input waits, the gap from the previous event of
  any type (including Codex ``usage_report``) to each user message; identical to
  ``human_in_the_loop/human_input_wait`` (provider-agnostic definition).
* **Request e2e** matches ``user_turn_decomposition`` turn-for-turn. Summed per-round generation
  and per-tool effective latency can overlap (concurrent tools, generation that streams while a
  tool runs), so their shares can exceed the measured e2e.

A **request** is one user turn -- the same turn state machine as
``human_in_the_loop/user_turn_decomposition`` (identical to ``user_turn_response_time``,
``session_internal_counts``, and ``session_cost_distribution``). A **step** is one LLM round; a
**session** is one ``session_id``.

Run with the standard trace-db CLI (``--db`` | ``-i/--input`` | ``-o/--output-dir``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # session_timing_distribution -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402  (gfm_table / section_tables for the web detail page)
import trace_db  # noqa: E402
from timing import human_waits_from_event_pairs  # noqa: E402

SCOPES = ("merged", "claude", "codex")
PERCENTILES = (25, 50, 90, 99)
# (granularity key, block label). The capped-session block reuses the session units but recomputes
# its total with human idle clamped per gap (see UNIT_KEY_BY_GRAN / HUMAN_CAP_SECONDS).
GRANULARITIES = (
    ("session", "Per session"),
    ("session_capped", "Per session, human capped (1h)"),
    ("request", "Per request"),
    ("step", "Per step"),
    ("individual", "Per individual latency"),
)
# Which per-unit list each block draws from (the capped block reuses the session units).
UNIT_KEY_BY_GRAN = {
    "session": "session",
    "session_capped": "session",
    "request": "request",
    "step": "step",
}
# Per-gap cap for the capped-session block: idle past one prompt-cache TTL horizon is moot for
# serving (the KV/prefix cache is already cold), so each gap contributes min(gap, 1h).
HUMAN_CAP_SECONDS = 3600.0

# (key, label, share?) per granularity. The "total" row carries no share cell; the remaining
# categories each report their share of the block's total time.
SESSION_CATS = (
    ("total", "Total elapsed", False),
    ("human", "Human thinking", True),
    ("gen", "LLM generation", True),
    ("tool", "Tool execution", True),
)
# Capped-session block: total = capped human + generation + tool, so shares partition cleanly and
# the abandoned-session idle tail no longer drowns out generation/tool.
SESSION_CAPPED_CATS = (
    ("total_capped", "Total", False),
    ("human_capped", "Human thinking", True),
    ("gen", "LLM generation", True),
    ("tool", "Tool execution", True),
)
REQUEST_CATS = (
    ("total", "Total (response time)", False),
    ("gen", "LLM generation", True),
    ("tool", "Tool execution", True),
)
STEP_CATS = (
    ("gen", "LLM generation", True),
    ("tool", "Tool execution", True),
)
INDIVIDUAL_CATS = (
    ("human", "Human thinking", False),
    ("gen", "LLM generation", False),
    ("tool", "Tool execution", False),
)
CATS_BY_GRAN = {
    "session": SESSION_CATS,
    "session_capped": SESSION_CAPPED_CATS,
    "request": REQUEST_CATS,
    "step": STEP_CATS,
    "individual": INDIVIDUAL_CATS,
}


def _load_module(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Reuse the turn state machine + per-round timing/tool helpers verbatim so every span matches.
DEC = _load_module(
    "std_turn_decomposition",
    "artifacts/human_in_the_loop/user_turn_decomposition/analyze.py",
)


def collect(con) -> dict[str, dict[str, Any]]:
    """One stateful pass over rounds (ingestion order). Returns ``units[scope][granularity]``,
    a list of per-unit time dicts in seconds."""
    events_by_round = DEC.load_timing_events(con)
    tools_by_round = DEC.load_round_tools(con)
    rows = con.execute(
        "SELECT round_pk, provider, session_id FROM rounds ORDER BY round_pk"
    ).fetchall()

    units: dict[str, dict[str, Any]] = {
        s: {"session": [], "human_wait": [], "gen_observed": [], "tool_call": [], "request": [], "step": []}
        for s in SCOPES
    }

    eff = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL
    for provider, eff_ms in con.execute(
        f"""
        SELECT COALESCE(r.provider, '<unknown-provider>') AS provider, ({eff}) AS eff_ms
        FROM tool_calls tc JOIN rounds r USING (round_pk)
        WHERE ({eff}) IS NOT NULL AND ({eff}) > 0
        ORDER BY tc.round_pk, tc.tool_index
        """
    ).fetchall():
        unit = {"tool": float(eff_ms) / 1000.0}
        for scope in ("merged", provider):
            if scope in units:
                units[scope]["tool_call"].append(unit)
    # Per-session accumulator: provider, summed gen/tool/human, wall-clock min/max event ts.
    sess: dict[str, dict] = {}
    last_event_at: dict[str, Any] = {}  # session_id -> last event datetime (any type; for human wait)
    active: dict[str, dict] = {}  # session_id -> open turn

    def close_turn(sid: str) -> None:
        turn = active.pop(sid, None)
        if turn is None or turn["end"] is None:
            return
        e2e = (turn["end"] - turn["start"]).total_seconds()
        if e2e <= 0:
            return
        unit = {"e2e": e2e, "gen": turn["gen"], "tool": turn["tool"]}
        for scope in ("merged", turn["provider"]):
            if scope in units:
                units[scope]["request"].append(unit)

    for rpk, prov, sid in rows:
        sid = sid if isinstance(sid, str) and sid else None
        provider = prov if prov else "<unknown-provider>"
        events = events_by_round.get(rpk, [])

        gen = DEC.input_to_last_output_span_seconds(events) or 0.0
        rtools = tools_by_round.get(rpk)
        tool = rtools.tool_effective_seconds if rtools is not None else 0.0

        if gen > 0:
            gen_unit = {"gen": gen}
            for scope in ("merged", provider):
                if scope in units:
                    units[scope]["gen_observed"].append(gen_unit)

        # --- per step (every round) ---
        step_unit = {"gen": gen, "tool": tool}
        for scope in ("merged", provider):
            if scope in units:
                units[scope]["step"].append(step_unit)

        # --- request turn state machine (mirrors user_turn_decomposition) ---
        start = DEC.response_trigger_user_message_timestamp(events)
        if start is not None and sid is not None:
            close_turn(sid)
            active[sid] = {"provider": provider, "start": start, "end": None, "gen": 0.0, "tool": 0.0}

        # --- session: human wait (provider-agnostic: gap from the previous event of any type to
        #     each user_message; see timing.human_waits_from_event_pairs), plus gen/tool sums and
        #     wall-clock span ---
        if sid is not None:
            acc = sess.setdefault(
                sid,
                {"provider": provider, "gen": 0.0, "tool": 0.0, "human": 0.0,
                 "human_capped": 0.0, "first": None, "last": None},
            )
            waits, _n_user, last_ev = human_waits_from_event_pairs(events, last_event_at.get(sid))
            acc["human"] += sum(waits)
            acc["human_capped"] += sum(min(w, HUMAN_CAP_SECONDS) for w in waits)
            for wait in waits:
                wait_unit = {"human": wait, "human_capped": min(wait, HUMAN_CAP_SECONDS)}
                for scope in ("merged", provider):
                    if scope in units:
                        units[scope]["human_wait"].append(wait_unit)
            if last_ev is not None:
                last_event_at[sid] = last_ev
            acc["gen"] += gen
            acc["tool"] += tool
            # wall-clock span: min/max over all of this round's timing events
            ev_ts = [ts for _, ts in events if ts is not None]
            if ev_ts:
                lo, hi = min(ev_ts), max(ev_ts)
                acc["first"] = lo if acc["first"] is None else min(acc["first"], lo)
                acc["last"] = hi if acc["last"] is None else max(acc["last"], hi)

        # advance the open turn with this round's outputs/tools
        turn = active.get(sid) if sid is not None else None
        if turn is not None:
            turn["gen"] += gen
            turn["tool"] += tool
            response_end = DEC.last_response_end_timestamp(events)
            if response_end is not None and (turn["end"] is None or response_end > turn["end"]):
                turn["end"] = response_end

    for sid in list(active):
        close_turn(sid)

    # finalize per-session units (wall-clock requires both ends)
    for acc in sess.values():
        if acc["first"] is None or acc["last"] is None:
            continue
        wall = (acc["last"] - acc["first"]).total_seconds()
        if wall <= 0:
            continue
        unit = {
            "wall": wall, "human": acc["human"], "human_capped": acc["human_capped"],
            "gen": acc["gen"], "tool": acc["tool"],
        }
        for scope in ("merged", acc["provider"]):
            if scope in units:
                units[scope]["session"].append(unit)

    return units


def unit_list_for(scope: dict[str, Any], granularity: str, category: str) -> list[dict]:
    if granularity == "individual":
        return scope[{"human": "human_wait", "gen": "gen_observed", "tool": "tool_call"}[category]]
    return scope[UNIT_KEY_BY_GRAN[granularity]]


def series(unit_list: list[dict], granularity: str, category: str) -> list[float]:
    """One number per unit for ``category`` at ``granularity`` (all seconds)."""
    if granularity == "session":
        if category == "total":
            return [u["wall"] for u in unit_list]
        return [u[category] for u in unit_list]  # human / gen / tool
    if granularity == "session_capped":
        if category == "total_capped":
            return [u["human_capped"] + u["gen"] + u["tool"] for u in unit_list]
        return [u[category] for u in unit_list]  # human_capped / gen / tool
    if granularity == "individual":
        return [u[category] for u in unit_list]  # human / gen / tool from per-category lists
    if granularity == "request":
        if category == "total":
            return [u["e2e"] for u in unit_list]
        return [u[category] for u in unit_list]  # gen / tool
    # step: gen / tool
    return [u[category] for u in unit_list]


def stats(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if not arr.size:
        return {"avg": 0.0, "p25": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0}
    row = {"avg": float(arr.mean())}
    row.update({f"p{q}": float(v) for q, v in zip(PERCENTILES, np.percentile(arr, PERCENTILES))})
    return row


def shares(unit_list: list[dict], granularity: str) -> dict[str, float]:
    """Aggregate share of total time for each non-total category (Σ category / Σ total)."""
    if granularity == "session":
        total = sum(u["wall"] for u in unit_list)
        if total <= 0:
            return {}
        comp = {k: sum(u[k] for u in unit_list) for k in ("human", "gen", "tool")}
    elif granularity == "session_capped":
        total = sum(u["human_capped"] + u["gen"] + u["tool"] for u in unit_list)
        if total <= 0:
            return {}
        comp = {k: sum(u[k] for u in unit_list) for k in ("human_capped", "gen", "tool")}
    elif granularity == "individual":
        return {}
    elif granularity == "request":
        total = sum(u["e2e"] for u in unit_list)
        if total <= 0:
            return {}
        comp = {k: sum(u[k] for u in unit_list) for k in ("gen", "tool")}
    else:  # step
        total = sum(u["gen"] + u["tool"] for u in unit_list)
        if total <= 0:
            return {}
        comp = {k: sum(u[k] for u in unit_list) for k in ("gen", "tool")}
    return {k: v / total for k, v in comp.items()}


# ----- formatting -----
def dur(x: float) -> str:
    """Adaptive duration with unit suffix: <60 s, <60 m, else h (signed)."""
    a = abs(x)
    if a >= 3600:
        v, unit = x / 3600, "h"
    elif a >= 60:
        v, unit = x / 60, "m"
    else:
        v, unit = x, "s"
    if abs(v) < 0.05:  # avoid printing "-0.0"
        v = 0.0
    return f"{v:.1f}{unit}"


def pct(x: float) -> str:
    return f"{x * 100:.1f}\\%"


def render_tex(units: dict[str, dict[str, Any]]) -> str:
    scope = units["merged"]
    lines = [
        "% AUTO-GENERATED by artifacts/session/session_timing_distribution/analyze.py -- do not",
        "% edit by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Per-session, per-request, per-step, and individual latency wall-clock time by category. "
        "\\emph{Human thinking} in the session blocks is the sum of gaps from the previous event to "
        "the next user message. The final block reports individual positive human-input waits, "
        "observable per-round generation spans, and per-tool effective latencies, matching the "
        "CDF/summary distributions. The \\emph{human capped (1h)} block re-states the per-session "
        "budget with each idle gap clamped to one hour (a prompt-cache TTL horizon), dropping the "
        "long abandoned-session tail so the engaged-time split is visible. \\emph{\\% time} is each "
        "category's share of its block total; shares can exceed 100\\% only from generation/tool "
        "overlap (concurrent tools, generation streaming during a tool call).}",
        "\\label{tab:timing_distribution}",
        "\\small",
        "\\setlength{\\tabcolsep}{4pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Avg} & \\textbf{P50} & \\textbf{P90} & \\textbf{P99} "
        "& \\textbf{\\% time} \\\\",
        "\\midrule",
    ]
    for gi, (gkey, glabel) in enumerate(GRANULARITIES):
        if gi:
            lines.append("\\addlinespace")
        lines.append(f"\\multicolumn{{6}}{{@{{}}l}}{{\\emph{{{glabel}}}}} \\\\")
        sh = shares(scope[UNIT_KEY_BY_GRAN[gkey]], gkey) if gkey != "individual" else {}
        for ckey, clabel, has_share in CATS_BY_GRAN[gkey]:
            unit_list = unit_list_for(scope, gkey, ckey)
            s = stats(series(unit_list, gkey, ckey))
            cells = " & ".join(dur(s[k]) for k in ("avg", "p50", "p90", "p99"))
            share = pct(sh[ckey]) if has_share and ckey in sh else ""
            lines.append(f"\\quad {clabel} & {cells} & {share} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def render_md(units: dict[str, dict[str, Any]]) -> str:
    """GFM mirror of :func:`render_tex` for the web detail page (table only — the page adds a caption)."""
    scope = units["merged"]
    headers = ["Metric", "Avg", "P50", "P90", "P99", "% time"]
    sections: list[tuple[str, list[list[str]]]] = []
    for gkey, glabel in GRANULARITIES:
        sh = shares(scope[UNIT_KEY_BY_GRAN[gkey]], gkey) if gkey != "individual" else {}
        rows: list[list[str]] = []
        for ckey, clabel, has_share in CATS_BY_GRAN[gkey]:
            unit_list = unit_list_for(scope, gkey, ckey)
            s = stats(series(unit_list, gkey, ckey))
            share = f"{sh[ckey] * 100:.1f}%" if has_share and ckey in sh else ""
            rows.append([clabel, dur(s["avg"]), dur(s["p50"]), dur(s["p90"]), dur(s["p99"]), share])
        sections.append((glabel, rows))
    return md_table.section_tables(headers, sections, ["l", "r", "r", "r", "r", "r"])


def render_headline(units: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """The 2–4 most important numbers for the Overview gallery card (label + formatted value).

    Derived from the same merged data as the table so the card never drifts. Consumed by
    web/scripts/build-payload.mjs (aggregated into public/data/headlines.json by experiment slug).
    """
    scope = units["merged"]
    sess_sh = shares(scope["session"], "session")  # human / gen / tool share of wall-clock
    req = stats(series(scope["request"], "request", "total"))  # per-request e2e
    req_sh = shares(scope["request"], "request")  # gen / tool share of response time
    return [
        {"label": "Human thinking", "value": f"{sess_sh['human'] * 100:.1f}%"},
        {"label": "Avg response / request", "value": dur(req["avg"])},
        {"label": "Request: gen / tool", "value": f"{req_sh['gen'] * 100:.0f}% / {req_sh['tool'] * 100:.0f}%"},
    ]


def render_stdout(units: dict[str, dict[str, Any]]) -> str:
    out: list[str] = []
    for scope in SCOPES:
        sc = units[scope]
        out.append(
            f"[{scope}]  sessions={len(sc['session']):,}  requests={len(sc['request']):,}  "
            f"human_waits={len(sc['human_wait']):,}  gen_spans={len(sc['gen_observed']):,}  "
            f"tool_latencies={len(sc['tool_call']):,}  steps={len(sc['step']):,}"
        )
        for gkey, glabel in GRANULARITIES:
            sh = shares(sc[UNIT_KEY_BY_GRAN[gkey]], gkey) if gkey != "individual" else {}
            out.append(f"  {glabel}:")
            for ckey, clabel, has_share in CATS_BY_GRAN[gkey]:
                unit_list = unit_list_for(sc, gkey, ckey)
                s = stats(series(unit_list, gkey, ckey))
                share = f"  ({sh[ckey] * 100:5.1f}%)" if has_share and ckey in sh else ""
                out.append(
                    f"    {clabel:26s} avg {dur(s['avg']):>8s}  p25 {dur(s['p25']):>8s}  "
                    f"p50 {dur(s['p50']):>8s}  p90 {dur(s['p90']):>8s}  p99 {dur(s['p99']):>8s}{share}"
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
        units = collect(con)
    finally:
        con.close()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "session_timing_distribution.tex"
    tex_path.write_text(render_tex(units), encoding="utf-8")
    md_path = out_dir / "session_timing_distribution.md"
    md_path.write_text(render_md(units), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(units), indent=2) + "\n", encoding="utf-8")

    print(render_stdout(units))
    print(f"Wrote {tex_path}, {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
