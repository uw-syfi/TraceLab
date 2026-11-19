"""Shared request ownership and timing. Missing lifecycle evidence stays missing."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from timing import effective_tool_seconds, human_waits_from_event_pairs, union_seconds
from subagent_cohorts import session_kinds_sql

OUTPUT_TYPES = {"reasoning", "text", "tool_call"}
INPUT_TYPES = {"user_message", "tool_result"}
LIFECYCLE_TYPES = {"request_start", "request_end", "request_aborted"}
INTERACTION_TOOLS = {"AskUserQuestion", "ExitPlanMode", "request_user_input", "request_user_input_async"}
EPOCH = datetime(1970, 1, 1)


def _columns(con, table):
    return {row[0] for row in con.execute(f"DESCRIBE {table}").fetchall()}


def load_events(con):
    """Return (type, epoch microseconds, native turn ID) in recorded event order."""
    turn = "CAST(turn_id AS VARCHAR)" if "turn_id" in _columns(con, "timing_events") else "NULL"
    events = defaultdict(list)
    for rpk, kind, stamp, turn_id, call_id in con.execute(
        f"SELECT round_pk, event_type, CASE WHEN isfinite(timestamp) THEN epoch_us(timestamp) END, {turn}, tool_call_id "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        events[rpk].append((kind, stamp, turn_id, call_id))
    return events


def generation_interval(events):
    inputs = [stamp for kind, stamp, *_ in events if kind in INPUT_TYPES and stamp is not None]
    outputs = [stamp for kind, stamp, *_ in events if kind in OUTPUT_TYPES and stamp is not None]
    if not inputs or not outputs:
        return None
    candidates = [stamp for stamp in inputs if stamp <= min(outputs)]
    return (max(candidates), max(outputs)) if candidates and max(outputs) > max(candidates) else None


@dataclass
class Request:
    provider: str
    session_id: str
    session_kind: str = "unknown"
    turn_id: str | None = None
    start_us: int | None = None
    end_us: int | None = None
    status: str = "legacy"
    round_pks: list[int] = field(default_factory=list)
    has_completion: bool = False

    @property
    def elapsed_seconds(self):
        if self.status != "complete" or self.start_us is None or self.end_us is None:
            return None
        return (self.end_us - self.start_us) / 1_000_000 if self.end_us > self.start_us else None


def collect_requests(con, events=None):
    """Keep legacy request groups for counts; only native completed lifecycles yield duration.

    Native IDs own rows and markers, even when a late result or overlapping turn occurs.
    Claude completion markers have no turn ID and close the observed user-triggered group.
    Neither another prompt nor EOF fabricates a completion.
    """
    events = load_events(con) if events is None else events
    session_kinds = {(provider, sid): kind for provider, sid, kind in con.execute(session_kinds_sql(con)).fetchall()}
    columns = _columns(con, "rounds")
    turn = "CAST(turn_id AS VARCHAR)" if "turn_id" in columns else "NULL"
    kind = "session_kind" if "session_kind" in columns else "'unknown'"
    rows = con.execute(
        f"SELECT round_pk, provider, session_id, {turn}, {kind} FROM rounds ORDER BY round_pk"
    ).fetchall()
    lifecycle_keys = {
        (provider, sid, event_turn or turn_id)
        for rpk, provider, sid, turn_id, _ in rows
        for event_type, _, event_turn, *_ in events.get(rpk, [])
        if event_type in LIFECYCLE_TYPES and (event_turn or turn_id) is not None
    }
    native_keys = lifecycle_keys | {(provider, sid, turn_id) for _, provider, sid, turn_id, _ in rows
                                    if turn_id is not None}
    requests = []
    native = {}
    current = {}

    def new(provider, sid, session_kind, turn_id=None):
        request = Request(provider or "<unknown-provider>", sid, session_kinds.get((provider, sid), "unknown"), turn_id)
        requests.append(request)
        return request

    for rpk, provider, sid, turn_id, session_kind in rows:
        if not isinstance(sid, str) or not sid:
            continue
        ev = events.get(rpk, [])
        key = (provider, sid)
        marker_ids = {event_turn for typ, _, event_turn, *_ in ev
                      if typ in LIFECYCLE_TYPES and event_turn is not None}
        if turn_id is None and len(marker_ids) == 1:
            turn_id = next(iter(marker_ids))
        native_key = (*key, turn_id)
        request = None
        if native_key in native_keys:
            request = native.setdefault(native_key, None)
            if request is None:
                request = native[native_key] = new(provider, sid, session_kind, turn_id)
                request.status = "open" if native_key in lifecycle_keys else "legacy"
        else:
            inputs = [stamp for typ, stamp, *_ in ev if typ == "user_message" and stamp is not None]
            outputs = [stamp for typ, stamp, *_ in ev if typ in OUTPUT_TYPES and stamp is not None]
            candidates = [stamp for stamp in inputs if outputs and stamp <= min(outputs)]
            if candidates:
                request = new(provider, sid, session_kind, turn_id)
                request.start_us = max(candidates)
                current[key] = request
            else:
                request = current.get(key)
        if request is not None:
            request.round_pks.append(rpk)

        for event_type, stamp, event_turn, *_ in ev:
            if event_type not in LIFECYCLE_TYPES:
                continue
            marker_id = event_turn or turn_id
            owner = request
            if marker_id is not None:
                marker_key = (*key, marker_id)
                owner = native.get(marker_key)
                if owner is None:
                    owner = native[marker_key] = new(provider, sid, session_kind, marker_id)
                    owner.status = "open"
            if owner is None:
                continue
            if event_type == "request_start":
                if owner.start_us is not None and owner.start_us != stamp:
                    owner.status = "invalid"
                else:
                    owner.start_us = stamp
                    if owner.status not in {"invalid", "aborted"}:
                        owner.status = "open"
            elif event_type == "request_aborted":
                owner.end_us = stamp
                owner.status = "aborted"
            else:
                owner.has_completion = True
                if owner.end_us is not None and owner.end_us != stamp:
                    owner.status = "invalid"
                elif owner.status not in {"aborted", "invalid"}:
                    owner.end_us = stamp
                    owner.status = "complete" if stamp is not None and owner.start_us is not None else "missing_start"
    for request in requests:
        if request.has_completion and request.status not in {"aborted", "invalid"}:
            request.status = "missing_start" if request.start_us is None else (
                "complete" if request.end_us is not None and request.end_us > request.start_us else "invalid")
    return requests


def load_tools(con):
    tools = defaultdict(list)
    for rpk, name, start, end, internal, wall in con.execute(
        "SELECT round_pk, tool_name, CASE WHEN isfinite(emitted_at) THEN epoch_us(emitted_at) END, "
        "CASE WHEN isfinite(result_at) THEN epoch_us(result_at) END, "
        "tool_internal_latency_ms, tool_wall_latency_ms FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        tools[rpk].append((name, start, end, internal, wall))
    return tools


def timing_summary(round_pks, events, tools, *, start_us=None, end_us=None):
    """Attributed work and separate bounded occupancy. Internal durations are never clipped."""
    stamps = []
    intervals = []
    generation = []
    effective = interaction = 0.0
    missing = invalid_endpoints = 0
    compressed = False
    calls = 0
    unmeasured_observations = 0
    for rpk in round_pks:
        ev = events.get(rpk, [])
        stamps.extend(stamp for _, stamp, *_ in ev if stamp is not None)
        unmeasured_observations += sum(kind == "web_search_observation" and not call_id
            for kind, _, _, call_id in ev)
        gen = generation_interval(ev)
        if gen is not None:
            generation.append(gen)
        for name, a, b, internal, wall in tools.get(rpk, []):
            calls += 1
            value = effective_tool_seconds(internal, wall)
            missing += value is None
            effective += value or 0.0
            if name in INTERACTION_TOOLS:
                interaction += value or 0.0
            if a is None or b is None or b < a:
                invalid_endpoints += 1
                continue
            stamps.extend((a, b))
            intervals.append((a, b))
            # Positive runner time cannot fit an exactly zero recorded interval.
            # No duration threshold: even subsecond contradictions invalidate occupancy.
            if a == b and internal is not None and internal > 0:
                compressed = True
    observed = (max(stamps) - min(stamps)) / 1_000_000 if stamps else None
    bounds = (start_us, end_us) if start_us is not None and end_us is not None else None
    if bounds is not None:
        lo, hi = bounds
        intervals = [(max(a, lo), min(b, hi)) for a, b in intervals if min(b, hi) >= max(a, lo)]
    outstanding = None if invalid_endpoints or compressed or unmeasured_observations else union_seconds(intervals)
    return {
        "gen": sum((b - a) / 1_000_000 for a, b in generation),
        "tool": effective, "interaction": interaction, "outstanding": outstanding,
        "observed_wall": observed, "wall": None if compressed else observed,
        "missing_tool_timing": missing + unmeasured_observations,
        "unmeasured_tool_observations": unmeasured_observations, "invalid_tool_endpoints": invalid_endpoints,
        "compressed_timestamps": compressed, "tool_calls": calls,
    }


def collect_session_timing(con, events=None, tools=None):
    events = load_events(con) if events is None else events
    tools = load_tools(con) if tools is None else tools
    columns = _columns(con, "rounds")
    kind = "session_kind" if "session_kind" in columns else "'unknown'"
    groups = {}
    session_kinds = {(provider, sid): kind for provider, sid, kind in con.execute(session_kinds_sql(con)).fetchall()}
    for rpk, provider, sid, session_kind in con.execute(
        f"SELECT round_pk, provider, session_id, {kind} FROM rounds ORDER BY round_pk"
    ).fetchall():
        if isinstance(sid, str) and sid:
            group = groups.setdefault((provider, sid), {"provider": provider, "session_id": sid,
                "session_kind": session_kinds.get((provider, sid), "unknown"), "round_pks": []})
            group["round_pks"].append(rpk)
    for group in groups.values():
        group.update(timing_summary(group["round_pks"], events, tools))
        last = None
        waits = []
        for rpk in group["round_pks"]:
            pairs = [(kind, EPOCH + timedelta(microseconds=stamp) if stamp is not None else None)
                     for kind, stamp, *_ in events.get(rpk, [])]
            values, _, last = human_waits_from_event_pairs(pairs, last)
            waits.extend(values)
        group.update(human=sum(waits), human_capped=sum(min(w, 3600) for w in waits), human_waits=waits)
    return groups


def collect_request_timing(con, events=None, tools=None, requests=None):
    events = load_events(con) if events is None else events
    tools = load_tools(con) if tools is None else tools
    requests = collect_requests(con, events) if requests is None else requests
    rows = []
    for request in requests:
        measured = timing_summary(request.round_pks, events, tools,
                                  start_us=request.start_us, end_us=request.end_us)
        measured.update(provider=request.provider, session_id=request.session_id,
                        session_kind=request.session_kind, status=request.status,
                        round_pks=request.round_pks, turn_id=request.turn_id,
                        e2e=None if measured["compressed_timestamps"] else request.elapsed_seconds)
        if measured["e2e"] is None:
            measured["outstanding"] = None
        rows.append(measured)
    return rows


def request_ownership_coverage(con, requests=None, events=None, tools=None):
    """Account for recorded rows outside request groups without inventing an owner."""
    events = load_events(con) if events is None else events
    tools = load_tools(con) if tools is None else tools
    requests = collect_requests(con, events) if requests is None else requests
    owned = {rpk for request in requests for rpk in request.round_pks}
    has_tokens = "output_tokens" in _columns(con, "rounds")
    tokens = "output_tokens" if has_tokens else "NULL"
    coverage = {}

    def bucket(provider):
        return coverage.setdefault(provider, {"rounds": 0, "owned_rounds": 0, "unowned_rounds": 0,
            "unowned_output_tokens": 0 if has_tokens else None, "unowned_tool_calls": 0,
            "unowned_tool_seconds": 0.0, "unowned_lifecycle": {kind: 0 for kind in sorted(LIFECYCLE_TYPES)}})

    bucket("all")
    for rpk, provider, output_tokens in con.execute(f"SELECT round_pk, provider, {tokens} FROM rounds").fetchall():
        provider = provider or "<unknown-provider>"
        for scope in ("all", provider):
            record = bucket(scope)
            record["rounds"] += 1
            if rpk in owned:
                record["owned_rounds"] += 1
                continue
            record["unowned_rounds"] += 1
            if has_tokens:
                record["unowned_output_tokens"] += int(output_tokens or 0)
            for _, _, _, internal, wall in tools.get(rpk, []):
                record["unowned_tool_calls"] += 1
                record["unowned_tool_seconds"] += effective_tool_seconds(internal, wall) or 0.0
            for kind, *_ in events.get(rpk, []):
                if kind in LIFECYCLE_TYPES:
                    record["unowned_lifecycle"][kind] += 1
    return coverage


def ownership_coverage_note(coverage):
    record = coverage["all"]
    markers = sum(record["unowned_lifecycle"].values())
    return (f"Request groups own {record['owned_rounds']:,} of {record['rounds']:,} recorded rounds. "
            f"The {record['unowned_rounds']:,} unowned rounds retain {record['unowned_tool_calls']:,} tool calls "
            f"and {record['unowned_tool_seconds']:,.3f} seconds of known attributed tool work in session/step totals, "
            f"but are excluded from request statistics. {markers:,} lifecycle markers lack request ownership. "
            "No request or completion is invented for them.")


def render_ownership_coverage(coverage):
    import md_table
    rows = []
    for provider, record in coverage.items():
        tokens = record["unowned_output_tokens"]
        markers = record["unowned_lifecycle"]
        rows.append([provider, f"{record['owned_rounds']:,} / {record['rounds']:,}",
            f"{record['unowned_rounds']:,}", "N/A" if tokens is None else f"{tokens:,}",
            f"{record['unowned_tool_calls']:,}", f"{record['unowned_tool_seconds']:,.3f}",
            " / ".join(str(markers[kind]) for kind in ("request_start", "request_end", "request_aborted"))])
    return ("**Request ownership coverage**\n\n" + ownership_coverage_note(coverage) + "\n\n" +
            md_table.gfm_table(["Provider", "Owned / recorded rounds", "Unowned rounds", "Unowned output tokens",
                "Unowned tool calls", "Known unowned tool seconds", "Unowned start / end / abort markers"], rows))


def render_request_audit(con, title="Request lifecycle and timing audit"):
    from collections import Counter
    rows = collect_request_timing(con)
    lines = [f"# {title}", "", "Request ownership is shared with count, cost, and timing reports. "
        "Only native completed lifecycles with consistent timestamps have response durations. "
        "Legacy groups remain countable; next prompts and EOF never imply completion. "
        "Canonical attributed tool work uses positive internal-else-wall latency, includes interaction tools, "
        "and is not divided by a shorter response span. Outstanding intervals are a separate bounded wall metric.", "",
        "| Provider | Groups | Native complete | Measurable durations | Legacy | Open/missing start | Aborted | Invalid | Missing tool timing/observations | Compressed captures |",
        "| :-- | --: | --: | --: | --: | --: | --: | --: | --: | --: |"]
    for provider in ["all", *sorted({row["provider"] for row in rows})]:
        members = [row for row in rows if provider == "all" or row["provider"] == provider]
        status = Counter(row["status"] for row in members)
        values = [len(members), status["complete"], sum(row["e2e"] is not None for row in members),
            status["legacy"], status["open"] + status["missing_start"], status["aborted"], status["invalid"],
            sum(row["missing_tool_timing"] for row in members), sum(row["compressed_timestamps"] for row in members)]
        lines.append("| " + " | ".join([provider, *map(str, values)]) + " |")
    return "\n".join(lines) + "\n\n" + render_ownership_coverage(request_ownership_coverage(con)) + "\n"
