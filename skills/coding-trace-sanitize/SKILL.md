---
name: coding-trace-sanitize
description: "Sanitize normalized coding-trace JSONL rows for public sharing. Use when preparing trace/llm_round_trace*.jsonl outputs for release, removing local paths, pseudonymizing user identifiers, dropping tools[].input, replacing session/round/tool/project identifiers with stable pseudorandom values, choosing deterministic versus random sanitizer seeds, or checking what sensitive fields remain after sanitize_round_trace.py."
---

# Coding Trace Sanitize

## Overview

Use this skill to prepare normalized round traces for sharing. The sanitizer targets the normalized JSONL schema, not arbitrary raw Claude/Codex logs.

## First Steps

1. Work from the coding-trace repo root, identified by `pyproject.toml`, `README.md`, and `scripts/sanitize_round_trace.py`.
2. Confirm the input is a normalized round trace such as `trace/llm_round_trace.jsonl`, not an expanded raw example.
3. If starting from local histories, run `$coding-trace-collect` first to produce normalized rows.
4. The default output is stdout; pass `-o/--output` for files. Use deterministic seeds when comparing versions; use `--random-seed` when producing a one-off public artifact and stable cross-run ids are not needed.

## Main Commands

Sanitize the default private trace to a public output:

```bash
uv run python scripts/sanitize_round_trace.py trace/llm_round_trace.jsonl -o trace/llm_round_trace.public.jsonl
```

Use a named deterministic seed:

```bash
uv run python scripts/sanitize_round_trace.py trace/llm_round_trace.jsonl -o trace/share.jsonl --seed my-release-v1
```

Use a fresh random seed:

```bash
uv run python scripts/sanitize_round_trace.py trace/llm_round_trace.jsonl -o trace/share.jsonl --random-seed
```

The all-user sudo wrapper can sanitize immediately after collection:

```bash
scripts/collect_all_users_sudo.sh --sanitize
```

After sanitizing, use the sanitized output for public analysis and validators:

```bash
uv run python artifacts/run_all.py --input trace/llm_round_trace.public.jsonl
uv run python validators/run_all.py --input trace/llm_round_trace.public.jsonl
```

## What The Sanitizer Does

`scripts/sanitize_round_trace.py` performs these transformations:

- Replaces `project`, `session_id`, `turn_id`, `round_id`, `tool_call_id`, and `trace_key` with stable pseudorandom alternatives while preserving relationships inside the output.
- Replaces `user`, `user_name`, and `username` values with stable pseudonyms while preserving distinct-user counts and grouping.
- Removes sensitive local context keys such as `cwd`, `home`, `host`, `hostname`, `file_path`, `filepath`, `repo_url`, `repository_url`, `session_file`, and `workdir`.
- Removes keys ending in path-like forms such as `_path`, `_filepath`, or `filepath`.
- Drops `tools[].input` entirely.
- Preserves `tools[].input_chars`, `result_chars`, timing, model, provider, token counts, event types, and aggregate structure.
- Rewrites `timing_events[].tool_call_id` consistently with the corresponding tool id.
- Prints a short `rows=... tools=... output=... seed=...` status line to stderr.

## What It Does Not Guarantee

Do not claim the sanitizer proves a trace is safe for every release context. It does not inspect semantic leakage in model names, visible text summaries, tool names, timing patterns, token counts, or result sizes. It also does not sanitize arbitrary raw provider logs.

If the user wants to share raw examples, inspect or create public raw examples separately using `$coding-trace-raw`; do not run the normalized sanitizer on expanded raw text and assume it worked.

## Verification

After sanitizing, run a shape summary:

```bash
uv run python artifacts/trace_facts/overview_summary/analyze.py -i trace/llm_round_trace.public.jsonl --json
```

Use targeted searches for obvious leftovers:

```bash
rg -n '"(cwd|home|session_file|workdir)"' trace/llm_round_trace.public.jsonl
```

```bash
rg -n '"input":' trace/llm_round_trace.public.jsonl
```

Empty search output is a useful smoke test, not a complete privacy audit. `user` fields
are expected to remain, but their values should look like `user_<hex>`. When reporting
results, state the sanitizer's exact scope and any remaining review risk.

For an end-to-end smoke test, run the artifact dispatcher and validator dispatcher on the
sanitized file. The artifact dispatcher will derive the local timing-fit CSV from the same
sanitized JSONL trace before timing analyses consume it.
