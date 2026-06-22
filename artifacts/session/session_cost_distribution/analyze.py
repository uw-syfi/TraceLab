#!/usr/bin/env python3
"""Per-session / per-request / per-step token *and* cost distributions for the paper's
Cost Distribution table (``tab:cost_distribution`` in ``src/04_SessionContext.tex``).

For each granularity we break the workload into the three billed categories and report, for
both the token count and the USD cost, avg / p25 / p50 / p90 / p99:

* **Append tokens** -- ``newly_append_tokens``, billed at the fresh-input rate.
* **Prefix tokens** -- ``prefix_tokens``, billed at the cache-read rate.
* **Output tokens** -- ``output_tokens`` (reasoning included), billed at the output rate.
* **Total** -- the sum of the three.

Cost rows also carry each category's **share of total spend** (the same three numbers for any
grouping, since they are sums over the same priced rounds).

Definitions reuse the canonical experiments so the numbers reconcile:

* **Cost** is computed with the single-source price table ``artifacts/utils/pricing.json`` via
  ``web_analytics/pricing.py`` (``price_for`` -> per-model exact/family resolve; ``round_cost``
  -> append at input rate, prefix at cache-read rate, output at output rate). Rounds whose model
  has no price are *unpriced* and excluded (99.1% of rounds are priced); coverage is reported.
* A **request** is one user turn -- the same turn state machine as
  ``human_in_the_loop/user_turn_decomposition`` (identical to ``user_turn_response_time`` and
  ``session_internal_counts``). A **step** is one LLM round; a **session** is one ``session_id``.

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
REPO_ROOT = EXP_DIR.parents[2]  # session_cost_distribution -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "web_analytics"))

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
    events_by_round = DEC.load_timing_events(con)
    rows = con.execute(
        "SELECT round_pk, provider, model, session_id, prefix_tokens, newly_append_tokens, "
        "output_tokens FROM rounds ORDER BY round_pk"
    ).fetchall()

    units: dict[str, dict[str, list[dict]]] = {
        s: {"session": [], "request": [], "step": []} for s in SCOPES
    }
    sess_acc: dict[str, dict[str, dict]] = {s: {} for s in SCOPES}
    active: dict[str, dict] = {}
    priced = unpriced = 0

    def close(sid: str) -> None:
        turn = active.pop(sid, None)
        if turn is None or turn["end"] is None:
            return
        if (turn["end"] - turn["start"]).total_seconds() <= 0:
            return
        for scope in ("merged", turn["provider"]):
            if scope in units:
                units[scope]["request"].append(turn["acc"])

    for rpk, prov, model, sid, pre, app, out in rows:
        sid = sid if isinstance(sid, str) and sid else None
        events = events_by_round.get(rpk, [])
        start = DEC.response_trigger_user_message_timestamp(events)
        if start is not None and sid is not None:
            close(sid)
            active[sid] = {"provider": prov, "start": start, "end": None, "acc": _unit()}

        price = pricing.price_for(prov or "", model)
        if price is None:
            unpriced += 1
        else:
            priced += 1
            pre, app, out = int(pre or 0), int(app or 0), int(out or 0)
            pc = pre * price["cachedInputPerM"] / _PER_M
            ac = app * price["inputPerM"] / _PER_M
            oc = out * price["outputPerM"] / _PER_M
            unit = {"pre_t": pre, "app_t": app, "out_t": out, "pre_c": pc, "app_c": ac, "out_c": oc}
            for scope in ("merged", prov):
                if scope in units:
                    units[scope]["step"].append(unit)
            if sid is not None:
                for scope in ("merged", prov):
                    if scope in sess_acc:
                        _add(sess_acc[scope].setdefault(sid, _unit()), pre, app, out, pc, ac, oc)
            turn = active.get(sid) if sid is not None else None
            if turn is not None:
                _add(turn["acc"], pre, app, out, pc, ac, oc)

        turn = active.get(sid) if sid is not None else None
        if turn is not None:
            response_end = DEC.last_response_end_timestamp(events)
            if response_end is not None and (turn["end"] is None or response_end > turn["end"]):
                turn["end"] = response_end
    for sid in list(active):
        close(sid)

    for scope in SCOPES:
        units[scope]["session"] = list(sess_acc[scope].values())
    return units, {"priced": priced, "unpriced": unpriced}


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
        "\\caption{Per-session, per-request, and per-step cost (USD) by category. List prices "
        "(\\texttt{pricing.json}, as of 2026-06): append tokens at the fresh-input rate, "
        "prefix tokens at the cache-read rate, output tokens at the output rate; "
        f"{priced_pct:.1f}\\% of rounds priced. \\emph{{\\% cost}} is each category's share of "
        "total spend (identical across groupings).}",
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
    return "\n".join(lines) + "\n"


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

    out_path = Path(args.output_dir) / "session_cost_distribution.tex"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_tex(units, meta), encoding="utf-8")

    print(render_stdout(units, meta))
    print(f"Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
