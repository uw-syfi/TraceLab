"""Matplotlib style, color palette, and shared plotting primitives."""

from __future__ import annotations

from typing import Any
from pathlib import Path
import os
import sys
import tempfile


def configure_matplotlib_cache() -> None:
    """Keep Matplotlib quiet when the launching user's config dir is read-only."""
    if "MPLCONFIGDIR" in os.environ:
        return

    config_home = os.environ.get("XDG_CONFIG_HOME")
    config_base = Path(config_home) if config_home else Path.home() / ".config"
    matplotlib_dir = config_base / "matplotlib"

    if matplotlib_dir.exists() and os.access(matplotlib_dir, os.W_OK):
        return
    if (
        not matplotlib_dir.exists()
        and config_base.exists()
        and os.access(config_base, os.W_OK)
    ):
        return

    fallback_dir = Path(tempfile.gettempdir()) / "coding-trace-matplotlib"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MPLCONFIGDIR"] = str(fallback_dir)


configure_matplotlib_cache()

import matplotlib.patches as mpatches  # noqa: E402,F401
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker as mticker  # noqa: E402,F401


SAVE_DPI = 260
TEXT_COLOR = "#172033"
MUTED_TEXT = "#526070"
GRID_COLOR = "#e6eaf0"
AXIS_COLOR = "#c9d2df"
BAR_BLUE = "#2563eb"
BAR_GREEN = "#059669"
BAR_ORANGE = "#d97706"
BAR_RED = "#dc2626"
BOX_FACE = "#93c5fd"
BOX_EDGE = "#1e3a8a"
SCATTER_COLORS = {
    "codex": BAR_BLUE,
    "claude": BAR_ORANGE,
}
PROVIDER_COMPARISON_ORDER = ("claude", "codex")
PLOT_COLORS = [
    BAR_BLUE,
    BAR_GREEN,
    BAR_ORANGE,
    BAR_RED,
    "#0891b2",
    "#7c3aed",
    "#64748b",
    "#be123c",
]

plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": AXIS_COLOR,
        "axes.labelcolor": TEXT_COLOR,
        "axes.titlecolor": TEXT_COLOR,
        "xtick.color": MUTED_TEXT,
        "ytick.color": MUTED_TEXT,
        "text.color": TEXT_COLOR,
        "font.family": "DejaVu Sans",
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.titleweight": "semibold",
        "axes.labelsize": 10,
        "figure.titlesize": 15,
        "legend.fontsize": 9,
        "legend.frameon": False,
        "savefig.dpi": SAVE_DPI,
    }
)


def provider_order(providers: Any) -> list[str]:
    names = list(providers.keys()) if isinstance(providers, dict) else list(providers)
    preferred = [name for name in PROVIDER_COMPARISON_ORDER if name in names]
    remaining = sorted(name for name in names if name not in preferred)
    return [*preferred, *remaining]


def short_label(value: str, max_len: int = 36) -> str:
    return value if len(value) <= max_len else value[: max_len - 3] + "..."


def provider_title(provider: str) -> str:
    return provider[:1].upper() + provider[1:] if provider else provider


def plot_color(label: str, index: int) -> str:
    return SCATTER_COLORS.get(label, PLOT_COLORS[index % len(PLOT_COLORS)])


def polish_axes(ax: plt.Axes, *, grid_axis: str = "both", minor: bool = False) -> None:
    ax.set_axisbelow(True)
    ax.grid(True, which="major", axis=grid_axis, color=GRID_COLOR, linewidth=0.8)
    if minor:
        ax.grid(
            True,
            which="minor",
            axis=grid_axis,
            color=GRID_COLOR,
            linewidth=0.45,
            alpha=0.55,
        )
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS_COLOR)
        ax.spines[spine].set_linewidth(0.8)


def save_plot(fig: plt.Figure, out: Path) -> None:
    fig.savefig(out, dpi=SAVE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)


def readable_text_color(rgba: tuple[float, ...]) -> str:
    """Pick white or dark text for a filled segment based on its luminance."""
    r, g, b = rgba[:3]
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#ffffff" if luminance < 0.55 else TEXT_COLOR
