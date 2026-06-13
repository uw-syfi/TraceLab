"""Clickable "superlatives" — the argmax/argmin headline facts.

Each ``Fact`` is a pre-formatted headline (the compute side owns units/rounding) plus an optional
``sessionId`` / ``roundIndex`` deep-link so the card jumps into the session view. Mirrors the ``facts``
list in lib/mock/analytics.ts (dimensions: time / cost / session / tool). Facts whose underlying data
is absent are simply omitted, so a thin trace yields fewer cards rather than empty ones.

All computed from the shared per-round rows + session list + per-day buckets + a single tool-calls
scan — no extra heavy passes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from daily import _local
from pricing import price_for, round_cost
from rounds import RoundRow

_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MIN_TOOL_SAMPLE = 20  # ignore rarely-used tools in slowest/flakiest superlatives
# A session_id persists across days, so a session's raw first..last span folds in overnight idle.
# "Continuous" / "unattended" facts segment on round-to-round gaps: a step-to-step gap beyond this
# splits the sitting (the user stepped away), so we never count idle hours as continuous work.
_ACTIVE_GAP_US = 30 * 60 * 1_000_000  # 30 min


def _short(sid: Optional[str]) -> str:
    if not sid:
        return "?"
    head = sid.split("-")[0]
    return head if len(head) <= 12 else head[:12]


def _dur(seconds: float) -> str:
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    if s >= 60:
        return f"{s // 60} min"
    return f"{s}s"


def _tok(n: float) -> str:
    n = int(round(n))
    if n >= 100_000:
        return f"{n / 1000:.0f}K tok"
    if n >= 1000:
        return f"{n / 1000:.1f}K tok"
    return f"{n} tok"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def _money_k(x: float) -> str:
    return "$" + (f"{x / 1000:.1f}K" if x >= 1000 else f"{x:.0f}")


def _hour_label(h: int) -> str:
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}"


def _date_label(day: str) -> str:
    dt = datetime.strptime(day, "%Y-%m-%d")
    return f"{_MONTHS[dt.month]} {dt.day} {dt.year}"


def _streak(days: set[str]) -> Optional[tuple[int, str, str]]:
    """Longest run of consecutive calendar days. Returns (length, start_day, end_day)."""
    if not days:
        return None
    ordered = sorted(datetime.strptime(d, "%Y-%m-%d") for d in days)
    best_len, best_start, best_end = 1, ordered[0], ordered[0]
    run_len, run_start = 1, ordered[0]
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur - prev).days == 1:
            run_len += 1
        else:
            run_len, run_start = 1, cur
        if run_len > best_len:
            best_len, best_start, best_end = run_len, run_start, cur
    fmt = lambda d: f"{_MONTHS[d.month]} {d.day}"
    return best_len, fmt(best_start), fmt(best_end)


def _tool_facts(con) -> list[dict[str, Any]]:
    """Top / slowest-p90 / flakiest tool, from one scan of tool_calls (latency aggregated in Python
    since DuckDB 1.1 wasm lacks reliable QUANTILE_CONT)."""
    from _overview import percentile  # local import keeps module load cheap

    agg: dict[str, dict[str, Any]] = {}
    for tool_name, internal, wall, is_error in con.execute(
        "SELECT tool_name, tool_internal_latency_ms, tool_wall_latency_ms, is_error "
        "FROM tool_calls"
    ).fetchall():
        if not tool_name:
            continue
        a = agg.setdefault(tool_name, {"n": 0, "errors": 0, "lat": []})
        a["n"] += 1
        if is_error:
            a["errors"] += 1
        lat = internal if internal is not None else wall
        if lat is not None and lat > 0:
            a["lat"].append(float(lat))

    if not agg:
        return []

    facts: list[dict[str, Any]] = []
    top_name, top = max(agg.items(), key=lambda kv: kv[1]["n"])
    facts.append({
        "id": "top-tool", "dimension": "tool", "title": "Most-used tool",
        "value": top_name, "detail": f"{top['n']:,} calls across the trace",
    })

    eligible = {k: v for k, v in agg.items() if v["n"] >= _MIN_TOOL_SAMPLE}
    pool = eligible or agg
    p90 = {k: percentile(v["lat"], 0.9) for k, v in pool.items() if v["lat"]}
    if p90:
        slow_name = max(p90, key=p90.get)
        facts.append({
            "id": "slow-tool", "dimension": "tool", "title": "Slowest tool (p90)",
            "value": f"{slow_name} · {p90[slow_name] / 1000:.1f}s", "detail": "effective latency",
        })
    flaky_name = max(pool, key=lambda k: pool[k]["errors"] / pool[k]["n"])
    flaky_rate = pool[flaky_name]["errors"] / pool[flaky_name]["n"]
    if flaky_rate > 0:
        facts.append({
            "id": "flaky-tool", "dimension": "tool", "title": "Most error-prone tool",
            "value": f"{flaky_name} · {flaky_rate * 100:.0f}%",
            "detail": "share of calls returning an error",
        })
    return facts


def build_facts(
    con,
    rows: list[RoundRow],
    cost: dict[str, Any],
    sessions: list[dict[str, Any]],
    per_day: list[dict[str, Any]],
    tz_offset_min: int,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []

    # --- session-level superlatives (from the prebuilt session list) ---
    if sessions:
        most_rounds = max(sessions, key=lambda s: s["rounds"])
        facts.append({
            "id": "longest-convo", "dimension": "session", "title": "Longest conversation",
            "value": f"{most_rounds['rounds']} steps", "detail": f"Session {_short(most_rounds['sessionId'])}",
            "sessionId": most_rounds["sessionId"],
        })
        priciest = max(sessions, key=lambda s: s["costUsd"])
        if priciest["costUsd"] > 0:
            facts.append({
                "id": "priciest-session", "dimension": "cost", "title": "Most expensive session",
                "value": _money(priciest["costUsd"]), "detail": f"Session {_short(priciest['sessionId'])}",
                "sessionId": priciest["sessionId"],
            })

    # --- continuous-session / unattended-run facts (gap-aware) ---
    # A raw first..last span over-counts: a session_id can sprawl across days of idle. We split each
    # session's rounds into active segments (no step-to-step gap > _ACTIVE_GAP_US), measure wall time
    # within a segment, and take the longest. "Continuous" = longest active segment; "unattended" =
    # longest sub-run of consecutive tool-result rounds inside one segment (so idle never inflates it).
    sess_rounds: dict[str, list[RoundRow]] = {}
    for r in rows:
        if r["session_id"]:
            sess_rounds.setdefault(r["session_id"], []).append(r)

    def _span(seg: list[RoundRow]) -> Optional[tuple[float, int]]:
        ts = [x["first_ts_us"] for x in seg if x["first_ts_us"] is not None]
        if len(seg) < 2 or len(ts) < 2:
            return None
        return (max(ts) - min(ts)) / 1_000_000, len(seg)

    best_cont = None  # (duration_s, steps, sessionId)
    best_run = None   # (duration_s, steps, sessionId)
    for sid, rs in sess_rounds.items():
        rs = sorted(rs, key=lambda x: x["round_index"])
        segment: list[RoundRow] = []  # contiguous active window
        run: list[RoundRow] = []      # consecutive tool-result rounds within the window
        last_ts: Optional[int] = None

        def _close_run():
            nonlocal best_run
            s = _span(run)
            if s and (best_run is None or s[0] > best_run[0]):
                best_run = (s[0], s[1], sid)
            run.clear()

        def _close_segment():
            nonlocal best_cont
            s = _span(segment)
            if s and (best_cont is None or s[0] > best_cont[0]):
                best_cont = (s[0], s[1], sid)
            segment.clear()

        for r in rs:
            ts = r["first_ts_us"]
            if ts is not None and last_ts is not None and ts - last_ts > _ACTIVE_GAP_US:
                _close_run()        # the gap breaks both the run and the sitting
                _close_segment()
            segment.append(r)
            if r["is_user_input"]:
                _close_run()
            else:
                run.append(r)
            if ts is not None:
                last_ts = ts
        _close_run()
        _close_segment()

    if best_cont is not None:
        facts.append({
            "id": "longest-wallclock", "dimension": "time", "title": "Longest continuous session",
            "value": _dur(best_cont[0]),
            "detail": f"Session {_short(best_cont[2])} · {best_cont[1]} steps, no break over 30 min",
            "sessionId": best_cont[2],
        })
    if best_run is not None:
        facts.append({
            "id": "longest-autonomous", "dimension": "time", "title": "Longest unattended run",
            "value": _dur(best_run[0]), "detail": f"{best_run[1]} tool-result steps with no human turn",
            "sessionId": best_run[2],
        })

    # --- per-round superlatives (one pass: cost, context peak, output, reasoning, think-ratio) ---
    max_cost = max_context = max_output = max_reason = max_ratio = None
    for r in rows:
        price = price_for(r["provider"] or "", r["model"])
        rc = round_cost(price, r["prefix"], r["append"], r["output"], r["reasoning"])["total"] if price else 0.0
        context = r["prefix"] + r["append"]
        if max_cost is None or rc > max_cost[0]:
            max_cost = (rc, r)
        if max_context is None or context > max_context[0]:
            max_context = (context, r)
        if max_output is None or r["output"] > max_output[0]:
            max_output = (r["output"], r)
        if max_reason is None or r["reasoning"] > max_reason[0]:
            max_reason = (r["reasoning"], r)
        if r["output"] > 0 and r["reasoning"] > 0:
            ratio = r["reasoning"] / r["output"]
            if max_ratio is None or ratio > max_ratio[0]:
                max_ratio = (ratio, r)

    def _link(r: RoundRow) -> dict[str, Any]:
        return {"sessionId": r["session_id"], "roundIndex": r["round_index"]}

    if max_cost and max_cost[0] > 0:
        rc, r = max_cost
        facts.append({
            "id": "priciest-round", "dimension": "cost", "title": "Most expensive round",
            "value": _money(rc), "detail": f"{_tok(r['prefix'] + r['append'])} context reused", **_link(r),
        })
    facts.append({
        "id": "cache-saved", "dimension": "cost", "title": "Saved by prefix cache",
        "value": _money_k(cost["cacheSavingsUsd"]), "detail": "vs. billing every cached token fresh",
    })
    if max_context and max_context[0] > 0:
        ctx, r = max_context
        facts.append({
            "id": "context-peak", "dimension": "session", "title": "Context peak",
            "value": _tok(ctx), "detail": "fullest single input", **_link(r),
        })
    if max_output and max_output[0] > 0:
        out, r = max_output
        facts.append({
            "id": "biggest-output", "dimension": "session", "title": "Biggest single output",
            "value": _tok(out), "detail": "one round", **_link(r),
        })
    if max_reason and max_reason[0] > 0:
        rea, r = max_reason
        facts.append({
            "id": "longest-think", "dimension": "session", "title": "Deepest single think",
            "value": _tok(rea), "detail": "reasoning tokens in one round", **_link(r),
        })
    if max_ratio and max_ratio[0] > 0:
        ratio, r = max_ratio
        facts.append({
            "id": "think-ratio", "dimension": "session", "title": "Most think-heavy round",
            "value": f"{ratio:.1f}×", "detail": "reasoning : output ratio", **_link(r),
        })

    # --- day / hour superlatives ---
    if per_day:
        busiest = max(per_day, key=lambda p: p["rounds"])
        facts.append({
            "id": "busiest-day", "dimension": "time", "title": "Busiest day",
            "value": f"{busiest['rounds']} steps", "detail": _date_label(busiest["day"]),
        })
    day_set: set[str] = set()
    hour_counts = [0] * 24
    for r in rows:
        ts = r["first_ts_us"]
        if ts is None:
            continue
        lt = _local(ts, tz_offset_min)
        day_set.add(lt.strftime("%Y-%m-%d"))
        hour_counts[lt.hour] += 1
    streak = _streak(day_set)
    if streak and streak[0] >= 2:
        facts.append({
            "id": "streak", "dimension": "time", "title": "Longest daily streak",
            "value": f"{streak[0]} days", "detail": f"{streak[1]} → {streak[2]}",
        })
    if any(hour_counts):
        peak = max(range(24), key=lambda h: hour_counts[h])
        facts.append({
            "id": "night-owl", "dimension": "time", "title": "Peak hour",
            "value": _hour_label(peak), "detail": f"Most steps land {peak:02d}:00–{(peak + 1) % 24:02d}:00 local",
        })

    facts.extend(_tool_facts(con))
    return facts
