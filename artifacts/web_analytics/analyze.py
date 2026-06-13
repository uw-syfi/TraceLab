#!/usr/bin/env python3
"""Top-level aggregator: one trace DuckDB -> the entire ``AnalyticsPayload`` JSON.

This is the single entry point the web app calls (over the QA ``run-python`` RPC). The frontend
loads the trace (worker materializes ``/work/trace.duckdb``), then runs a one-line snippet:

    import sys; sys.path[:0] = ["/repo/artifacts/web_analytics", "/repo/artifacts/utils"]
    import analyze
    print(analyze.bulk_json("/work/trace.duckdb", tz_offset_min=-420))

and parses the printed JSON. All data work happens here; the frontend only renders. See the contract
in web/app/src/lib/analytics/types.ts (camelCase) — the dict keys match it 1:1.

On-demand ``session_detail_json`` / ``round_raw_json`` ride the same RPC (added with their builders).

Native parity / debugging:

    python artifacts/web_analytics/analyze.py /tmp/trace.duckdb --tz -420
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Bare sibling/utils imports must resolve both under the QA snippet (which prepends both dirs to
# sys.path) and when run as a script. Prepend defensively; idempotent if already present.
_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "utils")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import trace_db  # noqa: E402  (artifacts/utils)

from _overview import read_summary_from_db  # noqa: E402
from cost_breakdown import build_cost, build_providers  # noqa: E402
from daily import build_hour_weekday, build_per_day  # noqa: E402
from distributions import build_distributions  # noqa: E402
from facts import build_facts  # noqa: E402
from kpis import build_kpis  # noqa: E402
from rounds import load_rounds  # noqa: E402
from sessions import build_session_detail, build_sessions  # noqa: E402
from stats import build_stats  # noqa: E402


def bulk(db_path, tz_offset_min: int = 0) -> dict:
    """Compute the full payload dict for one trace DB. ``tz_offset_min`` shifts naive-UTC -> local
    for the per-day / work-rhythm buckets (``local = utc + offset``).

    One connection feeds every builder: the shared per-round rows (``load_rounds``) cover kpis / cost
    / daily / sessions / facts, while ``read_summary_from_db`` reuses the proven overview aggregator
    for the rich stats + per-provider distribution lists. The few SQL-only metrics (tool error rate,
    latency-by-category, session error counts) run against the same open connection."""
    con = trace_db.connect(db_path, read_only=True)
    try:
        rows = load_rounds(con)
        bundle = read_summary_from_db(con)

        cost = build_cost(rows)
        providers = build_providers(cost["byModel"])
        total_cost_usd = sum(m["costUsd"] for m in cost["byModel"])

        per_day = build_per_day(rows, tz_offset_min)
        hour_weekday = build_hour_weekday(rows, tz_offset_min)

        kpis = build_kpis(
            rows,
            total_cost_usd=total_cost_usd,
            cache_savings_usd=cost["cacheSavingsUsd"],
            active_days=len(per_day),
        )

        sessions = build_sessions(con, rows)
        stats = build_stats(con, bundle, rows, cost, sessions, per_day, tz_offset_min)
        facts = build_facts(con, rows, cost, sessions, per_day, tz_offset_min)
        distributions = build_distributions(con, bundle, rows)
    finally:
        con.close()

    return {
        "kpis": kpis,
        "perDay": per_day,
        "hourWeekday": hour_weekday,
        "cost": cost,
        "providers": providers,
        "stats": stats,
        "facts": facts,
        "sessions": sessions,
        "distributions": distributions,
        # raw per-round text is LOCAL-only; the worker flips this true when a local raw map exists.
        "rawAvailable": False,
    }


def bulk_json(db_path, tz_offset_min: int = 0) -> str:
    return json.dumps(bulk(db_path, tz_offset_min))


def session_detail(db_path, session_id: str) -> dict:
    """On-demand per-round timeline for ONE session (``SessionDetail``). Opens its own short-lived
    read-only connection; scans are scoped to ``session_id`` so this stays cheap on big traces."""
    con = trace_db.connect(db_path, read_only=True)
    try:
        return build_session_detail(con, session_id)
    finally:
        con.close()


def session_detail_json(db_path, session_id: str) -> str:
    return json.dumps(session_detail(db_path, session_id))


def _main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", type=Path, help="materialized trace DuckDB (trace_db.materialize)")
    ap.add_argument("--tz", type=int, default=0, dest="tz_offset_min",
                    help="UTC offset in minutes (local = utc + offset); e.g. -420 for PDT")
    ap.add_argument("--pretty", action="store_true", help="indent the JSON for reading")
    args = ap.parse_args(argv)
    payload = bulk(args.db, args.tz_offset_min)
    print(json.dumps(payload, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
