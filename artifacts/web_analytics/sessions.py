"""Session list (the ``SessionRow[]`` for the session picker).

One row per ``session_id``, aggregated from the shared per-round rows: token split, tool-call count,
cost (billed through ``pricing``), and the round-start time span. ``provider`` / ``primaryModel`` are
the session's most common values (sessions are usually single-provider, but a few switch mid-run).
``errors`` is the count of tool calls that returned an error, joined back per session.

``firstTsUs`` / ``lastTsUs`` span the rounds' *start* timestamps (the only per-round time we load);
``durationS`` is their difference — a close, honest approximation of wall-clock session length
without re-scanning every timing event. ``title`` stays ``None`` here; conversation titles are a
LOCAL-only ingest concern (task #17) layered on top, never part of this aggregate.

Subagent hierarchy: every session carries its ``threadKind`` (``main`` / ``subagent`` / ``guardian``),
its ``agentRole`` when delegated, and ``childSessions``. On a trace collected before these columns
existed all of them are ``None``: the hierarchy is unknown, which is not the same as "main thread".
``build_session_detail`` adds the opened session's ``topology`` (its parent, its children, the
workflows that launched them, and the automatic reviews of it).

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
from rounds import RoundRow, _has_round_column, hierarchy_select

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


def _most_common(values) -> Any:
    counts = Counter(v for v in values if v is not None)
    return counts.most_common(1)[0][0] if counts else None


def session_kind(kinds, self_referenced: bool = False) -> Optional[str]:
    """One kind per session, by rule rather than majority: a session holding any main-thread round is a
    main session (older clients wrote subagent work inline in it); otherwise guardian, then subagent.
    ``self_referenced`` (a round names its own session as parent) marks inline sidechain rounds, which
    only a main transcript holds, so such a session is main even when every round is a sidechain.
    None when no round carries a kind (a trace collected before the field existed)."""
    present = {k for k in kinds if k is not None}
    if self_referenced and present:
        return "main"
    for kind in ("main", "guardian", "subagent"):
        if kind in present:
            return kind
    return None


def session_parent(session_id: str, parents) -> Optional[str]:
    """The session's parent, ignoring self-references (an inline sidechain names its own session)."""
    return _most_common(p for p in parents if p is not None and p != session_id)


def build_sessions(con, rows: list[RoundRow]) -> list[dict[str, Any]]:
    errors_by_session = _session_errors(con)

    groups: dict[str, list[RoundRow]] = {}
    for r in rows:
        sid = r["session_id"]
        if sid:
            groups.setdefault(sid, []).append(r)

    parent_of = {
        sid: session_parent(sid, (r["parent_session_id"] for r in rs)) for sid, rs in groups.items()
    }
    kind_of = {
        sid: session_kind(
            (r["thread_kind"] for r in rs), any(r["parent_session_id"] == sid for r in rs)
        )
        for sid, rs in groups.items()
    }
    # Subagent children only: an automatic review names the session it reviewed, but it is not
    # delegated work (the session detail lists reviews separately).
    children_of: Counter[str] = Counter(
        p for sid, p in parent_of.items() if p is not None and kind_of[sid] != "guardian"
    )

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
                    price,
                    r["prefix"],
                    r["append"],
                    r["output"],
                    r["reasoning"],
                    cache_write_tokens=r["cache_write"],
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
            "threadKind": (kind := kind_of[sid]),
            # Role describes a delegated session (card label); a main session carries none. Parent and
            # workflow are served per session by build_session_topology, where the UI reads them.
            "agentRole": (
                _most_common(r["agent_role"] for r in rs) if kind in ("subagent", "guardian") else None
            ),
            # Per session, not per trace: an untagged session in a mixed trace is unknown, not 0.
            "childSessions": children_of.get(sid, 0) if kind is not None else None,
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

    return {
        "sessionId": session_id,
        "rounds": rounds_out,
        "tools": tools_stats,
        "topology": build_session_topology(con, session_id),
    }


def build_session_topology(con, session_id: str) -> dict[str, Any]:
    """Where one session sits in the subagent tree: its parent, its direct children, the workflows
    that launched them, and the automatic reviews of it (Codex ``guardian`` sessions naming it as
    the reviewed session; kept apart from children, which are delegated work). Reads the hierarchy
    through ``hierarchy_select`` so a trace carrying only some of the columns degrades to NULLs
    instead of failing. ``known`` is False when this session's rounds carry no thread kind (an
    older trace, or an older part of a mixed one)."""
    empty = {"known": False, "threadKind": None, "parentSessionId": None, "agentRole": None,
             "workflowId": None, "children": [], "workflows": [], "reviews": []}
    if not _has_round_column(con, "thread_kind"):
        return empty
    hierarchy = f"""
        WITH h AS (
            SELECT r.round_pk, r.session_id, r.prefix_tokens, r.newly_append_tokens, r.output_tokens,
                   {hierarchy_select(con)}
            FROM rounds r
        )"""
    own = con.execute(
        f"{hierarchy} SELECT thread_kind, parent_session_id, workflow_id, agent_role"
        " FROM h WHERE session_id = ?",
        [session_id],
    ).fetchall()
    kind = session_kind((row[0] for row in own), any(row[1] == session_id for row in own))
    if kind is None:
        return empty

    # Direct children: other sessions whose rounds name this session as parent (self-references are
    # inline sidechain rounds of this same session, not children).
    children: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    rows = con.execute(
        f"""{hierarchy},
        first_ev AS (
            SELECT round_pk, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us
            FROM timing_events WHERE event_index = 1
        )
        SELECT h.session_id, mode(h.agent_role), mode(h.workflow_id),
               bool_and(coalesce(h.thread_kind = 'guardian', false)),
               count(*), sum(h.prefix_tokens), sum(h.newly_append_tokens), sum(h.output_tokens),
               min(f.ts_us), max(f.ts_us)
        FROM h LEFT JOIN first_ev f USING (round_pk)
        WHERE h.parent_session_id = ? AND h.session_id <> ?
        GROUP BY h.session_id
        """,
        [session_id, session_id],
    ).fetchall()
    for (child_id, role, workflow, is_review, n, prefix, append, output, first_us, last_us) in rows:
        prefix, append = int(prefix or 0), int(append or 0)
        (reviews if is_review else children).append({
            "sessionId": child_id,
            "agentRole": role,
            "workflowId": workflow,
            "rounds": int(n),
            "inputTokens": prefix + append,
            "outputTokens": int(output or 0),
            "prefixShare": prefix / (prefix + append) if prefix + append else None,
            "firstTsUs": int(first_us) if first_us is not None else 0,
            "lastTsUs": int(last_us) if last_us is not None else 0,
        })
    children.sort(key=lambda c: (c["firstTsUs"], c["sessionId"]))
    reviews.sort(key=lambda c: (c["firstTsUs"], c["sessionId"]))

    by_workflow: dict[Optional[str], dict[str, Any]] = {}
    for c in children:
        w = by_workflow.setdefault(
            c["workflowId"], {"workflowId": c["workflowId"], "agents": 0, "rounds": 0}
        )
        w["agents"] += 1
        w["rounds"] += c["rounds"]
    workflows = sorted(by_workflow.values(), key=lambda w: w["rounds"], reverse=True)

    delegated = kind in ("subagent", "guardian")
    return {
        "known": True,
        "threadKind": kind,
        "parentSessionId": session_parent(session_id, (row[1] for row in own)),
        "agentRole": _most_common(row[3] for row in own) if delegated else None,
        "workflowId": _most_common(row[2] for row in own) if kind == "subagent" else None,
        "children": children,
        "workflows": workflows,
        "reviews": reviews,
    }
