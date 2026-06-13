#!/usr/bin/env python3
"""Build the E2B template used by SYFI QA.

The template bakes the public SYFI DuckDB into the sandbox image so request-time
sandboxes do not upload the DuckDB database. Build once, then start QA sandboxes from
the resulting template name/id.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "trace" / "syfi_coding_trace.duckdb"
DEFAULT_TEMPLATE_NAME = "syfi-qa-code-interpreter:latest"
DEFAULT_BASE_TEMPLATE = "code-interpreter-v1"
REMOTE_DB = "/data/syfi_coding_trace.duckdb"


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


def db_summary(db_path: Path) -> dict[str, Any]:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return {
            "rounds": con.execute("SELECT count(*) FROM rounds").fetchone()[0],
            "tool_calls": con.execute("SELECT count(*) FROM tool_calls").fetchone()[0],
            "timing_events": con.execute("SELECT count(*) FROM timing_events").fetchone()[0],
            "providers": con.execute(
                """
                SELECT provider, count(*) AS rounds
                FROM rounds
                GROUP BY provider
                ORDER BY rounds DESC
                """
            ).fetchall(),
        }
    finally:
        con.close()


def build_template(args: argparse.Namespace) -> None:
    require_env("E2B_API_KEY", "E2B_KEY")

    from e2b import Template

    db_path = args.db.resolve()
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")
    try:
        rel_db = db_path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise SystemExit(f"DB must be inside repo root for E2B copy context: {db_path}") from exc

    summary = db_summary(db_path)
    print(
        "local_db: "
        + json.dumps(
            {
                "path": str(db_path),
                "size_mb": round(db_path.stat().st_size / 1024 / 1024, 1),
                **summary,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    verify_py = (
        "import duckdb, json; "
        f"con=duckdb.connect({REMOTE_DB!r}, read_only=True); "
        "print(json.dumps({'rounds': con.execute('select count(*) from rounds').fetchone()[0]})); "
        "con.close()"
    )
    template = (
        Template(file_context_path=REPO_ROOT)
        .from_template(args.base_template)
        .run_cmd("mkdir -p /data /out", user="root")
        .pip_install(["duckdb", "matplotlib"], g=True)
        .copy(str(rel_db), REMOTE_DB, mode=0o444)
        .run_cmd(f"chmod 0444 {shlex.quote(REMOTE_DB)}", user="root")
        .run_cmd(f"python -c {shlex.quote(verify_py)}")
    )

    print(
        "building_template: "
        + json.dumps(
            {
                "name": args.name,
                "base_template": args.base_template,
                "remote_db": REMOTE_DB,
                "cpu_count": args.cpu_count,
                "memory_mb": args.memory_mb,
                "skip_cache": args.skip_cache,
            },
            sort_keys=True,
        ),
        flush=True,
    )

    def on_log(entry: Any) -> None:
        message = getattr(entry, "message", str(entry)).rstrip()
        level = getattr(entry, "level", "info")
        if message:
            print(f"build[{level}]: {message}", flush=True)

    info = Template.build(
        template,
        name=args.name,
        cpu_count=args.cpu_count,
        memory_mb=args.memory_mb,
        skip_cache=args.skip_cache,
        on_build_logs=on_log,
    )
    print(
        "template_built: "
        + json.dumps(
            {
                "template_id": info.template_id,
                "build_id": info.build_id,
                "name": info.name,
                "alias": info.alias,
                "tags": info.tags,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--name", default=DEFAULT_TEMPLATE_NAME)
    parser.add_argument("--base-template", default=DEFAULT_BASE_TEMPLATE)
    parser.add_argument("--cpu-count", type=int, default=1)
    parser.add_argument("--memory-mb", type=int, default=2048)
    parser.add_argument("--skip-cache", action="store_true")
    return parser.parse_args()


def main() -> int:
    build_template(parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
