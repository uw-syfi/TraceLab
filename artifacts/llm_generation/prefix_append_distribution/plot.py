#!/usr/bin/env python3
"""Distribution of LLM input tokens split into cached prefix vs newly appended.

Renders the prefix/append input-token figures and summaries for the normalized
round trace. See README.md for definitions and assumptions.

Data layer: this experiment queries the shared trace DuckDB
(``artifacts/utils/trace_db.py``) instead of re-parsing JSONL. DuckDB keeps every
row, so the histograms, CDFs, percentiles, means, and append-weighted bins are
**exact** over all rounds (the old loader reservoir-sampled to bound memory). The
only inherently-sampled figure is the prefix-vs-append scatter, which stays a
deterministic visual subsample (see ``scatter_pairs``).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from dataclasses import dataclass, field  # noqa: E402
from typing import Any, Callable  # noqa: E402
from collections import Counter  # noqa: E402
import csv  # noqa: E402
import numpy as np  # noqa: E402
from style import (
    PLOT_COLORS,
    mticker,
    plot_color,
    plt,
    provider_order,
    provider_title,
    short_label,
)  # noqa: E402
from accumulators import (
    AppendTokenBinStats,
    in_half_open_bin,
    make_append_token_bins,
)  # noqa: E402
from formatters import (
    apply_binary_token_axis,
    infer_first_token_tick,
    token_axis_bins,
    token_axis_values,
)  # noqa: E402
from cdf import active_bin_mask, plot_stacked_share_panels  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

# group_key() equivalents in SQL (COALESCE mirrors the old "<unknown-*>" fallbacks).
_GROUP_EXPR = {
    "provider": "COALESCE(provider, '<unknown-provider>')",
    "model": "COALESCE(model, '<unknown-model>')",
    "provider_model": "COALESCE(provider, '<unknown-provider>') || ':' || COALESCE(model, '<unknown-model>')",
}
# The token columns plotted/summarized, in the order the summary CSV emits them.
_TOKEN_METRICS = ("prefix_tokens", "newly_append_tokens")

# Default visual subsample size for the prefix-vs-append scatter (old --pair-sample-size).
SCATTER_SAMPLE_SIZE = 80_000


@dataclass
class MetricStats:
    """Exact distribution of one token column within one group — full data, no sampling.

    DuckDB lets us keep every value, so percentiles/histograms/means are exact instead of
    sampled. (The old NumericTracker reservoir-sampled at 200k/group to bound memory.) ``values``
    holds every valid (non-null, ``>= 0``) observation in ingest order, ``missing`` counts the
    nulls and ``invalid`` the negatives — mirroring NumericTracker's ``allow_zero`` rule.
    """

    values: np.ndarray
    missing: int = 0
    invalid: int = 0

    @property
    def count(self) -> int:
        return int(self.values.size)

    @property
    def maximum(self) -> float | None:
        return float(self.values.max()) if self.values.size else None

    @property
    def minimum(self) -> float | None:
        return float(self.values.min()) if self.values.size else None

    @property
    def mean(self) -> float | None:
        # Match the old running ``total += number`` (left-to-right float sum in ingest order)
        # exactly: sum the Python floats in order, then divide.
        if not self.values.size:
            return None
        return float(sum(self.values.tolist())) / self.values.size

    def summary(self) -> dict[str, Any]:
        if self.values.size:
            p50, p90, p99 = (float(v) for v in np.percentile(self.values, [50, 90, 99]))
        else:
            p50 = p90 = p99 = None
        return {
            "count": self.count,
            "missing": self.missing,
            "invalid": self.invalid,
            "mean": self.mean,
            "min": self.minimum,
            "max": self.maximum,
            "sample_count": self.count,  # exact: the "sample" is the full data
            "sampled": False,
            "p50": p50,
            "p90": p90,
            "p99": p99,
        }


@dataclass
class TokenGroup:
    """Per-group prefix/append metric stats plus the group's total row count."""

    rows: int = 0
    prefix: MetricStats = field(default_factory=lambda: MetricStats(np.empty(0)))
    append: MetricStats = field(default_factory=lambda: MetricStats(np.empty(0)))

    def metric(self, name: str) -> MetricStats:
        return self.prefix if name == "prefix_tokens" else self.append


def _metric_stats(con, *, column: str, expr: str, group_filter: str) -> dict[str, MetricStats]:
    """``{group_label: MetricStats}`` for one token column (valid values + missing/invalid counts).

    Valid = non-null and ``>= 0`` (NumericTracker's ``allow_zero`` rule); missing = null;
    invalid = ``< 0``. Values are returned in ingest order so the running-sum mean matches the
    old loader byte-for-byte.
    """
    valid_rows = con.execute(
        f"SELECT {expr} AS grp, CAST({column} AS DOUBLE) AS val "
        f"FROM rounds WHERE {group_filter} AND {column} IS NOT NULL AND {column} >= 0 "
        f"ORDER BY round_pk"
    ).fetchall()
    miss_rows = con.execute(
        f"SELECT {expr} AS grp, "
        f"       count(*) FILTER (WHERE {column} IS NULL) AS missing, "
        f"       count(*) FILTER (WHERE {column} < 0) AS invalid "
        f"FROM rounds WHERE {group_filter} GROUP BY grp"
    ).fetchall()

    by_group: dict[str, list[float]] = {}
    for grp, val in valid_rows:
        by_group.setdefault(grp, []).append(val)
    miss: dict[str, tuple[int, int]] = {grp: (int(m), int(i)) for grp, m, i in miss_rows}

    groups = set(by_group) | set(miss)
    out: dict[str, MetricStats] = {}
    for grp in groups:
        m, i = miss.get(grp, (0, 0))
        out[grp] = MetricStats(
            np.asarray(by_group.get(grp, []), dtype=float), missing=m, invalid=i
        )
    return out


def load_token_groups(con, *, group_by: str) -> dict[str, TokenGroup]:
    """Build ``{group_label: TokenGroup}`` (plus an ``all`` group) for prefix/append tokens.

    Mirrors the old loader: every row contributes to its ``group_key`` group and to ``all``;
    each group tracks prefix/append valid values + missing/invalid counts and its total ``rows``.
    """
    expr = _GROUP_EXPR[group_by]

    # Per-group total row count (the old TokenGroup.rows, incremented for every row regardless
    # of token validity).
    rows_by_group = {
        grp: int(n)
        for grp, n in con.execute(
            f"SELECT {expr} AS grp, count(*) FROM rounds GROUP BY grp"
        ).fetchall()
    }
    total_rows = int(con.execute("SELECT count(*) FROM rounds").fetchone()[0])

    prefix_stats = _metric_stats(con, column="prefix_tokens", expr=expr, group_filter="TRUE")
    append_stats = _metric_stats(con, column="newly_append_tokens", expr=expr, group_filter="TRUE")
    prefix_all = _metric_stats(con, column="prefix_tokens", expr="'all'", group_filter="TRUE")
    append_all = _metric_stats(con, column="newly_append_tokens", expr="'all'", group_filter="TRUE")

    empty = lambda: MetricStats(np.empty(0))
    groups: dict[str, TokenGroup] = {}
    for grp, n in rows_by_group.items():
        groups[grp] = TokenGroup(
            rows=n,
            prefix=prefix_stats.get(grp, empty()),
            append=append_stats.get(grp, empty()),
        )
    groups["all"] = TokenGroup(
        rows=total_rows,
        prefix=prefix_all.get("all", empty()),
        append=append_all.get("all", empty()),
    )
    return groups


def scatter_pairs(con, *, group_by: str, sample_size: int) -> list[tuple[str, float, float]]:
    """Deterministic visual subsample of ``(group_label, prefix, append)`` for the scatter.

    The scatter is inherently visual — 350k+ points can't be drawn — so we keep a fixed-size
    subsample. Determinism comes from a Knuth-multiplicative hash of the surrogate key:
    order by ``(round_pk * 2654435761) % 1000000`` and take the first ``sample_size`` rows
    (ties broken by ``round_pk``). This is reproducible across DB builds and engines but is NOT
    the old reservoir sample, so the scatter CSV/figure is not byte-compatible with the old run.
    """
    expr = _GROUP_EXPR[group_by]
    rows = con.execute(
        f"SELECT {expr} AS grp, CAST(prefix_tokens AS DOUBLE), CAST(newly_append_tokens AS DOUBLE) "
        f"FROM rounds WHERE prefix_tokens >= 0 AND newly_append_tokens >= 0 "
        f"ORDER BY (round_pk * 2654435761) % 1000000, round_pk "
        f"LIMIT {int(sample_size)}"
    ).fetchall()
    return [(grp, float(prefix), float(append)) for grp, prefix, append in rows]


def append_bins(con, *, by_provider: bool) -> dict[str, list[AppendTokenBinStats]] | list[AppendTokenBinStats]:
    """Append-token weighted bins (rounds + total append tokens per half-open bucket).

    Exact over all rounds with valid prefix AND append (matches the old loader's pair gate:
    ``prefix >= 0 AND append >= 0``). Returns the global bin list when ``by_provider`` is False,
    else ``{provider: bins}``.
    """
    if by_provider:
        rows = con.execute(
            "SELECT COALESCE(provider, '<unknown-provider>') AS prov, "
            "       CAST(newly_append_tokens AS DOUBLE) AS append "
            "FROM rounds WHERE prefix_tokens >= 0 AND newly_append_tokens >= 0 "
            "ORDER BY round_pk"
        ).fetchall()
        bins_by_provider: dict[str, list[AppendTokenBinStats]] = {}
        for prov, append in rows:
            bins = bins_by_provider.setdefault(prov, make_append_token_bins())
            for bin_stats in bins:
                if in_half_open_bin(append, bin_stats.lo_tokens, bin_stats.hi_tokens):
                    bin_stats.add(append)
                    break
        return bins_by_provider

    rows = con.execute(
        "SELECT CAST(newly_append_tokens AS DOUBLE) AS append "
        "FROM rounds WHERE prefix_tokens >= 0 AND newly_append_tokens >= 0 "
        "ORDER BY round_pk"
    ).fetchall()
    bins = make_append_token_bins()
    for (append,) in rows:
        for bin_stats in bins:
            if in_half_open_bin(append, bin_stats.lo_tokens, bin_stats.hi_tokens):
                bin_stats.add(append)
                break
    return bins


def selected_token_groups(
    token_groups: dict[str, TokenGroup], max_groups: int
) -> list[tuple[str, TokenGroup]]:
    """The plotted groups: everything except ``all``, most rows first, capped at ``max_groups``."""
    items = [(key, value) for key, value in token_groups.items() if key != "all"]
    if not items:
        items = [("all", token_groups["all"])]
    items.sort(key=lambda item: item[1].rows, reverse=True)
    return items[:max_groups]


def plot_token_histograms(
    token_groups: dict[str, TokenGroup],
    output_dir: Path,
    max_groups: int,
) -> None:
    groups = selected_token_groups(token_groups, max_groups)
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))
    fig.suptitle("Prefix and Append Token Length Distribution", fontsize=14, y=1.02)

    metrics: list[tuple[str, str, Callable[[TokenGroup], MetricStats], float | None]] = [
        ("prefix_tokens", "Prefix Tokens", lambda group: group.prefix, None),
        ("newly_append_tokens", "Append Tokens", lambda group: group.append, 32.0),
    ]

    for ax, (_metric_name, title, getter, first_tick_override) in zip(axes, metrics):
        max_value = 1.0
        all_values: list[float] = []
        for _label, group in groups:
            if getter(group).maximum is not None:
                max_value = max(max_value, float(getter(group).maximum))
            all_values.extend(getter(group).values.tolist())
        first_tick = first_tick_override or infer_first_token_tick(all_values)
        bins = token_axis_bins(max_value, first_tick)
        ax.set_title(title)
        ax.set_xlabel("Tokens (binary scale)")
        ax.set_ylabel("Invocations")
        ax.yaxis.set_major_formatter(mticker.StrMethodFormatter("{x:,.0f}"))
        ax.grid(True, alpha=0.3)
        apply_binary_token_axis(
            ax,
            axis="x",
            max_value=max_value,
            first_tick=first_tick,
            max_ticks=18,
        )

        for index, (label, group) in enumerate(groups):
            tracker = getter(group)
            values = tracker.values.tolist()
            if not values:
                continue
            summary = tracker.summary()
            sample_note = ", sampled" if summary["sampled"] else ""
            ax.hist(
                token_axis_values(values, first_tick),
                bins=bins,
                histtype="step",
                linewidth=1.8,
                color=PLOT_COLORS[index % len(PLOT_COLORS)],
                label=(
                    f"{short_label(label)} "
                    f"(n={summary['count']:,}, p50={summary['p50']:.0f}, "
                    f"p90={summary['p90']:.0f}{sample_note})"
                ),
            )

        ax.legend(fontsize=8.5)

    fig.tight_layout()
    out = output_dir / "prefix_append_distribution.png"
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)


def plot_token_cdfs(
    token_groups: dict[str, TokenGroup], output_dir: Path, max_groups: int
) -> None:
    groups = selected_token_groups(token_groups, max_groups)
    fig, axes = plt.subplots(1, 2, figsize=(15, 5.5))
    fig.suptitle("Prefix and Append Token Length CDF", fontsize=14, y=1.02)

    metrics: list[tuple[str, Callable[[TokenGroup], MetricStats]]] = [
        ("Prefix Tokens", lambda group: group.prefix),
        ("Append Tokens", lambda group: group.append),
    ]

    for ax, (title, getter) in zip(axes, metrics):
        all_values: list[float] = []
        max_value = 1.0
        for _label, group in groups:
            all_values.extend(getter(group).values.tolist())
            if getter(group).maximum is not None:
                max_value = max(max_value, float(getter(group).maximum))
        first_tick = infer_first_token_tick(all_values)

        ax.set_title(title)
        ax.set_xlabel("Tokens (binary scale)")
        ax.set_ylabel("CDF")
        ax.set_ylim(0, 100)
        ax.grid(True, alpha=0.3, which="both")
        ax.yaxis.set_major_formatter(mticker.PercentFormatter())
        apply_binary_token_axis(
            ax, axis="x", max_value=max_value, first_tick=first_tick
        )

        for index, (label, group) in enumerate(groups):
            values = np.sort(np.asarray(getter(group).values, dtype=float))
            if values.size == 0:
                continue
            y = np.arange(1, values.size + 1) / values.size * 100
            ax.plot(
                token_axis_values(values, first_tick),
                y,
                linewidth=1.8,
                color=PLOT_COLORS[index % len(PLOT_COLORS)],
                label=f"{short_label(label)} (sample n={values.size:,})",
            )

        ax.legend(fontsize=8.5)

    fig.tight_layout()
    out = output_dir / "prefix_append_cdf.png"
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)


def plot_prefix_append_scatter(
    pairs: list[tuple[str, float, float]],
    output_dir: Path,
    max_groups: int,
) -> None:
    values = pairs
    if not values:
        return

    counts = Counter(item[0] for item in values)
    plotted_groups = [label for label, _count in counts.most_common(max_groups)]

    fig, ax = plt.subplots(figsize=(8, 6.5))
    ax.set_xlabel("Prefix Tokens", fontsize=19)
    ax.set_ylabel("Append Tokens", fontsize=19)
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
    ax.tick_params(axis="both", labelsize=15)

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
            rasterized=True,  # keep points raster so the PDF stays small; axes/text stay vector
        )

    ax.legend(fontsize=14, markerscale=2)
    fig.tight_layout()
    out = output_dir / "prefix_vs_append_sample.png"
    fig.savefig(out, dpi=180, bbox_inches="tight", facecolor="white")
    # Vector PDF for the paper (rasterized point cloud at the same dpi, vector axes/labels).
    fig.savefig(
        output_dir / "prefix_vs_append_sample.pdf", dpi=180, bbox_inches="tight", facecolor="white"
    )
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)


def plot_append_weighted_bins(
    bins_by_provider: dict[str, list[AppendTokenBinStats]],
    output_dir: Path,
) -> None:
    provider_panels = [
        (provider, bins)
        for provider, bins in (
            (provider, bins_by_provider[provider])
            for provider in provider_order(bins_by_provider)
        )
        if sum(item.rounds for item in bins) > 0
    ]
    if not provider_panels:
        return

    labels = [item.label for item in provider_panels[0][1]]
    panels: list[tuple[str, list[float], list[float]]] = []
    for provider, bins in provider_panels:
        total_tokens = sum(item.total_append_tokens for item in bins)
        total_rounds = sum(item.rounds for item in bins)
        round_share = [
            item.rounds / total_rounds * 100 if total_rounds else 0.0 for item in bins
        ]
        token_share = [
            item.total_append_tokens / total_tokens * 100 if total_tokens else 0.0
            for item in bins
        ]
        panels.append((provider_title(provider), round_share, token_share))

    active = active_bin_mask(panels, len(labels))
    if not active:
        return
    active_panels = [
        (title, [count_share[i] for i in active], [mass_share[i] for i in active])
        for title, count_share, mass_share in panels
    ]
    plot_stacked_share_panels(
        active_panels,
        [labels[i] for i in active],
        output_dir,
        count_bar_label="rounds",
        mass_bar_label="append tokens",
        suptitle="Rounds vs Append Tokens",
        caption="Most Rounds Are Small — But Most Tokens Come From the Rare Large Rounds",
        legend_title="Append Length per Round (tokens)",
        out_name="append_tokens_weighted_bins.png",
        compact=True,  # single-LaTeX-column profile for the paper figure
    )


def write_token_summary(
    token_groups: dict[str, TokenGroup],
    output_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label, group in sorted(
        token_groups.items(), key=lambda item: (item[0] != "all", item[0])
    ):
        for metric_name in _TOKEN_METRICS:
            row = {
                "group": label,
                "metric": metric_name,
                "rows": group.rows,
                **group.metric(metric_name).summary(),
            }
            rows.append(row)

    path = output_dir / "token_length_summary.csv"
    fieldnames = [
        "group",
        "metric",
        "rows",
        "count",
        "missing",
        "invalid",
        "mean",
        "min",
        "p50",
        "p90",
        "p99",
        "max",
        "sample_count",
        "sampled",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Saved {path}", file=sys.stderr)
    return rows


def write_append_weighted_bins(
    bins: list[AppendTokenBinStats],
    output_dir: Path,
) -> list[dict[str, Any]]:
    total_tokens = sum(item.total_append_tokens for item in bins)
    total_rounds = sum(item.rounds for item in bins)
    rows: list[dict[str, Any]] = []
    for item in bins:
        rows.append(
            {
                "label": item.label,
                "lo_tokens": item.lo_tokens,
                "hi_tokens": "" if item.hi_tokens is None else item.hi_tokens,
                "rounds": item.rounds,
                "total_append_tokens": item.total_append_tokens,
                "token_share": (
                    item.total_append_tokens / total_tokens if total_tokens else 0.0
                ),
                "round_share": item.rounds / total_rounds if total_rounds else 0.0,
            }
        )

    path = output_dir / "append_tokens_weighted_bins.csv"
    fieldnames = [
        "label",
        "lo_tokens",
        "hi_tokens",
        "rounds",
        "total_append_tokens",
        "token_share",
        "round_share",
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
        help="grouping for the token distributions",
    )
    parser.add_argument("--max-groups", type=int, default=8, help="maximum token groups to plot")
    parser.add_argument(
        "--pair-sample-size", type=int, default=SCATTER_SAMPLE_SIZE,
        help="deterministic visual subsample size for the prefix-vs-append scatter",
    )
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    out = args.output_dir

    token_groups = load_token_groups(con, group_by=args.group_by)
    pairs = scatter_pairs(con, group_by=args.group_by, sample_size=args.pair_sample_size)
    bins_by_provider = append_bins(con, by_provider=True)
    bins = append_bins(con, by_provider=False)

    plot_token_histograms(token_groups, out, args.max_groups)
    plot_token_cdfs(token_groups, out, args.max_groups)
    plot_prefix_append_scatter(pairs, out, args.max_groups)
    plot_append_weighted_bins(bins_by_provider, out)
    write_token_summary(token_groups, out)
    write_append_weighted_bins(bins, out)

    # Final step: fuse README + CSV data + plotting code into each PNG.
    png_sidecar.make_self_contained(
        out,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=EXP_DIR / "README.md",
    )
    print(f"All outputs saved to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
