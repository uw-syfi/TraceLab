#!/usr/bin/env python3
"""Plot prefix vs append tokens after subtracting prior-round output."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import png_sidecar
from style import TEXT_COLOR, plot_color, save_plot, short_label  # noqa: E402
from accumulators import ReservoirSampler  # noqa: E402
from formatters import (
    apply_binary_token_axis,
    infer_first_token_tick,
    token_axis_values,
)  # noqa: E402
import trace_db  # noqa: E402

import matplotlib.pyplot as plt


DEFAULT_OUTPUT_DIR = EXP_DIR


def coerce_int(value: Any) -> int:
    """Match the old int_field: keep ints, treat everything else (incl. None) as 0.

    DuckDB returns BIGINT columns as Python ints and NULL as None; booleans are
    excluded to mirror ``isinstance(value, int)`` rejecting bools the JSON loader
    never produced for these numeric token columns.
    """
    if isinstance(value, bool):
        return 0
    return value if isinstance(value, int) else 0


def output_proxy(
    *, output_tokens: Any, reasoning_output_tokens: Any, provider: Any, mode: str
) -> int:
    output_value = coerce_int(output_tokens)
    if mode == "total":
        return output_value
    if mode == "visible-for-codex" and provider == "codex":
        return max(0, output_value - coerce_int(reasoning_output_tokens))
    return output_value


def should_subtract_previous_output(previous: dict[str, Any], policy: str) -> bool:
    if policy == "all":
        return True
    if policy == "claude-and-gpt55":
        if previous["provider"] == "claude":
            return True
        return previous["provider"] == "codex" and previous.get("model") == "gpt-5.5"
    raise ValueError(f"Unsupported subtract policy: {policy}")


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def fmt(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6g}"


def load_adjusted_pairs(
    con: "duckdb.DuckDBPyConnection",
    *,
    subtract_output: str,
    subtract_policy: str,
    sample_size: int,
    seed: int,
) -> tuple[ReservoirSampler, dict[str, list[float]], Counter[str]]:
    """Reconstruct adjacent (previous, current) round pairs per session from the trace DB.

    The old JSONL loader grouped rows by ``session_id`` in file order (the per-session ``list``
    appears in first-appearance order), then did a *stable* sort by ``round_index`` — so within a
    session, equal ``round_index`` kept file order. ``round_pk`` (== ``ingest_seq``) is exactly that
    file order, so pulling ``ORDER BY ingest_seq`` and grouping into ``rows_by_session`` reproduces
    BOTH the per-session row order AND the first-appearance session-visitation order byte-for-byte.
    Session-visitation order matters because the reservoir sampler's retained PNG sample depends on
    the order of ``add()`` calls (the summary CSV is order-independent, but the scatter is not).
    Rows missing a string ``provider``/``session_id`` or an integer ``round_index`` are skipped
    exactly as before; the DB pins those columns, so the skips are the NULL rows.
    """
    stats: Counter[str] = Counter()

    rows_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (
        provider,
        session_id,
        model,
        round_index,
        prefix_tokens,
        newly_append_tokens,
        output_tokens,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT provider, session_id, model, round_index, "
        "prefix_tokens, newly_append_tokens, output_tokens, reasoning_output_tokens "
        "FROM rounds ORDER BY ingest_seq"
    ).fetchall():
        if not isinstance(provider, str) or not isinstance(session_id, str):
            stats["missing_provider_or_session"] += 1
            continue
        if not isinstance(round_index, int) or isinstance(round_index, bool):
            stats["missing_round_index"] += 1
            continue
        rows_by_session[session_id].append(
            {
                "provider": provider,
                "model": model,
                "round_index": round_index,
                "prefix_tokens": coerce_int(prefix_tokens),
                "newly_append_tokens": coerce_int(newly_append_tokens),
                "output_proxy": output_proxy(
                    output_tokens=output_tokens,
                    reasoning_output_tokens=reasoning_output_tokens,
                    provider=provider,
                    mode=subtract_output,
                ),
            }
        )
        stats["rows"] += 1

    sampler = ReservoirSampler(sample_size, seed)
    summary_values: dict[str, list[float]] = defaultdict(list)

    for rows in rows_by_session.values():
        rows.sort(key=lambda item: item["round_index"])
        for previous, current in zip(rows, rows[1:]):
            if current["round_index"] != previous["round_index"] + 1:
                stats["skipped_non_adjacent_pair"] += 1
                continue
            prefix = float(current["prefix_tokens"])
            raw_append = float(current["newly_append_tokens"])
            subtract_this_pair = should_subtract_previous_output(
                previous, subtract_policy
            )
            previous_output = float(
                previous["output_proxy"] if subtract_this_pair else 0
            )
            signed_adjusted = raw_append - previous_output
            adjusted_append = max(0.0, signed_adjusted)
            provider = current["provider"]
            sampler.add((provider, prefix, adjusted_append))
            summary_values[f"{provider}:raw_append"].append(raw_append)
            summary_values[f"{provider}:previous_output"].append(previous_output)
            summary_values[f"{provider}:signed_adjusted_append"].append(signed_adjusted)
            summary_values[f"{provider}:adjusted_append"].append(adjusted_append)
            summary_values[f"{provider}:subtracted_pair"].append(
                1.0 if subtract_this_pair else 0.0
            )
            if signed_adjusted < 0:
                summary_values[f"{provider}:clipped_after_subtract"].append(1.0)
            else:
                summary_values[f"{provider}:clipped_after_subtract"].append(0.0)
            stats["pairs"] += 1
            stats[f"subtract_policy_{subtract_policy}"] += 1
            if subtract_this_pair:
                stats["subtracted_pairs"] += 1
            else:
                stats["not_subtracted_pairs"] += 1

    return sampler, summary_values, stats


def plot_adjusted_prefix_append(
    pair_sampler: ReservoirSampler,
    output_dir: Path,
    *,
    title: str,
    max_groups: int,
) -> Path | None:
    values = list(pair_sampler.values)
    if not values:
        return None

    counts = Counter(item[0] for item in values)
    plotted_groups = [label for label, _count in counts.most_common(max_groups)]

    fig, ax = plt.subplots(figsize=(8, 6.5))
    ax.set_title(title, fontsize=16, fontweight="semibold", color=TEXT_COLOR)
    ax.set_xlabel("Prefix Tokens (binary scale)")
    ax.set_ylabel("Append Tokens (binary scale)")
    ax.grid(True, alpha=0.3, which="both")

    prefixes = [prefix for _group, prefix, _append in values]
    appends = [append for _group, _prefix, append in values]
    prefix_first_tick = infer_first_token_tick(prefixes)
    append_first_tick = infer_first_token_tick(appends)
    apply_binary_token_axis(
        ax,
        axis="x",
        max_value=max(prefixes) if prefixes else 1.0,
        first_tick=prefix_first_tick,
    )
    apply_binary_token_axis(
        ax,
        axis="y",
        max_value=max(appends) if appends else 1.0,
        first_tick=append_first_tick,
    )

    for index, label in enumerate(plotted_groups):
        xs = [prefix for group, prefix, _append in values if group == label]
        ys = [append for group, _prefix, append in values if group == label]
        ax.scatter(
            token_axis_values(xs, prefix_first_tick),
            token_axis_values(ys, append_first_tick),
            s=9,
            alpha=0.18,
            linewidths=0,
            color=plot_color(label, index),
            label=f"{short_label(label)} (sample n={len(xs):,})",
        )

    ax.legend(fontsize=8.5, markerscale=2)
    fig.tight_layout()
    out = output_dir / "prefix_vs_adjusted_append_sample.png"
    save_plot(fig, out)
    return out


def write_summary_csv(
    values_by_key: dict[str, list[float]],
    output_dir: Path,
) -> Path:
    out = output_dir / "prefix_vs_adjusted_append_summary.csv"
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "provider",
                "metric",
                "count",
                "median",
                "p90",
                "p95",
                "p99",
                "min",
                "max",
            ],
        )
        writer.writeheader()
        for key, values in sorted(values_by_key.items()):
            provider, metric = key.split(":", 1)
            writer.writerow(
                {
                    "provider": provider,
                    "metric": metric,
                    "count": len(values),
                    "median": fmt(median(values) if values else None),
                    "p90": fmt(percentile(values, 0.90)),
                    "p95": fmt(percentile(values, 0.95)),
                    "p99": fmt(percentile(values, 0.99)),
                    "min": fmt(min(values) if values else None),
                    "max": fmt(max(values) if values else None),
                }
            )
    print(f"Saved {out}", file=sys.stderr)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--subtract-output",
        choices=["total", "visible-for-codex"],
        default="total",
        help=(
            "Output token proxy to subtract from the next round's append tokens. "
            "'visible-for-codex' subtracts output_tokens - reasoning_output_tokens for Codex."
        ),
    )
    parser.add_argument(
        "--subtract-policy",
        choices=["all", "claude-and-gpt55"],
        default="claude-and-gpt55",
        help=(
            "Which previous-round outputs to subtract. Default 'claude-and-gpt55' "
            "subtracts only Claude and Codex gpt-5.5, whose prior output (incl. "
            "reasoning) is carried into the next round's append; gpt-5.4 and earlier "
            "Codex models do not carry reasoning forward and are left raw. 'all' "
            "subtracts every provider/model (over-subtracts gpt-5.4-and-before)."
        ),
    )
    parser.add_argument("--pair-sample-size", type=int, default=80_000)
    parser.add_argument("--max-groups", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    sampler, summary_values, stats = load_adjusted_pairs(
        con,
        subtract_output=args.subtract_output,
        subtract_policy=args.subtract_policy,
        sample_size=args.pair_sample_size,
        seed=args.seed + 100_000,
    )
    plot_adjusted_prefix_append(
        sampler,
        args.output_dir,
        title="Prefix vs Append Tokens",
        max_groups=args.max_groups,
    )
    write_summary_csv(summary_values, args.output_dir)
    png_sidecar.make_self_contained(
        args.output_dir,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
