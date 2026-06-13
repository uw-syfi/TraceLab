# cache_replay

**Do the prompt-cache counters behave as a replay model predicts — does this round's cached prefix
match the previous request's total input, and does this round's cache write / append contain the
previous assistant output plus the new content?**

## Experiment overview

The previous assistant response is normally replayed into the next prompt, so the prompt-cache
counters should line up across adjacent rounds. This experiment formalizes the hand analysis in
`../../../docs/prompt_cache_accounting.md` into a repeatable report by walking adjacent rounds
**within a session** and checking two relationships, plus an adjusted-append accounting:

- **Prefix continuity** — `next.prefix_tokens ≈ prev.input_tokens_total` (the reused cached prefix is
  the prior request's full input). The report tabulates the signed error, absolute error, relative
  error, the within-{0,1,2,8,32,128,512,2048}-token shares, and the same error after adding the prior
  output back in.
- **Adjusted append** — `signed_adjusted_append = next.newly_append_tokens − prev.output_tokens` and
  `adjusted_append = max(0, signed_adjusted_append)`, isolating genuinely new content from the
  replayed prior response. For **Codex**, `visible_output_tokens = max(0, output_tokens −
  reasoning_output_tokens)` is also tracked, since reasoning is generally not replayed as visible
  history. Reported with per-start-event breakdowns, clamped-vs-not-clamped subsets, long-current-
  input bands (tool/user/raw chars), and long-prior-output bands.
- **Claude cache creation** — when the normalized Claude cache fields
  (`claude_uncached_input_tokens`, `claude_cache_creation_input_tokens`,
  `claude_cache_read_input_tokens`) are all present, the report builds a Claude-specific cache-write
  section (prior output into next cache write, short/long current-input behavior).

Method and assumptions:

- **Adjacency ordering is file order within a session.** Rounds are grouped per
  `(provider, session_id)` in first-appearance (file) order, then stably sorted by `round_index`, so
  ties keep file order; only pairs whose `round_index` differs by exactly 1 are kept. The shared
  DuckDB surrogate key `ingest_seq` (`= round_pk`) *is* that file order, so pulling
  `ORDER BY ingest_seq` and grouping in Python reproduces the per-session row order and
  session-visitation order byte-for-byte versus the pre-migration JSONL loader.
- **Validity gate, unchanged.** Rows whose `provider` is not `claude`/`codex`, whose `session_id` is
  not a string, or whose `round_index` is not an integer are skipped (in the pinned-schema DB these
  are the NULL rows). Per-round input-event type and char/count fields come from the normalized
  top-level `current_*` / `first_input_event_type` columns (the legacy `timing_events` fallback path
  is preserved in code but never fires on the normalized trace, where those columns are always
  present).
- **Stats are exact (full data).** Every count, fraction, percentile, mean, and median is computed
  over **all** rows / pairs — nothing is sampled. Percentiles use the legacy linear-interpolation
  helper (`(n−1)·q`).
- These relationships are **strong approximations, not identities** — tool outputs can be
  clipped/compacted before being replayed.
- **Optional raw-Claude debug path (off by default).** `--raw-claude` re-reads reachable raw Claude
  session files (`meta["claude_source_files"]`, collected from each Claude round's `session_file`) to
  recompute the cache-creation metrics directly. This is a debug/fallback source only and is left
  exactly as it was pre-migration; it does not affect the default report.

## Code structure

`analyze.py` is a query→shape→report pipeline over the shared trace DuckDB:

- `load_normalized_rows_from_db(con)` — pulls the round-level columns `ORDER BY ingest_seq`,
  rebuilds each DB tuple into a `raw`-like dict (`_db_row_to_raw` over `_ROUND_COLUMNS`) and feeds it
  to the unchanged helpers (`first_input_event_and_chars`, `int_field`, `optional_int_field`,
  `first_present_int`, `visible_output_tokens`), groups into `rows_by_session` preserving file order,
  stably sorts each session by `round_index`, then walks adjacent (`round_index` step 1) pairs. It
  returns the same `rows`, `pairs`, and `meta` (incl. `meta["claude_source_files"]`) structures the
  report builders consume.
- `build_normalized_claude_cache_detail(...)` — the Claude normalized-cache section (rounds + pairs).
- `summarize_prefix_relationship` / `summarize_adjusted_append` / `summarize_clamped_cases` /
  `summarize_long_current_inputs` / `summarize_long_prior_outputs` — the per-provider report sections,
  unchanged.
- `load_raw_claude(...)` / `extract_raw_claude_rounds(...)` / `summarize_raw_claude(...)` — the
  optional `--raw-claude` debug path that reads raw Claude session files, **untouched**.
- `build_report(...)` / `render_markdown(...)` / `write_outputs(...)` — assemble the report dict,
  render the Markdown tables, and write the JSON + Markdown files, all unchanged.
- `main()` — wires the standard `trace_db` CLI (`--db` | `-i/--input` | `-o/--output-dir`; the legacy
  `--input` / `--output-dir` long names still work) and keeps `--raw-claude` /
  `--max-raw-claude-files`.

The data layer (parsing, surrogate keys, schema) lives in `artifacts/utils/trace_db.py`; see
`artifacts/utils/DB_SCHEMA.md`.

## Running it

```bash
# default merged trace, report written next to this README
uv run python artifacts/prefix_cache/cache_replay/analyze.py

# a specific trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/prefix_cache/cache_replay/analyze.py -i trace/sample.jsonl

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/prefix_cache/cache_replay/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

Useful flags: `--raw-claude` / `--no-raw-claude` (debug-only raw Claude session parsing, default
**off**), `--max-raw-claude-files` (cap raw-Claude files parsed when debugging).

## Outputs

Written to `-o` (default this folder) — a JSON + Markdown report, no figures:

- `cache_replay_analysis.json` — machine-readable metrics: the normalized-trace stats, and per
  provider the `prefix_relationship`, `adjusted_append`, `clamped_cases`, `long_current_inputs`, and
  `long_prior_visible_outputs` sections, plus a `claude_cache_detail` section (from the normalized
  Claude cache fields, or the raw-Claude debug path when `--raw-claude` is set) and a
  `raw_claude_debug` section when `--raw-claude` is set.
- `cache_replay_analysis.md` — the human-readable report: normalized-trace summary, prefix
  relationship, adjusted append, clamped cases, long current inputs, and (when available) the Claude
  cache-creation tables.

There are no figures, so no self-contained PNG sidecars here.

## SyFI result analysis

### cache_replay_analysis.json / cache_replay_analysis.md

The report answers the replay question directly:

- **Prefix continuity holds tightly.** The per-provider prefix-relationship tables show the
  `next.prefix_tokens ≈ prev.input_tokens_total` error concentrated near zero (high within-8-token and
  within-1% shares), and adding the prior output back in shifts the residual — evidence the cached
  prefix is the prior request's full input.
- **Much of each round's "new" append is the replayed prior response.** `adjusted_append` (raw append
  minus prior output) is far smaller than `raw_append`, and the `adjusted_zero` / `signed_negative`
  shares quantify how often subtracting the prior output drives the append to (or below) zero — the
  replay signal. The clamped-cases tables characterize where the subtraction over-shoots (small
  current inputs, short tool/user content).
- **Claude cache writes carry the prior output.** When the normalized Claude cache fields are present,
  the cache-creation section shows the next round's cache write generally meets or exceeds the prior
  round's output (`create ≥ prev output`), consistent with the prior response being written into the
  next round's cache.

Treat the tabulated relationships as strong approximations: tool outputs may be clipped or compacted
before replay, so the residuals are non-zero by design rather than counter-evidence.
