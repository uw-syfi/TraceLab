#!/usr/bin/env python3
"""Dispatcher for coding-trace validators.

Runs validation/audit scripts against a normalized JSONL trace. Validator scripts
write reports next to themselves under `validators/`; plotting and analysis
artifacts remain under `artifacts/` and are run by `artifacts/run_all.py`.

Examples
--------
    uv run python validators/run_all.py
    uv run python validators/run_all.py --list
    uv run python validators/run_all.py --only human_in_the_loop
    uv run python validators/run_all.py --only trace_facts/tool_duplicate_audit
    uv run python validators/run_all.py --input trace/sample.jsonl --dry-run
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

VALIDATORS_DIR = Path(__file__).resolve().parent
REPO_ROOT = VALIDATORS_DIR.parent
DEFAULT_JSONL = REPO_ROOT / "trace" / "llm_round_trace.merged.all_users.jsonl"
DEFAULT_JOBS = 4

GLOBAL_SHIM = (
    "import sys, pathlib, importlib.util as u\n"
    "script = pathlib.Path(sys.argv[1]).resolve()\n"
    "inp = pathlib.Path(sys.argv[2]).resolve()\n"
    "sys.path.insert(0, str(script.parent))\n"
    "spec = u.spec_from_file_location('validator_module', script)\n"
    "mod = u.module_from_spec(spec)\n"
    "sys.modules[spec.name] = mod\n"
    "spec.loader.exec_module(mod)\n"
    "mod.INPUT = inp\n"
    "rc = mod.main()\n"
    "sys.exit(rc if isinstance(rc, int) else 0)\n"
)


@dataclass(frozen=True)
class Validator:
    category: str
    name: str
    script: str  # path relative to validators/
    style: str   # "-i" | "global"


VALIDATORS: list[Validator] = [
    Validator(
        "human_in_the_loop",
        "user_turn_response_audit",
        "human_in_the_loop/user_turn_response_audit/analyze.py",
        "global",
    ),
    Validator(
        "human_in_the_loop",
        "user_turn_gap_audit",
        "human_in_the_loop/user_turn_gap_audit/analyze.py",
        "global",
    ),
    Validator(
        "human_in_the_loop",
        "e2e_formula_check",
        "human_in_the_loop/e2e_formula_check/analyze.py",
        "global",
    ),
    Validator(
        "trace_facts",
        "tool_duplicate_audit",
        "trace_facts/tool_duplicate_audit/analyze.py",
        "-i",
    ),
]


def build_command(item: Validator, python: str, jsonl: Path) -> list[str]:
    script = VALIDATORS_DIR / item.script
    if item.style == "global":
        return [python, "-c", GLOBAL_SHIM, str(script), str(jsonl)]
    if item.style == "-i":
        return [python, str(script), "-i", str(jsonl)]
    raise ValueError(f"unknown validator style: {item.style}")


def display_command(cmd: list[str]) -> str:
    if len(cmd) >= 5 and cmd[1] == "-c":
        parts = [cmd[0], "-c", "<global-shim>", *cmd[3:]]
    else:
        parts = cmd
    return " ".join(shlex.quote(c) for c in parts)


def matches(item: Validator, only: str | None) -> bool:
    if not only:
        return True
    only = only.strip("/")
    full_name = f"{item.category}/{item.name}"
    if "/" in only:
        return full_name.startswith(only)
    return item.category == only or item.name == only


def log_path_for(log_dir: Path, item: Validator) -> Path:
    return log_dir / f"{item.category}__{item.name.replace('/', '_')}.log"


def schedule(selected, *, jobs, python, jsonl, log_dir, stop_on_fail):
    pending = list(selected)
    running: dict[str, tuple] = {}
    results: list[tuple[Validator, int, float]] = []
    stop = False

    def launch(item: Validator):
        cmd = build_command(item, python, jsonl)
        lp = log_path_for(log_dir, item)
        log_f = open(lp, "w", encoding="utf-8")
        print(f"START {item.category}/{item.name}: {display_command(cmd)}", file=sys.stderr)
        start = time.time()
        proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT)
        running[f"{item.category}/{item.name}"] = (proc, item, start, lp, log_f)

    while pending or running:
        while pending and not stop and len(running) < jobs:
            launch(pending.pop(0))

        if not running:
            break

        finished: list[str] = []
        for key, (proc, item, start, lp, log_f) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            elapsed = time.time() - start
            log_f.close()
            print(f"DONE  {item.category}/{item.name}: rc={rc} elapsed={elapsed:.1f}s log={lp}", file=sys.stderr)
            results.append((item, rc, elapsed))
            finished.append(key)
            if rc != 0 and stop_on_fail:
                stop = True
        for key in finished:
            running.pop(key, None)
        if not finished:
            time.sleep(0.25)

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=DEFAULT_JSONL, help="normalized JSONL trace for validators")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS, help=f"max validators to run concurrently (default {DEFAULT_JOBS})")
    parser.add_argument("--only", help="Run one category (e.g. human_in_the_loop) or validator (e.g. trace_facts/tool_duplicate_audit)")
    parser.add_argument("--python", default=sys.executable, help="Python interpreter to launch validators with")
    parser.add_argument("--log-dir", type=Path, default=Path(tempfile.gettempdir()) / "coding_trace_validator_runlogs", help="where to write per-validator console logs")
    parser.add_argument("--list", action="store_true", help="List validators and exit")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop launching new validators after the first failure")
    args = parser.parse_args()

    selected = [item for item in VALIDATORS if matches(item, args.only)]
    if not selected:
        print(f"No validators match --only {args.only!r}", file=sys.stderr)
        return 2

    if args.list:
        for item in selected:
            print(f"{item.category:<18} {item.name:<32} [{item.style}]")
        return 0

    if args.dry_run:
        for item in selected:
            cmd = build_command(item, args.python, args.input)
            print(f"# {item.category}/{item.name}", file=sys.stderr)
            print(f"$ {display_command(cmd)}", file=sys.stderr)
        return 0

    args.log_dir.mkdir(parents=True, exist_ok=True)
    jobs = max(1, args.jobs)
    print(f"Validator dispatcher: {len(selected)} validator(s), up to {jobs} at a time", file=sys.stderr)
    print(f"  jsonl = {args.input}", file=sys.stderr)
    print(f"  logs  = {args.log_dir}", file=sys.stderr)

    results = schedule(
        selected,
        jobs=jobs,
        python=args.python,
        jsonl=args.input,
        log_dir=args.log_dir,
        stop_on_fail=args.stop_on_fail,
    )
    failures = [item for item, rc, _elapsed in results if rc != 0]
    if failures:
        print(f"FAILED: {len(failures)} validator(s)", file=sys.stderr)
        return 1
    print("All validators passed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
