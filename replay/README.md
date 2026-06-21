# Session Runner

`session_runner` is a session-aware closed-loop workload runner for vLLM. It replays trace-derived sessions as ordered chains of rounds instead of independent requests:

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

A small example lives at `examples/session_workload_example.csv`.

## Text Corpus (`--text-file`)

The runner only needs the *token shape* of text, not its meaning, so any large UTF-8 text file works: your own code/logs, a Project Gutenberg book, a Wikipedia dump, etc. Only about `--token-pool-limit` tokens are consumed (default: `200000`, roughly 1 MB of text), so a small file is enough; the rest of a large file is never read.

A convenient, widely used option is **enwik9**: the first 10^9 bytes of English Wikipedia from the Large Text Compression Benchmark. It is **not bundled** with this repository. Since enwik9 is derived from Wikipedia content, users should download it from the original source and comply with the applicable license terms.

```bash
curl -O http://mattmahoney.net/dc/enwik9.zip
unzip enwik9.zip   # -> ./enwik9 (~1 GB; only the first ~1 MB is tokenized)
```

Then pass `--text-file ./enwik9`. Any other sufficiently large UTF-8 text file works just as well.

## Request Path

The runner targets vLLM's OpenAI-compatible completions endpoint:

```text
POST {base_url}/completions
```

Pass `--base-url http://HOST:PORT/v1`. The runner constructs exact prompt token ids internally, then decodes them to text before sending the request. Direct token-id HTTP submission is not implemented because the supported vLLM HTTP contract depends on server mode and version.

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

# Honor arrival_time by default; use this to start all sessions immediately.
--ignore-arrival-time

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

To collect server-reported cached prompt tokens from the Qwen helper script, start vLLM with prompt-token details enabled:

```bash
ENABLE_PROMPT_TOKENS_DETAILS=1 web/ai_infra/serve_qwen36_35b_a3b_fp8_vllm.sh
```

Then run the runner with:

```bash
--assume-missing-cache-details-zero
```

Use that flag only when the server is launched with `--enable-prompt-tokens-details`; otherwise missing cache details mean "not reported", not necessarily zero cached tokens.

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
- synthetic output-token context update
- vLLM streaming completions request
- TTFT and total latency logging
- JSON run summary output
- JSONL per-round output
- planned vs. server-reported prefix cache hit-rate logging

Not implemented yet:

- direct token-id HTTP submission to vLLM
- TTFT/TPOT SLO judgment
- per-token timeline dump
- raw trace prompt/tool-result text reconstruction
- block-level Prometheus prefix-cache metric collection
