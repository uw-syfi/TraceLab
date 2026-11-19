#!/usr/bin/env python3
"""Observed elapsed time and outstanding-tool wall time in matched subagent sessions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import md_table  # noqa: E402
import trace_db  # noqa: E402
from subagent_cohorts import COHORT_SQL, summarize  # noqa: E402

PROVIDERS = (("claude", "Claude"), ("codex", "Codex"))


def union_duration(intervals) -> int:
    """Union valid intervals in integer microseconds, including touching intervals."""
    if not intervals:
        return 0
    ordered = sorted(intervals)
    start, end = ordered[0]
    total = 0
    for next_start, next_end in ordered[1:]:
        if next_start > end:
            total += end - start
            start = next_start
        end = max(end, next_end)
    return total + end - start


def collect(con) -> dict:
    import request_timing as rt
    members = con.execute(f"WITH cohort AS ({COHORT_SQL}) SELECT provider, session_id, role FROM cohort").fetchall()
    measured = rt.collect_session_timing(con) if members else {}
    out = {}
    for provider, _ in PROVIDERS:
        descendants = [measured[p, sid] for p, sid, role in members
                       if p == provider and role == "descendant" and (p, sid) in measured]
        mains = [measured[p, sid] for p, sid, role in members
                 if p == provider and role == "main" and (p, sid) in measured]
        def metrics(rows):
            eligible = [r for r in rows if r["wall"] is not None and r["wall"] > 0
                        and not r["missing_tool_timing"] and not r["invalid_tool_endpoints"]]
            occupancy = [r for r in rows if r["wall"] is not None and r["wall"] > 0
                         and r["outstanding"] is not None]
            def ratio(records, field):
                total = sum(r["wall"] for r in records)
                return 100 * sum(r[field] for r in records) / total if total else None
            return {
                "sessions": len(rows), "elapsed": summarize([r["observed_wall"] for r in rows]),
                "tools": summarize([r["tool"] for r in rows]),
                "outstanding": summarize([r["outstanding"] for r in rows]),
                "share": summarize([100 * r["tool"] / r["wall"] for r in eligible]),
                "pooled_share": ratio(eligible, "tool"), "share_sessions": len(eligible),
                "outstanding_share": ratio(occupancy, "outstanding"), "occupancy_sessions": len(occupancy),
                "calls": sum(r["tool_calls"] for r in rows),
                "missing_calls": sum(r["missing_tool_timing"] for r in rows),
                "anonymous_observations": sum(r["unmeasured_tool_observations"] for r in rows),
                "invalid_endpoints": sum(r["invalid_tool_endpoints"] for r in rows),
                "compressed_sessions": sum(r["compressed_timestamps"] for r in rows),
            }
        out[provider] = metrics(descendants)
        out[provider]["groups"] = {"matched mains": metrics(mains)}
        for kind in ("worker", "guardian", "unknown"):
            rows = [r for r in descendants if r["session_kind"] == kind]
            if rows:
                out[provider]["groups"][kind] = metrics(rows)
    return out


def _number(value) -> str:
    return "N/A" if value is None else f"{value:,.2f}"


def render_md(data: dict) -> str:
    blocks = ["# Recorded session timing\n",
        "Canonical tool work uses the same positive internal-else-wall call durations as the overall session analysis. "
        "It is attributed work, including interaction tools, not exclusive execution or model-blocked time. "
        "Tool-outstanding wall time is a separately named interval-union measure.\n",
        "Subagents: " + "; ".join(f"{label} {data[p]['sessions']:,}" for p, label in PROVIDERS) + ".\n"]
    for key, heading in (("elapsed", "Observed elapsed span (seconds)"),
                         ("tools", "Canonical attributed tool work (seconds)"),
                         ("outstanding", "Time with tools outstanding (seconds; extra wall metric)")):
        blocks += [f"## {heading}\n", md_table.gfm_table(
            ["Provider", "N", "Avg", "P25", "P50", "P90", "P99"],
            [[label, str(data[p][key]["n"])] + [_number(data[p][key][stat]) for stat in ("avg", "p25", "p50", "p90", "p99")]
             for p, label in PROVIDERS])]
    blocks += ["## Same-definition pooled ratios and populations\n",
        "Ratios use sums over eligible sessions, matching the overall session timing table. "
        "Canonical work / observed span may exceed 100% with parallel work; it is not a wall-clock partition. "
        "Outstanding / observed span requires complete valid endpoints. "
        "Positive internal duration with identical endpoints excludes a capture from wall ratios. "
        "Each distribution shows its own sample N. Observed spans and known canonical work retain finite recorded values, "
        "while ratios use the explicitly counted complete eligible subset; averages need not describe that subset. "
        "Unknown legacy kinds stay unknown.\n"]
    rows = []
    for p, label in PROVIDERS:
        for group, metrics in [("all linked subagents", data[p]), *data[p]["groups"].items()]:
            rows.append([label, group, str(metrics["sessions"]), str(metrics["share_sessions"]),
                         _number(metrics["pooled_share"]), str(metrics["occupancy_sessions"]),
                         _number(metrics["outstanding_share"])])
    blocks.append(md_table.gfm_table(
        ["Provider", "Population", "Sessions", "Work-ratio eligible", "Work / span (%)",
         "Wall-ratio eligible", "Outstanding / span (%)"], rows))
    blocks += ["## Timing coverage\n", md_table.gfm_table(
        ["Provider", "Recorded calls", "Missing timing/observations", "Anonymous tool observations", "Invalid/missing endpoints", "Compressed sessions"],
        [[label, str(data[p]["calls"]), str(data[p]["missing_calls"]), str(data[p]["anonymous_observations"]),
          str(data[p]["invalid_endpoints"]), str(data[p]["compressed_sessions"])] for p, label in PROVIDERS]),
        "Known positive tool work remains reported when timing is incomplete; unknown durations are not zero measurements. "
        "Low ratios can reflect fast tools, text-only guardians, and long between-request gaps. "
        "Missing native web-search starts leave their duration unavailable. "
        "Observed spans are not verified spawn-to-return times, and the remainder is not measured model compute.\n"]
    return "\n".join(blocks)


def render_headline(data: dict) -> list[dict[str, str]]:
    return [
        {"label": f"{label} median observed subagent elapsed time (seconds)",
         "value": _number(data[provider]["elapsed"]["p50"])}
        for provider, label in PROVIDERS
    ]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args(argv)
    con = trace_db.open_from_args(args)
    try:
        data = collect(con)
    finally:
        con.close()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "timing.md"
    md_path.write_text(render_md(data), encoding="utf-8")
    headline_path = out_dir / "headline.json"
    headline_path.write_text(json.dumps(render_headline(data), indent=2) + "\n", encoding="utf-8")
    print(render_md(data))
    print(f"Wrote {md_path} and {headline_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
