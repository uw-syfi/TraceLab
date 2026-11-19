# Trace DuckDB schema

The canonical reference for the DuckDB that every experiment queries. Built by
[`trace_db.py`](trace_db.py) (`materialize()`); **keep this file in sync when the schema changes.**

## Why a DB

Each experiment used to re-parse the normalized JSONL trace in Python, and `run_all.py` runs ~28
of them as separate processes — so a 647 MB trace was parsed ~28×. Instead we ingest the trace
**once** (DuckDB's C++ `read_json`, ~19 s for the full merged trace) into round/child tables and let every
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

`sessions` adds one observed entity per valid `(provider, session_id)`, **not** a replacement
round key. Valid identities use `claude` / `codex` and a nonempty same-provider-prefixed ID.
No provenance-derived keys or synthetic missing sessions are created. Collisions already present
after sanitization cannot be detected reliably; conflicting observations remain in `rounds`.

Claude batch extraction can recover direct parents from a native sidecar's `toolUseId` by matching
a unique `Agent`/`Task` launch and its paired structured child result within the same source family.
The extractor validates identities and chronology before sanitization; the normalized trace retains
only the resulting `parent_session_id`. Missing or ambiguous evidence does not create a parent,
and `spawnDepth` alone is not treated as parent evidence. Family membership remains independent of
direct-parent coverage.

Trusted lineage requires **one canonical lowercase UUID `identity_scope_id` on every input round**,
identifying one controlled sanitization batch. The DB accepts only the upstream UUID spelling
(`8-4-4-4-12` lowercase hex), validating original JSON scalars in an extra scope-only scan when the
column exists: DuckDB inference can normalize both UUID case and timestamp spelling. Entirely
unscoped input (missing/null markers) remains analyzable, but
linkage is `unavailable`; this DB-only legacy allowance is broader than upstream's absent-only
policy. Malformed markers, multiple scopes, or mixed scoped/unscoped rows raise `ValueError`. Materialization builds
a staging DB and replaces the target only after success, so rejected scopes preserve an existing
DB. Input and output paths resolving to the same file (including symlink aliases) are rejected
before materialization. Scope is a producer assertion, not proof that identifiers are collision-free.

## Tables

### `rounds` — one row per LLM round (PK `round_pk`)

| column | type | notes |
|---|---|---|
| `round_pk` | BIGINT | surrogate PK = ingestion ordinal (file order) |
| `ingest_seq` | BIGINT | = `round_pk`; stable order key for tie-breaks |
| `provider` | VARCHAR | `claude` / `codex` |
| `project` | VARCHAR | |
| `session_id` | VARCHAR | non-unique across `(project, session_file)` |
| `identity_scope_id` | VARCHAR | optional controlled-sanitization batch marker; missing column is null-filled |
| `session_origin` | VARCHAR | optional allowlisted origin evidence; missing column is null-filled |
| `session_kind` | VARCHAR | `main`, `worker`, `guardian`, or `unknown`; missing/unsupported evidence stays `unknown` |
| `parent_session_id` | VARCHAR | observed direct parent; retained independently of resolved lineage |
| `root_session_id` | VARCHAR | observed root; retained independently of resolved lineage |
| `session_file` | VARCHAR | |
| `round_index` | BIGINT | position within the session (not unique) |
| `round_id` | VARCHAR | source id — **non-unique**, do not key on it |
| `model` | VARCHAR | |
| `input_tokens_total` | BIGINT | |
| `prefix_tokens` | BIGINT | cached/prefix portion |
| `newly_append_tokens` | BIGINT | freshly appended portion |
| `claude_uncached_input_tokens` | BIGINT | Claude-only accounting (may be 0/null for Codex) |
| `claude_cache_creation_input_tokens` | BIGINT | |
| `claude_cache_read_input_tokens` | BIGINT | |
| `claude_cache_creation_1h_input_tokens` | BIGINT | 1-hour-TTL part of `claude_cache_creation_input_tokens` (billed at the 1-hour write rate); null = no TTL split recorded (Codex, older Claude Code): priced as 5-minute writes; pinned to BIGINT |
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

### `sessions` — one row per observed `(provider, session_id)` (composite PK)

| column | type | notes |
|---|---|---|
| `identity_scope_id` | VARCHAR | trusted common scope, or null for legacy/unscoped input |
| `provider`, `session_id` | VARCHAR | observed identity; no surrogate |
| `session_origin` | VARCHAR | canonical origin from the allowlist below; `unknown` if absent/conflicting/invalid |
| `observed_parent_session_id` | VARCHAR | unique nonempty observation; null if absent or conflicting |
| `observed_root_session_id` | VARCHAR | unique nonempty observation; null if absent or conflicting |
| `parent_status` | VARCHAR | `unknown`, `resolved`, `missing`, `invalid`, `conflict`, `cycle`, `unavailable` |
| `root_status` | VARCHAR | `unknown`, `resolved_explicit`, `resolved_chain`, `missing`, `invalid`, `conflict`, `cycle`, `unavailable` |
| `resolved_root_session_id` | VARCHAR | same-provider observed root, or null; never a synthetic entity |
| `depth` | BIGINT | direct-parent distance to a positive top-level root; null if not established |

Origin allowlist: `claude_top_level_layout`, `claude_subagent_layout`,
`codex_top_level_source`, `codex_subagent_source`, `unknown`. Null/missing/unknown origins do
not conflict with a consistent known origin. Different nonunknown origins conflict; malformed,
unsupported or wrong-provider origins invalidate lineage. Canonical origin is always `unknown` when origin evidence
is invalid or conflicting, including on unscoped rows. Null/empty references do not conflict
with a consistent nonempty reference; different nonempty references do. Canonical conflicting
references are null, but every underlying observation stays in `rounds`.

**Top-level describes the observed/provider-native hierarchy, not proof that no external
orchestrator exists.** Claude classification is layout evidence; Codex producers may classify
explicit `cli` / `vscode` / `exec` sources as top-level only without contradictory parent/subagent
evidence. Missing/default/custom/internal sources remain unknown. No source fields are needed in
this table, and absence of a parent is **never** top-level evidence.

- A positive top-level origin establishes self-root membership (`resolved_explicit`, depth 0).
  A parent or different explicit root contradicts that origin. Self-parent links, wrong-provider
  references, and self-root links with child-origin or parent evidence are invalid. An unknown
  origin plus a self-root reference alone does not establish a root.
- `parent_status` describes the direct edge: uniquely observed targets resolve, absent targets
  are missing. Parent cycles are detected iteratively. Descendants can retain a resolved direct
  edge while inheriting invalid/conflict/cycle **root** status from their ancestry.
- A complete parent chain to positive root evidence resolves membership (`resolved_chain`) and
  depth. Claude explicit root references can also establish membership in an observed root with
  unknown origin, provided the target has no child/parent contradictions. This does **not** promote
  that target's origin or resolve its own root. Codex explicit roots require positive target origin.
- Root-only membership is `resolved_explicit`, with unknown parent and null depth. Descendants may
  inherit that membership as `resolved_chain`, still with null depth. Explicit membership can
  survive a missing parent, but not invalid/conflicting/cyclic ancestry. Explicit roots disagreeing
  with parent-chain root claims conflict, even when an ancestor's claimed root is missing.
  Validated incoming explicit-root references also constrain a target's descendant root claims:
  if a child names unknown-origin A as ultimate root, another branch below A cannot name a different
  ultimate root. These constraints never establish A's own root, depth, or origin. Incoming references
  already contradicted by their ancestry do not supply constraints.
- Missing explicit root targets are `missing`, not fabricated rows. Missing ancestry without other
  root evidence is also `missing`. An observed parent whose root is unknown leaves root status
  unknown. Explicit evidence takes precedence over chain evidence when both agree; depth is still
  populated if the full direct-parent chain is known.
- Entirely unscoped new JSONL still gets observed session rows, but both linkage statuses are
  `unavailable`, resolved roots/depth are null, and existing session analyses are unchanged.

For prebuilt DBs without `sessions`, `connect()` installs a **TEMP view** of valid distinct round
identities with unknown origin, unavailable linkage, and typed null evidence/root/depth columns.
It works read-only and does not migrate or modify the file or old analysis tables. The resolver
lives in `trace_db.py`, so the existing source digest invalidates native/browser materialization
caches when these rules change.

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
| `report_source` | VARCHAR | nullable allowlist: `codex_wait_completed` (supported receipt), `claude_agent_task_result` (candidate only) |
| `report_session_ids` | VARCHAR[] | nullable unique provider-qualified child session IDs; nonempty only for supported Codex wait receipts; uses the same sanitizer mapping as session/parent IDs |

**Report evidence is not report token accounting.** The tool's owning round/session is the actual
recipient, which may itself be a subagent, not necessarily the ultimate root. Match its existing
`tool_call_id` to a `tool_result` timing event in the **same recipient session** to identify the
consuming round. A received result remains on the tool even if no subsequent model round exists;
in that case consumption and any usage change are unavailable.

- `codex_wait_completed` recognizes only a paired legacy `wait` function output with JSON shape
  `{status: {<canonical thread UUID>: {completed: <string>}, ...}, timed_out: <boolean>}` from pinned
  Codex source `fe439afb81c1ba9999a29e9e996ac6fca561ae4a`. Only string-valued `completed`
  entries supply child IDs. A malformed completed child ID invalidates the receipt. Running,
  errors, null completion, and timeout without completed entries do not qualify. A timeout with
  completed entries still carries those receipts. Spawn acknowledgements, `wait_agent` mailbox
  readiness, and generic `task_complete` events are unsupported, not report evidence.
- `claude_agent_task_result` marks an actual paired result of exact `Agent` / `Task` tool names
  before anonymization. It is **candidate-only**, not confirmed completion or an exact report.
  Child IDs remain null: no prose/agentId extraction or unproved completion metadata parsing.
  Candidates include error results, distinguished by existing `is_error`; analysis must filter
  known errors. Repeated outputs for either provider clear report evidence rather than choosing
  an ambiguous receipt/consumption pairing.
- No report body, text-derived count, token estimate, or input delta is extracted into these fields.
  Provider-recorded round usage can support separately labeled **report-associated** context changes,
  not exact report-only or provider-native report token counts.
- Missing legacy fields and unsupported evidence are typed **NULL**, never zero or an empty report
  list. Native/wasm materialization pins `VARCHAR` / `VARCHAR[]`; read-only prebuilt DBs receive
  typed null columns in the existing temporary `tool_calls` compatibility view without file changes.
  Sanitization, merge, and contribute validate the allowlist, provider, list shape, canonical Codex
  IDs, and uniqueness through the existing pre-dedup lineage validator. Claude candidates require
  null IDs; Codex receipts require a nonempty list. This validates the contract, not proof of origin.

**Effective tool latency** = `internal` if present else `wall` (legacy `latency_ms` is not in the
normalized data). Use the shared fragment `trace_db.EFFECTIVE_TOOL_LATENCY_MS_SQL`.

For a Codex `exec_command` that yields a running command, `tool_*_latency_ms` still describes only
that individual tool interaction. Use `command_chains.command_chains(con)` to derive one row per
initial command, including the first observed finish, wall time until that finish, and the separate
sum of linked tool-call time.

### `timing_events` — one row per timing event (FK `round_pk`)

UNNEST of each round's `timing_events` list; absent optional fields become null.

Native lifecycle events are `request_start` (`event_msg.task_started`), `request_end`
(`event_msg.task_complete` or Claude `assistant.stop_reason`), and `request_aborted`
(`event_msg.turn_aborted`). Claude completion requires `end_turn` or `stop_sequence`, never
`tool_use`. Markers retain observed timestamps and optional native/context `turn_id`. They attach
to existing usage-bearing rounds; no request or zero-token round is fabricated without usage.
Older traces have no explicit completion evidence.

`web_search_observation` preserves native built-in web activity without treating it as the next
model call's input. Only an explicit `call_id` establishes a `web_search` tool row. Anonymous
completions and response-item IDs do not establish extra calls. Missing native starts leave emission
and duration null. Anonymous observations and missing-start tools prevent complete wall-time
accounting. Private queries/actions are never copied into these events.

`session_kind` describes native identity evidence: positive top-level origins are `main`, Claude
subagent layouts and Codex `thread_spawn` are `worker`, and Codex `subagent.other == 'guardian'`
is `guardian`. Unknown/conflicting evidence stays `unknown`. Replayed metadata is reconciled by
native session identity. Kind does not alter lineage or rule out an external orchestrator.

Raw extractors accept an optional numeric `diagnostics_sink`; their CLIs print these counters.
Ingest returns provider totals in `coverage_diagnostics`. Counters distinguish malformed/incomplete
records, unaccounted trailing output/tool calls, and unattached or partial lifecycle/web observations.
Trailing output counts describe raw event records, not rounds or wholly lost sessions.
`unaccounted_output_records` counts model-output entries (`reasoning`, `text`, `tool_call`) left in
the parser's unclosed segment after its last accepted usage report. A syntactically valid replayed
`token_count` does not close that segment, so scanning only after the last raw usage record can undercount.

| column | type | notes |
|---|---|---|
| `round_pk` | BIGINT | → `rounds.round_pk` |
| `event_index` | BIGINT | 1-based position in the round's `timing_events` list (recover the first/ordered event) |
| `event_type` | VARCHAR | |
| `source` | VARCHAR | raw record the event was read from, e.g. `response_item.function_call_output`, `assistant.content.tool_use`; a Codex `user_message` has `event_msg.user_message`, or `event_msg.item_completed` on the main thread of Codex CLI ≥ 0.147; automatic-review rounds of CLI ≥ 0.158 mostly have no prompt event |
| `timestamp` | TIMESTAMP | normalized to naive UTC microseconds; fetch via `epoch_us` (see gotchas) |
| `turn_id` | VARCHAR | optional native/context request identity; sanitized with the round-level turn-ID map; legacy null |
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
    `reasoning_output_tokens` → BIGINT). A typed field that is null in every row, or holds mixed value
    types, is inferred as JSON: `materialize()` casts the three `claude_*` token columns back to BIGINT,
    and `_child_projection` the BIGINT / BOOLEAN fields of `tool_calls` and `timing_events`.
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
