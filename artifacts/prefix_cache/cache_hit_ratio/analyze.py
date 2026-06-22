#!/usr/bin/env python3
"""Analyze per-round prefix cache hit ratios for normalized round traces."""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


def configure_matplotlib_cache() -> None:
    if "MPLCONFIGDIR" in os.environ:
        return

    config_home = os.environ.get("XDG_CONFIG_HOME")
    config_base = Path(config_home) if config_home else Path.home() / ".config"
    matplotlib_dir = config_base / "matplotlib"
    if matplotlib_dir.exists() and os.access(matplotlib_dir, os.W_OK):
        return
    if not matplotlib_dir.exists() and config_base.exists() and os.access(config_base, os.W_OK):
        return

    fallback_dir = Path(tempfile.gettempdir()) / "coding-trace-matplotlib"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(fallback_dir)


configure_matplotlib_cache()

import matplotlib.pyplot as plt


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

DEFAULT_OUTPUT_DIR = SCRIPT_DIR

TEXT_COLOR = "#172033"
MUTED_TEXT = "#526070"
GRID_COLOR = "#e6eaf0"
FAILURE_COLOR = "#dc2626"
MID_COLOR = "#d97706"
GOOD_COLOR = "#2563eb"

SCOPES = ("merged", "claude", "codex")
TRIGGERS = ("all", "user", "tool_result")
HIT_BIN_EDGES = [
    0.0,
    0.01,
    0.05,
    0.10,
    0.20,
    0.40,
    0.60,
    0.80,
    0.90,
    0.95,
    0.98,
    0.99,
    0.995,
    1.0000001,
]
HIT_BIN_LABELS = [
    "0",
    "(0,.01]",
    "(.01,.05]",
    "(.05,.10]",
    "(.10,.20]",
    "(.20,.40]",
    "(.40,.60]",
    "(.60,.80]",
    "(.80,.90]",
    "(.90,.95]",
    "(.95,.98]",
    "(.98,.99]",
    "(.99,.995]",
    "(.995,1]",
]
ROUND_SPLIT_BINS = [
    ("<10%", 0.0, 0.10),
    ("10-40%", 0.10, 0.40),
    ("40-80%", 0.40, 0.80),
    ("80%+", 0.80, None),
]


plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#c9d2df",
        "axes.labelcolor": TEXT_COLOR,
        "axes.titlecolor": TEXT_COLOR,
        "xtick.color": MUTED_TEXT,
        "ytick.color": MUTED_TEXT,
        "text.color": TEXT_COLOR,
        "font.family": "DejaVu Sans",
        "font.size": 9,
        "axes.titlesize": 10,
        "axes.titleweight": "semibold",
        "axes.labelsize": 9,
        "figure.titlesize": 14,
        "savefig.dpi": 220,
    }
)


@dataclass
class HitRatioGroup:
    ratios: list[float] = field(default_factory=list)
    prefix_tokens: list[int] = field(default_factory=list)
    append_tokens: list[int] = field(default_factory=list)

    def add(self, ratio: float, prefix_tokens: int, append_tokens: int) -> None:
        self.ratios.append(ratio)
        self.prefix_tokens.append(prefix_tokens)
        self.append_tokens.append(append_tokens)

    @property
    def count(self) -> int:
        return len(self.ratios)

    @property
    def total_append_tokens(self) -> int:
        return sum(self.append_tokens)

    @property
    def total_prefix_tokens(self) -> int:
        return sum(self.prefix_tokens)

    @property
    def total_input_tokens(self) -> int:
        return self.total_prefix_tokens + self.total_append_tokens

    @property
    def token_weighted_hit_rate(self) -> float | None:
        total = self.total_input_tokens
        if total == 0:
            return None
        return self.total_prefix_tokens / total


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower_index = math.floor(index)
    upper_index = math.ceil(index)
    if lower_index == upper_index:
        return ordered[lower_index]
    lower_weight = upper_index - index
    upper_weight = index - lower_index
    return (
        ordered[lower_index] * lower_weight
        + ordered[upper_index] * upper_weight
    )


def hit_bin_index(ratio: float) -> int:
    if ratio == 0:
        return 0
    for index in range(1, len(HIT_BIN_EDGES)):
        if ratio <= HIT_BIN_EDGES[index]:
            return index
    return len(HIT_BIN_LABELS) - 1


def bin_color(index: int) -> str:
    upper = HIT_BIN_EDGES[index] if index > 0 else 0.0
    if upper < 0.5:
        return FAILURE_COLOR
    if upper < 0.9:
        return MID_COLOR
    return GOOD_COLOR


def read_groups(con) -> dict[tuple[str, str], HitRatioGroup]:
    """Per-(scope, trigger) hit ratios + append tokens, exact over all qualifying rounds.

    Mirrors the old JSONL loader exactly:
      * A round qualifies only when its FIRST timing event (``event_index = 1``) is a
        ``user_message`` or ``tool_result`` (recovers ``timing_events[0].event_type``).
      * ``prefix_tokens`` and ``newly_append_tokens`` must both be non-null (the old
        ``int_field`` rejected None; in the DB these are BIGINT, so non-null suffices).
      * The round's total input (``prefix + append``) must be > 0.
      * ``hit_ratio = prefix_tokens / (prefix_tokens + newly_append_tokens)``.
      * Provider is ``provider`` (or ``"unknown"`` when null); trigger is ``user`` for a
        ``user_message`` first event, else ``tool_result``. Each round feeds both its
        provider scope and the ``merged`` scope, under its trigger and the ``all`` trigger.
    Ordered by ``round_pk`` (file order) for deterministic accumulation.
    """
    rows = con.execute(
        """
        WITH first_ev AS (
            SELECT round_pk, event_type
            FROM timing_events
            WHERE event_index = 1
        )
        SELECT r.provider AS provider,
               f.event_type AS event_type,
               r.prefix_tokens AS prefix_tokens,
               r.newly_append_tokens AS append_tokens
        FROM rounds r JOIN first_ev f USING (round_pk)
        WHERE f.event_type IN ('user_message', 'tool_result')
          AND r.prefix_tokens IS NOT NULL
          AND r.newly_append_tokens IS NOT NULL
          AND (r.prefix_tokens + r.newly_append_tokens) > 0
        ORDER BY r.round_pk
        """
    ).fetchall()

    groups: dict[tuple[str, str], HitRatioGroup] = defaultdict(HitRatioGroup)
    for provider, event_type, prefix_tokens, append_tokens in rows:
        provider = provider if isinstance(provider, str) else "unknown"
        prefix_tokens = int(prefix_tokens)
        append_tokens = int(append_tokens)
        total_input_tokens = prefix_tokens + append_tokens
        trigger = "user" if event_type == "user_message" else "tool_result"
        hit_ratio = prefix_tokens / total_input_tokens
        for scope in ("merged", provider):
            groups[(scope, trigger)].add(hit_ratio, prefix_tokens, append_tokens)
            groups[(scope, "all")].add(hit_ratio, prefix_tokens, append_tokens)
    return dict(groups)


def read_token_weighted_groups(con) -> dict[tuple[str, str], HitRatioGroup]:
    """Token-weighted hit-rate groups for the paper table.

    The overall (`all`) row intentionally includes every valid input-token row,
    while the trigger rows are restricted to first-event `user_message` and
    `tool_result` steps. This matches the paper summary macros and keeps the
    histogram-oriented `read_groups` eligibility unchanged.
    """
    rows = con.execute(
        """
        WITH first_ev AS (
            SELECT round_pk, event_type
            FROM timing_events
            WHERE event_index = 1
        )
        SELECT r.provider AS provider,
               f.event_type AS event_type,
               r.prefix_tokens AS prefix_tokens,
               r.newly_append_tokens AS append_tokens
        FROM rounds r LEFT JOIN first_ev f USING (round_pk)
        WHERE r.prefix_tokens IS NOT NULL
          AND r.newly_append_tokens IS NOT NULL
          AND (r.prefix_tokens + r.newly_append_tokens) > 0
        ORDER BY r.round_pk
        """
    ).fetchall()

    groups: dict[tuple[str, str], HitRatioGroup] = defaultdict(HitRatioGroup)
    for provider, event_type, prefix_tokens, append_tokens in rows:
        provider = provider if isinstance(provider, str) else "unknown"
        prefix_tokens = int(prefix_tokens)
        append_tokens = int(append_tokens)
        total_input_tokens = prefix_tokens + append_tokens
        hit_ratio = prefix_tokens / total_input_tokens
        trigger = None
        if event_type == "user_message":
            trigger = "user"
        elif event_type == "tool_result":
            trigger = "tool_result"
        for scope in ("merged", provider):
            groups[(scope, "all")].add(hit_ratio, prefix_tokens, append_tokens)
            if trigger is not None:
                groups[(scope, trigger)].add(hit_ratio, prefix_tokens, append_tokens)
    return dict(groups)


def format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.2f}%"


def format_pct_one_latex(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value * 100:.1f}\\%"


def write_token_weighted_csv(
    path: Path,
    groups: dict[tuple[str, str], HitRatioGroup],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "scope",
        "trigger",
        "rounds",
        "prefix_tokens",
        "append_tokens",
        "total_input_tokens",
        "token_weighted_hit_rate",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            for trigger in TRIGGERS:
                group = groups.get((scope, trigger), HitRatioGroup())
                writer.writerow(
                    {
                        "scope": scope,
                        "trigger": trigger,
                        "rounds": group.count,
                        "prefix_tokens": group.total_prefix_tokens,
                        "append_tokens": group.total_append_tokens,
                        "total_input_tokens": group.total_input_tokens,
                        "token_weighted_hit_rate": (
                            group.token_weighted_hit_rate
                            if group.token_weighted_hit_rate is not None
                            else ""
                        ),
                    }
                )


def write_latex_hit_rate_table(
    path: Path,
    groups: dict[tuple[str, str], HitRatioGroup],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("Prefix cache-hit rate", "all"),
        ("Prefix hit rate (user-initiated)", "user"),
        ("Prefix hit rate (tool-result)", "tool_result"),
    ]
    scopes = ["claude", "codex", "merged"]
    lines = [
        "% ==========================================================================",
        "% Data source: TraceLab/artifacts/prefix_cache/cache_hit_ratio/analyze.py",
        "%   (token-weighted prefix-cache hit rates; auto-generates this table).",
        "% ==========================================================================",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Token-weighted prefix cache hit rate by provider and step trigger.}",
        "\\label{tab:prefix_cache_hit_rate}",
        "\\small",
        "\\setlength{\\tabcolsep}{6pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Claude} & \\textbf{Codex} & \\textbf{Total} \\\\",
        "\\midrule",
    ]
    for label, trigger in rows:
        values = [
            format_pct_one_latex(
                groups.get((scope, trigger), HitRatioGroup()).token_weighted_hit_rate
            )
            for scope in scopes
        ]
        lines.append(f"{label} & {values[0]} & {values[1]} & {values[2]} \\\\")
    lines.extend(
        [
            "\\bottomrule",
            "\\end{tabular}",
            "\\end{table}",
            "",
        ]
    )
    path.write_text("\n".join(lines))


def write_summary_csv(path: Path, groups: dict[tuple[str, str], HitRatioGroup]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "scope",
        "trigger",
        "rounds",
        "append_tokens",
        "mean_hit_ratio",
        "p01",
        "p05",
        "p10",
        "p25",
        "median",
        "p75",
        "p90",
        "p95",
        "p99",
        "round_hit_lt_0_5_pct",
        "round_hit_0_5_to_0_9_pct",
        "round_hit_gte_0_9_pct",
        "round_hit_gte_0_95_pct",
        "round_hit_gte_0_98_pct",
        "round_hit_gte_0_99_pct",
        "append_hit_lt_0_5_pct",
        "append_hit_0_5_to_0_9_pct",
        "append_hit_gte_0_9_pct",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            for trigger in TRIGGERS:
                group = groups.get((scope, trigger), HitRatioGroup())
                ratios = group.ratios
                append_tokens = group.append_tokens
                count = len(ratios)
                total_append = sum(append_tokens)
                append_low = sum(
                    append for ratio, append in zip(ratios, append_tokens) if ratio < 0.5
                )
                append_mid = sum(
                    append
                    for ratio, append in zip(ratios, append_tokens)
                    if 0.5 <= ratio < 0.9
                )
                append_high = sum(
                    append for ratio, append in zip(ratios, append_tokens) if ratio >= 0.9
                )
                writer.writerow(
                    {
                        "scope": scope,
                        "trigger": trigger,
                        "rounds": count,
                        "append_tokens": total_append,
                        "mean_hit_ratio": sum(ratios) / count if count else "",
                        "p01": percentile(ratios, 0.01),
                        "p05": percentile(ratios, 0.05),
                        "p10": percentile(ratios, 0.10),
                        "p25": percentile(ratios, 0.25),
                        "median": percentile(ratios, 0.50),
                        "p75": percentile(ratios, 0.75),
                        "p90": percentile(ratios, 0.90),
                        "p95": percentile(ratios, 0.95),
                        "p99": percentile(ratios, 0.99),
                        "round_hit_lt_0_5_pct": format_pct(
                            sum(ratio < 0.5 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "round_hit_0_5_to_0_9_pct": format_pct(
                            sum(0.5 <= ratio < 0.9 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "round_hit_gte_0_9_pct": format_pct(
                            sum(ratio >= 0.9 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "round_hit_gte_0_95_pct": format_pct(
                            sum(ratio >= 0.95 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "round_hit_gte_0_98_pct": format_pct(
                            sum(ratio >= 0.98 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "round_hit_gte_0_99_pct": format_pct(
                            sum(ratio >= 0.99 for ratio in ratios) / count
                            if count
                            else None
                        ),
                        "append_hit_lt_0_5_pct": format_pct(
                            append_low / total_append if total_append else None
                        ),
                        "append_hit_0_5_to_0_9_pct": format_pct(
                            append_mid / total_append if total_append else None
                        ),
                        "append_hit_gte_0_9_pct": format_pct(
                            append_high / total_append if total_append else None
                        ),
                    }
                )


def write_bins_csv(path: Path, groups: dict[tuple[str, str], HitRatioGroup]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "scope",
        "trigger",
        "bin",
        "rounds",
        "round_share",
        "append_tokens",
        "append_token_share",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            for trigger in TRIGGERS:
                group = groups.get((scope, trigger), HitRatioGroup())
                round_counts = [0] * len(HIT_BIN_LABELS)
                append_counts = [0] * len(HIT_BIN_LABELS)
                for ratio, append_tokens in zip(group.ratios, group.append_tokens):
                    index = hit_bin_index(ratio)
                    round_counts[index] += 1
                    append_counts[index] += append_tokens
                total_rounds = sum(round_counts)
                total_append = sum(append_counts)
                for label, round_count, append_count in zip(
                    HIT_BIN_LABELS, round_counts, append_counts
                ):
                    writer.writerow(
                        {
                            "scope": scope,
                            "trigger": trigger,
                            "bin": label,
                            "rounds": round_count,
                            "round_share": format_pct(
                                round_count / total_rounds if total_rounds else None
                            ),
                            "append_tokens": append_count,
                            "append_token_share": format_pct(
                                append_count / total_append if total_append else None
                            ),
                        }
                    )


def write_round_split_csv(
    path: Path,
    groups: dict[tuple[str, str], HitRatioGroup],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["scope", "trigger", "hit_ratio_bucket", "rounds", "round_share"]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            for trigger in TRIGGERS:
                group = groups.get((scope, trigger), HitRatioGroup())
                total_rounds = len(group.ratios)
                for label, lower, upper in ROUND_SPLIT_BINS:
                    if upper is None:
                        count = sum(ratio >= lower for ratio in group.ratios)
                    else:
                        count = sum(lower <= ratio < upper for ratio in group.ratios)
                    writer.writerow(
                        {
                            "scope": scope,
                            "trigger": trigger,
                            "hit_ratio_bucket": label,
                            "rounds": count,
                            "round_share": format_pct(
                                count / total_rounds if total_rounds else None
                            ),
                        }
                    )


def plot_histograms(
    path: Path,
    groups: dict[tuple[str, str], HitRatioGroup],
    *,
    weighted_by_append: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(
        len(SCOPES),
        len(TRIGGERS),
        figsize=(17, 10),
        sharex=True,
        sharey=True,
        constrained_layout=True,
    )
    colors = [bin_color(index) for index in range(len(HIT_BIN_LABELS))]
    for row_index, scope in enumerate(SCOPES):
        for col_index, trigger in enumerate(TRIGGERS):
            ax = axes[row_index][col_index]
            group = groups.get((scope, trigger), HitRatioGroup())
            bin_values = [0.0] * len(HIT_BIN_LABELS)
            for ratio, append_tokens in zip(group.ratios, group.append_tokens):
                value = append_tokens if weighted_by_append else 1
                bin_values[hit_bin_index(ratio)] += value
            total = sum(bin_values)
            shares = [value / total * 100 if total else 0 for value in bin_values]
            ax.bar(range(len(HIT_BIN_LABELS)), shares, color=colors, width=0.82)
            ax.grid(axis="y", color=GRID_COLOR, linewidth=0.8)
            ax.set_title(f"{scope} / {trigger}")
            if col_index == 0:
                ax.set_ylabel("% append tokens" if weighted_by_append else "% rounds")
            if row_index == len(SCOPES) - 1:
                ax.set_xticks(range(len(HIT_BIN_LABELS)))
                ax.set_xticklabels(HIT_BIN_LABELS, rotation=55, ha="right")
            else:
                ax.tick_params(labelbottom=False)
            ax.set_ylim(0, 100)
    title = (
        "Prefix hit-ratio histogram, append-token weighted"
        if weighted_by_append
        else "Prefix hit-ratio histogram, round weighted"
    )
    fig.suptitle(title)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-plots", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    con = trace_db.open_from_args(args)
    groups = read_groups(con)
    token_weighted_groups = read_token_weighted_groups(con)
    output_dir = args.output_dir
    summary_csv = output_dir / "cache_hit_ratio_summary.csv"
    bins_csv = output_dir / "cache_hit_ratio_bins.csv"
    round_split_csv = output_dir / "cache_hit_ratio_round_split.csv"
    token_weighted_csv = output_dir / "cache_hit_ratio_token_weighted.csv"
    latex_table = output_dir / "prefix_cache_hit_rate_table.tex"
    write_summary_csv(summary_csv, groups)
    write_bins_csv(bins_csv, groups)
    write_round_split_csv(round_split_csv, groups)
    write_token_weighted_csv(token_weighted_csv, token_weighted_groups)
    write_latex_hit_rate_table(latex_table, token_weighted_groups)
    print(f"summary_csv={summary_csv}")
    print(f"bins_csv={bins_csv}")
    print(f"round_split_csv={round_split_csv}")
    print(f"token_weighted_csv={token_weighted_csv}")
    print(f"latex_table={latex_table}")
    if not args.no_plots:
        round_plot = output_dir / "cache_hit_ratio_histogram.png"
        append_plot = output_dir / "cache_hit_ratio_append_weighted_histogram.png"
        plot_histograms(round_plot, groups, weighted_by_append=False)
        plot_histograms(append_plot, groups, weighted_by_append=True)
        print(f"round_histogram={round_plot}")
        print(f"append_weighted_histogram={append_plot}")
    total_rounds = groups.get(("merged", "all"), HitRatioGroup()).count
    print(f"rounds={total_rounds}")
    png_sidecar.make_self_contained(
        output_dir,
        code_files=[Path(__file__), *png_sidecar.util_code_files()],
        readme_path=SCRIPT_DIR / "README.md",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
