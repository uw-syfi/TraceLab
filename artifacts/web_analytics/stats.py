"""The full ``Stats`` superset — every aggregate the /lab stats panel can show.

Most fields come straight from the proven ``overview_summary`` aggregator (``bundle.merged.as_dict()``)
so they reconcile with the existing summary numbers exactly: token splits, per-trigger input,
context-growth shares, generation timing, human-wait, tool latency. The rest are computed here from
the shared per-round rows / cost / session list:

  * percentiles the summary dict doesn't pre-compute (p99 request, per-round hit-rate p50/p90) —
    via the summary's raw lists + ``percentile``;
  * per-session agentic shape (autonomy depth, model switches, human interjections);
  * per-day / per-week relatable rates and $-efficiency (from cost + local-day buckets).

Mirrors ``buildStats`` in lib/mock/analytics.ts field-for-field. Ratios/shares are 0–1, times in
seconds, ``*PerStep`` is per LLM round. Everything is a plain ``float``/``str`` — the UI formats.
"""

from __future__ import annotations

import statistics
from collections import Counter
from typing import Any, Optional

from _overview import percentile
from daily import _local
from rounds import RoundRow


def _div(a: float, b: float) -> float:
    return a / b if b else 0.0


def _f(x: Optional[float]) -> float:
    """Coerce a possibly-None summary value to a finite float (contract wants numbers, not null)."""
    return float(x) if x is not None else 0.0


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


# Best-effort read/write classification for the read:write ratio. Names are provider-specific; match
# on lowercase substrings so Claude (Read/Edit/Write/Grep/Glob) and Codex (shell/apply_patch/…) both
# land sensibly. Anything unmatched (Bash, Task, …) is neither and ignored in the ratio.
_READ_HINTS = ("read", "grep", "glob", "search", "fetch", "list", "cat", "view")
_WRITE_HINTS = ("edit", "write", "patch", "apply", "create", "insert")


def _classify_tool(name: str) -> Optional[str]:
    low = name.lower()
    if any(h in low for h in _WRITE_HINTS):
        return "write"
    if any(h in low for h in _READ_HINTS):
        return "read"
    return None


def _tool_aggregates(con) -> dict[str, Any]:
    """One scan of ``tool_calls`` grouped by name -> error rate, avg result chars, top tool, r/w."""
    total = 0
    errors = 0
    result_chars = 0
    reads = 0
    writes = 0
    by_name: Counter[str] = Counter()
    for name, n, err_n, rc_sum in con.execute(
        "SELECT tool_name, count(*) AS n, "
        "count(*) FILTER (WHERE is_error) AS err_n, "
        "COALESCE(sum(result_chars), 0) AS rc_sum "
        "FROM tool_calls GROUP BY tool_name"
    ).fetchall():
        n = int(n)
        total += n
        errors += int(err_n)
        result_chars += int(rc_sum)
        label = _classify_tool(name or "")
        if label == "read":
            reads += n
        elif label == "write":
            writes += n
        if name:
            by_name[name] += n

    top_name, top_count = (by_name.most_common(1)[0] if by_name else ("", 0))
    return {
        "toolErrorRate": _div(errors, total),
        "avgToolResultChars": _div(result_chars, total),
        "readWriteRatio": _div(reads, writes) if writes else float(reads),
        "topToolName": top_name,
        "topToolShare": _div(top_count, total),
    }


def build_stats(
    con,
    bundle,
    rows: list[RoundRow],
    cost: dict[str, Any],
    sessions: list[dict[str, Any]],
    per_day: list[dict[str, Any]],
    tz_offset_min: int,
) -> dict[str, Any]:
    merged = bundle.merged
    d = merged.as_dict()
    tin = d["tokens"]["input"]
    tout = d["tokens"]["output"]
    gen = d["generation_timing"]
    tools_d = d["tools"]
    ug = tin["total_input_growth_when_started_with_user_message"]
    tg = tin["total_input_growth_when_started_with_tool_result"]

    rounds = merged.rounds
    total_input = tin["total_input_tokens"]
    total_output = tout["total_output_tokens_including_reasoning"]
    total_reasoning = tout["reasoning_output_tokens_subset"]
    total_cost = sum(m["costUsd"] for m in cost["byModel"])
    total_tool_calls = tools_d["total_tool_calls"]

    # --- per-round hit-rate percentiles + decay (OLS slope over round order) ---
    hit_ratios: list[float] = []
    xs: list[float] = []
    for i, r in enumerate(rows):
        denom = r["prefix"] + r["append"]
        if denom > 0:
            ratio = r["prefix"] / denom
            hit_ratios.append(ratio)
            xs.append(float(i))
    hit_decay_per_100 = 0.0
    if len(xs) >= 2:
        mx = _mean(xs)
        my = _mean(hit_ratios)
        var = sum((x - mx) ** 2 for x in xs)
        if var > 0:
            cov = sum((x - mx) * (y - my) for x, y in zip(xs, hit_ratios))
            hit_decay_per_100 = (cov / var) * 100.0

    # --- per-session agentic shape (rounds grouped by session, in round order) ---
    sess_rounds: dict[str, list[RoundRow]] = {}
    for r in rows:
        sid = r["session_id"]
        if sid:
            sess_rounds.setdefault(sid, []).append(r)
    autonomy_runs: list[float] = []
    interjections: list[float] = []
    models_per_session: list[float] = []
    model_switches: list[float] = []
    for rs in sess_rounds.values():
        rs = sorted(rs, key=lambda x: x["round_index"])
        interjections.append(sum(1 for r in rs if r["is_user_input"]))
        seen_models = [r["model"] for r in rs if r["model"]]
        models_per_session.append(len(set(seen_models)))
        switches = sum(
            1 for a, b in zip(seen_models, seen_models[1:]) if a != b
        )
        model_switches.append(switches)
        run = 0
        for r in rs:
            if r["is_user_input"]:
                if run:
                    autonomy_runs.append(run)
                run = 0
            else:
                run += 1
        if run:
            autonomy_runs.append(run)

    # --- local-time day / hour / week buckets (for relatable rates) ---
    day_set: set[str] = set()
    dayhour_set: set[tuple[str, int]] = set()
    week_days: dict[tuple[int, int], set[str]] = {}
    for r in rows:
        ts = r["first_ts_us"]
        if ts is None:
            continue
        lt = _local(ts, tz_offset_min)
        day = lt.strftime("%Y-%m-%d")
        day_set.add(day)
        dayhour_set.add((day, lt.hour))
        iso = lt.isocalendar()
        week_days.setdefault((iso[0], iso[1]), set()).add(day)
    active_days = len(day_set)
    total_active_hours = len(dayhour_set)
    avg_active_days_per_week = _mean([len(v) for v in week_days.values()])

    # --- provider / model mix ---
    prov_counts: Counter[str] = Counter(r["provider"] for r in rows if r["provider"])
    by_model = d["rounds_by_model"]  # already most-common ordered
    top_model_name = next(iter(by_model), "")
    top_model_count = by_model.get(top_model_name, 0)
    opus_rounds = sum(n for m, n in by_model.items() if "opus" in m.lower())
    claude_cost = sum(m["costUsd"] for m in cost["byModel"] if m["provider"] == "claude")
    codex_cost = sum(m["costUsd"] for m in cost["byModel"] if m["provider"] == "codex")

    tool_agg = _tool_aggregates(con)

    # time split across generation / tool / waiting
    total_gen_s = gen["total_observable_generation_time_seconds"]
    total_tool_s = tools_d["effective_latency"]["total_seconds"]
    total_wait_s = gen["total_waiting_for_human_input_seconds"]
    time_denom = total_gen_s + total_tool_s + total_wait_s
    all_tokens = total_input + total_output
    cache_savings_usd = cost["cacheSavingsUsd"]

    return {
        # 2a. tokens per step
        "avgInputTokens": _f(tin["average_total_input_tokens_per_round"]),
        "avgCachedInputTokens": _f(tin["average_cached_read_input_tokens_per_round"]),
        "avgUncachedInputTokens": _f(tin["average_new_input_tokens_per_round"]),
        "avgOutputTokens": _f(tout["average_output_tokens_including_reasoning_per_round"]),
        "avgReasoningTokens": _div(total_reasoning, tout["rounds_with_positive_reasoning_output_tokens"]),
        "inputOutputRatio": _div(total_input, total_output),
        "freshTokenShare": _div(tin["new_input_tokens"], total_input),
        "reasoningShareOfOutput": _div(total_reasoning, total_output),

        # 2b. input by step trigger
        "userAvgTotalInput": _f(tin["average_total_input_tokens_when_started_with_user_message"]),
        "userAvgAppendInput": _f(tin["average_new_input_tokens_when_started_with_user_message"]),
        "toolAvgTotalInput": _f(tin["average_total_input_tokens_when_started_with_tool_result"]),
        "toolAvgAppendInput": _f(tin["average_new_input_tokens_when_started_with_tool_result"]),

        # 2c. context growth
        "totalContextIncrease": tin["total_context_increase_tokens"],
        "userContextDeltaAvg": _f(tin["average_user_context_delta_tokens"]),
        "userContextDeltaP50": _f(tin["median_user_context_delta_tokens"]),
        "userContextDeltaP90": _f(tin["p90_user_context_delta_tokens"]),
        "toolContextDeltaAvg": _f(tin["average_tool_result_context_delta_tokens"]),
        "toolContextDeltaP50": _f(tin["median_tool_result_context_delta_tokens"]),
        "toolContextDeltaP90": _f(tin["p90_tool_result_context_delta_tokens"]),
        "contextIncreaseToAppendRatio": _div(tin["total_context_increase_tokens"], tin["new_input_tokens"]),
        "userContextIncreaseToAppendRatio": _div(
            ug["total_context_increase_tokens"], tin["total_new_input_tokens_when_started_with_user_message"]),
        "toolContextIncreaseToAppendRatio": _div(
            tg["total_context_increase_tokens"], tin["total_new_input_tokens_when_started_with_tool_result"]),
        "userGrowthShare": _f(ug["positive_growth_share"]),
        "toolGrowthShare": _f(tg["positive_growth_share"]),
        "userReductionShare": _f(ug["negative_growth_share"]),
        "toolReductionShare": _f(tg["negative_growth_share"]),
        "userMajorCompactShare": _f(ug["major_compact_share"]),
        "toolMajorCompactShare": _f(tg["major_compact_share"]),

        # 2d. cache efficiency
        "prefixHitRate": _f(tin["prefix_hit_rate"]),
        "hitRateP50": _f(percentile(hit_ratios, 0.5)),
        "hitRateP90": _f(percentile(hit_ratios, 0.9)),
        "userHitRate": _f(tin["prefix_hit_rate_when_started_with_user_message"]),
        "toolHitRate": _f(tin["prefix_hit_rate_when_started_with_tool_result"]),
        "hitRateDecayPer100": hit_decay_per_100,

        # 2e. request timing (per-round wall = observable generation time)
        "avgRequestS": _div(total_gen_s, gen["rounds_with_observable_generation_time"]),
        "p50RequestS": _f(percentile(merged.observable_generation_time_seconds, 0.5)),
        "p90RequestS": _f(percentile(merged.observable_generation_time_seconds, 0.9)),
        "p99RequestS": _f(percentile(merged.observable_generation_time_seconds, 0.99)),
        "genTimeP50S": _f(gen["p50_observable_generation_time_seconds"]),
        "genTimeP90S": _f(gen["p90_observable_generation_time_seconds"]),
        "totalGenerationS": total_gen_s,
        "avgDecodeTps": _f(gen["average_normalized_decoding_speed_tokens_per_second"]),
        "postReasoningDecodeTps": _f(
            gen["post_reasoning_tpot_estimate"]["average_decode_speed_tokens_per_second"]),
        "estTtftS": _f(gen["estimated_ttft_from_exact_reasoning_tokens"]["estimated_average_seconds"]),

        # 2f. tools
        "avgToolCallsPerRound": _div(total_tool_calls, rounds),
        "toolCallsPerRequest": _f(tools_d["tool_calls_per_visible_user_message_round"]),
        "stepsWithToolsShare": _div(tools_d["rounds_with_tool_calls"], rounds),
        "toolLatencyP50Ms": _f(tools_d["effective_latency"]["p50_seconds"]) * 1000.0,
        "toolLatencyP90Ms": _f(tools_d["effective_latency"]["p90_seconds"]) * 1000.0,
        "totalToolTimeS": total_tool_s,
        "toolErrorRate": tool_agg["toolErrorRate"],
        "readWriteRatio": tool_agg["readWriteRatio"],
        "avgToolResultChars": tool_agg["avgToolResultChars"],
        "topToolName": tool_agg["topToolName"],
        "topToolShare": tool_agg["topToolShare"],

        # 2g. human-in-the-loop
        "totalHumanWaitS": total_wait_s,
        "humanWaitAvgS": _f(gen["average_waiting_for_human_input_seconds"]),
        "humanWaitP50S": _f(gen["median_waiting_for_human_input_seconds"]),
        "humanWaitP90S": _f(gen["p90_waiting_for_human_input_seconds"]),
        "humanInLoopShare": _div(d["scope"]["rounds_with_visible_user_message"], rounds),
        "timeSplitGenerationShare": _div(total_gen_s, time_denom),
        "timeSplitToolShare": _div(total_tool_s, time_denom),
        "timeSplitWaitingShare": _div(total_wait_s, time_denom),

        # 2h. per session
        "avgRoundsPerSession": _mean([s["rounds"] for s in sessions]),
        "avgToolCallsPerSession": _mean([s["toolCalls"] for s in sessions]),
        "avgSessionDurationS": _mean([s["durationS"] for s in sessions]),
        "avgSessionCostUsd": _mean([s["costUsd"] for s in sessions]),
        "autonomyDepthAvg": _mean(autonomy_runs),
        "autonomyDepthP90": _f(percentile(autonomy_runs, 0.9)),
        "avgHumanInterjections": _mean(interjections),
        "avgModelsPerSession": _mean(models_per_session),
        "avgModelSwitches": _mean(model_switches),
        "typicalSessionSteps": _median([s["rounds"] for s in sessions]),
        "typicalSessionMinutes": _div(_median([s["durationS"] for s in sessions]), 60.0),
        "typicalSessionCostUsd": _median([s["costUsd"] for s in sessions]),

        # 2i. per day / relatable rates
        "avgStepsPerDay": _mean([p["rounds"] for p in per_day]),
        "avgSessionsPerDay": _div(len(sessions), active_days),
        "avgActiveHoursPerDay": _div(total_active_hours, active_days),
        "avgCostPerDay": _mean([p["costUsd"] for p in per_day]),
        "avgStepsPerWeek": _mean([p["rounds"] for p in per_day]) * avg_active_days_per_week,
        "avgSessionsPerWeek": _div(len(sessions), active_days) * avg_active_days_per_week,
        "avgActiveDaysPerWeek": avg_active_days_per_week,
        "avgCostPerWeek": _mean([p["costUsd"] for p in per_day]) * avg_active_days_per_week,
        "costPerHourUsd": _div(total_cost, total_active_hours),
        "tokensPerUsd": _div(all_tokens, total_cost),
        "stepsPerUsd": _div(rounds, total_cost),
        "toolCallsPerUsd": _div(total_tool_calls, total_cost),
        "blendedUsdPerMtok": _div(total_cost, all_tokens / 1_000_000) if all_tokens else 0.0,
        "cacheSavedPct": _div(cache_savings_usd, total_cost + cache_savings_usd),
        "reasoningTaxPerStepUsd": _div(cost["reasoningCostUsd"], rounds),

        # 2j. models & providers
        "claudeStepShare": _div(prov_counts.get("claude", 0), rounds),
        "codexStepShare": _div(prov_counts.get("codex", 0), rounds),
        "claudeCostShare": _div(claude_cost, total_cost),
        "codexCostShare": _div(codex_cost, total_cost),
        "modelsRepresented": len(by_model),
        "topModelName": top_model_name,
        "topModelStepShare": _div(top_model_count, rounds),
        "opusStepShare": _div(opus_rounds, rounds),
    }
