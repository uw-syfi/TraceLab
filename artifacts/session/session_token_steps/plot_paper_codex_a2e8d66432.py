#!/usr/bin/env python3
"""One-off paper figure: first-30-step token accumulation for one specific codex session.

Faithfully reproduces the full `plot.py` session figure (wall-clock timeline strip, title,
metadata header, prefix/append stacked bars, total line, user-message markers, compaction
markers, full legend) for the FIRST 30 invocation steps of session
`codex:2e1c4b78-77ba-2958-db3d-630ed4246004`, but with larger fonts and a wider/shorter
aspect ratio so it drops into a two-column paper as a full-width (figure*) figure.

    uv run python artifacts/session/session_token_steps/plot_paper_codex_a2e8d66432.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
sys.path.insert(0, str(SCRIPT_DIR))

import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

import trace_db  # noqa: E402
import plot as base  # noqa: E402  (the production plot.py in this folder)

SESSION_ID = "claude:985ce830-b92e-8856-8506-c709b91a9a12"
N_STEPS = 40
DB_PATH = REPO_ROOT / "trace" / "syfi_coding_trace.duckdb"

# Pull colors/constants straight from the production module so the look stays identical.
B = base
OUT_STEM = SCRIPT_DIR / f"{base.short_session_id(SESSION_ID)}_first{N_STEPS}_paper"


def load_session():
    args = SimpleNamespace(db=DB_PATH, input=None, output_dir=None)
    con = trace_db.open_from_args(args)
    sessions = base.load_sessions_from_db(con)
    if SESSION_ID not in sessions:
        raise SystemExit(f"session not found in DB: {SESSION_ID}")
    return sessions[SESSION_ID]


def draw_timeline(ax, rounds, fs: float) -> None:
    """Wall-clock timeline strip — mirror of base.draw_timeline_axis with scaled fonts.

    Layout is spread vertically so the larger paper fonts do not collide: the blocks sit
    in the middle band, absolute elapsed-time labels go *below* them, and skipped-time gap
    labels go *above* them. The descriptive caption is drawn by the caller (fig.text).
    """
    timestamps = [item.timestamp for item in rounds]
    available = [(i, t) for i, t in enumerate(timestamps) if t]
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
    for spine in ("top", "right", "left", "bottom"):
        ax.spines[spine].set_visible(False)
    ax.set_facecolor("white")
    if not available:
        return

    start_time = min(t for _i, t in available)
    bucket_to_indices: dict[int, list[int]] = {}
    for i, t in available:
        bucket = int((t - start_time).total_seconds() // B.TIMELINE_BLOCK_SECONDS)
        bucket_to_indices.setdefault(bucket, []).append(i)

    sorted_buckets = sorted(bucket_to_indices.items())
    last_label_x = -1e9  # suppress elapsed labels that would crowd the previous one
    for ordinal, (bucket, indices) in enumerate(sorted_buckets):
        x0 = min(indices) - 0.41
        width = max(indices) - min(indices) + 0.82
        color = B.TIMELINE_BLUE if ordinal % 2 == 0 else "#bfdbfe"
        ax.add_patch(Rectangle((x0, 0.40), width, 0.26, facecolor=color,
                               edgecolor="white", linewidth=0.5, alpha=0.95))
        # Absolute elapsed-time label at the start (left edge) of the block, but only
        # when it clears the previous label (narrow back-to-back blocks would overlap).
        if x0 >= last_label_x + 2.4:
            ax.text(x0, 0.04, f"{bucket * 5}m", ha="left", va="bottom",
                    fontsize=fs, color=B.TEXT_COLOR, clip_on=False)
            last_label_x = x0

    # Skipped wall-clock between occupied blocks, above the blocks.
    for (lb, li), (rb, ri) in zip(sorted_buckets, sorted_buckets[1:]):
        missing = rb - lb - 1
        if missing <= 0:
            continue
        gap_x = (max(li) + min(ri)) / 2
        ax.text(gap_x, 0.74, f"+{missing * 5}m", ha="center", va="bottom",
                fontsize=fs, color=B.MUTED_TEXT, clip_on=False)

    # Strip label: "Time" leading all blocks, on the same horizontal line.
    ax.text(-0.008, 0.53, "Time", transform=ax.transAxes, ha="right", va="center",
            fontsize=fs, fontweight="semibold", color=B.TEXT_COLOR, clip_on=False)


def main() -> int:
    session = load_session()
    rounds = session.sorted_rounds()[:N_STEPS]

    prefix = np.asarray([r.prefix_tokens for r in rounds], dtype=float)
    append = np.asarray([r.append_tokens for r in rounds], dtype=float)
    total = prefix + append
    x = np.arange(len(rounds))
    ymax = max(float(total.max()), 1.0)

    plt.rcParams.update({
        "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.edgecolor": B.AXIS_COLOR, "font.family": "DejaVu Sans",
        "legend.frameon": False, "axes.titleweight": "semibold",
    })

    fig, (ax_tl, ax) = plt.subplots(
        2, 1, figsize=(16.5, 6.2), sharex=True,
        gridspec_kw={"height_ratios": [1.35, 7.2]},
    )

    ax.bar(x, prefix, width=0.82, color=B.PREFIX_BLUE, alpha=0.84,
           label="prefix / cache read", zorder=2)
    ax.bar(x, append, width=0.82, bottom=prefix, color=B.APPEND_ORANGE, alpha=0.9,
           label="append / new input", zorder=2)
    ax.plot(x, total, color=B.TOTAL_LINE, linewidth=2.0, alpha=0.8,
            label="total input", zorder=3)

    # User-initiated steps. Stop the dashes at the tallest bar so they clear the legend band.
    for i, r in enumerate(rounds):
        if r.is_user_input:
            ax.vlines(i, 0, ymax, color=B.USER_RED, linestyle="--", linewidth=1.5,
                      alpha=0.6, zorder=2.5)

    # Title and metadata subtitle lines intentionally omitted for this paper figure;
    # the LaTeX caption carries that context instead.
    draw_timeline(ax_tl, rounds, fs=12)

    ax.set_xlabel("Agentic steps", fontsize=20)
    ax.set_ylabel("Input tokens", fontsize=20)
    ax.set_xticks(x)
    # First row: the step number 1..N. Second row: "User" at the start of each run of
    # user-initiated steps (every such step still gets its own red dashed marker; the
    # label is only de-duplicated for back-to-back user steps so "User" never collides).
    tick_labels = []
    prev_user = False
    for i, r in enumerate(rounds, start=1):
        if r.is_user_input and not prev_user:
            tick_labels.append(f"{i}\nUser")
        else:
            tick_labels.append(str(i))
        prev_user = r.is_user_input
    ax.set_xticklabels(tick_labels, fontsize=11)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(base.format_count))
    ax.tick_params(axis="y", labelsize=17)
    ax.set_xlim(-0.8, len(rounds) - 0.2)
    ax.set_ylim(0, ymax * 1.12)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", color=B.GRID_COLOR, linewidth=1.0)
    ax.grid(True, axis="x", color=B.GRID_COLOR, linewidth=0.4, alpha=0.45)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(B.AXIS_COLOR)

    legend_handles = [
        Rectangle((0, 0), 1, 1, facecolor=B.PREFIX_BLUE, alpha=0.84, edgecolor="none",
                  label="prefix / cache read"),
        Rectangle((0, 0), 1, 1, facecolor=B.APPEND_ORANGE, alpha=0.9, edgecolor="none",
                  label="append / new input"),
        Line2D([0], [0], color=B.TOTAL_LINE, linewidth=2.0, label="total input"),
        Line2D([0], [0], color=B.USER_RED, linestyle="--", linewidth=1.5, label="user-initiated"),
    ]
    ax.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(0.0, 1.03),
              ncols=4, fontsize=14, handlelength=1.4, columnspacing=1.1, handletextpad=0.5)

    fig.subplots_adjust(top=0.965, bottom=0.13, left=0.072, right=0.992, hspace=0.12)
    for ext in ("pdf", "png"):
        out = Path(f"{OUT_STEM}.{ext}")
        fig.savefig(out, dpi=300, facecolor="white")
        print(f"Saved {out}", file=sys.stderr)
    plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
