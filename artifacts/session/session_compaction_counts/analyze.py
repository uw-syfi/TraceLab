#!/usr/bin/env python3
"""How many *context compactions* does a coding session undergo?

A **compaction** is the behavioral event the paper distinguishes from the plain size
buckets in ``total_input_growth``: when the running context is near its limit, it is
summarized/dropped to a short history and then slowly re-accumulates. We detect it
structurally from the per-step total input length (``prefix_tokens + newly_append_tokens``),
ordered by ``round_pk`` within each session. A step ``i`` is a compaction when all three
hold:

1. **Great reduction** -- ``total[i-1] - total[i] >= 64k`` (``--min-drop-tokens``, defaults
   to ``growth.MAJOR_REDUCTION_MIN_TOKENS``). Every compaction is therefore also a
   *major reduction*; compactions are the strict subset that also satisfy (2) and (3).
2. **Near the context limit** -- the pre-drop level ``total[i-1]`` is at least
   ``--near-max-ratio`` (0.75) of the session's observed max total input. The drop happens
   near the session's peak context, not at some small early dip.
3. **Recovers slowly** -- the context does *not* rebound to ``--rebound-ratio`` (0.75) of the
   pre-drop level within the next ``--rebound-steps`` (3) steps, and at least one step
   follows (so re-accumulation is actually observable). A drop that immediately snaps back is
   a branch/edit artifact, not a compaction.

Each compaction is attributed to the trigger of step ``i`` (the first step on the compacted
context), using the same ``user-initiated`` / ``tool-initiated`` split the rest of the paper
uses: ``user_message`` -> user-initiated (e.g. an explicit ``/compact`` or a new request that
forced summarization); ``tool_result`` -> tool-initiated (auto-compaction mid-loop).

Reports, for merged / Claude / Codex: the compactions-per-session distribution
(avg / p25 / p50 / p90 / p99) over all sessions, the share of sessions with >=1 compaction,
the distribution among only those sessions, the total compaction count, and the
user-initiated-vs-tool-initiated trigger split.

Run with the standard trace-db CLI (``--db`` | ``-i/--input`` | ``-o/--output-dir``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # session_compaction_counts -> session -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import growth  # noqa: E402  (for MAJOR_REDUCTION_MIN_TOKENS, so the size criterion stays in sync)
import trace_db  # noqa: E402

SCOPES = ("merged", "claude", "codex")
PERCENTILES = (25, 50, 90, 99)


VALID_TRIGGERS = {"user_message", "tool_result"}  # the steps the growth taxonomy buckets


def scan_session(
    totals: list[int],
    triggers: list[str | None],
    *,
    min_drop_tokens: int,
    near_max_ratio: float,
    rebound_ratio: float,
    rebound_steps: int,
) -> tuple[list[int], list[int]]:
    """Return ``(major_reduction_idxs, compaction_idxs)`` for one session's ordered steps.

    Both are restricted to steps with a valid trigger (``user_message`` / ``tool_result``) -- the
    same population the growth taxonomy buckets -- so the major-reduction count reconciles with
    ``total_input_growth`` and every compaction is one of those major reductions.
    """
    n = len(totals)
    major: list[int] = []
    compaction: list[int] = []
    if n < 2:
        return major, compaction
    session_max = max(totals)
    if session_max <= 0:
        return major, compaction
    for i in range(1, n):
        if triggers[i] not in VALID_TRIGGERS:
            continue
        prev, cur = totals[i - 1], totals[i]
        if prev - cur < min_drop_tokens:  # (1) great reduction (>= major-reduction threshold)
            continue
        major.append(i)
        if prev < near_max_ratio * session_max:  # (2) near the context limit
            continue
        following = totals[i + 1 : i + 1 + rebound_steps]
        if not following:  # (3) need at least one step after to observe recovery
            continue
        if any(t >= rebound_ratio * prev for t in following):  # fast rebound -> not a compaction
            continue
        compaction.append(i)
    return major, compaction


def collect(con, **criteria: Any) -> dict[str, dict[str, Any]]:
    """Walk every session and tally compactions per session and by trigger."""
    rows = con.execute(
        "SELECT session_id, provider, "
        "prefix_tokens + newly_append_tokens AS total_input, first_input_event_type "
        "FROM rounds WHERE session_id IS NOT NULL AND session_id <> '' "
        "ORDER BY session_id, round_pk"
    ).fetchall()

    # group consecutive rows by session_id (already ordered by session_id, round_pk)
    sessions: list[tuple[str, list[int], list[str | None]]] = []
    cur_sid = None
    totals: list[int] = []
    triggers: list[str | None] = []
    provider_of: dict[str, str] = {}
    for sid, provider, total_input, first_event in rows:
        if sid != cur_sid:
            if cur_sid is not None:
                sessions.append((cur_sid, totals, triggers))
            cur_sid, totals, triggers = sid, [], []
        provider_of[sid] = provider
        totals.append(int(total_input or 0))
        triggers.append(first_event)
    if cur_sid is not None:
        sessions.append((cur_sid, totals, triggers))

    out: dict[str, dict[str, Any]] = {
        scope: {
            "per_session": [], "user_initiated": 0, "tool_initiated": 0,
            "total": 0, "major_reductions": 0,
        }
        for scope in SCOPES
    }
    for sid, totals, triggers in sessions:
        major_idxs, comp_idxs = scan_session(totals, triggers, **criteria)
        provider = provider_of.get(sid)
        n_user = sum(1 for i in comp_idxs if triggers[i] == "user_message")
        n_tool = sum(1 for i in comp_idxs if triggers[i] == "tool_result")
        for scope in ("merged", provider):
            bucket = out.get(scope)
            if bucket is None:
                continue
            bucket["per_session"].append(len(comp_idxs))
            bucket["user_initiated"] += n_user
            bucket["tool_initiated"] += n_tool
            bucket["total"] += len(comp_idxs)
            bucket["major_reductions"] += len(major_idxs)
    return out


def summarize(scope_data: dict[str, Any]) -> dict[str, Any]:
    per_session = np.asarray(scope_data["per_session"], dtype=float)
    n_sessions = int(per_session.size)
    with_compaction = per_session[per_session >= 1]
    pcts = np.percentile(per_session, PERCENTILES) if n_sessions else [0] * 4
    pcts_pos = (
        np.percentile(with_compaction, PERCENTILES) if with_compaction.size else [0] * 4
    )
    return {
        "n_sessions": n_sessions,
        "total": int(scope_data["total"]),
        "major_reductions": int(scope_data["major_reductions"]),
        "user_initiated": int(scope_data["user_initiated"]),
        "tool_initiated": int(scope_data["tool_initiated"]),
        "avg": float(per_session.mean()) if n_sessions else 0.0,
        **{f"p{q}": float(v) for q, v in zip(PERCENTILES, pcts)},
        "sessions_with": int(with_compaction.size),
        "share_with": float(with_compaction.size / n_sessions) if n_sessions else 0.0,
        "avg_pos": float(with_compaction.mean()) if with_compaction.size else 0.0,
        **{f"p{q}_pos": float(v) for q, v in zip(PERCENTILES, pcts_pos)},
    }


def render_table(data: dict[str, dict[str, Any]]) -> str:
    out: list[str] = []
    for scope in SCOPES:
        s = data[scope]
        mr = s["major_reductions"]
        comp_share = s["total"] / mr * 100 if mr else 0.0
        out.append(f"[{scope}]  ({s['n_sessions']:,} sessions)")
        out.append(f"  major reductions (>=64k)    : {mr:,}")
        out.append(f"  total compactions          : {s['total']:,} ({comp_share:.1f}% of major)")
        out.append(
            f"  sessions with >=1 compaction: {s['sessions_with']:,} "
            f"({s['share_with'] * 100:.2f}%)"
        )
        out.append(
            f"  per session (all)           : avg {s['avg']:.3f}  "
            f"p25 {s['p25']:.0f}  p50 {s['p50']:.0f}  p90 {s['p90']:.0f}  p99 {s['p99']:.0f}"
        )
        out.append(
            f"  per session (>=1 only)      : avg {s['avg_pos']:.2f}  "
            f"p50 {s['p50_pos']:.0f}  p90 {s['p90_pos']:.0f}  p99 {s['p99_pos']:.0f}"
        )
        split_total = s["user_initiated"] + s["tool_initiated"]
        u = s["user_initiated"] / split_total * 100 if split_total else 0.0
        t = s["tool_initiated"] / split_total * 100 if split_total else 0.0
        out.append(
            f"  trigger split               : user-initiated {s['user_initiated']:,} ({u:.1f}%)  "
            f"tool-initiated {s['tool_initiated']:,} ({t:.1f}%)"
        )
        out.append("")
    return "\n".join(out)


def render_tex(data: dict[str, dict[str, Any]]) -> str:
    """Render the Claude / Codex table, matching ``tab:context_growth_and_compaction``."""
    c, x = data["claude"], data["codex"]

    def comp_share(s: dict[str, Any]) -> float:
        return s["total"] / s["major_reductions"] * 100 if s["major_reductions"] else 0.0

    def trig_pct(s: dict[str, Any], key: str) -> float:
        total = s["user_initiated"] + s["tool_initiated"]
        return s[key] / total * 100 if total else 0.0

    lines = [
        "% AUTO-GENERATED by artifacts/session/session_compaction_counts/analyze.py -- do not",
        "% edit by hand; re-run on the trace to refresh.",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Context compactions per session: a near-limit total-input drop "
        "($\\geq$64k) that recovers slowly (no rebound to 75\\% of the pre-drop level within "
        "three steps).}",
        "\\label{tab:session_compaction}",
        "\\small",
        "\\setlength{\\tabcolsep}{5pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Claude} & \\textbf{Codex} \\\\",
        "\\midrule",
        f"Sessions & {c['n_sessions']:,} & {x['n_sessions']:,} \\\\",
        f"Major reductions ($\\geq$64k drop) & {c['major_reductions']:,} "
        f"& {x['major_reductions']:,} \\\\",
        f"\\quad of which compactions & {c['total']:,} ({comp_share(c):.1f}\\%) "
        f"& {x['total']:,} ({comp_share(x):.1f}\\%) \\\\",
        f"Sessions with $\\geq$1 compaction & {c['sessions_with']:,} "
        f"({c['share_with'] * 100:.1f}\\%) & {x['sessions_with']:,} "
        f"({x['share_with'] * 100:.1f}\\%) \\\\",
        "\\addlinespace",
        "\\multicolumn{3}{@{}l}{\\emph{Compactions per session}} \\\\",
        f"\\quad Avg (all sessions) & {c['avg']:.3f} & {x['avg']:.3f} \\\\",
        f"\\quad Avg (sessions with $\\geq$1) & {c['avg_pos']:.2f} & {x['avg_pos']:.2f} \\\\",
        f"\\quad P90 / P99 (sessions with $\\geq$1) & "
        f"{c['p90_pos']:.0f} / {c['p99_pos']:.0f} & {x['p90_pos']:.0f} / {x['p99_pos']:.0f} \\\\",
        "\\addlinespace",
        "\\multicolumn{3}{@{}l}{\\emph{Trigger}} \\\\",
        f"\\quad User-initiated & {c['user_initiated']:,} ({trig_pct(c, 'user_initiated'):.1f}\\%) "
        f"& {x['user_initiated']:,} ({trig_pct(x, 'user_initiated'):.1f}\\%) \\\\",
        f"\\quad Tool-initiated & {c['tool_initiated']:,} ({trig_pct(c, 'tool_initiated'):.1f}\\%) "
        f"& {x['tool_initiated']:,} ({trig_pct(x, 'tool_initiated'):.1f}\\%) \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\end{table}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument(
        "--min-drop-tokens", type=int, default=growth.MAJOR_REDUCTION_MIN_TOKENS,
        help="Minimum single-step total-input drop to qualify (>= major-reduction threshold).",
    )
    parser.add_argument(
        "--near-max-ratio", type=float, default=0.75,
        help="Pre-drop level must be at least this fraction of the session's max total input.",
    )
    parser.add_argument(
        "--rebound-ratio", type=float, default=0.75,
        help="A rebound to this fraction of the pre-drop level disqualifies (fast recovery).",
    )
    parser.add_argument(
        "--rebound-steps", type=int, default=3,
        help="Number of following steps checked for a disqualifying rebound.",
    )
    args = parser.parse_args()

    criteria = dict(
        min_drop_tokens=args.min_drop_tokens,
        near_max_ratio=args.near_max_ratio,
        rebound_ratio=args.rebound_ratio,
        rebound_steps=args.rebound_steps,
    )

    con = trace_db.open_from_args(args)
    try:
        raw = collect(con, **criteria)
    finally:
        con.close()

    data = {scope: summarize(raw[scope]) for scope in SCOPES}

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "session_compaction_counts.tex").write_text(
        render_tex(data), encoding="utf-8"
    )

    print(
        f"criteria: drop>={args.min_drop_tokens:,}  near-max>={args.near_max_ratio:g}x  "
        f"no rebound to {args.rebound_ratio:g}x within {args.rebound_steps} steps\n"
    )
    print(render_table(data))
    print(f"Wrote {out_dir / 'session_compaction_counts.tex'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
