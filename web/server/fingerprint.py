#!/usr/bin/env python3
"""Seed-invariant session fingerprints for dedup.

The sanitizer rewrites identifiers (and, with ``--random-seed``, differently on every run) but
**never touches token counts**. So the only stable identity of a session across independent
sanitizations is its ordered series of per-round token fields. We hash that series per session;
two uploads of the same underlying session produce the same fingerprint even if their pseudonymous
ids and bytes differ.

Dedup is therefore **whole-session**: an upload's already-seen sessions are skipped, genuinely new
sessions are appended intact. We never drop individual rows within a session (duplicate
``trace_key`` rows are legitimate — see ``artifacts/utils/trace_db.py``).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

# Token-accounting fields, all preserved verbatim by the sanitizer. The tuple is the per-round
# signal; the ordered list of tuples within a session is the fingerprinted series.
TOKEN_FIELDS = (
    "prefix_tokens",
    "newly_append_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "claude_uncached_input_tokens",
    "claude_cache_creation_input_tokens",
    "claude_cache_read_input_tokens",
)


def _round_tuple(row: dict[str, Any]) -> list[Any]:
    return [row.get(field) for field in TOKEN_FIELDS]


def session_fingerprints(rows: Sequence[dict[str, Any]]) -> dict[str, list[int]]:
    """Map ``fingerprint -> [indices into ``rows``]`` for each session present in ``rows``.

    Rows are grouped by ``session_id`` and ordered by ``round_index`` (file order as the stable
    tie-break), then the token series is hashed. ``session_id`` itself is **excluded** from the
    hash so the fingerprint stays invariant to the sanitizer's per-seed id mangling.
    """
    groups: dict[Any, list[tuple[Any, int, list[Any]]]] = {}
    for index, row in enumerate(rows):
        sid = row.get("session_id")
        round_index = row.get("round_index")
        groups.setdefault(sid, []).append((round_index, index, _round_tuple(row)))

    out: dict[str, list[int]] = {}
    for items in groups.values():
        # Order by round_index (None last), then original position as a stable tie-break.
        items.sort(key=lambda t: (t[0] is None, t[0], t[1]))
        series = [tup for _, _, tup in items]
        indices = [idx for _, idx, _ in items]
        payload = json.dumps(series, separators=(",", ":"))
        fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        out.setdefault(fingerprint, []).extend(indices)
    return out
