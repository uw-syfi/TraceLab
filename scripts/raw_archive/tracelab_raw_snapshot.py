#!/usr/bin/env python3
"""Discover Claude Code / Codex session stores anywhere on this host and archive them.

Detection is by content, not by directory name: agent systems often point
CODEX_HOME / CLAUDE_CONFIG_DIR at directories without a leading dot
(e.g. ``.../agent-workspaces/w_x/codex/<id>/sessions``).

  Codex store  = D with D/sessions/YYYY/MM/DD/rollout-*.jsonl or
                 D/archived_sessions/rollout-*.jsonl, first line a session_meta.
  Claude store = D with D/projects/<project>/<uuid>.jsonl carrying a sessionId.

Each run archives only files that are new or changed since the previous
successful run (raw rollouts are append-only), so files users delete later stay
in older archives. Output per run, one archive per store owner so a user's data
can be removed on its own:

  <output>/<host>/<YYYYmmddTHHMMSSZ>-<full|incr>/<user>.tar.zst
      files under their absolute path without the leading "/"
  <output>/<host>/<YYYYmmddTHHMMSSZ>-<full|incr>/manifest.json
      stores found (kind, root, owner), per-archive counts and sha256, errors
  <output>/<host>/state.json   size/mtime of every file at the last run

Images are not kept: inline base64 images become
``<tracelab-image sha256=… media=… bytes=… b64len=… w=… h=…>`` placeholders and
loose image files in the stores are skipped (counted in the manifest). Every
other byte of the transcripts is archived unchanged.

Stdlib only; Python >= 3.9. Run as root (see install.sh) to read every user.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import tarfile
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

LOCAL_FS = {"xfs", "ext4", "ext3", "zfs", "btrfs"}
DEFAULT_PRUNE_PATHS = ["/proc", "/sys", "/dev", "/run", "/boot", "/usr", "/snap",
                       "/var/lib/docker", "/var/lib/containers", "/var/cache"]
DEFAULT_PRUNE_NAMES = ["node_modules", ".git", "__pycache__", "site-packages",
                       ".npm", ".cache"]

CODEX_RE = (r".*/(sessions/[0-9]{4}/[0-9]{2}/[0-9]{2}|archived_sessions)"
            r"/rollout-[^/]*\.jsonl")
CLAUDE_RE = r".*/projects/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl"
CODEX_SPLIT = re.compile(r"/(sessions|archived_sessions)/")
CLAUDE_SPLIT = re.compile(r"/projects/[^/]+/[^/]+\.jsonl$")
# Subtrees archived for each kind of store.
STORE_SUBDIRS = {"codex": ("sessions", "archived_sessions"), "claude": ("projects",)}


LOG_FILE = None  # --log-file: appended to in addition to stderr


def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)
    if LOG_FILE is not None:
        LOG_FILE.write(line + "\n")
        LOG_FILE.flush()


def local_mountpoints() -> list[str]:
    mounts = []
    with open("/proc/self/mounts") as fh:
        for line in fh:
            _dev, mnt, fstype, *_ = line.split()
            mnt = mnt.replace("\\040", " ")
            if fstype in LOCAL_FS:
                mounts.append(mnt)
    return sorted(set(mounts))


# Parallel walk. Each task walks a few directories depth-first for at most WALK_SLICE_S
# seconds, then hands its unvisited directories back to be split across idle workers, so a
# single huge subtree (millions of files) is shared by all workers instead of pinning one.
WALK_SLICE_S = 1.0
_W: dict = {}


def _walk_init(prune_paths: list[str], prune_names: list[str], mounts: list[str]) -> None:
    _W["prune_paths"] = set(prune_paths)
    _W["prune_names"] = set(prune_names)
    _W["mounts"] = set(mounts)
    _W["match"] = re.compile(f"(?:{CODEX_RE})|(?:{CLAUDE_RE})")


def _walk_slice(dirs: list[str]) -> tuple[list[str], list[str], int]:
    """Walk `dirs` (on one filesystem, like find -xdev) until the time slice ends.
    Returns (matching files, directories left unvisited, directories visited)."""
    prune_paths, prune_names, mounts, match = (
        _W["prune_paths"], _W["prune_names"], _W["mounts"], _W["match"])
    deadline = time.monotonic() + WALK_SLICE_S
    stack, hits, visited = list(dirs), [], 0
    while stack and time.monotonic() < deadline:
        d = stack.pop()
        visited += 1
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        p = e.path
                        # Another mount is another filesystem; local ones are roots of their own.
                        if e.name not in prune_names and p not in prune_paths and p not in mounts:
                            stack.append(p)
                    elif (e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False)
                          and match.fullmatch(e.path)):
                        hits.append(e.path)
                except OSError:
                    continue
    return hits, stack, visited


def walk_candidates(root: str, prune_paths: list[str], prune_names: list[str],
                    workers: int) -> tuple[list[str], dict]:
    """Paths under root (same filesystem) that look like session transcripts."""
    with open("/proc/self/mounts") as fh:
        mounts = [line.split()[1].replace("\\040", " ") for line in fh]
    hits: list[str] = []
    tasks = visited = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_walk_init,
                             initargs=(prune_paths, prune_names, mounts)) as pool:
        running = {pool.submit(_walk_slice, [root])}
        while running:
            done, running = wait(running, return_when=FIRST_COMPLETED)
            for fut in done:
                found, left, n = fut.result()
                hits += found
                visited += n
                tasks += 1
                # Split what is left so idle workers can take part of it.
                parts = max(1, min(len(left), workers - len(running)))
                running |= {pool.submit(_walk_slice, left[i::parts]) for i in range(parts) if left[i::parts]}
    return hits, {"dirs": visited, "tasks": tasks}


def classify(path: str) -> tuple[str, str] | None:
    m = CODEX_SPLIT.search(path)
    if m and path.rsplit("/", 1)[-1].startswith("rollout-"):
        return "codex", path[: m.start()]
    m = CLAUDE_SPLIT.search(path)
    if m:
        return "claude", path[: m.start()]
    return None


def looks_valid(kind: str, path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return False
    if kind == "codex":
        first = head.split(b"\n", 1)[0]
        # Codex CLI before session_meta (2025) began with {"id":…,"timestamp":…,"instructions":…}.
        return b'"session_meta"' in first or (b'"id"' in first and b'"instructions"' in first)
    return b'"sessionId"' in head


def discover(roots: list[str], prune_paths: list[str], prune_names: list[str],
             workers: int) -> tuple[dict, dict]:
    """Return ({(kind, store_root): sample_paths}, per-root timings)."""
    stores: dict[tuple[str, str], list[str]] = {}
    timings = {}
    for root in roots:
        t0 = time.monotonic()
        hits, stats = walk_candidates(root, prune_paths, prune_names, workers)
        timings[root] = {"seconds": round(time.monotonic() - t0, 1), "candidate_files": len(hits), **stats}
        log(f"scan {root}: {len(hits)} candidate files in {timings[root]['seconds']}s "
            f"({stats['dirs']} dirs, {stats['tasks']} tasks, {workers} workers)")
        for p in hits:
            c = classify(p)
            if c:
                samples = stores.setdefault(c, [])
                if len(samples) < 5:
                    samples.append(p)
    return stores, timings


def owner_name(path: str) -> str:
    try:
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except (KeyError, OSError):
        return "?"


def list_files(store_root: str, kind: str, errors: list[str]) -> list[tuple[str, os.stat_result]]:
    out = []
    for sub in STORE_SUBDIRS[kind]:
        top = os.path.join(store_root, sub)
        if not os.path.isdir(top) or os.path.islink(top):
            continue
        for d, dirs, files in os.walk(top, onerror=lambda e: errors.append(str(e))):
            dirs.sort()
            for name in sorted(files):
                p = os.path.join(d, name)
                try:
                    st = os.lstat(p)
                except OSError as exc:
                    errors.append(str(exc))
                    continue
                if stat.S_ISREG(st.st_mode):
                    out.append((p, st))
    return out


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def chown_to(path: Path, owner: str | None) -> None:
    if owner and os.geteuid() == 0:
        pw = pwd.getpwnam(owner)
        os.chown(path, pw.pw_uid, pw.pw_gid)


# Inline images are dropped: they dominate compressed size (base64 PNG/JPEG barely
# compresses) and are the most sensitive content. Each payload becomes a placeholder
# that keeps what analysis may need:
#   <tracelab-image sha256=<hex> media=image/png bytes=<decoded> b64len=<chars> w=<px> h=<px>>
_B64 = rb"(?:iVBOR|/9j/|R0lGOD|UklGR)[A-Za-z0-9+/]{96,}={0,2}"
IMAGE_FIELD_RE = re.compile(rb'("(?:data|base64)":\s*")(' + _B64 + rb')(")')
IMAGE_URL_RE = re.compile(rb"data:image/[a-zA-Z0-9.+-]+;base64,(" + _B64 + rb")")
LOOSE_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _image_dims(raw: bytes) -> tuple[str, int | None, int | None]:
    """(media type, width, height) from the image header; dims None if unknown."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
        return "image/png", int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        return "image/gif", int.from_bytes(raw[6:8], "little"), int.from_bytes(raw[8:10], "little")
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", None, None
    if raw[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(raw):
            if raw[i] != 0xFF:
                break
            marker, seg = raw[i + 1], int.from_bytes(raw[i + 2:i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return ("image/jpeg", int.from_bytes(raw[i + 7:i + 9], "big"),
                        int.from_bytes(raw[i + 5:i + 7], "big"))
            i += 2 + seg
        return "image/jpeg", None, None
    return "image/unknown", None, None


def _placeholder(b64: bytes) -> bytes:
    try:
        raw = base64.b64decode(b64, validate=False)
    except ValueError:
        raw = b""
    media, w, h = _image_dims(raw)
    dims = f" w={w} h={h}" if w is not None else ""
    return (f"<tracelab-image sha256={hashlib.sha256(raw).hexdigest()} media={media} "
            f"bytes={len(raw)} b64len={len(b64)}{dims}>").encode()


def strip_images(data: bytes) -> tuple[bytes, int, int]:
    """Replace inline base64 images. Returns (data, images replaced, bytes removed)."""
    if b"base64" not in data:
        return data, 0, 0
    count = 0

    def field(m: re.Match) -> bytes:
        nonlocal count
        count += 1
        return m.group(1) + _placeholder(m.group(2)) + m.group(3)

    def url(m: re.Match) -> bytes:
        nonlocal count
        count += 1
        return _placeholder(m.group(1))

    out = IMAGE_URL_RE.sub(url, IMAGE_FIELD_RE.sub(field, data))
    return out, count, len(data) - len(out)


def safe_name(user: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in user) or "_"


def write_archive(path: Path, files: list[tuple[str, os.stat_result]], level: int,
                  errors: list[str], shrunk: list[str]) -> dict | None:
    """Write files into a tar.zst at their absolute path (no leading /), inline images
    replaced by placeholders and loose image files left out. None if zstd fails."""
    archived, skipped = 0, []
    images = {"inline_replaced": 0, "inline_bytes_removed": 0, "loose_files_skipped": 0,
              "loose_bytes_skipped": 0}
    with path.open("xb") as dest:
        zstd = subprocess.Popen(["zstd", "-q", f"-{level}", "-T0", "--long=27", "-c"],
                                stdin=subprocess.PIPE, stdout=dest)
        try:
            with tarfile.open(fileobj=zstd.stdin, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for p, st in files:
                    if os.path.splitext(p)[1].lower() in LOOSE_IMAGE_EXT:
                        images["loose_files_skipped"] += 1
                        images["loose_bytes_skipped"] += st.st_size
                        continue
                    try:
                        with open(p, "rb") as fh:
                            info = tar.gettarinfo(arcname=p.lstrip("/"), fileobj=fh)
                            # Sessions still being written grow while we read; archive the
                            # prefix that existed at listing time.
                            data = fh.read(st.st_size)
                        if len(data) < st.st_size:
                            # Truncated/rewritten since listing; retry next run.
                            shrunk.append(p)
                            skipped.append(p)
                            continue
                        data, n_img, removed = strip_images(data)
                        images["inline_replaced"] += n_img
                        images["inline_bytes_removed"] += removed
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
                        archived += info.size
                    except OSError as exc:
                        errors.append(f"{p}: {exc}")
                        skipped.append(p)
            zstd.stdin.close()
        finally:
            zstd.wait()
    if zstd.returncode != 0:
        errors.append(f"zstd exited with {zstd.returncode} for {path.name}")
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return {"files": len(files) - len(skipped) - images["loose_files_skipped"], "bytes": archived,
            "compressed_bytes": path.stat().st_size, "sha256": digest.hexdigest(),
            "images": images, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="JSON config (keys: output, roots, owner, "
                    "prune_paths, prune_names, zstd_level, scan_workers)")
    ap.add_argument("--output", type=Path, help="Archive directory (overrides config)")
    ap.add_argument("--root", action="append", help="Scan root; repeatable. Default: all local mounts")
    ap.add_argument("--full", action="store_true", help="Archive every file, ignoring previous state")
    ap.add_argument("--discover-only", action="store_true", help="Print stores found and exit")
    ap.add_argument("--workers", type=int, help="Parallel scan processes (config scan_workers; "
                    "default min(16, CPUs))")
    ap.add_argument("--request-file", type=Path,
                    help="Manual-trigger file (see tracelab-raw-snapshot.path): consumed on start; "
                         "content 'full' forces --full")
    ap.add_argument("--log-file", type=Path,
                    help="Also append log lines here (the service uses this instead of systemd's "
                         "StandardOutput=append:, which SELinux blocks on unlabeled filesystems)")
    args = ap.parse_args()
    if args.log_file is not None:
        global LOG_FILE
        LOG_FILE = args.log_file.open("a")
        log(f"--- start (pid {os.getpid()})")

    if args.request_file is not None and args.request_file.exists():
        try:
            requested = args.request_file.read_text(errors="replace")[:16].strip().lower()
            args.request_file.unlink()
        except OSError as exc:
            requested = ""
            log(f"could not consume {args.request_file}: {exc}")
        log(f"manual trigger (request={requested or 'incr'})")
        args.full = args.full or requested == "full"

    cfg = json.loads(args.config.read_text()) if args.config else {}
    output = args.output or (Path(cfg["output"]) if cfg.get("output") else None)
    if output is None and not args.discover_only:
        ap.error("--output or config 'output' is required")
    roots = args.root or cfg.get("roots") or local_mountpoints()
    prune_paths = cfg.get("prune_paths", DEFAULT_PRUNE_PATHS)
    prune_names = cfg.get("prune_names", DEFAULT_PRUNE_NAMES)
    owner = cfg.get("owner")
    level = int(cfg.get("zstd_level", 10))
    workers = args.workers or int(cfg.get("scan_workers", min(16, os.cpu_count() or 1)))
    host = socket.gethostname().split(".")[0]

    host_dir = None
    if output is not None:
        host_dir = output.resolve() / host
        prune_paths = prune_paths + [str(output.resolve())]
    # Mounts inside pruned paths (e.g. /boot, or the archive dir, which systemd's
    # ReadWritePaths= turns into its own mount) are not scan roots.
    roots = [r for r in roots
             if not any(r == p or r.startswith(p.rstrip("/") + "/") for p in prune_paths)]

    started = datetime.now(timezone.utc)
    log(f"host={host} roots={roots}")
    found, timings = discover(roots, prune_paths, prune_names, workers)

    stores, rejected = [], []
    for (kind, root), samples in sorted(found.items()):
        (stores if any(looks_valid(kind, s) for s in samples) else rejected).append((kind, root))
    log(f"{len(stores)} stores ({sum(k == 'codex' for k, _ in stores)} codex, "
        f"{sum(k == 'claude' for k, _ in stores)} claude), {len(rejected)} rejected")

    if args.discover_only:
        for kind, root in stores:
            print(f"{kind}\t{owner_name(root)}\t{root}")
        for kind, root in rejected:
            print(f"REJECTED-{kind}\t{owner_name(root)}\t{root}")
        return 0

    os.umask(0o027)
    host_dir.mkdir(parents=True, exist_ok=True)
    chown_to(host_dir, owner)
    state_path = host_dir / "state.json"
    prev = {} if args.full or not state_path.exists() else json.loads(state_path.read_text())
    prev_files = prev.get("files", {})

    errors: list[str] = []
    store_reports, new_state = [], {}
    by_user: dict[str, list[tuple[str, os.stat_result]]] = {}
    for kind, root in stores:
        user = owner_name(root)
        files = list_files(root, kind, errors)
        changed = 0
        for p, st in files:
            sig = [st.st_size, st.st_mtime_ns]
            new_state[p] = sig
            if prev_files.get(p) != sig:
                by_user.setdefault(user, []).append((p, st))
                changed += 1
        store_reports.append({"kind": kind, "root": root, "owner": user,
                              "files": len(files), "bytes": sum(st.st_size for _, st in files),
                              "changed_files": changed})
    # Files that vanished since last run stay in older archives.
    gone = sorted(set(prev_files) - set(new_state))

    mode = "full" if not prev_files else "incr"
    run_name = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{mode}"
    partial_dir = host_dir / (run_name + ".partial")
    partial_dir.mkdir()
    log(f"archiving {sum(map(len, by_user.values()))} files for {len(by_user)} users -> {run_name}/")

    archives, shrunk = {}, []
    for user, files in sorted(by_user.items()):
        path = partial_dir / f"{safe_name(user)}.tar.zst"
        report = write_archive(path, files, level, errors, shrunk)
        if report is None:
            log(f"zstd failed for {user}; keeping {partial_dir.name} and not advancing state")
            return 1
        for p in report.pop("skipped"):
            new_state.pop(p, None)  # retry next run
        archives[user] = {"archive": path.name, **report}
        log(f"  {user}: {report['files']} files, {report['compressed_bytes'] / 2**20:,.1f} MiB")

    manifest = {
        "host": socket.gethostname(), "mode": mode, "run": run_name,
        "started_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "previous_run": prev.get("last_run"), "roots": roots, "scan": timings,
        "stores": store_reports, "rejected_store_roots": [{"kind": k, "root": r} for k, r in rejected],
        "archives": archives, "files_gone_since_previous": len(gone),
        "files_skipped_shrunk": shrunk, "errors": errors,
    }
    write_json_atomic(partial_dir / "manifest.json", manifest)
    for p in [partial_dir, *partial_dir.iterdir()]:
        chown_to(p, owner)
    run_dir = host_dir / run_name
    os.replace(partial_dir, run_dir)
    write_json_atomic(state_path, {"last_run": run_name, "files": new_state})
    chown_to(state_path, owner)
    total = sum(a["compressed_bytes"] for a in archives.values())
    log(f"done: {run_name} {total / 2**20:,.1f} MiB in {len(archives)} archives, {len(errors)} errors")
    return 0 if not errors else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("crashed:\n" + traceback.format_exc())
        raise
