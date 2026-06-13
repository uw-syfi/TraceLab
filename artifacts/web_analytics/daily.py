"""Per-day activity + work-rhythm (hour × weekday) buckets, in the viewer's LOCAL time.

Timestamps in the DB are naive UTC microseconds. The frontend passes its UTC offset
(``tz_offset_min`` = ``-new Date().getTimezoneOffset()``, so ``local = utc + offset``); we shift
each round's start timestamp by it before bucketing, so "busiest day" / "night owl" read in the
user's own clock. Mirrors ``buildPerDay`` / ``buildHourWeekday`` in lib/mock/analytics.ts.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pricing import price_for, round_cost
from rounds import RoundRow

_EPOCH = datetime(1970, 1, 1)


def _local(ts_us: int, tz_offset_min: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ts_us + tz_offset_min * 60_000_000)


def build_per_day(rows: list[RoundRow], tz_offset_min: int) -> list[dict[str, Any]]:
    """One row per local calendar day with activity: rounds, input/output tokens, and USD cost."""
    days: dict[str, dict[str, Any]] = {}
    for r in rows:
        ts = r["first_ts_us"]
        if ts is None:
            continue
        day = _local(ts, tz_offset_min).strftime("%Y-%m-%d")
        d = days.get(day)
        if d is None:
            d = {"day": day, "rounds": 0, "inputTokens": 0, "outputTokens": 0, "costUsd": 0.0}
            days[day] = d
        d["rounds"] += 1
        d["inputTokens"] += r["prefix"] + r["append"]
        d["outputTokens"] += r["output"]
        price = price_for(r["provider"] or "", r["model"])
        if price is not None:
            d["costUsd"] += round_cost(price, r["prefix"], r["append"], r["output"], r["reasoning"])["total"]
    return [days[k] for k in sorted(days)]


def build_hour_weekday(rows: list[RoundRow], tz_offset_min: int) -> list[dict[str, Any]]:
    """Dense 7×24 grid of round counts by (weekday 0=Mon..6=Sun, hour 0..23) in local time."""
    grid: dict[tuple[int, int], int] = {}
    for r in rows:
        ts = r["first_ts_us"]
        if ts is None:
            continue
        local = _local(ts, tz_offset_min)
        key = (local.weekday(), local.hour)  # datetime.weekday(): Mon=0..Sun=6 (matches contract)
        grid[key] = grid.get(key, 0) + 1
    out: list[dict[str, Any]] = []
    for weekday in range(7):
        for hour in range(24):
            out.append({"weekday": weekday, "hour": hour, "rounds": grid.get((weekday, hour), 0)})
    return out
