#!/usr/bin/env python3
"""Unit tests for the contribute sidecar: privacy gate, seed-invariant dedup, bookkeeping.

Run via `uv run python -m pytest web/server/tests` or standalone `uv run python <this file>`.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Point the module-global store at a throwaway dir before importing the app.
os.environ["CONTRIB_DIR"] = tempfile.mkdtemp(prefix="contrib-test-")

import sanitize_round_trace as san  # noqa: E402
import trace_privacy as tp  # noqa: E402
from web.server import stats  # noqa: E402
from web.server import app as appmod  # noqa: E402
from web.server.fingerprint import session_fingerprints  # noqa: E402


# ---- fixtures -----------------------------------------------------------------------------
def make_raw_session(sid: str, n: int, base: int) -> list[dict]:
    """A synthetic *unsanitized* session (still carries home/session_file/tools.input)."""
    rows = []
    for i in range(n):
        rows.append({
            "provider": "claude",
            "session_id": sid,
            "round_index": i,
            "round_id": f"msg_{i:08x}",
            "home": "/home/kanzhu",
            "session_file": "/home/kanzhu/x.jsonl",
            "user": "kanzhu",
            "project": "/repo",
            "input_tokens_total": base + i * 1000,
            "prefix_tokens": i * 500,
            "newly_append_tokens": base + i * 500,
            "output_tokens": 100 + i,
            "reasoning_output_tokens": None,
            "claude_uncached_input_tokens": 10,
            "claude_cache_creation_input_tokens": base,
            "claude_cache_read_input_tokens": i * 200,
            "tools": [{"tool_name": "Bash", "tool_call_id": f"toolu_{i:08x}",
                       "input": {"command": "ls"}, "input_chars": 2}],
            "trace_key": f"claude:{sid}:msg_{i:08x}",
        })
    return rows


def sanitize(rows: list[dict], seed: str) -> list[dict]:
    ids = san.StableIdSanitizer(seed)
    return [san.sanitize_row(r, ids) for r in rows]


def gz(rows: list[dict]) -> bytes:
    return gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8"))


# ---- tests --------------------------------------------------------------------------------
def test_fingerprint_is_seed_invariant():
    raw = make_raw_session("claude:real-1", 4, 30000)
    a, b = sanitize(raw, "seed-A"), sanitize(raw, "seed-B")
    assert a[0]["session_id"] != b[0]["session_id"]          # ids differ across seeds
    assert set(session_fingerprints(a)) == set(session_fingerprints(b))  # ...but fp matches


def test_fingerprint_distinguishes_content():
    a = sanitize(make_raw_session("claude:real-1", 4, 30000), "s")
    c = sanitize(make_raw_session("claude:real-2", 4, 99999), "s")
    assert set(session_fingerprints(a)).isdisjoint(session_fingerprints(c))


def test_stats_add_and_subtract_roundtrip():
    rows = sanitize(make_raw_session("claude:real-1", 4, 30000), "s")
    sub = stats.subtotals(rows)
    assert sub["rows"] == 4 and sub["provider_counts"] == {"claude": 4}
    t = stats.add_totals(stats.empty_totals(), sub)
    t = stats.add_totals(t, sub, sign=-1)
    assert t == stats.empty_totals()  # subtracting drops zeroed providers


def test_gate_rejects_sensitive_and_tool_input():
    clean = sanitize(make_raw_session("claude:real-1", 1, 100), "s")[0]
    assert tp.find_sensitive(clean) == []
    for leak in ({**clean, "cwd": "/secret"},
                 {**clean, "tools": [{**clean["tools"][0], "input": {"x": 1}}]}):
        try:
            appmod._parse_and_validate(gz([leak]))
            raise AssertionError("expected rejection")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "status_code", None) == 422


def test_full_dedup_and_bookkeeping_flow():
    raw1 = make_raw_session("claude:real-1", 4, 30000)
    raw2 = make_raw_session("claude:real-2", 4, 88000)
    a = sanitize(raw1, "seed-A")
    bytes_a = gz(a)

    # first accept
    r1 = appmod._process_accept(bytes_a, appmod._parse_and_validate(bytes_a))
    assert (r1["accepted"], r1["new_sessions"]) == (4, 1)

    # identical re-post -> content-hash no-op
    assert appmod._process_accept(bytes_a, appmod._parse_and_validate(bytes_a))["duplicate"]

    # same session, re-sanitized with a different seed -> different bytes/ids, fp matches -> skipped
    bytes_b = gz(sanitize(raw1, "seed-B"))
    r2 = appmod._process_accept(bytes_b, appmod._parse_and_validate(bytes_b))
    assert (r2["accepted"], r2["skipped_sessions"]) == (0, 1)

    # genuinely new session -> accepted
    bytes_c = gz(sanitize(raw2, "seed-A"))
    r3 = appmod._process_accept(bytes_c, appmod._parse_and_validate(bytes_c))
    assert r3["accepted"] == 4

    summary = appmod._store.read_summary()
    assert summary["rounds"] == 8 and summary["contributors"] == 2
    assert summary["split"]["claude"] == 8

    # bookkeeping == cold rescan
    assert stats.rebuild_stats(appmod._store)["rows"] == 8

    # remove the first upload -> totals subtract, its fingerprint frees up for re-accept
    appmod._store.remove_upload(hashlib.sha256(bytes_a).hexdigest())
    idx = appmod._store.read_index()
    assert idx["totals"]["rows"] == 4 and len(idx["uploads"]) == 1
    assert appmod._process_accept(bytes_a, appmod._parse_and_validate(bytes_a))["accepted"] == 4


def test_chunk_assembly_reorders_and_validates():
    """The finish step concatenates chunks by offset, so shuffled arrival assembles byte-identically
    and gaps/short uploads are rejected — the integrity guarantee the chunked client relies on."""
    payload = gz(sanitize(make_raw_session("claude:real-1", 6, 30000), "s"))
    size = max(1, len(payload) // 5)  # guarantee several chunks regardless of gz size
    chunks = {off: payload[off:off + size] for off in range(0, len(payload), size)}
    assert len(chunks) >= 3  # the gap/reorder assertions below need multiple chunks

    # arrival order doesn't matter — assembly is by offset
    shuffled = dict(sorted(chunks.items(), key=lambda kv: (kv[0] * 7) % 13))
    up = {"chunks": shuffled, "total": len(payload)}
    assert appmod._assemble_upload(up) == payload

    # a missing interior chunk -> rejected (gap)
    gapped = {o: b for o, b in chunks.items() if o != size}
    try:
        appmod._assemble_upload({"chunks": gapped, "total": len(payload)})
        raise AssertionError("expected a gap to be rejected")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 400

    # all chunks present but total understated -> rejected (incomplete/overrun)
    try:
        appmod._assemble_upload({"chunks": chunks, "total": len(payload) - 1})
        raise AssertionError("expected a length mismatch to be rejected")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 400

    # reassembled bytes flow through the normal accept path unchanged
    assert appmod._process_accept(payload, appmod._parse_and_validate(payload))["accepted"] == 6


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
