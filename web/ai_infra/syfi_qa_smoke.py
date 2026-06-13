#!/usr/bin/env python3
"""Smoke tests for the planned SYFI QA stack.

This script has three useful modes:

1. Always validate the local SYFI DuckDB and print small counts.
2. With --e2b, create an E2B code-interpreter sandbox and run a Python smoke test.
3. With --openrouter, ask an OpenRouter model to call run_python. By default the tool
   result is canned; with --e2b-tool it executes the model's code in E2B.

Secrets are read from E2B_API_KEY / E2B_KEY and OPENROUTER_API_KEY / OPENROUTE_KEY
but never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "trace" / "syfi_coding_trace.duckdb"
REMOTE_DB = "/data/syfi_coding_trace.duckdb"
REMOTE_OUT = "/out"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_TEMPERATURE = float(os.environ.get("SYFI_LLM_TEMPERATURE", 1.0))
DEFAULT_TOP_P = float(os.environ.get("SYFI_LLM_TOP_P", 0.95))
DEFAULT_TOP_K = int(os.environ.get("SYFI_LLM_TOP_K", 20))
DEFAULT_MIN_P = float(os.environ.get("SYFI_LLM_MIN_P", 0.0))
DEFAULT_PRESENCE_PENALTY = float(os.environ.get("SYFI_LLM_PRESENCE_PENALTY", 1.5))
DEFAULT_REPETITION_PENALTY = float(os.environ.get("SYFI_LLM_REPETITION_PENALTY", 1.0))


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


def print_json(label: str, value: Any) -> None:
    print(f"{label}: {json.dumps(value, sort_keys=True)}", flush=True)


def default_sampling_params() -> dict[str, Any]:
    return {
        "temperature": DEFAULT_TEMPERATURE,
        "top_p": DEFAULT_TOP_P,
        "top_k": DEFAULT_TOP_K,
        "min_p": DEFAULT_MIN_P,
        "presence_penalty": DEFAULT_PRESENCE_PENALTY,
        "repetition_penalty": DEFAULT_REPETITION_PENALTY,
    }


def local_db_check(db_path: Path) -> dict[str, Any]:
    import duckdb

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        tables = {
            name: con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("rounds", "tool_calls", "timing_events")
        }
        providers = con.execute(
            """
            SELECT provider, count(*) AS rounds
            FROM rounds
            GROUP BY provider
            ORDER BY rounds DESC
            """
        ).fetchall()
        top_tools = con.execute(
            """
            SELECT tool_name, count(*) AS calls
            FROM tool_calls
            GROUP BY tool_name
            ORDER BY calls DESC
            LIMIT 5
            """
        ).fetchall()
    finally:
        con.close()

    result = {
        "db_path": str(db_path),
        "file_size_mb": round(db_path.stat().st_size / 1024 / 1024, 1),
        "tables": tables,
        "providers": providers,
        "top_tools": top_tools,
    }
    print_json("local_db", result)
    return result


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
    err = execution.error
    return {
        "stdout": stdout,
        "stderr": stderr,
        "error": None
        if err is None
        else {
            "name": err.name,
            "value": err.value,
            "traceback": err.traceback,
        },
        "results": results,
    }


def create_e2b_sandbox(args: argparse.Namespace):
    from e2b_code_interpreter import Sandbox

    kwargs: dict[str, Any] = {
        "timeout": args.sandbox_timeout,
        "allow_internet_access": not args.e2b_no_internet,
        "metadata": {
            "app": "coding-trace",
            "purpose": "syfi-qa-smoke",
            "repo": "coding_trace_refactor",
            "script": "web/ai_infra/syfi_qa_smoke.py",
        },
    }
    if args.e2b_template:
        kwargs["template"] = args.e2b_template
    print("e2b: creating sandbox", flush=True)
    return Sandbox.create(**kwargs)


def maybe_install_e2b_deps(sandbox: Any) -> None:
    print("e2b: installing duckdb/matplotlib in sandbox", flush=True)
    result = sandbox.commands.run(
        "python -m pip install duckdb matplotlib",
        timeout=240,
        request_timeout=300,
    )
    if getattr(result, "exit_code", 0) != 0:
        print(getattr(result, "stdout", ""), flush=True)
        print(getattr(result, "stderr", ""), file=sys.stderr, flush=True)
        raise SystemExit(f"pip install failed with exit code {result.exit_code}")


def upload_db_to_e2b(sandbox: Any, db_path: Path) -> None:
    print(f"e2b: uploading {db_path} -> {REMOTE_DB}", flush=True)
    sandbox.files.make_dir("/data")
    with db_path.open("rb") as fh:
        sandbox.files.write(
            REMOTE_DB,
            fh,
            request_timeout=900,
            use_octet_stream=True,
        )


def e2b_smoke(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    require_env("E2B_API_KEY", "E2B_KEY")
    sandbox = create_e2b_sandbox(args)
    try:
        sandbox.files.make_dir(REMOTE_OUT)
        if args.install_deps:
            maybe_install_e2b_deps(sandbox)
        if args.upload_db:
            upload_db_to_e2b(sandbox, args.db)

        db_line = f"db_path = {REMOTE_DB!r}" if args.upload_db or args.e2b_template else "db_path = None"
        code = f"""
        import json
        import os
        import sys

        os.makedirs({REMOTE_OUT!r}, exist_ok=True)
        {db_line}
        print("python", sys.version.split()[0])

        try:
            import duckdb
            print("duckdb", duckdb.__version__)
        except Exception as exc:
            print("duckdb_import_error", type(exc).__name__, str(exc))
            raise

        if db_path:
            con = duckdb.connect(db_path, read_only=True)
            con.execute("SET memory_limit='1GB'")
            con.execute("SET threads=2")
            rows = con.execute('''
                SELECT provider, count(*) AS rounds
                FROM rounds
                GROUP BY provider
                ORDER BY rounds DESC
            ''').fetchall()
            print("provider_rounds", json.dumps(rows))

            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            labels = [r[0] for r in rows]
            values = [r[1] for r in rows]
            fig, ax = plt.subplots(figsize=(6, 3.5))
            ax.bar(labels, values)
            ax.set_title("SYFI rounds by provider")
            ax.set_ylabel("rounds")
            fig.tight_layout()
            fig.savefig({REMOTE_OUT!r} + "/provider_rounds.png", dpi=160)
            plt.show()
            con.close()
        """
        execution = sandbox.run_code(textwrap.dedent(code), timeout=120, request_timeout=180)
        summary = summarize_execution(execution)
        if args.upload_db or args.e2b_template:
            artifacts = sandbox.files.list(REMOTE_OUT)
            summary["artifacts"] = [
                {"path": a.path, "size": a.size, "type": str(a.type)} for a in artifacts
            ]
        print_json("e2b_smoke", summary)
        return sandbox, summary
    except Exception:
        try:
            sandbox.kill()
        finally:
            pass
        raise


def openrouter_chat(
    *,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 800,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    payload.update(default_sampling_params())
    if tools is not None:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
        payload["parallel_tool_calls"] = False

    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/uw-syfi/TraceLab",
            "X-Title": "SyFI Trace Atlas QA Smoke",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"OpenRouter HTTP {exc.code}: {body}") from exc


def run_python_tool_canned(_args: dict[str, Any]) -> dict[str, Any]:
    return {
        "stdout": [
            "provider_rounds [[\"codex\", 216823], [\"claude\", 140338]]",
            "tool execution was canned by --openrouter without --e2b-tool",
        ],
        "stderr": [],
        "artifacts": [],
    }


def run_python_tool_e2b(sandbox: Any, code: str) -> dict[str, Any]:
    execution = sandbox.run_code(code, timeout=120, request_timeout=180)
    summary = summarize_execution(execution)
    try:
        artifacts = sandbox.files.list(REMOTE_OUT)
        summary["artifacts"] = [
            {"path": a.path, "size": a.size, "type": str(a.type)} for a in artifacts
        ]
    except Exception as exc:
        summary["artifact_error"] = f"{type(exc).__name__}: {exc}"
    return summary


def openrouter_tool_smoke(args: argparse.Namespace) -> None:
    api_key = require_env("OPENROUTER_API_KEY", "OPENROUTE_KEY")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run_python",
                "description": (
                    "Run Python code against the public SYFI DuckDB trace. "
                    f"The database is at {REMOTE_DB}; write plots/files to {REMOTE_OUT}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python code to execute in the sandbox.",
                        }
                    },
                    "required": ["code"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a data analyst for the public SYFI coding-trace DuckDB. "
                "Use run_python for any data question. Query DuckDB first and only "
                "materialize small aggregated results. Do not read environment variables "
                "or access the network. The DuckDB path is /data/syfi_coding_trace.duckdb. "
                "Connect with duckdb.connect('/data/syfi_coding_trace.duckdb', read_only=True). "
                "Available tables are rounds, tool_calls, and timing_events. "
                "For round counts by provider, query: "
                "SELECT provider, count(*) AS rounds FROM rounds GROUP BY provider ORDER BY rounds DESC."
            ),
        },
        {
            "role": "user",
            "content": args.question,
        },
    ]

    sandbox = None
    if args.e2b_tool:
        require_env("E2B_API_KEY", "E2B_KEY")
        sandbox = create_e2b_sandbox(args)
        sandbox.files.make_dir(REMOTE_OUT)
        if args.install_deps:
            maybe_install_e2b_deps(sandbox)
        if args.upload_db:
            upload_db_to_e2b(sandbox, args.db)
        elif not args.e2b_template:
            raise SystemExit("--e2b-tool requires --e2b-template or --upload-db")

    try:
        first = openrouter_chat(
            api_key=api_key,
            model=args.model,
            messages=messages,
            tools=tools,
        )
        choice = first["choices"][0]
        msg = choice["message"]
        print_json(
            "openrouter_first",
            {
                "finish_reason": choice.get("finish_reason"),
                "content": msg.get("content"),
                "tool_calls": [
                    {
                        "id": call.get("id"),
                        "name": call.get("function", {}).get("name"),
                        "args_preview": call.get("function", {}).get("arguments", "")[:500],
                    }
                    for call in msg.get("tool_calls", []) or []
                ],
            },
        )

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            print("openrouter: model did not request a tool call", flush=True)
            return

        messages.append(msg)
        for call in tool_calls:
            fn = call.get("function", {})
            if fn.get("name") != "run_python":
                result = {"error": f"unsupported tool {fn.get('name')}"}
            else:
                try:
                    parsed = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    parsed = {"code": "", "parse_error": str(exc)}
                code = parsed.get("code", "")
                if sandbox is not None:
                    result = run_python_tool_e2b(sandbox, code)
                else:
                    result = run_python_tool_canned(parsed)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": fn.get("name", "run_python"),
                    "content": json.dumps(result),
                }
            )

        messages.append(
            {
                "role": "user",
                "content": "Use the tool result you just received to answer. Do not call another tool.",
            }
        )
        second = openrouter_chat(
            api_key=api_key,
            model=args.model,
            messages=messages,
            tools=None,
            max_tokens=1000,
        )
        final_msg = second["choices"][0]["message"]
        print_json(
            "openrouter_final",
            {
                "finish_reason": second["choices"][0].get("finish_reason"),
                "content": final_msg.get("content"),
            },
        )
    finally:
        if sandbox is not None:
            sandbox.kill()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--e2b", action="store_true", help="run a direct E2B sandbox smoke test")
    parser.add_argument(
        "--e2b-tool",
        action="store_true",
        help="when combined with --openrouter, execute run_python tool calls in E2B",
    )
    parser.add_argument("--e2b-template", default=None, help="optional E2B template id/name")
    parser.add_argument("--e2b-no-internet", action="store_true", help="disable sandbox internet")
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="pip install duckdb/matplotlib inside the sandbox before running",
    )
    parser.add_argument(
        "--upload-db",
        action="store_true",
        help="upload the local DuckDB to E2B and run a real query/plot",
    )
    parser.add_argument("--sandbox-timeout", type=int, default=600)
    parser.add_argument("--openrouter", action="store_true", help="run OpenRouter tool-call smoke")
    parser.add_argument(
        "--model",
        default=env_value("OPENROUTER_MODEL", "OPENROUTE_MODEL") or "openai/gpt-4o-mini",
        help="OpenRouter model id",
    )
    parser.add_argument(
        "--question",
        default="Use Python to count SYFI rounds by provider, then summarize the result.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.db = args.db.resolve()

    local_db_check(args.db)

    sandbox = None
    if args.e2b:
        sandbox, _summary = e2b_smoke(args)
        sandbox.kill()

    if args.openrouter:
        openrouter_tool_smoke(args)

    if not args.e2b and not args.openrouter:
        print("No remote tests requested. Add --e2b and/or --openrouter when API keys are set.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
