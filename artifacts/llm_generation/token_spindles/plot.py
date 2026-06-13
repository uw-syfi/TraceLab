#!/usr/bin/env python3
"""Generate transparent spindle plots for adjusted prefix/append/output tokens."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
import sys  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import png_sidecar  # noqa: E402
import trace_db  # noqa: E402

EXP_DIR = SCRIPT_DIR
RESULTS_MD = SCRIPT_DIR / "result_analysis.md"

COLORS = {
    "prefix_tokens": "#1C38A0",
    "adjusted_append_tokens": "#579BF4",
    "output_tokens": "#F2942C",
}

TOKEN_AXIS_OFFSET = 32.0


@dataclass(frozen=True)
class PairValues:
    provider: str
    previous_provider: str
    previous_model: str
    prefix_tokens: float
    adjusted_append_tokens: float
    output_used_for_adjustment_tokens: float
    raw_append_tokens: float
    signed_adjusted_append_tokens: float
    subtracted_pair: bool


def int_field(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    return value if isinstance(value, int) else 0


def output_proxy(row: dict[str, Any]) -> int:
    output_tokens = int_field(row, "output_tokens")
    if row.get("provider") == "codex":
        return max(0, output_tokens - int_field(row, "reasoning_output_tokens"))
    return output_tokens


def should_subtract_previous_output(previous: dict[str, Any]) -> bool:
    if previous["provider"] == "claude":
        return True
    return previous["provider"] == "codex" and previous.get("model") == "gpt-5.5"


def load_pairs(con) -> tuple[list[PairValues], list[float], dict[str, int]]:
    """Per-session adjacent-round pairs + the output-token spindle, read from the trace DB.

    The DB replaces the old line-by-line JSONL parse, but the consumption order is identical: rows
    arrive in file order (``round_pk`` == ingest_seq == line order) and are grouped by ``session_id``
    preserving that order, so the per-session ``ORDER BY round_index, ingest_seq`` below reproduces
    the old stable sort by ``round_index`` (file order tie-break). The DB only holds parseable rows,
    so the old type guards (provider/session_id/round_index must be present) are encoded as the
    ``IS NOT NULL`` filter rather than per-line ``continue`` counters; ``stats`` keeps the same keys
    the result log reported. ``reasoning_output_tokens`` is fetched so the Codex output proxy matches
    bit-for-bit (it is null/0 for Claude rows, mirroring the old ``int_field`` fallback).
    """
    rows_by_session: dict[str, list[dict[str, Any]]] = {}
    all_output_tokens: list[float] = []
    stats: dict[str, int] = {}

    def bump(key: str, amount: int = 1) -> None:
        stats[key] = stats.get(key, 0) + amount

    # File order = round_pk; within a session, round_index then ingest_seq (= file order tie-break).
    # WHERE drops rows the old parser skipped on type grounds (missing provider/session_id/round
    # _index); the DB never holds malformed JSON, so bad_json / missing_* counters stay implicitly 0.
    for (
        session_id,
        provider,
        model,
        round_index,
        prefix_tokens,
        newly_append_tokens,
        output_tokens,
        reasoning_output_tokens,
    ) in con.execute(
        "SELECT session_id, provider, model, round_index, "
        "prefix_tokens, newly_append_tokens, output_tokens, reasoning_output_tokens "
        "FROM rounds "
        "WHERE provider IS NOT NULL AND session_id IS NOT NULL AND round_index IS NOT NULL "
        "ORDER BY session_id, round_index, ingest_seq"
    ).fetchall():
        raw = {
            "provider": provider,
            "model": model,
            "round_index": round_index,
            "prefix_tokens": prefix_tokens,
            "newly_append_tokens": newly_append_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
        }
        rows_by_session.setdefault(session_id, []).append(
            {
                "provider": provider,
                "model": model,
                "round_index": int(round_index),
                "prefix_tokens": int_field(raw, "prefix_tokens"),
                "newly_append_tokens": int_field(raw, "newly_append_tokens"),
                "output_tokens": int_field(raw, "output_tokens"),
                "output_proxy": output_proxy(raw),
            }
        )
        all_output_tokens.append(float(int_field(raw, "output_tokens")))
        bump("rows")

    pairs: list[PairValues] = []
    for rows in rows_by_session.values():
        rows.sort(key=lambda item: item["round_index"])
        for previous, current in zip(rows, rows[1:]):
            if current["round_index"] != previous["round_index"] + 1:
                bump("skipped_non_adjacent_pair")
                continue

            raw_append = float(current["newly_append_tokens"])
            subtracted_pair = should_subtract_previous_output(previous)
            output_tokens = float(previous["output_proxy"] if subtracted_pair else 0)
            signed_adjusted = raw_append - output_tokens
            adjusted_append = max(0.0, signed_adjusted)
            pairs.append(
                PairValues(
                    provider=str(current["provider"]),
                    previous_provider=str(previous["provider"]),
                    previous_model=str(previous.get("model") or ""),
                    prefix_tokens=float(current["prefix_tokens"]),
                    adjusted_append_tokens=adjusted_append,
                    output_used_for_adjustment_tokens=output_tokens,
                    raw_append_tokens=raw_append,
                    signed_adjusted_append_tokens=signed_adjusted,
                    subtracted_pair=subtracted_pair,
                )
            )
            bump("pairs")
            if subtracted_pair:
                bump("subtracted_pairs")
            else:
                bump("not_subtracted_pairs")

    return pairs, all_output_tokens, stats


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo = math.floor(index)
    hi = math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


def fmt_number(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.6g}"


def summary_row(metric: str, values: list[float]) -> dict[str, Any]:
    positives = sum(1 for value in values if value > 0)
    return {
        "metric": metric,
        "count": len(values),
        "positive_count": positives,
        "zero_count": len(values) - positives,
        "p25": fmt_number(percentile(values, 0.25)),
        "median": fmt_number(percentile(values, 0.50)),
        "p90": fmt_number(percentile(values, 0.90)),
        "p95": fmt_number(percentile(values, 0.95)),
        "p99": fmt_number(percentile(values, 0.99)),
        "min": fmt_number(min(values) if values else None),
        "max": fmt_number(max(values) if values else None),
    }


def token_ticks(max_value: float) -> tuple[list[float], list[str]]:
    raw = [0, 32, 128, 512, 2_048, 8_192, 32_768, 131_072, 524_288]
    ticks = [value for value in raw if value <= max_value * 1.05]
    labels = []
    for value in ticks:
        if value == 0:
            labels.append("0")
        elif value >= 1_048_576:
            labels.append(f"{value / 1_048_576:g}M")
        elif value >= 1024:
            labels.append(f"{value / 1024:g}k")
        else:
            labels.append(f"{value:g}")
    return [token_axis_x(value) for value in ticks], labels


def token_axis_x(value: float) -> float:
    return math.log2(value + TOKEN_AXIS_OFFSET) - math.log2(TOKEN_AXIS_OFFSET)


def smooth_density(values: list[float], x_min: float, x_max: float) -> tuple[np.ndarray, np.ndarray]:
    transformed = np.asarray([token_axis_x(value) for value in values], dtype=float)
    bins = np.linspace(x_min, x_max, 240)
    counts, edges = np.histogram(transformed, bins=bins, density=False)
    centers = (edges[:-1] + edges[1:]) / 2
    if counts.max(initial=0) <= 0:
        return centers, np.zeros_like(centers)

    sigma = 2.4
    radius = int(math.ceil(sigma * 4))
    kernel_x = np.arange(-radius, radius + 1)
    kernel = np.exp(-(kernel_x**2) / (2 * sigma**2))
    kernel /= kernel.sum()
    smoothed = np.convolve(counts.astype(float), kernel, mode="same")
    density = smoothed / smoothed.max()
    return centers, density


def plot_combined_spindles(
    metrics: dict[str, list[float]],
    titles: dict[str, str],
    x_max_value: float,
    output_dir: Path,
) -> Path:
    x_min = 0.0
    x_max = token_axis_x(x_max_value)

    fig, ax = plt.subplots(figsize=(11.625, 5.8))
    fig.patch.set_alpha(0)
    ax.set_facecolor((1, 1, 1, 0))

    metric_order = ["prefix_tokens", "adjusted_append_tokens", "output_tokens"]
    y_positions = {
        "prefix_tokens": 2.0,
        "adjusted_append_tokens": 1.0,
        "output_tokens": 0.0,
    }
    for metric in metric_order:
        values = metrics[metric]
        color = COLORS[metric]
        y_base = y_positions[metric]
        x, density = smooth_density(values, x_min, x_max)
        width = 0.34 * density
        ax.fill_between(
            x,
            y_base - width,
            y_base + width,
            color=color,
            alpha=1.0,
            linewidth=0,
            zorder=3,
        )

        quantile_markers = [
            ("p25", percentile(values, 0.25), 0.92, 2.2),
            ("p50", percentile(values, 0.50), 1.00, 3.0),
            ("p90", percentile(values, 0.90), 0.94, 2.4),
            ("p99", percentile(values, 0.99), 0.92, 2.2),
        ]
        for label, q_value, alpha, line_width in quantile_markers:
            if q_value is None:
                continue
            xpos = token_axis_x(q_value)
            ax.vlines(
                xpos,
                y_base,
                y_base + 0.38,
                color=color,
                alpha=alpha,
                linewidth=line_width,
                zorder=5,
            )
            ax.text(
                xpos,
                y_base + 0.43,
                label,
                color=color,
                fontsize=12,
                fontweight="bold",
                ha="center",
                va="bottom",
                clip_on=False,
                zorder=6,
            )

    ax.set_yticks([])
    ax.set_ylim(-0.55, 2.55)
    ax.set_xlim(x_min, x_max)
    ticks, labels = token_ticks(x_max_value)
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, fontsize=20, color="#526070")
    ax.tick_params(axis="x", length=0, pad=4)
    ax.tick_params(axis="y", length=0, pad=8)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(
        True,
        axis="x",
        color="#8FA1B8",
        alpha=0.42,
        linewidth=0.85,
        linestyle=(0, (4, 6)),
        zorder=0,
    )
    fig.tight_layout(pad=0.35)

    out = output_dir / "token_spindles_transparent.png"
    fig.savefig(out, dpi=260, transparent=True, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)
    return out


def write_summary(rows: list[dict[str, Any]], output_dir: Path) -> Path:
    out = output_dir / "token_spindle_summary.csv"
    fieldnames = [
        "metric",
        "count",
        "positive_count",
        "zero_count",
        "p25",
        "median",
        "p90",
        "p95",
        "p99",
        "min",
        "max",
    ]
    with out.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    pairs, all_output_tokens, stats = load_pairs(con)
    metrics = {
        "prefix_tokens": [pair.prefix_tokens for pair in pairs],
        "adjusted_append_tokens": [pair.adjusted_append_tokens for pair in pairs],
        "output_tokens": all_output_tokens,
    }
    x_max_value = max(max(values) for values in metrics.values() if values)
    x_max_value = max(x_max_value, 2_097_152)

    titles = {
        "prefix_tokens": "Prefix Tokens",
        "adjusted_append_tokens": "Adjusted Append Tokens",
        "output_tokens": "Output Tokens",
    }

    stale_separate_plots = [
        output_dir / "prefix_tokens_spindle_transparent.png",
        output_dir / "adjusted_append_tokens_spindle_transparent.png",
        output_dir / "output_tokens_spindle_transparent.png",
    ]
    for stale_path in stale_separate_plots:
        if stale_path.exists():
            stale_path.unlink()

    plot_path = plot_combined_spindles(metrics, titles, x_max_value, output_dir)
    summary_rows = [summary_row(metric, values) for metric, values in metrics.items()]
    summary_path = write_summary(summary_rows, output_dir)

    RESULTS_MD.write_text(
        "\n".join(
            [
                "# Token Spindle Plots",
                "",
                f"Output dir: `{output_dir}`",
                "",
                "Policy: adjusted append subtracts previous output only when the previous row is Claude or Codex `gpt-5.5`; Codex output proxy for subtraction is visible output (`output_tokens - reasoning_output_tokens`). The output spindle itself uses true `output_tokens` from every parsed invocation row.",
                f"Axis: compressed binary token scale using `log2(tokens + {TOKEN_AXIS_OFFSET:g}) - log2({TOKEN_AXIS_OFFSET:g})`, so the 0-32 token region is not visually over-expanded.",
                "",
                "Generated transparent PNG:",
                f"- `{plot_path}`",
                "",
                f"Summary CSV: `{summary_path}`",
                "",
                "Stats:",
                "```json",
                json.dumps(stats, indent=2, sort_keys=True),
                "```",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    png_sidecar.make_self_contained(
        output_dir,
        code_files=[Path(__file__)],
        readme_path=SCRIPT_DIR / "README.md",
    )
    print(RESULTS_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
