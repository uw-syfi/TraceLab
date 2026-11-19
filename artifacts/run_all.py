#!/usr/bin/env python3
"""Dispatcher for the coding-trace analysis experiments.

Runs every experiment (or a category / single experiment) against a chosen trace,
up to ``--jobs`` at a time (default 16). Each experiment writes its outputs into its
own folder; this script just invokes them with the right input flag, captures each
one's console output to a per-experiment log file, and reports pass/fail + duration.

The registry below is the manifest of all experiments and how each is launched.
DB-backed experiments receive ``--db``; ``csv_export`` additionally needs an output,
``overview_summary`` prints to stdout (captured to ``summary.json``),
``timing_fit/build_trace`` derives the local timing CSV from the DuckDB trace, and
dependency-aware multi-step artifacts such as
``bash_command_breakdown`` are scheduled in their required order.

Examples
--------
    # run everything on the released DuckDB, 16 at a time (defaults)
    uv run python artifacts/run_all.py

    # rebuild a compact DuckDB from JSONL first, then run everything against it
    uv run python artifacts/run_all.py --build-db --input trace/syfi_coding_trace.jsonl.gz --db trace/syfi_coding_trace.duckdb

    # serial, or a different width
    uv run python artifacts/run_all.py --jobs 1
    uv run python artifacts/run_all.py --jobs 32

    # just list what would run
    uv run python artifacts/run_all.py --list

    # one category, or one experiment
    uv run python artifacts/run_all.py --only tool_calls
    uv run python artifacts/run_all.py --only prefix_cache/cache_hit_ratio

    # run against a different prebuilt DuckDB, and a dry run
    uv run python artifacts/run_all.py --db trace/syfi_coding_trace.duckdb
    uv run python artifacts/run_all.py --dry-run
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ARTIFACTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = ARTIFACTS_DIR.parent
DEFAULT_JSONL = Path("trace") / "syfi_coding_trace.jsonl.gz"
DEFAULT_DB = Path("trace") / "syfi_coding_trace.duckdb"
DEFAULT_TIMING = ARTIFACTS_DIR / "llm_generation" / "timing_fit" / "timing_fit_trace.csv"
DEFAULT_JOBS = 16
TIMING_BUILD_NAME = "timing_fit/build_trace"

# Shared DuckDB foundation: one experiment materializes and compacts the trace once
# (artifacts/utils/trace_db.py), and every db-backed experiment (`data="db"`) runs against it via
# `--db`, depending on this build.
BUILD_DB_NAME = "build"
TRACE_DB_SCRIPT = "utils/trace_db.py"


def repo_relative(path: Path) -> Path:
    """Resolve relative CLI paths against the repository root."""
    return path if path.is_absolute() else REPO_ROOT / path

@dataclass(frozen=True)
class Experiment:
    category: str
    name: str
    script: str           # path relative to artifacts/
    style: str            # "-i" | "--input" | "direct" | "io" | "stdout" | "none" | "timing-build" | "db-build"
    data: str = "jsonl"   # "jsonl" -> merged trace, "timing" -> timing CSV, "db" -> trace DuckDB (--db)
    after: str | tuple[str, ...] = ""  # experiment name(s) that must finish first


def dependencies(experiment: Experiment) -> tuple[str, ...]:
    if isinstance(experiment.after, str):
        return (experiment.after,) if experiment.after else ()
    return experiment.after


# Order is informational; scheduling is by dependency + free worker slots.
# Almost every experiment is now db-backed (``data="db"``, ``after=BUILD_DB_NAME``): it queries the
# once-materialized trace DuckDB instead of re-parsing the JSONL. The exceptions are the build step
# itself and the timing-CSV consumers (they read ``timing_fit_trace.csv``, produced by the
# db-backed ``timing-build`` collector, not the trace directly).
EXPERIMENTS: list[Experiment] = [
    # shared foundation: materialize the trace DuckDB once; db-backed experiments depend on it ----
    Experiment("trace_db", BUILD_DB_NAME, TRACE_DB_SCRIPT, "db-build"),
    # llm_generation -------------------------------------------------------
    Experiment("llm_generation", "token_length_distribution", "llm_generation/token_length_distribution/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "prefix_append_distribution", "llm_generation/prefix_append_distribution/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "append_by_prefix_bin", "llm_generation/append_by_prefix_bin/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "output_tokens", "llm_generation/output_tokens/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "output_attribution_schematic", "llm_generation/output_attribution_schematic/plot.py", "none"),
    Experiment("llm_generation", "context_decode_speed_scatter", "llm_generation/context_decode_speed_scatter/plot.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "generation_time_cdf", "llm_generation/generation_time_cdf/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "adjusted_prefix_append", "llm_generation/adjusted_prefix_append/plot.py", "--input", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "output_append_assignment", "llm_generation/output_append_assignment/plot.py", "--input", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", TIMING_BUILD_NAME, "llm_generation/timing_fit/collect_timing_fit_trace.py", "timing-build", "db", after=BUILD_DB_NAME),
    Experiment("llm_generation", "append_vs_prefix_latency", "llm_generation/append_vs_prefix_latency/analyze.py", "-i", "timing", after=TIMING_BUILD_NAME),
    Experiment("llm_generation", "timing_fit", "llm_generation/timing_fit/fit_timing_trace.py", "-i", "timing", after=TIMING_BUILD_NAME),
    Experiment("llm_generation", "timing_feature_ambiguity", "llm_generation/timing_feature_ambiguity/analyze.py", "-i", "timing", after="timing_fit"),
    Experiment("llm_generation", "timing_feature_ambiguity/build_summary", "llm_generation/timing_feature_ambiguity/build_summary.py", "none", "jsonl", after="timing_feature_ambiguity"),
    Experiment("llm_generation", "token_spindles", "llm_generation/token_spindles/plot.py", "direct", "db", after=BUILD_DB_NAME),
    # tool_calls -----------------------------------------------------------
    Experiment("tool_calls", "tool_latency_distribution", "tool_calls/tool_latency_distribution/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "tool_call_counts", "tool_calls/tool_call_counts/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "tool_time_by_kind", "tool_calls/tool_time_by_kind/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "tool_category_distribution", "tool_calls/tool_category_distribution/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "claude_long_tool_calls", "tool_calls/claude_long_tool_calls/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "codex_wall_internal_gap", "tool_calls/codex_wall_internal_gap/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    # bash_command_breakdown is a dependency-ordered artifact pipeline. The exporter must finish
    # before its command_calls.jsonl consumers, while command_stats also needs tool_time_by_kind.
    Experiment("tool_calls", "bash_command_breakdown/classify_commands", "tool_calls/bash_command_breakdown/classify_commands.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("tool_calls", "bash_command_breakdown/popularity", "tool_calls/bash_command_breakdown/analyze_popularity.py", "none", after="bash_command_breakdown/classify_commands"),
    Experiment("tool_calls", "bash_command_breakdown/executable_runtime", "tool_calls/bash_command_breakdown/analyze_executable_runtime.py", "none", after="bash_command_breakdown/classify_commands"),
    Experiment("tool_calls", "bash_command_breakdown/executable_runtime_updated", "tool_calls/bash_command_breakdown/analyze_executable_runtime_updated.py", "-i", "db", after="bash_command_breakdown/classify_commands"),
    Experiment(
        "tool_calls",
        "bash_command_breakdown/command_stats",
        "tool_calls/bash_command_breakdown/analyze_command_stats.py",
        "none",
        after=("bash_command_breakdown/classify_commands", "tool_time_by_kind"),
    ),
    # prefix_cache ---------------------------------------------------------
    Experiment("prefix_cache", "cache_hit_ratio", "prefix_cache/cache_hit_ratio/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "redundant_prefill", "prefix_cache/redundant_prefill/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "human_idle_cache_counterfactual", "prefix_cache/human_idle_cache_counterfactual/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "cache_hit_idle_relationship/gap", "prefix_cache/cache_hit_idle_relationship/cache_hit_idle_gap_analysis.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "cache_hit_idle_relationship/scatters", "prefix_cache/cache_hit_idle_relationship/plot_user_wait_time_vs_hit_rate.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "cache_replay", "prefix_cache/cache_replay/analyze.py", "--input", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "kv_cache_active_ratio", "prefix_cache/kv_cache_active_ratio/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("prefix_cache", "eviction_tradeoff", "prefix_cache/eviction_tradeoff/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    # synthetic schematic — takes no trace input, so no build-db dependency.
    Experiment("prefix_cache", "timeout_miss_pattern", "prefix_cache/timeout_miss_pattern/plot.py", "none"),
    # human_in_the_loop ----------------------------------------------------
    Experiment("human_in_the_loop", "human_input_wait", "human_in_the_loop/human_input_wait/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("human_in_the_loop", "user_turn_response_time", "human_in_the_loop/user_turn_response_time/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("human_in_the_loop", "user_turn_decomposition", "human_in_the_loop/user_turn_decomposition/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    # trace_facts ----------------------------------------------------------
    Experiment("trace_facts", "overview_summary", "trace_facts/overview_summary/analyze.py", "stdout", "db", after=BUILD_DB_NAME),
    Experiment("trace_facts", "csv_export", "trace_facts/csv_export/convert.py", "io", "db", after=BUILD_DB_NAME),
    # session --------------------------------------------------------------
    Experiment("session", "session_internal_counts", "session/session_internal_counts/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("session", "session_compaction_counts", "session/session_compaction_counts/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("session", "session_timing_distribution", "session/session_timing_distribution/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("session", "session_token_steps", "session/session_token_steps/plot.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("session", "total_input_growth", "session/total_input_growth/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    Experiment("session", "session_cost_distribution", "session/session_cost_distribution/analyze.py", "-i", "db", after=BUILD_DB_NAME),
    # subagents ------------------------------------------------------------
    Experiment("subagents", "adoption", "subagents/adoption/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("subagents", "workload", "subagents/workload/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("subagents", "tokens_and_reports", "subagents/tokens_and_reports/analyze.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("subagents", "tool_usage", "subagents/tool_usage/plot.py", "direct", "db", after=BUILD_DB_NAME),
    Experiment("subagents", "timing", "subagents/timing/analyze.py", "direct", "db", after=BUILD_DB_NAME),
]

# Return codes used internally for non-process outcomes.
RC_SKIPPED = -2  # a dependency failed, so this experiment never ran


def build_command(exp: Experiment, python: str, jsonl: Path, timing: Path, db: Path) -> tuple[list[str], Path | None]:
    """Return (argv, stdout_redirect_path) for one experiment.

    db-backed experiments (``data="db"``) read the once-materialized trace DuckDB via ``--db``
    instead of re-parsing the JSONL; their *output* handling still follows ``style`` (a plot/CSV
    experiment writes to its own folder, ``stdout`` still redirects ``--json``, ``io`` still pins
    the CSV path, ``timing-build`` still pins the timing CSV). So we pick the input args from
    ``data`` and the output args from ``style``.
    """
    script = ARTIFACTS_DIR / exp.script
    inp = timing if exp.data == "timing" else jsonl
    script_dir = script.parent

    if exp.style == "db-build":
        # Materialize the trace into the shared DuckDB (artifacts/utils/trace_db.py <jsonl> <db>).
        return [python, str(script), str(jsonl), str(db)], None

    # Source args: db-backed → --db <db>; otherwise the style's own input flag (filled in below).
    src = ["--db", str(db)] if exp.data == "db" else None

    if exp.style == "none":
        return [python, str(script)], None
    if exp.style == "direct":
        if src is None:
            raise ValueError(f"direct-style experiment must be DB-backed: {exp.name}")
        return [python, str(script), *src], None
    if exp.style == "io":
        out = script_dir / "coding_trace.csv"
        return [python, str(script), *(src or ["-i", str(inp)]), "-o", str(out)], None
    if exp.style == "stdout":
        return [python, str(script), *(src or ["-i", str(inp)]), "--json"], script_dir / "summary.json"
    if exp.style == "timing-build":
        return [python, str(script), *(src or ["-i", str(jsonl)]), "-o", str(timing)], None
    # "-i" or "--input": db-backed passes --db; otherwise the declared input flag + path.
    if src is not None:
        return [python, str(script), *src], None
    return [python, str(script), exp.style, str(inp)], None


def display_command(exp: Experiment, cmd: list[str], redirect: Path | None) -> str:
    shown = " ".join(shlex.quote(command_part) for command_part in cmd)
    return shown + (f"  > {redirect}" if redirect else "")


def matches(exp: Experiment, only: str | None) -> bool:
    if not only:
        return True
    only = only.strip("/")
    if "/" in only:
        cat, _, rest = only.partition("/")
        return exp.category == cat and exp.name.startswith(rest)
    return exp.category == only or exp.name == only


def select_with_dependencies(only: str | None, *, skip_timing_build: bool, include_db_build: bool) -> list[Experiment]:
    """Select matching experiments and add required prerequisites."""
    by_name = {exp.name: exp for exp in EXPERIMENTS}
    selected: dict[str, Experiment] = {
        exp.name: exp
        for exp in EXPERIMENTS
        if matches(exp, only)
        and not (not include_db_build and exp.name == BUILD_DB_NAME)
        and not (skip_timing_build and exp.name == TIMING_BUILD_NAME)
    }

    changed = True
    while changed:
        changed = False
        for exp in list(selected.values()):
            for dependency_name in dependencies(exp):
                if skip_timing_build and dependency_name == TIMING_BUILD_NAME:
                    continue
                dependency = by_name.get(dependency_name)
                if (
                    dependency is not None
                    and dependency.name == BUILD_DB_NAME
                    and not include_db_build
                ):
                    continue
                if dependency is None or dependency.name in selected:
                    continue
                selected[dependency.name] = dependency
                changed = True

    return [exp for exp in EXPERIMENTS if exp.name in selected]


def log_path_for(log_dir: Path, exp: Experiment) -> Path:
    return log_dir / f"{exp.category}__{exp.name.replace('/', '_')}.log"


def schedule(selected, *, jobs, python, jsonl, timing, db, log_dir, stop_on_fail):
    """Run selected experiments with a bounded worker pool, honoring `after` deps."""
    selected_names = {e.name for e in selected}
    pending = list(selected)
    running: dict[str, tuple] = {}        # name -> (popen, exp, start, log_path, handles)
    completed: dict[str, int] = {}        # name -> rc
    results: list[tuple[Experiment, int, float]] = []
    stop = False

    def dep_state(experiment: Experiment):
        """True = ready, False = dependency failed, None = waiting on dependency."""
        selected_dependencies = [
            dependency_name
            for dependency_name in dependencies(experiment)
            if dependency_name in selected_names
        ]
        if not selected_dependencies:
            return True
        if any(dependency_name not in completed for dependency_name in selected_dependencies):
            return None
        return all(completed[dependency_name] == 0 for dependency_name in selected_dependencies)

    def launch(e: Experiment):
        cmd, redirect = build_command(e, python, jsonl, timing, db)
        lp = log_path_for(log_dir, e)
        log_f = open(lp, "w", encoding="utf-8")
        handles = [log_f]
        if redirect is not None:
            out_f = open(redirect, "w", encoding="utf-8")
            handles.append(out_f)
            popen = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=out_f, stderr=log_f)
        else:
            popen = subprocess.Popen(cmd, cwd=REPO_ROOT, stdout=log_f, stderr=subprocess.STDOUT)
        return popen, lp, handles

    total = len(selected)
    while pending or running:
        # Launch as many ready experiments as free slots allow.
        for e in list(pending):
            if stop or len(running) >= jobs:
                break
            state = dep_state(e)
            if state is None:
                continue  # still waiting on its dependency
            pending.remove(e)
            if state is False:
                completed[e.name] = RC_SKIPPED
                results.append((e, RC_SKIPPED, 0.0))
                failed_dependencies = ", ".join(
                    dependency_name
                    for dependency_name in dependencies(e)
                    if completed.get(dependency_name) != 0
                )
                print(
                    f"[skip] {e.category}/{e.name} "
                    f"(dependency failed: {failed_dependencies})",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            popen, lp, handles = launch(e)
            running[e.name] = (popen, e, time.monotonic(), lp, handles)
            print(f"[start {len(completed) + len(running)}/{total}] {e.category}/{e.name}  ({len(running)} running)  log={lp}", file=sys.stderr, flush=True)

        # Reap any finished workers.
        finished = [(n, p.poll()) for n, (p, *_ ) in running.items()]
        finished = [(n, rc) for n, rc in finished if rc is not None]
        for name, rc in finished:
            popen, e, start, lp, handles = running.pop(name)
            for h in handles:
                try:
                    h.close()
                except Exception:
                    pass
            dur = time.monotonic() - start
            completed[name] = rc
            results.append((e, rc, dur))
            mark = "OK  " if rc == 0 else "FAIL"
            print(f"[{mark}] {e.category}/{e.name}  rc={rc}  {dur:.1f}s", file=sys.stderr, flush=True)
            if rc != 0 and stop_on_fail:
                stop = True

        if stop and not running:
            break
        if not finished:
            time.sleep(0.3)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"DuckDB trace used by db-backed experiments (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_JSONL,
        help=f"JSONL trace used only with --build-db or jsonl-backed experiments (default: {DEFAULT_JSONL})",
    )
    parser.add_argument(
        "--build-db",
        action="store_true",
        help="Materialize --input into --db before running db-backed experiments",
    )
    parser.add_argument(
        "--timing-input",
        type=Path,
        default=None,
        help=(
            "Existing timing-segment CSV for timing experiments. If omitted, "
            f"run_all builds and uses {DEFAULT_TIMING} from --db."
        ),
    )
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS, help=f"max experiments to run concurrently (default {DEFAULT_JOBS})")
    parser.add_argument("--only", help="Run one category (e.g. tool_calls) or experiment (e.g. tool_calls/tool_call_counts)")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter to launch experiments with")
    parser.add_argument("--log-dir", type=Path, default=Path(tempfile.gettempdir()) / "coding_trace_runlogs", help="where to write per-experiment console logs")
    parser.add_argument("--list", action="store_true", help="List experiments and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop launching new experiments after the first failure")
    args = parser.parse_args()

    jobs = max(1, args.jobs)
    timing_input = args.timing_input or DEFAULT_TIMING
    skip_timing_build = args.timing_input is not None
    selected = select_with_dependencies(
        args.only,
        skip_timing_build=skip_timing_build,
        include_db_build=args.build_db,
    )
    if not selected:
        print(f"No experiments match --only {args.only!r}", file=sys.stderr)
        return 2

    if args.list:
        selected_names = {e.name for e in selected}
        for e in selected:
            selected_dependencies = [
                dependency_name
                for dependency_name in dependencies(e)
                if dependency_name in selected_names
            ]
            dep = (
                f"  after={','.join(selected_dependencies)}"
                if selected_dependencies
                else ""
            )
            print(f"{e.category:<18} {e.name:<38} [{e.style}, {e.data}]{dep}")
        return 0

    db_path = args.db
    if args.dry_run:
        for e in selected:
            cmd, redirect = build_command(e, args.python, args.input, timing_input, db_path)
            print(f"# {e.category}/{e.name}", file=sys.stderr)
            print(f"$ {display_command(e, cmd, redirect)}", file=sys.stderr)
        return 0

    db_fs_path = repo_relative(db_path)
    if not args.build_db and any(e.data == "db" for e in selected) and not db_fs_path.exists():
        print(f"Missing DuckDB trace: {db_path}", file=sys.stderr)
        print("Download the released database first:", file=sys.stderr)
        print("  mkdir -p trace", file=sys.stderr)
        print("  curl -L --fail -o trace/syfi_coding_trace.duckdb \\", file=sys.stderr)
        print("    https://github.com/uw-syfi/TraceLab/releases/latest/download/syfi_coding_trace.duckdb", file=sys.stderr)
        return 2

    if args.build_db:
        jsonl_fs_path = repo_relative(args.input)
        if not jsonl_fs_path.exists():
            print(f"Missing JSONL input for --build-db: {args.input}", file=sys.stderr)
            return 2

    args.log_dir.mkdir(parents=True, exist_ok=True)
    print(f"Dispatcher: {len(selected)} experiment(s), up to {jobs} at a time", file=sys.stderr)
    if args.build_db:
        print(f"  jsonl   = {args.input}", file=sys.stderr)
    print(f"  timing  = {timing_input}", file=sys.stderr)
    print(f"  db      = {db_path}", file=sys.stderr)
    print(f"  logs    = {args.log_dir}", file=sys.stderr)

    wall_start = time.monotonic()
    results = schedule(
        selected,
        jobs=jobs,
        python=args.python,
        jsonl=args.input,
        timing=timing_input,
        db=db_path,
        log_dir=args.log_dir,
        stop_on_fail=args.stop_on_fail,
    )
    wall = time.monotonic() - wall_start

    ran = [r for r in results if r[1] != RC_SKIPPED]
    failed = [r for r in results if r[1] != 0]
    print("\n" + "=" * 64, file=sys.stderr)
    print(f"Summary: {len(ran) - len([r for r in failed if r[1] != RC_SKIPPED])}/{len(ran)} ok in {wall:.1f}s wall", file=sys.stderr)
    for e, rc, dur in sorted(results, key=lambda r: (r[0].category, r[0].name)):
        mark = "ok  " if rc == 0 else ("skip" if rc == RC_SKIPPED else "FAIL")
        print(f"  {mark}  {e.category}/{e.name:<40} {dur:7.1f}s  ({log_path_for(args.log_dir, e).name})", file=sys.stderr)
    if failed:
        print("FAILED: " + ", ".join(f"{e.category}/{e.name}" for e, _, _ in failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
