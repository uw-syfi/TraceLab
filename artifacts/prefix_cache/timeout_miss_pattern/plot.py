#!/usr/bin/env python3
"""Schematic: the timeout-driven prefix-cache miss pattern.

This figure is *synthetic* (no trace input). It illustrates one idea with fake,
hand-shaped ``prefix``/``append`` token counts laid out exactly like the real
per-session step plot (``session/session_token_steps``): each x position is one
LLM invocation step, the light bar is the cached prefix (a cache *hit*) and the
dark bar stacked on top is the newly appended input (the *miss* portion).

The point it makes:

* **Context grows monotonically.** The conversation only accumulates — every
  step's total input (prefix + append) is at least the previous step's. An idle
  gap does **not** shrink the context; nothing is forgotten.
* **A cache hit means the whole prior context is the cached prefix.** So a
  normal tool-loop step appends only the little that is new (prev output + the
  new tool result), and its bar is almost all light with a thin dark tip.
* **A long idle gap expires the cache, not the context.** After a >5-minute
  human idle wait the KV cache is evicted. The next user round must re-prefill
  the *entire* accumulated context, so that one bar is a full-height **miss**
  (all dark) even though only a few thousand new tokens were actually added.

Contrast with the common (wrong) mental model where the bars "reset" after the
gap: here the dark timeout bar is *tall* precisely because the context kept
growing the whole time — only the cache went cold.

Run::

    uv run python artifacts/prefix_cache/timeout_miss_pattern/plot.py
    uv run python artifacts/prefix_cache/timeout_miss_pattern/plot.py -o /tmp/out
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def configure_matplotlib_cache() -> None:
    """Keep Matplotlib quiet when the launching user's config dir is read-only."""
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
import numpy as np
from matplotlib.font_manager import FontProperties, fontManager
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Circle, Rectangle

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import png_sidecar  # noqa: E402

DEFAULT_OUTPUT_DIR = SCRIPT_DIR
OUTPUT_NAME = "timeout_miss_pattern.png"
SAVE_DPI = 260

# Palette mirrors the prefix-cache slide: a light salmon hit with a shared
# dark SyFI red miss stacked on top, so a fully-dark bar instantly reads as a
# total miss.
TEXT_COLOR = "#1b1b1f"
MUTED_TEXT = "#2f2f36"
FIG_BG = "none"
HIT_FILL = "#ffd0d0"      # prefix tokens (hit) — light
MISS_FILL = "#c90000"     # append tokens (miss portion) — dark
MISS_EDGE = "#940000"
USER_MARK = "#c90000"
ARROW_GRAY = "#9ca3af"
CLOCK_EDGE = USER_MARK
CLOCK_RADIUS_PTS = 14.5
# This host does not ship Microsoft Arial; fontconfig maps Arial to Arimo, an
# Arial-compatible Croscore font. Add the font files directly so Matplotlib
# uses that face without noisy family-name fallback warnings.
ARIAL_COMPAT_REGULAR = Path("/usr/share/fonts/truetype/croscore/Arimo-Regular.ttf")
ARIAL_COMPAT_BOLD = Path("/usr/share/fonts/truetype/croscore/Arimo-Bold.ttf")
for font_path in (ARIAL_COMPAT_REGULAR, ARIAL_COMPAT_BOLD):
    if font_path.exists():
        fontManager.addfont(str(font_path))
FONT_FAMILY = ["Arimo", "Arial", "DejaVu Sans"]
BOLD_FONT = (
    FontProperties(fname=str(ARIAL_COMPAT_BOLD), weight="bold")
    if ARIAL_COMPAT_BOLD.exists()
    else FontProperties(family=FONT_FAMILY, weight="bold")
)
FONT_SCALE = 1.10


def fs(size: float) -> float:
    """Apply one shared text scale to the schematic."""
    return size * FONT_SCALE

plt.rcParams.update(
    {
        "figure.facecolor": "none",
        "axes.facecolor": "none",
        "axes.labelcolor": TEXT_COLOR,
        "axes.titlecolor": TEXT_COLOR,
        "xtick.color": MUTED_TEXT,
        "ytick.color": MUTED_TEXT,
        "text.color": TEXT_COLOR,
        "font.family": FONT_FAMILY,
        "font.size": fs(9),
        "font.weight": "bold",
        "axes.titleweight": "bold",
        "legend.frameon": False,
        "savefig.dpi": SAVE_DPI,
    }
)


@dataclass
class Step:
    """One synthetic LLM invocation step."""

    prefix: float   # cached prefix tokens (cache read / hit)
    append: float   # newly appended tokens charged at full price (miss portion)
    is_user: bool   # started from a visible user message
    timeout_miss: bool  # the whole bar is a miss (cache expired during idle)

    @property
    def total(self) -> float:
        return self.prefix + self.append


def build_steps() -> tuple[list[Step], int]:
    """Hand-shape a session whose context grows monotonically with one timeout miss.

    Cache-hit accounting (the honest model): on a hit the cached *prefix* is the
    entire previous step's input, and the *append* is only what is new since then
    (the model's prior reply plus the new tool result or user message). On a
    timeout miss the cache is gone, so prefix = 0 and the whole accumulated
    context is re-appended. Either way ``total`` only ever increases.
    """
    rng = np.random.default_rng(20250613)
    steps: list[Step] = []

    # Implicit warm base: the session's opening (system prompt + tools + first
    # exchange) is already cached before step 0, so round 0 reads as mostly-hit
    # like the rest — the dramatic full-dark bar is reserved for the timeout.
    total = 6500.0

    def hit(is_user: bool, increment: float) -> None:
        nonlocal total
        prev = total
        total = prev + increment
        steps.append(Step(prefix=prev, append=increment, is_user=is_user, timeout_miss=False))

    def timeout_miss(increment: float) -> None:
        nonlocal total
        total = total + increment
        # Cache expired during the idle gap: nothing is cached, the entire
        # (now large) context is re-prefilled as one big miss.
        steps.append(Step(prefix=0.0, append=total, is_user=True, timeout_miss=True))

    def tool_inc() -> float:
        return float(rng.uniform(1100, 2400))

    def user_inc() -> float:
        return float(rng.uniform(3000, 4200))

    # Phase 1 — user round 0, then a longer tool loop (all warm hits).
    hit(True, user_inc())
    for _ in range(17):
        hit(False, tool_inc())

    # Phase 2 — user round 1 after a SHORT (~1 min) wait: cache still valid, hit.
    hit(True, user_inc())
    for _ in range(18):
        hit(False, tool_inc())

    # Phase 3 — a >5 min idle gap expires the cache; the next user round is a
    # full re-prefill (total miss), then the tool loop warms back up.
    miss_index = len(steps)
    timeout_miss(user_inc())
    for _ in range(19):
        hit(False, tool_inc())

    return steps, miss_index


def group_bracket(ax, x0: float, x1: float, y: float, tick: float, label: str, color: str) -> None:
    """A compact red bracket, close to the reference schematic's tool-loop caps."""
    mid = (x0 + x1) / 2
    notch = max(0.08, min(0.20, (x1 - x0) * 0.025))
    ax.plot(
        [x0, x0 + notch, mid - notch, mid, mid + notch, x1 - notch, x1],
        [y - tick * 0.40, y, y, y + tick * 0.46, y, y, y - tick * 0.40],
        color=color,
        linewidth=1.0,
        clip_on=False,
        solid_capstyle="round",
    )
    ax.text(
        mid,
        y + tick * 0.85,
        label,
        ha="center",
        va="bottom",
        fontsize=fs(10.2),
        fontproperties=BOLD_FONT,
        color=color,
        clip_on=False,
    )


def clock_icon(radius_pts: float, edge: str) -> DrawingArea:
    """A small analog clock as a fixed-pixel ``DrawingArea`` so it always renders as
    a true circle (immune to the axes' token-vs-step data aspect ratio)."""
    pad = 2.5
    size = 2 * radius_pts + 2 * pad
    da = DrawingArea(size, size, 0, 0, clip=False)
    cx = cy = size / 2.0
    da.add_artist(Circle((cx, cy), radius_pts, facecolor="white", edgecolor=edge, linewidth=1.6))
    # hour ticks at 12 / 3 / 6 / 9
    for deg in (0, 90, 180, 270):
        a = math.radians(90 - deg)
        r0, r1 = radius_pts * 0.74, radius_pts * 0.95
        da.add_artist(
            Line2D(
                [cx + r0 * math.cos(a), cx + r1 * math.cos(a)],
                [cy + r0 * math.sin(a), cy + r1 * math.sin(a)],
                color=edge,
                linewidth=1.0,
                solid_capstyle="round",
            )
        )
    # generic hands (identical for both clocks → visually equal); clockwise from 12
    for deg, length, width in ((300, radius_pts * 0.46, 2.3), (54, radius_pts * 0.72, 1.5)):
        a = math.radians(90 - deg)
        da.add_artist(
            Line2D([cx, cx + length * math.cos(a)], [cy, cy + length * math.sin(a)],
                   color=edge, linewidth=width, solid_capstyle="round")
        )
    da.add_artist(Circle((cx, cy), radius_pts * 0.12, facecolor=edge, edgecolor="none"))
    return da


def place_clock(ax, x: float, y: float, radius_pts: float) -> None:
    ab = AnnotationBbox(
        clock_icon(radius_pts, CLOCK_EDGE),
        (x, y),
        xycoords="data",
        frameon=False,
        box_alignment=(0.5, 0.5),
        pad=0,
        zorder=8,
    )
    ax.add_artist(ab)


def outward_gap_arrows(ax, x: float, y: float, inner: float, outer: float) -> None:
    """Small outward arrows around an idle-clock marker."""
    ax.annotate(
        "",
        xy=(x - outer, y),
        xytext=(x - inner, y),
        arrowprops=dict(arrowstyle="-|>", color=ARROW_GRAY, lw=1.0, shrinkA=0, shrinkB=0),
        zorder=7,
        clip_on=False,
    )
    ax.annotate(
        "",
        xy=(x + outer, y),
        xytext=(x + inner, y),
        arrowprops=dict(arrowstyle="-|>", color=ARROW_GRAY, lw=1.0, shrinkA=0, shrinkB=0),
        zorder=7,
        clip_on=False,
    )


def plot(output_dir: Path) -> Path:
    steps, miss_index = build_steps()
    prefix = np.asarray([s.prefix for s in steps], dtype=float)
    append = np.asarray([s.append for s in steps], dtype=float)
    total = prefix + append
    n = len(steps)
    last = n - 1
    ymax = float(total.max())

    # Insert horizontal breaks in the step layout at the two idle gaps. The short
    # wait is intentionally smaller than the >5-minute timeout gap.
    SMALL_GAP, LARGE_GAP = 3.1, 4.8
    first_gap_at = next(i for i, s in enumerate(steps) if s.is_user and i > 0 and not s.timeout_miss)
    gaps_before = {first_gap_at: SMALL_GAP, miss_index: LARGE_GAP}
    xs = np.empty(n)
    off = 0.0
    for i in range(n):
        off += gaps_before.get(i, 0.0)
        xs[i] = i + off

    fig = plt.figure(figsize=(16.0, 2.75))
    fig.patch.set_facecolor(FIG_BG)

    fig.text(
        0.014,
        0.905,
        "TIMEOUT-DRIVEN MISS PATTERN",
        ha="left",
        va="center",
        fontsize=fs(20.5),
        fontproperties=BOLD_FONT,
        color=USER_MARK,
    )
    fig.text(
        0.375,
        0.905,
        "Each block is one LLM step (user-initiated step marked).",
        ha="left",
        va="center",
        fontsize=fs(12.0),
        fontproperties=BOLD_FONT,
        color=TEXT_COLOR,
    )

    legend_y = 0.770
    fig.add_artist(
        Rectangle(
            (0.014, legend_y - 0.026),
            0.016,
            0.052,
            transform=fig.transFigure,
            facecolor=HIT_FILL,
            edgecolor="#ffaaaa",
            linewidth=0.6,
            clip_on=False,
        )
    )
    fig.text(
        0.039,
        legend_y,
        "Prefix Tokens",
        ha="left",
        va="center",
        fontsize=fs(11.1),
        fontproperties=BOLD_FONT,
        color=TEXT_COLOR,
    )
    fig.add_artist(
        Rectangle(
            (0.142, legend_y - 0.026),
            0.016,
            0.052,
            transform=fig.transFigure,
            facecolor=MISS_FILL,
            edgecolor=MISS_EDGE,
            linewidth=0.6,
            clip_on=False,
        )
    )
    fig.text(
        0.167,
        legend_y,
        "Append Tokens",
        ha="left",
        va="center",
        fontsize=fs(11.1),
        fontproperties=BOLD_FONT,
        color=TEXT_COLOR,
    )
    fig.add_artist(
        Line2D(
            [0.296, 0.324],
            [legend_y, legend_y],
            transform=fig.transFigure,
            color=USER_MARK,
            linestyle=(0, (4, 4)),
            linewidth=1.3,
            clip_on=False,
        )
    )
    fig.text(
        0.335,
        legend_y,
        "User-Initiated Steps",
        ha="left",
        va="center",
        fontsize=fs(11.1),
        fontproperties=BOLD_FONT,
        color=TEXT_COLOR,
    )

    ax = fig.add_axes([0.018, 0.065, 0.965, 0.630])
    ax.set_facecolor(FIG_BG)

    bar_width = 0.70
    ax.bar(xs, prefix, width=bar_width, color=HIT_FILL, edgecolor=HIT_FILL, linewidth=0.2, zorder=2)
    ax.bar(
        xs,
        append,
        width=bar_width,
        bottom=prefix,
        color=MISS_FILL,
        edgecolor=MISS_EDGE,
        linewidth=0.3,
        zorder=2,
    )

    headroom = ymax * 1.56
    ax.set_ylim(0, headroom)
    ax.set_xlim(xs[0] - 1.65, xs[last] + 0.65)

    # User-message rounds: dashed red lines just before the bar they initiate.
    user_counter = 0
    for i, s in enumerate(steps):
        if not s.is_user:
            continue
        marker_x = xs[i]
        ax.vlines(
            marker_x,
            0,
            ymax * 1.10,
            color=USER_MARK,
            linestyle=(0, (5, 4)),
            linewidth=1.0,
            zorder=1,
            clip_on=False,
        )
        if user_counter < 2:
            ax.text(
                marker_x,
                ymax * 1.08,
                f"User-initiated\nstep {user_counter}",
                ha="center",
                va="bottom",
                fontsize=fs(9.5),
                color=TEXT_COLOR,
                fontproperties=BOLD_FONT,
                linespacing=1.18,
                clip_on=False,
            )
        user_counter += 1

    # --- group brackets --------------------------------------------------
    bracket_y = ymax * 1.03
    tick = ymax * 0.030
    group_bracket(ax, xs[1] - bar_width / 2, xs[first_gap_at - 1] + bar_width / 2, bracket_y, tick,
                  "Tool-initiated steps: mostly hits", USER_MARK)
    group_bracket(ax, xs[first_gap_at + 1] - bar_width / 2, xs[miss_index - 1] + bar_width / 2, bracket_y, tick,
                  "Tool-initiated steps: mostly hits", USER_MARK)
    group_bracket(ax, xs[miss_index + 1] - bar_width / 2, xs[last] + bar_width / 2, bracket_y, tick,
                  "Tool-initiated steps again: mostly hits", USER_MARK)

    # --- idle gaps: the reference figure's representation (a break + a clock) ---
    clock_y = ymax * 0.16
    short_cap_y = ymax * 0.70
    short_detail_y = ymax * 0.625
    large_cap_y = ymax * 0.56
    large_detail_y = ymax * 0.492
    # small gap — cache survives
    gx1 = (xs[first_gap_at - 1] + xs[first_gap_at]) / 2
    place_clock(ax, gx1, clock_y, CLOCK_RADIUS_PTS)
    outward_gap_arrows(ax, gx1, clock_y, inner=0.34, outer=0.62)
    ax.text(gx1, short_cap_y, "Idle gap\n~1 min", ha="center", va="bottom",
            fontsize=fs(9.8), color=USER_MARK, fontproperties=BOLD_FONT, linespacing=1.02)
    ax.text(gx1, short_detail_y, "cache still valid\n(still hits)", ha="center", va="top",
            fontsize=fs(8.8), color=TEXT_COLOR, fontproperties=BOLD_FONT, linespacing=1.10)
    # larger gap — cache expires
    gx2 = (xs[miss_index - 1] + xs[miss_index]) / 2
    place_clock(ax, gx2, clock_y, CLOCK_RADIUS_PTS)
    outward_gap_arrows(ax, gx2, clock_y, inner=0.43, outer=0.92)
    ax.text(gx2, large_cap_y, "Idle gap\n> 5min", ha="center", va="bottom",
            fontsize=fs(9.8), color=USER_MARK, fontproperties=BOLD_FONT, linespacing=1.02)
    ax.text(gx2, large_detail_y, "cache expires", ha="center", va="top",
            fontsize=fs(9.0), color=TEXT_COLOR, fontproperties=BOLD_FONT)

    # The full re-prefill bar: keep the corrected bar height, but annotate it in
    # the compact reference style.
    ax.annotate(
        "User return:\ntimeout miss\n(full re-prefill)",
        xy=(xs[miss_index], total[miss_index]),
        xytext=(xs[miss_index] + 0.65, ymax * 1.43),
        ha="left",
        va="bottom",
        fontsize=fs(10.6),
        color=USER_MARK,
        fontproperties=BOLD_FONT,
        linespacing=1.18,
        arrowprops=dict(arrowstyle="-|>", color=USER_MARK, lw=1.2, shrinkB=3),
        zorder=6,
        clip_on=False,
    )

    ax.axis("off")

    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / OUTPUT_NAME
    fig.savefig(out, dpi=SAVE_DPI, transparent=True)
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render the synthetic timeout-driven prefix-cache miss schematic.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write the figure into.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plot(args.output_dir)
    png_sidecar.make_self_contained(
        args.output_dir,
        code_files=[Path(__file__)],
        readme_path=SCRIPT_DIR / "README.md",
        png_names=[OUTPUT_NAME],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
