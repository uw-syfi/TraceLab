#!/usr/bin/env python3
"""Build a compact merged timing-fit summary markdown."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path


# Post-processes the timing_feature_ambiguity outputs that live in this same folder.
ARTIFACT_DIR = Path(__file__).resolve().parent
COMPARISON_CSV = ARTIFACT_DIR / "timing_irreducible_error_fit_comparison.csv"
IRREDUCIBLE_JSON = ARTIFACT_DIR / "timing_irreducible_error.json"
OUTPUT_MD = ARTIFACT_DIR / "timing_fit_compact_summary.md"
RESULT_MD = ARTIFACT_DIR / "result_analysis.md"
TOP_GROUPS = 16
INCLUDE_SEGMENT_LABELS = {"e2e"}

SEGMENT_LABELS = {
    "codex_reasoning_end_to_tool_call": "output",
    "codex_tool_result_to_tool_call": "e2e",
    "codex_tool_result_to_reasoning_end": "reasoning",
    "codex_user_message_to_reasoning_end": "user->reasoning",
    "codex_user_message_to_tool_call": "user->e2e",
    "claude_tool_result_to_tool_call": "e2e",
    "claude_user_message_to_tool_call": "user->e2e",
}


def num(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    if value == "":
        return float("nan")
    return float(value)


def fmt_int(value: float) -> str:
    return f"{int(round(value)):,}"


def fmt_ms(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    if abs(value) >= 1000:
        return f"{value / 1000:.2f}s"
    return f"{value:.0f}ms"


def fmt_pct(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def fmt_x(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    return f"{value:.2f}x"


def fmt_r2(value: float) -> str:
    if not math.isfinite(value):
        return "n/a"
    return f"{value:.3f}"


def short_group(row: dict[str, str]) -> str:
    segment = SEGMENT_LABELS.get(row["segment_kind"], row["segment_kind"])
    return f"{row['provider']} / {row['model']} / {segment}"


def segment_label(row: dict[str, str]) -> str:
    return SEGMENT_LABELS.get(row["segment_kind"], row["segment_kind"])


def display_sort_key(row: dict[str, str]) -> tuple[str, str, str]:
    return (row["provider"], row["model"], segment_label(row))


def table_row(row: dict[str, str]) -> str:
    return (
        f"| {short_group(row)} "
        f"| {fmt_int(num(row, 'local_rows'))} "
        f"| {fmt_ms(num(row, 'local_median_duration_ms'))} "
        f"| {fmt_ms(num(row, 'local_mae_ms'))} "
        f"| {fmt_pct(num(row, 'local_mae_ratio_vs_median'))} "
        f"| {fmt_ms(num(row, 'fit_test_mae_ms'))} "
        f"| {fmt_pct(num(row, 'fit_relative_mae_ratio'))} "
        f"| {fmt_x(num(row, 'fit_over_local_mae'))} "
        f"| {fmt_r2(num(row, 'fit_test_r2'))} "
        f"| {fmt_pct(num(row, 'local_smooth_relative_floor'))} "
        f"| {fmt_pct(num(row, 'smooth_target_test_smooth_relative_absolute_error'))} "
        f"| {fmt_x(num(row, 'smooth_target_over_local_smooth_relative_floor'))} |"
    )


def build_markdown(rows: list[dict[str, str]], summary: dict[str, object]) -> str:
    local = summary["local_neighborhood_error"]
    exact = summary["exact_duplicate_pure_error"]
    load = summary["load_stats"]
    assert isinstance(local, dict)
    assert isinstance(exact, dict)
    assert isinstance(load, dict)

    lines = [
        "# Timing Fit Compact Summary",
        "",
        "This is a compact view of the timing-fit results, merging the earlier MAE-target comparison with the smooth-relative comparison.",
        "",
        "## Sources",
        "",
        f"- Input trace: `{summary['input']}`",
        f"- Comparison CSV: `{COMPARISON_CSV}`",
        f"- Irreducible-error JSON: `{IRREDUCIBLE_JSON}`",
        "",
        "## Segment Labels",
        "",
        "- `codex_tool_result_to_tool_call` and `claude_tool_result_to_tool_call` -> `e2e`",
        "- Other segment types are excluded from this compact table.",
        "",
        "## Headline",
        "",
        f"- Rows scanned: {fmt_int(float(load['input_rows']))}; usable timing rows: {fmt_int(float(load['usable_rows']))}.",
        f"- Exact duplicate feature keys: {fmt_int(float(exact['duplicate_feature_keys']))}; duplicate rows: {fmt_int(float(exact['duplicate_rows']))}.",
        f"- Exact-duplicate best possible MAE: {fmt_ms(float(exact['duplicate_subset_best_possible_mae_ms']))}; smooth-relative floor: {fmt_pct(float(exact['duplicate_subset_best_possible_smooth_relative_loss']))}.",
        f"- Local-neighborhood rows covered: {fmt_int(float(local['total_rows_in_local_buckets']))} of {fmt_int(float(local['total_rows_after_trim']))}.",
        f"- Local-neighborhood MAE floor: {fmt_ms(float(local['weighted_local_best_constant_mae_ms']))}; smooth-relative floor: {fmt_pct(float(local['weighted_local_best_constant_smooth_relative_loss']))}.",
        "",
        "## Merged Group Summary",
        "",
        f"Rows shown are the top {TOP_GROUPS} comparable `e2e` groups by local-neighborhood row count. MAE columns use raw millisecond error. Smooth-loss columns use `abs(predicted - actual) / (actual + 1000ms)`.",
        "",
        "| group | local rows | typical latency | local MAE floor | local MAE % | raw-target MAE | raw MAE % | MAE / local | raw-target R2 | local smooth-loss floor | smooth-target smooth loss | smooth target / local |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines.extend(table_row(row) for row in rows)
    lines.extend(
        [
            "",
            "## Reading Notes",
            "",
            "- `local MAE floor` is the best constant prediction inside narrow nearby-token buckets, not a learned model.",
            "- `raw-target MAE` is the quadratic fit trained to predict raw duration in milliseconds.",
            "- `local MAE %` and `raw MAE %` are MAE divided by typical local latency.",
            "- `MAE / local` compares the raw-duration fit MAE against the local-neighborhood MAE floor.",
            "- `smooth-target smooth loss` means the smooth-relative weighted model evaluated with smooth-relative loss.",
            "- Values near `1.0x` mean the fitted model is close to the local-neighborhood floor for that metric.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    with COMPARISON_CSV.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows = [row for row in rows if segment_label(row) in INCLUDE_SEGMENT_LABELS]
    rows.sort(key=lambda row: num(row, "local_rows"), reverse=True)
    rows = sorted(rows[:TOP_GROUPS], key=display_sort_key)

    with IRREDUCIBLE_JSON.open("r", encoding="utf-8") as fh:
        summary = json.load(fh)

    markdown = build_markdown(rows, summary)
    OUTPUT_MD.write_text(markdown, encoding="utf-8")

    RESULT_MD.write_text(
        "\n".join(
            [
                "# Timing Fit By Model Summary Generation",
                "",
                f"- Generated summary: `{OUTPUT_MD}`",
                f"- Source comparison CSV: `{COMPARISON_CSV}`",
                f"- Rows in summary table: {len(rows)}",
                f"- Row selection: top {TOP_GROUPS} `e2e` groups by local-neighborhood row count.",
                "- Display sort: provider, model, then shortened segment type.",
                "- Segment names were shortened according to the mapping in the generated summary.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(OUTPUT_MD)
    print(RESULT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
