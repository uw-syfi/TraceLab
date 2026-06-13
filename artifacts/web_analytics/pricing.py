"""Model price resolution + cost math for the web analytics payload.

The PRICE TABLE itself is NOT here — it lives in the single-source JSON at
``artifacts/utils/pricing.json`` (the same file the web mock reads via ``lib/analytics/cost.ts``),
so prices are edited in exactly one place. This module mirrors that file's resolve rules
(exact ``provider:model`` -> family lowercase-substring -> unpriced) so native and in-browser
(Pyodide) cost numbers reconcile with the mock.

USD per 1,000,000 tokens. ``cachedInputPerM`` is the cache-READ rate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TypedDict

# pricing.json lives one level up under utils/. Resolve relative to this file so it works both
# natively (artifacts/web_analytics -> artifacts/utils) and under Pyodide (/repo/artifacts/...).
_PRICING_PATH = Path(__file__).resolve().parent.parent / "utils" / "pricing.json"

_PER_M = 1_000_000


class ModelPrice(TypedDict):
    inputPerM: float
    cachedInputPerM: float
    outputPerM: float


def _load_table() -> dict:
    with open(_PRICING_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_TABLE = _load_table()
#: When the prices were last reviewed — surface near any cost number as a disclaimer.
PRICING_AS_OF: str = _TABLE.get("as_of", "")
_EXACT: dict = _TABLE.get("exact", {})
_FAMILY: list = _TABLE.get("family", [])


def price_for(provider: str, model: Optional[str]) -> Optional[ModelPrice]:
    """Resolve a price for (provider, model). Returns ``None`` when truly unknown (caller marks
    the rounds *unpriced* rather than guessing). Matches ``priceFor`` in lib/analytics/cost.ts."""
    if not model:
        return None
    exact = _EXACT.get(f"{provider}:{model}")
    if exact:
        return {
            "inputPerM": exact["inputPerM"],
            "cachedInputPerM": exact["cachedInputPerM"],
            "outputPerM": exact["outputPerM"],
        }
    lowered = model.lower()
    for fam in _FAMILY:
        if fam["match"] in lowered:
            return {
                "inputPerM": fam["inputPerM"],
                "cachedInputPerM": fam["cachedInputPerM"],
                "outputPerM": fam["outputPerM"],
            }
    return None


def _per_token(per_m: float) -> float:
    return per_m / _PER_M


class RoundCost(TypedDict):
    inputCost: float
    cachedCost: float
    outputCost: float
    reasoningCost: float
    total: float


def round_cost(
    price: ModelPrice,
    prefix_tokens: float,
    append_tokens: float,
    output_tokens: float,
    reasoning_tokens: float = 0,
) -> RoundCost:
    """Cost of one round (or a summed bucket) given its token split. Mirrors ``roundCost`` in
    cost.ts: append billed at fresh-input rate, prefix at cache-read rate, output at output rate;
    ``reasoningCost`` is the reasoning slice of output (already included in ``total`` via output)."""
    input_cost = append_tokens * _per_token(price["inputPerM"])
    cached_cost = prefix_tokens * _per_token(price["cachedInputPerM"])
    output_cost = output_tokens * _per_token(price["outputPerM"])
    reasoning_cost = (reasoning_tokens or 0) * _per_token(price["outputPerM"])
    return {
        "inputCost": input_cost,
        "cachedCost": cached_cost,
        "outputCost": output_cost,
        "reasoningCost": reasoning_cost,
        "total": input_cost + cached_cost + output_cost,
    }


def cache_savings(price: ModelPrice, prefix_tokens: float) -> float:
    """What prefix caching saved vs. billing those cached tokens at the fresh-input rate."""
    return prefix_tokens * _per_token(price["inputPerM"] - price["cachedInputPerM"])
