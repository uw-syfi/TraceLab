#!/usr/bin/env python3
"""Scatter plot of round wait time against prefix-cache hit rate.

Reads the shared trace DuckDB (via ``cache_hit_idle_gap_analysis.load_rounds_by_session``) instead
of re-parsing the normalized JSONL. The per-session walk that turns rounds into scatter points is
unchanged; only the input mechanism (JSONL -> DB) changed. Every plotted point — its value and its
order within a provider — matches the pre-DuckDB result, so the scatter renders pixel-identically.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import cache_hit_idle_gap_analysis as idle  # noqa: E402
import trace_db  # noqa: E402
import png_sidecar  # noqa: E402

TRIGGERS = {
    "user": {
        "event_type": "user_message",
        "title": "User rounds",
        "x_label": "Wait time since previous activity",
        "x_measure": "wait time",
        "output": "user_wait_time_vs_hit_rate_scatter.png",
        "paper_output": "user_wait_time_vs_hit_rate_scatter_paper.pdf",
    },
    "tool_result": {
        "event_type": "tool_result",
        "title": "Tool-result rounds",
        "x_label": "Tool duration (result_at - emitted_at)",
        "x_measure": "tool duration",
        "output": "tool_result_wait_time_vs_hit_rate_scatter.png",
        "paper_output": "tool_result_wait_time_vs_hit_rate_scatter_paper.pdf",
    },
}


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
from matplotlib.ticker import FuncFormatter, FixedLocator


PROVIDER_COLORS = {
    "claude": "#2563eb",
    "codex": "#d97706",
}
DEFAULT_PROVIDERS = ("claude", "codex")
MIN_DISPLAY_WAIT_SECONDS = 0.001


def default_output_path(trigger: str, output_dir: Path = SCRIPT_DIR) -> Path:
    return output_dir / TRIGGERS[trigger]["output"]


def default_paper_output_path(trigger: str, output_dir: Path = SCRIPT_DIR) -> Path:
    return output_dir / TRIGGERS[trigger]["paper_output"]


def collect_points(
    con,
    *,
    trigger: str,
) -> dict[str, tuple[list[float], list[float]]]:
    """Build ``{provider: ([wait_seconds...], [hit_rate...])}`` for one trigger.

    Reproduces the old single-pass session walk exactly, but over ``RoundData`` from the trace DB:
    points are emitted in file order within each session, and sessions are visited in first-
    appearance order, so each provider's point list (value and order) matches the pre-DuckDB result.
    """
    wait_seconds_by_provider: dict[str, list[float]] = defaultdict(list)
    hit_rate_by_provider: dict[str, list[float]] = defaultdict(list)
    event_type = TRIGGERS[trigger]["event_type"]

    rounds_by_session = idle.load_rounds_by_session(con)
    for (provider, _session_id), rounds in rounds_by_session.items():
        # Session-scoped accumulator: call id -> tool duration seconds (or None), remembered only
        # after a round is processed, so tool_result rounds look up durations emitted by *previous*
        # rounds — identical to the old tools_by_call_id walk.
        durations_by_call_id: dict[str, float | None] = {}
        for row_offset, current in enumerate(rounds):
            if current.first_event_type != event_type:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue

            prefix_tokens = current.prefix_tokens
            append_tokens = current.append_tokens
            if prefix_tokens is None or append_tokens is None:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue
            total_tokens = prefix_tokens + append_tokens
            if total_tokens <= 0:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue

            if trigger == "user":
                if row_offset == 0:
                    durations_by_call_id.update(current.emitted_tool_durations)
                    continue
                previous_last = rounds[row_offset - 1].last_activity_us
                current_first = current.first_activity_us
                if previous_last is None or current_first is None:
                    durations_by_call_id.update(current.emitted_tool_durations)
                    continue
                wait_seconds = (current_first - previous_last) / 1e6
                if wait_seconds < 0:
                    durations_by_call_id.update(current.emitted_tool_durations)
                    continue
            else:
                durations = [
                    duration
                    for tool_call_id in current.leading_tool_result_call_ids
                    if (duration := durations_by_call_id.get(tool_call_id)) is not None
                ]
                if not durations:
                    durations_by_call_id.update(current.emitted_tool_durations)
                    continue
                wait_seconds = max(durations)

            if wait_seconds < 0:
                durations_by_call_id.update(current.emitted_tool_durations)
                continue

            wait_seconds_by_provider[provider].append(wait_seconds)
            hit_rate_by_provider[provider].append(prefix_tokens / total_tokens)
            durations_by_call_id.update(current.emitted_tool_durations)

    return {
        provider: (wait_seconds_by_provider[provider], hit_rate_by_provider[provider])
        for provider in sorted(wait_seconds_by_provider)
    }


WAIT_TICKS = [
    (MIN_DISPLAY_WAIT_SECONDS, "0s"),
    (0.01, "10ms"),
    (0.1, "100ms"),
    (1, "1s"),
    (10, "10s"),
    (60, "1m"),
    (300, "5m"),
    (1800, "30m"),
    (3600, "1h"),
    (21600, "6h"),
    (86400, "1d"),
    (604800, "7d"),
    (1209600, "14d"),
]


def format_wait_time(value: float, _position: int) -> str:
    for tick, label in WAIT_TICKS:
        if abs(value - tick) < 1e-6:
            return label
    return ""


def plot(
    points: dict[str, tuple[list[float], list[float]]],
    output_path: Path,
    *,
    title: str,
    x_label: str,
    x_measure: str,
    trigger: str,
    paper: bool = False,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    providers = list(DEFAULT_PROVIDERS)
    providers.extend(provider for provider in sorted(points) if provider not in providers)
    total_points = sum(len(points.get(provider, ([], []))[0]) for provider in providers)
    max_wait_seconds = max(
        (max(wait_seconds) for wait_seconds, _hit_rates in points.values() if wait_seconds),
        default=300,
    )
    max_display_wait = max(max_wait_seconds * 1.15, 600)
    if paper:
        figsize = (4.2, 4.4)  # near-square; two of these sit side-by-side across \textwidth
        provider_title_size = 12
        label_size = 11
        tick_size = 9.5
        annotation_size = 9.5
    else:
        figsize = (12.0, 7.2)
        provider_title_size = 11
        label_size = None
        tick_size = None
        annotation_size = 9

    fig, axes = plt.subplots(
        len(providers),
        1,
        figsize=figsize,
        sharex=True,
        sharey=True,
    )
    if len(providers) == 1:
        axes = [axes]

    visible_ticks = [tick for tick, _label in WAIT_TICKS if tick <= max_wait_seconds * 1.05]
    for index, (ax, provider) in enumerate(zip(axes, providers)):
        wait_seconds, hit_rates = points.get(provider, ([], []))
        point_count = len(wait_seconds)
        if trigger == "tool_result":
            point_size = 6 if paper else 8
            point_alpha = 0.10 if paper else 0.085
        else:
            if paper:
                point_size = 4.2 if point_count < 50_000 else 2.6
                point_alpha = 0.16 if point_count < 50_000 else 0.05
            else:
                point_size = 5 if point_count < 50_000 else 2.2
                point_alpha = 0.13 if point_count < 50_000 else 0.035
        ax.axvspan(
            300,
            max_display_wait,
            color="#f8fafc",
            alpha=0.9,
            zorder=0,
        )
        ax.axhspan(
            0,
            10,
            color="#fff1f2",
            alpha=0.45,
            zorder=0,
        )
        if wait_seconds:
            ax.scatter(
                [max(wait, MIN_DISPLAY_WAIT_SECONDS) for wait in wait_seconds],
                [hit_rate * 100.0 for hit_rate in hit_rates],
                s=point_size,
                alpha=point_alpha,
                linewidths=0,
                rasterized=True,
                color=PROVIDER_COLORS.get(provider, "#64748b"),
                zorder=2,
            )
        ax.axhline(10, color="#dc2626", linewidth=1.1, linestyle="--", zorder=3)
        ax.axvline(300, color="#334155", linewidth=1.0, linestyle=":", zorder=3)
        ax.axvline(3600, color="#64748b", linewidth=1.0, linestyle="-.", zorder=3)
        ax.set_xscale("log")
        ax.set_xlim(MIN_DISPLAY_WAIT_SECONDS * 0.85, max_display_wait)
        ax.set_ylim(-1, 101)
        ax.set_ylabel("Hit rate", fontsize=label_size)
        ax.set_title(
            f"{provider} ({len(wait_seconds):,})",
            loc="left",
            fontsize=provider_title_size,
            pad=4,
        )
        if tick_size is not None:
            ax.tick_params(axis="both", which="major", labelsize=tick_size)
        ax.xaxis.set_major_locator(FixedLocator(visible_ticks))
        ax.xaxis.set_major_formatter(FuncFormatter(format_wait_time))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:g}%"))
        ax.grid(True, which="major", color="#e6eaf0", linewidth=0.8)
        ax.grid(True, which="minor", axis="x", color="#eef2f7", linewidth=0.45, alpha=0.65)
        if index == 0:
            ax.text(
                315,
                96,
                "> 5m wait",
                ha="left",
                va="top",
                color="#334155",
                fontsize=annotation_size,
            )
            ax.text(
                3720,
                88,
                "1h wait",
                ha="left",
                va="top",
                color="#64748b",
                fontsize=annotation_size,
            )
            ax.text(
                MIN_DISPLAY_WAIT_SECONDS * 1.2,
                8.0,
                "low hit",
                ha="left",
                va="top",
                color="#be123c",
                fontsize=annotation_size,
            )

    paper_x_label = "Human idle time" if trigger == "user" else "Tool duration"
    axes[-1].set_xlabel(paper_x_label if paper else x_label, fontsize=label_size)
    if not paper:
        fig.suptitle(
            f"{title}: {x_measure} vs cache hit rate ({total_points:,} measurable rounds)",
            y=0.99,
            fontsize=15,
        )
        fig.tight_layout(rect=(0, 0, 1, 0.965))
    else:
        fig.tight_layout(pad=0.25)
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


COMBINED_PAPER_OUTPUT = "cache_hit_idle_combined_paper.pdf"
COMBINED_TRIGGERS = ("user", "tool_result")  # left column, right column
COMBINED_X_LABELS = {"user": "Human idle time", "tool_result": "Tool duration"}
COMBINED_OMIT_TICKS = {1800, 1209600}  # drop the 30m and 14d ticks to de-clutter


def plot_combined(
    points_by_trigger: dict[str, dict[str, tuple[list[float], list[float]]]],
    output_path: Path,
) -> None:
    """One paper PDF holding both triggers as columns and both providers as rows.

    The whole grid shares a single y-axis, so only the left column carries the "Hit rate"
    label and tick labels -- this recovers the horizontal space that two stand-alone panels
    waste on duplicate y-axes. Each column keeps its own log x-axis (idle time vs. tool
    duration) via ``sharex='col'``.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    providers = list(DEFAULT_PROVIDERS)
    extra = sorted({p for pts in points_by_trigger.values() for p in pts})
    providers.extend(p for p in extra if p not in providers)
    triggers = list(COMBINED_TRIGGERS)

    label_size, tick_size, title_size, annotation_size = 12, 10, 12, 10
    fig, axes = plt.subplots(
        len(providers),
        len(triggers),
        figsize=(10.0, 3.5),
        sharex="col",
        sharey=True,
        squeeze=False,
    )

    col_max_wait, col_max_display = {}, {}
    for col, trigger in enumerate(triggers):
        pts = points_by_trigger[trigger]
        mw = max((max(ws) for ws, _hr in pts.values() if ws), default=300)
        col_max_wait[col] = mw
        col_max_display[col] = max(mw * 1.15, 600)

    for row, provider in enumerate(providers):
        for col, trigger in enumerate(triggers):
            ax = axes[row][col]
            wait_seconds, hit_rates = points_by_trigger[trigger].get(provider, ([], []))
            point_count = len(wait_seconds)
            if trigger == "tool_result":
                point_size, point_alpha = 6, 0.10
            else:
                point_size = 4.2 if point_count < 50_000 else 2.6
                point_alpha = 0.16 if point_count < 50_000 else 0.05
            max_display = col_max_display[col]
            ax.axvspan(300, max_display, color="#f8fafc", alpha=0.9, zorder=0)
            ax.axhspan(0, 10, color="#fff1f2", alpha=0.45, zorder=0)
            if wait_seconds:
                ax.scatter(
                    [max(wait, MIN_DISPLAY_WAIT_SECONDS) for wait in wait_seconds],
                    [hit_rate * 100.0 for hit_rate in hit_rates],
                    s=point_size,
                    alpha=point_alpha,
                    linewidths=0,
                    rasterized=True,
                    color=PROVIDER_COLORS.get(provider, "#64748b"),
                    zorder=2,
                )
            ax.axhline(10, color="#dc2626", linewidth=1.1, linestyle="--", zorder=3)
            ax.axvline(300, color="#334155", linewidth=1.0, linestyle=":", zorder=3)
            ax.axvline(3600, color="#64748b", linewidth=1.0, linestyle="-.", zorder=3)
            ax.set_xscale("log")
            ax.set_xlim(MIN_DISPLAY_WAIT_SECONDS * 0.85, max_display)
            ax.set_ylim(-1, 101)
            ax.set_title(f"{provider} ({point_count:,})", loc="left", fontsize=title_size, pad=4)
            ax.tick_params(axis="both", which="major", labelsize=tick_size)
            visible_ticks = [
                t for t, _label in WAIT_TICKS
                if t <= col_max_wait[col] * 1.05 and t not in COMBINED_OMIT_TICKS
            ]
            ax.xaxis.set_major_locator(FixedLocator(visible_ticks))
            ax.xaxis.set_major_formatter(FuncFormatter(format_wait_time))
            ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _pos: f"{value:g}%"))
            ax.grid(True, which="major", color="#e6eaf0", linewidth=0.8)
            ax.grid(True, which="minor", axis="x", color="#eef2f7", linewidth=0.45, alpha=0.65)
            if col == 0:
                ax.set_ylabel("Hit rate", fontsize=label_size)
            if row == 0:
                ax.text(315, 96, "> 5m wait", ha="left", va="top", color="#334155", fontsize=annotation_size)
                ax.text(3720, 88, "1h wait", ha="left", va="top", color="#64748b", fontsize=annotation_size)
                ax.text(MIN_DISPLAY_WAIT_SECONDS * 1.2, 8.0, "low hit", ha="left", va="top", color="#be123c", fontsize=annotation_size)

    for col, trigger in enumerate(triggers):
        axes[-1][col].set_xlabel(COMBINED_X_LABELS[trigger], fontsize=label_size)

    fig.tight_layout(pad=0.3)
    fig.subplots_adjust(wspace=0.13)  # a bit of breathing room between the two columns
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def selected_triggers(trigger: str) -> list[str]:
    if trigger == "all":
        return list(TRIGGERS)
    return [trigger]


def count_points(points: dict[str, tuple[list[float], list[float]]]) -> dict[str, int]:
    providers = list(DEFAULT_PROVIDERS)
    providers.extend(provider for provider in sorted(points) if provider not in providers)
    return {
        provider: len(points.get(provider, ([], []))[0])
        for provider in providers
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    # --db | -i/--input (materialized if --db absent) + -o/--output-dir for the default PNG names.
    trace_db.add_db_args(parser, default_output_dir=SCRIPT_DIR)
    parser.add_argument("--trigger", choices=["all", *sorted(TRIGGERS)], default="all")
    parser.add_argument(
        "--output",
        type=Path,
        help="Single scatter path; only valid with one concrete --trigger.",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Write title-free PDF panels with larger fonts for the paper.",
    )
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Write one paper PDF with both triggers as columns sharing a single y-axis.",
    )
    args = parser.parse_args()

    triggers = selected_triggers(args.trigger)
    if args.output is not None and len(triggers) != 1:
        parser.error("--output can only be used with one concrete --trigger")

    con = trace_db.open_from_args(args)

    if args.combined:
        points_by_trigger = {
            trigger: collect_points(con, trigger=trigger) for trigger in COMBINED_TRIGGERS
        }
        output_path = args.output or (args.output_dir / COMBINED_PAPER_OUTPUT)
        plot_combined(points_by_trigger, output_path)
        png_sidecar.make_self_contained(
            args.output_dir if args.output is None else args.output.parent,
            code_files=[Path(__file__), Path(idle.__file__)],
            readme_path=SCRIPT_DIR / "README.md",
        )
        print(f"Wrote {output_path}")
        for trigger in COMBINED_TRIGGERS:
            print(
                f"{TRIGGERS[trigger]['title']} with measurable wait time: "
                f"{count_points(points_by_trigger[trigger])}"
            )
        return

    all_counts: dict[str, dict[str, int]] = {}
    output_paths: list[Path] = []
    for trigger in triggers:
        if args.output is not None:
            output_path = args.output
        elif args.paper:
            output_path = default_paper_output_path(trigger, args.output_dir)
        else:
            output_path = default_output_path(trigger, args.output_dir)
        points = collect_points(con, trigger=trigger)
        plot(
            points,
            output_path,
            title=TRIGGERS[trigger]["title"],
            x_label=TRIGGERS[trigger]["x_label"],
            x_measure=TRIGGERS[trigger]["x_measure"],
            trigger=trigger,
            paper=args.paper,
        )
        all_counts[trigger] = count_points(points)
        output_paths.append(output_path)

    png_sidecar.make_self_contained(
        args.output_dir if args.output is None else args.output.parent,
        code_files=[Path(__file__), Path(idle.__file__)],
        readme_path=SCRIPT_DIR / "README.md",
    )
    for output_path in output_paths:
        print(f"Wrote {output_path}")
    for trigger in triggers:
        print(
            f"{TRIGGERS[trigger]['title']} with measurable wait time: "
            f"{all_counts[trigger]}"
        )


if __name__ == "__main__":
    main()
