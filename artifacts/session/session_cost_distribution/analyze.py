#!/usr/bin/env python3
"""Per-session, reconstructed-request, and per-round token/cost distributions.

Request ownership is shared with timing/count analyses through request_timing.py.
Legacy request groups retain attributed costs without a verified response duration.
Pricing and session/round billing definitions are unchanged."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # session_cost_distribution -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "web_analytics"))

import md_table  # noqa: E402  (gfm_table / section_tables for the web detail page)
import pricing  # noqa: E402  (price_for / round_cost over artifacts/utils/pricing.json)
import trace_db  # noqa: E402

SCOPES = ("merged", "claude", "codex")
PERCENTILES = (25, 50, 90, 99)
GRANULARITIES = (("session", "Per session"), ("request", "Per request"), ("step", "Per step"))
# (key, label) -- order follows the paper's wording: new append, prefix, output, then total.
CATEGORIES = (
    ("total", "Total"),
    ("append", "Append tokens"),
    ("prefix", "Prefix tokens"),
    ("output", "Output tokens"),
)
_PER_M = 1_000_000


def _has_round_column(con, column: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT count(*) > 0
            FROM information_schema.columns
            WHERE table_name = 'rounds' AND column_name = ?
            """,
            [column],
        ).fetchone()[0]
    )


def _load_module(name: str, relpath: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DEC = _load_module(
    "scd_turn_decomposition",
    "artifacts/human_in_the_loop/user_turn_decomposition/analyze.py",
)


def _unit() -> dict[str, float]:
    return {"pre_t": 0, "app_t": 0, "out_t": 0, "pre_c": 0.0, "app_c": 0.0, "out_c": 0.0}


def _add(acc: dict[str, float], pre, app, out, pc, ac, oc) -> None:
    acc["pre_t"] += pre
    acc["app_t"] += app
    acc["out_t"] += out
    acc["pre_c"] += pc
    acc["app_c"] += ac
    acc["out_c"] += oc


def collect(con) -> tuple[dict[str, dict[str, list[dict]]], dict[str, int]]:
    """Return ``(units, meta)`` where ``units[scope][granularity]`` is a list of per-unit token/
    cost dicts, and ``meta`` carries priced/unpriced round counts."""
    from request_timing import collect_requests
    requests = collect_requests(con)
    request_by_round = {rpk: index for index, request in enumerate(requests) for rpk in request.round_pks}
    request_acc = {index: _unit() for index in range(len(requests))}
    cache_write_expr = (
        "claude_cache_creation_input_tokens"
        if _has_round_column(con, "claude_cache_creation_input_tokens")
        else "CAST(NULL AS BIGINT) AS claude_cache_creation_input_tokens"
    )
    cache_write_1h_expr = (
        "CAST(claude_cache_creation_1h_input_tokens AS BIGINT)"
        if _has_round_column(con, "claude_cache_creation_1h_input_tokens")
        else "CAST(NULL AS BIGINT)"
    )
    rows = con.execute(
        "SELECT round_pk, provider, model, session_id, prefix_tokens, newly_append_tokens, "
        f"{cache_write_expr}, {cache_write_1h_expr}, output_tokens FROM rounds ORDER BY round_pk"
    ).fetchall()

    units: dict[str, dict[str, list[dict]]] = {
        s: {"session": [], "request": [], "step": []} for s in SCOPES
    }
    sess_acc: dict[str, dict[str, dict]] = {s: {} for s in SCOPES}
    priced = unpriced = 0

    for rpk, prov, model, sid, pre, app, cache_write, cache_write_1h, out in rows:
        sid = sid if isinstance(sid, str) and sid else None
        price = pricing.price_for(prov or "", model)
        if price is None:
            unpriced += 1
        else:
            priced += 1
            pre, app, out = int(pre or 0), int(app or 0), int(out or 0)
            cache_write = int(cache_write or 0)
            rc = pricing.round_cost(
                price, pre, app, out, cache_write_tokens=cache_write,
                cache_write_1h_tokens=int(cache_write_1h or 0),
            )
            pc = rc["cachedCost"]
            ac = rc["inputCost"]
            oc = rc["outputCost"]
            unit = {"pre_t": pre, "app_t": app, "out_t": out, "pre_c": pc, "app_c": ac, "out_c": oc}
            for scope in ("merged", prov):
                if scope in units:
                    units[scope]["step"].append(unit)
            if sid is not None:
                for scope in ("merged", prov):
                    if scope in sess_acc:
                        _add(sess_acc[scope].setdefault((prov, sid), _unit()), pre, app, out, pc, ac, oc)
            index = request_by_round.get(rpk)
            if index is not None:
                _add(request_acc[index], pre, app, out, pc, ac, oc)

    for index, request in enumerate(requests):
        if not request.round_pks:
            continue
        for scope in ("merged", request.provider):
            if scope in units:
                units[scope]["request"].append(request_acc[index])

    for scope in SCOPES:
        units[scope]["session"] = list(sess_acc[scope].values())
    from request_timing import request_ownership_coverage
    return units, {"priced": priced, "unpriced": unpriced,
                   "ownership_coverage": request_ownership_coverage(con, requests=requests)}


def series(unit_list: list[dict], category: str, kind: str) -> list[float]:
    """Extract one number per unit for ``category`` (``total``/``append``/``prefix``/``output``)
    and ``kind`` (``t`` tokens / ``c`` cost)."""
    pre, app, out = f"pre_{kind}", f"app_{kind}", f"out_{kind}"
    if category == "total":
        return [u[pre] + u[app] + u[out] for u in unit_list]
    key = {"append": app, "prefix": pre, "output": out}[category]
    return [u[key] for u in unit_list]


def stats(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if not arr.size:
        return {"avg": 0.0, "p25": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0}
    row = {"avg": float(arr.mean())}
    row.update({f"p{q}": float(v) for q, v in zip(PERCENTILES, np.percentile(arr, PERCENTILES))})
    return row


def cost_shares(step_units: list[dict]) -> dict[str, float]:
    app = sum(u["app_c"] for u in step_units)
    pre = sum(u["pre_c"] for u in step_units)
    out = sum(u["out_c"] for u in step_units)
    total = app + pre + out
    if total <= 0:
        return {"append": 0.0, "prefix": 0.0, "output": 0.0}
    return {"append": app / total, "prefix": pre / total, "output": out / total}


# ----- formatting -----
def toks(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.1f}K"
    return f"{x:.0f}"


def money(x: float, tex: bool = False) -> str:
    # Constant-width rule: <10 -> two decimals (x.xx); 10-99 -> one (xx.x); >=100 -> integer.
    sign = "\\$" if tex else "$"
    if x >= 100:
        return f"{sign}{x:,.0f}"
    if x >= 10:
        return f"{sign}{x:.1f}"
    return f"{sign}{x:.2f}"


# Short row labels for the cost-only table (the full names live in CATEGORIES / the caption).
SHORT_LABEL = {
    "total": "Total",
    "append": "Append tokens",
    "prefix": "Prefix tokens",
    "output": "Output tokens",
}


def render_tex(units: dict[str, dict[str, list[dict]]], meta: dict[str, int]) -> str:
    """Cost-only single-column table: USD per session/request/step by category."""
    scope = units["merged"]
    shares = cost_shares(scope["step"])
    priced_pct = meta["priced"] / (meta["priced"] + meta["unpriced"]) * 100

    def cost_cells(s: dict[str, float]) -> str:
        return " & ".join(money(s[k], tex=True) for k in ("avg", "p50", "p90", "p99"))

    lines = [
        "% AUTO-GENERATED by artifacts/session/session_cost_distribution/analyze.py -- do not",
        "% edit by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Per-session, per-request, and per-step cost (USD) by category under the "
        "provider list prices in \\cref{tab:pricing_snapshot}: append tokens at "
        "fresh-input/cache-write rates, prefix tokens at the cache-read rate, and output tokens "
        f"at the output rate. Overall, {priced_pct:.1f}\\% of rounds are priced; "
        "\\emph{\\% cost} is each category's share of total spend.}",
        "\\label{tab:cost_distribution}",
        "\\small",
        "\\setlength{\\tabcolsep}{4pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Avg} & \\textbf{P50} & \\textbf{P90} & \\textbf{P99} "
        "& \\textbf{\\% cost} \\\\",
        "\\midrule",
    ]
    for gi, (gkey, glabel) in enumerate(GRANULARITIES):
        if gi:
            lines.append("\\addlinespace")
        lines.append(f"\\multicolumn{{6}}{{@{{}}l}}{{\\emph{{{glabel}}}}} \\\\")
        for ckey, _ in CATEGORIES:
            cost = stats(series(scope[gkey], ckey, "c"))
            share = "" if ckey == "total" else f"{shares[ckey] * 100:.1f}\\%"
            lines.append(f"\\quad {SHORT_LABEL[ckey]} & {cost_cells(cost)} & {share} \\\\")
    lines += ["\\bottomrule", "\\end{tabular}", "\\end{table}"]
    from request_timing import ownership_coverage_note
    if meta.get("ownership_coverage"):
        lines.append("% " + ownership_coverage_note(meta["ownership_coverage"]))
    return "\n".join(lines) + "\n"


def render_md(units: dict[str, dict[str, list[dict]]], meta: dict[str, int]) -> str:
    """GFM mirror of :func:`render_tex` for the web detail page (table only — the page adds a caption)."""
    scope = units["merged"]
    shares = cost_shares(scope["step"])
    headers = ["Metric", "Avg", "P50", "P90", "P99", "% cost"]
    sections: list[tuple[str, list[list[str]]]] = []
    for gkey, glabel in GRANULARITIES:
        rows: list[list[str]] = []
        for ckey, _ in CATEGORIES:
            cost = stats(series(scope[gkey], ckey, "c"))
            share = "" if ckey == "total" else f"{shares[ckey] * 100:.1f}%"
            rows.append(
                [SHORT_LABEL[ckey], money(cost["avg"]), money(cost["p50"]), money(cost["p90"]), money(cost["p99"]), share]
            )
        sections.append((glabel, rows))
    from request_timing import render_ownership_coverage
    return (md_table.section_tables(headers, sections, ["l", "r", "r", "r", "r", "r"])
            + "\n\n" + render_ownership_coverage(meta["ownership_coverage"]))


def render_headline(units: dict[str, dict[str, list[dict]]], meta: dict[str, int]) -> list[dict[str, str]]:
    """The 2–4 most important numbers for the Overview gallery card (label + formatted value).

    Derived from the same merged data as the table so the card never drifts. Consumed by
    web/scripts/build-payload.mjs (aggregated into public/data/headlines.json by experiment slug).
    """
    scope = units["merged"]
    shares = cost_shares(scope["step"])
    sess = stats(series(scope["session"], "total", "c"))
    return [
        {"label": "Avg cost / session", "value": money(sess["avg"])},
        {"label": "Prefix share of cost", "value": f"{shares['prefix'] * 100:.1f}%"},
        {"label": "Append / output share", "value": f"{shares['append'] * 100:.0f}% / {shares['output'] * 100:.0f}%"},
    ]


def render_stdout(units: dict[str, dict[str, list[dict]]], meta: dict[str, int]) -> str:
    out: list[str] = []
    total = meta["priced"] + meta["unpriced"]
    out.append(
        f"priced rounds: {meta['priced']:,} / {total:,} "
        f"({meta['priced'] / total * 100:.2f}%)  unpriced: {meta['unpriced']:,}\n"
    )
    for scope in SCOPES:
        sc = units[scope]
        shares = cost_shares(sc["step"])
        out.append(
            f"[{scope}]  sessions={len(sc['session']):,}  requests={len(sc['request']):,}  "
            f"steps={len(sc['step']):,}"
        )
        out.append(
            f"  cost composition: append {shares['append'] * 100:.1f}%  "
            f"prefix {shares['prefix'] * 100:.1f}%  output {shares['output'] * 100:.1f}%"
        )
        for gkey, glabel in GRANULARITIES:
            out.append(f"  {glabel}:")
            for ckey, clabel in CATEGORIES:
                tok = stats(series(sc[gkey], ckey, "t"))
                cost = stats(series(sc[gkey], ckey, "c"))
                out.append(
                    f"    {clabel:22s} tok  avg {toks(tok['avg']):>8s}  p50 {toks(tok['p50']):>8s}  "
                    f"p90 {toks(tok['p90']):>8s}  p99 {toks(tok['p99']):>8s}"
                )
                out.append(
                    f"    {'':22s} cost avg {money(cost['avg']):>8s}  p50 {money(cost['p50']):>8s}  "
                    f"p90 {money(cost['p90']):>8s}  p99 {money(cost['p99']):>8s}"
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
        units, meta = collect(con)
    finally:
        con.close()

    from request_timing import ownership_coverage_note
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / "session_cost_distribution.tex"
    tex_path.write_text(render_tex(units, meta) + "\n% " + ownership_coverage_note(meta["ownership_coverage"]) + "\n", encoding="utf-8")
    md_path = out_dir / "session_cost_distribution.md"
    md_path.write_text(render_md(units, meta), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(units, meta), indent=2) + "\n", encoding="utf-8")

    print(render_stdout(units, meta))
    print(f"Wrote {tex_path}, {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
