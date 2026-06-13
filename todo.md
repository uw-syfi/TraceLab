# TODO

## SYFI QA architecture

- [ ] Add a public "Ask the SYFI trace" assistant on the gallery / Overview surface.
- [ ] Use a backend API for SYFI QA; do not run public-trace QA in each visitor's browser.
- [ ] Use an isolated code sandbox for AI-written code. Data is public, but generated code is untrusted.
- [ ] Prefer E2B for the sandbox layer to avoid self-managing container isolation.
- [ ] Keep the model provider swappable. OpenRouter is acceptable if the selected model supports tool/function calling.
- [x] Use `web/ai_infra/build_syfi_e2b_template.py` to build the E2B template with the SYFI DuckDB baked in.
- [x] Use `web/ai_infra/test_syfi_e2b_template.py` to test the prebuilt E2B template without uploading the DB.
- [x] Use `web/ai_infra/syfi_qa_smoke.py` to test the OpenRouter integration sketch:
  - local DB sanity: `uv run python web/ai_infra/syfi_qa_smoke.py`
  - build E2B template: `E2B_API_KEY=... .venv/bin/python web/ai_infra/build_syfi_e2b_template.py --name syfi-qa-code-interpreter:latest`
  - test E2B template: `E2B_API_KEY=... .venv/bin/python web/ai_infra/test_syfi_e2b_template.py --template syfi-qa-code-interpreter:latest`
  - OpenRouter tool-call dry run: `OPENROUTER_API_KEY=... uv run python web/ai_infra/syfi_qa_smoke.py --openrouter --model <tool-capable-model>`
  - full tool loop: `E2B_API_KEY=... OPENROUTER_API_KEY=... uv run python web/ai_infra/syfi_qa_smoke.py --openrouter --e2b-tool --e2b-template syfi-qa-code-interpreter:latest --model <tool-capable-model>`
  - env aliases are also supported: `E2B_KEY` and `OPENROUTE_KEY`
- [ ] Expose tools from the backend, not directly to the model:
  - `run_python(code)`
  - `list_artifacts()`
  - `read_artifact(path)`
- [x] Move the SYFI QA system prompt into `web/ai_infra/syfi_qa_system_prompt.md` and load it from
  the OpenRouter/E2B runtime.
- [x] Rename the maintained OpenRouter/E2B runtime to `web/ai_infra/syfi_llm_runtime.py`.
- [x] Add a local HTML tester for multi-turn SYFI QA and generated image display.
- [x] Return both inline plots and file artifacts from sandbox execution:
  - `plt.show()` for inline E2B PNG results
  - `plt.savefig("/out/name.png")` for downloadable artifacts

## DuckDB data layer

- [ ] Treat `syfi.duckdb` as the shared prerequisite for public figures and SYFI QA.
- [ ] Build `syfi.duckdb` once offline from `trace/syfi_coding_trace.jsonl.gz`.
- [ ] Mount `syfi.duckdb` read-only in backend/E2B sandboxes.
- [ ] Finish `run_all.py` migration so it builds one DB once and passes `--db` to DB-aware experiments.
- [ ] Keep `round_pk` as the surrogate ingestion-order primary key.
- [ ] Preserve duplicate source rows; do not dedupe on `round_id`, `trace_key`, or `(session_id, round_index)`.
- [ ] Keep the normalized table split:
  - `rounds`
  - `tool_calls`
  - `timing_events`
- [ ] Add stable SQL views/macros for AI-generated code, for example:
  - `effective_tool_calls`
  - `round_token_summary`
  - `session_summary`
- [ ] In AI prompts and helper docs, require SQL-first analysis: aggregate in DuckDB, then convert only small results to pandas/matplotlib.

## Browser uploaded traces

- [ ] Preserve the current privacy promise: uploaded traces stay local unless the user explicitly opts into cloud AI analysis.
- [ ] Keep the existing multi-worker Pyodide path for fixed uploaded-trace figures until the DB worker design is benchmarked.
- [ ] If adding private AI Q&A for uploaded traces, prefer one local DB/Pyodide worker that owns the uploaded trace and executes queued query/code jobs.
- [ ] Do not fan out one browser-built DuckDB file to four Pyodide workers unless benchmark results show acceptable memory use.
- [ ] If cloud AI analysis for uploaded traces is added, make it an explicit opt-in mode with clear consent copy.

## Browser memory notes

- Current measured local trace:
  - `trace/kanzhu_sanitize.jsonl.gz`: about 11 MB
  - `/tmp/coding_trace_kanzhu_sanitize.duckdb`: about 34 MB
  - DB/gzip ratio: about 3.2x
- Current SYFI trace:
  - `trace/syfi_coding_trace.jsonl.gz`: about 52 MB
  - `trace/syfi_coding_trace.jsonl`: about 617 MB
  - estimated `syfi.duckdb`: about 165 MB, using the measured 3.2x ratio
- Duplicating the estimated SYFI DB across four browser workers would be about 660 MB of DB file copies before adding Pyodide, DuckDB runtime/cache, decompression, query memory, matplotlib, and PNG buffers.
- Expected browser peak for build-once-then-copy-to-four-workers could plausibly reach 1.5-3+ GB. Benchmark before committing to that design.

## Backend sandbox policy

- [ ] Do not expose secrets to the sandbox.
- [ ] Mount dataset read-only.
- [ ] Use a writable `/out` directory for plots/CSVs.
- [ ] Set CPU, memory, file-count, output-size, and execution-time limits.
- [ ] Disable network by default unless a specific future tool requires it.
- [ ] Store generated artifacts separately from the sandbox lifecycle if they need stable URLs.
