"""Cost decomposition by model + provider, from the per-round rows.

Groups rounds by (provider, model), bills each bucket's token split through ``pricing`` (append at
fresh-input rate, prefix at cache-read rate, output at output rate), and marks unknown models
*unpriced* rather than guessing. Mirrors ``buildCost`` in lib/mock/analytics.ts so the real payload
reconciles with the mock. Cost is computed HERE (Python); the frontend only displays the dollars.
"""

from __future__ import annotations

from typing import Any

from pricing import cache_savings, price_for, round_cost
from rounds import RoundRow


def build_cost(rows: list[RoundRow]) -> dict[str, Any]:
    """Return the ``CostBreakdown`` shape (byModel sorted by spend, savings, priced/unpriced)."""
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        provider = r["provider"] or "unknown"
        model = r["model"] or "unknown"
        key = (provider, model)
        b = buckets.get(key)
        if b is None:
            b = {"provider": provider, "model": model, "rounds": 0,
                 "prefix": 0, "append": 0, "output": 0, "reasoning": 0}
            buckets[key] = b
        b["rounds"] += 1
        b["prefix"] += r["prefix"]
        b["append"] += r["append"]
        b["output"] += r["output"]
        b["reasoning"] += r["reasoning"]

    by_model: list[dict[str, Any]] = []
    cache_savings_usd = 0.0
    reasoning_cost_usd = 0.0
    priced_rounds = 0
    unpriced_rounds = 0

    for b in buckets.values():
        price = price_for(b["provider"], b["model"])
        if price is None:
            by_model.append({
                "provider": b["provider"], "model": b["model"], "rounds": b["rounds"],
                "inputCost": 0.0, "cachedCost": 0.0, "outputCost": 0.0,
                "reasoningCost": 0.0, "costUsd": 0.0, "priced": False,
            })
            unpriced_rounds += b["rounds"]
            continue
        rc = round_cost(price, b["prefix"], b["append"], b["output"], b["reasoning"])
        by_model.append({
            "provider": b["provider"], "model": b["model"], "rounds": b["rounds"],
            "inputCost": rc["inputCost"], "cachedCost": rc["cachedCost"],
            "outputCost": rc["outputCost"], "reasoningCost": rc["reasoningCost"],
            "costUsd": rc["total"], "priced": True,
        })
        cache_savings_usd += cache_savings(price, b["prefix"])
        reasoning_cost_usd += rc["reasoningCost"]
        priced_rounds += b["rounds"]

    by_model.sort(key=lambda m: m["costUsd"], reverse=True)
    return {
        "byModel": by_model,
        "cacheSavingsUsd": cache_savings_usd,
        "reasoningCostUsd": reasoning_cost_usd,
        "pricedRounds": priced_rounds,
        "unpricedRounds": unpriced_rounds,
    }


def build_providers(by_model: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Provider-level rollup (rounds + cost + shares) for the Providers split band — precomputed so
    the frontend reads it instead of aggregating ``cost.byModel`` itself."""
    agg: dict[str, dict[str, float]] = {}
    for m in by_model:
        p = agg.setdefault(m["provider"], {"rounds": 0, "cost": 0.0})
        p["rounds"] += m["rounds"]
        p["cost"] += m["costUsd"]
    total_rounds = sum(p["rounds"] for p in agg.values()) or 1
    total_cost = sum(p["cost"] for p in agg.values()) or 1.0
    out = [
        {
            "provider": provider,
            "rounds": int(p["rounds"]),
            "cost": p["cost"],
            "stepShare": p["rounds"] / total_rounds,
            "costShare": p["cost"] / total_cost,
        }
        for provider, p in agg.items()
    ]
    out.sort(key=lambda x: x["rounds"], reverse=True)
    return out
