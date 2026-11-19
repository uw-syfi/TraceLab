from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def test_sibling_parent_metadata_invalidates_signature(tmp_path, monkeypatch):
    from web.local_sidecar import app

    session = tmp_path / "agent-child.jsonl"
    session.write_text("{}\n")
    monkeypatch.setattr(app, "_present_roots", lambda: [("projects", tmp_path)])
    original = app._roots_signature()
    metadata = session.with_suffix(".meta.json")
    metadata.write_text('{"parentAgentId":"one"}')
    added = app._roots_signature()
    assert added != original
    stamp = metadata.stat().st_mtime_ns
    metadata.write_text('{"parentAgentId":"two"}')
    os.utime(metadata, ns=(stamp + 1, stamp + 1))
    assert app._roots_signature() != added
    metadata.unlink()
    assert app._roots_signature() == original
