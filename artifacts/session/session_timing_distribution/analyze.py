#!/usr/bin/env python3
"""Recorded per-session, request, round, and individual timing components.

Canonical attributed tool work and bounded wall occupancy are separate. Request
duration requires native lifecycle evidence; legacy ownership remains countable.
Definitions and eligibility are shared with subagent timing in request_timing.py."""

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
from request_timing import render_ownership_coverage  # noqa: E402
from timing import human_waits_from_event_pairs  # noqa: E402

SCOPES = ("merged", "claude", "codex")
PERCENTILES = (25, 50, 90, 99)
# (granularity key, block label). The capped-session block reuses the session units but recomputes
# its total with human idle clamped per gap (see UNIT_KEY_BY_GRAN / HUMAN_CAP_SECONDS).
GRANULARITIES = (
    ("session", "Per session"),
    ("session_capped", "Component budget, input gaps capped (1h)"),
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
    ("total", "Observed elapsed span", False),
    ("human", "Input gaps", True),
    ("gen", "Observed response spans", True),
    ("tool", "Attributed tool work", True),
    ("interaction", "Interaction-tool waits (included)", False),
    ("outstanding", "Tool-outstanding time", True),
)
# Capped-session block: total = capped human + generation + tool, so shares partition cleanly and
# the abandoned-session idle tail no longer drowns out generation/tool.
SESSION_CAPPED_CATS = (
    ("total_capped", "Total", False),
    ("human_capped", "Input gaps", True),
    ("gen", "Observed response spans", True),
    ("tool", "Attributed tool work", True),
)
REQUEST_CATS = (
    ("total", "Native completed response time", False),
    ("gen", "Attributed response spans", False),
    ("tool", "Attributed tool work", False),
    ("interaction", "Interaction-tool waits (included)", False),
    ("outstanding", "Bounded tool-outstanding time", True),
)
STEP_CATS = (
    ("gen", "Observed response spans", True),
    ("tool", "Attributed tool work", True),
)
INDIVIDUAL_CATS = (
    ("human", "Input gaps", False),
    ("gen", "Observed response spans", False),
    ("tool", "Attributed tool work", False),
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
    """Use the same request ownership and recorded-time definitions as the subagent report."""
    import request_timing as rt
    from collections import Counter
    events = rt.load_events(con)
    tools = rt.load_tools(con)
    units = {scope: {key: [] for key in
             ("session", "human_wait", "gen_observed", "tool_call", "request", "step")}
             for scope in SCOPES}
    for rpk, provider in con.execute("SELECT round_pk, provider FROM rounds ORDER BY round_pk").fetchall():
        measured = rt.timing_summary([rpk], events, tools)
        for scope in ("merged", provider):
            if scope not in units:
                continue
            units[scope]["step"].append(measured)
            if measured["gen"] > 0:
                units[scope]["gen_observed"].append({"gen": measured["gen"]})
            for _, _, _, internal, wall in tools.get(rpk, []):
                value = rt.effective_tool_seconds(internal, wall)
                if value is not None and value > 0:
                    units[scope]["tool_call"].append({"tool": value})
    for measured in rt.collect_session_timing(con).values():
        for scope in ("merged", measured["provider"]):
            if scope in units:
                units[scope]["session"].append(measured)
                units[scope]["human_wait"].extend({"human": value} for value in measured["human_waits"])
    requests = rt.collect_requests(con, events)
    ownership = rt.request_ownership_coverage(con, requests, events, tools)
    for measured in rt.collect_request_timing(con, events, tools, requests):
        for scope in ("merged", measured["provider"]):
            if scope in units:
                units[scope]["request"].append(measured)
    units["merged"]["ownership_coverage"] = ownership
    for scope in SCOPES:
        units[scope]["coverage"] = dict(Counter(r["status"] for r in units[scope]["request"]))
    return units


def unit_list_for(scope: dict[str, Any], granularity: str, category: str) -> list[dict]:
    if granularity == "individual":
        return scope[{"human": "human_wait", "gen": "gen_observed", "tool": "tool_call"}[category]]
    return scope[UNIT_KEY_BY_GRAN[granularity]]


def series(unit_list: list[dict], granularity: str, category: str) -> list[float]:
    """One number per unit for ``category`` at ``granularity`` (all seconds)."""
    if granularity == "session":
        if category == "total":
            return [u["observed_wall"] for u in unit_list]
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
    arr = np.asarray([value for value in values if value is not None and np.isfinite(value)], dtype=float)
    if not arr.size:
        return {"n": 0, **{key: None for key in ("avg", "p25", "p50", "p90", "p99")}}
    row = {"n": int(arr.size), "avg": float(arr.mean())}
    row.update({f"p{q}": float(v) for q, v in zip(PERCENTILES, np.percentile(arr, PERCENTILES))})
    return row


def ratio_population(unit_list, granularity, category):
    """The exact units included in both sides of a displayed pooled ratio."""
    if granularity == "session":
        return [u for u in unit_list if u.get("wall") and u.get(category) is not None
                and (category != "tool" or not (u.get("missing_tool_timing") or u.get("invalid_tool_endpoints")))]
    if granularity == "request":
        return [u for u in unit_list if u.get("e2e") and u.get("outstanding") is not None]
    return unit_list


def shares(unit_list: list[dict], granularity: str) -> dict[str, float]:
    """Ratio of pooled components, using the same eligible units for both sides."""
    if granularity == "individual":
        return {}
    keys = (("human", "gen", "tool", "outstanding") if granularity == "session" else
            ("outstanding",) if granularity == "request" else
            ("human_capped", "gen", "tool") if granularity == "session_capped" else ("gen", "tool"))
    ratios = {}
    for key in keys:
        eligible = ratio_population(unit_list, granularity, key)
        if granularity in ("session", "request"):
            denominator = "wall" if granularity == "session" else "e2e"
            total = sum(u[denominator] for u in eligible)
        else:
            total = sum(sum(u[k] for k in keys) for u in eligible)
        if total > 0:
            ratios[key] = sum(u[key] for u in eligible) / total
    return ratios


def ratio_cell(unit_list, granularity, category, ratios, *, tex=False):
    count = len(ratio_population(unit_list, granularity, category))
    if category not in ratios:
        return f"N/A (N={count:,})"
    value = pct(ratios[category]) if tex else f"{ratios[category] * 100:.1f}%"
    return f"{value} (N={count:,})"


# ----- formatting -----
def dur(x: float) -> str:
    """Adaptive duration with unit suffix: <60 s, <60 m, else h (signed)."""
    if x is None:
        return "N/A"
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
        "\\caption{Recorded timing components, not a partition of wall-clock compute. "
        "Tool work sums positive internal-else-wall latencies; interaction waits are included. "
        "N is the distribution sample count; each ratio shows its own eligible N. "
        "Observed-span distributions retain recorded captures; wall ratios exclude contradictions and missing coverage. "
        "Request wall time requires native completion. Only bounded outstanding intervals "
        "are divided by request wall time. N/A means unavailable.}",
        "\\label{tab:timing_distribution}",
        "\\small",
        "\\setlength{\\tabcolsep}{4pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{N} & \\textbf{Avg} & \\textbf{P50} & \\textbf{P90} & \\textbf{P99} "
        "& \\textbf{Ratio} \\\\",
        "\\midrule",
    ]
    for gi, (gkey, glabel) in enumerate(GRANULARITIES):
        if gi:
            lines.append("\\addlinespace")
        lines.append(f"\\multicolumn{{7}}{{@{{}}l}}{{\\emph{{{glabel}}}}} \\\\")
        sh = shares(scope[UNIT_KEY_BY_GRAN[gkey]], gkey) if gkey != "individual" else {}
        for ckey, clabel, has_share in CATS_BY_GRAN[gkey]:
            unit_list = unit_list_for(scope, gkey, ckey)
            s = stats(series(unit_list, gkey, ckey))
            cells = " & ".join(dur(s[k]) for k in ("avg", "p50", "p90", "p99"))
            share = ratio_cell(unit_list, gkey, ckey, sh, tex=True) if has_share else ""
            lines.append(f"\\quad {clabel} & {s['n']:,} & {cells} & {share} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    return "\n".join(lines) + "\n"


def render_md(units: dict[str, dict[str, Any]]) -> str:
    """GFM mirror of :func:`render_tex` for the web detail page (table only — the page adds a caption)."""
    scope = units["merged"]
    headers = ["Metric", "N", "Avg", "P50", "P90", "P99", "Pooled ratio (eligible N)"]
    sections: list[tuple[str, list[list[str]]]] = []
    for gkey, glabel in GRANULARITIES:
        sh = shares(scope[UNIT_KEY_BY_GRAN[gkey]], gkey) if gkey != "individual" else {}
        rows: list[list[str]] = []
        for ckey, clabel, has_share in CATS_BY_GRAN[gkey]:
            unit_list = unit_list_for(scope, gkey, ckey)
            s = stats(series(unit_list, gkey, ckey))
            share = ratio_cell(unit_list, gkey, ckey, sh) if has_share else ""
            rows.append([clabel, str(s["n"]), dur(s["avg"]), dur(s["p50"]), dur(s["p90"]), dur(s["p99"]), share])
        sections.append((glabel, rows))
    coverage = scope.get("coverage", {})
    measured = sum(u.get("e2e") is not None for u in scope["request"])
    note = ("Request groups: " + "; ".join(f"{key} {value:,}" for key, value in sorted(coverage.items()))
            + f". Measurable native completed durations: {measured:,}. "
            "Legacy, aborted, open, invalid, and compressed captures have no response duration. "
            "Attributed work remains counted; it is not divided by that duration. "
            "N counts the values behind each distribution. Ratio cells show a separate eligible N: "
            "session wall ratios exclude contradictory timestamps, and tool ratios also require complete timing coverage. "
            "Observed-span and known-work distributions retain finite recorded values, including partial or contradictory captures. "
            "Thus a displayed average is not necessarily the ratio population. Request work includes owned groups without verified durations; "
            "native duration and bounded occupancy use their own measurable subsets. Capped and step ratios divide the recorded component budget. "
            "Components overlap and do not partition compute time.\n\n")
    coverage_rows = []
    for provider in SCOPES:
        members = units[provider]["session"]
        work_eligible = [u for u in members if u.get("wall") and not u["missing_tool_timing"]
                         and not u["invalid_tool_endpoints"]]
        wall_eligible = [u for u in members if u.get("wall") and u["outstanding"] is not None]
        coverage_rows.append([provider, str(len(members)), str(len(work_eligible)), str(len(wall_eligible)),
            str(sum(u["missing_tool_timing"] for u in members)),
            str(sum(u["unmeasured_tool_observations"] for u in members)),
            str(sum(u["compressed_timestamps"] for u in members))])
    coverage_table = md_table.gfm_table(
        ["Provider", "Sessions", "Work-ratio eligible", "Wall-ratio eligible", "Missing timing/observations",
         "Anonymous tool observations", "Compressed sessions"], coverage_rows)
    return (note + md_table.section_tables(headers, sections, ["l", "r", "r", "r", "r", "r", "r"])
            + "\n\n**Session timing coverage**\n\n" + coverage_table
            + "\n\n" + render_ownership_coverage(scope["ownership_coverage"]))


def render_headline(units: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    """The 2–4 most important numbers for the Overview gallery card (label + formatted value).

    Derived from the same merged data as the table so the card never drifts. Consumed by
    web/scripts/build-payload.mjs (aggregated into public/data/headlines.json by experiment slug).
    """
    scope = units["merged"]
    sess_sh = shares(scope["session"], "session")  # human / gen / tool share of wall-clock
    req = stats(series(scope["request"], "request", "total"))  # per-request e2e
    return [
        {"label": "Input-gap / observed-span ratio", "value": f"{sess_sh['human'] * 100:.1f}%" if "human" in sess_sh else "N/A"},
        {"label": "Avg native completed response", "value": dur(req["avg"])},
        {"label": "Measurable native responses", "value": str(sum(u.get("e2e") is not None for u in scope["request"]))},
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
                    f"    {clabel:26s} N={s['n']:,} avg {dur(s['avg']):>8s}  p25 {dur(s['p25']):>8s}  "
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
