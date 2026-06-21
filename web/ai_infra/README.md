# SYFI QA AI Infra

This folder contains the experimental E2B/OpenRouter plumbing for the public SYFI trace QA
assistant.

## Build The E2B Template

Build once so request-time sandboxes do not upload the DuckDB file:

```bash
source ~/.bashrc
E2B_API_KEY="$E2B_KEY" \
  .venv/bin/python web/ai_infra/build_syfi_e2b_template.py \
  --name syfi-qa-code-interpreter:latest
```

The builder:

- starts from `code-interpreter-v1`
- installs `duckdb` and `matplotlib`
- copies `trace/syfi_coding_trace.duckdb` to `/data/syfi_coding_trace.duckdb`
- verifies the DB during template build
- defaults to a small runtime shape: `1` CPU and `2048` MB RAM. The prompt sets DuckDB
  `threads=1` and `memory_limit='768MB'`, so this should be enough for normal aggregate queries and
  small plots. Use `--memory-mb 4096` only if tests show OOMs for heavier plotting or joins.

Current built template:

- name: `syfi-qa-code-interpreter:latest`
- template id: `99uyki46cnrm95uz6mii`
- build id: `7d42fdee-d195-4839-adc9-19527614dc3d`
- build verification: `select count(*) from rounds` returned `357161`

## Test The Template

Create a sandbox from the template and run a real query/plot:

```bash
source ~/.bashrc
E2B_API_KEY="$E2B_KEY" \
  .venv/bin/python web/ai_infra/test_syfi_e2b_template.py \
  --template syfi-qa-code-interpreter:latest
```

## Test OpenRouter Tool Calling

The canonical system prompt lives in `syfi_qa_system_prompt.md`. `syfi_llm_runtime.py` loads it by
default and substitutes the remote DB/artifact paths. Use `--prompt-file path/to/prompt.md` for
prompt experiments without editing the runner.

This checks only model tool-call shape, with a canned tool result:

```bash
source ~/.bashrc
OPENROUTER_API_KEY="$OPENROUTE_KEY" \
  .venv/bin/python web/ai_infra/syfi_qa_smoke.py --openrouter
```

Full OpenRouter-to-E2B test using the prebuilt template:

```bash
source ~/.bashrc
E2B_API_KEY="$E2B_KEY" OPENROUTER_API_KEY="$OPENROUTE_KEY" \
  .venv/bin/python web/ai_infra/syfi_llm_runtime.py \
  --template syfi-qa-code-interpreter:latest
```

Use `syfi_qa_smoke.py` for lower-level diagnostics. Use `syfi_llm_runtime.py` for the maintained
full-stack path: model tool call -> E2B execution -> model final answer. Do not use `--upload-db`
for normal testing; it exists only as a diagnostic fallback in the smoke script.

## Browser Tester

For the full website with both API backends and the frontend proxy, use the web runbook:
[`../README.md`](../README.md). The tester below is only the standalone AI sidecar page.

Run a local multi-turn HTML tester. The sidecar is a FastAPI + uvicorn app; install the extra once
with `uv sync --extra ai`:

```bash
source ~/.bashrc
E2B_API_KEY="$E2B_KEY" OPENROUTER_API_KEY="$OPENROUTE_KEY" \
  uv run --extra ai python web/ai_infra/app.py --port 60980
```

Open `http://127.0.0.1:60980` (the AI backend port; see `config/services.json`). The tester stores conversation history in browser `localStorage` and
carries each turn over a single WebSocket (`/api/chat/ws`): the server runs the model -> tool loop
and streams events back. The model -> tool loop is identical for both data sources; only where
`run_python` runs differs:

- **Public SYFI trace** — `run_python` runs server-side in an E2B sandbox over the baked-in DuckDB
  (needs `E2B_API_KEY`); a small in-process sandbox pool keeps a warm sandbox per session.
- **Uploaded trace in browser** — the server emits `tool_request` frames; the page runs the code in
  the Pyodide QA worker over the *local* trace and replies with `tool_result`. Only the generated
  code and aggregated results cross the socket — never raw trace rows. No E2B needed.

Inline E2B/Pyodide PNG results and image artifacts saved under `/out` are displayed in the chat.

Tester display defaults:

- Final assistant answers are shown in the main chat.
- Tool code/stdout/stderr/artifacts are folded under the assistant turn that produced them.
- Intermediate model/tool events are hidden unless `show intermediate events` is enabled.
- Events always stream over the WebSocket as the loop runs; there is no separate streaming toggle.
- Model calls default to `8192` max output tokens.
- If a model turn ends with `finish_reason=length` / `max_tokens`, the runtime retries that
  generation up to 3 times before accepting the truncated response.
- The tester never displays provider-hidden reasoning fields. If a local model emits visible thinking
  text, intermediate mode shows it alongside sandbox creation, model tool-call requests, generated
  code, tool results, and finalization.

Image display policy:

- If E2B returns image artifacts under `/out` with an image MIME type, the runtime downloads them
  before killing the sandbox and returns browser-ready data URLs.
- If there are no image artifacts but E2B returns an inline PNG result from `plt.show()`, the
  runtime returns that inline image.
- If both exist, artifact images win, so the tester does not show duplicate copies of the same plot.
- The LLM receives only compact artifact metadata, not base64 image payloads.

This follows the same broad strategy as `coding_trace_upload`: keep the site/frontend mostly static
and put dynamic behavior behind a small `/api/*` sidecar.

## Choosing the LLM Backend (self-hosted vLLM vs OpenRouter)

The active backend is selected in **`config/services.json`** under `llm.provider` — `"vllm"` (local
or remote self-hosted deployment, the default) or `"openrouter"` — with a per-provider `base_url` + `model`. The runtime
reads the selected block, so `just ai-serve` / the CLI / the tester all use it with no env needed:

```jsonc
"llm": {
  "provider": "vllm",                                  // or "openrouter"
  "fallback": "openrouter",                            // optional: used when the primary is unreachable
  "vllm":       { "base_url": "http://127.0.0.1:60995/v1", "model": "qwen3.6-35b-a3b-fp8" },
  "openrouter": {
    "base_url": "https://openrouter.ai/api/v1",
    "model": "qwen/qwen3.6-35b-a3b",
    "provider_routing": { "order": ["atlas-cloud/fp8"], "allow_fallbacks": true }  // OpenRouter-only
  }
}
```

### Provider Failover (primary → fallback)

When `llm.fallback` names a second provider, each turn first probes the primary's `/models`
endpoint and caches the verdict process-wide, so only the first turn in each cache window pays for
the probe — every other turn (across all sessions) is zero-overhead. On a connection error or a 5xx
the runtime transparently routes that turn to the **fallback provider's own backend** — e.g.
self-hosted vLLM → OpenRouter — using the fallback's own `base_url`, `model`, key, and
`provider_routing`. A 4xx (incl. 401/404) counts as "up", so we never fail over on an auth quirk.
The fallback block is resolved purely from its config (the flat `SYFI_LLM_*` overrides, which target
the primary, do not bleed across). The loop emits `llm_probe`, `llm_failover`, and `llm_target`
events so the tester shows which backend served the turn.

The only real cost is a *cold* probe when the primary is **unreachable**: a host that drops packets
makes the probe block for the full timeout (a host that refuses the connection returns instantly).
To keep that tax small and rare, the timeout is short and "down" verdicts are cached longer than
"up" ones, so a down primary is re-probed infrequently (the trade-off is up to `*_TTL_DOWN` seconds
before a recovered primary is picked back up). Tuning knobs:

- `SYFI_LLM_PROBE_TIMEOUT` — per-probe timeout (default `2`s). A healthy `/models` answers in well
  under this; lower it for a snappier cold failover, raise it if a loaded primary false-negatives.
- `SYFI_LLM_PROBE_TTL` — cache window for an **up** primary (default `30`s).
- `SYFI_LLM_PROBE_TTL_DOWN` — cache window for a **down** primary (default `90`s).

`provider_routing` (OpenRouter only) is passed through as the request `provider` field. Order
entries may carry a quantization suffix (`atlas-cloud/fp8`); `allow_fallbacks: true` lets OpenRouter
pick another provider when the preferred one is down. Setting `SYFI_LLM_PROVIDER` pins one provider
and disables failover.

Env still overrides config (precedence: env > config > fallback):

- `SYFI_LLM_PROVIDER=openrouter` flips the provider for one run.
- `SYFI_LLM_BASE_URL` / `SYFI_LLM_MODEL` override the URL/model directly.
- API keys are never stored in config. Set `OPENROUTER_API_KEY` for OpenRouter or
  `SYFI_LLM_API_KEY` for a protected vLLM endpoint.

So a typical public-trace run against the configured vLLM endpoint is `E2B_API_KEY=$E2B_KEY just ai-serve` after
`SYFI_LLM_API_KEY` is exported in the shell. Switching to OpenRouter is either editing
`llm.provider` or `SYFI_LLM_PROVIDER=openrouter OPENROUTER_API_KEY=$OPENROUTE_KEY just ai-serve`.

## vLLM Backend

Serve the Qwen model (port + served-model name come from `config/services.json`):

```bash
DETACH=1 web/ai_infra/serve_qwen36_35b_a3b_fp8_vllm.sh
```

The script defaults to:

- model: `Qwen/Qwen3.6-35B-A3B-FP8`
- served model name: `qwen3.6-35b-a3b-fp8`
- endpoint: configured by `HOST`/`PORT` (e.g. `http://<vllm-host>:60995/v1`)
- GPU selection: `VLLM_GPUS=device=1`
- max context: `32768`
- tool parser: `qwen3_xml`
- auth: set `VLLM_API_KEY` or `SYFI_VLLM_API_KEY` before launching vLLM; clients set
  `SYFI_LLM_API_KEY` to the same value.

With `llm.provider: "vllm"` in the config (the default), the tester/runtime already point here — just
`E2B_API_KEY=$E2B_KEY just ai-serve`. To enable Qwen thinking, add
`SYFI_LLM_CHAT_TEMPLATE_KWARGS='{"enable_thinking":true}'` to the environment.

For uploaded traces using browser/Pyodide tools, E2B is not needed. For public SYFI trace analysis
through the baked DuckDB template, keep `E2B_API_KEY` set.

## Session Workload Replay

A session-aware vLLM workload runner lives in [`session_runner/`](session_runner/). It replays
trace-derived sessions as closed-loop chains of LLM rounds, preserving `prefix_len`, appended input
length, target decode length, `arrival_time`, and `tool_wait_after_ms` while using a synthetic text
corpus instead of raw private prompts.

Use it when you need to benchmark serving behavior for multi-round coding-agent sessions rather than
independent request arrivals. See [`session_runner/README.md`](session_runner/README.md) for build,
dry-run, replay, and prefix-cache accounting instructions.

## Standalone Loop Tests (no browser, no server)

The same model -> tool loop is testable as plain CLI calls, so the user-trace path has a regression
guard that needs no E2B, server, or browser:

```bash
# Public path (unchanged): model -> E2B -> answer over the baked DuckDB template.
E2B_API_KEY="$E2B_KEY" OPENROUTER_API_KEY="$OPENROUTE_KEY" \
  uv run python web/ai_infra/syfi_llm_runtime.py --question "top tools by provider" --print-code

# User path: --db swaps E2B for a LocalDuckDBExecutor (mirrors the browser's Pyodide) and runs the
# whole loop in-process against a local DuckDB.
source ~/.bashrc
uv run python web/ai_infra/syfi_llm_runtime.py \
  --db trace/syfi_coding_trace.duckdb --print-code \
  --question "How many rows are in tool_calls?"
```

For a transport-level check without a real browser, `tools/ws_smoke.py` connects to `/api/chat/ws`,
answers `tool_request` frames by running the code against a local DuckDB, and asserts a final
answer comes back:

```bash
uv run --extra ai python web/ai_infra/tools/ws_smoke.py \
  --source user --db trace/syfi_coding_trace.duckdb --model qwen3.6-35b-a3b-fp8 \
  --question "How many distinct tool_name values are in tool_calls?"
```

Runtime sampling defaults follow the Qwen3.6 model-card recommendation for thinking-mode general
tasks:

- `temperature=1.0`
- `top_p=0.95`
- `top_k=20`
- `min_p=0.0`
- `presence_penalty=1.5`
- `repetition_penalty=1.0`

Override them with `SYFI_LLM_TEMPERATURE`, `SYFI_LLM_TOP_P`, `SYFI_LLM_TOP_K`,
`SYFI_LLM_MIN_P`, `SYFI_LLM_PRESENCE_PENALTY`, or `SYFI_LLM_REPETITION_PENALTY`. For precise
coding-style experiments, Qwen recommends `SYFI_LLM_TEMPERATURE=0.6` and
`SYFI_LLM_PRESENCE_PENALTY=0.0`.

Set `SYFI_LLM_CHAT_TEMPLATE_KWARGS='{"enable_thinking":false}'` only when testing strict
final-only answers. For model-quality evaluation and harder trace analysis, keep thinking enabled.

Latest smoke results:

- Template test found `/data/syfi_coding_trace.duckdb` in the sandbox.
- DB size in sandbox: `170143744` bytes.
- Provider counts: `codex=216823`, `claude=140338`.
- Top tools: `exec_command`, `Bash`, `write_stdin`, `Read`, `apply_patch`.
- Plot artifact created: `/out/provider_rounds.png`.
- OpenRouter produced a structured `run_python` tool call and the E2B tool result produced a final answer.
- Dedicated `syfi_llm_runtime.py` generated Python at runtime, executed it in E2B, and returned
  exact provider counts: `codex=216823`, `claude=140338`.
