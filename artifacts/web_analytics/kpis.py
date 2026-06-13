"""Top-of-page headline numbers (the ``Kpis`` shape).

Session/user/round counts align with ``overview_summary``: a session is a distinct ``session_id``
string and a user a distinct ``user`` string (so totals cross-check against the existing summary).
Token totals are the prefix/append split summed. ``firstTsUs``/``lastTsUs`` span the round start
timestamps; ``activeDays`` is the number of local days with activity (= ``len(perDay)``).
"""

from __future__ import annotations

from typing import Any

from rounds import RoundRow


def build_kpis(
    rows: list[RoundRow],
    *,
    total_cost_usd: float,
    cache_savings_usd: float,
    active_days: int,
) -> dict[str, Any]:
    cached = sum(r["prefix"] for r in rows)
    uncached = sum(r["append"] for r in rows)
    output = sum(r["output"] for r in rows)
    sessions = len({r["session_id"] for r in rows if r["session_id"]})
    # Distinct contributor ids when the trace carries them (SYFI/normalized); a raw local export has
    # no `user` attribution, so fall back to 1 — it's one person's own trace, not zero users.
    users = len({r["user"] for r in rows if r["user"]}) or (1 if rows else 0)
    times = [r["first_ts_us"] for r in rows if r["first_ts_us"] is not None]
    return {
        "sessions": sessions,
        "users": users,
        "rounds": len(rows),
        "inputTokens": cached + uncached,
        "cachedInputTokens": cached,
        "uncachedInputTokens": uncached,
        "outputTokens": output,
        "totalCostUsd": total_cost_usd,
        "cacheSavingsUsd": cache_savings_usd,
        "firstTsUs": min(times) if times else 0,
        "lastTsUs": max(times) if times else 0,
        "activeDays": active_days,
    }
