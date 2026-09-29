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
    cache_write: int
    output: int
    reasoning: int
    first_ts_us: Optional[int]
    # True when the round was triggered by a visible user message (vs. a tool result). Drives the
    # human-vs-autonomous split (sessions timeline, autonomy depth, human-in-the-loop stats).
    is_user_input: bool
    tool_calls: int
    # Subagent hierarchy (None throughout on traces collected before these columns existed, which
    # the UI shows as "unknown" rather than as main-thread work).
    thread_kind: Optional[str]
    parent_session_id: Optional[str]
    workflow_id: Optional[str]
    agent_role: Optional[str]


# round_pk is unique; event_index = 1 is exactly one row per round, so the LEFT JOIN keeps every
# round (ts NULL when it has no timing events). tool call counts come from a grouped child scan.
_SQL_TEMPLATE = """
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
       r.prefix_tokens, r.newly_append_tokens, {cache_write_expr},
       r.output_tokens, r.reasoning_output_tokens,
       r.first_input_event_type, f.ts_us, COALESCE(t.n, 0) AS tool_calls,
       {hierarchy_exprs}
FROM rounds r
LEFT JOIN first_ev f USING (round_pk)
LEFT JOIN tool_n  t USING (round_pk)
ORDER BY r.round_pk
"""


def _i(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _has_round_column(con, column: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT count(*) > 0
            FROM information_schema.columns
            WHERE table_name = 'rounds' AND column_name = ?
            """,
            [column],
        ).fetchone()[0]
    )


# (column, SQL type). Cast explicitly: a DB built by an older trace_db.py may hold these untyped.
HIERARCHY_COLUMNS = (
    ("thread_kind", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("workflow_id", "VARCHAR"),
    ("agent_role", "VARCHAR"),
)


def hierarchy_select(con, alias: str = "r") -> str:
    """SELECT expressions for the hierarchy columns, NULL for any column the trace predates."""
    return ", ".join(
        f"CAST({alias}.{name} AS {sql_type}) AS {name}"
        if _has_round_column(con, name)
        else f"CAST(NULL AS {sql_type}) AS {name}"
        for name, sql_type in HIERARCHY_COLUMNS
    )


def load_rounds(con) -> list[RoundRow]:
    rows: list[RoundRow] = []
    cache_write_expr = (
        "r.claude_cache_creation_input_tokens"
        if _has_round_column(con, "claude_cache_creation_input_tokens")
        else "CAST(NULL AS BIGINT) AS claude_cache_creation_input_tokens"
    )
    sql = _SQL_TEMPLATE.format(
        cache_write_expr=cache_write_expr, hierarchy_exprs=hierarchy_select(con)
    )
    for (
        round_pk,
        provider,
        model,
        session_id,
        user,
        round_index,
        prefix,
        append,
        cache_write,
        output,
        reasoning,
        first_input_event_type,
        ts_us,
        tool_calls,
        thread_kind,
        parent_session_id,
        workflow_id,
        agent_role,
    ) in con.execute(sql).fetchall():
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
                "cache_write": _i(cache_write),
                "output": _i(output),
                "reasoning": _i(reasoning),
                "first_ts_us": int(ts_us) if ts_us is not None else None,
                "is_user_input": first_input_event_type == "user_message",
                "tool_calls": int(tool_calls),
                "thread_kind": thread_kind,
                "parent_session_id": parent_session_id,
                "workflow_id": workflow_id,
                "agent_role": agent_role,
            }
        )
    return rows
