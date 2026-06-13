"""Shared per-round loader.

Every bulk builder (kpis / cost / daily / sessions-list / facts) needs the same lightweight
per-round projection — token split, model, session, and the round's *start* timestamp. We pull it
once into memory (mirrors ``overview_summary._rows_from_db``'s in-Python pattern) and let the
builders derive from the list, so the DB is scanned a small constant number of times rather than
once per metric.

``first_ts_us`` is the round's FIRST timing event (``event_index = 1``) as integer
epoch-microseconds (native/wasm-identical marshalling per DB_SCHEMA.md). It's the round's wall-clock
"start", used for per-day / work-rhythm bucketing. ``None`` when the round carried no timing events.
"""

from __future__ import annotations

from typing import Any, Optional, TypedDict


class RoundRow(TypedDict):
    round_pk: int
    provider: Optional[str]
    model: Optional[str]
    session_id: Optional[str]
    user: Optional[str]
    round_index: int
    prefix: int
    append: int
    output: int
    reasoning: int
    first_ts_us: Optional[int]
    # True when the round was triggered by a visible user message (vs. a tool result). Drives the
    # human-vs-autonomous split (sessions timeline, autonomy depth, human-in-the-loop stats).
    is_user_input: bool
    tool_calls: int


# round_pk is unique; event_index = 1 is exactly one row per round, so the LEFT JOIN keeps every
# round (ts NULL when it has no timing events). tool call counts come from a grouped child scan.
_SQL = """
WITH first_ev AS (
    SELECT round_pk, CAST(epoch_us(timestamp) AS BIGINT) AS ts_us
    FROM timing_events
    WHERE event_index = 1
),
tool_n AS (
    SELECT round_pk, count(*) AS n
    FROM tool_calls
    GROUP BY round_pk
)
SELECT r.round_pk, r.provider, r.model, r.session_id, r."user", r.round_index,
       r.prefix_tokens, r.newly_append_tokens, r.output_tokens, r.reasoning_output_tokens,
       r.first_input_event_type, f.ts_us, COALESCE(t.n, 0) AS tool_calls
FROM rounds r
LEFT JOIN first_ev f USING (round_pk)
LEFT JOIN tool_n  t USING (round_pk)
ORDER BY r.round_pk
"""


def _i(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def load_rounds(con) -> list[RoundRow]:
    rows: list[RoundRow] = []
    for (
        round_pk,
        provider,
        model,
        session_id,
        user,
        round_index,
        prefix,
        append,
        output,
        reasoning,
        first_input_event_type,
        ts_us,
        tool_calls,
    ) in con.execute(_SQL).fetchall():
        rows.append(
            {
                "round_pk": int(round_pk),
                "provider": provider,
                "model": model,
                "session_id": session_id,
                "user": user,
                "round_index": _i(round_index),
                "prefix": _i(prefix),
                "append": _i(append),
                "output": _i(output),
                "reasoning": _i(reasoning),
                "first_ts_us": int(ts_us) if ts_us is not None else None,
                "is_user_input": first_input_event_type == "user_message",
                "tool_calls": int(tool_calls),
            }
        )
    return rows
