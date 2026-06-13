#!/usr/bin/env python3
"""FastAPI sidecar for the Ask-the-trace assistant (public + uploaded trace).

One WebSocket — ``WS /api/chat/ws`` — drives both data sources through the *same* server-side
tool loop (:func:`syfi_llm_runtime.run_chat_turn`). The only thing that forks is where ``run_python``
executes:

- **public/syfi**  → an E2B sandbox over the baked-in public DuckDB (runs server-side; keys never
  leave this process). A small :class:`SandboxPool` keeps a warm sandbox per session.
- **user**         → a :class:`~syfi_llm_runtime.ClientBridgeExecutor`: the server loop emits a
  ``tool_request`` frame, the browser runs the code in Pyodide over the *local* uploaded trace, and
  replies with a ``tool_result`` frame. Only generated code + aggregated results cross the socket —
  never raw trace rows.

The loop is synchronous; the socket is async. Each turn runs in a worker thread
(``asyncio.to_thread``) while the receive loop stays free to answer ``tool_request`` frames. Logger
events and tool requests are pushed back onto the event loop with ``run_coroutine_threadsafe``; the
browser's ``tool_result`` frames resolve the executor's pending Futures.

This sidecar also serves ``tester.html`` and its assets so the browser tester runs against the real
backend. It is a local/dev sidecar; production fronts ``/api`` with a reverse proxy that upgrades the
WebSocket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import os
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import syfi_llm_runtime as runtime  # noqa: E402 — needs ROOT on sys.path first

REPO_ROOT = ROOT.parents[1]

TESTER_HTML = ROOT / "tester.html"
PYODIDE_QA_WORKER = ROOT / "pyodide_qa_worker.js"
PY_PAYLOAD_ROOT = REPO_ROOT / "web" / "app" / "public" / "py"
SAMPLE_TRACE = REPO_ROOT / "example_sessions" / "sanitized" / "round_trace.jsonl"

DEFAULT_SANDBOX_TTK_SECONDS = 180
SANDBOX_REAPER_INTERVAL_SECONDS = 15
USER_SOURCES = {"user", "upload", "uploaded", "browser"}

# ---- rate limiting + per-turn logging config (env-overridable) ----------------------------
# A "turn" is the only metered unit that costs real money (LLM tokens + an E2B sandbox), so the
# WS endpoint — previously unlimited — gets two per-IP guards.
AI_RATE_LIMIT_MAX = int(os.environ.get("AI_RATE_LIMIT_MAX", 40))        # turns ...
AI_RATE_LIMIT_WINDOW_S = float(os.environ.get("AI_RATE_LIMIT_WINDOW_S", 3600))  # ... per window
AI_MAX_CONN_PER_IP = int(os.environ.get("AI_MAX_CONN_PER_IP", 3))      # simultaneous sockets/IP
# One JSONL line per turn (DuckDB-loadable). PUBLIC (syfi) turns log full content (question/answer/
# generated code); USER-uploaded-trace turns log metadata ONLY (ts/ip/session/usage/latency/...) —
# never the question/answer/code, since those can quote private trace content.
AI_CHAT_LOG = Path(os.environ.get("AI_CHAT_LOG", str(ROOT / "logs" / "chat_turns.jsonl")))


# ---- sandbox pool (warm-reuse of E2B sandboxes per session) --------------------------------
class SandboxRecord:
    def __init__(self, *, future: Future, expires_at: float) -> None:
        self.future = future
        self.expires_at = expires_at
        self.active = 0


class SandboxPool:
    """Keyed pool of E2B sandboxes with TTK-based reuse and a background reaper.

    Public turns reuse a warm sandbox across messages instead of paying cold-start each time; idle
    sandboxes are killed after their time-to-keep elapses. (Moved verbatim from the old tester
    server — the only sidecar that needs it.)
    """

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="syfi-e2b")
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str, bool], SandboxRecord] = {}
        self._closed = False
        self._reaper = threading.Thread(target=self._reap_loop, name="syfi-e2b-reaper", daemon=True)
        self._reaper.start()

    def prefetch(
        self,
        *,
        session_id: str,
        template: str,
        sandbox_timeout: int,
        allow_internet: bool,
        ttk_seconds: int,
        logger,
    ) -> tuple[tuple[str, str, bool], Future]:
        key = (session_id, template, allow_internet)
        now = time.monotonic()
        retired: list[SandboxRecord] = []
        with self._lock:
            record = self._records.get(key)
            failed = record.future.done() and record.future.exception() is not None if record is not None else False
            expired = record is not None and record.expires_at <= now and record.active == 0
            if record is not None and (expired or failed):
                retired.append(record)
                self._records.pop(key, None)
                record = None
            if record is None:
                future = self._executor.submit(
                    runtime.create_sandbox,
                    template=template,
                    sandbox_timeout=sandbox_timeout,
                    allow_internet=allow_internet,
                )
                record = SandboxRecord(future=future, expires_at=now + ttk_seconds)
                self._records[key] = record
                status = "prefetch_started"
            else:
                future = record.future
                record.expires_at = now + ttk_seconds
                status = "reuse_pending" if not future.done() else "reuse_sandbox"
            record.active += 1

        for item in retired:
            self._retire(item)

        logger(
            "e2b",
            {"status": status, "template": template, "session_id": session_id, "ttk_seconds": ttk_seconds},
        )
        return key, future

    def release(self, key: tuple[str, str, bool], *, ttk_seconds: int) -> None:
        with self._lock:
            record = self._records.get(key)
            if record is None:
                return
            record.active = max(0, record.active - 1)
            record.expires_at = time.monotonic() + ttk_seconds
            future = record.future if record.future.done() else None

        if future is not None:
            try:
                future.result().set_timeout(ttk_seconds)
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            records = list(self._records.values())
            self._records.clear()
        for record in records:
            self._retire(record)
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _reap_loop(self) -> None:
        while True:
            time.sleep(SANDBOX_REAPER_INTERVAL_SECONDS)
            if self._closed:
                return
            self.reap_expired()

    def reap_expired(self) -> None:
        now = time.monotonic()
        expired: list[SandboxRecord] = []
        with self._lock:
            for key, record in list(self._records.items()):
                if record.active == 0 and record.expires_at <= now:
                    expired.append(record)
                    self._records.pop(key, None)
        for record in expired:
            self._retire(record)

    def _retire(self, record: SandboxRecord) -> None:
        future = record.future
        if future.done():
            self._kill_future_sandbox(future)
            return
        future.add_done_callback(self._kill_future_sandbox)

    @staticmethod
    def _kill_future_sandbox(future: Future) -> None:
        try:
            sandbox = future.result()
            sandbox.kill()
        except Exception:
            pass


# ---- request-knob parsing -----------------------------------------------------------------
def sandbox_ttk_seconds(payload: dict[str, Any]) -> int:
    raw = payload.get("sandbox_ttk_seconds", DEFAULT_SANDBOX_TTK_SECONDS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SANDBOX_TTK_SECONDS
    return min(max(value, 30), 1800)


def trace_context(payload: dict[str, Any], default: str) -> str:
    raw = payload.get("trace_context")
    if not isinstance(raw, str) or not raw.strip():
        return default
    return " ".join(raw.split())[:700]


def _chat_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
    """Knobs shared by both sources. Model/template/limits stay server-trusted; the client only
    influences them through these bounded fields (the frontend will send just messages/source/id)."""
    return {
        # None lets the runtime use the resolved backend's model (vLLM vs OpenRouter on failover);
        # a client-supplied model still overrides.
        "model": payload.get("model") or None,
        "max_tool_turns": int(payload.get("max_tool_turns") or 4),
        "max_tokens": int(payload.get("max_tokens") or runtime.DEFAULT_MAX_TOKENS),
        "max_generation_retries": int(
            payload.get("max_generation_retries") or runtime.DEFAULT_MAX_GENERATION_RETRIES
        ),
        "tool_timeout": float(payload.get("tool_timeout") or 120),
        "print_code": bool(payload.get("print_code") or False),
        "max_history_messages": int(payload.get("max_history_messages") or 12),
        "openrouter_max_retries": int(
            payload.get("openrouter_max_retries") or runtime.DEFAULT_OPENROUTER_MAX_RETRIES
        ),
    }


# ---- sync-loop runners (executed in a worker thread) --------------------------------------
def _run_public_turn(pool: SandboxPool, payload: dict[str, Any], emit) -> dict[str, Any]:
    """Public path: run the loop over the pooled E2B sandbox. Byte-identical to the old sidecar."""
    e2b_key = runtime.require_env("E2B_API_KEY", "E2B_KEY")
    os.environ.setdefault("E2B_API_KEY", e2b_key)

    template = str(payload.get("template") or runtime.DEFAULT_TEMPLATE)
    allow_internet = bool(payload.get("allow_internet") or False)
    tool_timeout = float(payload.get("tool_timeout") or 120)
    ttk_seconds = sandbox_ttk_seconds(payload)
    sandbox_timeout = int(payload.get("sandbox_timeout") or max(ttk_seconds, int(tool_timeout) + 60))
    session_id = str(payload.get("session_id") or "anonymous")

    key, sandbox_future = pool.prefetch(
        session_id=session_id,
        template=template,
        sandbox_timeout=sandbox_timeout,
        allow_internet=allow_internet,
        ttk_seconds=ttk_seconds,
        logger=emit,
    )
    try:
        return runtime.run_chat_turn(
            messages=payload["messages"],
            template=template,
            allow_internet=allow_internet,
            sandbox_timeout=sandbox_timeout,
            sandbox_future=sandbox_future,
            kill_sandbox=False,
            trace_context=runtime.DEFAULT_SYFI_TRACE_CONTEXT,
            logger=emit,
            **_chat_kwargs(payload),
        )
    finally:
        pool.release(key, ttk_seconds=ttk_seconds)


def _run_user_turn(executor: "runtime.ClientBridgeExecutor", payload: dict[str, Any], emit) -> dict[str, Any]:
    """User path: run the loop with the injected client-bridge executor (browser runs run_python)."""
    return runtime.run_chat_turn(
        messages=payload["messages"],
        executor=executor,
        trace_context=trace_context(payload, runtime.DEFAULT_USER_TRACE_CONTEXT),
        logger=emit,
        **_chat_kwargs(payload),
    )


# ---- rate limiting + per-turn logging -----------------------------------------------------
# In-memory, single-worker (asyncio): every access happens on the event loop, so no lock is needed.
# Multi-worker would need shared state (Redis) — same limitation as the contribute sidecar.
_ai_rate_hits: dict[str, deque] = defaultdict(deque)
_conn_count: dict[str, int] = defaultdict(int)


def _client_ip(ws: WebSocket) -> str:
    # Sidecar binds 127.0.0.1, reachable only through the dev-server proxy (Vite, `xfwd: true`), which
    # APPENDS the real client IP to X-Forwarded-For. Trust the right-most entry (proxy-added), not the
    # left-most one a client can spoof by sending its own header.
    fwd = ws.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return ws.client.host if ws.client else "unknown"


def _check_ai_rate_limit(ip: str) -> bool:
    """True if this IP may start another turn; False once it exceeds the per-window cap. Keyed by IP,
    not session_id (which is regenerated each conversation and so can't bound abuse)."""
    now = time.monotonic()
    hits = _ai_rate_hits[ip]
    while hits and now - hits[0] > AI_RATE_LIMIT_WINDOW_S:
        hits.popleft()
    if len(hits) >= AI_RATE_LIMIT_MAX:
        return False
    hits.append(now)
    return True


def _last_user_question(payload: dict[str, Any]) -> str:
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for msg in reversed(msgs):
            if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str):
                return msg["content"]
    return ""


def _retry_meta(label: str, value: dict[str, Any]) -> dict[str, Any]:
    """Compact one retry event (always metadata — never content)."""
    meta: dict[str, Any] = {
        "kind": "openrouter" if label == "openrouter_retry" else "generation",
        "status": value.get("status"),
    }
    for k in ("attempt", "retry", "max_retries", "http_status", "finish_reason", "retryable"):
        if value.get(k) is not None:
            meta[k] = value[k]
    return meta


def _tool_result_meta(summary: dict[str, Any], *, log_content: bool) -> dict[str, Any]:
    """Per-round tool-result detail. Always: counts + ok/error. When ``log_content`` (public): also
    the (trimmed) stdout/stderr, error detail, result shapes, and artifact paths."""
    meta: dict[str, Any] = {
        "ok": summary.get("error") is None,
        "stdout_lines": len(summary.get("stdout") or []),
        "stderr_lines": len(summary.get("stderr") or []),
        "error": bool(summary.get("error")),
        "result_count": len(summary.get("results") or []),
        "artifact_count": len(summary.get("artifacts") or []),
    }
    if not log_content:
        return meta  # user-trace: shape only, never the actual output

    def _trim(lines: Any, n: int = 60, w: int = 600) -> list[str]:
        return [str(x)[:w] for x in (lines or [])[-n:]]

    meta["stdout"] = _trim(summary.get("stdout"))
    meta["stderr"] = _trim(summary.get("stderr"))
    err = summary.get("error")
    if isinstance(err, dict):
        meta["error_detail"] = {k: (str(err[k])[:1500] if err.get(k) else None)
                                for k in ("name", "value", "traceback")}
    elif err:
        meta["error_detail"] = str(err)[:1500]
    results = []
    for r in (summary.get("results") or [])[:10]:
        results.append({
            "text": r.get("text")[:1000] if isinstance(r.get("text"), str) else None,
            "has_json": r.get("json") is not None,
            "png_bytes_approx": r.get("png_bytes_approx"),
            "svg_chars": r.get("svg_chars"),
        })
    if results:
        meta["results"] = results
    artifacts = [{"path": a.get("path"), "size": a.get("size"), "mime": a.get("mime")}
                 for a in (summary.get("artifacts") or [])[:20]]
    if artifacts:
        meta["artifacts"] = artifacts
    return meta


def _rounds_from_events(events: list[tuple[str, Any]], *, log_content: bool) -> list[dict[str, Any]]:
    """Group the per-turn event stream into one entry per model step (round).

    A round opens on each ``model_turn`` and absorbs the ``tool_code``/``tool_result`` for the tool
    calls it issued. Retries (``openrouter_retry``/``generation_retry``) precede the call they
    retry, so they're buffered and attached to the next round. Always records finish_reason / usage /
    tool names / result counts; content (model thinking+text, tool code, tool stdout) only when public.
    """
    rounds: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    pending_retries: list[dict[str, Any]] = []
    for label, value in events:
        if label in ("openrouter_retry", "generation_retry"):
            pending_retries.append(_retry_meta(label, value))
        elif label == "model_turn":
            cur: dict[str, Any] = {
                "round": value.get("turn"),
                "finish_reason": value.get("finish_reason"),
                "usage": value.get("usage"),
            }
            if value.get("reasoning_redacted"):
                cur["reasoning_redacted"] = True
            if pending_retries:
                cur["retries"] = pending_retries
                pending_retries = []
            if log_content:
                if value.get("content"):
                    cur["content"] = value["content"]
                if value.get("thinking"):
                    cur["thinking"] = value["thinking"]
            by_id = {}
            tools = []
            for tc in value.get("tool_calls") or []:
                entry: dict[str, Any] = {"name": tc.get("name")}
                tools.append(entry)
                if tc.get("id"):
                    by_id[tc["id"]] = entry
            if tools:
                cur["tools"] = tools
            rounds.append(cur)
        elif label == "tool_code":
            entry = by_id.get(value.get("tool_call_id"))
            if entry is not None and log_content and value.get("code"):
                entry["code"] = value["code"]
        elif label == "tool_result":
            entry = by_id.get(value.get("tool_call_id"))
            if entry is not None:
                entry["result"] = _tool_result_meta(value.get("summary") or {}, log_content=log_content)
    if pending_retries:  # retries on the forced-final call (no model_turn follows it)
        rounds.append({"round": "final", "retries": pending_retries})
    return rounds


def _chat_turn_record(
    payload: dict[str, Any], ip: str, result: dict[str, Any] | None, error: str | None,
    started: float, *, source: str, log_content: bool, events: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Build one turn log row. `result` is the run_chat_turn return (None on failure).

    Metadata (ts/ip/session/usage/latency/provider/per-round finish_reason+tokens+tool counts) is
    always recorded. Content (question/answer/thinking/generated code/tool stdout) is recorded ONLY
    when ``log_content`` — i.e. public turns. User-trace turns keep the same per-round skeleton with
    the content fields omitted (privacy).
    """
    rec: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": str(payload.get("session_id") or "anonymous"),
        "source": source,
        "ip": ip,
        "latency_ms": round((time.monotonic() - started) * 1000),
        "error": error,
    }
    if log_content:
        rec["question"] = _last_user_question(payload)
    if result:
        tool_events = result.get("tool_events") or []
        rec.update(
            provider=result.get("provider"),
            model=result.get("model"),
            turns=result.get("turns"),
            forced=result.get("forced"),
            usage=result.get("usage"),
            tool_count=len(tool_events),
        )
        if log_content:
            rec["answer"] = result.get("content")
    rec["rounds"] = _rounds_from_events(events, log_content=log_content)
    return rec


def _log_chat_turn(record: dict[str, Any]) -> None:
    """Append one JSONL line for a completed/failed PUBLIC turn. Best-effort; never raises."""
    try:
        AI_CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AI_CHAT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- app ----------------------------------------------------------------------------------
app = FastAPI(title="SyFI Trace Atlas Ask API")
app.state.pool = SandboxPool()


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.websocket("/api/chat/ws")
async def chat_ws(ws: WebSocket) -> None:
    await ws.accept()
    ip = _client_ip(ws)
    # Connection cap: bounds how many sandboxes one client can hold open at once.
    if _conn_count[ip] >= AI_MAX_CONN_PER_IP:
        try:
            await ws.send_json({"type": "error", "error": "too many concurrent connections; close another tab and retry"})
        finally:
            await ws.close(code=1013)  # 1013 = try again later
        return
    _conn_count[ip] += 1
    loop = asyncio.get_running_loop()
    pool: SandboxPool = ws.app.state.pool
    # The executor of the in-flight user turn, so the receive loop can route `tool_result` frames to
    # it. None between turns and for public turns (E2B runs server-side, nothing to route back).
    bridge: runtime.ClientBridgeExecutor | None = None
    turn: asyncio.Task | None = None

    def send_threadsafe(frame: dict[str, Any]) -> None:
        # Called from the worker thread; block until the frame is actually sent to preserve order
        # and surface a dead socket to the caller (the bridge turns that into a tool error).
        asyncio.run_coroutine_threadsafe(ws.send_json(frame), loop).result(60)

    def emit(label: str, value: Any) -> None:
        try:
            send_threadsafe({"type": "event", "label": label, "value": value})
        except Exception:
            pass  # best-effort telemetry; never break the loop on a send hiccup

    async def run_turn(payload: dict[str, Any]) -> None:
        nonlocal bridge
        source = str(payload.get("source") or payload.get("executor") or "syfi").lower()
        is_public = source not in USER_SOURCES
        started = time.monotonic()
        result: dict[str, Any] | None = None
        error: str | None = None
        # Buffer this turn's event stream (one turn at a time per connection) so it can be grouped
        # into per-round log detail. turn_emit still forwards every event to the browser.
        events: list[tuple[str, Any]] = []

        def turn_emit(label: str, value: Any) -> None:
            events.append((label, value))
            emit(label, value)

        try:
            if not is_public:
                bridge = runtime.ClientBridgeExecutor(
                    send=send_threadsafe,
                    db_path=str(payload.get("db_path") or "/work/trace.duckdb"),
                    out_dir=str(payload.get("out_dir") or "/out"),
                )
                result = await asyncio.to_thread(_run_user_turn, bridge, payload, turn_emit)
            else:
                result = await asyncio.to_thread(_run_public_turn, pool, payload, turn_emit)
            await ws.send_json({"type": "done", "result": result})
        except Exception as exc:  # noqa: BLE001 — report any turn failure to the client
            error = f"{type(exc).__name__}: {exc}"
            try:
                await ws.send_json({"type": "error", "error": error})
            except Exception:
                pass
        finally:
            bridge = None
            # Always log a row (success or failure). Public turns carry full content; user-source
            # turns carry metadata only — question/answer/code/tool-output are never persisted.
            _log_chat_turn(_chat_turn_record(
                payload, ip, result, error, started,
                source="syfi" if is_public else "user",
                log_content=is_public,
                events=events,
            ))

    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "tool_result":
                if bridge is not None:
                    bridge.resolve(str(msg.get("tool_call_id") or ""), msg.get("result"))
                continue
            if mtype in {"chat", None}:
                if not isinstance(msg.get("messages"), list) or not msg["messages"]:
                    await ws.send_json({"type": "error", "error": "messages must be a non-empty list"})
                    continue
                if turn is not None and not turn.done():
                    await ws.send_json({"type": "error", "error": "a turn is already in progress"})
                    continue
                if not _check_ai_rate_limit(ip):
                    await ws.send_json({"type": "error", "error": "rate limited; please try again later"})
                    continue
                turn = asyncio.create_task(run_turn(msg))
                continue
            # unknown frame type: ignore
    except WebSocketDisconnect:
        pass
    finally:
        _conn_count[ip] = max(0, _conn_count[ip] - 1)
        if _conn_count[ip] == 0:
            _conn_count.pop(ip, None)
        if bridge is not None:
            bridge.fail_all("client disconnected")
        if turn is not None and not turn.done():
            turn.cancel()


# ---- static assets for the browser tester -------------------------------------------------
@app.get("/")
@app.get("/tester.html")
async def tester_page() -> HTMLResponse:
    return HTMLResponse(TESTER_HTML.read_text(encoding="utf-8"))


@app.get("/pyodide_qa_worker.js")
async def pyodide_worker() -> Response:
    return Response(PYODIDE_QA_WORKER.read_bytes(), media_type="text/javascript; charset=utf-8")


@app.get("/fixtures/example_trace.jsonl")
async def example_trace() -> Response:
    return Response(SAMPLE_TRACE.read_bytes(), media_type="application/x-ndjson")


@app.get("/py/{rel_path:path}")
async def py_payload(rel_path: str) -> Response:
    target = (PY_PAYLOAD_ROOT / rel_path).resolve()
    try:
        target.relative_to(PY_PAYLOAD_ROOT.resolve())
    except ValueError:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if not target.is_file():
        return JSONResponse({"error": "py payload not found"}, status_code=404)
    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return Response(target.read_bytes(), media_type=mime)


@app.on_event("shutdown")
async def _shutdown() -> None:
    app.state.pool.close()


def main() -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    default_port = int(runtime.service_config().get("ports", {}).get("ai_backend", 60980))
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()
    print(f"SYFI Ask API + tester: http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
