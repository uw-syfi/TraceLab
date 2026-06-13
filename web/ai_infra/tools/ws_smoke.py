#!/usr/bin/env python3
"""Headless WebSocket smoke client for ``WS /api/chat/ws`` — no browser, no Chromium.

Validates the sidecar's socket + sync↔async thread bridge end to end. For ``--source user`` it plays
the role the browser's Pyodide worker plays: it answers each ``tool_request`` frame by running the
model's code against a local DuckDB (via :func:`syfi_llm_runtime.local_python_exec`) and replies with
a ``tool_result`` frame. For ``--source public`` it just observes frames (the server runs ``run_python``
in E2B; no ``tool_request`` ever arrives).

Usage::

    # Start the sidecar first. The LLM endpoint/model/key come from config/services.json + env:
    source ~/.bashrc
    E2B_API_KEY="$E2B_KEY" uv run --extra ai python web/ai_infra/app.py

    # User path (browser stand-in) against a local DuckDB:
    uv run --extra ai python web/ai_infra/tools/ws_smoke.py \
        --source user --db trace/syfi_coding_trace.duckdb \
        --question "How many rows are in the tool_calls table?"

Exits non-zero if the turn ends in ``error`` or never reaches ``done``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import syfi_llm_runtime as runtime  # noqa: E402 — needs ROOT on sys.path first


def _brief(value: object, limit: int = 200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


async def run(args: argparse.Namespace) -> int:
    url = f"ws://{args.host}:{args.port}/api/chat/ws"
    out_dir = tempfile.mkdtemp(prefix="syfi-ws-smoke-out-")
    db_path = str(Path(args.db).resolve()) if args.db else None

    chat = {
        "type": "chat",
        "source": args.source,
        "messages": [{"role": "user", "content": args.question}],
        "model": args.model,
        "max_tool_turns": args.max_tool_turns,
        "print_code": True,
        "tool_timeout": args.tool_timeout,
    }
    if args.source == "user":
        # Tell the server (and thus the model's system prompt) to read/write these local paths; the
        # tool_request handler below runs the code against the very same db_path/out_dir.
        chat["db_path"] = db_path
        chat["out_dir"] = out_dir

    print(f"→ connecting {url}  (source={args.source})", flush=True)
    async with websockets.connect(url, max_size=64 * 1024 * 1024, open_timeout=15) as ws:
        await ws.send(json.dumps(chat))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=args.recv_timeout)
            frame = json.loads(raw)
            ftype = frame.get("type")

            if ftype == "event":
                print(f"  · event[{frame.get('label')}] {_brief(frame.get('value'))}", flush=True)

            elif ftype == "tool_request":
                code = frame.get("code") or ""
                print(f"  ⤷ tool_request {frame.get('tool_call_id')}  ({len(code)} chars)", flush=True)
                result = runtime.local_python_exec(code, out_dir=out_dir)
                await ws.send(json.dumps({
                    "type": "tool_result",
                    "tool_call_id": frame.get("tool_call_id"),
                    "result": result,
                }))

            elif ftype == "done":
                content = (frame.get("result") or {}).get("content", "")
                print("✓ done\n" + "-" * 60 + f"\n{content}\n" + "-" * 60, flush=True)
                return 0

            elif ftype == "error":
                print(f"✗ error: {frame.get('error')}", flush=True)
                return 1

            else:
                print(f"  ? unknown frame: {_brief(frame)}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--source", choices=["user", "public"], default="user")
    p.add_argument("--db", default=None, help="Local DuckDB to answer tool_request frames (user source).")
    p.add_argument("--question", required=True)
    p.add_argument("--model", default=None, help="Override model id (default: chosen by the active/failover provider).")
    p.add_argument("--max-tool-turns", type=int, default=3)
    p.add_argument("--tool-timeout", type=float, default=120)
    p.add_argument("--recv-timeout", type=float, default=300)
    args = p.parse_args()
    if args.source == "user" and not args.db:
        p.error("--source user requires --db <path to local .duckdb>")
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
