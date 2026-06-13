---
name: coding-trace-collect
description: "Collect, count, and extract Claude Code and Codex CLI local histories into normalized coding-trace JSONL files. Use when running collect_llm_traces.py, extract_claude_rounds.py, extract_codex_rounds.py, collect_all_users_sudo.sh, scanning current-user or all-user homes, choosing output paths, using --fresh-extract or --append-dedup, or troubleshooting extraction counts."
---

# Coding Trace Collect

## Overview

Use this skill to collect local Claude Code and Codex history stores and write normalized round traces. Collection finds source files and counts sessions; extraction converts provider-specific histories into JSONL rows where each line is one LLM invocation.

## First Steps

1. Work from the coding-trace repo root, identified by `pyproject.toml`, `README.md`, and `scripts/collect_llm_traces.py`.
2. Use `uv run python ...` when dependencies or the repo virtualenv matter. Plain `python ...` is enough for standard-library-only extraction helpers.
3. Decide scan scope before running commands:
   - Current user: default behavior.
   - All users under `/home`: use `--all-user`, or the sudo wrapper when unreadable homes are expected. Use `--home-root PATH` for nonstandard home roots.
4. Decide write behavior:
   - Default extraction appends deduped rows.
   - `--fresh-extract` removes selected output files first.
5. Do not publish private outputs until `$coding-trace-sanitize` has been applied.
6. After sanitization, use `$coding-trace-analyze` for artifact and validator dispatchers. Collection should not own plotting outputs or timing-fit CSVs.

## Main Collection Commands

Count current-user Claude and Codex history without extraction:

```bash
uv run python scripts/collect_llm_traces.py
```

Extract a combined current-user trace:

```bash
uv run python scripts/collect_llm_traces.py --extract-rounds
```

Write to a specific file and start fresh. This is the recommended current-user command
for an end-to-end run:

```bash
uv run python scripts/collect_llm_traces.py --extract-rounds trace/llm_round_trace.jsonl --fresh-extract
```

Scan all homes under `/home` without sudo:

```bash
uv run python scripts/collect_llm_traces.py --all-user --extract-rounds
```

Use the sudo wrapper for all-user collection while preserving final file ownership:

```bash
scripts/collect_all_users_sudo.sh --sanitize
```

The wrapper writes `trace/llm_round_trace.all_users.jsonl`, a sibling `.collection_report.json`, and with `--sanitize` a sibling `.public.jsonl`. It prints an overview summary by default; add `--no-summary` for quiet batch runs. Add `--quiet-progress` only to suppress collector progress messages.

For a private-to-public current-user flow:

```bash
uv run python scripts/collect_llm_traces.py \
  --extract-rounds trace/llm_round_trace.jsonl \
  --fresh-extract

uv run python scripts/sanitize_round_trace.py \
  trace/llm_round_trace.jsonl \
  -o trace/llm_round_trace.public.jsonl
```

## Timing Fit CSV

The long-form timing-segment CSV is now owned by the timing-fit artifact, not the
collection pipeline. `artifacts/run_all.py --input <trace.jsonl>` builds it
automatically before timing analyses. Do not precompute it during normal collection.
To build it directly for a timing-only manual run:

```bash
uv run python artifacts/llm_generation/timing_fit/collect_timing_fit_trace.py \
  -i trace/llm_round_trace.all_users.public.jsonl \
  -o artifacts/llm_generation/timing_fit/timing_fit_trace.csv
```

The output has one timing segment per row. Claude segments use message-level output accounting; Codex can also emit reasoning-split segments when reasoning markers and exact reasoning-token counts are available.

## Output Paths

Default output names:

- Current-user combined trace: `trace/llm_round_trace.jsonl`.
- Current-user Claude-only trace: `trace/claude_round_trace.jsonl`.
- Current-user Codex-only trace: `trace/codex_round_trace.jsonl`.
- All-user combined trace: `trace/llm_round_trace.all_users.jsonl`.
- Sudo-wrapper collection report: `trace/llm_round_trace.all_users.collection_report.json`.
- Sudo-wrapper sanitized output with `--sanitize`: `trace/llm_round_trace.all_users.public.jsonl`.
- Timing-fit artifact CSV: `artifacts/llm_generation/timing_fit/timing_fit_trace.csv`.

Use `--trace-dir DIR` to change the default extraction directory for omitted output
paths. Passing an explicit path to `--extract-rounds`, `--extract-claude-rounds`, or
`--extract-codex-rounds` overrides both the built-in default and `--trace-dir`.

## Provider-Specific Extraction

Use direct extractors when the user already has a provider source directory:

```bash
python scripts/extract_claude_rounds.py ~/.claude/projects/PROJECT_DIR -o trace/claude_round_trace.jsonl --append-dedup
```

```bash
python scripts/extract_codex_rounds.py ~/.codex/sessions -o trace/codex_round_trace.jsonl --append-dedup
```

Prefer `collect_llm_traces.py` for normal use because it scans both providers, handles `.claude.back`, tracks skipped paths, and combines extraction stats.

## Important Options

- `--json`: emit a machine-readable collection report.
- `--no-claude-back`: skip `.claude.back/projects`.
- `--quiet-host-progress`: collector option that suppresses progress messages during host scanning and extraction.
- `--quiet-progress`: sudo-wrapper option that passes the collector quiet mode.
- `--no-summary`: sudo-wrapper option that skips the post-collection overview summary.
- `--no-sudo`: sudo-wrapper option that runs all-user collection without sudo.
- `--extract-project-filter TEXT`: only extract Claude projects whose directory name contains `TEXT`; repeat the option for multiple filters.
- `--fresh-extract`: remove selected extraction outputs before writing.
- `--append-dedup`: direct extractor option that appends only unseen `trace_key` rows.

## Validation After Collection

After extraction, check basic shape before analysis:

```bash
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.jsonl
```

For JSON output:

```bash
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.jsonl --json
```

If outputs are unexpectedly small, inspect the collection report for `skipped_paths`, source counts, `candidate_rounds`, `written_rounds`, and `skipped_duplicate_rounds`.
