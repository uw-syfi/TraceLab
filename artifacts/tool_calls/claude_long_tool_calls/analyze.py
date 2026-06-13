#!/usr/bin/env python3
"""Audit Claude tool calls whose effective latency exceeds one hour.

Migrated onto the shared DuckDB layer (``artifacts/utils/trace_db.py``): the over-1h
selection, the per-tool / per-model / per-source rollups, and the duplicate-signature
accounting are all computed in SQL over the ``tool_calls`` / ``rounds`` tables instead of
re-parsing the JSONL in Python. Effective latency uses the shared
``trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`` precedence (internal else wall).

Schema note (``input_preview``): the audit's per-row ``input_preview`` column needs the raw
``tool.input`` dict, which ``trace_db.materialize()`` deliberately drops (a schema-drift trap).
The DB cannot supply it. We therefore fetch the raw ``input`` text for the (small) flagged set
straight from the source trace JSONL when one is reachable: with ``-i`` that is the given trace;
with ``--db`` we read the DB's own ``trace_source`` provenance if present, else fall back to
``-i``. When no matching trace is reachable, ``input_preview`` is left blank (logged on stderr).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _open_trace_text(path: Path):
    """Open a normalized trace for line reading, transparently decompressing ``.gz``.

    ``trace_db`` ingests gzipped traces fine (DuckDB autodetects), and the provenance path it records
    can therefore point at a ``.gz`` — so this best-effort backfill reader must handle it too. Gzip
    decompresses to the same line order DuckDB saw, so ``line_no`` still equals ``round_pk``.
    """
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]  # experiment -> category -> artifacts -> repo root
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import trace_db  # noqa: E402

ONE_HOUR_MS = 3_600_000
HUMAN_OR_APPROVAL_TOOLS = {"AskUserQuestion", "ExitPlanMode", "PushNotification"}

# Effective latency precedence (internal else wall), as a SQL fragment over `tool_calls tc`.
_EFF = trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def preview(value: Any, limit: int = 220) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def ms_to_hours(ms: float) -> float:
    return ms / ONE_HOUR_MS


def fmt_counter(counter: Counter[str], limit: int | None = None) -> str:
    items = counter.most_common(limit)
    if not items:
        return "- none"
    return "\n".join(f"- {name}: {count:,}" for name, count in items)


def _iso_ms(us: int | None) -> str:
    """Rebuild the trace's ``YYYY-MM-DDTHH:MM:SS.mmmZ`` string from a microsecond epoch.

    ``trace_db`` stores ``emitted_at`` / ``result_at`` as naive-UTC microsecond TIMESTAMPs (the
    ISO ``T``/``Z`` are stripped on ingest). We fetch them as ``epoch_us`` BIGINTs (the schema's
    recommended engine-portable path) and reconstruct the original millisecond-precision string so
    the CSV/markdown match the legacy raw-string output byte-for-byte.
    """
    if us is None:
        return ""
    sec, micro = divmod(int(us), 1_000_000)
    dt = datetime.fromtimestamp(sec, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{micro // 1000:03d}Z"


def _trace_source(con) -> Path | None:
    """Best-effort recovery of the trace the DB was built from (for ``input_preview``).

    ``trace_db.materialize()`` records the source path in a one-row ``trace_source`` table (see
    ``DB_SCHEMA.md``); we use it when the path still exists, else the caller falls back to ``-i``.
    """
    try:
        row = con.execute(
            "SELECT path FROM trace_source LIMIT 1"  # provenance recorded by trace_db.materialize()
        ).fetchone()
    except Exception:
        return None
    if row and row[0]:
        p = Path(str(row[0]))
        return p if p.exists() else None
    return None


def load_input_previews(
    trace_path: Path, expected: dict[tuple[int, int], tuple[str, Any]]
) -> dict[tuple[int, int], str]:
    """``{(round_pk, tool_index): input_preview}`` for the flagged rows, from the source JSONL.

    ``round_pk`` follows file order (``trace_db`` ingests single-threaded with no blank-line skips,
    matching the legacy 1-based ``line_no``), so we read the raw ``tool.input`` dict from the same
    line and run the identical ``preview()`` the old loader used. Only the (small) flagged set is
    materialized.

    ``expected`` carries each row's DB identity ``(tool_call_id, input_chars)``; we only trust a
    JSONL line whose tool matches it. This guards against ``--input`` pointing at a trace that is not
    the one the DB was built from (the DB records no provenance), in which case the line at that
    ordinal is a *different* call — we skip it (leaving the preview blank) rather than emit a wrong
    one. When ``--input`` is the DB's source trace (the common case, incl. the merged trace of which
    the sample is a prefix) every row matches and the preview is exact.
    """
    if not expected:
        return {}
    wanted_lines = {rpk for rpk, _ti in expected}
    out: dict[tuple[int, int], str] = {}
    with _open_trace_text(trace_path) as fh:
        for line_no, line in enumerate(fh, start=1):
            if line_no not in wanted_lines:
                continue
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("provider") != "claude":
                continue
            tools = row.get("tools")
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                key = (line_no, tool.get("tool_index"))
                want = expected.get(key)
                if want is None:
                    continue
                want_call_id, want_input_chars = want
                got_call_id = str(tool.get("tool_call_id") or "")
                got_input_chars = tool.get("input_chars")
                if got_call_id == want_call_id and got_input_chars == want_input_chars:
                    out[key] = preview(tool.get("input"))
    return out


def fetch_over_1h(con, threshold_ms: float) -> list[dict[str, Any]]:
    """All Claude tool calls with positive effective latency strictly above ``threshold_ms``.

    Sorted most-latency-first with a total, deterministic tie-break (``round_pk, tool_index``) so
    the row order is stable across DB builds (DuckDB does not guarantee tie ordering otherwise).
    The legacy code's ``line_no`` is the 1-based file line; with no blank lines in the trace this is
    exactly ``round_pk`` (single-threaded ingest = file order).
    """
    query = f"""
        SELECT
            tc.round_pk                                            AS round_pk,
            r.trace_key                                           AS trace_key,
            r.session_id                                          AS session_id,
            r.round_id                                            AS round_id,
            r.round_index                                         AS round_index,
            r.model                                               AS model,
            tc.tool_index                                         AS tool_index,
            CASE WHEN tc.tool_name IS NULL OR tc.tool_name = ''
                 THEN '<unknown-tool>' ELSE tc.tool_name END      AS tool_name,
            tc.tool_call_id                                       AS tool_call_id,
            CASE WHEN tc.tool_internal_latency_ms IS NOT NULL
                 THEN 'internal' ELSE 'wall_fallback' END         AS source,
            ({_EFF})                                              AS eff,
            tc.tool_wall_latency_ms                               AS wall_ms,
            tc.tool_internal_latency_ms                           AS internal_ms,
            CAST(epoch_us(tc.emitted_at) AS BIGINT)               AS emit_us,
            CAST(epoch_us(tc.result_at) AS BIGINT)                AS res_us,
            tc.is_error                                           AS is_error,
            tc.input_chars                                        AS input_chars,
            tc.result_chars                                       AS result_chars
        FROM tool_calls tc
        JOIN rounds r USING (round_pk)
        WHERE r.provider = 'claude'
          AND ({_EFF}) IS NOT NULL
          AND ({_EFF}) > 0
          AND ({_EFF}) > {threshold_ms}
        ORDER BY ({_EFF}) DESC, tc.round_pk, tc.tool_index
    """
    rows: list[dict[str, Any]] = []
    for (
        round_pk, trace_key, session_id, round_id, round_index, model, tool_index,
        tool_name, tool_call_id, source, eff, wall_ms, internal_ms, emit_us, res_us,
        is_error, input_chars, result_chars,
    ) in con.execute(query).fetchall():
        latency_ms = float(eff)
        wall_f = float(wall_ms) if wall_ms is not None else None
        internal_f = float(internal_ms) if internal_ms is not None else None
        emitted_at = _iso_ms(emit_us) or None
        result_at = _iso_ms(res_us) or None
        computed_wall_ms = None
        tool_result_delay_hours = None
        if emit_us is not None and res_us is not None:
            # legacy: round((result_ts - emitted_ts).total_seconds() * 1000); us math is exact.
            computed_wall_ms = round((res_us - emit_us) / 1000)
            tool_result_delay_hours = ms_to_hours(computed_wall_ms)
        rows.append(
            {
                "line_no": int(round_pk),
                "trace_key": trace_key,
                "session_id": str(session_id or ""),
                "round_id": round_id,
                "round_index": round_index,
                "model": model,
                "tool_index": tool_index,
                "tool_name": tool_name,
                "tool_call_id": str(tool_call_id or ""),
                "source": source,
                "effective_latency_ms": latency_ms,
                "effective_latency_hours": ms_to_hours(latency_ms),
                "wall_latency_ms": wall_f,
                "wall_latency_hours": ms_to_hours(wall_f) if wall_f is not None else None,
                "computed_wall_latency_ms": computed_wall_ms,
                "internal_latency_ms": internal_f,
                "internal_latency_hours": (
                    ms_to_hours(internal_f) if internal_f is not None else None
                ),
                "emitted_at": emitted_at,
                "result_at": result_at,
                "tool_result_delay_hours": tool_result_delay_hours,
                "is_error": is_error is True,
                "input_chars": input_chars,
                "result_chars": result_chars,
                "input_preview": "",  # filled from the source JSONL (schema gap; see module docstring)
            }
        )
    return rows


def fetch_scan_counts(con) -> dict[str, int]:
    """Claude tool-call scan tallies (total / positive / missing / nonpositive effective latency)."""
    row = con.execute(
        f"""
        SELECT
            count(*)                                                       AS scanned,
            count(*) FILTER (WHERE eff IS NOT NULL AND eff > 0)            AS positive,
            count(*) FILTER (WHERE eff IS NULL)                            AS missing,
            count(*) FILTER (WHERE eff IS NOT NULL AND eff <= 0)           AS nonpositive
        FROM (
            SELECT ({_EFF}) AS eff
            FROM tool_calls tc JOIN rounds r USING (round_pk)
            WHERE r.provider = 'claude'
        )
        """
    ).fetchone()
    return {
        "scanned": int(row[0]),
        "positive": int(row[1]),
        "missing": int(row[2]),
        "nonpositive": int(row[3]),
    }


def fetch_source_counts(con) -> Counter[str]:
    """Effective-latency source over ALL Claude tool calls: internal / wall_fallback / missing.

    Mirrors the legacy ``effective_latency`` source labels: ``internal`` when internal timing is
    present, else ``wall_fallback`` when wall is present, else ``missing``.
    """
    rows = con.execute(
        f"""
        WITH ordered AS (
            SELECT
                CASE
                    WHEN tc.tool_internal_latency_ms IS NOT NULL THEN 'internal'
                    WHEN tc.tool_wall_latency_ms IS NOT NULL THEN 'wall_fallback'
                    ELSE 'missing'
                END AS source,
                row_number() OVER (ORDER BY tc.round_pk, tc.tool_index) AS call_ord
            FROM tool_calls tc JOIN rounds r USING (round_pk)
            WHERE r.provider = 'claude'
        )
        SELECT source, count(*) AS n, min(call_ord) AS first_seen
        FROM ordered
        GROUP BY source
        ORDER BY first_seen
        """
    ).fetchall()
    counter: Counter[str] = Counter()
    for source, n, _first_seen in rows:
        counter[source] = int(n)
    return counter


def fetch_wall_over_threshold_internal_won(con, threshold_ms: float) -> int:
    """Calls where wall > threshold but the (smaller) internal timing won, so effective ≤ threshold."""
    return int(
        con.execute(
            f"""
            SELECT count(*)
            FROM tool_calls tc JOIN rounds r USING (round_pk)
            WHERE r.provider = 'claude'
              AND ({_EFF}) IS NOT NULL AND ({_EFF}) > 0
              AND tc.tool_wall_latency_ms IS NOT NULL
              AND tc.tool_wall_latency_ms > {threshold_ms}
              AND ({_EFF}) <= {threshold_ms}
            """
        ).fetchone()[0]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=EXP_DIR)
    parser.add_argument("--threshold-ms", type=float, default=ONE_HOUR_MS)
    args = parser.parse_args()

    con = trace_db.open_from_args(args)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    input_label = str(args.db) if getattr(args, "db", None) is not None else str(args.input)

    # --- over-1h detail rows (DB) ---
    over_1h = fetch_over_1h(con, args.threshold_ms)

    # --- input_preview backfill from the source trace (schema gap: raw `input` not in DB) ---
    expected = {
        (item["line_no"], item["tool_index"]): (item["tool_call_id"], item["input_chars"])
        for item in over_1h
    }
    trace_path = _trace_source(con)
    if trace_path is None and getattr(args, "input", None) is not None:
        candidate = Path(args.input)
        if candidate.exists():
            trace_path = candidate
    previews: dict[tuple[int, int], str] = {}
    if expected:
        if trace_path is not None:
            previews = load_input_previews(trace_path, expected)
            if len(previews) < len(expected):
                print(
                    f"warning: {len(expected) - len(previews)} of {len(expected)} input_preview "
                    f"rows had no matching call in {trace_path} (left blank); raw tool.input is not "
                    "stored in the DB, so pass -i pointing at the trace the DB was built from",
                    file=sys.stderr,
                )
        else:
            print(
                "warning: no source trace reachable for input_preview; column left blank "
                "(raw tool.input is not stored in the DB)",
                file=sys.stderr,
            )
    for item in over_1h:
        item["input_preview"] = previews.get((item["line_no"], item["tool_index"]), "")

    # --- aggregate rollups over the >1h set (Python over the fetched rows = order-independent) ---
    scan = fetch_scan_counts(con)
    source_counts = fetch_source_counts(con)
    wall_over_1h_effective_not_over_1h = fetch_wall_over_threshold_internal_won(
        con, args.threshold_ms
    )

    source_over_1h_counts: Counter[str] = Counter()
    tool_over_1h_counts: Counter[str] = Counter()
    model_over_1h_counts: Counter[str] = Counter()
    error_over_1h_counts: Counter[str] = Counter()
    missing_internal_over_1h_counts: Counter[str] = Counter()
    latency_sum_by_tool: defaultdict[str, float] = defaultdict(float)

    # Duplicate-key / signature accounting (matches the legacy Counters keyed off the flagged rows).
    over_1h_keys: Counter[tuple[str, str, str]] = Counter()
    over_1h_trace_tool_keys: Counter[tuple[str, str, str]] = Counter()
    over_1h_signatures: Counter[tuple[Any, ...]] = Counter()
    over_1h_signature_latency: dict[tuple[Any, ...], float] = {}
    over_1h_signature_sessions: defaultdict[tuple[Any, ...], set[str]] = defaultdict(set)

    # The legacy code built these Counters while scanning the file top-to-bottom, so their
    # `most_common()` tie order (and `latency_sum_by_tool`'s stable-sort tie order) follows FILE
    # order, not the latency-desc order the CSV uses. Iterate a file-ordered view to reproduce it.
    for item in sorted(over_1h, key=lambda it: (it["line_no"], it["tool_index"])):
        tool_name = item["tool_name"]
        session_id = item["session_id"]
        round_id = str(item["round_id"] or item["trace_key"] or "")
        trace_key = str(item["trace_key"] or "")
        tool_call_id = item["tool_call_id"]
        latency_ms = item["effective_latency_ms"]

        source_over_1h_counts[item["source"]] += 1
        tool_over_1h_counts[tool_name] += 1
        model_over_1h_counts[str(item["model"] or "<unknown-model>")] += 1
        error_over_1h_counts[str(item["is_error"] is True)] += 1
        missing_internal_over_1h_counts[str(item["internal_latency_ms"] is None)] += 1
        latency_sum_by_tool[tool_name] += latency_ms

        signature = (
            round_id,
            tool_call_id,
            tool_name,
            str(item["emitted_at"] or ""),
            str(item["result_at"] or ""),
            float(latency_ms),
            item["input_chars"],
            item["result_chars"],
        )
        over_1h_signatures[signature] += 1
        over_1h_signature_latency.setdefault(signature, latency_ms)
        over_1h_signature_sessions[signature].add(session_id)
        if tool_call_id:
            over_1h_keys[(session_id, round_id, tool_call_id)] += 1
            over_1h_trace_tool_keys[(trace_key, str(item["tool_index"]), tool_call_id)] += 1

    detail_path = args.output_dir / "claude_gt1h_tool_calls.csv"
    fieldnames = [
        "line_no",
        "trace_key",
        "session_id",
        "round_id",
        "round_index",
        "model",
        "tool_index",
        "tool_name",
        "tool_call_id",
        "source",
        "effective_latency_ms",
        "effective_latency_hours",
        "wall_latency_ms",
        "wall_latency_hours",
        "computed_wall_latency_ms",
        "internal_latency_ms",
        "internal_latency_hours",
        "emitted_at",
        "result_at",
        "tool_result_delay_hours",
        "is_error",
        "input_chars",
        "result_chars",
        "input_preview",
    ]
    csv_rows = [{k: item[k] for k in fieldnames} for item in over_1h]
    with detail_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(csv_rows)

    duplicate_over_1h = sum(1 for count in over_1h_keys.values() if count > 1)
    duplicate_trace_tool_over_1h = sum(
        1 for count in over_1h_trace_tool_keys.values() if count > 1
    )
    duplicate_signature_groups = sum(
        1 for count in over_1h_signatures.values() if count > 1
    )
    duplicate_signature_extra_rows = sum(
        count - 1 for count in over_1h_signatures.values() if count > 1
    )
    cross_session_signature_groups = sum(
        1 for signature, sessions in over_1h_signature_sessions.items()
        if len(sessions) > 1 and over_1h_signatures[signature] > 1
    )
    unique_signature_total_ms = sum(over_1h_signature_latency.values())
    total_over_1h_ms = sum(item["effective_latency_ms"] for item in over_1h)
    human_or_approval_calls = [
        item for item in over_1h if item["tool_name"] in HUMAN_OR_APPROVAL_TOOLS
    ]
    human_or_approval_ms = sum(item["effective_latency_ms"] for item in human_or_approval_calls)

    top_latency_lines = []
    for item in over_1h[:12]:
        top_latency_lines.append(
            "- "
            f"{item['effective_latency_hours']:.2f}h "
            f"{item['tool_name']} source={item['source']} "
            f"error={item['is_error']} emitted={item['emitted_at']} result={item['result_at']} "
            f"trace={item['trace_key']}"
        )

    top_tool_latency_lines = []
    # Stable sort on latency desc only: equal-latency tools keep `latency_sum_by_tool` insertion
    # (file) order, exactly like the legacy `sorted(..., key=item[1], reverse=True)`.
    for tool_name, latency_ms in sorted(
        latency_sum_by_tool.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )[:12]:
        top_tool_latency_lines.append(
            f"- {tool_name}: {ms_to_hours(latency_ms):.2f}h over "
            f"{tool_over_1h_counts[tool_name]:,} calls"
        )

    duplicate_signature_lines = []
    for signature, count in over_1h_signatures.most_common():
        if count <= 1:
            continue
        round_id, tool_call_id, tool_name, emitted_at, result_at, latency_ms, _ic, _rc = signature
        duplicate_signature_lines.append(
            "- "
            f"{count}x {ms_to_hours(latency_ms):.2f}h {tool_name} "
            f"sessions={len(over_1h_signature_sessions[signature])} "
            f"round={round_id} tool={tool_call_id} emitted={emitted_at} result={result_at}"
        )
        if len(duplicate_signature_lines) >= 12:
            break

    analysis_path = args.output_dir / "result_analysis.md"
    analysis_path.write_text(
        "\n".join(
            [
                "# Claude >1h tool-call latency audit",
                "",
                f"Input: `{input_label}`",
                f"Threshold: `{args.threshold_ms:,.0f}` ms",
                "",
                "## Summary",
                "",
                f"- Claude tool calls scanned: {scan['scanned']:,}",
                f"- Claude positive effective-latency calls: {scan['positive']:,}",
                f"- Claude missing effective-latency calls: {scan['missing']:,}",
                f"- Claude nonpositive effective-latency calls: {scan['nonpositive']:,}",
                f"- Claude effective-latency calls >1h: {len(over_1h):,}",
                f"- Total effective latency in >1h Claude calls: {ms_to_hours(total_over_1h_ms):.2f}h",
                f"- Effective >1h duplicate `(session_id, round_id, tool_call_id)` keys: {duplicate_over_1h:,}",
                f"- All-Claude duplicate `(session_id, round_id, tool_call_id)` keys: {fetch_all_dup_keys(con):,}",
                f"- Effective >1h duplicate `(trace_key, tool_index, tool_call_id)` keys: {duplicate_trace_tool_over_1h:,}",
                f"- All-Claude duplicate `(trace_key, tool_index, tool_call_id)` keys: {fetch_all_dup_trace_tool_keys(con):,}",
                f"- Effective >1h duplicate cross-session signatures: {duplicate_signature_groups:,}",
                f"- Effective >1h extra rows after signature dedup: {duplicate_signature_extra_rows:,}",
                f"- Effective >1h duplicate signature groups spanning multiple sessions: {cross_session_signature_groups:,}",
                f"- Wall timestamp mismatch count: {fetch_timestamp_mismatches(con):,}",
                f"- Wall >1h but not effective >1h because internal timing won: {wall_over_1h_effective_not_over_1h:,}",
                f"- Signature-deduped effective >1h calls: {len(over_1h_signatures):,}",
                f"- Signature-deduped effective >1h latency: {ms_to_hours(unique_signature_total_ms):.2f}h",
                f"- Raw minus signature-deduped >1h latency: {ms_to_hours(total_over_1h_ms - unique_signature_total_ms):.2f}h",
                f"- Human/approval-like >1h rows: {len(human_or_approval_calls):,}",
                f"- Human/approval-like >1h latency: {ms_to_hours(human_or_approval_ms):.2f}h",
                "",
                "## Effective latency source",
                "",
                "All Claude calls:",
                "",
                fmt_counter(source_counts),
                "",
                ">1h Claude calls:",
                "",
                fmt_counter(source_over_1h_counts),
                "",
                "## >1h tool names",
                "",
                fmt_counter(tool_over_1h_counts, 20),
                "",
                "## >1h latency by tool name",
                "",
                "\n".join(top_tool_latency_lines) if top_tool_latency_lines else "- none",
                "",
                "## >1h models",
                "",
                fmt_counter(model_over_1h_counts, 20),
                "",
                "## >1h error status",
                "",
                fmt_counter(error_over_1h_counts),
                "",
                "## >1h missing internal timing",
                "",
                fmt_counter(missing_internal_over_1h_counts),
                "",
                "## Duplicate >1h signatures",
                "",
                "\n".join(duplicate_signature_lines) if duplicate_signature_lines else "- none",
                "",
                "## Longest >1h calls",
                "",
                "\n".join(top_latency_lines) if top_latency_lines else "- none",
                "",
                "## Interpretation",
                "",
                "The plotter's effective latency uses `tool_internal_latency_ms` when present, "
                "otherwise `tool_wall_latency_ms`. For Claude, wall latency is the timestamp "
                "gap from the assistant `tool_use` record to the later user `tool_result` "
                "record. When the >1h calls use wall fallback and lack internal timing, they "
                "are real trace-observed gaps, but not proof of runner-measured execution time.",
                "",
                "`AskUserQuestion`, `ExitPlanMode`, and `PushNotification` are human/approval-like "
                "tools, so their >1h values should be interpreted as waiting for user or approval "
                "flow completion. The remaining `Bash`, `Agent`, `Write`, and `Edit` rows are "
                "trace-observed tool-result gaps; without internal duration fields, this audit "
                "cannot prove whether each one was active execution, a suspended command, or a "
                "session-resume delay.",
                "",
                f"Detail CSV: `{detail_path}`",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"wrote {detail_path}")
    print(f"wrote {analysis_path}")
    print(f"claude_gt1h_calls={len(over_1h)}")
    return 0


def fetch_all_dup_keys(con) -> int:
    """All-Claude duplicate ``(session_id, round_id, tool_call_id)`` keys (only non-blank ids).

    Legacy keyed off ``round_id`` falling back to ``trace_key`` when ``round_id`` is blank, but the
    Counter only counts entries with a non-blank ``tool_call_id``; we reproduce that here.
    """
    return int(
        con.execute(
            """
            SELECT count(*) FROM (
                SELECT count(*) AS n
                FROM tool_calls tc JOIN rounds r USING (round_pk)
                WHERE r.provider = 'claude'
                  AND tc.tool_call_id IS NOT NULL AND tc.tool_call_id <> ''
                GROUP BY COALESCE(r.session_id, ''),
                         CASE WHEN COALESCE(r.round_id, '') <> '' THEN r.round_id
                              ELSE COALESCE(r.trace_key, '') END,
                         tc.tool_call_id
                HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
    )


def fetch_all_dup_trace_tool_keys(con) -> int:
    """All-Claude duplicate ``(trace_key, tool_index, tool_call_id)`` keys (non-blank ids only)."""
    return int(
        con.execute(
            """
            SELECT count(*) FROM (
                SELECT count(*) AS n
                FROM tool_calls tc JOIN rounds r USING (round_pk)
                WHERE r.provider = 'claude'
                  AND tc.tool_call_id IS NOT NULL AND tc.tool_call_id <> ''
                GROUP BY COALESCE(r.trace_key, ''),
                         CAST(tc.tool_index AS VARCHAR),
                         tc.tool_call_id
                HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
    )


def fetch_timestamp_mismatches(con) -> int:
    """Calls where the stored wall latency disagrees with the timestamp gap by > 1 ms.

    Legacy: ``abs(round((result_ts - emitted_ts).total_seconds()*1000) - wall_ms) > 1`` over the
    positive-effective-latency Claude calls that have both timestamps and a wall value.
    """
    return int(
        con.execute(
            f"""
            SELECT count(*)
            FROM tool_calls tc JOIN rounds r USING (round_pk)
            WHERE r.provider = 'claude'
              AND ({_EFF}) IS NOT NULL AND ({_EFF}) > 0
              AND tc.emitted_at IS NOT NULL AND tc.result_at IS NOT NULL
              AND tc.tool_wall_latency_ms IS NOT NULL
              AND abs(
                    round((CAST(epoch_us(tc.result_at) AS BIGINT)
                           - CAST(epoch_us(tc.emitted_at) AS BIGINT)) / 1000.0)
                    - tc.tool_wall_latency_ms
                  ) > 1
            """
        ).fetchone()[0]
    )


if __name__ == "__main__":
    raise SystemExit(main())
