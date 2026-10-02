# Trace DuckDB schema

The canonical reference for the DuckDB that every experiment queries. Built by
[`trace_db.py`](trace_db.py) (`materialize()`); **keep this file in sync when the schema changes.**

## Why a DB

Each experiment used to re-parse the normalized JSONL trace in Python, and `run_all.py` runs ~28
of them as separate processes — so a 647 MB trace was parsed ~28×. Instead we ingest the trace
**once** (DuckDB's C++ `read_json`, ~19 s for the full merged trace) into three tables and let every
experiment query them. The same code runs natively and under Pyodide (DuckDB ships in both; Pyodide
pins **1.1.2**, so SQL must stay within the 1.1 feature set).

## Identity model (read this before writing SQL)

The natural keys in the source data are **not unique**:

| candidate key | unique? | notes |
|---|---|---|
| `round_id` | ❌ | ~8.9k duplicated ids in the merged trace |
| `trace_key` | ❌ | 514 duplicates, some differ only by `project`/`session_file` |
| `(session_id, round_index)` | ❌ | 514 duplicates |

So the key is a **surrogate**: `round_pk` = the row's ingestion ordinal. Ingestion is
**single-threaded**, so `round_pk` (= `ingest_seq`) follows **file order** — this reproduces the
line-order tie-break the stateful experiments (e.g. `session_token_steps`) rely on. Child tables
(`tool_calls`, `timing_events`) join back via `round_pk`.

**Duplicate policy:** we preserve **all** rows (de-duping would change results). Join on `round_pk`,
never on `round_id`/`trace_key`. To order rounds within a session use
`ORDER BY round_index, ingest_seq`.

## Tables

### `rounds` — one row per LLM round (PK `round_pk`)

| column | type | notes |
|---|---|---|
| `round_pk` | BIGINT | surrogate PK = ingestion ordinal (file order) |
| `ingest_seq` | BIGINT | = `round_pk`; stable order key for tie-breaks |
| `provider` | VARCHAR | `claude` / `codex` |
| `project` | VARCHAR | |
| `session_id` | VARCHAR | non-unique across `(project, session_file)` |
| `session_file` | VARCHAR | |
| `thread_kind` | VARCHAR | `main` / `subagent` / `guardian` (Codex auto-review); null = unknown (older trace), never "main". The four hierarchy columns are pinned to VARCHAR when present and exposed as NULL on older DBs by `connect()` |
| `parent_session_id` | VARCHAR | parent's `session_id` for a delegated child; for a Codex automatic review (`guardian`), the session it reviewed; equals `session_id` only for Claude inline sidechain rounds (round-level, not a child session) |
| `workflow_id` | VARCHAR | Claude workflow that launched the child; null otherwise (always null for Codex) |
| `agent_role` | VARCHAR | Claude `attributionAgent` / Codex `thread_spawn.agent_role` / `guardian` / `review` / `compact`; sanitized: built-ins kept, others `custom_role_N` |
| `round_index` | BIGINT | position within the session (not unique) |
| `round_id` | VARCHAR | source id — **non-unique**, do not key on it |
| `model` | VARCHAR | |
| `input_tokens_total` | BIGINT | |
| `prefix_tokens` | BIGINT | cached/prefix portion |
| `newly_append_tokens` | BIGINT | freshly appended portion |
| `claude_uncached_input_tokens` | BIGINT | Claude-only accounting (may be 0/null for Codex) |
| `claude_cache_creation_input_tokens` | BIGINT | |
| `claude_cache_read_input_tokens` | BIGINT | |
| `output_tokens` | BIGINT | |
| `reasoning_output_tokens` | BIGINT | int\|null; **pinned to BIGINT** (all-null traces otherwise infer JSON) |
| `current_input_event_count` | BIGINT | |
| `current_user_message_count` | BIGINT | |
| `current_tool_result_count` | BIGINT | |
| `current_user_message_chars` | BIGINT | |
| `current_tool_result_chars` | BIGINT | |
| `current_input_chars` | BIGINT | |
| `first_input_event_type` | VARCHAR | |
| `home`, `user`, `store` | VARCHAR | provenance |
| `trace_key` | VARCHAR | source key — **non-unique** |

The four hierarchy columns may be absent (traces predating them); a DB built by an older
`trace_db.py` may also hold them untyped. Readers go through
`web_analytics/rounds.py::hierarchy_select()`, which casts each to its type and substitutes a typed
NULL for a missing column; sessions and topology read only through it.

### `tool_calls` — one row per tool call (FK `round_pk`)

UNNEST of each round's `tools` list. The raw `tool.input` dict is **dropped** (schema-drift trap;
re-add as a single JSON column only if an experiment truly needs it).

| column | type | notes |
|---|---|---|
| `round_pk` | BIGINT | → `rounds.round_pk` |
| `tool_index` | BIGINT | position within the round |
| `tool_name` | VARCHAR | |
| `tool_call_id` | VARCHAR | |
| `emitted_at` | TIMESTAMP | |
| `result_at` | TIMESTAMP | |
| `tool_wall_latency_ms` | BIGINT | `result_at − emitted_at` (nullable) |
| `tool_internal_latency_ms` | BIGINT | runner-reported duration (nullable) |
| `is_error` | BOOLEAN | |
| `input_chars` | BIGINT | |
| `result_chars` | BIGINT | |
| `continuation_of_tool_call_id` | VARCHAR | initial `exec_command.tool_call_id`; present only on linked `write_stdin` calls |
| `command_status` | VARCHAR | `running` \| `finished` \| `aborted` \| `failed` \| `session_error`; only for `exec_command` / `write_stdin` results |
| `command_exit_code` | BIGINT | present when that specific result reports completion with an exit code |
| `executables` | VARCHAR[] | ordered executable occurrences for command-launch calls; public/common names remain visible and all other names become stable `custom_N` labels |
| `executable_parse_status` | VARCHAR | `success` \| `partial` \| `failed`; null for non-command tools |
| `executable_parse_reason` | VARCHAR | short reason for partial/failed extraction, otherwise null |
| `command_skeleton` | VARCHAR | privacy-safe executable/operator structure with arguments removed; empty when unavailable |

**Effective tool latency** = `internal` if present else `wall` (legacy `latency_ms` is not in the
normalized data). Use the shared fragment `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`.

For a Codex `exec_command` that yields a running command, `tool_*_latency_ms` still describes only
that individual tool interaction. Use `command_chains.command_chains(con)` to derive one row per
initial command, including the first observed finish, wall time until that finish, and the separate
sum of linked tool-call time.

### `timing_events` — one row per timing event (FK `round_pk`)

UNNEST of each round's `timing_events` list (union of 5 key variants → nulls where a key is absent).

| column | type | notes |
|---|---|---|
| `round_pk` | BIGINT | → `rounds.round_pk` |
| `event_index` | BIGINT | 1-based position in the round's `timing_events` list (recover the first/ordered event) |
| `event_type` | VARCHAR | |
| `source` | VARCHAR | |
| `timestamp` | TIMESTAMP | normalized to naive UTC microseconds; fetch via `epoch_us` (see gotchas) |
| `tool_call_id` | VARCHAR | nullable |
| `tool_index` | BIGINT | nullable |
| `tool_name` | VARCHAR | nullable |
| `is_error` | BOOLEAN | nullable |
| `result_chars` | BIGINT | nullable |
| `content_chars` | BIGINT | nullable |

Order rows within a round by `event_index`; the first event is `event_index = 1`.

### `trace_source` — provenance (single row)

| column | type | notes |
|---|---|---|
| `path` | VARCHAR | absolute path of the source JSONL this DB was materialized from |

A one-row table recording the trace the DB was built from. Lets an experiment recover data the slim
schema deliberately drops — e.g. `claude_long_tool_calls` re-reads the raw `tool.input` from this
source to fill its `input_preview` column — without a separate `-i`. (A shipped/copied DB whose
`path` no longer exists falls back gracefully: the consumer checks `exists()` first.)

## Building / using it

```bash
# materialize + print table counts
uv run python artifacts/utils/trace_db.py trace/sample.jsonl "$TMPDIR/trace.duckdb"
```

In an experiment:

```python
import trace_db
parser = argparse.ArgumentParser()
trace_db.add_db_args(parser, default_output_dir=EXP_DIR)   # --db | -i/--input | -o/--output-dir
args = parser.parse_args()
con = trace_db.open_from_args(args)      # opens --db, else materializes -i to a temp cache
rows = con.execute("SELECT provider, count(*) FROM rounds GROUP BY 1").fetchall()
```

`run_all.py` builds the DB once (`build-db` step) and passes `--db` to every experiment.

## Conventions / gotchas

- **DuckDB 1.1 compatibility** — Pyodide pins 1.1.2; avoid 1.2+-only syntax.
- **Native vs. wasm engine differences (verified, both handled):**
  - `read_json` **type inference disagrees**: native parses ISO timestamp strings to `TIMESTAMP`,
    duckdb-wasm leaves them `VARCHAR`. So `materialize()` pins timestamp columns explicitly via
    `_ts()` (round-trip through VARCHAR, strip ISO `T`/`Z`) — identical naive microsecond
    `TIMESTAMP` on both engines. Don't rely on inference for any typed column; pin it (see also
    `reasoning_output_tokens` → BIGINT).
  - **Fetch marshalling differs**: native returns `TIMESTAMP` cells as Python `datetime`,
    duckdb-wasm returns them as **strings**. Never fetch a raw `TIMESTAMP` and do datetime math —
    select `CAST(epoch_us(ts) AS BIGINT)` (an int round-trips identically) and rebuild the datetime
    in Python.
- **Determinism** — ingest is single-threaded for file-order `round_pk`. `GROUP BY` output order is
  **not** deterministic (hash aggregate); pin any tie-break explicitly (e.g. first-appearance
  ordinal via `min(row_number() OVER (ORDER BY round_pk, tool_index))`), or output flips between DB
  builds. Pin sampling (hash/`row_number`-based, or sample in Python) and percentile method
  (`quantile_cont` = linear interpolation) so output matches the legacy Python path.
- **Stateful experiments are hybrid** — SQL for ordered rows / adjacent pairs, Python for session
  heuristics + plotting.
- **DB size ≈ JSONL size** (high-cardinality strings, timestamps; ~527 MB for the 647 MB merged
  trace; ~74 MB for the 50k-round sample, identical native and wasm). Fine natively; for the
  in-browser worker pool the DB is *distributed* (one copy per worker), so keep it slim and watch
  peak memory — a 122 MB trace materialized in wasm peaked at ~720 MB heap in one worker.
