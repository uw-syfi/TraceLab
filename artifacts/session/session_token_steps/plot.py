#!/usr/bin/env python3
"""Plot per-session prefix/append token accumulation as step sequences.

Each normalized JSONL row is one LLM invocation. For a selected session, this
script draws one x-axis step per invocation, stacks cached/prefix tokens under
newly appended input tokens, marks visible user messages, and marks compaction
when the full input size (`prefix + append`) drops sharply between invocations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple


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
from matplotlib.patches import Rectangle
from matplotlib.lines import Line2D
import matplotlib.ticker as mticker
import numpy as np


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import png_sidecar  # noqa: E402
import trace_db  # noqa: E402

DEFAULT_OUTPUT_DIR = SCRIPT_DIR
# Rounds with no observed timestamp share this sort sentinel. It is naive to match the
# reconstructed timestamps below, so timestamped and un-timestamped rounds compare without tz errors.
DATETIME_MAX = datetime.max
# Timestamps are pulled from the DB as integer epoch-microseconds (epoch_us) rather than as a
# TIMESTAMP, because native duckdb marshals TIMESTAMP to datetime but duckdb-wasm marshals it to a
# *string* — the int round-trips identically in both. We rebuild the naive datetime here, exactly
# (integer microseconds), so durations match the pre-DuckDB path bit-for-bit on either engine.
_EPOCH = datetime(1970, 1, 1)


def _epoch_us_to_datetime(value: int | None) -> datetime | None:
    return None if value is None else _EPOCH + timedelta(microseconds=value)

SAVE_DPI = 260
TEXT_COLOR = "#172033"
MUTED_TEXT = "#526070"
GRID_COLOR = "#e6eaf0"
AXIS_COLOR = "#c9d2df"
PREFIX_BLUE = "#2563eb"
APPEND_ORANGE = "#d97706"
USER_RED = "#dc2626"
COMPACTION_PURPLE = "#7c3aed"
COMPACTION_BAND = "#ede9fe"
TOTAL_LINE = "#111827"
TIMELINE_BLUE = "#93c5fd"

INPUT_EVENT_TYPES = {"user_message", "tool_result"}
MODEL_OUTPUT_EVENT_TYPES = {"reasoning", "text", "tool_call"}
TIMELINE_BLOCK_SECONDS = 5 * 60
TIMELINE_MAJOR_LABEL_MINUTES = 30
COMPACTION_MIN_PREVIOUS_TOTAL = 32_768
COMPACTION_MIN_DROP_TOKENS = 8_192
COMPACTION_DROP_RATIO = 0.75
COMPACTION_CONFIRM_STEPS = 3
COMPACTION_REBOUND_RATIO = 0.75

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
        "font.size": 11,
        "axes.titleweight": "semibold",
        "legend.frameon": False,
        "savefig.dpi": SAVE_DPI,
    }
)


class TimingEvent(NamedTuple):
    """One row of a round's `timing_events`, as read from the trace DB."""

    event_type: str | None
    source: str | None
    timestamp: datetime | None  # naive, rebuilt from epoch_us; see _epoch_us_to_datetime


def int_value(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    return default


def round_event_types(events: list[TimingEvent]) -> set[str]:
    return {e.event_type for e in events if isinstance(e.event_type, str)}


def first_observed_timestamp(events: list[TimingEvent]) -> datetime | None:
    timestamps = [e.timestamp for e in events if e.timestamp is not None]
    return min(timestamps) if timestamps else None


def input_to_last_output_span_seconds(events: list[TimingEvent]) -> float | None:
    input_timestamps: list[datetime] = []
    output_timestamps: list[datetime] = []
    for event in events:
        if event.timestamp is None:
            continue
        if event.event_type in INPUT_EVENT_TYPES:
            input_timestamps.append(event.timestamp)
        elif event.event_type in MODEL_OUTPUT_EVENT_TYPES:
            output_timestamps.append(event.timestamp)

    if not input_timestamps or not output_timestamps:
        return None
    first_output_at = min(output_timestamps)
    candidate_inputs = [
        timestamp for timestamp in input_timestamps if timestamp <= first_output_at
    ]
    if not candidate_inputs:
        return None
    duration = (max(output_timestamps) - max(candidate_inputs)).total_seconds()
    return duration if duration > 0 else None


def format_count(value: float, _pos: int | None = None) -> str:
    if not math.isfinite(value):
        return ""
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs(value) >= 100_000:
        return f"{value / 1_000:.0f}k"
    if abs(value) >= 10_000:
        return f"{value / 1_000:.0f}k"
    if abs(value) >= 1_000:
        return f"{value / 1_000:.2f}k"
    if abs(value) >= 10:
        return f"{value:.0f}"
    return f"{value:.2g}"


def format_tokens(value: float | int | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return format_count(float(value))


def format_ratio(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def format_seconds(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    if value < 1:
        return f"{value * 1000:.0f} ms"
    if value < 60:
        return f"{value:.2f} s"
    return f"{value / 60:.1f} min"


def short_session_id(session_id: str) -> str:
    digest = hashlib.sha1(session_id.encode("utf-8")).hexdigest()[:10]
    provider = session_id.split(":", 1)[0] if ":" in session_id else "session"
    return f"{provider}-{digest}"


def safe_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")
    return stem[:120] if stem else "session"


@dataclass
class RoundRow:
    sequence: int  # ingestion ordinal (round_pk); final sort tie-break = file order
    provider: str | None
    model: str | None
    tool_call_count: int
    sort_key: tuple[int, datetime, int]
    prefix_tokens: int
    append_tokens: int
    output_tokens: int
    event_types: set[str]
    timestamp: datetime | None
    inference_seconds: float | None
    explicit_compaction: bool

    @property
    def is_user_input(self) -> bool:
        return "user_message" in self.event_types

    @property
    def is_tool_result(self) -> bool:
        return "tool_result" in self.event_types

    @property
    def total_input_tokens(self) -> int:
        return self.prefix_tokens + self.append_tokens


@dataclass(frozen=True)
class CompactionMarker:
    index: int
    kind: str
    previous_total_input_tokens: int | None
    current_total_input_tokens: int
    dropped_tokens: int | None
    drop_ratio: float | None


def has_explicit_compaction_marker(events: list[TimingEvent]) -> bool:
    for event in events:
        event_type = str(event.event_type or "").lower()
        source = str(event.source or "").lower()
        if "compact" in event_type or "compact" in source:
            return True
    return False


def find_compaction_markers(rounds: list[RoundRow]) -> list[CompactionMarker]:
    """Find explicit compaction events or large full-input drops.

    The inferred signal is based only on `prefix + append` decreasing sharply
    and staying lower for the next few rounds. A prefix/cache-read decrease
    alone is intentionally ignored because a cache miss can move tokens from
    prefix to append without reducing total context. A one-round total-input dip
    that immediately rebounds is also ignored.
    """
    markers: list[CompactionMarker] = []
    marked_indices: set[int] = set()
    for index, item in enumerate(rounds):
        if item.explicit_compaction:
            markers.append(
                CompactionMarker(
                    index=index,
                    kind="explicit",
                    previous_total_input_tokens=(
                        rounds[index - 1].total_input_tokens if index > 0 else None
                    ),
                    current_total_input_tokens=item.total_input_tokens,
                    dropped_tokens=None,
                    drop_ratio=None,
                )
            )
            marked_indices.add(index)

    for index in range(1, len(rounds)):
        if index in marked_indices:
            continue
        previous_total = rounds[index - 1].total_input_tokens
        current_total = rounds[index].total_input_tokens
        dropped_tokens = previous_total - current_total
        if (
            previous_total >= COMPACTION_MIN_PREVIOUS_TOTAL
            and dropped_tokens >= COMPACTION_MIN_DROP_TOKENS
            and current_total <= previous_total * COMPACTION_DROP_RATIO
        ):
            future_totals = [
                rounds[future_index].total_input_tokens
                for future_index in range(
                    index + 1,
                    min(len(rounds), index + 1 + COMPACTION_CONFIRM_STEPS),
                )
            ]
            if not future_totals:
                continue
            if any(
                total >= previous_total * COMPACTION_REBOUND_RATIO
                for total in future_totals
            ):
                continue
            markers.append(
                CompactionMarker(
                    index=index,
                    kind="input_drop",
                    previous_total_input_tokens=previous_total,
                    current_total_input_tokens=current_total,
                    dropped_tokens=dropped_tokens,
                    drop_ratio=current_total / previous_total if previous_total else None,
                )
            )
    markers.sort(key=lambda marker: marker.index)
    return markers


@dataclass
class SessionStats:
    session_id: str
    rounds: list[RoundRow] = field(default_factory=list)
    providers: Counter[str] = field(default_factory=Counter)
    models: Counter[str] = field(default_factory=Counter)
    tool_calls: int = 0
    _sorted_rounds: list[RoundRow] | None = field(default=None, init=False, repr=False)
    _compaction_markers: list[CompactionMarker] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def add(self, item: RoundRow) -> None:
        self.rounds.append(item)
        self._sorted_rounds = None
        self._compaction_markers = None
        if isinstance(item.provider, str):
            self.providers[item.provider] += 1
        if isinstance(item.model, str):
            self.models[item.model] += 1
        self.tool_calls += item.tool_call_count

    def sorted_rounds(self) -> list[RoundRow]:
        if self._sorted_rounds is None:
            self._sorted_rounds = sorted(self.rounds, key=lambda item: item.sort_key)
        return self._sorted_rounds

    def compaction_markers(self) -> list[CompactionMarker]:
        if self._compaction_markers is None:
            self._compaction_markers = find_compaction_markers(self.sorted_rounds())
        return self._compaction_markers

    @property
    def provider(self) -> str:
        return self.providers.most_common(1)[0][0] if self.providers else "<unknown>"

    @property
    def model(self) -> str:
        return self.models.most_common(1)[0][0] if self.models else "<unknown>"

    @property
    def round_count(self) -> int:
        return len(self.rounds)

    @property
    def user_input_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.is_user_input)

    @property
    def tool_result_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.is_tool_result)

    @property
    def max_prefix_tokens(self) -> int:
        return max((item.prefix_tokens for item in self.rounds), default=0)

    @property
    def max_append_tokens(self) -> int:
        return max((item.append_tokens for item in self.rounds), default=0)

    @property
    def max_total_input_tokens(self) -> int:
        return max((item.total_input_tokens for item in self.rounds), default=0)

    @property
    def total_prefix_tokens(self) -> int:
        return sum(item.prefix_tokens for item in self.rounds)

    @property
    def total_append_tokens(self) -> int:
        return sum(item.append_tokens for item in self.rounds)

    @property
    def total_input_tokens(self) -> int:
        return self.total_prefix_tokens + self.total_append_tokens

    @property
    def total_output_tokens(self) -> int:
        return sum(item.output_tokens for item in self.rounds)

    @property
    def aggregate_cache_hit_ratio(self) -> float | None:
        total_input = self.total_input_tokens
        if total_input <= 0:
            return None
        return self.total_prefix_tokens / total_input

    @property
    def average_prefix_tokens(self) -> float | None:
        if not self.rounds:
            return None
        return self.total_prefix_tokens / len(self.rounds)

    @property
    def average_append_tokens(self) -> float | None:
        if not self.rounds:
            return None
        return self.total_append_tokens / len(self.rounds)

    @property
    def average_output_tokens(self) -> float | None:
        if not self.rounds:
            return None
        return self.total_output_tokens / len(self.rounds)

    @property
    def average_inference_seconds(self) -> float | None:
        values = [
            item.inference_seconds
            for item in self.rounds
            if item.inference_seconds is not None
        ]
        if not values:
            return None
        return sum(values) / len(values)

    @property
    def inference_rounds(self) -> int:
        return sum(1 for item in self.rounds if item.inference_seconds is not None)

    @property
    def compaction_count(self) -> int:
        return len(self.compaction_markers())

    @property
    def input_drop_compaction_count(self) -> int:
        return sum(
            1 for marker in self.compaction_markers()
            if marker.kind == "input_drop"
        )

    @property
    def explicit_compaction_count(self) -> int:
        return sum(
            1 for marker in self.compaction_markers()
            if marker.kind == "explicit"
        )

    def score(self) -> float:
        mixed_round_bonus = min(self.user_input_rounds, self.tool_result_rounds)
        return (
            self.round_count
            + 5 * mixed_round_bonus
            + math.log2(max(1, self.max_total_input_tokens))
            + 0.03 * self.tool_calls
            + 12 * self.compaction_count
        )

    def context_score(self) -> float:
        return (
            18 * math.log2(max(1, self.max_total_input_tokens))
            + 10 * math.log2(max(1, self.total_input_tokens))
            + 0.25 * self.round_count
            + 0.2 * min(self.user_input_rounds, self.tool_result_rounds)
            + 10 * self.compaction_count
        )

    def compaction_score(self) -> float:
        return (
            100 * self.compaction_count
            + 10 * math.log2(max(1, self.max_total_input_tokens))
            + 0.1 * self.round_count
            + 0.01 * self.tool_calls
        )

    def as_summary_row(self, *, selected: bool = False) -> dict[str, Any]:
        return {
            "selected": selected,
            "session_id": self.session_id,
            "short_id": short_session_id(self.session_id),
            "provider": self.provider,
            "model": self.model,
            "rounds": self.round_count,
            "user_input_rounds": self.user_input_rounds,
            "tool_result_rounds": self.tool_result_rounds,
            "tool_calls": self.tool_calls,
            "total_prefix_tokens": self.total_prefix_tokens,
            "total_append_tokens": self.total_append_tokens,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "aggregate_cache_hit_ratio": self.aggregate_cache_hit_ratio,
            "average_inference_seconds": self.average_inference_seconds,
            "inference_rounds": self.inference_rounds,
            "compaction_count": self.compaction_count,
            "input_drop_compaction_count": self.input_drop_compaction_count,
            "explicit_compaction_count": self.explicit_compaction_count,
            "average_output_tokens": self.average_output_tokens,
            "average_prefix_tokens": self.average_prefix_tokens,
            "average_append_tokens": self.average_append_tokens,
            "max_prefix_tokens": self.max_prefix_tokens,
            "max_append_tokens": self.max_append_tokens,
            "max_total_input_tokens": self.max_total_input_tokens,
            "score": self.score(),
            "context_score": self.context_score(),
        }


def make_round(
    *,
    sequence: int,
    provider: Any,
    model: Any,
    round_index: Any,
    prefix_tokens: Any,
    append_tokens: Any,
    output_tokens: Any,
    tool_call_count: int,
    timing_events: list[TimingEvent],
) -> RoundRow:
    observed_timestamp = first_observed_timestamp(timing_events)
    timestamp = observed_timestamp or DATETIME_MAX
    sort_index = round_index if isinstance(round_index, int) else 1_000_000_000
    return RoundRow(
        sequence=sequence,
        provider=provider if isinstance(provider, str) else None,
        model=model if isinstance(model, str) else None,
        tool_call_count=int(tool_call_count or 0),
        sort_key=(sort_index, timestamp, sequence),
        prefix_tokens=max(0, int_value(prefix_tokens)),
        append_tokens=max(0, int_value(append_tokens)),
        output_tokens=max(0, int_value(output_tokens)),
        event_types=round_event_types(timing_events),
        timestamp=observed_timestamp,
        inference_seconds=input_to_last_output_span_seconds(timing_events),
        explicit_compaction=has_explicit_compaction_marker(timing_events),
    )


def load_sessions_from_db(con: "duckdb.DuckDBPyConnection") -> dict[str, SessionStats]:
    """Assemble per-session round sequences from the trace DB (one C++ ingest, no JSONL re-parse).

    Rounds come back in ingestion order (``round_pk`` == file order), so the surrogate key doubles
    as the sequence / sort tie-break that the line-number tie-break provided before. Per-round
    timing events and tool-call counts are pulled once and grouped in Python; the session
    heuristics then run exactly as on the old JSONL path.
    """
    # Per-round timing events, grouped by round_pk. Order within a round is irrelevant — the
    # consumers use set / min / max / any, none of which depend on event order. Timestamps come back
    # as epoch-microseconds (int) for native/wasm-identical marshalling, rebuilt to datetimes here.
    timing_by_round: dict[int, list[TimingEvent]] = {}
    for round_pk, event_type, source, ts_us in con.execute(
        "SELECT round_pk, event_type, source, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us "
        "FROM timing_events ORDER BY round_pk"
    ).fetchall():
        timing_by_round.setdefault(round_pk, []).append(
            TimingEvent(event_type, source, _epoch_us_to_datetime(ts_us))
        )

    # Per-round tool-call counts (UNNEST of each round's tools[]).
    tool_counts: dict[int, int] = dict(
        con.execute("SELECT round_pk, count(*) FROM tool_calls GROUP BY round_pk").fetchall()
    )

    sessions: dict[str, SessionStats] = {}
    for (
        round_pk,
        session_id,
        provider,
        model,
        round_index,
        prefix_tokens,
        append_tokens,
        output_tokens,
    ) in con.execute(
        "SELECT round_pk, session_id, provider, model, round_index, "
        "prefix_tokens, newly_append_tokens, output_tokens "
        "FROM rounds ORDER BY round_pk"
    ).fetchall():
        if not isinstance(session_id, str) or not session_id:
            continue
        item = make_round(
            sequence=round_pk,
            provider=provider,
            model=model,
            round_index=round_index,
            prefix_tokens=prefix_tokens,
            append_tokens=append_tokens,
            output_tokens=output_tokens,
            tool_call_count=tool_counts.get(round_pk, 0),
            timing_events=timing_by_round.get(round_pk, []),
        )
        sessions.setdefault(session_id, SessionStats(session_id)).add(item)
    return sessions


def select_sessions(
    sessions: dict[str, SessionStats],
    *,
    session_ids: list[str],
    provider: str | None,
    top_sessions: int,
    context_sessions: int,
    compaction_sessions: int,
    min_rounds: int,
    max_rounds: int,
    min_user_input_rounds: int,
    min_tool_result_rounds: int,
    max_user_input_fraction: float,
) -> list[SessionStats]:
    if session_ids:
        missing = [session_id for session_id in session_ids if session_id not in sessions]
        if missing:
            raise SystemExit("Session id not found: " + ", ".join(missing))
        return [sessions[session_id] for session_id in session_ids]

    candidates = []
    for session in sessions.values():
        if provider is not None and session.provider != provider:
            continue
        if session.round_count < min_rounds or session.round_count > max_rounds:
            continue
        if session.user_input_rounds < min_user_input_rounds:
            continue
        if session.tool_result_rounds < min_tool_result_rounds:
            continue
        if session.user_input_rounds / session.round_count > max_user_input_fraction:
            continue
        candidates.append(session)

    balanced = sorted(candidates, key=lambda session: session.score(), reverse=True)
    context_heavy = sorted(
        candidates,
        key=lambda session: session.context_score(),
        reverse=True,
    )
    compaction_heavy = sorted(
        (session for session in candidates if session.compaction_count > 0),
        key=lambda session: session.compaction_score(),
        reverse=True,
    )

    selected: list[SessionStats] = []
    seen: set[str] = set()
    for group in (
        balanced[:top_sessions],
        context_heavy[:context_sessions],
        compaction_heavy[:compaction_sessions],
    ):
        for session in group:
            if session.session_id in seen:
                continue
            selected.append(session)
            seen.add(session.session_id)
    return selected


def select_window(rounds: list[RoundRow], max_steps: int | None) -> tuple[list[RoundRow], int]:
    if max_steps is None or len(rounds) <= max_steps:
        return rounds, 0
    best_start = 0
    best_score: tuple[int, int, int] | None = None
    for start in range(0, len(rounds) - max_steps + 1):
        window = rounds[start : start + max_steps]
        user_count = sum(1 for item in window if item.is_user_input)
        max_total = max((item.total_input_tokens for item in window), default=0)
        tool_like = sum(1 for item in window if item.is_tool_result)
        score = (user_count, max_total, tool_like)
        if best_score is None or score > best_score:
            best_score = score
            best_start = start
    return rounds[best_start : best_start + max_steps], best_start


def step_tick_labels(rounds: list[RoundRow]) -> list[str]:
    labels: list[str] = []
    user_index = 0
    for index, item in enumerate(rounds, start=1):
        if item.is_user_input:
            user_index += 1
            labels.append(f"U{user_index}\n{index}")
        elif index == 1 or index == len(rounds) or index % 10 == 0:
            labels.append(str(index))
        else:
            labels.append("")
    return labels


def draw_timeline_axis(ax: plt.Axes, rounds: list[RoundRow]) -> None:
    timestamps = [item.timestamp for item in rounds]
    available = [(index, timestamp) for index, timestamp in enumerate(timestamps) if timestamp]
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.tick_params(axis="x", which="both", bottom=False, labelbottom=False)
    for spine in ("top", "right", "left", "bottom"):
        ax.spines[spine].set_visible(False)
    ax.set_facecolor("white")

    if not available:
        ax.text(
            0.0,
            0.5,
            "timeline unavailable",
            transform=ax.transAxes,
            ha="left",
            va="center",
            fontsize=10,
            color=MUTED_TEXT,
        )
        return

    start_time = min(timestamp for _index, timestamp in available)
    bucket_to_indices: dict[int, list[int]] = {}
    round_buckets: dict[int, int] = {}
    for index, timestamp in available:
        bucket = int((timestamp - start_time).total_seconds() // TIMELINE_BLOCK_SECONDS)
        bucket_to_indices.setdefault(bucket, []).append(index)
        round_buckets[index] = bucket

    sorted_buckets = sorted(bucket_to_indices.items())
    major_bucket_step = TIMELINE_MAJOR_LABEL_MINUTES // 5
    for ordinal, (bucket, indices) in enumerate(sorted_buckets):
        x0 = min(indices) - 0.41
        width = max(indices) - min(indices) + 0.82
        color = TIMELINE_BLUE if ordinal % 2 == 0 else "#bfdbfe"
        ax.add_patch(
            Rectangle(
                (x0, 0.32),
                width,
                0.36,
                facecolor=color,
                edgecolor="white",
                linewidth=0.5,
                alpha=0.95,
            )
        )
        if bucket % major_bucket_step == 0:
            ax.text(
                x0,
                0.86,
                f"{bucket * 5}m",
                ha="left",
                va="center",
                fontsize=8.0,
                color=TEXT_COLOR,
            )

    # Empty five-minute buckets cannot consume real x-axis width because this
    # figure is indexed by invocation step. Mark large wall-clock gaps with a
    # compact elapsed-time label below the timeline between occupied blocks.
    gap_labels: list[tuple[float, str]] = []
    for (_left_bucket, left_indices), (right_bucket, right_indices) in zip(
        sorted_buckets,
        sorted_buckets[1:],
    ):
        missing = right_bucket - _left_bucket - 1
        if missing <= 0:
            continue
        gap_minutes = missing * 5
        gap_x = (max(left_indices) + min(right_indices)) / 2
        gap_labels.append((gap_x, f"+{gap_minutes}m"))

    placed_gap_labels: list[tuple[float, int]] = []
    lane_y = [0.08, -0.18, -0.44]
    for gap_x, label in gap_labels:
        lane = 0
        for candidate_lane in range(len(lane_y)):
            if all(
                abs(gap_x - prior_x) >= 7.0 or prior_lane != candidate_lane
                for prior_x, prior_lane in placed_gap_labels
            ):
                lane = candidate_lane
                break
        placed_gap_labels.append((gap_x, lane))
        ax.text(
            gap_x,
            lane_y[lane],
            label,
            ha="center",
            va="bottom",
            fontsize=8.2,
            color=MUTED_TEXT,
            clip_on=False,
        )

    ax.text(
        0.0,
        1.10,
        "5-minute wall-clock blocks",
        transform=ax.transAxes,
        ha="left",
        va="center",
        fontsize=10.0,
        color=MUTED_TEXT,
        clip_on=False,
    )


def round_metric_summary(rounds: list[RoundRow]) -> dict[str, Any]:
    total_prefix = sum(item.prefix_tokens for item in rounds)
    total_append = sum(item.append_tokens for item in rounds)
    total_input = total_prefix + total_append
    total_output = sum(item.output_tokens for item in rounds)
    compactions = find_compaction_markers(rounds)
    inference_values = [
        item.inference_seconds for item in rounds if item.inference_seconds is not None
    ]
    tool_calls = sum(item.tool_call_count for item in rounds)
    return {
        "rounds": len(rounds),
        "user_input_rounds": sum(1 for item in rounds if item.is_user_input),
        "tool_result_rounds": sum(1 for item in rounds if item.is_tool_result),
        "tool_calls": tool_calls,
        "total_prefix_tokens": total_prefix,
        "total_append_tokens": total_append,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "aggregate_cache_hit_ratio": (
            total_prefix / total_input if total_input > 0 else None
        ),
        "average_prefix_tokens": total_prefix / len(rounds) if rounds else None,
        "average_append_tokens": total_append / len(rounds) if rounds else None,
        "average_output_tokens": total_output / len(rounds) if rounds else None,
        "average_inference_seconds": (
            sum(inference_values) / len(inference_values) if inference_values else None
        ),
        "inference_rounds": len(inference_values),
        "compaction_count": len(compactions),
        "explicit_compaction_count": sum(
            1 for marker in compactions if marker.kind == "explicit"
        ),
        "input_drop_compaction_count": sum(
            1 for marker in compactions if marker.kind == "input_drop"
        ),
        "max_total_input_tokens": max(
            (item.total_input_tokens for item in rounds),
            default=0,
        ),
    }


def plot_session(
    session: SessionStats,
    output_dir: Path,
    max_steps: int | None,
) -> dict[str, Any]:
    rounds, window_start = select_window(session.sorted_rounds(), max_steps)
    if not rounds:
        raise ValueError(f"Session has no rounds: {session.session_id}")

    prefix = np.asarray([item.prefix_tokens for item in rounds], dtype=float)
    append = np.asarray([item.append_tokens for item in rounds], dtype=float)
    total = prefix + append
    x = np.arange(len(rounds))
    window_metrics = round_metric_summary(rounds)
    compactions = find_compaction_markers(rounds)

    fig_width = max(13.0, min(24.0, 0.18 * len(rounds) + 8.5))
    fig, (ax_timeline, ax) = plt.subplots(
        2,
        1,
        figsize=(fig_width, 9.1),
        sharex=True,
        gridspec_kw={"height_ratios": [0.46, 7.5]},
    )
    fig.suptitle(
        f"Session Token Accumulation: {short_session_id(session.session_id)}",
        fontsize=18,
        fontweight="semibold",
        y=0.985,
    )

    ymax = max(float(total.max()), 1.0)
    for marker in compactions:
        ax.axvspan(
            marker.index - 0.46,
            marker.index + 0.46,
            color=COMPACTION_BAND,
            alpha=0.72,
            linewidth=0,
            zorder=0,
        )

    ax.bar(
        x,
        prefix,
        width=0.82,
        color=PREFIX_BLUE,
        alpha=0.84,
        label="prefix / cache read",
        zorder=2,
    )
    ax.bar(
        x,
        append,
        width=0.82,
        bottom=prefix,
        color=APPEND_ORANGE,
        alpha=0.9,
        label="append / new input",
        zorder=2,
    )
    ax.plot(
        x,
        total,
        color=TOTAL_LINE,
        linewidth=1.2,
        alpha=0.78,
        label="total input",
        zorder=3,
    )

    user_positions = [index for index, item in enumerate(rounds) if item.is_user_input]
    user_counter = 0
    for index in user_positions:
        user_counter += 1
        ax.axvline(index, color=USER_RED, linestyle="--", linewidth=0.9, alpha=0.55)
    for compaction_index, marker in enumerate(compactions, start=1):
        ax.axvline(
            marker.index,
            color=COMPACTION_PURPLE,
            linestyle="-",
            linewidth=1.25,
            alpha=0.9,
            zorder=4,
        )
        ax.text(
            marker.index,
            ymax * 1.075,
            f"C{compaction_index}",
            ha="center",
            va="bottom",
            fontsize=9.2,
            fontweight="semibold",
            color=COMPACTION_PURPLE,
            clip_on=False,
        )

    window_note = (
        f"window {window_start + 1}-{window_start + len(rounds)} of {session.round_count}"
        if window_start or len(rounds) < session.round_count
        else f"{session.round_count} rounds"
    )
    fig.text(
        0.055,
        0.938,
        (
            f"{session.provider} | {session.model} | {window_note} | "
            f"user-input={window_metrics['user_input_rounds']:,} | "
            f"tool-result={window_metrics['tool_result_rounds']:,} | "
            f"tools={window_metrics['tool_calls']:,} | "
            f"compactions={window_metrics['compaction_count']:,} "
            f"(drops={window_metrics['input_drop_compaction_count']:,}) | "
            f"context score={session.context_score():.1f}"
        ),
        ha="left",
        va="bottom",
        fontsize=12.0,
        color=MUTED_TEXT,
    )
    fig.text(
        0.055,
        0.913,
        (
            f"cache hit={format_ratio(window_metrics['aggregate_cache_hit_ratio'])} | "
            f"avg inference={format_seconds(window_metrics['average_inference_seconds'])} "
            f"({window_metrics['inference_rounds']:,} rounds) | "
            f"avg output={format_tokens(window_metrics['average_output_tokens'])} tok | "
            f"avg append={format_tokens(window_metrics['average_append_tokens'])} tok | "
            f"avg prefix={format_tokens(window_metrics['average_prefix_tokens'])} tok | "
            f"total input={format_tokens(window_metrics['total_input_tokens'])} tok"
        ),
        ha="left",
        va="bottom",
        fontsize=11.6,
        color=MUTED_TEXT,
    )

    draw_timeline_axis(ax_timeline, rounds)
    ax.set_xlabel("LLM invocation step; U labels mark visible user-message rounds", fontsize=14)
    ax.set_ylabel("Input tokens", fontsize=14)
    ax.set_xticks(x)
    ax.set_xticklabels(step_tick_labels(rounds), fontsize=8.5)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(format_count))
    ax.tick_params(axis="y", labelsize=12)
    ax.set_xlim(-0.8, len(rounds) - 0.2)
    ax.set_ylim(0, ymax * 1.12)
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", color=GRID_COLOR, linewidth=0.9)
    ax.grid(True, axis="x", color=GRID_COLOR, linewidth=0.35, alpha=0.45)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(AXIS_COLOR)
    legend_handles = [
        Rectangle(
            (0, 0),
            1,
            1,
            facecolor=PREFIX_BLUE,
            alpha=0.84,
            edgecolor="none",
            label="prefix / cache read",
        ),
        Rectangle(
            (0, 0),
            1,
            1,
            facecolor=APPEND_ORANGE,
            alpha=0.9,
            edgecolor="none",
            label="append / new input",
        ),
        Line2D([0], [0], color=TOTAL_LINE, linewidth=1.2, label="total input"),
        Line2D(
            [0],
            [0],
            color=USER_RED,
            linestyle="--",
            linewidth=1.0,
            label="user input",
        ),
        Line2D(
            [0],
            [0],
            color=COMPACTION_PURPLE,
            linewidth=1.25,
            label="compaction: total input drop",
        ),
    ]
    ax.legend(
        handles=legend_handles,
        loc="upper left",
        bbox_to_anchor=(0.0, 1.01),
        ncols=5,
        fontsize=11,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_stem(short_session_id(session.session_id)) + "_token_steps.png"
    out = output_dir / filename
    fig.subplots_adjust(top=0.87, bottom=0.12, left=0.055, right=0.99, hspace=0.08)
    fig.savefig(out, dpi=SAVE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Saved {out}", file=sys.stderr)

    return {
        **session.as_summary_row(selected=True),
        "plot": str(out),
        "window_start_step": window_start + 1,
        "window_end_step": window_start + len(rounds),
        "window_rounds": len(rounds),
        "window_user_input_rounds": sum(1 for item in rounds if item.is_user_input),
        "window_max_total_input_tokens": int(total.max()),
        "window_compaction_count": len(compactions),
        "window_metrics": window_metrics,
    }


def write_outputs(
    output_dir: Path,
    candidates: list[SessionStats],
    selected_rows: list[dict[str, Any]],
    bad_json: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "session_token_steps_candidates.csv"
    fieldnames = [
        "selected",
        "session_id",
        "short_id",
        "provider",
        "model",
        "rounds",
        "user_input_rounds",
        "tool_result_rounds",
        "tool_calls",
        "total_prefix_tokens",
        "total_append_tokens",
        "total_input_tokens",
        "total_output_tokens",
        "aggregate_cache_hit_ratio",
        "average_inference_seconds",
        "inference_rounds",
        "compaction_count",
        "input_drop_compaction_count",
        "explicit_compaction_count",
        "average_output_tokens",
        "average_prefix_tokens",
        "average_append_tokens",
        "max_prefix_tokens",
        "max_append_tokens",
        "max_total_input_tokens",
        "score",
        "context_score",
    ]
    selected_ids = {row["session_id"] for row in selected_rows}
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for session in candidates:
            writer.writerow(session.as_summary_row(selected=session.session_id in selected_ids))
    print(f"Saved {csv_path}", file=sys.stderr)

    json_path = output_dir / "selected_session_token_steps.json"
    payload = {
        "bad_json": bad_json,
        "selected_sessions": selected_rows,
    }
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"Saved {json_path}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot per-session prefix/append token accumulation and compaction markers.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--session-id",
        action="append",
        default=[],
        help="Specific session id to plot. Can be supplied multiple times.",
    )
    parser.add_argument("--provider", choices=["claude", "codex"], default=None)
    parser.add_argument("--top-sessions", type=int, default=6)
    parser.add_argument(
        "--select-offset",
        type=int,
        default=0,
        help="Render only selected sessions starting at this index (for sharded parallel rendering).",
    )
    parser.add_argument(
        "--select-stride",
        type=int,
        default=1,
        help="Render every Nth selected session (i.e. the shard count) from --select-offset.",
    )
    parser.add_argument(
        "--context-sessions",
        type=int,
        default=4,
        help="Additional sessions selected by context-heavy score after balanced selection.",
    )
    parser.add_argument(
        "--compaction-sessions",
        type=int,
        default=4,
        help="Additional sessions selected for explicit or full-input-drop compaction markers.",
    )
    parser.add_argument("--min-rounds", type=int, default=60)
    parser.add_argument("--max-rounds", type=int, default=220)
    parser.add_argument("--min-user-input-rounds", type=int, default=2)
    parser.add_argument("--min-tool-result-rounds", type=int, default=10)
    parser.add_argument(
        "--max-user-input-fraction",
        type=float,
        default=0.7,
        help="Reject automatic candidates where visible user-message rounds dominate.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="Optional maximum plotted invocations per session. By default, plot the full session from the first round.",
    )
    parser.add_argument(
        "--candidate-limit",
        type=int,
        default=100,
        help="Number of ranked candidate sessions to write to the CSV.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.db is None and not Path(args.input).exists():
        print(f"Input not found: {args.input}", file=sys.stderr)
        return 2
    if args.top_sessions <= 0:
        print("--top-sessions must be positive", file=sys.stderr)
        return 2
    if args.context_sessions < 0:
        print("--context-sessions must be nonnegative", file=sys.stderr)
        return 2
    if args.compaction_sessions < 0:
        print("--compaction-sessions must be nonnegative", file=sys.stderr)
        return 2
    if args.max_steps is not None and args.max_steps <= 0:
        print("--max-steps must be positive", file=sys.stderr)
        return 2

    con = trace_db.open_from_args(args)
    print("Reading trace DB", file=sys.stderr)
    sessions = load_sessions_from_db(con)
    # The DB only holds parseable rows, so there is no bad-line count to report; kept for the
    # selected-JSON schema the downstream consumers expect.
    bad_json = 0
    print(f"Loaded sessions={len(sessions):,}", file=sys.stderr)

    selected = select_sessions(
        sessions,
        session_ids=args.session_id,
        provider=args.provider,
        top_sessions=args.top_sessions,
        context_sessions=args.context_sessions,
        compaction_sessions=args.compaction_sessions,
        min_rounds=args.min_rounds,
        max_rounds=args.max_rounds,
        min_user_input_rounds=args.min_user_input_rounds,
        min_tool_result_rounds=args.min_tool_result_rounds,
        max_user_input_fraction=args.max_user_input_fraction,
    )
    if not selected:
        print("No sessions matched the selection criteria.", file=sys.stderr)
        return 1

    # Sharded parallel rendering: render only this shard's slice of the (deterministically
    # ordered) selected sessions. Defaults (offset 0, stride 1) keep the full list.
    if args.select_stride > 1 or args.select_offset:
        selected = selected[args.select_offset :: max(1, args.select_stride)]
        if not selected:
            print(f"No sessions in shard offset={args.select_offset} stride={args.select_stride}.", file=sys.stderr)
            return 0

    ranked_candidates = sorted(
        (
            session
            for session in sessions.values()
            if (
                (args.provider is None or session.provider == args.provider)
                and session.round_count >= args.min_rounds
                and session.round_count <= args.max_rounds
                and session.user_input_rounds >= args.min_user_input_rounds
                and session.tool_result_rounds >= args.min_tool_result_rounds
                and session.user_input_rounds / session.round_count
                <= args.max_user_input_fraction
            )
        ),
        key=lambda session: session.score(),
        reverse=True,
    )[: args.candidate_limit]

    selected_rows = [
        plot_session(session, args.output_dir, args.max_steps)
        for session in selected
    ]
    write_outputs(args.output_dir, ranked_candidates, selected_rows, bad_json)
    png_sidecar.make_self_contained(
        args.output_dir,
        code_files=[Path(__file__)],
        readme_path=SCRIPT_DIR / "README.md",
    )

    print("Selected sessions:", file=sys.stderr)
    for row in selected_rows:
        print(
            f"  {row['short_id']}: provider={row['provider']} "
            f"rounds={row['rounds']} user_inputs={row['user_input_rounds']} "
            f"compactions={row['compaction_count']} "
            f"max_total={row['max_total_input_tokens']:,} "
            f"total_input={row['total_input_tokens']:,} "
            f"score={row['score']:.1f} context_score={row['context_score']:.1f}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
