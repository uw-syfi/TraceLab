#!/usr/bin/env python3
"""Storage for the contributed pool.

Source of truth = one immutable gzip object per accepted contribution (releasable, append-only,
S3-portable). A small ``index.json`` holds running totals (additive bookkeeping) + per-upload
metadata + the set of seen session fingerprints. All disk access is funnelled through the ``Store``
protocol so a future ``S3Store`` is a drop-in: ``put_upload``/``iter_pool_rows`` map to S3
get/put, the index becomes an object, nothing else changes.

Layout under ``CONTRIB_DIR``::

    uploads/<upload_sha>.jsonl.gz   filtered (new-session-only) rows for one accepted contribution
    index.json                      {totals, fingerprints, uploads[]}
    summary.json                    cached PoolPreview snapshot served at GET /api/pool

The object is keyed by the **sha256 of the originally posted bytes** so an identical re-post is an
O(1) no-op; its contents are the deduplicated rows actually added to the pool.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable

from . import stats

DEFAULT_CONTRIB_DIR = Path(__file__).resolve().parent / "data" / "contributed"

# Deterministic, pseudonymous display slug per upload (no real identity exists post-sanitization).
_SLUG_WORDS = (
    "atelier", "quiet-loom", "sandkiln", "umber-fox", "slowriver", "terra", "sage-mill",
    "ochre", "driftwood", "kiln", "marl", "willow", "ferro", "indigo", "cobble", "thistle",
)


def _slug(sha: str) -> str:
    word = _SLUG_WORDS[int(sha[:8], 16) % len(_SLUG_WORDS)]
    return f"{word}-{sha[:4]}"


@runtime_checkable
class Store(Protocol):
    def has_upload(self, sha: str) -> bool: ...
    def put_upload(self, sha: str, gz_bytes: bytes) -> None: ...
    def remove_upload(self, sha: str) -> None: ...
    def seen_fingerprints(self) -> set[str]: ...
    def record(self, entry: dict[str, Any], new_fingerprints: set[str]) -> None: ...
    def iter_pool_rows(self) -> Iterator[dict[str, Any]]: ...
    def read_index(self) -> dict[str, Any]: ...
    def write_summary(self, obj: dict[str, Any]) -> None: ...
    def read_summary(self) -> dict[str, Any]: ...


def _empty_index() -> dict[str, Any]:
    return {"totals": stats.empty_totals(), "fingerprints": [], "uploads": []}


class LocalStore:
    """Filesystem-backed ``Store``. Not internally locked — the app serializes writes."""

    def __init__(self, contrib_dir: Path | str | None = None):
        env = os.environ.get("CONTRIB_DIR")
        self.root = Path(contrib_dir or env or DEFAULT_CONTRIB_DIR)
        self.uploads_dir = self.root / "uploads"
        self.index_path = self.root / "index.json"
        self.summary_path = self.root / "summary.json"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)

    # ---- uploads -------------------------------------------------------------------------
    def _upload_path(self, sha: str) -> Path:
        return self.uploads_dir / f"{sha}.jsonl.gz"

    def has_upload(self, sha: str) -> bool:
        return self._upload_path(sha).exists()

    def put_upload(self, sha: str, gz_bytes: bytes) -> None:
        tmp = self._upload_path(sha).with_suffix(".gz.tmp")
        tmp.write_bytes(gz_bytes)
        tmp.replace(self._upload_path(sha))

    def remove_upload(self, sha: str) -> None:
        path = self._upload_path(sha)
        if path.exists():
            path.unlink()
        index = self.read_index()
        index["uploads"] = [u for u in index["uploads"] if u.get("sha") != sha]
        index["totals"] = stats.empty_totals()
        for upload in index["uploads"]:
            index["totals"] = stats.add_totals(index["totals"], upload["subtotals"])
        index["fingerprints"] = sorted(
            {fp for upload in index["uploads"] for fp in upload.get("fingerprints", [])}
        )
        self._write_index(index)

    def iter_pool_rows(self) -> Iterator[dict[str, Any]]:
        for path in sorted(self.uploads_dir.glob("*.jsonl.gz")):
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

    # ---- index ---------------------------------------------------------------------------
    def read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return _empty_index()
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write_index(self, index: dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index), encoding="utf-8")
        tmp.replace(self.index_path)

    def seen_fingerprints(self) -> set[str]:
        return set(self.read_index().get("fingerprints", []))

    def record(self, entry: dict[str, Any], new_fingerprints: set[str]) -> None:
        index = self.read_index()
        index["totals"] = stats.add_totals(index["totals"], entry["subtotals"])
        index["fingerprints"] = sorted(set(index.get("fingerprints", [])) | new_fingerprints)
        index["uploads"].append(entry)
        self._write_index(index)

    # ---- summary snapshot ----------------------------------------------------------------
    def write_summary(self, obj: dict[str, Any]) -> None:
        tmp = self.summary_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        tmp.replace(self.summary_path)

    def read_summary(self) -> dict[str, Any]:
        if not self.summary_path.exists():
            return pool_preview(_empty_index())
        return json.loads(self.summary_path.read_text(encoding="utf-8"))


def make_contribution_entry(sha: str, received_at: str, sub: dict[str, Any],
                            fingerprints: set[str]) -> dict[str, Any]:
    """Build the per-upload ``index.json`` entry recorded on accept."""
    providers = [p for p in ("claude", "codex") if sub["provider_counts"].get(p)]
    return {
        "sha": sha,
        "slug": _slug(sha),
        "received_at": received_at,
        "subtotals": sub,
        "providers": providers,
        "fingerprints": sorted(fingerprints),
    }


def pool_preview(index: dict[str, Any], *, recent: int = 12) -> dict[str, Any]:
    """Project the index into the PoolPreview shape the dashboard consumes.

    Timestamps stay ISO; the client formats relative ("2h ago") at fetch time so the snapshot
    never goes stale between contributions.
    """
    totals = index.get("totals", stats.empty_totals())
    uploads = index.get("uploads", [])
    counts = totals["provider_counts"]
    contributions = [
        {
            "id": u.get("slug", u["sha"][:8]),
            "receivedAt": u["received_at"],
            "rows": u["subtotals"]["rows"],
            "providers": u.get("providers", []),
            "status": "validated",  # synchronous gate: an accepted upload is always validated
        }
        for u in reversed(uploads[-recent:])
    ]
    return {
        "contributors": len(uploads),
        "rounds": totals["rows"],
        "totalInputTokens": totals["input_tokens"],
        "lastContributionAt": uploads[-1]["received_at"] if uploads else None,
        "split": {"claude": counts.get("claude", 0), "codex": counts.get("codex", 0)},
        "contributions": contributions,
    }
