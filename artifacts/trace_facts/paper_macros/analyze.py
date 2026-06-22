#!/usr/bin/env python3
"""Compute every result-number macro used in the TraceLab paper, in one place.

The paper (``src/*.tex``) cites a fixed set of headline numbers through LaTeX
``\\newcommand`` macros (defined in ``_command.tex``). This script is the single
source of truth for those numbers: it derives each one from the trace and emits a
``paper_macros.tex`` block of ``\\newcommand`` definitions plus a human-readable
table, so every macro maps 1:1 to a value reproducible from the public dataset.

Definitions are *reused* from the canonical experiments rather than reimplemented:

* token totals / hit rate / context growth / decode-latency come from the
  ``trace_facts/overview_summary`` aggregate (``Summary``);
* per-request (user-turn) metrics replay the exact turn state machine from
  ``human_in_the_loop/user_turn_decomposition`` (same turn boundaries as
  ``user_turn_response_time`` -- cross-checked identical);
* per-step medians and tool stats are computed directly off the trace DB.

Run with the standard trace-db CLI (``--db`` | ``-i/--input`` | ``-o/--output-dir``).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # paper_macros -> trace_facts -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402

# Long-tool threshold: "longer than 1 minute" == the >=60s tool-latency bins
# (1-10m / 10m-1h / >=1h in TOOL_LATENCY_BINS_MS), i.e. effective latency >= 60000 ms.
LONG_TOOL_MS = 60_000
# Effective tool latency precedence (shared with tool_latency_distribution): internal else wall.
_EFF = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL


def _load_module(name: str, relpath: str) -> Any:
    """Import a sibling experiment module under a unique name (they are all ``analyze.py``)."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


OV = _load_module("pm_overview", "artifacts/trace_facts/overview_summary/analyze.py")
DEC = _load_module(
    "pm_turn_decomposition",
    "artifacts/human_in_the_loop/user_turn_decomposition/analyze.py",
)


# --------------------------------------------------------------------------------------
# Computation
# --------------------------------------------------------------------------------------
def compute_overview_and_decode(con) -> tuple[dict[str, Any], dict[str, list[float]]]:
    """One pass over reconstructed rounds -> (overview-as-dict, per-step decode-speed lists).

    Decode-speed lists (tok/s), all using the overview's own per-step timing helpers so the
    medians line up with the overview's aggregate means:
      * ``norm[<scope>]``           = output_tokens / input-to-last-output span (normalized decode)
      * ``codex_post``              = visible tokens / post-reasoning span      (Codex pure decode)
      * ``codex_ttft_inputs``       = (input-to-reasoning-end span, reasoning_tokens) for the
                                      Codex TTFT residual (residual computed once the aggregate
                                      decode latency is known).
    """
    bundle = OV.SummaryBundle()
    norm: dict[str, list[float]] = {"merged": [], "claude": [], "codex": []}
    codex_post: list[float] = []
    codex_ttft_inputs: list[tuple[float, int]] = []

    for row in OV._rows_from_db(con):
        bundle.add(row)
        provider = row.get("provider")
        output = OV.output_tokens_including_reasoning(row)
        span = OV.input_to_last_output_span_seconds(row)
        if span and output > 0:
            norm["merged"].append(output / span)
            if provider in norm:
                norm[provider].append(output / span)
        if provider == "codex":
            reasoning = OV.int_field(row, "reasoning_output_tokens")
            if reasoning > 0:
                visible = OV.visible_or_structured_output_tokens(row)
                post_span = OV.post_reasoning_output_span_seconds(row)
                if post_span and visible > 0:
                    codex_post.append(visible / post_span)
                input_to_reasoning_end = OV.input_to_reasoning_end_span_seconds(row)
                if input_to_reasoning_end is not None:
                    codex_ttft_inputs.append((input_to_reasoning_end, reasoning))

    overview = bundle.as_dict()
    decode = {"norm_merged": norm["merged"], "norm_codex": norm["codex"]}
    decode["codex_post"] = codex_post

    # Codex TTFT residual: input-to-reasoning-end span minus reasoning tokens decoded at the
    # aggregate Codex decode latency (same latency the overview uses for its TTFT estimate).
    latency = overview["codex"]["generation_timing"]["post_reasoning_tpot_estimate"][
        "average_decode_latency_seconds_per_token"
    ]
    decode["codex_ttft"] = [
        span - reasoning * latency for span, reasoning in codex_ttft_inputs
    ]
    return overview, decode


def compute_step_medians(con) -> dict[str, dict[str, float]]:
    """Per-step medians (over all rounds) of prefix / append / output tokens, by scope."""
    out: dict[str, dict[str, float]] = {}
    for scope in ("merged", "claude", "codex"):
        where = "" if scope == "merged" else f"WHERE provider = '{scope}'"
        prefix, append, output = con.execute(
            "SELECT quantile_cont(prefix_tokens, 0.5), "
            "quantile_cont(newly_append_tokens, 0.5), "
            f"quantile_cont(output_tokens, 0.5) FROM rounds {where}"
        ).fetchone()
        out[scope] = {
            "prefix": float(prefix),
            "append": float(append),
            "output": float(output),
        }
    return out


def compute_turn_metrics(con) -> dict[str, dict[str, float]]:
    """Per-request (user-turn) metrics, replaying the canonical user-turn state machine.

    A turn opens on a response-triggering ``user_message`` and runs until the next one in the
    same session; it is dropped if it has no response-end event or non-positive duration -- the
    exact drop rule shared by ``user_turn_decomposition`` and ``user_turn_response_time``. For
    each kept turn we record steps (rounds), tool calls, and end-to-end seconds.
    """
    events_by_round = DEC.load_timing_events(con)
    tools_by_round = DEC.load_round_tools(con)

    active: dict[str, dict[str, Any]] = {}
    samples: dict[str, list[tuple[int, int, float]]] = {"merged": [], "claude": [], "codex": []}

    def close(session_id: str) -> None:
        turn = active.pop(session_id, None)
        if turn is None or turn["end"] is None:
            return
        e2e = (turn["end"] - turn["start"]).total_seconds()
        if e2e <= 0:
            return
        record = (turn["steps"], turn["tools"], e2e)
        samples["merged"].append(record)
        if turn["provider"] in samples:
            samples[turn["provider"]].append(record)

    for round_pk, provider, session_id in con.execute(
        "SELECT round_pk, provider, session_id FROM rounds ORDER BY round_pk"
    ).fetchall():
        events = events_by_round.get(round_pk, [])
        provider = str(provider) if provider else "<unknown-provider>"
        session_id = session_id if isinstance(session_id, str) else None

        start = DEC.response_trigger_user_message_timestamp(events)
        if start is not None and session_id is not None:
            close(session_id)
            active[session_id] = {
                "provider": provider,
                "start": start,
                "end": None,
                "steps": 0,
                "tools": 0,
            }

        turn = active.get(session_id) if session_id is not None else None
        if turn is None:
            continue
        turn["steps"] += 1
        response_end = DEC.last_response_end_timestamp(events)
        if response_end is not None and (turn["end"] is None or response_end > turn["end"]):
            turn["end"] = response_end
        round_tools = tools_by_round.get(round_pk)
        if round_tools is not None:
            turn["tools"] += round_tools.tool_calls

    for session_id in list(active):
        close(session_id)

    out: dict[str, dict[str, float]] = {}
    for scope, records in samples.items():
        steps = np.asarray([r[0] for r in records], dtype=float)
        tools = np.asarray([r[1] for r in records], dtype=float)
        e2e = np.asarray([r[2] for r in records], dtype=float)
        out[scope] = {
            "turns": int(steps.size),
            "steps_per_request": float(steps.mean()),
            "tools_per_request": float(tools.mean()),
            "mean_minutes": float(e2e.mean()) / 60,
            "p90_minutes": float(np.percentile(e2e, 90)) / 60,
        }
    return out


def compute_tool_stats(con) -> dict[str, Any]:
    """Distinct tool count, per-provider top-3 share, and long-tool (>1 min) call/time shares."""
    distinct_tools = con.execute(
        "SELECT count(DISTINCT tool_name) FROM tool_calls"
    ).fetchone()[0]

    top3_share: dict[str, dict[str, Any]] = {}
    for provider in ("claude", "codex"):
        total = con.execute(
            "SELECT count(*) FROM tool_calls tc JOIN rounds r USING (round_pk) "
            "WHERE r.provider = ?",
            [provider],
        ).fetchone()[0]
        top = con.execute(
            "SELECT tc.tool_name, count(*) c FROM tool_calls tc JOIN rounds r USING (round_pk) "
            "WHERE r.provider = ? GROUP BY 1 ORDER BY c DESC LIMIT 3",
            [provider],
        ).fetchall()
        top3_share[provider] = {
            "tools": [name for name, _ in top],
            "share": sum(c for _, c in top) / total if total else None,
        }

    long_tool: dict[str, dict[str, float]] = {}
    for scope in ("merged", "claude", "codex"):
        where = "" if scope == "merged" else f"AND r.provider = '{scope}'"
        n_lat, n_long, t_lat, t_long = con.execute(
            f"""
            SELECT count(*) FILTER (WHERE eff IS NOT NULL AND eff > 0),
                   count(*) FILTER (WHERE eff >= {LONG_TOOL_MS}),
                   sum(CASE WHEN eff IS NOT NULL AND eff > 0 THEN eff ELSE 0 END),
                   sum(CASE WHEN eff >= {LONG_TOOL_MS} THEN eff ELSE 0 END)
            FROM (
                SELECT ({_EFF}) AS eff
                FROM tool_calls tc JOIN rounds r USING (round_pk)
                WHERE 1 = 1 {where}
            )
            """
        ).fetchone()
        long_tool[scope] = {
            "call_share": (n_long / n_lat) if n_lat else 0.0,
            "time_share": (t_long / t_lat) if t_lat else 0.0,
        }

    return {
        "distinct_tools": int(distinct_tools),
        "top3": top3_share,
        "long_tool": long_tool,
    }


# --------------------------------------------------------------------------------------
# Macro assembly
# --------------------------------------------------------------------------------------
def _floor_to_10(value: float) -> int:
    return int(value // 10 * 10)


def build_macros(
    overview: dict[str, Any],
    decode: dict[str, list[float]],
    medians: dict[str, dict[str, float]],
    turns: dict[str, dict[str, float]],
    tools: dict[str, Any],
) -> list[dict[str, str]]:
    """Return the ordered macro table: (name, body, scope, basis) per result number.

    ``body`` is the literal ``\\newcommand`` body (with ``\\xspace``). ``basis`` records the raw
    value(s) the body was rounded/derived from, for the printed audit trail.
    """
    merged_in = overview["merged"]["tokens"]["input"]
    hit_rate = merged_in["prefix_hit_rate"]
    amplification = merged_in["new_input_tokens"] / merged_in["total_context_increase_tokens"]

    claude_top3 = tools["top3"]["claude"]["share"]
    codex_top3 = tools["top3"]["codex"]["share"]
    top3_floor = _floor_to_10(min(claude_top3, codex_top3) * 100)
    tools_floor = _floor_to_10(tools["distinct_tools"])

    def median(values: list[float]) -> float:
        return float(np.median(values))

    macros: list[dict[str, str]] = [
        # --- Session level (per request / user turn), merged ---
        {
            "name": "avgstepperrequest",
            "body": f"{turns['merged']['steps_per_request']:.1f}\\xspace",
            "scope": "merged",
            "basis": f"{turns['merged']['steps_per_request']:.3f} steps/turn over {turns['merged']['turns']:,} turns",
        },
        {
            "name": "avgtollcallsperrequest",
            "body": f"{turns['merged']['tools_per_request']:.1f}\\xspace",
            "scope": "merged",
            "basis": f"{turns['merged']['tools_per_request']:.3f} tool calls/turn",
        },
        {
            "name": "avgtimeperrequest",
            "body": f"{turns['merged']['mean_minutes']:.1f}\\xspace",
            "scope": "merged",
            "basis": f"{turns['merged']['mean_minutes']:.3f} min mean e2e",
        },
        {
            "name": "pntimeperrequest",
            "body": f"{turns['merged']['p90_minutes']:.1f}\\xspace",
            "scope": "merged",
            "basis": f"{turns['merged']['p90_minutes']:.3f} min p90 e2e",
        },
        # --- LLM generation, per step ---
        {
            "name": "mediancachedinputtokens",
            "body": f"{round(medians['merged']['prefix'] / 1000)}K\\xspace",
            "scope": "merged",
            "basis": f"{medians['merged']['prefix']:,.0f} median prefix tokens",
        },
        {
            "name": "medianuncachedinputtokens",
            "body": f"{round(medians['merged']['append'])}\\xspace",
            "scope": "merged",
            "basis": f"{medians['merged']['append']:,.0f} median append tokens",
        },
        {
            "name": "medianoutputtokens",
            "body": f"{round(medians['merged']['output'])}\\xspace",
            "scope": "merged",
            "basis": f"{medians['merged']['output']:,.0f} median output tokens",
        },
        {
            "name": "mediandecodespeed",
            "body": f"{median(decode['norm_merged']):.1f}\\xspace",
            "scope": "merged",
            "basis": f"{median(decode['norm_merged']):.3f} tok/s median normalized decode",
        },
        {
            "name": "mediancodecdecodespeed",
            "body": f"{median(decode['codex_post']):.1f}\\xspace",
            "scope": "codex",
            "basis": f"{median(decode['codex_post']):.3f} tok/s median post-reasoning decode (Codex)",
        },
        {
            "name": "mediancodecdecodespeedttft",
            "body": f"{median(decode['codex_ttft']):.1f}\\xspace",
            "scope": "codex",
            "basis": f"{median(decode['codex_ttft']):.3f} s median TTFT residual (Codex)",
        },
        # --- Tool calls ---
        {
            "name": "totaltoolcatetory",
            "body": f"{tools_floor}\\xspace",
            "scope": "merged",
            "basis": f"{tools['distinct_tools']} distinct raw tool names (floored to {tools_floor})",
        },
        {
            "name": "topthreetoolpercent",
            "body": f"{top3_floor}\\%\\xspace",
            "scope": "per-provider",
            "basis": f"Claude top-3 {claude_top3 * 100:.2f}%, Codex top-3 {codex_top3 * 100:.2f}% (floored to {top3_floor}%)",
        },
        {
            "name": "toolcalltoppercentage",
            "body": f"{_floor_to_10(claude_top3 * 100)}\\%\\xspace",
            "scope": "claude",
            "basis": f"Claude top-3 {claude_top3 * 100:.2f}% (floored for an 'over' statement)",
        },
        {
            "name": "toolcalltoppercentagecodex",
            "body": f"{round(codex_top3 * 100)}\\%\\xspace",
            "scope": "codex",
            "basis": f"Codex top-3 {codex_top3 * 100:.2f}% (rounded)",
        },
        {
            "name": "toolcallslongerthanonemin",
            "body": f"{round(tools['long_tool']['merged']['call_share'] * 100)}\\xspace",
            "scope": "merged",
            "basis": f"{tools['long_tool']['merged']['call_share'] * 100:.2f}% of tool calls >1 min",
        },
        {
            "name": "toolcallslongerthanoneminpercent",
            "body": f"{round(tools['long_tool']['merged']['time_share'] * 100)}\\%\\xspace",
            "scope": "merged",
            "basis": f"{tools['long_tool']['merged']['time_share'] * 100:.2f}% of tool time from >1 min calls",
        },
        # --- Prefix cache ---
        {
            "name": "prefixcachehitrate",
            "body": f"{hit_rate * 100:.1f}\\%\\xspace",
            "scope": "merged",
            "basis": f"{hit_rate * 100:.3f}% token-weighted global prefix hit rate",
        },
        {
            "name": "prefillamplificationfactor",
            "body": f"{amplification:.1f}$\\times$\\xspace",
            "scope": "merged",
            "basis": (
                f"{amplification:.3f}x = total new append "
                f"({merged_in['new_input_tokens']:,}) / context growth "
                f"({merged_in['total_context_increase_tokens']:,})"
            ),
        },
    ]
    return macros


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def render_tex(macros: list[dict[str, str]]) -> str:
    width = max(len(m["name"]) for m in macros)
    lines = [
        "% Result-number macros for the TraceLab paper.",
        "% AUTO-GENERATED by artifacts/trace_facts/paper_macros/analyze.py -- do not edit by hand;",
        "% re-run the script on the trace to refresh. Each macro maps 1:1 to a computed value.",
    ]
    for macro in macros:
        lines.append(
            f"\\newcommand{{\\{macro['name']}}}{{{macro['body']}}}"
            f"  % {macro['basis']}"
        )
    return "\n".join(lines) + "\n"


def render_table(macros: list[dict[str, str]]) -> str:
    name_w = max(len(m["name"]) for m in macros)
    body_w = max(len(m["body"]) for m in macros)
    rows = [f"  {'macro'.ljust(name_w)}  {'value'.ljust(body_w)}  basis"]
    rows.append(f"  {'-' * name_w}  {'-' * body_w}  {'-' * 40}")
    for macro in macros:
        rows.append(f"  {macro['name'].ljust(name_w)}  {macro['body'].ljust(body_w)}  {macro['basis']}")
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    try:
        overview, decode = compute_overview_and_decode(con)
        medians = compute_step_medians(con)
        turns = compute_turn_metrics(con)
        tools = compute_tool_stats(con)
    finally:
        con.close()

    macros = build_macros(overview, decode, medians, turns, tools)

    out_path = Path(args.output_dir) / "paper_macros.tex"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_tex(macros), encoding="utf-8")

    print("Result-number macros for the TraceLab paper:\n")
    print(render_table(macros))
    print(f"\nWrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
