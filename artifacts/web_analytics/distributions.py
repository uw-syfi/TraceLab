"""The eight distribution figures, as data (``SeriesSpec``) instead of PNGs.

Keys match lib/mock/analytics.ts so the interactive charts read real data unchanged:

  cdf       tool_latency_distribution   per-provider effective tool latency (ms, x-log)
  boxplot   tool_latency_by_category    effective latency grouped by tool category (ms, y-log)
  barh      tool_call_counts            calls per tool name (top N)
  scatter   prefix_append_scatter       per-round (prefix, append) cloud, per provider (x/y-log)
  histogram output_tokens               output tokens / round freq-polygon, per provider (x-log)
  cdf       generation_time_cdf         observable generation time (s), per provider
  histogram cache_hit_ratio             per-round prefix hit-ratio bins
  cdf       human_input_wait            human wait between turns (s, x-log)

The per-provider latency / generation / wait series reuse the summary's already-collected raw lists
(``SummaryBundle.by_provider[p].*`` / ``.merged.*``) — same effective-latency logic, nonpositive
already excluded — so they reconcile with the tool-latency / overview experiments. CDFs are
downsampled to a fixed point budget so the payload stays small even at 50k rounds; scatter clouds are
systematically subsampled per provider. Nothing here is formatted — axis labels/units are in the spec.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from _overview import percentile
from rounds import RoundRow
from trace_db import EFFECTIVE_TOOL_LATENCY_MS_SQL

_CDF_POINTS = 56      # samples along each CDF curve
_SCATTER_CAP = 700    # max points per scatter series (per provider)
_TOP_TOOLS = 14       # bars in tool_call_counts


def _cdf_series(name: str, values: list[float], *, positive_only: bool, digits: int = 3) -> dict[str, Any]:
    """One CDF series: ``points = [[x, cumulative_share], …]`` sampled to a fixed budget.

    ``positive_only`` drops <=0 values (x-log axes can't show them). Points are sampled by sorted
    index so x is monotonic and each y is the true empirical share at that x; consecutive duplicate
    x are collapsed (keeping the highest share)."""
    vals = sorted(v for v in values if (v > 0 if positive_only else v is not None))
    m = len(vals)
    points: list[list[float]] = []
    last_x: Optional[float] = None
    for k in range(1, _CDF_POINTS + 1):
        idx = math.ceil(k / _CDF_POINTS * m) - 1
        if idx < 0:
            continue
        idx = min(idx, m - 1)
        x = round(vals[idx], digits)
        y = round((idx + 1) / m, 4)
        if last_x is not None and x == last_x:
            points[-1][1] = y  # same x bucket -> keep the larger cumulative share
        else:
            points.append([x, y])
            last_x = x
    return {"name": name, "points": points}


def _box(name: str, values: list[float]) -> Optional[dict[str, Any]]:
    """Five-number summary for a boxplot group (None when the group is empty)."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {
        "name": name,
        "min": round(min(vals), 3),
        "q1": round(percentile(vals, 0.25), 3),
        "median": round(percentile(vals, 0.5), 3),
        "q3": round(percentile(vals, 0.75), 3),
        "max": round(max(vals), 3),
    }


def _providers(rows: list[RoundRow]) -> list[str]:
    return sorted({r["provider"] for r in rows if r["provider"]})


# tool_name -> coarse category for the by-category latency boxplot. Substring match (lowercase) so
# Claude (Read/Edit/Bash/Web*) and Codex (shell/apply_patch/…) both land. Order matters: more specific
# hints first. Unmatched tools fall to "Other" (kept, not silently dropped).
_CATEGORY_RULES = [
    ("Planning", ("todo", "plan")),
    ("Agent / task", ("agent", "task", "dispatch", "subagent")),
    ("Web / lookup", ("web", "fetch", "url", "browse", "search_web")),
    ("File edit / patch", ("edit", "write", "patch", "apply", "insert", "create", "notebook")),
    ("File read / search", ("read", "grep", "glob", "search", "list", "find", "cat", "view")),
    ("Shell / command", ("bash", "shell", "exec", "command", "run", "terminal")),
]
_CATEGORY_ORDER = ["Planning", "File read / search", "File edit / patch",
                   "Shell / command", "Web / lookup", "Agent / task", "Other"]


def _category(tool_name: str) -> str:
    low = (tool_name or "").lower()
    for label, hints in _CATEGORY_RULES:
        if any(h in low for h in hints):
            return label
    return "Other"


def _tool_latency_by_category(con) -> dict[str, Any]:
    by_cat: dict[str, list[float]] = {}
    for tool_name, lat in con.execute(
        f"SELECT tool_name, ({EFFECTIVE_TOOL_LATENCY_MS_SQL}) AS lat "
        "FROM tool_calls WHERE tool_internal_latency_ms IS NOT NULL "
        "OR tool_wall_latency_ms IS NOT NULL"
    ).fetchall():
        if lat is None or lat <= 0:
            continue
        by_cat.setdefault(_category(tool_name), []).append(float(lat))
    groups = []
    for label in _CATEGORY_ORDER:
        box = _box(label, by_cat.get(label, []))
        if box is not None:
            groups.append(box)
    return {"kind": "boxplot", "yLabel": "effective latency", "yLog": True, "yUnit": "ms", "groups": groups}


def _tool_call_counts(con) -> dict[str, Any]:
    items = [
        {"name": name, "value": int(n)}
        for name, n in con.execute(
            "SELECT tool_name, count(*) AS n FROM tool_calls "
            "GROUP BY tool_name ORDER BY n DESC LIMIT ?", [_TOP_TOOLS]
        ).fetchall()
        if name
    ]
    return {"kind": "barh", "xLabel": "calls", "items": items}


def _prefix_append_scatter(rows: list[RoundRow]) -> dict[str, Any]:
    # x = prefix (cached context depth), y = newly-appended tokens — matches the renderer's tooltip
    # (prefix value[0] · append value[1]) and the mock's axes.
    series = []
    for provider in _providers(rows):
        pts = [
            (r["prefix"], r["append"])
            for r in rows
            if r["provider"] == provider and r["append"] > 0 and r["prefix"] > 0
        ]
        step = max(1, math.ceil(len(pts) / _SCATTER_CAP))
        sampled = [[p, a] for p, a in pts[::step]]
        series.append({"name": provider, "points": sampled})
    return {
        "kind": "scatter", "xLabel": "prefix tokens / round", "yLabel": "new append tokens / round",
        "xLog": True, "yLog": True, "series": series,
    }


def _output_tokens_hist(rows: list[RoundRow]) -> dict[str, Any]:
    """Per-round output-token distribution as a frequency polygon (line histogram) per provider.

    All providers share ONE set of log-spaced bins (from the merged p1..p99) so the lines are directly
    comparable; each point is ``[bin_center, share_of_that_provider's_rounds]``. Mirrors the mock's
    ``output_tokens`` line histogram (was a boxplot here)."""
    all_out = [r["output"] for r in rows if r["output"] and r["output"] > 0]
    base = {"kind": "histogram", "xLabel": "output tokens / round", "yLabel": "share of rounds", "xLog": True}
    if not all_out:
        return {**base, "series": []}

    lo = max(1.0, percentile(all_out, 0.01))
    hi = percentile(all_out, 0.99)
    if hi <= lo:
        hi = lo * 10
    n = 14
    log_lo, log_hi = math.log(lo), math.log(hi)
    edges = [math.exp(log_lo + (log_hi - log_lo) * i / n) for i in range(n + 1)]
    centers = [math.sqrt(edges[i] * edges[i + 1]) for i in range(n)]

    def _bin(v: float) -> int:
        if v <= edges[0]:
            return 0  # clamp the tails into the end bins so every round is represented
        if v >= edges[-1]:
            return n - 1
        for i in range(n):
            if edges[i] <= v < edges[i + 1]:
                return i
        return n - 1

    series = []
    for provider in _providers(rows):
        outs = [r["output"] for r in rows if r["provider"] == provider and r["output"] and r["output"] > 0]
        if not outs:
            continue
        counts = [0] * n
        for v in outs:
            counts[_bin(float(v))] += 1
        total = len(outs)
        points = [[round(centers[i], 1), round(counts[i] / total, 4)] for i in range(n)]
        series.append({"name": provider, "points": points})
    return {**base, "series": series}


def _cache_hit_ratio_hist(rows: list[RoundRow]) -> dict[str, Any]:
    # edges mirror the mock's labels
    edges = [(0.0, 0.5, "<50%"), (0.5, 0.7, "50–70%"), (0.7, 0.85, "70–85%"),
             (0.85, 0.95, "85–95%"), (0.95, 1.0001, "95–100%")]
    counts = [0] * len(edges)
    for r in rows:
        denom = r["prefix"] + r["append"]
        if denom <= 0:
            continue
        ratio = r["prefix"] / denom
        for i, (lo, hi, _label) in enumerate(edges):
            if lo <= ratio < hi:
                counts[i] += 1
                break
    bins = [{"label": label, "count": counts[i]} for i, (_lo, _hi, label) in enumerate(edges)]
    return {"kind": "histogram", "xLabel": "prefix hit ratio", "yLabel": "rounds", "bins": bins}


def build_distributions(con, bundle, rows: list[RoundRow]) -> dict[str, Any]:
    providers = _providers(rows)

    tool_latency = {
        "kind": "cdf", "xLabel": "effective latency (ms)", "yLabel": "cumulative share", "xLog": True,
        "series": [
            _cdf_series(p, bundle.by_provider[p].tool_effective_latency_ms, positive_only=True)
            for p in providers if p in bundle.by_provider
        ],
    }
    generation_time = {
        "kind": "cdf", "xLabel": "generation time (s)", "yLabel": "cumulative share",
        "series": [
            _cdf_series(p, bundle.by_provider[p].observable_generation_time_seconds, positive_only=True)
            for p in providers if p in bundle.by_provider
        ],
    }
    human_wait = {
        "kind": "cdf", "xLabel": "human wait (s)", "yLabel": "cumulative share", "xLog": True,
        "series": [_cdf_series("all", bundle.merged.human_input_wait_seconds, positive_only=True)],
    }

    return {
        "tool_latency_distribution": tool_latency,
        "tool_latency_by_category": _tool_latency_by_category(con),
        "tool_call_counts": _tool_call_counts(con),
        "prefix_append_scatter": _prefix_append_scatter(rows),
        "output_tokens": _output_tokens_hist(rows),
        "generation_time_cdf": generation_time,
        "cache_hit_ratio": _cache_hit_ratio_hist(rows),
        "human_input_wait": human_wait,
    }
