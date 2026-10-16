"""Images count as 0 characters, so original raw and image-stripped archived raw
(scripts/raw_archive) extract to the same rounds."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
import zlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from extract_claude_rounds import content_chars, extract_session_with_key  # noqa: E402
from extract_codex_rounds import extract_codex_session  # noqa: E402


# Stand-in for the raw archive's image stripping, kept here so this test does not depend on
# it: base64 images in "data"/"base64" fields and data:image URLs become
#   <tracelab-image sha256=<hex> media=<type> bytes=<n> b64len=<n>>
_B64 = rb"(?:iVBOR|/9j/|R0lGOD|UklGR)[A-Za-z0-9+/]{96,}={0,2}"
_IMAGE_FIELD_RE = re.compile(rb'("(?:data|base64)":\s*")(' + _B64 + rb')(")')
_IMAGE_URL_RE = re.compile(rb"data:image/[a-zA-Z0-9.+-]+;base64,(" + _B64 + rb")")


def _placeholder(b64: bytes) -> bytes:
    raw = base64.b64decode(b64)
    return (f"<tracelab-image sha256={hashlib.sha256(raw).hexdigest()} media=image/png "
            f"bytes={len(raw)} b64len={len(b64)}>").encode()


def strip_images(data: bytes) -> tuple[bytes, int, int]:
    """Returns (data, images replaced, bytes removed)."""
    data_f, n_field = _IMAGE_FIELD_RE.subn(lambda m: m.group(1) + _placeholder(m.group(2)) + m.group(3), data)
    out, n_url = _IMAGE_URL_RE.subn(lambda m: _placeholder(m.group(1)), data_f)
    return out, n_field + n_url, len(data) - len(out)


def _png(w: int = 4, h: int = 3) -> str:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")
    ihdr = w.to_bytes(4, "big") + h.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", bytes(200)) + chunk(b"IEND", b"")
    return base64.b64encode(raw).decode()


PNG = _png()
PLACEHOLDER = "<tracelab-image sha256=ab media=image/png bytes=10 b64len=20 w=4 h=3>"


# --------------------------------------------------------------------------- content_chars


def test_plain_values_unchanged() -> None:
    assert content_chars(None) == 0
    assert content_chars("hello") == 5
    assert content_chars("mentions base64 and /9j/ in prose") == len("mentions base64 and /9j/ in prose")
    value = [{"type": "text", "text": "hi"}]
    assert content_chars(value) == len(json.dumps(value, separators=(",", ":")))


def test_image_blocks_count_zero() -> None:
    text = [{"type": "text", "text": "hi"}]
    expected = content_chars(text)
    claude = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    codex = {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}
    assert content_chars(claude) == 0
    assert content_chars([*text, claude]) == expected
    assert content_chars([*text, codex]) == expected
    assert content_chars([codex, *text, claude]) == expected


def test_images_inside_text_count_zero() -> None:
    assert content_chars(PNG) == 0
    assert content_chars(PLACEHOLDER) == 0
    assert content_chars(f"see data:image/png;base64,{PNG} here") == len("see  here")
    assert content_chars(f"see {PLACEHOLDER} here") == len("see  here")
    assert content_chars(f"see data:image/png;base64,{PLACEHOLDER} here") == len("see  here")


def test_original_and_stripped_values_match() -> None:
    values = [
        f"x data:image/jpeg;base64,{PNG} y",
        [{"type": "text", "text": "a"},
         {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}],
        [{"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}],
        {"result": {"base64": PNG, "note": "n"}},
    ]
    for value in values:
        raw = json.dumps(value).encode()
        stripped, n, _ = strip_images(raw)
        assert n == 1
        assert content_chars(json.loads(stripped)) == content_chars(value)


# --------------------------------------------------------------------------- extractors


def _strip_file(src: Path, dst: Path) -> None:
    data, n, _ = strip_images(src.read_bytes())
    assert n > 0
    dst.write_bytes(data)


def _same_rounds(a: list[dict], b: list[dict]) -> None:
    assert len(a) == len(b) > 0
    for ra, rb in zip(a, b):
        ra, rb = dict(ra), dict(rb)
        for r in (ra, rb):  # path and tool input values legitimately differ (placeholder vs base64)
            r.pop("session_file", None)
            for t in r.get("tools") or []:
                t.pop("input", None)
        assert ra == rb


CLAUDE_SESSION = "33333333-3333-4333-8333-333333333333"


def test_claude_extraction_matches_after_strip(tmp_path: Path) -> None:
    def rec(ts: str, kind: str, message: dict, uid: str) -> dict:
        return {"type": kind, "uuid": uid, "timestamp": f"2026-01-01T00:00:{ts}Z",
                "sessionId": CLAUDE_SESSION, "message": message}
    usage = {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 5}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    records = [
        rec("01.000", "user", {"role": "user", "content": [{"type": "text", "text": "look"}, image]}, "u1"),
        rec("02.000", "assistant", {"id": "m1", "role": "assistant", "model": "claude-opus-4-8", "usage": usage,
                                    "content": [{"type": "tool_use", "id": "t1", "name": "Read",
                                                 "input": {"file_path": "/a.png"}}]}, "a1"),
        rec("03.000", "user", {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [image]}]}, "u2"),
        rec("04.000", "assistant", {"id": "m2", "role": "assistant", "model": "claude-opus-4-8", "usage": usage,
                                    "content": [{"type": "text", "text": "a picture"}]}, "a2"),
    ]
    original = tmp_path / f"{CLAUDE_SESSION}.jsonl"
    original.write_text("".join(json.dumps(r) + "\n" for r in records))
    archived = tmp_path / "archived" / original.name
    archived.parent.mkdir()
    _strip_file(original, archived)
    a = extract_session_with_key(original, "p", CLAUDE_SESSION)
    b = extract_session_with_key(archived, "p", CLAUDE_SESSION)
    _same_rounds(a, b)
    user = [e for e in a[0]["timing_events"] if e["event_type"] == "user_message"]
    assert user[0]["content_chars"] == content_chars([{"type": "text", "text": "look"}])
    assert a[0]["tools"][0]["result_chars"] == 2  # "[]"


CODEX_SESSION = "019a0000-0000-7000-8000-000000000002"


def test_codex_extraction_matches_after_strip(tmp_path: Path) -> None:
    def rec(ts: str, top: str, payload: dict) -> dict:
        return {"timestamp": f"2026-09-01T10:00:{ts}Z", "type": top, "payload": payload}
    last = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50, "reasoning_output_tokens": 0}
    usage = {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": last}}
    records = [
        rec("00.000", "session_meta", {"id": CODEX_SESSION, "cli_version": "0.150.0", "source": "cli", "cwd": "/r"}),
        rec("00.100", "turn_context", {"turn_id": "t", "model": "gpt-5.6-sol"}),
        rec("01.000", "event_msg", {"type": "user_message", "message": "look", "images": [f"data:image/png;base64,{PNG}"]}),
        rec("02.000", "response_item", {"type": "function_call", "call_id": "c1", "name": "view_image",
                                         "arguments": "{\"path\":\"/a.png\"}"}),
        rec("02.100", "event_msg", usage),
        rec("03.000", "response_item", {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_image", "image_url": f"data:image/png;base64,{PNG}"}]}),
        rec("04.000", "response_item", {"type": "message", "role": "assistant",
                                         "content": [{"type": "output_text", "text": "a picture"}]}),
        rec("04.100", "event_msg", usage),
    ]
    original = tmp_path / f"rollout-2026-09-01T10-00-00-{CODEX_SESSION}.jsonl"
    original.write_text("".join(json.dumps(r) + "\n" for r in records))
    archived = tmp_path / "archived" / original.name
    archived.parent.mkdir()
    _strip_file(original, archived)
    a = extract_codex_session(original)
    b = extract_codex_session(archived)
    _same_rounds(a, b)
    assert a[0]["tools"][0]["result_chars"] == 2  # "[]"
