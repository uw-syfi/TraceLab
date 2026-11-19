"""Read-only matched subagent families and shared distribution summaries."""

from __future__ import annotations

import numpy as np


# Unknown-origin root anchors are not positively identified mains.
COHORT_SQL = """
WITH mains AS (
    SELECT provider, session_id
    FROM sessions
    WHERE session_origin IN ('claude_top_level_layout', 'codex_top_level_source')
      AND root_status IN ('resolved_explicit', 'resolved_chain')
      AND resolved_root_session_id = session_id
), descendants AS (
    SELECT s.provider, s.session_id, s.resolved_root_session_id AS root_session_id
    FROM sessions s
    JOIN mains m ON s.provider = m.provider
                AND s.resolved_root_session_id = m.session_id
    WHERE s.root_status IN ('resolved_explicit', 'resolved_chain')
      AND s.session_id <> s.resolved_root_session_id
)
SELECT m.provider, m.session_id, m.session_id AS root_session_id, 'main' AS role
FROM mains m
WHERE EXISTS (
    SELECT 1 FROM descendants d
    WHERE d.provider = m.provider AND d.root_session_id = m.session_id
)
UNION ALL
SELECT provider, session_id, root_session_id, 'descendant' AS role FROM descendants
""".strip()


def session_kinds_sql(con) -> str:
    """Session-level source kinds; legacy, absent and conflicting evidence stays unknown."""
    columns = {row[1] for row in con.execute("PRAGMA table_info('rounds')").fetchall()}
    if "session_kind" not in columns:
        return "SELECT provider, session_id, 'unknown' AS session_kind FROM rounds GROUP BY 1, 2"
    known = "CASE WHEN session_kind IN ('main', 'worker', 'guardian') THEN session_kind END"
    return f"""
        SELECT provider, session_id,
               CASE WHEN count(DISTINCT {known}) = 1 THEN max({known})
                    ELSE 'unknown' END AS session_kind
        FROM rounds GROUP BY provider, session_id
    """


def summarize(values) -> dict[str, int | float | None]:
    """Summarize finite, nonmissing observations; preserve genuine zero and signed values."""
    arr = np.asarray([value for value in values if value is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    row = {"n": int(arr.size), "avg": float(arr.mean()) if arr.size else None}
    for q, value in zip((25, 50, 90, 99), np.percentile(arr, (25, 50, 90, 99)) if arr.size else [None] * 4):
        row[f"p{q}"] = float(value) if value is not None else None
    return row
