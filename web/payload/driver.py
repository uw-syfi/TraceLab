#!/usr/bin/env python3
"""In-process re-implementation of run_all.py's dispatcher for the v1 Analyze set.

run_all.py launches each experiment as a *subprocess*; that is impossible in Pyodide
(WASM has no process model). This driver instead imports each experiment module in the
*same* interpreter and calls its ``main()`` after setting up the argv / globals it expects —
exactly what run_all's GLOBAL_SHIM and ``build_command`` do, minus the subprocess.

It is deliberately pure-Python (no Pyodide imports) so it runs unchanged under native
CPython, which lets us verify parity against run_all.py before it ever touches the browser
(``python driver.py <trace.jsonl> <out_dir>``).

Contract (the worker mounts the artifacts tree at ``repo_root`` in MEMFS):
    for event in run(repo_root, input_path):
        event["type"] == "progress" -> {experiment, index, total}
                       == "summary"  -> {data: {merged, claude, codex, ...}}
                       == "figure"   -> {experiment, name, png: bytes}
                       == "error"    -> {experiment, message}

Each experiment writes into its own folder by default; pass ``output_root`` to redirect
outputs (used by the native parity test so it never clobbers the committed figures).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Force a headless backend before any experiment imports matplotlib (style.py imports
# pyplot at module load). Harmless natively; required under Pyodide.
os.environ.setdefault("MPLBACKEND", "Agg")


@dataclass(frozen=True)
class Exp:
    category: str
    name: str
    script: str               # path relative to artifacts/
    style: str                # "-i" | "--input" | "global" | "overview"
    emits: tuple[str, ...] = ()        # literal output PNG filename(s)
    emit_glob: tuple[str, str] | None = None  # (regex, canonical_name): first match -> one figure
    emit_all_glob: str | None = None   # regex: emit EVERY match under its own name (e.g. sessions)
    extra: tuple[str, ...] = ()        # extra CLI args appended for -i/--input styles
    data: str = "jsonl"       # "jsonl" -> -i <trace>; "db" -> --db <materialized trace DuckDB>


# The "regular" figures: one PNG each, distributed round-robin across the worker pool.
REGULAR: list[Exp] = [
    Exp("tool_calls", "tool_latency_distribution", "tool_calls/tool_latency_distribution/plot.py", "-i", ("tool_latency_by_tool.png",), data="db"),
    Exp("tool_calls", "tool_call_counts", "tool_calls/tool_call_counts/plot.py", "-i", ("tool_call_counts.png",), data="db"),
    Exp("llm_generation", "prefix_append_distribution", "llm_generation/prefix_append_distribution/plot.py", "-i", ("prefix_append_distribution.png",), data="db"),
    Exp("llm_generation", "output_tokens", "llm_generation/output_tokens/plot.py", "-i", ("output_tokens_distribution.png",), data="db"),
    Exp("llm_generation", "generation_time_cdf", "llm_generation/generation_time_cdf/plot.py", "-i", ("llm_generation_time_count_cdf_by_provider.png",), data="db"),
    Exp("prefix_cache", "cache_hit_ratio", "prefix_cache/cache_hit_ratio/analyze.py", "-i", ("cache_hit_ratio_histogram.png",), data="db"),
    Exp("prefix_cache", "kv_cache_active_ratio", "prefix_cache/kv_cache_active_ratio/plot.py", "-i", ("kv_cache_active_ratio_by_provider.png",), data="db"),
    Exp("human_in_the_loop", "human_input_wait", "human_in_the_loop/human_input_wait/plot.py", "-i", ("human_input_wait_cdf.png",), data="db"),
]

# Overview reuses the shared trace DuckDB too (data="db" so it's materialized; the overview path
# in _run_one opens it read-only and calls read_summary_from_db — no second parse on worker 0).
OVERVIEW = Exp("trace_facts", "overview_summary", "trace_facts/overview_summary/analyze.py", "overview", data="db")


def _session_exp(offset: int, stride: int, total: int) -> Exp:
    """session_token_steps configured to render this worker's slice of the top `total` sessions.

    Runs on EVERY worker; worker i renders selected[offset::stride], so the (independent,
    deterministic) selection is split across the pool. Each session PNG streams under its own
    name (emit_all_glob) so the UI can show them as a mini-gallery.
    """
    return Exp(
        "session", "session_token_steps", "session/session_token_steps/plot.py", "-i",
        emit_all_glob=r"_token_steps\.png$",
        extra=(
            "--top-sessions", str(total),
            "--context-sessions", "0",
            "--compaction-sessions", "0",
            "--select-offset", str(offset),
            "--select-stride", str(stride),
        ),
        data="db",
    )


def _unique_name(exp: Exp) -> str:
    return "exp_" + re.sub(r"[^0-9A-Za-z]+", "_", f"{exp.category}_{exp.name}")


def _import_module(script_path: Path, unique_name: str):
    """Import a script under a synthetic name (plot.py / analyze.py repeat across folders).

    The module is registered in sys.modules *before* execution so dataclasses and other
    things keyed on ``__module__`` resolve correctly (mirrors run_all's GLOBAL_SHIM).
    """
    spec = importlib.util.spec_from_file_location(unique_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


def _collect_pngs(exp: Exp, out_dir: Path) -> list[tuple[str, bytes]]:
    figures: list[tuple[str, bytes]] = []
    if exp.emit_all_glob is not None:
        for p in sorted(out_dir.glob("*.png")):
            if re.search(exp.emit_all_glob, p.name):
                figures.append((p.name, p.read_bytes()))
    if exp.emit_glob is not None:
        pattern, canonical = exp.emit_glob
        matches = sorted(p for p in out_dir.glob("*.png") if re.search(pattern, p.name))
        if matches:
            figures.append((canonical, matches[0].read_bytes()))
    for name in exp.emits:
        png = out_dir / name
        if png.exists():
            figures.append((name, png.read_bytes()))
    return figures


def _close_figures() -> None:
    """Drop any open matplotlib figures so memory doesn't grow across experiments."""
    plt = sys.modules.get("matplotlib.pyplot")
    if plt is not None:
        plt.close("all")


def _run_one(
    exp: Exp, repo_root: Path, input_path: Path, db_path: Path | None, output_root: Path | None
) -> Iterator[dict]:
    script_path = (repo_root / "artifacts" / exp.script).resolve()
    exp_dir = script_path.parent
    out_dir = (output_root / exp.category / exp.name) if output_root else exp_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    unique = _unique_name(exp)
    saved_argv = sys.argv
    saved_path = list(sys.path)
    try:
        module = _import_module(script_path, unique)

        if exp.style == "overview":
            # Reuse the once-materialized trace DuckDB (avoids a second full parse on worker 0).
            if db_path is not None and hasattr(module, "read_summary_from_db"):
                utils = str((repo_root / "artifacts" / "utils").resolve())
                if utils not in sys.path:
                    sys.path.insert(0, utils)
                import trace_db  # local import: only needed for the db-backed overview path
                con = trace_db.connect(db_path, read_only=True)
                try:
                    data = module.read_summary_from_db(con).as_dict()
                finally:
                    con.close()
            else:
                data = module.read_summary(input_path).as_dict()
            yield {"type": "summary", "data": data}
            return

        if exp.data == "db":
            # db-backed experiment: query the once-materialized trace DuckDB (no per-exp re-parse).
            sys.argv = [str(script_path), "--db", str(db_path), "-o", str(out_dir), *exp.extra]
        elif exp.style == "global":
            # No CLI flag: insert the script's dir on sys.path, set INPUT, call main().
            sys.path.insert(0, str(exp_dir))
            module.INPUT = input_path
            sys.argv = [str(script_path)]
        else:  # "-i" / "--input": output_dir defaults to EXP_DIR; -o pins it explicitly.
            sys.argv = [str(script_path), exp.style, str(input_path), "-o", str(out_dir), *exp.extra]

        rc = module.main()
        if isinstance(rc, int) and rc != 0:
            yield {"type": "error", "experiment": exp.name, "message": f"exit code {rc}"}
            return

        for name, png in _collect_pngs(exp, out_dir):
            yield {"type": "figure", "experiment": exp.name, "name": name, "png": png}
    finally:
        sys.argv = saved_argv
        sys.path[:] = saved_path
        sys.modules.pop(unique, None)
        _close_figures()


def _materialize_db_if_needed(
    repo_root: Path,
    input_path: Path,
    plan: list[Exp],
    output_root: Path | None,
    reuse_db: bool = False,
) -> Path | None:
    """Materialize the trace into a DuckDB once (shared by every ``data="db"`` experiment).

    Runs per worker — each Pyodide worker has its own MEMFS, so each materializes its own copy.
    When ``reuse_db`` is set and the target ``.duckdb`` already exists (e.g. the prepare step pulled
    it from the shared Cache-API DuckDB cache and wrote it here), skip the rebuild and reuse it — this
    is how the shard pool avoids N redundant materializations of the same trace.
    """
    if not any(exp.data == "db" for exp in plan):
        return None
    db_root = output_root if output_root is not None else Path(tempfile.gettempdir()) / "coding_trace_driver"
    db_root.mkdir(parents=True, exist_ok=True)
    db_path = db_root / f"{input_path.stem}.duckdb"
    if reuse_db and db_path.exists():
        return db_path
    utils = str((repo_root / "artifacts" / "utils").resolve())
    if utils not in sys.path:
        sys.path.insert(0, utils)
    import trace_db  # local import: only needed when a db-backed experiment is planned

    trace_db.materialize(input_path, db_path)
    return db_path


def run(
    repo_root,
    input_path,
    *,
    shard_index: int = 0,
    shard_count: int = 1,
    session_total: int = 4,
    include_overview: bool = True,
    output_root=None,
    reuse_db: bool = False,
) -> Iterator[dict]:
    """Run this worker's shard over ``input_path``, yielding progress/summary/figure/error.

    Worker ``i`` of ``shard_count`` renders ``REGULAR[i::shard_count]`` plus its slice of the
    top ``session_total`` session figures (``select-offset i``/``select-stride shard_count``).
    Only worker 0 computes the overview summary. Session figures stream under their own names.
    db-backed experiments share one materialized trace DuckDB (built once up front).
    """
    repo_root = Path(repo_root)
    input_path = Path(input_path)
    output_root = Path(output_root) if output_root is not None else None

    plan: list[Exp] = list(REGULAR[shard_index::shard_count])
    plan.append(_session_exp(shard_index, shard_count, session_total))
    if include_overview:
        plan = [OVERVIEW, *plan]

    db_path = _materialize_db_if_needed(repo_root, input_path, plan, output_root, reuse_db=reuse_db)

    total = len(plan)
    for index, exp in enumerate(plan):
        yield {"type": "progress", "experiment": exp.name, "index": index, "total": total}
        try:
            yield from _run_one(exp, repo_root, input_path, db_path, output_root)
        except Exception as exc:  # one bad experiment shouldn't abort the rest
            yield {
                "type": "error",
                "experiment": exp.name,
                "message": f"{type(exc).__name__}: {exc}",
                "trace": traceback.format_exc(),
            }


# ---------------------------------------------------------------------------
# Native parity harness: `python driver.py <trace.jsonl> [out_dir]`
# Writes figures to out_dir and prints a summary of what ran. Lets us diff against
# run_all.py without a browser.
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python driver.py <trace.jsonl> [out_dir]", file=sys.stderr)
        return 2
    here = Path(__file__).resolve()
    # Native default: the toolkit repo two levels up from web/payload/driver.py.
    repo_root = here.parents[2]
    input_path = Path(argv[0]).resolve()
    out_root = Path(argv[1]).resolve() if len(argv) > 1 else (here.parent / "_native_out")
    out_root.mkdir(parents=True, exist_ok=True)

    summary = None
    figures: list[str] = []
    errors: list[str] = []
    for ev in run(repo_root, input_path, output_root=out_root):
        kind = ev["type"]
        if kind == "progress":
            print(f"[{ev['index'] + 1}/{ev['total']}] {ev['experiment']}", file=sys.stderr)
        elif kind == "summary":
            summary = ev["data"]
        elif kind == "figure":
            dest = out_root / ev["name"]
            dest.write_bytes(ev["png"])
            figures.append(ev["name"])
            print(f"    figure -> {ev['name']} ({len(ev['png'])} bytes)", file=sys.stderr)
        elif kind == "error":
            errors.append(f"{ev['experiment']}: {ev['message']}")
            print(f"    ERROR {ev['experiment']}: {ev['message']}", file=sys.stderr)
            if ev.get("trace"):
                print(ev["trace"], file=sys.stderr)

    print("\n=== driver.py summary ===", file=sys.stderr)
    print(f"figures: {len(figures)} -> {', '.join(figures)}", file=sys.stderr)
    if summary is not None:
        merged = summary.get("merged", {})
        scope = merged.get("scope", {})
        print(
            "overview: rounds={} sessions={} users={}".format(
                scope.get("llm_rounds_total"),
                scope.get("total_sessions"),
                scope.get("distinct_users"),
            ),
            file=sys.stderr,
        )
        print(f"providers: {[k for k in summary if k != 'merged']}", file=sys.stderr)
    if errors:
        print(f"errors: {len(errors)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
