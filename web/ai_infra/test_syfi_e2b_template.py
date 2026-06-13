#!/usr/bin/env python3
"""Test a prebuilt SYFI QA E2B template.

This test creates a sandbox from the template, verifies that the DuckDB is already
available at /data/syfi_coding_trace.duckdb, runs a small aggregate query, writes a
plot to /out, and reports inline image/artifact metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from pathlib import Path
from typing import Any


DEFAULT_TEMPLATE = os.environ.get("E2B_SYFI_TEMPLATE", "syfi-qa-code-interpreter:latest")
REMOTE_DB = "/data/syfi_coding_trace.duckdb"
REMOTE_OUT = "/out"


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def require_env(*names: str) -> str:
    value = env_value(*names)
    if not value:
        raise SystemExit(f"{' or '.join(names)} is not set")
    return value


def summarize_execution(execution: Any) -> dict[str, Any]:
    stdout = [getattr(msg, "line", str(msg)) for msg in getattr(execution.logs, "stdout", [])]
    stderr = [getattr(msg, "line", str(msg)) for msg in getattr(execution.logs, "stderr", [])]
    results = []
    for item in execution.results:
        results.append(
            {
                "text": getattr(item, "text", None),
                "json": getattr(item, "json", None),
                "png_bytes_approx": (
                    round(len(item.png) * 3 / 4) if getattr(item, "png", None) else None
                ),
                "svg_chars": len(item.svg) if getattr(item, "svg", None) else None,
            }
        )
    error = execution.error
    return {
        "stdout": stdout,
        "stderr": stderr,
        "error": None
        if error is None
        else {
            "name": error.name,
            "value": error.value,
            "traceback": error.traceback,
        },
        "results": results,
    }


def create_sandbox(args: argparse.Namespace):
    from e2b_code_interpreter import Sandbox

    print(f"e2b: creating sandbox from template {args.template}", flush=True)
    return Sandbox.create(
        template=args.template,
        timeout=args.sandbox_timeout,
        allow_internet_access=not args.no_internet,
        metadata={
            "app": "coding-trace",
            "purpose": "syfi-qa-template-test",
            "repo": "coding_trace_refactor",
            "script": "web/ai_infra/test_syfi_e2b_template.py",
        },
    )


def run_template_test(args: argparse.Namespace) -> dict[str, Any]:
    require_env("E2B_API_KEY", "E2B_KEY")
    sandbox = create_sandbox(args)
    try:
        code = f"""
        import json
        import os
        import sys

        db_path = {REMOTE_DB!r}
        out_dir = {REMOTE_OUT!r}
        os.makedirs(out_dir, exist_ok=True)

        print("python", sys.version.split()[0])
        print("db_exists", os.path.exists(db_path))
        print("db_size", os.path.getsize(db_path) if os.path.exists(db_path) else None)

        import duckdb
        print("duckdb", duckdb.__version__)
        con = duckdb.connect(db_path, read_only=True)
        con.execute("SET memory_limit='1GB'")
        con.execute("SET threads=2")

        provider_rows = con.execute('''
            SELECT provider, count(*) AS rounds
            FROM rounds
            GROUP BY provider
            ORDER BY rounds DESC
        ''').fetchall()
        top_tools = con.execute('''
            SELECT tool_name, count(*) AS calls
            FROM tool_calls
            GROUP BY tool_name
            ORDER BY calls DESC
            LIMIT 5
        ''').fetchall()
        print("provider_rounds", json.dumps(provider_rows))
        print("top_tools", json.dumps(top_tools))

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.bar([r[0] for r in provider_rows], [r[1] for r in provider_rows])
        ax.set_title("SYFI rounds by provider")
        ax.set_ylabel("rounds")
        fig.tight_layout()
        fig.savefig(out_dir + "/provider_rounds.png", dpi=160)
        plt.show()
        con.close()
        """
        execution = sandbox.run_code(textwrap.dedent(code), timeout=120, request_timeout=180)
        summary = summarize_execution(execution)
        try:
            artifacts = sandbox.files.list(REMOTE_OUT)
            summary["artifacts"] = [
                {"path": a.path, "size": a.size, "type": str(a.type)} for a in artifacts
            ]
        except Exception as exc:
            summary["artifact_error"] = f"{type(exc).__name__}: {exc}"
        print("template_test: " + json.dumps(summary, sort_keys=True), flush=True)
        if summary["error"] is not None:
            raise SystemExit(1)
        return summary
    finally:
        sandbox.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument("--sandbox-timeout", type=int, default=600)
    parser.add_argument("--no-internet", action="store_true")
    return parser.parse_args()


def main() -> int:
    run_template_test(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
