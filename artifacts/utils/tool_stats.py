"""Provider/tool aggregation, rare-tool collapsing, and plot-ready tool stats."""

from __future__ import annotations

from typing import Any
import argparse

from style import provider_order
from accumulators import ToolStats


def aggregate_tool_stats(
    label: str,
    items: list[tuple[str, ToolStats]],
    *,
    sample_size: int,
    seed: int,
) -> tuple[str, ToolStats] | None:
    if not items:
        return None

    aggregate = ToolStats(sample_size, seed)
    for _name, stats in items:
        aggregate.calls += stats.calls
        aggregate.latency_count += stats.latency_count
        aggregate.missing_latency += stats.missing_latency
        aggregate.nonpositive_latency += stats.nonpositive_latency
        aggregate.error_calls += stats.error_calls
        aggregate.latency_sum += stats.latency_sum
        aggregate.providers.update(stats.providers)
        if stats.latency_min is not None:
            aggregate.latency_min = (
                stats.latency_min
                if aggregate.latency_min is None
                else min(aggregate.latency_min, stats.latency_min)
            )
        if stats.latency_max is not None:
            aggregate.latency_max = (
                stats.latency_max
                if aggregate.latency_max is None
                else max(aggregate.latency_max, stats.latency_max)
            )
        for value in stats.sampler.values:
            aggregate.sampler.add(value)

    aggregate.sampler.seen = max(aggregate.sampler.seen, aggregate.latency_count)
    return label, aggregate


def collapse_rare_tool_stats(
    tool_stats: dict[str, ToolStats],
    *,
    min_calls: int,
    sample_size: int,
    seed: int,
) -> dict[str, ToolStats]:
    if min_calls <= 1:
        return dict(tool_stats)

    kept: dict[str, ToolStats] = {}
    rare: list[tuple[str, ToolStats]] = []
    for name, stats in tool_stats.items():
        if stats.calls < min_calls:
            rare.append((name, stats))
        else:
            kept[name] = stats

    other = aggregate_tool_stats(
        f"Other (<{min_calls} calls/tool)",
        rare,
        sample_size=sample_size,
        seed=seed,
    )
    if other is not None:
        name, stats = other
        kept[name] = stats
    return kept


def collapse_rare_tool_stats_by_provider(
    tool_stats_by_provider: dict[str, dict[str, ToolStats]],
    *,
    min_calls: int,
    sample_size: int,
    seed: int,
) -> dict[str, dict[str, ToolStats]]:
    return {
        provider: collapse_rare_tool_stats(
            tool_stats_by_provider[provider],
            min_calls=min_calls,
            sample_size=sample_size,
            seed=seed + index * 10_000,
        )
        for index, provider in enumerate(provider_order(tool_stats_by_provider))
    }


def plot_tool_name(name: str) -> str:
    if name.startswith("mcp_"):
        return "mcp"
    return name


def merge_tool_stats_for_plot(
    tool_stats: dict[str, ToolStats],
    *,
    sample_size: int,
    seed: int,
) -> dict[str, ToolStats]:
    groups: dict[str, list[tuple[str, ToolStats]]] = {}
    for name, stats in tool_stats.items():
        groups.setdefault(plot_tool_name(name), []).append((name, stats))

    merged: dict[str, ToolStats] = {}
    for index, (label, items) in enumerate(sorted(groups.items())):
        if len(items) == 1 and items[0][0] == label:
            merged[label] = items[0][1]
            continue
        aggregate = aggregate_tool_stats(
            label,
            items,
            sample_size=sample_size,
            seed=seed + index * 10_000,
        )
        if aggregate is not None:
            merged[aggregate[0]] = aggregate[1]
    return merged


def merge_tool_stats_for_plot_by_provider(
    tool_stats_by_provider: dict[str, dict[str, ToolStats]],
    *,
    sample_size: int,
    seed: int,
) -> dict[str, dict[str, ToolStats]]:
    return {
        provider: merge_tool_stats_for_plot(
            tool_stats_by_provider[provider],
            sample_size=sample_size,
            seed=seed + index * 100_000,
        )
        for index, provider in enumerate(provider_order(tool_stats_by_provider))
    }


def plot_ready_tool_stats_by_provider(
    result: dict[str, Any], args: argparse.Namespace
) -> dict[str, dict[str, "ToolStats"]]:
    """Merge plot-name aliases and collapse rare tools, as the old main() did."""
    merged = merge_tool_stats_for_plot_by_provider(
        result["tool_stats_by_provider"],
        sample_size=args.per_tool_sample_size,
        seed=args.seed + 350_000,
    )
    return collapse_rare_tool_stats_by_provider(
        merged,
        min_calls=args.min_tool_calls_for_plot,
        sample_size=args.per_tool_sample_size,
        seed=args.seed + 400_000,
    )
