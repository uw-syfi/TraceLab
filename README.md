# TraceLab

TraceLab is an open toolkit and public dataset hub for collecting, sanitizing,
analyzing, and visualizing agent interaction traces.

## Layout

- `scripts/`: the data pipeline only — collection, extraction, and sanitizing.
- `docs/`: cross-cutting methodology notes that are shared by multiple experiments.
- `trace/`: generated normalized JSONL traces from `scripts/collect_llm_traces.py`.
- `artifacts/`: analysis & plotting **experiments**, organized by category, plus shared
  helpers in `artifacts/utils/`. Each experiment is a folder
  `artifacts/<category>/<experiment>/` with one analyze/plot script, a `README.md`
  documenting the question and metric definitions, and (when run) its generated outputs.
  Only the scripts and `README.md` files are tracked in git; generated
  `*.png/*.csv/*.json/*.md` outputs are ignored.
- `validators/`: validation and audit checks, separate from plotting artifacts. Each
  validator lives in `validators/<category>/<validator>/` and is dispatched by
  `validators/run_all.py`.
- `example_sessions/`: small public expanded trace examples with human explanations.
- `example_sessions/derived/`: normalized round-trace example derived from the public examples.

Categories under `artifacts/`: `utils`, `trace_facts`, `llm_generation`, `tool_calls`,
`prefix_cache`, `human_in_the_loop`, `session`.

Validator categories: `human_in_the_loop`, `trace_facts`.

## Pipeline usage

Install dependencies with uv:

```bash
uv sync
```

### Recommended end-to-end run

For a current-user trace that is safe to share or use for public figures:

```bash
# 1. Collect a fresh private normalized trace.
uv run python scripts/collect_llm_traces.py \
  --extract-rounds trace/llm_round_trace.jsonl \
  --fresh-extract

# 2. Sanitize it.
uv run python scripts/sanitize_round_trace.py \
  trace/llm_round_trace.jsonl \
  -o trace/llm_round_trace.public.jsonl

# 3. Regenerate all analysis artifacts.
uv run python artifacts/run_all.py \
  --input trace/llm_round_trace.public.jsonl

# 4. Run validation/audit checks.
uv run python validators/run_all.py \
  --input trace/llm_round_trace.public.jsonl
```

`artifacts/run_all.py` also derives the timing-fit CSV locally before timing analyses run,
so a normal full run does not need a separate timing preprocessing step.

Extract a normalized combined Claude/Codex round trace from the launching user's home to
`trace/llm_round_trace.jsonl`:

```bash
uv run python scripts/collect_llm_traces.py --extract-rounds
```

Scan every user home under `/home` instead:

```bash
uv run python scripts/collect_llm_traces.py --all-user --extract-rounds
```

For a sudo-backed all-user collection that keeps final outputs owned by the launching user:

```bash
scripts/collect_all_users_sudo.sh --sanitize
```

Sanitize a normalized trace before sharing:

```bash
uv run python scripts/sanitize_round_trace.py trace/llm_round_trace.jsonl -o trace/llm_round_trace.public.jsonl
```

The sanitizer rewrites session, round, turn, tool-call, project, and user identifiers with
stable pseudorandom replacements. It removes local context fields such as `home`, `cwd`,
`workdir`, `session_file`, and path-like keys, and it drops `tools[].input` entirely while
preserving `input_chars`. Distinct-user counts remain available through pseudonymous
`user` values.

## Public Trace Download

The sanitized all-user trace and prebuilt DuckDB database are distributed as GitHub
Release assets, not committed into Git history:

- JSONL file: `syfi_coding_trace.jsonl.gz`
- DuckDB file: `syfi_coding_trace.duckdb`
- Release tag: `v2026-06-08-syfi-trace`
- Rows: `357,161`
- Tool records: `432,510`
- Distinct pseudonymous users: `43`
- Providers: `claude=140,338`, `codex=216,823`
- JSONL SHA256: `9d265eae69a31cae203848bea936f018148eed7ca8bf56050c5abe96da0b4e6b`
- DuckDB SHA256: `5d2ef12486cdfc26d770e8432fb45fd17381377be739a4d8e5b8556587721507`

Download the pinned release assets:

```bash
mkdir -p trace
curl -L --fail \
  -o trace/syfi_coding_trace.jsonl.gz \
  https://github.com/uw-syfi/TraceLab/releases/download/v2026-06-08-syfi-trace/syfi_coding_trace.jsonl.gz
curl -L --fail \
  -o trace/syfi_coding_trace.duckdb \
  https://github.com/uw-syfi/TraceLab/releases/download/v2026-06-08-syfi-trace/syfi_coding_trace.duckdb
echo "9d265eae69a31cae203848bea936f018148eed7ca8bf56050c5abe96da0b4e6b  trace/syfi_coding_trace.jsonl.gz" \
  | sha256sum -c -
echo "5d2ef12486cdfc26d770e8432fb45fd17381377be739a4d8e5b8556587721507  trace/syfi_coding_trace.duckdb" \
  | sha256sum -c -
gzip -t trace/syfi_coding_trace.jsonl.gz
```

To always fetch the newest published public trace assets, use:

```bash
curl -L --fail \
  -o trace/syfi_coding_trace.jsonl.gz \
  https://github.com/uw-syfi/TraceLab/releases/latest/download/syfi_coding_trace.jsonl.gz
curl -L --fail \
  -o trace/syfi_coding_trace.duckdb \
  https://github.com/uw-syfi/TraceLab/releases/latest/download/syfi_coding_trace.duckdb
```

Decompress when a JSONL input is needed:

```bash
gzip -dk trace/syfi_coding_trace.jsonl.gz
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/syfi_coding_trace.jsonl
```

Pipeline scripts:

- `collect_llm_traces.py`: scan Claude/Codex local history, count sessions, and optionally
  write normalized round traces.
- `collect_all_users_sudo.sh`: sudo-friendly wrapper for all-user extraction.
- `extract_claude_rounds.py` / `extract_codex_rounds.py`: convert provider JSONL sessions
  into normalized round rows.
- `sanitize_round_trace.py`: remove public-release-sensitive fields.
- `find_representative_session_segments.py`: find compact raw-session windows for examples.

## Analysis experiments

Every analysis is a self-contained experiment under `artifacts/<category>/<experiment>/`.
Read that folder's `README.md` for the question it answers and exactly how it computes its
metric; the shared metric definitions are collected in `artifacts/utils/README.md`. Run an
experiment directly — it defaults its input to
`trace/llm_round_trace.merged.all_users.jsonl` and writes outputs into its own folder:

```bash
# headline aggregate facts (text or --json)
uv run python artifacts/trace_facts/overview_summary/analyze.py
# input token composition; tool latency; generation-time CDFs
uv run python artifacts/llm_generation/prefix_append_distribution/plot.py
uv run python artifacts/tool_calls/tool_latency_distribution/plot.py
uv run python artifacts/llm_generation/generation_time_cdf/plot.py
# multi-round CSV export
uv run python artifacts/trace_facts/csv_export/convert.py \
  -i trace/llm_round_trace.public.jsonl \
  -o artifacts/trace_facts/csv_export/coding_trace.csv
```

Each figure driver owns its own plotting/CSV payload and imports shared primitives from
the cohesive `artifacts/utils/` modules (`trace_loader`, `style`, `accumulators`,
`formatters`, `tool_stats`, `cdf`) split out of the former monolithic
`plot_trace_stats.py`. Common loader options: `--group-by`, `--sample-size`,
`--per-tool-sample-size`, `--min-tool-calls-for-plot`, `--seed`.

The timing-fit family owns its derived timing-segment CSV locally. `artifacts/run_all.py`
builds `artifacts/llm_generation/timing_fit/timing_fit_trace.csv` from the selected JSONL
trace before running timing analyses. Use `--timing-input` only when you intentionally
want to consume an existing external timing CSV instead of deriving one from `--input`.
To build the local timing CSV directly:

```bash
uv run python artifacts/llm_generation/timing_fit/collect_timing_fit_trace.py \
  -i trace/llm_round_trace.jsonl
```

### Self-contained figures

As the final step of every plotting experiment, each PNG embeds its README, the source CSV
data, and the plotting code as compressed PNG text chunks (CSVs are still written normally).
Inspect or unpack any figure with the helper:

```bash
python artifacts/utils/png_sidecar.py list    <figure>.png
python artifacts/utils/png_sidecar.py extract <figure>.png -o ./unpacked
```

## Validators

Validators are integrity checks and denominator/formula audits. They write Markdown/CSV
reports next to the validator, under `validators/`, and are intentionally kept out of
the plotting artifact tree.

```bash
uv run python validators/run_all.py
uv run python validators/run_all.py --list
uv run python validators/run_all.py --only human_in_the_loop
uv run python validators/run_all.py --only trace_facts/tool_duplicate_audit
uv run python validators/run_all.py --input trace/llm_round_trace.public.jsonl
```

## Normalized Rows

Each extracted JSONL row is one LLM invocation. The current format keeps token-accounting
fields, an ordered timing list, and nested tool metadata:

- Top-level fields include provider/session ids, model, input/output token counts, cache-prefix
  split, source store, `timing_events`, and `trace_key`.
- `timing_events[]` is the ordered trace-observed event list for the round. It may include
  `user_message`, `tool_result`, `reasoning`, `text`, `tool_call`, and Codex `usage_report`
  entries.
- Private extracted traces include serialized tool `input`; sanitized public traces remove
  `tools[].input` and keep only `input_chars`.
- `tools[]` includes `tool_name`, `tool_call_id`, `emitted_at`, `input_chars`, `result_chars`,
  `tool_wall_latency_ms`, `tool_internal_latency_ms`, `is_error`, and `result_at`.
- Tool result content is summarized by `result_chars`; full tool outputs are not stored in the normalized row.

Token fields are normalized as:

```text
input_tokens_total = prefix_tokens + newly_append_tokens
```

`claude_cache_creation_input_tokens` is emitted after `newly_append_tokens`. For Claude
rows it is copied from `usage.cache_creation_input_tokens`; for Codex rows it is
`null`. `newly_append_tokens` still includes both Claude uncached `input_tokens`
and Claude cache-write tokens.

Tool latency is split into two fields:

- `tool_wall_latency_ms`: trace-observed wall latency, computed as `result_at - emitted_at`.
- `tool_internal_latency_ms`: tool/runner-reported duration when available, such as Codex
  wrapper `Wall time` or Claude `durationMs` / `durationSeconds`; otherwise `null`.

The analysis experiments use `tool_internal_latency_ms` when present, then fall back to
`tool_wall_latency_ms`. The CSV exporter uses `tool_wall_latency_ms` for `tool_wait_after_ms`
by default.

For LLM-side latency, use `timing_events[]` rather than a first/last timestamp pair. The
usual proxy for "input ready -> next tool input" is the latest `user_message` or `tool_result`
event before the first `tool_call`, subtracted from that `tool_call` timestamp.

See `docs/prompt_cache_accounting.md` for the full prompt-cache accounting derivation, and
`artifacts/utils/README.md` for the single-source-of-truth metric definitions
(effective tool latency, observable generation time, human input wait, user-turn response
time, prefix hit ratio, adjusted append, KV active ratio, growth buckets).
