#!/usr/bin/env python3
"""Shared DuckDB access layer for coding-trace experiments.

Instead of every experiment re-parsing the normalized JSONL trace in Python (and ``run_all.py``
doing it ~28× across subprocesses), we ingest the trace **once** into a small DuckDB with three
tables — ``rounds`` / ``tool_calls`` / ``timing_events`` — and every experiment queries that.

The same code runs natively and under Pyodide: DuckDB ships in both (native here is newer; Pyodide
pins **1.1.2**, so keep SQL within the 1.1 feature set — no 1.2+-only syntax).

Identity (important): ``round_id``, ``trace_key`` and ``(session_id, round_index)`` are **not
unique** in the data (thousands of duplicate ``round_id``; 514 duplicate ``trace_key``). So the key
is a surrogate ``round_pk`` = the row's **ingestion ordinal** (file order); child tables join on it.
Ingestion is single-threaded so ``round_pk`` follows file order, which reproduces the line-order
tie-break the stateful experiments (e.g. ``session_token_steps``) rely on. We preserve **all** rows;
de-duping would change results.

See ``DB_SCHEMA.md`` (next to this file) for the full table/column reference — keep it in sync.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
from pathlib import Path

import duckdb

EXP_UTILS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_UTILS_DIR.parents[1]  # artifacts/utils -> artifacts -> repo root
DEFAULT_DB = REPO_ROOT / "trace" / "syfi_coding_trace.duckdb"

# Round-level scalar columns kept in ``rounds`` (everything except the two nested lists).
_NESTED = ("tools", "timing_events")


def _sql_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _ts(expr: str) -> str:
    """Engine-independent TIMESTAMP for an ISO8601 column.

    ``read_json`` type inference disagrees across engines: native duckdb parses ISO timestamp
    strings to TIMESTAMP, but duckdb-wasm leaves them VARCHAR. We pin the type explicitly — round
    the value through VARCHAR and drop the ISO ``T``/``Z`` so both engines yield the *same* naive
    microsecond TIMESTAMP (the trace is uniformly UTC). ``TRY_CAST`` keeps unparseable values null.
    """
    return (
        f"TRY_CAST(replace(replace(CAST({expr} AS VARCHAR), 'T', ' '), 'Z', '') AS TIMESTAMP)"
    )


# Round columns that can be null on every row of a trace (read_json then infers JSON) and that older
# traces lack entirely: (column, pinned type).
_PINNED_OPTIONAL_ROUND_COLUMNS = (
    ("thread_kind", "VARCHAR"),
    ("parent_session_id", "VARCHAR"),
    ("workflow_id", "VARCHAR"),
    ("agent_role", "VARCHAR"),
)

# `tool_calls` / `timing_events` are UNNEST'd from each round's struct list. ``read_json`` infers each
# list's element struct from the keys ACTUALLY present in the trace, so a small or homogeneous export
# (e.g. a single Claude session whose timing events are all message-type) yields a struct MISSING keys
# the full SYFI schema has. DuckDB binds ``te.<key>`` against that inferred struct at PLAN time, so a
# missing key is a hard BinderError ("Could not find key … in struct") even with zero rows — there is
# no TRY/struct_extract escape (all of them bind-error too). We therefore introspect the element
# struct's fields and NULL-fill any the trace lacks, so the child tables always present the full
# documented schema. Each spec is (column, null-fill type, present-expr). When every key is present
# (the SYFI/full-trace case) the emitted SELECT is identical to the historical one, so the
# materialized DB stays byte-for-byte unchanged.
_TOOL_FIELDS = (
    ("tool_index", "BIGINT", "tc.tool_index"),
    ("tool_name", "VARCHAR", "tc.tool_name"),
    ("tool_call_id", "VARCHAR", "tc.tool_call_id"),
    ("emitted_at", "TIMESTAMP", _ts("tc.emitted_at")),
    ("result_at", "TIMESTAMP", _ts("tc.result_at")),
    ("tool_wall_latency_ms", "BIGINT", "tc.tool_wall_latency_ms"),
    ("tool_internal_latency_ms", "BIGINT", "tc.tool_internal_latency_ms"),
    ("is_error", "BOOLEAN", "tc.is_error"),
    ("input_chars", "BIGINT", "tc.input_chars"),
    ("result_chars", "BIGINT", "tc.result_chars"),
    ("continuation_of_tool_call_id", "VARCHAR", "tc.continuation_of_tool_call_id"),
    ("command_status", "VARCHAR", "tc.command_status"),
    ("command_exit_code", "BIGINT", "tc.command_exit_code"),
    ("executables", "VARCHAR[]", "CAST(tc.executables AS VARCHAR[])"),
    ("executable_parse_status", "VARCHAR", "CAST(tc.executable_parse_status AS VARCHAR)"),
    ("executable_parse_reason", "VARCHAR", "CAST(tc.executable_parse_reason AS VARCHAR)"),
    ("command_skeleton", "VARCHAR", "CAST(tc.command_skeleton AS VARCHAR)"),
)
_TIMING_FIELDS = (
    ("event_type", "VARCHAR", "te.event_type"),
    ("source", "VARCHAR", "te.source"),
    ("timestamp", "TIMESTAMP", _ts("te.timestamp")),
    ("tool_call_id", "VARCHAR", "te.tool_call_id"),
    ("tool_index", "BIGINT", "te.tool_index"),
    ("tool_name", "VARCHAR", "te.tool_name"),
    ("is_error", "BOOLEAN", "te.is_error"),
    ("result_chars", "BIGINT", "te.result_chars"),
    ("content_chars", "BIGINT", "te.content_chars"),
)


def _list_elem_fields(col_type: str) -> list[str] | None:
    """Field names of a list column's STRUCT element type, in declared order. Returns ``None`` when the
    column isn't a list of structs (e.g. ``JSON[]`` from non-unifiable dicts) — callers then keep
    DuckDB's permissive JSON access instead of null-filling. Field NAMES are identical across the
    native + wasm engines (they come from the data's keys), so this stays engine-independent."""
    s = col_type.strip()
    if not s.endswith("[]"):
        return None
    inner = s[:-2].strip()
    if not inner.upper().startswith("STRUCT(") or not inner.endswith(")"):
        return None
    body = inner[inner.index("(") + 1 : -1]
    parts: list[str] = []
    depth = 0
    tok = ""
    for ch in body:
        if ch in "(<[":
            depth += 1
            tok += ch
        elif ch in ")>]":
            depth -= 1
            tok += ch
        elif ch == "," and depth == 0:
            parts.append(tok)
            tok = ""
        else:
            tok += ch
    if tok.strip():
        parts.append(tok)
    names: list[str] = []
    for part in parts:
        p = part.strip()
        if not p:
            continue
        if p.startswith('"'):
            names.append(p[1 : p.index('"', 1)])  # quoted field name (e.g. "timestamp", "source")
        else:
            names.append(p.split()[0])
    return names


def _child_projection(alias: str, present: list[str] | None, specs) -> list[str]:
    """Per-field SELECT exprs producing the documented scalar TYPES regardless of how the trace's list
    element was inferred:

    * struct element, key present  → native struct access (``te.field``) — byte-identical to history;
    * struct element, key absent   → ``CAST(NULL AS <type>)`` so the column still exists, typed;
    * non-struct element (``JSON[]`` from non-unifiable dicts) → JSON-extract the key as text and CAST
      to the documented type. Struct access on a JSON element would otherwise yield JSON-typed columns
      that break downstream aggregates (e.g. ``sum(result_chars)`` has no JSON overload)."""
    out = []
    for name, null_type, native_expr in specs:
        if present is None:
            ref = f"{alias}->>'{name}'"  # JSON value-by-key → VARCHAR (NULL when the key is absent)
            expr = _ts(ref) if null_type == "TIMESTAMP" else f"CAST({ref} AS {null_type})"
        elif name in present:
            expr = native_expr
        else:
            expr = f"CAST(NULL AS {null_type})"
        out.append(f"{expr} AS {name}")
    return out


def _empty_child(lead: list[str], specs) -> str:
    """A 0-row child table carrying the full fixed schema (used when a trace has no tool/timing list)."""
    cols = lead + [f"CAST(NULL AS {null_type}) AS {name}" for name, null_type, _ in specs]
    return "SELECT " + ", ".join(cols) + " WHERE FALSE"


def materialize(trace_path, db_path) -> Path:
    """Ingest a normalized JSONL trace into a fresh DuckDB at ``db_path``. Idempotent (overwrites).

    Single C++ pass: ``read_json`` (full-sample inference so every nested key variant is captured)
    into ``_raw`` with a ``row_number()`` surrogate, then split into ``rounds`` + ``UNNEST`` the
    ``tools`` / ``timing_events`` lists into child tables keyed by ``round_pk``.
    """
    trace_path = Path(trace_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    con = duckdb.connect(str(db_path))
    try:
        # Single-threaded so round_pk == file order (reproduces line-order tie-breaks).
        con.execute("SET threads TO 1")
        con.execute(
            f"""
            CREATE TABLE _raw AS
            SELECT row_number() OVER () AS round_pk, *
            FROM read_json(
                {_sql_quote(trace_path)},
                format = 'newline_delimited',
                sample_size = -1,            -- full inference: capture every nested key variant
                maximum_object_size = 67108864
            )
            """
        )
        # rounds: surrogate key + ingest order + every round-level scalar (drop the nested lists).
        # reasoning_output_tokens is int|null; when a trace is all-null (e.g. Claude-only) read_json
        # infers JSON, so pin it to BIGINT for a stable schema across traces. It can also be ABSENT
        # entirely from a minimal export, so EXCLUDE it only when present (excluding a non-existent
        # column is itself a BinderError) and null-fill otherwise.
        #
        # `user` (the contributor id) is present in the SYFI/normalized schema but ABSENT from a raw
        # local .claude/.codex export — a personal trace carries no contributor identity. Guarantee it
        # as a nullable column for a stable schema across traces (same motivation as the cast above),
        # so consumers can read `r."user"` everywhere. It also matters that the column physically
        # EXISTS: in DuckDB a quoted `"user"` with no such column silently resolves to the `current_user`
        # keyword (e.g. 'duckdb'), which would otherwise mis-count distinct users as 1 on these traces.
        raw_types = {row[1]: row[2] for row in con.execute("PRAGMA table_info(_raw)").fetchall()}
        raw_cols = set(raw_types)
        # Nullable fields newer than older traces get the same pin when present (absent stays absent;
        # readers null-fill via _has_round_column).
        pinned = [(c, t) for c, t in _PINNED_OPTIONAL_ROUND_COLUMNS if c in raw_cols]
        exclude = [c for c in ("round_pk", "tools", "timing_events", "reasoning_output_tokens") if c in raw_cols]
        exclude += [c for c, _ in pinned]
        reason_select = (
            "TRY_CAST(reasoning_output_tokens AS BIGINT) AS reasoning_output_tokens"
            if "reasoning_output_tokens" in raw_cols
            else "CAST(NULL AS BIGINT) AS reasoning_output_tokens"
        )
        reason_select += "".join(f", TRY_CAST({c} AS {t}) AS {c}" for c, t in pinned)
        user_select = "" if "user" in raw_cols else ', CAST(NULL AS VARCHAR) AS "user"'
        con.execute(
            "CREATE TABLE rounds AS "
            "SELECT round_pk, round_pk AS ingest_seq, "
            f"       * EXCLUDE ({', '.join(exclude)}), "
            f"       {reason_select}"
            f"{user_select} "
            "FROM _raw"
        )
        # tool_calls: one row per tool call. Drop the raw `input` dict (schema-drift trap); keep only
        # the typed scalars experiments use. Null-fill any keys this trace's tool struct lacks (see
        # _TOOL_FIELDS); fall back to an empty, full-schema table when there's no tool list at all.
        tools_type = raw_types.get("tools", "")
        if "tools" in raw_cols and tools_type.strip().endswith("[]"):
            tc_cols = ["round_pk"] + _child_projection("tc", _list_elem_fields(tools_type), _TOOL_FIELDS)
            con.execute(
                "CREATE TABLE tool_calls AS SELECT "
                + ", ".join(tc_cols)
                + " FROM (SELECT round_pk, UNNEST(tools) AS tc FROM _raw WHERE tools IS NOT NULL)"
            )
        else:
            con.execute("CREATE TABLE tool_calls AS " + _empty_child(["CAST(NULL AS BIGINT) AS round_pk"], _TOOL_FIELDS))
        # timing_events: union of the observed key variants (nulls where a key is absent). The two
        # UNNESTs zip in lockstep, so event_index is the 1-based position in the round's list.
        te_type = raw_types.get("timing_events", "")
        if "timing_events" in raw_cols and te_type.strip().endswith("[]"):
            te_cols = ["round_pk", "event_index"] + _child_projection("te", _list_elem_fields(te_type), _TIMING_FIELDS)
            con.execute(
                "CREATE TABLE timing_events AS SELECT "
                + ", ".join(te_cols)
                + " FROM (SELECT round_pk, UNNEST(timing_events) AS te, "
                "UNNEST(range(1, length(timing_events) + 1)) AS event_index "
                "FROM _raw WHERE timing_events IS NOT NULL)"
            )
        else:
            con.execute(
                "CREATE TABLE timing_events AS "
                + _empty_child(["CAST(NULL AS BIGINT) AS round_pk", "CAST(NULL AS BIGINT) AS event_index"], _TIMING_FIELDS)
            )
        con.execute("DROP TABLE _raw")
        # Provenance: the absolute source trace this DB was built from. Lets an experiment recover
        # data deliberately dropped from the slim schema (e.g. claude_long_tool_calls re-reads the
        # raw `tool.input` for its `input_preview` column) without needing a separate `-i`.
        con.execute(
            f"CREATE TABLE trace_source AS SELECT {_sql_quote(trace_path.resolve())} AS path"
        )
    finally:
        con.close()
    return db_path


def compact_database(db_path) -> Path:
    """Rewrite a DuckDB file into a fresh copy with no retained free blocks.

    Building this DB from JSONL creates a large temporary ``_raw`` table and then drops it.
    DuckDB keeps those freed blocks in the file for reuse, which is fine for local queries but bad
    for upload/release artifacts because the apparent file size includes the free blocks. Exporting
    and importing into a fresh file rewrites only the live tables.
    """
    db_path = Path(db_path)
    parent = db_path.parent
    export_dir = Path(tempfile.mkdtemp(prefix=f".{db_path.name}.export.", dir=parent))
    compact_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{db_path.name}.compact.",
            suffix=".duckdb",
            dir=parent,
            delete=False,
        ) as fh:
            compact_path = Path(fh.name)
        compact_path.unlink()

        src_con = duckdb.connect(str(db_path), read_only=True)
        try:
            src_con.execute(f"EXPORT DATABASE {_sql_quote(export_dir)}")
        finally:
            src_con.close()

        dst_con = duckdb.connect(str(compact_path))
        try:
            dst_con.execute(f"IMPORT DATABASE {_sql_quote(export_dir)}")
        finally:
            dst_con.close()

        compact_path.replace(db_path)
        return db_path
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)
        if compact_path is not None and compact_path.exists():
            compact_path.unlink()


def _schema_version() -> str:
    """A short digest of this module's source — the cache key includes it so a schema change here
    (new column, type fix) invalidates stale caches automatically, even when the trace is unchanged."""
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:8]


def _cache_db_path(trace_path: Path) -> Path:
    """A stable temp DB path for a given (trace, schema-version) so repeated runs reuse the ingest."""
    key = f"{trace_path.resolve()}::{_schema_version()}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "coding_trace_db" / f"{trace_path.stem}.{digest}.duckdb"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _has_column(con: "duckdb.DuckDBPyConnection", table: str, column: str) -> bool:
    return bool(
        con.execute(
            """
            SELECT count(*) > 0
            FROM information_schema.columns
            WHERE table_name = ? AND column_name = ?
            """,
            [table, column],
        ).fetchone()[0]
    )


def _primary_database_name(con: "duckdb.DuckDBPyConnection") -> str:
    """Return the attached database name for the opened DuckDB file."""
    for _, name, path in con.execute("PRAGMA database_list").fetchall():
        if path:
            return name
    return "memory"


def _install_compat_views(con: "duckdb.DuckDBPyConnection") -> None:
    """Expose current-schema views for older released DB assets.

    The first public DuckDB release predated ``timing_events.event_index``. Current analyses use
    that column to recover the original per-round timing-event order. DuckDB table ``rowid`` preserves
    the insertion order of the materialized child rows, so a temp view can reconstruct the same
    1-based per-round index without modifying the read-only release asset.
    """
    db = _ident(_primary_database_name(con))
    if not _has_column(con, "timing_events", "event_index"):
        con.execute(
            f"""
            CREATE TEMP VIEW timing_events AS
            SELECT
              row_number() OVER (PARTITION BY round_pk ORDER BY rowid) AS event_index,
              *
            FROM {db}.timing_events
            """
        )

    # Command-continuation fields were simplified after their first release. For DBs with the old
    # process-oriented columns, derive the new per-call facts in a compatibility view. Earlier DBs
    # with no continuation data receive nullable columns.
    missing = [
        name
        for name in (
            "continuation_of_tool_call_id",
            "command_status",
            "command_exit_code",
            "executables",
            "executable_parse_status",
            "executable_parse_reason",
            "command_skeleton",
        )
        if not _has_column(con, "tool_calls", name)
    ]
    if missing:
        has_old_link = _has_column(con, "tool_calls", "root_tool_call_id")
        has_old_status = _has_column(con, "tool_calls", "process_state")
        has_old_exit = _has_column(con, "tool_calls", "process_exit_code")
        has_old_session = _has_column(con, "tool_calls", "process_session_id")
        derived: dict[str, str] = {
            "continuation_of_tool_call_id": (
                "CASE WHEN tool_name = 'write_stdin' THEN root_tool_call_id END"
                if has_old_link
                else "CAST(NULL AS VARCHAR)"
            ),
            "command_status": (
                "CASE "
                "WHEN tool_name = 'exec_command' AND process_session_id IS NOT NULL "
                "THEN 'running' "
                "WHEN process_state = 'exited' THEN 'finished' "
                "WHEN process_state = 'continuation_error' THEN 'session_error' "
                "ELSE process_state END"
                if has_old_status and has_old_session
                else (
                    "CASE "
                    "WHEN process_state = 'exited' THEN 'finished' "
                    "WHEN process_state = 'continuation_error' THEN 'session_error' "
                    "ELSE process_state END"
                    if has_old_status
                    else "CAST(NULL AS VARCHAR)"
                )
            ),
            "command_exit_code": (
                "CASE "
                "WHEN tool_name = 'exec_command' AND process_session_id IS NOT NULL THEN NULL "
                "ELSE process_exit_code END"
                if has_old_exit and has_old_session
                else ("process_exit_code" if has_old_exit else "CAST(NULL AS BIGINT)")
            ),
            "executables": "CAST(NULL AS VARCHAR[])",
            "executable_parse_status": "CAST(NULL AS VARCHAR)",
            "executable_parse_reason": "CAST(NULL AS VARCHAR)",
            "command_skeleton": "CAST(NULL AS VARCHAR)",
        }
        additions = ", ".join(
            f"{derived[name]} AS {_ident(name)}" for name in missing
        )
        con.execute(f"CREATE TEMP VIEW tool_calls AS SELECT *, {additions} FROM {db}.tool_calls")

    # Round fields newer than a DB read as typed NULLs:
    # unknown, not zero or "main".
    missing_round = [(c, t) for c, t in _PINNED_OPTIONAL_ROUND_COLUMNS if not _has_column(con, "rounds", c)]
    if missing_round:
        additions = ", ".join(f"CAST(NULL AS {t}) AS {_ident(c)}" for c, t in missing_round)
        con.execute(f"CREATE TEMP VIEW rounds AS SELECT *, {additions} FROM {db}.rounds")


def connect(db_path, *, read_only: bool = True) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect(str(db_path), read_only=read_only)
    _install_compat_views(con)
    return con


def raw_connect(db_path, *, read_only: bool = True) -> "duckdb.DuckDBPyConnection":
    """Open a DuckDB file without installing compatibility views."""
    return duckdb.connect(str(db_path), read_only=read_only)


def add_db_args(parser: argparse.ArgumentParser, *, default_output_dir: Path | None = None) -> argparse.ArgumentParser:
    """Uniform I/O surface for every experiment: --db | -i/--input, plus -o/--output-dir."""
    parser.add_argument(
        "-i", "--input", type=Path, default=None,
        help="optional normalized JSONL override; materialized to a temporary DuckDB",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"prebuilt DuckDB (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "-o", "--output-dir", type=Path, default=default_output_dir,
        help="directory for figures/CSVs (defaults to the experiment folder)",
    )
    return parser


def open_from_args(args) -> "duckdb.DuckDBPyConnection":
    """Open the explicit JSONL override, otherwise the requested/default trace DB."""
    out = getattr(args, "output_dir", None)
    if out is not None:
        Path(out).mkdir(parents=True, exist_ok=True)

    input_path = getattr(args, "input", None)
    if input_path is None:
        return connect(Path(args.db), read_only=True)

    trace = Path(input_path)
    cache = _cache_db_path(trace)
    fresh = cache.exists() and cache.stat().st_mtime >= trace.stat().st_mtime
    if not fresh:
        materialize(trace, cache)
    return connect(cache, read_only=True)


# Effective tool latency precedence: internal runner-reported duration, else wall (result-emitted).
# (Legacy `latency_ms` is not present in the normalized tool_calls schema.) Use as a SQL fragment.
EFFECTIVE_TOOL_LATENCY_MS_SQL = (
    "CASE WHEN tool_internal_latency_ms IS NOT NULL THEN tool_internal_latency_ms "
    "WHEN tool_wall_latency_ms IS NOT NULL THEN tool_wall_latency_ms END"
)


def _main(argv: list[str]) -> int:
    """CLI: `python trace_db.py <trace.jsonl> [db_path]` — materialize + print table counts."""
    ap = argparse.ArgumentParser(description="Materialize a normalized trace into a DuckDB.")
    ap.add_argument("trace", type=Path)
    ap.add_argument("db", type=Path, nargs="?", default=None)
    ap.add_argument(
        "--no-compact",
        action="store_true",
        help=(
            "skip the export/import compaction pass; faster, but the DB may retain large free "
            "blocks after the temporary _raw table is dropped"
        ),
    )
    args = ap.parse_args(argv)
    db_path = args.db or _cache_db_path(args.trace)
    materialize(args.trace, db_path)
    if not args.no_compact:
        compact_database(db_path)
    con = connect(db_path, read_only=True)
    try:
        for tbl in ("rounds", "tool_calls", "timing_events"):
            n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            print(f"{tbl:14s} {n:>12,}")
        uniq = con.execute("SELECT count(*) = count(DISTINCT round_pk) FROM rounds").fetchone()[0]
        print(f"round_pk unique: {bool(uniq)}")
    finally:
        con.close()
    print(f"db -> {db_path}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
