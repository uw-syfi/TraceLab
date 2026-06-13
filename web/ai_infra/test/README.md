# SYFI QA Model Tests

This directory holds a small benchmark set for the OpenRouter -> E2B -> DuckDB QA loop.

The task file is `syfi_qa_tasks.json`. Each task has:

- `id`: stable task identifier.
- `difficulty`: `easy`, `medium`, or `hard`.
- `tests`: model capabilities this task is meant to exercise.
- `question`: the only field that should be sent to the model.
- `expected`: reference answer data computed from `trace/syfi_coding_trace.duckdb`.
- `grading`: checks a future evaluator should apply.

Do not include `expected` in the model prompt. It is for scoring only.

## Task Mix

The current set has 21 tasks:

- Easy tasks check basic table counts, grouping, top-k, and simple sums.
- Medium tasks check joins, rates, null handling, percentiles, and provider splits.
- Hard tasks check deterministic ranking, session-style grouping, timestamp bucketing, multi-step
  CTEs, and plot/artifact generation.

## Manual Smoke Example

Run one task manually:

```bash
source ~/.bashrc
E2B_API_KEY="$E2B_KEY" OPENROUTER_API_KEY="$OPENROUTE_KEY" \
  .venv/bin/python web/ai_infra/syfi_llm_runtime.py \
  --template syfi-qa-code-interpreter:latest \
  --model tencent/hy3-preview \
  --question "Count SYFI rounds by provider. Return provider names and exact counts." \
  --print-code
```

For a real benchmark runner, iterate over `tasks[*].question`, capture the final answer, the emitted
tool code, stdout, errors, and artifact metadata, then compare against `expected`.

## Scoring Notes

At minimum, score:

- Tool use: data questions should call `run_python`.
- SQL correctness: uses real table/column names and joins child tables by `round_pk`.
- Result correctness: exact integer matches; decimal values within the task tolerance.
- Resource behavior: avoids full-table pandas loads unless the task explicitly requires sampling.
- Artifact behavior: plot tasks should save the requested PNG under `/out`.
