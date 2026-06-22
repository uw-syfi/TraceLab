# paper_macros

**The single source of truth for the headline numbers the TraceLab paper cites.**

The paper (`src/*.tex`) refers to a fixed set of result numbers through LaTeX `\newcommand`
macros defined in `_command.tex`. This experiment computes every one of those macros from the
trace and emits a ready-to-include `\newcommand` block, so each macro maps **1:1** to a value
reproducible from the public dataset.

## What it computes

| macro | meaning | scope | source of the definition |
|---|---|---|---|
| `\avgstepperrequest` | LLM steps per user request | merged | user-turn state machine (`user_turn_decomposition`) |
| `\avgtollcallsperrequest` | tool calls per user request | merged | same |
| `\avgtimeperrequest` | mean end-to-end minutes per request | merged | same (e2e == `user_turn_response_time`) |
| `\pntimeperrequest` | p90 end-to-end minutes per request | merged | same |
| `\mediancachedinputtokens` | median per-step prefix (cache-read) tokens | merged | trace DB |
| `\medianuncachedinputtokens` | median per-step newly-appended tokens | merged | trace DB |
| `\medianoutputtokens` | median per-step output tokens | merged | trace DB |
| `\mediandecodespeed` | median per-step normalized decode tok/s | merged | `overview_summary` per-step timing |
| `\mediancodecdecodespeed` | median post-reasoning decode tok/s | Codex | same |
| `\mediancodecdecodespeedttft` | median TTFT residual (s) | Codex | same + aggregate decode latency |
| `\totaltoolcatetory` | distinct tools observed (floored to 10) | merged | trace DB |
| `\topthreetoolpercent` | per-provider top-3 tool share (floored) | per-provider | trace DB |
| `\toolcalltoppercentage` | Claude top-3 tool share (floored) | Claude | trace DB |
| `\toolcalltoppercentagecodex` | Codex top-3 tool share (rounded) | Codex | trace DB |
| `\toolcallslongerthanonemin` | % of tool calls slower than 1 min | merged | effective latency `>= 60 s` |
| `\toolcallslongerthanoneminpercent` | % of tool time those calls hold | merged | same |
| `\prefixcachehitrate` | global token-weighted prefix hit rate | merged | `overview_summary` |
| `\prefillamplificationfactor` | total new append / context growth | merged | `overview_summary` (no prefix term) |

Definitions are **reused** from the canonical experiments rather than reimplemented: token
totals / hit rate / context growth / decode latency come from `trace_facts/overview_summary`;
per-request metrics replay the exact turn boundaries from
`human_in_the_loop/user_turn_decomposition` (identical to `user_turn_response_time`); per-step
medians and tool stats are computed off the trace DB. "Longer than 1 minute" is the `>= 60000 ms`
effective-latency bins (`internal` else `wall`), matching `tool_calls/tool_latency_distribution`.

The "more than X" macros (`\totaltoolcatetory`, `\topthreetoolpercent`,
`\toolcalltoppercentage`) are floored so the paper's "more than ..." phrasing stays true; the
Codex-specific `\toolcalltoppercentagecodex` macro is rounded because the paper states it as an
approximate total rather than a lower bound. The exact basis is printed and kept as a trailing
comment in the generated file. `\prefillamplificationfactor` is the
**context-growth** amplification (new append / positive same-session total-input growth), *not*
`1/(1 - hit_rate)`.

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/trace_facts/paper_macros/analyze.py

# the pinned public trace
uv run python artifacts/trace_facts/paper_macros/analyze.py -i trace/syfi_coding_trace.jsonl

# a prebuilt DB, into a chosen dir
uv run python artifacts/trace_facts/paper_macros/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs

- `paper_macros.tex` — the `\newcommand` block (each line annotated with its exact basis). Paste
  it into `_command.tex`, or `\input` it.
- stdout — a readable `macro / value / basis` table.

No figures.
