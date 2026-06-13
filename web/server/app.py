#!/usr/bin/env python3
"""FastAPI sidecar for contributing already-sanitized traces to the community pool.

``POST /api/contribute`` is **non-blocking**: it only receives the upload, registers a job, and
returns ``202 {job_id}`` immediately. The heavy work runs in a background task:

    gzip integrity -> JSONL parse -> schema sniff -> reject if any sensitive key or tools[].input
    survives  ──then under a lock──  content-hash idempotency -> session-fingerprint dedup (skip
    seen sessions) -> append the new rows -> add subtotals to running totals -> write the snapshot.

The client polls ``GET /api/contribute/status/{job_id}`` until the job is ``done`` (with the
accepted counts) or ``rejected`` (with the offending paths), then shows a result dialog.
``GET /api/pool`` serves the cached PoolPreview snapshot the dashboard hydrates from. The trace is
never re-sanitized server-side: the gate *rejects* leaks, it does not scrub.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import stats
from .fingerprint import session_fingerprints
from .store import LocalStore, make_contribution_entry, pool_preview

# Privacy gate lives in scripts/trace_privacy (shared with the sanitizer).
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from trace_privacy import find_sensitive, row_has_leak  # noqa: E402

# ---- config (env-overridable) -------------------------------------------------------------
MAX_UPLOAD_BYTES = int(os.environ.get("CONTRIB_MAX_UPLOAD_BYTES", 256 * 1024 * 1024))
# Decompressed-size ceiling (zip-bomb guard). The cap above bounds the COMPRESSED upload, but a
# pathological gzip inflates ~1000x — 256 MiB in could become hundreds of GiB out. We stream-
# decompress and abort past this many bytes, so a bomb can't OOM the worker. 8 GiB matches the
# untrusted ingest ceiling (web/payload/ingest.MAX_TOTAL_UNCOMPRESSED); a real sanitized round-trace
# is far smaller, so this only ever trips on abuse.
MAX_DECOMPRESSED_BYTES = int(
    os.environ.get("CONTRIB_MAX_DECOMPRESSED_BYTES", 8 * 1024 * 1024 * 1024)
)
RATE_LIMIT_MAX = int(os.environ.get("CONTRIB_RATE_LIMIT_MAX", 10))
RATE_LIMIT_WINDOW_S = float(os.environ.get("CONTRIB_RATE_LIMIT_WINDOW_S", 3600))
# A normalized round row must carry at least these — distinguishes a real trace from random JSON.
REQUIRED_KEYS = ("provider", "session_id", "round_index", "input_tokens_total")
_TRUTHY = {"1", "true", "yes", "on"}

# Chunked-upload config. The client streams the .gz in fixed-size chunks; each chunk POST only
# returns once the server has actually received that chunk, so the client measures real
# server-receive progress (xhr.upload.onprogress can't — it reports the local socket buffer).
MAX_CHUNK_BYTES = int(os.environ.get("CONTRIB_MAX_CHUNK_BYTES", 8 * 1024 * 1024))
UPLOAD_TTL_S = float(os.environ.get("CONTRIB_UPLOAD_TTL_S", 600))
MAX_PARTIAL_UPLOADS = int(os.environ.get("CONTRIB_MAX_PARTIAL_UPLOADS", 16))
_UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Append-only JSONL logs (DuckDB-loadable). Contribution audit = who uploaded what + the outcome;
# pageview metrics = the frontend beacon (access counts + top pages, no dwell time).
_DATA_ROOT = Path(__file__).resolve().parent / "data"
AUDIT_LOG = Path(os.environ.get("CONTRIB_AUDIT_LOG", str(_DATA_ROOT / "audit" / "contributions.jsonl")))
METRICS_LOG = Path(os.environ.get("METRICS_LOG", str(_DATA_ROOT / "metrics" / "pageviews.jsonl")))
METRICS_RATE_LIMIT_MAX = int(os.environ.get("METRICS_RATE_LIMIT_MAX", 600))
METRICS_RATE_LIMIT_WINDOW_S = float(os.environ.get("METRICS_RATE_LIMIT_WINDOW_S", 3600))

app = FastAPI(title="SyFI Trace Atlas Contribute API")
_store = LocalStore()
_write_lock = asyncio.Lock()  # serialize index read-modify-write (single-worker)
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_metrics_hits: dict[str, deque[float]] = defaultdict(deque)

# Async job registry. Uploads return a job_id immediately; processing happens in a background task
# and the client polls for the outcome. In-memory is fine for a single-worker sidecar (jobs are
# transient and lost on restart, which is acceptable — the pool itself is durable on disk).
JOB_TTL_S = float(os.environ.get("CONTRIB_JOB_TTL_S", 3600))
_jobs: dict[str, dict] = {}
_bg_tasks: set[asyncio.Task] = set()

# In-flight chunked uploads, keyed by client-supplied upload id. Each holds the received chunks
# (by offset) until finished/assembled. In-memory matches the rest of the sidecar (the whole file
# already lives in memory during processing); bounded by MAX_PARTIAL_UPLOADS + a TTL sweep.
_uploads: dict[str, dict] = {}


# ---- helpers ------------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    # This sidecar binds 127.0.0.1, so the only path in is the dev-server proxy (Vite, `xfwd: true`),
    # which APPENDS the real client IP to X-Forwarded-For. Trust the RIGHT-most entry — the one the
    # proxy added — not the left-most, which a client can spoof by sending its own XFF header.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    hits = _rate_hits[ip]
    while hits and now - hits[0] > RATE_LIMIT_WINDOW_S:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_MAX:
        raise HTTPException(429, "Too many contributions from this client; try again later.")
    hits.append(now)


def _check_metrics_rate_limit(ip: str) -> bool:
    """Generous per-IP cap so the pageview beacon can't be spammed into a runaway log. True = allow."""
    now = time.monotonic()
    hits = _metrics_hits[ip]
    while hits and now - hits[0] > METRICS_RATE_LIMIT_WINDOW_S:
        hits.popleft()
    if len(hits) >= METRICS_RATE_LIMIT_MAX:
        return False
    hits.append(now)
    return True


def _append_jsonl(path: Path, obj: dict) -> None:
    """Append one JSONL line. Best-effort: logging must never break the request path."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _read_capped(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"Upload exceeds {MAX_UPLOAD_BYTES} bytes.")
        chunks.append(chunk)
    return b"".join(chunks)


def _gunzip_capped(raw: bytes, limit: int) -> bytes:
    """Decompress a gzip blob, aborting once the output exceeds ``limit`` bytes (zip-bomb guard).

    Reads the decompressed stream in bounded chunks so a pathological compression ratio can't
    balloon memory: we stop the moment we cross ``limit`` instead of materializing the whole
    (potentially hundreds-of-GiB) output. Raises HTTPException(413) past the cap; gzip framing
    errors surface as OSError for the caller to map to a 422.
    """
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                break
            out += chunk
            if len(out) > limit:
                raise HTTPException(413, f"Upload decompresses past the {limit}-byte limit — refusing it.")
    return bytes(out)


def _parse_and_validate(raw: bytes) -> list[dict]:
    """Decompress, parse JSONL, schema-sniff, and reject any privacy leak. Raises HTTPException."""
    try:
        text = _gunzip_capped(raw, MAX_DECOMPRESSED_BYTES).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise HTTPException(422, f"Not a valid gzip/UTF-8 upload: {exc}")

    rows: list[dict] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(422, f"Invalid JSONL at line {line_no}: {exc}")
        if not isinstance(row, dict):
            raise HTTPException(422, f"Line {line_no} is not a JSON object.")
        missing = [k for k in REQUIRED_KEYS if k not in row]
        if missing:
            raise HTTPException(422, f"Line {line_no} missing required keys: {missing}.")
        # Fast clean-case scan; only pay for breadcrumb paths when something actually leaks.
        if row_has_leak(row):
            raise HTTPException(
                422,
                {
                    "error": "Upload still contains sensitive data — refusing it.",
                    "line": line_no,
                    "paths": find_sensitive(row)[:10],
                },
            )
        rows.append(row)
    if not rows:
        raise HTTPException(422, "Upload contained no rows.")
    return rows


def _gzip_rows(rows: list[dict]) -> bytes:
    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
    # Level 6: ~same ratio as the default 9 at a fraction of the CPU. These are stored artifacts,
    # not hot-served, so the speed/size trade favours 6.
    return gzip.compress(body.encode("utf-8"), compresslevel=6)


def _process_accept(raw: bytes, rows: list[dict]) -> dict:
    """Synchronous critical section: dedup, append, bookkeep. Run under ``_write_lock``."""
    upload_sha = hashlib.sha256(raw).hexdigest()
    if _store.has_upload(upload_sha):
        return {"accepted": 0, "duplicate": True, "new_sessions": 0,
                "skipped_sessions": 0, "rows_added": 0}

    fingerprints = session_fingerprints(rows)
    seen = _store.seen_fingerprints()
    new_fps = {fp: idxs for fp, idxs in fingerprints.items() if fp not in seen}
    skipped = len(fingerprints) - len(new_fps)
    if not new_fps:
        return {"accepted": 0, "duplicate": False, "new_sessions": 0,
                "skipped_sessions": skipped, "rows_added": 0}

    new_indices = sorted({i for idxs in new_fps.values() for i in idxs})
    new_rows = [rows[i] for i in new_indices]
    sub = stats.subtotals(new_rows)
    entry = make_contribution_entry(
        upload_sha, datetime.now(timezone.utc).isoformat(), sub, set(new_fps)
    )
    # Nothing filtered → the stored object is exactly the upload; reuse its bytes verbatim instead
    # of re-serializing + recompressing the whole trace (the common, expensive case).
    gz = raw if len(new_indices) == len(rows) else _gzip_rows(new_rows)
    _store.put_upload(upload_sha, gz)
    _store.record(entry, set(new_fps))
    _store.write_summary(pool_preview(_store.read_index()))
    return {"accepted": len(new_rows), "duplicate": False, "new_sessions": len(new_fps),
            "skipped_sessions": skipped, "rows_added": len(new_rows)}


# ---- background job processing ------------------------------------------------------------
def _prune_jobs(now: float) -> None:
    for jid in [j for j, v in _jobs.items() if now - v["created_at"] > JOB_TTL_S]:
        _jobs.pop(jid, None)


def _prune_uploads(now: float) -> None:
    for uid in [u for u, v in _uploads.items() if now - v["created_at"] > UPLOAD_TTL_S]:
        _uploads.pop(uid, None)


def _assemble_upload(up: dict) -> bytes:
    """Concatenate received chunks in offset order; reject gaps/overlaps or a short upload."""
    raw = bytearray()
    for off in sorted(up["chunks"]):
        if off != len(raw):  # offsets must tile [0, total) exactly — no gap, no overlap
            raise HTTPException(400, "Upload is missing chunks — please retry.")
        raw += up["chunks"][off]
    if len(raw) != up["total"]:
        raise HTTPException(400, "Upload is incomplete — please retry.")
    return bytes(raw)


def _start_job(raw: bytes, *, ip: str, ua: str) -> str:
    """Register a background validation/dedup job for ``raw`` and return its id."""
    now = time.time()
    _prune_jobs(now)
    job_id = secrets.token_hex(8)
    _jobs[job_id] = {"status": "processing", "created_at": now}
    task = asyncio.create_task(_run_job(job_id, raw, ip=ip, ua=ua))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return job_id


async def _run_job(job_id: str, raw: bytes, *, ip: str, ua: str) -> None:
    """Validate → dedup → append in the background; record the outcome on the job + an audit line."""
    audit: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "ua": ua,
        "upload_sha": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "consent": True,
    }
    try:
        rows = await asyncio.to_thread(_parse_and_validate, raw)  # raises HTTPException on a leak
        async with _write_lock:
            result = await asyncio.to_thread(_process_accept, raw, rows)
        _jobs[job_id].update(status="done", result=result)
        outcome = "duplicate" if result.get("duplicate") else ("accepted" if result.get("accepted") else "skipped")
        audit.update(
            outcome=outcome,
            accepted=result.get("accepted"),
            new_sessions=result.get("new_sessions"),
            skipped_sessions=result.get("skipped_sessions"),
            rows_added=result.get("rows_added"),
        )
    except HTTPException as exc:
        _jobs[job_id].update(status="rejected", code=exc.status_code, error=exc.detail)
        audit.update(outcome="rejected", reject_code=exc.status_code, reject_detail=exc.detail)
    except Exception as exc:  # noqa: BLE001 — never let a worker die silently
        _jobs[job_id].update(status="error", error=str(exc))
        audit.update(outcome="error", error=str(exc))
    finally:
        _append_jsonl(AUDIT_LOG, audit)


# ---- endpoints ----------------------------------------------------------------------------
@app.get("/api/pool")
async def get_pool() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(_store.read_summary))


@app.post("/api/contribute")
async def contribute(
    request: Request,
    file: UploadFile = File(...),
    consent: str = Form(...),
) -> JSONResponse:
    """Single-shot multipart upload (used by tests/curl) — hand off to a worker, return at once."""
    if consent.strip().lower() not in _TRUTHY:
        raise HTTPException(400, "Consent is required to contribute.")
    ip = _client_ip(request)
    _check_rate_limit(ip)
    raw = await _read_capped(file)  # the only synchronous cost: the upload transfer itself
    ua = request.headers.get("user-agent", "")
    return JSONResponse({"job_id": _start_job(raw, ip=ip, ua=ua), "status": "processing"}, status_code=202)


@app.post("/api/contribute/chunk")
async def contribute_chunk(request: Request) -> JSONResponse:
    """Receive one chunk of a streamed upload. Returns once the chunk is actually stored, so the
    client's per-chunk completion is a *real* server-receive acknowledgment (the basis for honest
    progress + throughput). Chunks are addressed by byte offset and may arrive concurrently."""
    uid = request.headers.get("x-upload-id", "")
    if not _UPLOAD_ID_RE.match(uid):
        raise HTTPException(400, "Missing or malformed upload id.")
    try:
        offset = int(request.headers.get("x-chunk-offset", ""))
        total = int(request.headers.get("x-total-bytes", ""))
    except ValueError:
        raise HTTPException(400, "Missing or malformed chunk headers.")
    if total <= 0 or total > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Upload exceeds the {MAX_UPLOAD_BYTES}-byte limit.")
    clen = int(request.headers.get("content-length", "0") or 0)
    if clen > MAX_CHUNK_BYTES:
        raise HTTPException(413, f"Chunk exceeds the {MAX_CHUNK_BYTES}-byte limit.")

    body = await request.body()
    if len(body) > MAX_CHUNK_BYTES:
        raise HTTPException(413, f"Chunk exceeds the {MAX_CHUNK_BYTES}-byte limit.")
    if offset < 0 or offset + len(body) > total:
        raise HTTPException(400, "Chunk falls outside the declared upload size.")

    now = time.time()
    _prune_uploads(now)
    up = _uploads.get(uid)
    if up is None:
        if len(_uploads) >= MAX_PARTIAL_UPLOADS:
            raise HTTPException(429, "Too many uploads in progress; try again shortly.")
        up = _uploads[uid] = {"chunks": {}, "received": 0, "total": total, "created_at": now}
    elif up["total"] != total:
        raise HTTPException(400, "Conflicting total size for this upload.")
    if offset not in up["chunks"]:  # idempotent: a retried chunk doesn't double-count
        up["chunks"][offset] = body
        up["received"] += len(body)
    return JSONResponse({"received": up["received"], "total": up["total"]})


@app.post("/api/contribute/finish")
async def contribute_finish(request: Request) -> JSONResponse:
    """Assemble a completed chunked upload and hand it to a background worker."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "Expected a JSON body.")
    uid = str(data.get("upload_id", ""))
    consent = data.get("consent")
    consented = consent is True or (isinstance(consent, str) and consent.strip().lower() in _TRUTHY)
    if not consented:
        raise HTTPException(400, "Consent is required to contribute.")
    ip = _client_ip(request)
    _check_rate_limit(ip)

    up = _uploads.pop(uid, None)
    if up is None:
        raise HTTPException(404, "No such upload, or it expired before finishing.")
    ua = request.headers.get("user-agent", "")
    return JSONResponse(
        {"job_id": _start_job(_assemble_upload(up), ip=ip, ua=ua), "status": "processing"},
        status_code=202,
    )


@app.get("/api/contribute/status/{job_id}")
async def contribute_status(job_id: str) -> JSONResponse:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown or expired job.")
    payload = {k: v for k, v in job.items() if k != "created_at"}
    return JSONResponse(payload)


@app.post("/api/metrics")
async def metrics(request: Request) -> Response:
    """Record one pageview beacon from the frontend (access counts + top pages; no dwell time).

    Best-effort and fire-and-forget: the browser sends this via ``navigator.sendBeacon`` on load and
    on surface change, so it must stay cheap and never error the client. Generous per-IP cap only.
    """
    ip = _client_ip(request)
    if not _check_metrics_rate_limit(ip):
        return Response(status_code=429)
    try:
        data = await request.json()  # sendBeacon body is JSON regardless of content-type
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _append_jsonl(
        METRICS_LOG,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
            "ua": request.headers.get("user-agent", ""),
            "path": str(data.get("path") or "")[:300],
            "hash": str(data.get("hash") or "")[:100],
            "ref": str(data.get("ref") or "")[:500],
        },
    )
    return Response(status_code=204)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}
