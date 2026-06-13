#!/usr/bin/env python3
"""Format-aware ingest: turn whatever the user dropped into normalized + sanitized round-trace.

The web app only ever consumes a *sanitized* normalized round-trace, but users may drop a raw
``~/.claude/projects/.../*.jsonl`` dump, a ``~/.codex/sessions`` dump, an already-normalized trace,
or an already-sanitized one — bare, gzipped, tar.gz'd, or zipped. This module sniffs what came in
(content-first; path markers only refine) and runs *only the stages still needed*, reusing the
canonical scripts verbatim so the browser path is byte-identical to the native CLI pipeline:

    extract_claude_rounds.py / extract_codex_rounds.py  (raw sessions -> normalized rounds)
    sanitize_round_trace.py                             (normalized   -> sanitized)

Like ``web/payload/driver.py`` it is deliberately pure-Python (no Pyodide imports) so it runs
unchanged under native CPython, which lets us verify parity before it ever touches the browser:

    python web/payload/ingest.py <archive|jsonl|.gz> <out_dir>

The Pyodide worker imports :func:`prepare`; the browser never sees raw rows leave the page — only the
derived normalized/sanitized artifacts surface, and only as user-initiated downloads.

Container detection (magic bytes):
    1f 8b            -> gzip; if the decompressed stream opens as a tar it's a tar.gz, else a .jsonl
    50 4b 03 04 (PK) -> zip
    otherwise        -> plain text (.jsonl / .json)

Per-file classification (scan the first records, strongest signal wins):
    round  = dict carrying all of REQUIRED_KEYS  (then leak-scan: normalized vs sanitized)
    claude = raw Claude session record (a `message` object + assistant/user `type`/`uuid`)
    codex  = raw Codex session record (a `payload` object + a session/turn/response `type`)
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

# Locate the canonical normalize/sanitize scripts in both layouts and import them flat (they import
# each other by bare name: extract_codex_rounds -> extract_claude_rounds, sanitize -> trace_privacy).
#   browser: this file at /repo/ingest.py, scripts mounted at /repo/normalize/
#   native:  this file at web/payload/ingest.py, scripts at <repo>/scripts/
_HERE = Path(__file__).resolve().parent
for _cand in (_HERE / "normalize", _HERE.parent.parent / "scripts"):
    if (_cand / "extract_claude_rounds.py").exists() and str(_cand) not in sys.path:
        sys.path.insert(0, str(_cand))

from extract_claude_rounds import extract_session_with_key, write_rounds_jsonl  # noqa: E402
from extract_codex_rounds import extract_codex_session  # noqa: E402
from sanitize_round_trace import DEFAULT_SEED, StableIdSanitizer, sanitize_row  # noqa: E402
from trace_privacy import row_has_leak  # noqa: E402

# Must match web/server/app.py::REQUIRED_KEYS — the structural fingerprint of a normalized round row.
REQUIRED_KEYS = ("provider", "session_id", "round_index", "input_tokens_total")
# Top-level record `type`s that mark a raw Codex rollout (extract_codex_rounds consumes these).
CODEX_TOP_TYPES = {"session_meta", "turn_context", "response_item", "event_msg"}

# Archive guards (zip-bomb / zip-slip). The whole upload already lives in memory in the browser, so
# these bound pathological inputs rather than enforce a real quota. They are the strict default for
# UNTRUSTED input (anything the user drags in). A trusted source — the local self-deploy sidecar
# streaming the user's *own* ~/.claude + ~/.codex from disk — raises them via prepare(trusted=True),
# since a real combined history (~1.3 GB+) legitimately exceeds the untrusted ceiling.
MAX_MEMBERS = 20_000
MAX_TOTAL_UNCOMPRESSED = 8 * 1024 * 1024 * 1024  # 8 GiB of JSON across all members
TRUSTED_MAX_MEMBERS = 200_000
TRUSTED_MAX_TOTAL_UNCOMPRESSED = 16 * 1024 * 1024 * 1024  # 16 GiB — still a guard, just higher
_JSON_SUFFIXES = (".jsonl", ".json", ".ndjson")


class IngestError(Exception):
    """Raised when nothing recognizable (no sessions, no round rows) was found."""


# ---------------------------------------------------------------------------
# Record / file classification
# ---------------------------------------------------------------------------
def _iter_json_records(data: bytes, *, limit: int | None = None) -> Iterable[dict]:
    """Yield JSON object records from a JSONL byte stream (best effort, skips junk lines).

    Falls back to a single top-level JSON array when the stream isn't line-delimited.
    """
    text = data.decode("utf-8", errors="replace")
    seen = 0
    produced = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        produced = True
        if isinstance(row, dict):
            yield row
            seen += 1
            if limit is not None and seen >= limit:
                return
    if not produced:
        try:
            whole = json.loads(text)
        except json.JSONDecodeError:
            return
        if isinstance(whole, list):
            for row in whole:
                if isinstance(row, dict):
                    yield row
                    seen += 1
                    if limit is not None and seen >= limit:
                        return


def _looks_round(row: dict) -> bool:
    return all(key in row for key in REQUIRED_KEYS)


def _looks_raw_claude(row: dict) -> bool:
    if not isinstance(row.get("message"), dict):
        return False
    return row.get("type") in {"assistant", "user"} or isinstance(row.get("uuid"), str)


def _looks_raw_codex(row: dict) -> bool:
    return isinstance(row.get("payload"), dict) and row.get("type") in CODEX_TOP_TYPES


def classify_records(records: Iterable[dict]) -> str:
    """Classify a file's records: 'round' | 'claude' | 'codex' | 'unknown' (round wins)."""
    saw_claude = saw_codex = False
    for row in records:
        if _looks_round(row):
            return "round"
        if not saw_claude and _looks_raw_claude(row):
            saw_claude = True
        if not saw_codex and _looks_raw_codex(row):
            saw_codex = True
    if saw_codex:
        return "codex"
    if saw_claude:
        return "claude"
    return "unknown"


# ---------------------------------------------------------------------------
# Container sniffing + safe archive extraction
# ---------------------------------------------------------------------------
def _safe_rel(name: str) -> str | None:
    """Normalize an archive member name; reject absolute paths and `..` traversal (zip-slip)."""
    name = name.replace("\\", "/")
    norm = os.path.normpath(name)
    if norm.startswith("/") or norm == ".." or norm.startswith("../") or "/../" in norm:
        return None
    return norm


def _is_json_member(name: str) -> bool:
    return name.lower().endswith(_JSON_SUFFIXES)


def _unpack_archive(
    data: bytes,
    kind: str,
    dest: Path,
    warnings: list[str],
    *,
    max_members: int = MAX_MEMBERS,
    max_total: int = MAX_TOTAL_UNCOMPRESSED,
) -> list[Path]:
    """Extract JSON members of a zip / tar.gz into ``dest``; return their paths. Caps guard bombs."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    total = 0
    count = 0

    def _emit(rel: str, payload: bytes) -> None:
        nonlocal total, count
        safe = _safe_rel(rel)
        if safe is None:
            warnings.append(f"skipped unsafe archive path: {rel}")
            return
        if not _is_json_member(safe):
            return
        count += 1
        total += len(payload)
        if count > max_members or total > max_total:
            raise IngestError("archive is too large or has too many files to process safely")
        target = dest / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        out.append(target)

    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if not _is_json_member(info.filename):
                    continue
                _emit(info.filename, zf.read(info))
    else:  # tar.gz
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile() or not _is_json_member(member.name):
                    continue
                fh = tf.extractfile(member)
                if fh is None:
                    continue
                _emit(member.name, fh.read())
    return out


def _gunzip_capped(data: bytes, limit: int) -> bytes:
    """Decompress a gzip blob, aborting past ``limit`` bytes (zip-bomb guard for the single-file path).

    The archive paths already bound their uncompressed total in ``_unpack_archive._emit``; this gives
    the bare ``*.jsonl.gz`` path the same ceiling instead of materializing an unbounded inflate.
    """
    out = bytearray()
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                break
            out += chunk
            if len(out) > limit:
                raise IngestError("gzip upload is too large to process safely")
    return bytes(out)


def _sniff_and_collect(
    input_path: Path,
    work_dir: Path,
    warnings: list[str],
    *,
    max_members: int = MAX_MEMBERS,
    max_total: int = MAX_TOTAL_UNCOMPRESSED,
) -> list[Path]:
    """Return the list of candidate JSON files to classify (unpacking archives as needed)."""
    data = input_path.read_bytes()
    caps = {"max_members": max_members, "max_total": max_total}
    if data[:2] == b"\x1f\x8b":  # gzip
        try:
            inner = _gunzip_capped(data, max_total)
        except OSError as exc:
            raise IngestError(f"not a valid gzip upload: {exc}")
        # gzipped tar vs gzipped jsonl: a tar's first 512-byte header has 'ustar' at offset 257.
        if inner[257:262] == b"ustar":
            return _unpack_archive(data, "tar", work_dir / "_unpacked", warnings, **caps)
        single = work_dir / "input.jsonl"
        single.write_bytes(inner)
        return [single]
    if data[:4] == b"PK\x03\x04" or data[:2] == b"PK":  # zip
        return _unpack_archive(data, "zip", work_dir / "_unpacked", warnings, **caps)
    # Some tools hand us an uncompressed tar (no gzip). Detect by the ustar magic, else plain jsonl.
    if data[257:262] == b"ustar":
        return _unpack_archive(data, "tar", work_dir / "_unpacked", warnings, **caps)
    return [input_path]


# ---------------------------------------------------------------------------
# Raw -> normalized routing (reuses the canonical extractors verbatim)
# ---------------------------------------------------------------------------
def _claude_project_and_key(path: Path) -> tuple[str, str]:
    """Mirror extract_claude_rounds.main()'s project/session_key derivation from a member path.

    CLI uses project = <projects-dir-name>, key = path-relative-to-project (suffix stripped). Under an
    archive we recover the same shape: the segment after `projects/`, else the parent dir name.
    """
    parts = path.parts
    if "projects" in parts:
        idx = parts.index("projects")
        if idx + 1 < len(parts):
            project = parts[idx + 1]
            rel = Path(*parts[idx + 2:]) if idx + 2 < len(parts) else Path(path.name)
            return project, str(rel.with_suffix(""))
    return path.parent.name or "project", path.stem


# Parallelize per-file extraction only when there are enough files to amortize pool startup + IPC.
_PARALLEL_MIN_FILES = 8


def _extract_one_file(path: Path):
    """Parse ONE candidate file -> ``(rows, clipped_raw_sink, title_sink, warning, kind)``.

    The picklable per-file primitive shared by ``_extract_normalized``'s serial and parallel paths.
    Mirrors the per-file branch below, but **clips raw values here** (``_clip_raw``; keys unchanged)
    so the parallel path's IPC carries the small (~180 MB) raw map, not the ~750 MB one. Pure and
    deterministic — so reassembling per-file results in sorted order is byte-identical to the serial
    walk. ``kind`` is ``'round' | 'claude' | 'codex' | 'skip'`` and drives the caller's
    note()/extracted_raw/warnings; ``warning`` is set only on a skip/unreadable file.
    """
    rs: dict[str, Any] = {}
    ts: dict[str, str] = {}
    try:
        kind = classify_records(_iter_json_records(path.read_bytes(), limit=200))
    except Exception as exc:  # unreadable member — skip, keep going
        return [], {}, {}, f"could not read {path.name}: {type(exc).__name__}", "skip"
    if kind == "round":
        rows = [r for r in _iter_json_records(path.read_bytes()) if _looks_round(r)]
        return rows, {}, {}, None, "round"
    if kind == "claude":
        project, key = _claude_project_and_key(path)
        rows = list(extract_session_with_key(path, project, key, raw_sink=rs, title_sink=ts))
    elif kind == "codex":
        rows = list(extract_codex_session(path, raw_sink=rs, title_sink=ts))
    else:
        return [], {}, {}, f"unrecognized file (no sessions or round rows): {path.name}", "skip"
    rs = {k: _clip_raw(v) for k, v in rs.items()}  # clip in-worker; trace_key keys unchanged
    return rows, rs, ts, None, kind


def _extract_normalized(
    files, warnings, note, *, raw_sink=None, title_sink=None, jobs: int = 1
) -> tuple[list[dict], bool]:
    """Classify each file and collect normalized rows. Returns (rows, extracted_any_raw).

    ``note(stage)`` is called once per provider when raw extraction begins, so the UI can show a
    separate "Extracting Claude/Codex sessions…" hint.

    ``raw_sink`` / ``title_sink`` are forwarded to the raw extractors (opt-in, local-only). A single
    shared sink across all files keeps "first occurrence wins" consistent with ``write_rounds_jsonl``
    de-dup, since both walk the files in the same sorted order. They are never populated for already
    normalized/sanitized inputs (those don't go through the extractors).

    ``jobs`` > 1 fans the per-file parse out across processes (native sidecar only). Pyodide always
    passes ``jobs=1`` and stays single-threaded; ``multiprocessing`` is imported lazily INSIDE the
    parallel branch so it never loads under Pyodide. Output is byte-identical to the serial walk:
    results are reassembled in sorted-file order and the sinks merge first-wins in that same order.
    """
    if raw_sink is None:
        raw_sink = {}
    if title_sink is None:
        title_sink = {}
    ordered = sorted(files)

    def _merge(result) -> tuple[list[dict], str]:
        rows_i, rs, ts, warning, kind = result
        if warning:
            warnings.append(warning)
        for k, v in rs.items():
            raw_sink.setdefault(k, v)
        for k, v in ts.items():
            title_sink.setdefault(k, v)
        return rows_i, kind

    results = None
    use_parallel = jobs and jobs > 1 and len(ordered) >= _PARALLEL_MIN_FILES
    if use_parallel:
        try:
            # Lazy import — never reached under Pyodide (jobs is always 1 there). `fork` so the worker
            # inherits the parent's runtime sys.path (the sidecar inserts web/payload at import time;
            # spawn/forkserver children would re-import on the default path and fail). The worker is
            # pure compute (json/regex, no logging/threading locks), so forking from the sidecar's
            # executor thread is safe — verified not to deadlock and to stay byte-identical.
            import multiprocessing as _mp

            note("Extracting sessions")
            ctx = _mp.get_context("fork")
            with ctx.Pool(jobs) as pool:
                results = list(pool.imap(_extract_one_file, ordered, chunksize=4))
        except Exception as exc:  # any pool failure -> serial fallback (still correct)
            warnings.append(f"parallel extract unavailable ({type(exc).__name__}); ran serially")
            results = None

    rows: list[dict] = []
    extracted_raw = False
    if results is not None:  # parallel: reassemble in the sorted order imap preserved
        for result in results:
            rows_i, kind = _merge(result)
            rows.extend(rows_i)
            extracted_raw = extracted_raw or kind in ("claude", "codex")
    else:  # serial (Pyodide, small inputs, or fallback)
        announced: set[str] = set()
        for path in ordered:
            result = _extract_one_file(path)
            kind = result[4]
            if kind in ("claude", "codex") and kind not in announced:
                note(f"Extracting {kind.capitalize()} sessions")
                announced.add(kind)
            rows_i, _ = _merge(result)
            rows.extend(rows_i)
            extracted_raw = extracted_raw or kind in ("claude", "codex")
    return rows, extracted_raw


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
def _gzip_bytes(data: bytes) -> bytes:
    # mtime=0 → deterministic output, so the same content always hashes the same (cache-friendly).
    return gzip.compress(data, compresslevel=6, mtime=0)


def _write_rows(rows: list[dict], path: Path) -> None:
    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
    path.write_text(body, encoding="utf-8")


# Raw drill-down (round_raw.json) is a local-only convenience viewer. A handful of rounds carry
# enormous text — pasted files, big tool dumps, inline base64 images — that dominate round_raw's size
# (hundreds of MB) and the prepare worker's memory while being unreadable anyway. Keep the readable
# extremities of any oversized field (first + last few KB) and drop the middle with a marker. Applied
# in both the native and Pyodide paths (same code), so the two stay byte-identical.
_RAW_HEAD = 1024  # chars kept from the start of an oversized field
_RAW_TAIL = 1024  # chars kept from the end — head + tail keep the useful ends, the middle is omitted


def _clip_raw_text(s: str) -> str:
    if len(s) <= _RAW_HEAD + _RAW_TAIL:
        return s
    omitted = len(s) - _RAW_HEAD - _RAW_TAIL
    return f"{s[:_RAW_HEAD]}\n<more text omitted: {omitted} chars>\n{s[-_RAW_TAIL:]}"


def _clip_raw(value: Any) -> Any:
    """Recursively clip a raw_sink entry (str / list / dict of the above)."""
    if isinstance(value, str):
        return _clip_raw_text(value)
    if isinstance(value, list):
        return [_clip_raw(v) for v in value]
    if isinstance(value, dict):
        return {k: _clip_raw(v) for k, v in value.items()}
    return value


def prepare(
    input_path: str | os.PathLike,
    work_dir: str | os.PathLike,
    progress=None,
    *,
    trusted: bool = False,
    jobs: int = 1,
    files=None,
) -> dict[str, Any]:
    """Normalize + sanitize whatever is at ``input_path``, writing artifacts into ``work_dir``.

    ``progress`` is an optional ``callable(stage: str)`` (the browser passes one so each conversion
    stage — detecting / extracting / sanitizing — shows as a separate hint; the native CLI passes
    nothing, so it's a no-op and parity is unaffected).

    ``trusted`` raises the archive size/member guards (see TRUSTED_MAX_*). It is set ONLY for the
    local self-deploy sidecar path, which streams the user's own on-disk ~/.claude + ~/.codex (a real
    combined history exceeds the strict untrusted 1 GiB ceiling). Untrusted drops keep the strict
    default. The native CLI never passes it, so parity is unaffected.

    ``jobs`` > 1 parallelizes per-file extraction (native sidecar only; Pyodide keeps the default 1 and
    stays single-threaded). ``files`` (a list of on-disk jsonl paths) bypasses ``_sniff_and_collect``
    entirely — the sidecar already has the trace unpacked on disk, so it skips the tar round-trip and
    hands the real paths straight in. Both keep the output byte-identical to the default path.

    Always writes a valid ``sanitized.jsonl`` (+ ``.gz``) — the canonical artifact every downstream
    consumer (Analyze figures, the assistant's QA, Contribute) uses. Writes ``normalized.jsonl.gz``
    only when we extracted it from raw sessions. Returns metadata incl. ``produced`` flags that drive
    the UI download buttons:
        kind=='raw'        -> produced {normalized:True,  sanitized:True}   (show both downloads)
        kind=='normalized' -> produced {normalized:False, sanitized:True}   (show sanitized only)
        kind=='sanitized'  -> produced {normalized:False, sanitized:False}  (show none)
    """
    note = progress if callable(progress) else (lambda *_a: None)
    input_path = Path(input_path)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    note("Detecting format")
    if files is not None:
        # Sidecar fast path: the trace is already unpacked on disk — use the given paths directly and
        # skip sniffing/unpacking (and the sidecar skips packing them into a tar in the first place).
        candidate_files = [Path(f) for f in files]
    else:
        max_members = TRUSTED_MAX_MEMBERS if trusted else MAX_MEMBERS
        max_total = TRUSTED_MAX_TOTAL_UNCOMPRESSED if trusted else MAX_TOTAL_UNCOMPRESSED
        candidate_files = _sniff_and_collect(
            input_path, work_dir, warnings, max_members=max_members, max_total=max_total
        )
    # Local-only side channels (raw per-round text + conversation titles). Populated only when we
    # extract from raw sessions; NEVER merged into normalized/sanitized rows (see below).
    raw_sink: dict[str, Any] = {}
    title_sink: dict[str, str] = {}
    normalized_rows, extracted_raw = _extract_normalized(
        candidate_files, warnings, note, raw_sink=raw_sink, title_sink=title_sink, jobs=jobs
    )
    if not normalized_rows:
        raise IngestError(
            "Couldn't find any Claude/Codex sessions or round-trace rows in this upload."
        )

    if extracted_raw:
        kind = "raw"
    elif any(row_has_leak(r) for r in normalized_rows):
        kind = "normalized"
    else:
        kind = "sanitized"

    # Normalize: run the canonical writer so trace_key assignment + dedup match the CLI byte-for-byte.
    # written_sink hands back the exact rows it wrote (deduped, trace_key-stamped) so we skip re-parsing
    # the file we just wrote — a ~3.7 s reload on a real history. These JSON-safe rows round-trip to
    # identity, so it's byte-for-byte what the reload produced (verified).
    normalized_path = work_dir / "normalized.jsonl"
    written: list[dict] = []
    write_rounds_jsonl(normalized_path, normalized_rows, append_dedup=False, written_sink=written)
    normalized_rows = written

    # Sanitize (unless the input was already sanitized — then pass through unchanged, no re-pseudonymize).
    if kind == "sanitized":
        sanitized_rows = normalized_rows
    else:
        note("Sanitizing")
        ids = StableIdSanitizer(DEFAULT_SEED)
        sanitized_rows = [sanitize_row(r, ids) for r in normalized_rows]

    sanitized_path = work_dir / "sanitized.jsonl"
    _write_rows(sanitized_rows, sanitized_path)
    sanitized_gz = work_dir / "sanitized.jsonl.gz"
    sanitized_gz.write_bytes(_gzip_bytes(sanitized_path.read_bytes()))

    produced = {"normalized": kind == "raw", "sanitized": kind in ("raw", "normalized")}
    normalized_gz_path: str | None = None
    if produced["normalized"]:
        gz = work_dir / "normalized.jsonl.gz"
        gz.write_bytes(_gzip_bytes(normalized_path.read_bytes()))
        normalized_gz_path = str(gz)

    providers = sorted({str(r.get("provider")) for r in normalized_rows if r.get("provider")})
    sessions = len({r.get("session_id") for r in normalized_rows if r.get("session_id") is not None})
    tools = sum(len(r.get("tools") or []) for r in normalized_rows)

    # Local-only raw originals + titles. PRIVACY: these never enter sanitized/normalized output —
    # the raw map lands in a sidecar JSON the prepare worker keeps in its own MEMFS and reads
    # per-round on demand; titles ride back in meta. Both must be keyed the way the *sanitized*
    # rounds are (the DB/payload is built from sanitized.jsonl): sanitize recomputes trace_key from
    # the pseudonymized session_id and randomizes session_id, so we remap raw→sanitized via the
    # row-aligned (normalized, sanitized) pairing (1:1 here because kind=='raw' sanitizes in place).
    raw_available = extracted_raw and bool(raw_sink)
    round_raw_path: str | None = None
    titles_by_session: dict[str, str] = {}
    if raw_available:
        trace_key_map = {
            n.get("trace_key"): s.get("trace_key")
            for n, s in zip(normalized_rows, sanitized_rows)
        }
        raw_by_sanitized: dict[str, Any] = {}
        for raw_tk, entry in raw_sink.items():
            san_tk = trace_key_map.get(raw_tk)
            if isinstance(san_tk, str):
                # Already clipped in _extract_one_file (drops unreadably-large lines/fields); re-clipping
                # would corrupt the "<more text omitted>" markers, so just remap raw->sanitized key here.
                raw_by_sanitized[san_tk] = entry
        round_raw_file = work_dir / "round_raw.json"
        round_raw_file.write_text(
            json.dumps(raw_by_sanitized, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        round_raw_path = str(round_raw_file)
        if title_sink:
            session_id_map = {
                n.get("session_id"): s.get("session_id")
                for n, s in zip(normalized_rows, sanitized_rows)
            }
            for raw_sid, title in title_sink.items():
                san_sid = session_id_map.get(raw_sid)
                if isinstance(san_sid, str):
                    titles_by_session[san_sid] = title

    return {
        "kind": kind,
        "providers": providers,
        "sessions": sessions,
        "rounds": len(normalized_rows),
        "tools": tools,
        "produced": produced,
        "warnings": warnings,
        "rawAvailable": raw_available,
        "titles": titles_by_session,
        "files": {
            "sanitized_jsonl": str(sanitized_path),
            "sanitized_gz": str(sanitized_gz),
            "normalized_gz": normalized_gz_path,
            "round_raw_json": round_raw_path,
        },
    }


# ---------------------------------------------------------------------------
# Native parity harness: `python web/payload/ingest.py <archive|jsonl|.gz> [out_dir]`
# ---------------------------------------------------------------------------
def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python ingest.py <archive|jsonl|.gz> [out_dir]", file=sys.stderr)
        return 2
    src = Path(argv[0]).resolve()
    out = Path(argv[1]).resolve() if len(argv) > 1 else (src.parent / "_ingest_out")
    try:
        meta = prepare(src, out)
    except IngestError as exc:
        print(f"ingest error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: v for k, v in meta.items() if k != "files"}, indent=2))
    print("files:", file=sys.stderr)
    for label, path in meta["files"].items():
        print(f"  {label}: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
