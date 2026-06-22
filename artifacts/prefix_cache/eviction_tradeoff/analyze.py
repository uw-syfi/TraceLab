#!/usr/bin/env python3
"""Storage-vs-hit-rate trade-off of prefix-cache eviction.

If a serving engine evicts a session's prefix after an idle timeout ``tau``, two quantities move
together as ``tau`` grows:

  * **Achievable hit rate** rises --- fewer idle gaps exceed ``tau``, so fewer prefixes are evicted.
  * **KV storage** rises --- suspended (idle-but-retained) requests hold their KV for longer.

This experiment sweeps ``tau`` and reports both, so the trade-off (and its diminishing returns) is
explicit.

Idealised per-step model (matches the paper's binary rule). For each session step ``S`` paired with
its predecessor ``P``, with idle gap ``g`` before ``S`` (human think time for a user step, tool
latency for a tool-result step):

  * ``L``        = ``prefix_tokens + newly_append_tokens`` --- total prompt tokens at ``S``.
  * ``fresh``    = ``clip(max(0, L - L_prev) - output_tokens(P), 0, append)`` --- truly new
    user/tool tokens (context growth minus the prior step's output; see ``redundant_prefill`` /
    ``trace_facts``). ``fresh`` is the irreducible prefill: the cache can never serve it.
  * ``cacheable``= ``L - fresh`` --- reusable tokens a perfect, eviction-free cache would serve.

This is an **idealised** cache whose only imperfection is the eviction timeout: a step is a **full
hit** when ``g <= tau`` (it prefills only its ``fresh`` tokens; the cache serves the rest) and a
**full miss** when ``g > tau`` (it re-prefills its whole context ``L``). Two token-weighted
quantities over all covered steps:

  * ``hit_rate(tau) = sum_{g<=tau} cacheable / sum L``   (rises to ``1 - fresh/L`` = optimal).
  * **Prefill amplification** ``A(tau) = prefill(tau) / sum fresh`` --- how many times more tokens
    are prefilled than the irreducible ``fresh`` minimum. With ``prefill(tau) = sum fresh +
    sum_{g>tau} cacheable``, ``A`` **floors at 1x** as ``tau -> inf`` (a perfect, eviction-free cache
    prefills only fresh) and climbs as eviction forces re-prefills. The equivalent share form
    ``redundant_prefill_ratio(tau) = 1 - 1/A(tau)`` is also written out.

The **real deployed cache** is a reference, not the idealised model: it actually prefills ``append``
(amplification ``sum append / sum fresh`` ~ 5x) and serves ``prefix`` (hit ``sum prefix / sum L`` ~
96%). That operating point lands *on* the idealised curve at the **effective eviction time** --- the
``tau`` where the idealised hit rate equals the real one (~8 min merged) --- which is how this
section reconciles with the static ``redundant_prefill`` table.

Storage axis (capped, as the paper states: idle is "capped by the eviction time" --- before ``tau``
elapses you cannot know the gap will outlast it, so the KV is held to ``tau`` regardless):

  * ``R(tau) = sum_i min(g_i, tau) / sum_r gen_r``  --- suspended-KV / active-KV storage ratio,
    with ``gen_r`` the per-round input->last-output span (active decode time).
  * ``kv_active_ratio(tau) = 1 / (1 + R(tau))``.

Gaps reuse ``cache_hit_idle_relationship`` (user: first-activity minus the previous round's
last-activity; tool: the max leading tool-result duration); ``gen`` reuses
``kv_cache_active_ratio``'s span SQL.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import numpy as np  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402
from style import (  # noqa: E402
    AXIS_COLOR,
    TEXT_COLOR,
    mticker,
    plot_color,
    plt,
    polish_axes,
    provider_order,
    provider_title,
    save_plot,
)
from formatters import (  # noqa: E402
    format_duration_compact,
    format_duration_seconds_tick,
)

# Mirror kv_cache_active_ratio's event-type sets for the generation span.
_INPUT_EVENT_TYPES = ("user_message", "tool_result")
_MODEL_OUTPUT_EVENT_TYPES = ("reasoning", "text", "tool_call")

# Local timeout grid (the shared formatters cap at 1h; this section sweeps out to 4h).
EVICTION_MAX_SECONDS = 4 * 3600.0
EVICTION_LANDMARKS_SECONDS = [60, 300, 600, 1800, 3600, 7200, 14400]  # 1m, 5m, 10m, 30m, 1h, 2h, 4h
EVICTION_TICK_SECONDS = [1, 10, 60, 300, 1800, 7200]  # 1s, 10s, 1m, 5m, 30m, 2h (sparse, no overlap)


def eviction_thresholds() -> np.ndarray:
    """Fine log sweep 1s..4h with the landmarks pinned in (local extension of the shared grid)."""
    fine = np.logspace(0.0, np.log10(EVICTION_MAX_SECONDS), 260)
    points = {1.0, EVICTION_MAX_SECONDS, *map(float, EVICTION_LANDMARKS_SECONDS), *map(float, fine)}
    return np.asarray(sorted(points), dtype=float)


def _int_or_none(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------------------------------
# Per-step records: idle gap, total prompt tokens, fresh, cacheable.
# --------------------------------------------------------------------------------------------------
@dataclass
class _Round:
    round_index: int
    round_pk: int
    prefix_tokens: int | None
    append_tokens: int | None
    output_tokens: int
    first_event_type: str | None
    first_activity_us: int | None
    last_activity_us: int | None
    leading_tool_result_call_ids: list[str] = field(default_factory=list)
    emitted_tool_durations: dict[str, float | None] = field(default_factory=dict)


@dataclass
class StepArrays:
    """Per-step vectors for one (scope) group; ``prefix``/``append`` feed the prefill accounting."""

    gaps: list[float] = field(default_factory=list)
    cacheable: list[float] = field(default_factory=list)
    total: list[float] = field(default_factory=list)
    prefix: list[float] = field(default_factory=list)
    append_sum: float = 0.0

    def add(self, gap: float, cacheable: float, total: float, prefix: float, append: float) -> None:
        self.gaps.append(gap)
        self.cacheable.append(cacheable)
        self.total.append(total)
        self.prefix.append(prefix)
        self.append_sum += append


def _load_rounds_by_session(con) -> dict[tuple[str, str], list[_Round]]:
    """``{(provider, session_id): [_Round, ...]}`` ordered within a session.

    Reproduces ``cache_hit_idle_relationship``'s session walk (first/leading timing events, the
    activity span, per-call tool durations) and additionally carries ``output_tokens`` for the
    fresh computation. Ordered by ``(round_index, first_activity)`` as that experiment does.
    """
    timing_by_round: dict[int, list[tuple[str | None, int | None, str | None]]] = defaultdict(list)
    for round_pk, event_type, ts_us, tool_call_id in con.execute(
        "SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us, tool_call_id "
        "FROM timing_events ORDER BY round_pk, event_index"
    ).fetchall():
        timing_by_round[round_pk].append((event_type, ts_us, tool_call_id))

    tools_by_round: dict[int, list[tuple[str | None, int | None, int | None]]] = defaultdict(list)
    for round_pk, tool_call_id, emitted_us, result_us in con.execute(
        "SELECT round_pk, tool_call_id, "
        "       CAST(epoch_us(emitted_at) AS BIGINT) AS emitted_us, "
        "       CAST(epoch_us(result_at) AS BIGINT) AS result_us "
        "FROM tool_calls ORDER BY round_pk, tool_index"
    ).fetchall():
        tools_by_round[round_pk].append((tool_call_id, emitted_us, result_us))

    sessions: dict[tuple[str, str], list[_Round]] = defaultdict(list)
    for (
        round_pk,
        provider,
        session_id,
        round_index,
        prefix_tokens,
        append_tokens,
        output_tokens,
    ) in con.execute(
        "SELECT round_pk, provider, session_id, round_index, "
        "prefix_tokens, newly_append_tokens, output_tokens "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        if not isinstance(provider, str) or not isinstance(session_id, str):
            continue
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            continue

        events = timing_by_round.get(round_pk, [])
        tools = tools_by_round.get(round_pk, [])

        first_event_type = events[0][0] if events else None

        activity_us: list[int] = [ts for _e, ts, _c in events if ts is not None]
        for _cid, emitted_us, result_us in tools:
            if emitted_us is not None:
                activity_us.append(emitted_us)
            if result_us is not None:
                activity_us.append(result_us)

        first_activity_us: int | None = None
        for _e, ts, _c in events:
            if ts is not None:
                first_activity_us = ts
                break
        if first_activity_us is None:
            first_activity_us = min(activity_us) if activity_us else None
        last_activity_us = max(activity_us) if activity_us else None

        leading_call_ids: list[str] = []
        for etype, _ts, cid in events:
            if etype != "tool_result":
                break
            if isinstance(cid, str) and cid:
                leading_call_ids.append(cid)

        emitted_durations: dict[str, float | None] = {}
        for cid, emitted_us, result_us in tools:
            if not isinstance(cid, str) or not cid:
                continue
            if emitted_us is None or result_us is None:
                emitted_durations[cid] = None
                continue
            duration = (result_us - emitted_us) / 1e6
            emitted_durations[cid] = duration if duration >= 0 else None

        sessions[(provider, session_id)].append(
            _Round(
                round_index=round_index,
                round_pk=round_pk,
                prefix_tokens=prefix_tokens,
                append_tokens=append_tokens,
                output_tokens=_int_or_none(output_tokens) or 0,
                first_event_type=first_event_type if isinstance(first_event_type, str) else None,
                first_activity_us=first_activity_us,
                last_activity_us=last_activity_us,
                leading_tool_result_call_ids=leading_call_ids,
                emitted_tool_durations=emitted_durations,
            )
        )

    for key, rounds in sessions.items():
        rounds.sort(
            key=lambda r: (
                r.round_index,
                r.first_activity_us if r.first_activity_us is not None else float("-inf"),
            )
        )
    return dict(sessions)


def load_step_arrays(
    con,
) -> tuple[dict[str, StepArrays], dict[str, float]]:
    """Per-(scope) step arrays and the summed fresh tokens per scope.

    A step contributes when its first timing event is ``user_message``/``tool_result``, it has a
    positive total prompt length, a predecessor (for fresh) and a measurable idle gap. Each step
    feeds both its provider scope and the ``merged`` scope.
    """
    arrays: dict[str, StepArrays] = defaultdict(StepArrays)
    fresh_sum: dict[str, float] = defaultdict(float)

    for (provider, _session_id), rounds in _load_rounds_by_session(con).items():
        durations_by_call_id: dict[str, float] = {}
        prev: _Round | None = None
        for current in rounds:
            event_type = current.first_event_type
            prefix = current.prefix_tokens
            append = current.append_tokens
            usable = (
                event_type in ("user_message", "tool_result")
                and prefix is not None
                and append is not None
                and (prefix + append) > 0
            )

            gap_seconds: float | None = None
            if usable:
                if event_type == "user_message":
                    if prev is not None and (
                        prev.last_activity_us is not None
                        and current.first_activity_us is not None
                    ):
                        delta = (current.first_activity_us - prev.last_activity_us) / 1e6
                        if delta >= 0:
                            gap_seconds = delta
                else:  # tool_result
                    durations = [
                        d
                        for cid in current.leading_tool_result_call_ids
                        if (d := durations_by_call_id.get(cid)) is not None
                    ]
                    if durations:
                        gap_seconds = max(durations)

            if usable and prev is not None and gap_seconds is not None:
                total = float(prefix + append)
                prev_total = float((prev.prefix_tokens or 0) + (prev.append_tokens or 0))
                context_growth = max(0.0, total - prev_total)
                fresh = context_growth - float(prev.output_tokens)
                fresh = min(max(fresh, 0.0), float(append))  # fresh is a subset of the appended tokens
                cacheable = total - fresh
                for scope in ("merged", provider):
                    arrays[scope].add(gap_seconds, cacheable, total, float(prefix), float(append))
                    fresh_sum[scope] += fresh

            durations_by_call_id.update(current.emitted_tool_durations)
            prev = current
    return dict(arrays), dict(fresh_sum)


def load_generation_total_seconds(con) -> dict[str, float]:
    """Total per-round input->last-output span (seconds), by scope (merged + each provider).

    Identical span definition to ``kv_cache_active_ratio.load_llm_generation_seconds_by_provider``.
    """
    input_in = " OR ".join(f"event_type = '{t}'" for t in _INPUT_EVENT_TYPES)
    output_in = " OR ".join(f"event_type = '{t}'" for t in _MODEL_OUTPUT_EVENT_TYPES)
    rows = con.execute(
        f"""
        WITH ev AS (
            SELECT round_pk, event_type, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us
            FROM timing_events
            WHERE timestamp IS NOT NULL AND (({input_in}) OR ({output_in}))
        ),
        bounds AS (
            SELECT round_pk,
                   min(CASE WHEN ({output_in}) THEN ts_us END) AS first_output_us,
                   max(CASE WHEN ({output_in}) THEN ts_us END) AS last_output_us
            FROM ev GROUP BY round_pk
        ),
        agg AS (
            SELECT b.round_pk, b.last_output_us,
                   max(CASE WHEN ({input_in}) AND ev.ts_us <= b.first_output_us
                            THEN ev.ts_us END) AS last_input_us
            FROM bounds b JOIN ev USING (round_pk)
            WHERE b.first_output_us IS NOT NULL
            GROUP BY b.round_pk, b.first_output_us, b.last_output_us
        )
        SELECT COALESCE(r.provider, '<unknown-provider>') AS provider,
               (a.last_output_us - a.last_input_us) / 1e6 AS span_seconds
        FROM agg a JOIN rounds r USING (round_pk)
        WHERE a.last_input_us IS NOT NULL AND (a.last_output_us - a.last_input_us) > 0
        """
    ).fetchall()

    totals: dict[str, float] = defaultdict(float)
    for provider, span_seconds in rows:
        totals[provider] += float(span_seconds)
        totals["merged"] += float(span_seconds)
    return dict(totals)


# --------------------------------------------------------------------------------------------------
# Sweep.
# --------------------------------------------------------------------------------------------------
@dataclass
class SweepResult:
    thresholds: np.ndarray
    hit_rate: np.ndarray
    prefill_amplification: np.ndarray  # idealised: total prefill / necessary (fresh); floor 1x
    redundant_prefill: np.ndarray  # redundant tokens as a share of prefill; floor 0
    storage_ratio: np.ndarray  # R = suspended/active KV
    kv_active_ratio: np.ndarray
    fresh_floor: float
    optimal_hit_rate: float  # tau -> inf ceiling: cacheable / L
    real_hit_rate: float  # observed deployed cache: prefix / L
    observed_amplification: float  # observed deployed cache: append / fresh
    effective_eviction_seconds: float  # tau where idealised hit == real hit
    total_tokens: float
    generation_seconds: float
    steps: int


def sweep_scope(
    arr: StepArrays,
    fresh_tokens: float,
    generation_seconds: float,
    thresholds: np.ndarray,
) -> SweepResult | None:
    if not arr.gaps or generation_seconds <= 0 or fresh_tokens <= 0:
        return None
    gaps = np.asarray(arr.gaps, dtype=float)
    cacheable = np.asarray(arr.cacheable, dtype=float)
    prefix = np.asarray(arr.prefix, dtype=float)
    total_tokens = float(np.sum(arr.total))
    append_total = float(arr.append_sum)
    if total_tokens <= 0:
        return None

    order = np.argsort(gaps)
    gaps_sorted = gaps[order]
    cacheable_cumsum = np.concatenate(([0.0], np.cumsum(cacheable[order])))
    prefix_cumsum = np.concatenate(([0.0], np.cumsum(prefix[order])))
    gaps_cumsum = np.concatenate(([0.0], np.cumsum(gaps_sorted)))
    n = gaps_sorted.size
    prefix_total = prefix_cumsum[-1]

    # retained = steps with gap <= tau (searchsorted 'right' counts <= tau).
    retained = np.searchsorted(gaps_sorted, thresholds, side="right")
    hit_tokens = cacheable_cumsum[retained]
    cacheable_total = cacheable_cumsum[-1]
    hit_rate = hit_tokens / total_tokens

    # Idealised prefill at tau (perfect cache limited only by eviction): a retained step prefills
    # only its fresh tokens; an evicted step (gap > tau) re-prefills its whole context L. So the
    # eviction-induced redundant re-prefill is the evicted steps' cacheable tokens, and
    #   prefill(tau) = sum fresh + sum_{g>tau} cacheable.
    redundant_tokens = cacheable_total - hit_tokens  # sum_{g>tau} cacheable
    prefill_tokens = fresh_tokens + redundant_tokens
    prefill_amplification = prefill_tokens / fresh_tokens  # = 1 + redundant/fresh; floors at 1x
    redundant_prefill = redundant_tokens / prefill_tokens  # share of prefill; floors at 0

    # Capped suspended-KV seconds: sum min(gap, tau) = (sum of gaps <= tau) + tau * (count > tau).
    suspended = gaps_cumsum[retained] + thresholds * (n - retained)
    storage_ratio = suspended / generation_seconds
    kv_active_ratio = 1.0 / (1.0 + storage_ratio)

    # Real deployed cache operating point: it prefills `append` and caches `prefix`. This point
    # lands on the idealised curve at the system's effective eviction time (where hit rates match).
    real_hit_rate = prefix_total / total_tokens
    eff_idx = int(np.argmin(np.abs(hit_rate - real_hit_rate)))

    return SweepResult(
        thresholds=thresholds,
        hit_rate=hit_rate,
        prefill_amplification=prefill_amplification,
        redundant_prefill=redundant_prefill,
        storage_ratio=storage_ratio,
        kv_active_ratio=kv_active_ratio,
        fresh_floor=fresh_tokens / total_tokens,
        optimal_hit_rate=cacheable_total / total_tokens,
        real_hit_rate=real_hit_rate,
        observed_amplification=append_total / fresh_tokens,
        effective_eviction_seconds=float(thresholds[eff_idx]),
        total_tokens=total_tokens,
        generation_seconds=generation_seconds,
        steps=n,
    )


# --------------------------------------------------------------------------------------------------
# Outputs.
# --------------------------------------------------------------------------------------------------
def write_csv(path: Path, results: dict[str, SweepResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scope",
        "cache_eviction_timeout_seconds",
        "cache_eviction_timeout_label",
        "landmark_timeout",
        "achievable_hit_rate",
        "prefill_amplification",
        "redundant_prefill_ratio",
        "fresh_floor",
        "optimal_hit_rate",
        "real_hit_rate",
        "observed_prefill_amplification",
        "effective_eviction_seconds",
        "storage_ratio_suspended_over_active",
        "kv_active_ratio",
    ]
    landmark_set = {float(v) for v in EVICTION_LANDMARKS_SECONDS}
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for scope in ("merged", "claude", "codex"):
            result = results.get(scope)
            if result is None:
                continue
            for i, tau in enumerate(result.thresholds):
                writer.writerow(
                    {
                        "scope": scope,
                        "cache_eviction_timeout_seconds": float(tau),
                        "cache_eviction_timeout_label": format_duration_compact(float(tau)),
                        "landmark_timeout": float(tau) in landmark_set,
                        "achievable_hit_rate": float(result.hit_rate[i]),
                        "prefill_amplification": float(result.prefill_amplification[i]),
                        "redundant_prefill_ratio": float(result.redundant_prefill[i]),
                        "fresh_floor": result.fresh_floor,
                        "optimal_hit_rate": result.optimal_hit_rate,
                        "real_hit_rate": result.real_hit_rate,
                        "observed_prefill_amplification": result.observed_amplification,
                        "effective_eviction_seconds": result.effective_eviction_seconds,
                        "storage_ratio_suspended_over_active": float(result.storage_ratio[i]),
                        "kv_active_ratio": float(result.kv_active_ratio[i]),
                    }
                )


def _landmark_indices(thresholds: np.ndarray) -> list[tuple[int, float]]:
    out: list[tuple[int, float]] = []
    for tau in EVICTION_LANDMARKS_SECONDS:
        idx = int(np.searchsorted(thresholds, tau, side="left"))
        if idx < thresholds.size and thresholds[idx] == tau:
            out.append((idx, float(tau)))
    return out


def plot_tradeoff_by_timeout(results: dict[str, SweepResult], output_dir: Path) -> None:
    """Three stacked panels sharing the eviction-timeout x-axis: hit rate, storage R, redundant."""
    scopes = [s for s in provider_order(["claude", "codex"]) if s in results]
    if not scopes:
        return
    thresholds = results[scopes[0]].thresholds

    # Single-column figure: keep the physical width small so the on-page downscale is mild and
    # the (deliberately large) fonts stay legible.
    LABEL_FS, TICK_FS, LEGEND_FS = 14.0, 12.0, 12.0
    fig, (ax_hit, ax_store, ax_amp) = plt.subplots(
        3, 1, sharex=True, figsize=(6.2, 6.0)
    )

    def _draw(ax, getter, *, refs=()) -> None:
        for index, scope in enumerate(scopes):
            result = results[scope]
            color = plot_color(scope, index)
            ax.plot(
                result.thresholds, getter(result), color=color, linewidth=2.3,
                label=provider_title(scope),
            )
            for ref in refs:
                ax.axhline(ref(result), color=color, linestyle=(0, (5, 2)), linewidth=2.0, alpha=0.95)
        for tau in EVICTION_LANDMARKS_SECONDS:
            ax.axvline(
                tau, color=AXIS_COLOR, linestyle=(0, (4, 3)), linewidth=0.9, alpha=0.55, zorder=0
            )
        polish_axes(ax, grid_axis="both")

    _draw(ax_hit, lambda r: r.hit_rate)
    _draw(ax_store, lambda r: r.storage_ratio)
    _draw(ax_amp, lambda r: r.prefill_amplification)

    ax_hit.set_ylabel("Achievable\nhit rate", fontsize=LABEL_FS)
    ax_hit.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax_hit.yaxis.set_major_locator(mticker.MultipleLocator(0.1))
    ax_hit.legend(fontsize=LEGEND_FS, loc="lower right", framealpha=0.92)

    ax_store.set_ylabel("Storage ratio $R$\n(susp./active KV)", fontsize=LABEL_FS)
    ax_store.set_ylim(bottom=0)
    ax_store.yaxis.set_major_locator(mticker.MultipleLocator(1.0))

    ax_amp.set_ylabel("Prefill amp.\n(total / fresh)", fontsize=LABEL_FS)
    ax_amp.set_yscale("log")
    ax_amp.axhline(1.0, color=AXIS_COLOR, linewidth=1.0, alpha=0.7)  # eviction-free ideal = 1x
    # Denser labelled ticks on the log amplification axis (1x..100x).
    ax_amp.yaxis.set_major_locator(
        mticker.FixedLocator([1, 2, 5, 10, 20, 50, 100])
    )
    ax_amp.yaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _p: f"{v:g}$\\times$"))
    ax_amp.yaxis.set_minor_locator(mticker.NullLocator())
    ax_amp.yaxis.set_minor_formatter(mticker.NullFormatter())

    ax_amp.set_xlabel("Cache eviction timeout", fontsize=LABEL_FS)
    ax_amp.set_xscale("log")
    ax_amp.set_xlim(thresholds[0], thresholds[-1])
    ax_amp.set_xticks(EVICTION_TICK_SECONDS)
    ax_amp.xaxis.set_major_formatter(mticker.FuncFormatter(format_duration_seconds_tick))
    ax_amp.xaxis.set_minor_formatter(mticker.NullFormatter())

    # Enlarge tick labels on every panel (after polish_axes, so it is not overridden).
    for ax in (ax_hit, ax_store, ax_amp):
        ax.tick_params(axis="both", which="major", labelsize=TICK_FS)

    fig.align_ylabels((ax_hit, ax_store, ax_amp))
    fig.tight_layout()
    fig.savefig(output_dir / "eviction_tradeoff_by_timeout.pdf", bbox_inches="tight")
    save_plot(fig, output_dir / "eviction_tradeoff_by_timeout.png")


def plot_pareto(results: dict[str, SweepResult], output_dir: Path) -> None:
    """Pareto view: achievable hit rate vs. storage ratio R, with eviction-time landmarks."""
    scopes = [s for s in provider_order(["claude", "codex"]) if s in results]
    if not scopes:
        return
    fig, ax = plt.subplots(figsize=(8.4, 5.6))
    ax.set_xlabel("Suspended/active KV storage ratio $R$", fontsize=16)
    ax.set_ylabel("Achievable prefix-cache hit rate", fontsize=16)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0))
    ax.yaxis.set_major_locator(mticker.MultipleLocator(0.1))
    ax.tick_params(axis="both", labelsize=14)
    polish_axes(ax, grid_axis="both")

    landmark_points: dict[float, list[tuple[float, float]]] = defaultdict(list)
    for index, scope in enumerate(scopes):
        result = results[scope]
        color = plot_color(scope, index)
        ax.plot(
            result.storage_ratio,
            result.hit_rate,
            color=color,
            linewidth=2.6,
            label=provider_title(scope),
        )
        for idx, tau in _landmark_indices(result.thresholds):
            ax.scatter(
                [result.storage_ratio[idx]],
                [result.hit_rate[idx]],
                color=color,
                s=34,
                zorder=5,
            )
            landmark_points[tau].append(
                (float(result.storage_ratio[idx]), float(result.hit_rate[idx]))
            )

    # One label per landmark, at the providers' midpoint, placed below the markers in the
    # open region under the curve so the (near-coincident) per-provider points don't double up.
    for tau, points in landmark_points.items():
        mx = sum(p[0] for p in points) / len(points)
        my = min(p[1] for p in points)
        ax.annotate(
            format_duration_compact(tau),
            (mx, my),
            textcoords="offset points",
            xytext=(0, -15),
            ha="center",
            va="top",
            fontsize=12,
            color=TEXT_COLOR,
        )
    ax.margins(x=0.04)
    ax.legend(fontsize=14, loc="lower right")
    fig.tight_layout()
    fig.savefig(output_dir / "eviction_tradeoff_pareto.pdf", bbox_inches="tight")
    save_plot(fig, output_dir / "eviction_tradeoff_pareto.png")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    output_dir = args.output_dir or EXP_DIR

    arrays, fresh_sum = load_step_arrays(con)
    generation = load_generation_total_seconds(con)
    thresholds = eviction_thresholds()

    results: dict[str, SweepResult] = {}
    for scope in ("merged", "claude", "codex"):
        result = sweep_scope(
            arrays.get(scope, StepArrays()),
            fresh_sum.get(scope, 0.0),
            generation.get(scope, 0.0),
            thresholds,
        )
        if result is not None:
            results[scope] = result

    csv_path = output_dir / "eviction_tradeoff_by_scope.csv"
    write_csv(csv_path, results)
    print(f"summary_csv={csv_path}")

    for scope, result in results.items():
        print(
            f"{scope}: steps={result.steps} optimal_hit={result.optimal_hit_rate:.4f} "
            f"fresh_floor={result.fresh_floor:.4f} "
            f"gen_hours={result.generation_seconds / 3600:.1f}"
        )

    if not args.no_plots:
        plot_tradeoff_by_timeout(results, output_dir)
        plot_pareto(results, output_dir)
        png_sidecar.make_self_contained(
            output_dir,
            code_files=[Path(__file__), *png_sidecar.util_code_files()],
            readme_path=EXP_DIR / "README.md",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
