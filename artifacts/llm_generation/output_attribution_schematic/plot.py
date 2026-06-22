#!/usr/bin/env python3
"""Schematic: how a prior step's output is accounted in the *next* step.

This is a hand-built illustration (fixed, representative segment lengths), **not**
a data plot. It depicts the two KV-cache accounting cases described in the paper's
"Output token attribution" subsection:

  (a) the prior step's output is folded into the next step's *cached prefix*
      (ideal reuse -- nothing re-billed); and
  (b) the prior step's output is re-sent as part of the next step's *new, billed
      input*, so that new-input slice is longer than the output it now contains.

Each step is one horizontal stacked bar of [prefix | new input | output]. The
data-driven companion analysis lives in ``../output_append_assignment``.

See README.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

from matplotlib.colors import to_rgba  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from style import (  # noqa: E402
    AXIS_COLOR,
    MUTED_TEXT,
    TEXT_COLOR,
    plt,
    readable_text_color,
)

# Match the compact stacked-share figures: soft colormap colors, white dividers,
# and compact labels. Prefix/input use the same blue ramp as Figure 2; output
# stays orange but at a comparable saturation.
BLUES = plt.colormaps["Blues"]
ORANGES = plt.colormaps["Oranges"]
C_PREFIX = BLUES(0.82)
C_INPUT = BLUES(0.38)
C_OUTPUT = ORANGES(0.68)
EDGE = "white"

# Illustrative segment lengths (schematic "token" units): prefix, new input, output.
PRIOR = (10.0, 2.0, 4.0)
A_NEXT = (16.0, 1.0, 2.0)
B_NEXT = (12.0, 5.0, 2.0)
PRIOR_PREFIX_PLUS_INPUT = PRIOR[0] + PRIOR[1]    # 12 -> boundary before prior output
PRIOR_TOTAL = sum(PRIOR)                         # 16 -> prior prefix + new + output

BAR_H = 0.55


def _seg(ax, y, start, width, color, label, fs):
    """One stacked segment of a horizontal bar."""
    ax.barh(y, width, left=start, height=BAR_H, color=color,
            edgecolor=EDGE, linewidth=0.6, zorder=3)
    if label:
        ax.text(
            start + width / 2,
            y,
            label,
            fontsize=fs,
            fontweight="semibold",
            color=readable_text_color(to_rgba(color)),
            ha="center",
            va="center",
            zorder=4,
        )


def _bar(ax, y, parts, labels, fs):
    x = 0.0
    for width, color, label in zip(parts, (C_PREFIX, C_INPUT, C_OUTPUT), labels, strict=True):
        _seg(ax, y, x, width, color, label, fs)
        x += width
    return x


def plot(output_dir: Path, compact: bool = True) -> None:
    if compact:
        figsize, fs_title, fs_row, fs_seg, fs_leg = (3.4, 2.35), 7.6, 6.8, 6.2, 6.5
    else:
        figsize, fs_title, fs_row, fs_seg, fs_leg = (7.2, 4.4), 12.0, 10.5, 10.0, 10.0

    # Row centers (y grows upward): group (a) on top, group (b) below.
    a_prior, a_next = 4.85, 4.05
    b_prior, b_next = 2.55, 1.75

    fig, ax = plt.subplots(figsize=figsize)

    # ----- (a) Output cached as prefix -----
    ax.text(-2.25, a_prior + 0.58, "(a) Output cached as prefix",
            fontsize=fs_title, fontweight="semibold", ha="left", va="center")
    _bar(ax, a_prior, PRIOR, ("10", "2", "4"), fs_seg)
    # next prefix equals the entire prior composition: 10 + 2 + 4 = 16.
    _bar(ax, a_next, A_NEXT, ("10 + 2 + 4", "1", "2"), fs_seg)
    ax.plot([PRIOR_TOTAL, PRIOR_TOTAL], [a_next - 0.45, a_prior + 0.45],
            ls="--", color=MUTED_TEXT, lw=0.85, zorder=4)

    # ----- (b) Output re-sent as new input -----
    ax.text(-2.25, b_prior + 0.58, "(b) Output re-sent as new input",
            fontsize=fs_title, fontweight="semibold", ha="left", va="center")
    _bar(ax, b_prior, PRIOR, ("10", "2", "4"), fs_seg)
    # next prefix stops at prior prefix + input; its new-input slice is prior
    # output (4) plus one genuinely new token unit.
    _bar(ax, b_next, B_NEXT, ("10 + 2", "4+1", "2"), fs_seg)
    ax.plot([PRIOR_PREFIX_PLUS_INPUT, PRIOR_PREFIX_PLUS_INPUT], [b_next - 0.45, b_prior + 0.45],
            ls="--", color=MUTED_TEXT, lw=0.85, zorder=4)

    # ----- per-bar row labels -----
    for y in (a_prior, b_prior):
        ax.text(-0.18, y, "prior", fontsize=fs_row, color=TEXT_COLOR, ha="right", va="center")
    for y in (a_next, b_next):
        ax.text(-0.18, y, "next", fontsize=fs_row, color=TEXT_COLOR, ha="right", va="center")

    # ----- legend -----
    handles = [
        Patch(facecolor=C_PREFIX, edgecolor=AXIS_COLOR, lw=0.5, label="prefix"),
        Patch(facecolor=C_INPUT, edgecolor=AXIS_COLOR, lw=0.5, label="new input"),
        Patch(facecolor=C_OUTPUT, edgecolor=AXIS_COLOR, lw=0.5, label="output"),
    ]
    ax.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, -0.06),
              ncol=3, frameon=False, fontsize=fs_leg, handlelength=1.1,
              columnspacing=0.85, handletextpad=0.45)

    ax.set_xlim(-2.3, 19.5)
    ax.set_ylim(0.7, 5.6)
    ax.axis("off")
    fig.tight_layout()

    out_png = output_dir / "output_attribution_schematic.png"
    fig.savefig(out_png, dpi=200, bbox_inches="tight", facecolor="white")
    fig.savefig(output_dir / "output_attribution_schematic.pdf",
                bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out_png}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=EXP_DIR,
        help="directory for the rendered figure",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="render at a larger (non-compact) size instead of one LaTeX column",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    plot(args.output_dir, compact=not args.full)
    print(f"All outputs saved to {args.output_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
