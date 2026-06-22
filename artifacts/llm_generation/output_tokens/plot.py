#!/usr/bin/env python3
"""Distribution of per-round output (generated) token counts by provider/model.

See README.md for definitions and assumptions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from dataclasses import dataclass  # noqa: E402
from typing import Any  # noqa: E402
import csv  # noqa: E402
import numpy as np  # noqa: E402
from style import PLOT_COLORS, mticker, plt, short_label  # noqa: E402
from formatters import apply_binary_token_axis, token_axis_bins, token_axis_values  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

# group_key() equivalents in SQL (COALESCE mirrors the old "<unknown-*>" fallbacks).
_GROUP_EXPR = {
    "provider": "COALESCE(provider, '<unknown-provider>')",
    "model": "COALESCE(model, '<unknown-model>')",
    "provider_model": "COALESCE(provider, '<unknown-provider>') || ':' || COALESCE(model, '<unknown-model>')",
}
SUMMARY_PERCENTILES = (50, 90, 95, 99)


def compact_count_label(value: float, _pos: int | None = None) -> str:
    """Format invocation counts compactly for paper figure axes."""
    if abs(value) >= 1000:
        return f"{value / 1000:g}k"
    return f"{value:g}"


@dataclass
class MetricStats:
    """Exact distribution of one group's metric — full data, no reservoir sampling.

    DuckDB lets us keep every value, so percentiles/histograms are exact instead of sampled. (The
    old loader reservoir-sampled at 200k/group to bound memory while parsing JSON; that cap is gone.)
    """

    values: np.ndarray

    @property
    def count(self) -> int:
        return int(self.values.size)

    @property
    def minimum(self) -> float | None:
        return float(self.values.min()) if self.values.size else None

    @property
    def maximum(self) -> float | None:
        return float(self.values.max()) if self.values.size else None

    @property
    def mean(self) -> float | None:
        return float(self.values.mean()) if self.values.size else None

    def percentiles(self, ps: tuple[int, ...]) -> dict[int, float | None]:
        if not self.values.size:
            return {p: None for p in ps}
        quantiles = np.percentile(self.values, list(ps))
        return {p: float(v) for p, v in zip(ps, quantiles, strict=True)}


def load_metric_by_group(con, *, column: str, group_by: str) -> dict[str, MetricStats]:
    """``{group_label: MetricStats}`` for a round-level token column, plus an ``all`` group.

    Values are every non-null, non-negative observation (matching the old NumericTracker's
    ``allow_zero`` rule), so the resulting stats are exact.
    """
    expr = _GROUP_EXPR[group_by]
    rows = con.execute(
        f"SELECT {expr} AS grp, CAST({column} AS DOUBLE) AS val "
        f"FROM rounds WHERE {column} IS NOT NULL AND {column} >= 0"
    ).fetchall()

    by_group: dict[str, list[float]] = {}
    all_values: list[float] = []
    for grp, val in rows:
        by_group.setdefault(grp, []).append(val)
        all_values.append(val)

    stats = {grp: MetricStats(np.asarray(vals, dtype=float)) for grp, vals in by_group.items()}
    stats["all"] = MetricStats(np.asarray(all_values, dtype=float))
    return stats


def selected_groups(stats: dict[str, MetricStats], max_groups: int) -> list[tuple[str, MetricStats]]:
    """The plotted groups: everything except ``all``, biggest first, capped at ``max_groups``."""
    items = [(label, s) for label, s in stats.items() if label != "all"]
    if not items:
        items = [("all", stats["all"])]
    items.sort(key=lambda item: item[1].count, reverse=True)
    return items[:max_groups]


def plot_output_tokens(
    stats: dict[str, MetricStats], output_dir: Path, max_groups: int, compact: bool = False
) -> None:
    groups = selected_groups(stats, max_groups)
    plotted: list[tuple[str, MetricStats]] = [(label, s) for label, s in groups if s.count]
    if not plotted:
        return
    max_value = max((s.maximum or 0.0) for _label, s in plotted)
    max_value = max(max_value, 1.0)

    first_tick = 16.0
    bins = token_axis_bins(max_value, first_tick)

    # The compact profile renders nicely at one LaTeX column (small native size, fonts
    # sized for that width, no in-figure title since the LaTeX caption carries it).
    if compact:
        figsize, lw, fs_lab, fs_tick, fs_leg, max_ticks = (3.4, 2.55), 1.4, 8.0, 7.0, 6.5, 8
    else:
        figsize, lw, fs_lab, fs_tick, fs_leg, max_ticks = (9.5, 5.8), 1.8, None, None, 8.5, 14

    fig, ax = plt.subplots(figsize=figsize)
    if not compact:
        ax.set_title("Output Token Length Distribution")
    ax.set_xlabel("Output Tokens (binary scale)", fontsize=fs_lab)
    ax.set_ylabel("Invocations", fontsize=fs_lab)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(compact_count_label))
    ax.tick_params(labelsize=fs_tick)
    ax.grid(True, alpha=0.3)
    apply_binary_token_axis(ax, axis="x", max_value=max_value, first_tick=first_tick, max_ticks=max_ticks)

    for index, (label, s) in enumerate(plotted):
        ax.hist(
            token_axis_values(list(s.values), first_tick),
            bins=bins,
            histtype="step",
            linewidth=lw,
            color=PLOT_COLORS[index % len(PLOT_COLORS)],
            label=short_label(label),
        )

    ax.legend(fontsize=fs_leg)
    fig.tight_layout()
    out = output_dir / "output_tokens_distribution.png"
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor="white")
    fig.savefig(output_dir / "output_tokens_distribution.pdf", bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)


def write_output_token_summary(stats: dict[str, MetricStats], output_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, s in sorted(stats.items(), key=lambda item: (item[0] != "all", item[0])):
        pcts = s.percentiles(SUMMARY_PERCENTILES)
        rows.append(
            {
                "group": label,
                "count": s.count,
                "min": s.minimum,
                "p50": pcts[50],
                "p90": pcts[90],
                "p95": pcts[95],
                "p99": pcts[99],
                "max": s.maximum,
                "mean": s.mean,
                "sample_count": s.count,  # exact: the "sample" is the full data
                "sampled": False,
            }
        )

    path = output_dir / "output_tokens_summary.csv"
    fieldnames = [
        "group", "count", "min", "p50", "p90", "p95", "p99", "max",
        "mean", "sample_count", "sampled",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument(
        "--group-by", choices=["provider", "model", "provider_model"], default="provider",
        help="grouping for the distribution",
    )
    parser.add_argument("--max-groups", type=int, default=8, help="maximum groups to plot")
    parser.add_argument(
        "--compact", action="store_true",
        help="render a single-LaTeX-column figure (small size, no in-figure title)",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir
    stats = load_metric_by_group(con, column="output_tokens", group_by=args.group_by)

    plot_output_tokens(stats, out, args.max_groups, compact=args.compact)
    write_output_token_summary(stats, out)

    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
