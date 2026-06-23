# Session Runner

`session_runner` is a session-aware closed-loop workload runner for OpenAI-compatible inference servers (vLLM today, SGLang and others via the pluggable backend). It replays trace-derived sessions as ordered chains of rounds instead of independent requests:

```text
send round i -> wait for full LLM response -> sleep tool_wait_after_ms -> send round i+1
```

The runner preserves the serving-side shape of a coding-agent trace: `prefix_len`, appended input length, target decode length, session order, session arrival, and tool/user waits. It uses a synthetic text corpus to construct prompt content, so it does not replay raw private prompts.

## Input CSV

Supported headers:

```csv
session_id,round_idx,prefix_len,input_len,output_len,tool_wait_after_ms
```

It also supports the canonical simulator form emitted by `artifacts/trace_facts/csv_export`:

```csv
id,input_len,output_len,arrival_time,round_idx,tool_wait_after_ms,prefix_len
```

Fields:

- `session_id` / `id`: session identifier.
- `arrival_time`: synthetic session arrival time in milliseconds. Missing values default to `0`.
- `prefix_len`: number of prior context tokens kept for this round.
- `input_len`: number of new synthetic input tokens appended for this round.
- `output_len`: `max_tokens` sent to vLLM.
- `tool_wait_after_ms`: sleep after this round completes before the next round in the same session.

Examples live at `examples/session_workload_example.csv` (single session),
`examples/multi_session_example.csv` (3 sessions with arrival times, for a quick multi-session
run), and `examples/multi_session_large.csv` (48 sessions / 303 rounds with cumulative-consistent
prefixes, for an end-to-end prefix-cache hit-rate measurement). In the large example each round's
`prefix_len` equals the prior round's full context, so the planned hit rate is the true achievable
rate and the server-measured aggregate matches it within vLLM's 16-token block alignment.

## Text Corpus (`--text-file`)

The runner only needs the *token shape* of text, not its meaning, so any large UTF-8 text file works: your own code/logs, a Project Gutenberg book, a Wikipedia dump, etc. By default the pool auto-sizes to the workload — large enough that no single request repeats content and every session gets a distinct content window — with a floor of `100M` tokens (~400 MB of `u32`, ~400–600 MB of source text). Override with `--token-pool-limit`. The corpus must therefore supply at least that many tokens; the rest of a larger file is never read. If the resulting pool is still shorter than the longest prompt, the runner warns that synthetic content will repeat.

A convenient, widely used option is **enwik9**: the first 10^9 bytes of English Wikipedia from the Large Text Compression Benchmark. It is **not bundled** with this repository. Since enwik9 is derived from Wikipedia content, users should download it from the original source and comply with the applicable license terms.

```bash
curl -O http://mattmahoney.net/dc/enwik9.zip
unzip enwik9.zip   # -> ./enwik9 (~1 GB; tokenized up to the pool size, ~250M tokens available)
```

Then pass `--text-file ./enwik9`. Any other sufficiently large UTF-8 text file works just as well. For million-token sessions, prefer a large corpus like enwik9 so the pool can reach its full size.

## Request Path

The runner targets an OpenAI-compatible completions endpoint:

```text
POST {base_url}/completions
```

Pass `--base-url http://HOST:PORT/v1`. The wire protocol is selected with `--backend` (default `openai`, which covers vLLM and SGLang's OpenAI endpoint); the endpoint path, request body, and response parsing all live behind a `Backend` adapter in `src/backend.rs`, so adding a server (e.g. SGLang's native `/generate`) is a new adapter, not a rewrite. The runner submits the exact prompt **token ids** directly (OpenAI's `prompt` accepts an integer array), so there is no client-side decode and the server's prefix-cache keys match the ids we built. With recent vLLM it also sets `return_token_ids` to carry the model's exact output tokens forward across rounds; servers that ignore the flag fall back to re-encoding the output text (a few tokens of drift).

## Build

```bash
cargo build --release --manifest-path replay/Cargo.toml --bin session_runner
```

## Dry Run

Dry-run mode validates and summarizes the CSV without contacting vLLM:

```bash
cargo run --manifest-path replay/Cargo.toml --bin session_runner -- \
  --trace replay/examples/session_workload_example.csv \
  --text-file /path/to/text-corpus \
  --tokenizer /path/to/tokenizer.json \
  --model qwen3.6-35b-a3b-fp8 \
  --dry-run \
  --max-model-len 65536
```

`--text-file` and `--tokenizer` are still required by the CLI, but dry-run mode returns before loading them.

## Run Against vLLM

```bash
cargo run --release --manifest-path replay/Cargo.toml --bin session_runner -- \
  --trace replay/examples/session_workload_example.csv \
  --text-file /path/to/text-corpus \
  --tokenizer /path/to/tokenizer.json \
  --model qwen3.6-35b-a3b-fp8 \
  --base-url http://127.0.0.1:60995/v1 \
  --stream-idle-timeout-secs 7200 \
  --max-model-len 65536 \
  --max-active-sessions 1 \
  --summary-path /tmp/session_runner_summary.json \
  --log-path /tmp/session_runner.jsonl
```

Useful controls:

```bash
# Validate against a model context limit and report the first overflowing round.
--dry-run --max-model-len 131072

# Bound active closed-loop sessions while still respecting arrival_time.
--max-active-sessions 128

# Skip rounds that exceed a known model context limit instead of sending them to vLLM.
--max-model-len 131072 --fail-on-context-overflow

# Write one JSON summary containing workload stats and replay latency stats.
--summary-path /tmp/session_runner_summary.json
```

## Prefix-Cache Accounting

The JSONL log includes per-round planned-vs-server cache fields:

- `planned_prefix_hit_rate`: `prefix_len / (prefix_len + input_len)` from the workload.
- `server_cached_prompt_tokens`: cached prompt tokens reported by vLLM usage, when available.
- `server_prefix_hit_rate`: `server_cached_prompt_tokens / server_prompt_tokens`, when available.
- `server_prefix_hit_rate_delta`: server hit rate minus planned hit rate for that round.

The runner always requests streaming usage and treats usage-present-but-cache-detail-absent as zero cached tokens (servers omit `prompt_tokens_details` when nothing was cached). For this to be meaningful, the server must report prompt-token details and have prefix caching enabled. With the Qwen helper script, start vLLM with both:

```bash
ENABLE_PROMPT_TOKENS_DETAILS=1 ENABLE_PREFIX_CACHING=1 web/ai_infra/serve_qwen36_35b_a3b_fp8_vllm.sh
```

Before replaying, the runner sends a two-request probe that forces a guaranteed prefix-cache hit and **aborts the run** if the server does not report cached prompt tokens. This fails fast on a server launched without prompt-token details (vLLM: `--enable-prompt-tokens-details`) or without prefix caching, instead of silently logging 0% hit rates. Dry-run mode skips the probe.

If the Qwen model is not already present locally, starting vLLM may download a large Hugging Face model and may execute model repository code depending on the serve flags.

## Current Scope

Implemented:

- session trace parsing
- both `session_id` and canonical `id` CSV schemas
- workload summary and dry-run validation
- per-session ordered replay
- optional session-start scheduling from `arrival_time`
- optional active-session concurrency limit
- optional model-context validation and overflow skipping
- session-internal closed-loop timing
- `prefix_len + input_len` prompt construction
- direct token-id prompt submission (no client-side decode) + exact output-id carry-forward via vLLM `return_token_ids` (re-encode fallback)
- pluggable backend adapter (OpenAI-compatible today; vLLM and SGLang OpenAI endpoints)
- OpenAI-compatible streaming completions request
- startup prefix-cache preflight that aborts when the server reports no cached tokens
- TTFT and total latency logging
- JSON run summary output
- JSONL per-round output
- planned vs. server-reported prefix cache hit-rate logging

Not implemented yet:

- SGLang native `/generate` backend adapter
- TTFT/TPOT SLO judgment
- per-token timeline dump
- raw trace prompt/tool-result text reconstruction
- block-level Prometheus prefix-cache metric collection
