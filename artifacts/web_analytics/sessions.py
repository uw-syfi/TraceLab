"""Session list (the ``SessionRow[]`` for the session picker).

One row per ``session_id``, aggregated from the shared per-round rows: token split, tool-call count,
cost (billed through ``pricing``), and the round-start time span. ``provider`` / ``primaryModel`` are
the session's most common values (sessions are usually single-provider, but a few switch mid-run).
``errors`` is the count of tool calls that returned an error, joined back per session.

``firstTsUs`` / ``lastTsUs`` span the rounds' *start* timestamps (the only per-round time we load);
``durationS`` is their difference — a close, honest approximation of wall-clock session length
without re-scanning every timing event. ``title`` stays ``None`` here; conversation titles are a
LOCAL-only ingest concern (task #17) layered on top, never part of this aggregate.

``build_session_detail`` is the on-demand per-round timeline for ONE session (fetched lazily over the
same QA RPC when a session is opened). It scopes every scan to one ``session_id`` so it stays cheap
even on a 50k-round trace, and reuses the overview aggregator's generation-time definition
(``input_to_last_output_span_seconds``) so the per-round "inference" duration reconciles with the
overview / generation-time experiments.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Optional

from pricing import price_for, round_cost
from rounds import RoundRow

# Timing-event classes, matching overview_summary's INPUT/MODEL_OUTPUT sets, so per-round generation
# time here equals the overview's ``input_to_last_output_span_seconds``.
_INPUT_EVENT_TYPES = {"user_message", "tool_result"}
_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}


def _session_errors(con) -> dict[str, int]:
    """session_id -> count of tool calls that returned an error (BOOLEAN ``is_error``)."""
    out: dict[str, int] = {}
    for session_id, n in con.execute(
        "SELECT r.session_id, count(*) AS n "
        "FROM tool_calls t JOIN rounds r USING (round_pk) "
        "WHERE t.is_error GROUP BY r.session_id"
    ).fetchall():
        if session_id is not None:
            out[session_id] = int(n)
    return out


def build_sessions(con, rows: list[RoundRow]) -> list[dict[str, Any]]:
    errors_by_session = _session_errors(con)

    groups: dict[str, list[RoundRow]] = {}
    for r in rows:
        sid = r["session_id"]
        if sid:
            groups.setdefault(sid, []).append(r)

    out: list[dict[str, Any]] = []
    for sid, rs in groups.items():
        providers: Counter[str] = Counter(r["provider"] for r in rs if r["provider"])
        models: Counter[str] = Counter(r["model"] for r in rs if r["model"])
        provider = providers.most_common(1)[0][0] if providers else "unknown"
        primary_model = models.most_common(1)[0][0] if models else "unknown"

        input_tokens = sum(r["prefix"] + r["append"] for r in rs)
        output_tokens = sum(r["output"] for r in rs)
        tool_calls = sum(r["tool_calls"] for r in rs)

        cost_usd = 0.0
        for r in rs:
            price = price_for(r["provider"] or "", r["model"])
            if price is not None:
                cost_usd += round_cost(
                    price, r["prefix"], r["append"], r["output"], r["reasoning"]
                )["total"]

        starts = [r["first_ts_us"] for r in rs if r["first_ts_us"] is not None]
        first_ts = min(starts) if starts else 0
        last_ts = max(starts) if starts else 0

        out.append({
            "sessionId": sid,
            "title": None,
            "provider": provider,
            "primaryModel": primary_model,
            "rounds": len(rs),
            "firstTsUs": first_ts,
            "lastTsUs": last_ts,
            "durationS": (last_ts - first_ts) / 1_000_000,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "costUsd": cost_usd,
            "toolCalls": tool_calls,
            "errors": errors_by_session.get(sid, 0),
        })

    out.sort(key=lambda s: s["rounds"], reverse=True)
    return out


def _gen_seconds(events: list[tuple[Optional[str], Optional[int]]]) -> float:
    """Per-round observable generation time: last input-at-or-before-first-output → last output.

    Mirrors ``overview_summary.input_to_last_output_span_seconds`` (operating on (event_type, ts_us)
    tuples instead of dict rows). Returns 0.0 when the round has no usable input/output pair."""
    inputs = [ts for et, ts in events if et in _INPUT_EVENT_TYPES and ts is not None]
    outputs = [ts for et, ts in events if et in _OUTPUT_EVENT_TYPES and ts is not None]
    if not inputs or not outputs:
        return 0.0
    first_out = min(outputs)
    candidates = [ts for ts in inputs if ts <= first_out]
    if not candidates:
        return 0.0
    duration = (max(outputs) - max(candidates)) / 1_000_000
    return duration if duration > 0 else 0.0


def build_session_detail(con, session_id: str) -> dict[str, Any]:
    """The ``SessionDetail`` (per-round timeline + per-session tool stats) for one session.

    Three session-scoped scans — rounds, this session's timing events, this session's tool calls —
    then everything is assembled in Python. Rounds come back ordered by ``round_index`` (the within-
    session step order); ``seq`` is a 1..N positional index for the timeline x-axis (matches the
    mock). ``tsUs`` is the round's first timing event, forward/back-filled across the rare round that
    carries no timing so the timeline's wall-clock bucketing never sees a 0."""
    from _overview import percentile  # local import keeps module load cheap

    round_rows = con.execute(
        "SELECT round_pk, round_index, first_input_event_type, "
        "prefix_tokens, newly_append_tokens, output_tokens, reasoning_output_tokens, trace_key "
        "FROM rounds WHERE session_id = ? ORDER BY round_index, round_pk",
        [session_id],
    ).fetchall()
    if not round_rows:
        return {"sessionId": session_id, "rounds": [], "tools": []}

    # This session's timing events, grouped by round (event_type + ts).
    timing: dict[int, list[tuple[Optional[str], Optional[int]]]] = {}
    for round_pk, event_type, ts_us in con.execute(
        "SELECT te.round_pk, te.event_type, CAST(epoch_us(te.timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events te JOIN rounds r USING (round_pk) WHERE r.session_id = ?",
        [session_id],
    ).fetchall():
        timing.setdefault(int(round_pk), []).append((event_type, int(ts_us) if ts_us is not None else None))

    # This session's tool calls, grouped by round (name / effective latency ms / error).
    tools_by_round: dict[int, list[dict[str, Any]]] = {}
    for round_pk, tool_name, internal, wall, is_error in con.execute(
        "SELECT t.round_pk, t.tool_name, t.tool_internal_latency_ms, t.tool_wall_latency_ms, t.is_error "
        "FROM tool_calls t JOIN rounds r USING (round_pk) WHERE r.session_id = ? ORDER BY t.round_pk",
        [session_id],
    ).fetchall():
        lat = internal if internal is not None else wall
        tools_by_round.setdefault(int(round_pk), []).append({
            "name": tool_name or "?",
            "ms": int(round(float(lat))) if lat is not None else 0,
            "error": bool(is_error),
        })

    # Per-round ts (min timing event), then forward-fill then back-fill so leading/empty rounds inherit
    # a neighbour's wall-clock instead of collapsing to 0.
    ts_seq: list[Optional[int]] = []
    for round_pk, *_rest in round_rows:
        evs = timing.get(int(round_pk), [])
        tss = [ts for _et, ts in evs if ts is not None]
        ts_seq.append(min(tss) if tss else None)
    last: Optional[int] = None
    for i, ts in enumerate(ts_seq):
        if ts is None:
            ts_seq[i] = last
        else:
            last = ts
    nxt: Optional[int] = None
    for i in range(len(ts_seq) - 1, -1, -1):
        if ts_seq[i] is None:
            ts_seq[i] = nxt
        else:
            nxt = ts_seq[i]

    rounds_out: list[dict[str, Any]] = []
    for seq, ((round_pk, _ridx, fiet, prefix, append, output, reasoning, trace_key), ts) in enumerate(
        zip(round_rows, ts_seq), start=1
    ):
        rtools = tools_by_round.get(int(round_pk), [])
        rounds_out.append({
            "seq": seq,
            "traceKey": trace_key or "",
            "prefixTokens": int(prefix or 0),
            "appendTokens": int(append or 0),
            "outputTokens": int(output or 0),
            "reasoningTokens": int(reasoning or 0),
            "tsUs": int(ts) if ts is not None else 0,
            "isUserInput": fiet == "user_message",
            "toolCount": len(rtools),
            "inferenceS": round(_gen_seconds(timing.get(int(round_pk), [])), 1),
            "tools": rtools,
        })

    # Per-session tool stats (count / errors / p50 latency), busiest first.
    agg: dict[str, dict[str, Any]] = {}
    for rtools in tools_by_round.values():
        for t in rtools:
            a = agg.setdefault(t["name"], {"count": 0, "errors": 0, "lat": []})
            a["count"] += 1
            if t["error"]:
                a["errors"] += 1
            if t["ms"] > 0:
                a["lat"].append(float(t["ms"]))
    tools_stats = [
        {
            "name": name,
            "count": a["count"],
            "errors": a["errors"],
            "p50Ms": int(round(percentile(a["lat"], 0.5))) if a["lat"] else 0,
        }
        for name, a in agg.items()
    ]
    tools_stats.sort(key=lambda x: x["count"], reverse=True)

    return {"sessionId": session_id, "rounds": rounds_out, "tools": tools_stats}
