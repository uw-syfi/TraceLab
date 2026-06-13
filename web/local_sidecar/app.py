#!/usr/bin/env python3
"""FastAPI app for the local self-deploy sidecar (see ``__init__`` for the why).

Route order matters (Starlette matches in declaration order; first match wins):

    1. GET/HEAD /api/local-trace          -> stream local ~/.claude + ~/.codex as one .tar.gz
       GET      /api/local-trace/meta      -> cheap stat-only summary (counts + approx size)
    2. WS       /api/chat/ws               -> reverse-proxy the assistant socket to the master
    3. *        /api/{path:path}           -> reverse-proxy every other /api call to the master
    4. mount /  -> StaticFiles(dist, html) -> the built site (declared LAST so /api wins)

The browser only ever talks to this sidecar (same-origin, no CORS); the master server is unchanged.
Raw trace bytes cross only sidecar->browser on localhost; the browser sanitizes before anything is
proxied onward.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Iterator

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# websockets ships with uvicorn[standard]; prefer the modern asyncio client, fall back to legacy.
try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover - older websockets
    from websockets import connect as ws_connect  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[2]

# Native ingest (web/payload/ingest.py) — pure stdlib, same module the browser runs under Pyodide.
# We reuse it server-side to normalize+sanitize this machine's trace natively (the "local executor"),
# so only the small sanitized .gz crosses to the browser instead of the ~1.3 GB raw history.
_PAYLOAD_DIR = REPO_ROOT / "web" / "payload"
if str(_PAYLOAD_DIR) not in sys.path:
    sys.path.insert(0, str(_PAYLOAD_DIR))
try:
    import ingest as _ingest  # noqa: E402  (resolves canonical scripts itself)
except Exception:  # pragma: no cover - missing payload tree
    _ingest = None

# Native analytics (the SAME pure-duckdb code the browser runs under Pyodide): trace_db.materialize
# builds the per-trace DuckDB; analyze.bulk_json / session_detail_json compute the dashboard JSON. We
# run these server-side so the browser fetches a ~60 KB JSON payload instead of building the DB + doing
# the analysis itself (tens of seconds of WASM work over a slow tunnel). Neither imports numpy/mpl.
_WEB_ANALYTICS_DIR = REPO_ROOT / "artifacts" / "web_analytics"
_UTILS_DIR = REPO_ROOT / "artifacts" / "utils"
for _p in (str(_WEB_ANALYTICS_DIR), str(_UTILS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    import trace_db as _trace_db  # noqa: E402
    import analyze as _analyze  # noqa: E402  (web_analytics/analyze.py — pulls the builders)
except Exception:  # pragma: no cover - missing analytics tree
    _trace_db = None
    _analyze = None


# ---- config (env-overridable; master address also reads config/services.json) -------------
def _load_master_server_address() -> str:
    """Master server origin: env MASTER_SERVER_ADDRESS > config/services.json > fallback."""
    env = os.environ.get("MASTER_SERVER_ADDRESS")
    if env and env.strip():
        return env.strip().rstrip("/")
    try:
        cfg = json.loads((REPO_ROOT / "config" / "services.json").read_text())
        val = cfg.get("master_server_address")
        if isinstance(val, str) and val.strip():
            return val.strip().rstrip("/")
    except Exception:
        pass
    # Neutral placeholder: real deployments set MASTER_SERVER_ADDRESS (or config/services.json,
    # or launch.sh --master). No internal hostname is baked into the source.
    return "https://master.example.com"


def _ws_origin(http_origin: str) -> str:
    if http_origin.startswith("https://"):
        return "wss://" + http_origin[len("https://") :]
    if http_origin.startswith("http://"):
        return "ws://" + http_origin[len("http://") :]
    return http_origin


MASTER_SERVER_ADDRESS = _load_master_server_address()

# Two roles, selected by LOCAL_MASTER_SERVER:
#   frontend (default, machine B): SERVE the local-trace endpoint (this machine's ~/.claude +
#     ~/.codex) and proxy the chat WS + the rest of /api to the remote master, which splits them
#     between its own AI + contribute backends.
#   master server (LOCAL_MASTER_SERVER=1, set by `launch.sh --master-server`): THIS machine is the
#     master. The backends run locally and we split here (mirroring the dev/prod reverse proxy):
#     /api/chat* -> the AI backend, everything else under /api -> the contribute backend. The
#     local-trace endpoint is NOT registered — the master never reads the operator's local sessions.
MASTER_SERVER = os.environ.get("LOCAL_MASTER_SERVER", "").strip().lower() in {"1", "true", "yes", "on"}
SERVE_LOCAL_TRACE = not MASTER_SERVER


def _config_port(name: str, default: int) -> int:
    """A backend port: env LOCAL_<NAME> > config/services.json:ports.<name> > default."""
    env = os.environ.get("LOCAL_" + name.upper())
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        cfg = json.loads((REPO_ROOT / "config" / "services.json").read_text())
        return int(cfg["ports"][name])
    except Exception:
        return default


if MASTER_SERVER:
    _AI_PORT = _config_port("ai_backend", 60980)
    _CONTRIB_PORT = _config_port("contribute_backend", 60981)
    CHAT_UPSTREAM = f"http://127.0.0.1:{_AI_PORT}"
    API_UPSTREAM = f"http://127.0.0.1:{_CONTRIB_PORT}"
    UPSTREAM_DESC = (
        f"master server (local) — /api/chat -> :{_AI_PORT}, /api -> :{_CONTRIB_PORT}; local-trace disabled"
    )
else:
    CHAT_UPSTREAM = MASTER_SERVER_ADDRESS
    API_UPSTREAM = MASTER_SERVER_ADDRESS
    UPSTREAM_DESC = f"frontend — local-trace on; proxy /api -> {MASTER_SERVER_ADDRESS}"

WS_TARGET = _ws_origin(CHAT_UPSTREAM) + "/api/chat/ws"

CLAUDE_ROOT = Path(os.environ.get("LOCAL_TRACE_CLAUDE_ROOT", str(Path.home() / ".claude" / "projects")))
CODEX_ROOT = Path(os.environ.get("LOCAL_TRACE_CODEX_ROOT", str(Path.home() / ".codex" / "sessions")))
DIST_ROOT = Path(os.environ.get("LOCAL_DIST_ROOT", str(REPO_ROOT / "web" / "app" / "dist")))
# Where the server-side native sanitize writes its artifacts (sanitized.jsonl.gz, normalized.jsonl.gz,
# round_raw.json). A persisted folder so you keep a reusable sanitized export you can re-analyze or
# contribute. The /api/local-trace/{prepare,sanitized,round-raw} endpoints read from here.
LOCAL_EXPORT_DIR = Path(
    os.environ.get("LOCAL_EXPORT_DIR", str(Path.home() / ".cache" / "syfi-trace" / "export"))
)
PROXY_TIMEOUT = float(os.environ.get("LOCAL_SIDECAR_PROXY_TIMEOUT", 300))
# Optional, OFF by default: drop inline base64 image blobs from the streamed trace (the screenshots
# the browser discards on normalize anyway, ~hundreds of MB). A size optimization, not sanitization.
STRIP_IMAGE_BLOBS = os.environ.get("STRIP_IMAGE_BLOBS", "").strip().lower() in {"1", "true", "yes", "on"}

# Hop-by-hop headers never forwarded across a proxy.
_HOP = {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "proxy-connection", "te", "trailer", "content-length"}
# Best-effort base64-image stripper (only used when STRIP_IMAGE_BLOBS): long base64 runs after a
# `base64,` data-URI marker or an image-block `"data":"..."`. 256-char floor avoids touching ids/hashes.
_B64_DATAURI = re.compile(rb'base64,[A-Za-z0-9+/=]{256,}')
_B64_DATA_FIELD = re.compile(rb'("data"\s*:\s*")[A-Za-z0-9+/=]{256,}(")')


# ---- app ----------------------------------------------------------------------------------
app = FastAPI(title="SyFI Trace Atlas Local Sidecar")


@app.on_event("startup")
async def _startup() -> None:
    # No base_url: targets are absolute and may differ per request (chat vs the rest in full-stack mode).
    app.state.client = httpx.AsyncClient(timeout=PROXY_TIMEOUT)


@app.on_event("shutdown")
async def _shutdown() -> None:
    client: httpx.AsyncClient | None = getattr(app.state, "client", None)
    if client is not None:
        await client.aclose()


# ---- 1. local trace ------------------------------------------------------------------------
def _present_roots() -> list[tuple[str, Path]]:
    """(arcname-prefix, root) for whichever of ~/.claude/projects, ~/.codex/sessions exist."""
    out: list[tuple[str, Path]] = []
    if CLAUDE_ROOT.is_dir():
        out.append(("projects", CLAUDE_ROOT))
    if CODEX_ROOT.is_dir():
        out.append(("sessions", CODEX_ROOT))
    return out


def _iter_jsonl(root: Path) -> Iterator[Path]:
    """Regular (non-symlink) *.jsonl files under root, sorted for deterministic archives."""
    for path in sorted(root.rglob("*.jsonl")):
        if path.is_file() and not path.is_symlink():
            yield path


def _strip_images(data: bytes) -> bytes:
    out = _B64_DATAURI.sub(b"base64,", data)
    return _B64_DATA_FIELD.sub(rb"\1\2", out)


class _Sink:
    """A write-only fileobj that buffers what tarfile emits so a generator can drain it."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> int:
        self.buf += data
        return len(data)


def _iter_tar_gz(roots: list[tuple[str, Path]]) -> Iterator[bytes]:
    """Stream a .tar.gz of the local trace. Streaming (``w|gz``) never seeks, so it fits a generator.

    Buffer peak is one member's compressed bytes (we yield between members). Unreadable members are
    skipped — once bytes have started flowing we can't signal an error, so we never raise mid-stream.
    """
    sink = _Sink()
    with tarfile.open(fileobj=sink, mode="w|gz") as tar:
        for prefix, root in roots:
            for path in _iter_jsonl(root):
                try:
                    arcname = f"{prefix}/{path.relative_to(root).as_posix()}"
                    if STRIP_IMAGE_BLOBS:
                        payload = _strip_images(path.read_bytes())
                        info = tarfile.TarInfo(name=arcname)
                        info.size = len(payload)
                        info.mtime = 0
                        tar.addfile(info, io.BytesIO(payload))
                    else:
                        tar.add(path, arcname=arcname, recursive=False)
                except (OSError, ValueError):
                    continue  # vanished / unreadable / outside-root — skip, keep going
                if sink.buf:
                    yield bytes(sink.buf)
                    sink.buf.clear()
    if sink.buf:
        yield bytes(sink.buf)
        sink.buf.clear()


async def local_trace(request: Request):
    """HEAD = cheap presence probe (drives the frontend's auto-load). GET = stream the .tar.gz."""
    roots = _present_roots()
    if not roots:
        raise HTTPException(404, "No local ~/.claude/projects or ~/.codex/sessions trace on this machine.")
    if request.method == "HEAD":
        return Response(status_code=200)
    return StreamingResponse(
        _iter_tar_gz(roots),
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="local-trace.tar.gz"'},
    )


async def local_trace_meta() -> JSONResponse:
    """Stat-only summary so the UI can show "loading local trace (~N MB)…" before the stream."""

    def _scan(root: Path) -> dict:
        files = 0
        total = 0
        if root.is_dir():
            for path in root.rglob("*.jsonl"):
                if path.is_file() and not path.is_symlink():
                    files += 1
                    try:
                        total += path.stat().st_size
                    except OSError:
                        pass
        return {"present": root.is_dir(), "jsonl_files": files, "bytes": total}

    claude = await asyncio.to_thread(_scan, CLAUDE_ROOT)
    codex = await asyncio.to_thread(_scan, CODEX_ROOT)
    return JSONResponse(
        {
            "claude": claude,
            "codex": codex,
            "approx_uncompressed_bytes": claude["bytes"] + codex["bytes"],
            "strip_image_blobs": STRIP_IMAGE_BLOBS,
        }
    )


# ---- 1b. local executor: native server-side sanitize (the "code to data" path) -------------
# Instead of streaming the raw ~1.3 GB trace to the browser to normalize+sanitize in Pyodide, we run
# the SAME ingest module natively here, in a thread, and hand the browser only the small sanitized .gz.
# We also surface ingest's local-only round_raw.json (sanitized trace_key -> original input/output) so
# the per-round raw drill-down keeps working — the browser can't recover it from sanitized rows.
_PREPARE_LOCK = asyncio.Lock()
# Cache the last native prepare so we don't re-parse the whole history on every click; invalidated by a
# cheap signature over the trace files (or ?refresh=1). Holds the public meta + artifact paths + the
# in-memory round_raw map.
_PREPARE_CACHE: dict[str, Any] = {}


def _iter_file(path: Path, chunk: int = 1 << 20) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            yield block


def _roots_signature() -> str:
    """Cheap content signature: (path, size, mtime) over every *.jsonl under the present roots."""
    h = hashlib.sha256()
    for _prefix, root in _present_roots():
        for path in _iter_jsonl(root):
            try:
                st = path.stat()
            except OSError:
                continue
            h.update(f"{path}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def _present_root_files() -> list[str]:
    """Every *.jsonl under the present roots, as REAL on-disk paths — handed straight to
    ``ingest.prepare(files=...)`` so it skips the tar pack+unpack round-trip (the files are already on
    disk; ingest reads them in place and fans the parse out across cores). The real paths still carry
    the ``projects/``/``sessions/`` segments ingest's claude routing keys off, so output matches the
    archive path. ingest re-sorts internally, so order here doesn't matter.
    """
    out: list[str] = []
    for _prefix, root in _present_roots():
        out.extend(str(path) for path in _iter_jsonl(root))
    return out


def _run_prepare_blocking(refresh: bool, progress=None) -> dict:
    """Native ingest.prepare over the local trace into LOCAL_EXPORT_DIR; cache by roots signature.

    Returns the public meta (ingest meta minus absolute `files`, plus `sanitizedBytes`). Blocking — the
    caller runs it in an executor. ``progress(stage)`` is called at each phase (and threaded into ingest,
    which emits its own Detecting/Extracting/Sanitizing stages) so the endpoint can stream them.
    """
    note = progress if callable(progress) else (lambda *_a: None)
    if _ingest is None:
        raise RuntimeError("native ingest module is unavailable (web/payload not importable)")
    if not _present_roots():
        raise FileNotFoundError("No local ~/.claude/projects or ~/.codex/sessions trace on this machine.")

    sig = _roots_signature()
    if not refresh and _PREPARE_CACHE.get("sig") == sig and _PREPARE_CACHE.get("meta"):
        return _PREPARE_CACHE["meta"]

    LOCAL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    # Run ingest in an EPHEMERAL work dir — its uncompressed intermediates (normalized/sanitized.jsonl,
    # round_raw.json, the materialized DB) are scratch we don't want lingering in the user-facing export
    # folder — then copy only the final artifacts into LOCAL_EXPORT_DIR.
    with tempfile.TemporaryDirectory() as work:
        workp = Path(work)
        note("Reading local trace")
        # Skip the tar pack+unpack round-trip: the trace is already on disk, so hand the real paths
        # straight to ingest (files=) and fan the per-file parse out across cores (jobs=). 16 is the
        # measured sweet spot; past it IPC of the raw map dominates. trusted=True: a real combined
        # history exceeds the strict untrusted 1 GiB ceiling. ingest emits its own Detecting/Extracting/
        # Sanitizing stages through `progress`.
        jobs = min(16, (os.cpu_count() or 4))
        meta = _ingest.prepare(
            str(workp), workp, progress=progress, trusted=True, jobs=jobs, files=_present_root_files()
        )

        note("Writing sanitized export")
        files = meta.get("files", {})
        exported: dict[str, str] = {}
        for label, name in (
            ("sanitized_gz", "sanitized.jsonl.gz"),
            ("normalized_gz", "normalized.jsonl.gz"),
            ("round_raw_json", "round_raw.json"),
        ):
            src = files.get(label)
            if src and Path(src).is_file():
                dst = LOCAL_EXPORT_DIR / name
                shutil.copyfile(src, dst)
                exported[label] = str(dst)

        # Build the DuckDB natively (the same trace_db the browser would run under Pyodide, but ~10×
        # faster here) from the still-present uncompressed sanitized.jsonl, then copy it out + gzip a
        # transfer copy. The browser fetches the small computed JSON for the dashboard; the .duckdb.gz
        # transfers in the background to prime the AI assistant's in-browser query cache.
        db_export: str | None = None
        db_gz_export: str | None = None
        sanitized_jsonl = files.get("sanitized_jsonl")
        if _trace_db is not None and sanitized_jsonl and Path(sanitized_jsonl).is_file():
            note("Building database")
            db_work = workp / "trace.duckdb"
            _trace_db.materialize(sanitized_jsonl, db_work)
            db_dst = LOCAL_EXPORT_DIR / "trace.duckdb"
            shutil.copyfile(db_work, db_dst)
            db_export = str(db_dst)
            db_gz_dst = LOCAL_EXPORT_DIR / "trace.duckdb.gz"
            # compresslevel=1: this .gz is a LOCAL-only transfer copy (loopback / tunnel, never uploaded),
            # so trade a few MB for ~2 s off the prepare critical path. Gunzipped bytes are identical.
            with open(db_work, "rb") as _src, gzip.open(db_gz_dst, "wb", compresslevel=1) as _gz:
                shutil.copyfileobj(_src, _gz, length=1 << 20)
            db_gz_export = str(db_gz_dst)

        # Load the (clipped) raw map into memory for /round-raw while the work dir still exists.
        note("Indexing raw originals")
        raw_map: dict[str, Any] = {}
        rr = exported.get("round_raw_json")
        if rr and Path(rr).is_file():
            try:
                raw_map = json.loads(Path(rr).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw_map = {}

    sanitized_gz = exported.get("sanitized_gz")
    san_bytes = (
        Path(sanitized_gz).stat().st_size if sanitized_gz and Path(sanitized_gz).is_file() else 0
    )
    public = {k: v for k, v in meta.items() if k != "files"}
    public["sanitizedBytes"] = san_bytes
    public["dbAvailable"] = db_export is not None
    _PREPARE_CACHE.update(
        {
            "sig": sig,
            "meta": public,
            "sanitized_gz": sanitized_gz,
            "round_raw_map": raw_map,
            "duckdb": db_export,
            "duckdb_gz": db_gz_export,
            "analytics": {},  # per-tz bulk_json cache, invalidated by this fresh prepare
        }
    )
    return public


async def local_trace_prepare(request: Request) -> StreamingResponse:
    """Run the native sanitize (once, cached) and STREAM progress as newline-delimited JSON so the
    browser can show which stage the server is on. Each line is `{"stage": "..."}`; the final line is
    `{"meta": {...}}` (rawAvailable + titles + counts, which drive the dashboard) or `{"error": "..."}`.
    Streamed because the sanitize of a real history takes tens of seconds — a black-box POST looks hung.
    """
    refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes", "on"}

    async def events():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        DONE = object()

        def on_progress(stage: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, {"stage": str(stage)})

        def work() -> None:
            try:
                meta = _run_prepare_blocking(refresh, on_progress)
                loop.call_soon_threadsafe(queue.put_nowait, {"meta": meta})
            except FileNotFoundError as exc:
                loop.call_soon_threadsafe(queue.put_nowait, {"error": str(exc)})
            except Exception as exc:  # ingest failure — surface in-band
                loop.call_soon_threadsafe(queue.put_nowait, {"error": f"Local sanitize failed: {exc}"})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, DONE)

        async with _PREPARE_LOCK:
            fut = loop.run_in_executor(None, work)
            while True:
                item = await queue.get()
                if item is DONE:
                    break
                yield (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
            await fut

    return StreamingResponse(events(), media_type="application/x-ndjson")


async def local_trace_sanitized() -> Response:
    """Stream the sanitized .jsonl.gz produced by the last prepare (much smaller than the raw trace)."""
    path = _PREPARE_CACHE.get("sanitized_gz")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "No sanitized export yet — POST /api/local-trace/prepare first.")
    return StreamingResponse(
        _iter_file(Path(path)),
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="local-trace.sanitized.jsonl.gz"'},
    )


async def local_trace_round_raw(request: Request) -> JSONResponse:
    """One round's LOCAL-only original text by sanitized trace_key (drives the raw drill-down). Served
    from the round_raw.json the native prepare wrote; local-only, same privacy as the in-browser path."""
    key = request.query_params.get("key", "")
    raw_map = _PREPARE_CACHE.get("round_raw_map") or {}
    entry = raw_map.get(key)
    if entry is None:
        raise HTTPException(404, "No raw original for this round.")
    return JSONResponse(entry)


# Serialize the (multi-second) bulk_json compute so two concurrent requests for the same tz don't both
# run it; session-detail is sub-second and read-only, so it runs unlocked.
_COMPUTE_LOCK = asyncio.Lock()


def _prepared_db() -> str:
    """The materialized DuckDB path from the last prepare, or a 404 if none/native-analytics missing."""
    if _analyze is None or _trace_db is None:
        raise HTTPException(503, "Native analytics unavailable on this deployment.")
    db = _PREPARE_CACHE.get("duckdb")
    if not db or not Path(db).is_file():
        raise HTTPException(404, "No analytics yet — POST /api/local-trace/prepare first.")
    return db


async def local_trace_analytics(request: Request) -> Response:
    """The whole dashboard payload, computed server-side (analyze.bulk_json over the materialized DuckDB)
    and returned as ~60 KB JSON. The browser renders it directly — no in-browser DB build, no Pyodide
    compute. Cached per tz offset (the only per-request input)."""
    db = _prepared_db()
    try:
        tz = int(request.query_params.get("tz", "0"))
    except ValueError:
        tz = 0
    cache = _PREPARE_CACHE.setdefault("analytics", {})
    payload = cache.get(tz)
    if payload is None:
        loop = asyncio.get_running_loop()
        async with _COMPUTE_LOCK:
            payload = cache.get(tz)
            if payload is None:
                payload = await loop.run_in_executor(None, _analyze.bulk_json, db, tz)
                cache[tz] = payload
    return Response(content=payload, media_type="application/json")


async def local_trace_session_detail(request: Request) -> Response:
    """One session's per-round timeline, computed server-side (analyze.session_detail_json). Sub-second."""
    db = _prepared_db()
    sid = request.query_params.get("id", "")
    if not sid:
        raise HTTPException(400, "missing ?id=<sessionId>")
    loop = asyncio.get_running_loop()
    payload = await loop.run_in_executor(None, _analyze.session_detail_json, db, sid)
    return Response(content=payload, media_type="application/json")


async def local_trace_duckdb() -> Response:
    """Stream the gzipped materialized DuckDB from the last prepare. The browser fetches this in the
    BACKGROUND (off the dashboard's critical path) and primes its query cache so the AI assistant's
    in-browser DuckDB load is a cache hit (skips the slow WASM materialize)."""
    path = _PREPARE_CACHE.get("duckdb_gz")
    if not path or not Path(path).is_file():
        raise HTTPException(404, "No database yet — POST /api/local-trace/prepare first.")
    return StreamingResponse(
        _iter_file(Path(path)),
        media_type="application/gzip",
        headers={"Content-Disposition": 'attachment; filename="trace.duckdb.gz"'},
    )


# Register the local-trace endpoints ONLY in the frontend role. In the master-server role they are
# never registered, so the master has no code path that reads the operator's ~/.claude / ~/.codex,
# and the frontend's auto-load probe gets a 404 and falls back to drag-and-drop.
if SERVE_LOCAL_TRACE:
    app.add_api_route("/api/local-trace", local_trace, methods=["GET", "HEAD"])
    app.add_api_route("/api/local-trace/meta", local_trace_meta, methods=["GET"])
    # Local executor: native sanitize + the small sanitized .gz + the raw drill-down map.
    app.add_api_route("/api/local-trace/prepare", local_trace_prepare, methods=["POST"])
    app.add_api_route("/api/local-trace/sanitized", local_trace_sanitized, methods=["GET"])
    app.add_api_route("/api/local-trace/round-raw", local_trace_round_raw, methods=["GET"])
    # Server-side analytics (the dashboard payload + drill-down) + the DuckDB for the assistant.
    app.add_api_route("/api/local-trace/analytics", local_trace_analytics, methods=["GET"])
    app.add_api_route("/api/local-trace/session-detail", local_trace_session_detail, methods=["GET"])
    app.add_api_route("/api/local-trace/duckdb", local_trace_duckdb, methods=["GET"])


@app.get("/api/sidecar-info")
async def sidecar_info() -> JSONResponse:
    """Lets the served frontend learn it's running behind a local-trace sidecar, so Analyze offers the
    local executor. In the frontend role the whole site IS the master's (reverse-proxied below); this
    endpoint plus the /api/local-trace/* executor are the only things answered locally. Absent on the
    hosted master (404) -> Analyze falls back to drag-and-drop and every tab is the master's own."""
    return JSONResponse(
        {
            "role": "master" if MASTER_SERVER else "frontend",
            "master": None if MASTER_SERVER else MASTER_SERVER_ADDRESS,
            "localTrace": SERVE_LOCAL_TRACE,
        }
    )


# ---- 2. chat WebSocket reverse-proxy -------------------------------------------------------
def _ws_connect_with_headers(target: str, headers: dict[str, str]):
    """Open an upstream WS, forwarding ``headers``. websockets>=13 names the kwarg
    ``additional_headers``; the legacy client names it ``extra_headers`` — try the modern one first."""
    try:
        return ws_connect(target, max_size=None, additional_headers=headers)
    except TypeError:
        return ws_connect(target, max_size=None, extra_headers=headers)


@app.websocket("/api/chat/ws")
async def proxy_chat_ws(browser: WebSocket) -> None:
    """Bidirectional JSON-text frame pump between the browser and the master's assistant socket.

    The master's user-source loop emits ``tool_request`` and blocks on the browser's ``tool_result``,
    so the proxy must stay alive for the whole (possibly long) turn — no per-frame timeout. We only
    tear down when either side actually closes.
    """
    await browser.accept()
    # Forward the real client IP so the master's AI backend rate-limits per genuine client, not per
    # this loopback proxy. Mirrors the HTTP proxy's X-Forwarded-For handling: append the connecting
    # peer to any prior XFF, and the backend trusts the right-most (proxy-added) entry.
    client_host = browser.client.host if browser.client else ""
    prior_xff = browser.headers.get("x-forwarded-for")
    xff = f"{prior_xff}, {client_host}" if prior_xff else client_host
    ws_headers = {"x-forwarded-for": xff} if xff else {}
    try:
        async with _ws_connect_with_headers(WS_TARGET, ws_headers) as upstream:

            async def browser_to_upstream() -> None:
                while True:
                    data = await browser.receive_text()
                    await upstream.send(data)

            async def upstream_to_browser() -> None:
                async for data in upstream:
                    if isinstance(data, (bytes, bytearray)):
                        data = data.decode("utf-8", "replace")
                    await browser.send_text(data)

            t1 = asyncio.create_task(browser_to_upstream())
            t2 = asyncio.create_task(upstream_to_browser())
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:  # surface a non-cancel error (helps debugging), ignore disconnects
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                    pass
    except Exception:
        # Upstream unreachable / handshake failed / either side dropped — close the browser side.
        pass
    finally:
        try:
            await browser.close()
        except Exception:
            pass


# ---- 3. catch-all HTTP reverse-proxy (/api/pool, /api/contribute*, /api/health, …) --------
def _upstream_base(path: str) -> str:
    """Mirror the dev/prod proxy split: /api/chat* -> the chat backend, the rest -> contribute/master.

    In frontend-only mode both are the master (it splits internally); in full-stack mode they are the
    two local backends.
    """
    return CHAT_UPSTREAM if path == "chat" or path.startswith("chat/") else API_UPSTREAM


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_api(path: str, request: Request) -> Response:
    """Forward any remaining /api call to the chosen upstream, streaming the response back unbuffered.

    Streaming both ways preserves the contribute protocol's "each chunk POST returns only once the
    upstream has stored it" semantics. X-Forwarded-For is appended so the upstream's per-IP rate limit
    sees the real client, not this loopback proxy.
    """
    client: httpx.AsyncClient = app.state.client
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
    client_host = request.client.host if request.client else ""
    prior_xff = request.headers.get("x-forwarded-for")
    headers["x-forwarded-for"] = f"{prior_xff}, {client_host}" if prior_xff else client_host

    target = f"{_upstream_base(path)}/api/{path}"
    if request.url.query:
        target += "?" + request.url.query
    body = await request.body()
    upstream_req = client.build_request(request.method, target, headers=headers, content=body)
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Could not reach the upstream backend: {exc}")

    resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP}
    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


# ---- 4. the site itself (declared LAST so the /api routes above take precedence) -----------
if MASTER_SERVER:
    # Master role: serve the locally-built site.
    if DIST_ROOT.is_dir():
        app.mount("/", StaticFiles(directory=str(DIST_ROOT), html=True), name="dist")
    else:  # pragma: no cover - misconfig guard

        @app.get("/{_path:path}")
        async def _no_dist(_path: str) -> JSONResponse:
            return JSONResponse(
                {"error": f"Built site not found at {DIST_ROOT}. Run `just site` (or set LOCAL_DIST_ROOT)."},
                status_code=503,
            )

else:
    # Frontend role: reverse-proxy the ENTIRE site to the master, so every non-Analyze page — overview,
    # comparison, the contributed pool, the /lab and /exp figure pages — is the master's REAL page with
    # real data. No redirects, no figure-less local dist, nothing to strand the user on another origin.
    # The only locally-answered routes are /api/sidecar-info + /api/local-trace/* (declared above), so
    # the browser sees the master site while Analyze detects the sidecar and runs the local executor.
    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    )
    async def proxy_site(path: str, request: Request) -> Response:
        client: httpx.AsyncClient = app.state.client
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP}
        target = f"{MASTER_SERVER_ADDRESS}/{path}"
        if request.url.query:
            target += "?" + request.url.query
        body = await request.body()
        upstream_req = client.build_request(request.method, target, headers=headers, content=body)
        try:
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Could not reach the master site at {MASTER_SERVER_ADDRESS}: {exc}")
        resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _HOP}
        return StreamingResponse(
            upstream_resp.aiter_raw(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(upstream_resp.aclose),
        )
