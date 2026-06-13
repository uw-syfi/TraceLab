#!/usr/bin/env python3
"""Additive bookkeeping for the contributed-pool dashboard counters.

Every aggregate the dashboard shows (rounds, input tokens, provider split, contributor count) is
**additive**, so we maintain running totals instead of rescanning the pool on each contribution:
each accepted upload's subtotals are added to the totals in ``index.json`` (O(new rows)); a removed
upload subtracts its stored subtotals (O(1)). :func:`rebuild_stats` is the cold-path fallback that
recomputes everything from the immutable pool objects — used for recovery or integrity audits, and
the right hook if non-additive stats (percentiles, distinct users) are ever wanted here.

``input_tokens`` sums ``input_tokens_total`` (= ``prefix_tokens + newly_append_tokens``) — the same
field ``overview_summary`` sums, so the number matches the canonical metric (cached prefix is
re-counted per round; this is a recounted-prefix sum, not unique tokens).
"""

from __future__ import annotations

from typing import Any, Iterable


def empty_totals() -> dict[str, Any]:
    return {"rows": 0, "input_tokens": 0, "provider_counts": {}}


def subtotals(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one contribution's rows into ``{rows, input_tokens, provider_counts}``."""
    totals = empty_totals()
    for row in rows:
        totals["rows"] += 1
        value = row.get("input_tokens_total")
        if isinstance(value, bool):  # bool is an int subclass; never a token count
            value = None
        if isinstance(value, (int, float)):
            totals["input_tokens"] += int(value)
        provider = row.get("provider") or "unknown"
        counts = totals["provider_counts"]
        counts[provider] = counts.get(provider, 0) + 1
    return totals


def add_totals(base: dict[str, Any], delta: dict[str, Any], *, sign: int = 1) -> dict[str, Any]:
    """Return ``base (+|-) delta`` as a fresh totals dict (``sign=-1`` to subtract)."""
    out = {
        "rows": base["rows"] + sign * delta["rows"],
        "input_tokens": base["input_tokens"] + sign * delta["input_tokens"],
        "provider_counts": dict(base["provider_counts"]),
    }
    for provider, count in delta["provider_counts"].items():
        new = out["provider_counts"].get(provider, 0) + sign * count
        if new:
            out["provider_counts"][provider] = new
        else:
            out["provider_counts"].pop(provider, None)
    return out


def rebuild_stats(store: Any) -> dict[str, Any]:
    """Cold path: recompute running totals from the immutable pool objects.

    Single additive pass over ``store.iter_pool_rows()``. Reserved for recovery (lost/corrupt
    index) and integrity audits; never called on the contribute hot path.
    """
    return subtotals(store.iter_pool_rows())
