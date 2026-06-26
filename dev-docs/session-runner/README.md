# Session Runner Next-Stage Development Notes

This directory records development plans for future `session_runner` features. These are developer notes, not user-facing manuals. Each feature is documented separately so it can be implemented, reviewed, and accepted task by task.

## Document Index

- [backend-architecture.md](./backend-architecture.md): Shared architecture for vLLM/OpenAI, SGLang, and llama.cpp backends.
- [sglang-backend.md](./sglang-backend.md): SGLang native backend design and validation notes.
- [llamacpp-backend.md](./llamacpp-backend.md): Support llama.cpp server by first validating the OpenAI-compatible path, then adding native `/completion` only if needed.

## General Development Principles

Future work should reuse the current project structure:

- Keep CLI arguments centralized in `replay/src/cli.rs`.
- Keep argument validation in `replay/src/main.rs`, failing as early as possible.
- Keep session scheduling logic in `replay/src/session.rs`.
- Keep server protocol differences behind the `Backend` adapter tree in `replay/src/backend/`.
- Do not opportunistically refactor workload statistics, trace parsing, or log schemas for a single feature.

Keep implementation changes minimal:

- Do not add a new trait or configuration layer for a single parameter.
- Do not add flags with duplicate meanings.
- Do not leak server-specific fields into the main session runner flow.
- Do not change default behavior; default arguments should reproduce the current behavior.
- Do not guess server protocol fields speculatively. Validate against the target service documentation, `/openapi.json`, or an actual response first.

Be precise about naming across stages: `csv_export --arrival-rate` is an export-time synthetic arrival rate measured in `sessions/s`; replay-time controls in `session_runner` should use scale-style naming so “generate arrival times” and “scale existing arrival times” are not conflated.

## Backend Extension Rules

The `session_runner` main flow should only know normalized request and response semantics:

- `GenRequest`: model name, prompt token ids, output length, temperature, and whether to stream.
- `StreamEvent`: text delta or cumulative text, generated token ids, finish reason, and usage.
- `Usage`: prompt, completion, total, and cached prompt token accounting.

When adding a backend, add only one `BackendKind` enum value, one adapter struct, and one `build_backend` match branch. Extend the normalized structures only when the current `GenRequest` truly cannot express required information.

When maintaining the vLLM backend, do not test only the standard completions fields. The current `VllmBackend` also sends vLLM extension fields: `ignore_eos`, `return_token_ids`, and `stream_options.include_usage` for streaming requests. The target service must accept those fields, explicitly ignore them, or receive an adjusted adapter; otherwise `--backend vllm` should not be considered validated.

## Shared Acceptance Criteria

- `cargo fmt` leaves the code style consistent with the current project.
- `cargo build --release --manifest-path replay/Cargo.toml --bin session_runner` succeeds.
- Default argument behavior is unchanged from the current version.
- Example commands in the README or corresponding developer note are executable.
- Cache metrics must not silently fabricate correctness: if the server cannot reliably report cached prompt tokens, the runner should fail fast or clearly mark the metric as unavailable.
