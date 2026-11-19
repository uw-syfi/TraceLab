# Recorded timing distributions

This report compares recorded timing components per session, reconstructed request group, LLM round, and positive individual latency. It does not partition hardware compute time.

## Shared definitions

`artifacts/utils/request_timing.py` supplies the same request ownership, per-session measurements, coverage checks, and canonical tool work used by subagent timing. Canonical attributed tool work sums strictly positive `tool_internal_latency_ms` when present, otherwise `tool_wall_latency_ms`. A recorded zero internal value wins over a nonzero wall value. Unknown duration remains a coverage gap. Interaction tools such as `AskUserQuestion`, `ExitPlanMode`, and input-request tools are included and identified separately; their time is not pure execution.

Observed response spans use the latest qualifying input before the first model output through the last output of each round. They include observable service delays and uninstrumented work; they are not measured model inference. Input gaps run from the previous recorded event to the next user-message event. Subagent prompts may be automated, so these gaps are not necessarily human thinking.

Observed session span is the earliest through latest recorded event or valid tool endpoint. Parallel call latencies can sum beyond that span. Pooled session ratios use the same eligible sessions in numerator and denominator. Missing canonical timing, anonymous native tool observations, or invalid/missing call endpoints exclude a session from a complete tool-work/span ratio. Known positive work remains reported separately. Exactly equal emitted/result timestamps with positive runner duration invalidate wall ratios, including subsecond contradictions.

Tool-outstanding time is a separately named extra metric: the union of call-to-result intervals, counting overlap once. It requires valid endpoints and consistent timestamps. It is not canonical tool execution time, and the remainder is not model compute.

## Requests and coverage

Native Codex turn IDs own request rows and lifecycle markers. A recorded start plus native completion yields a duration; abort, missing start, open capture, conflicting markers, and compressed timestamps do not. Claude uses an observed user-triggering input and explicit final stop marker; tool-use outputs are never completion. Legacy user-triggered groups remain usable for counts and attributed costs/work but have no verified completion or response duration. A next prompt and EOF never manufacture an end.

Per-request attributed tool work is reported independently of native response wall time. Only bounded outstanding wall intervals are divided by native response time; internal durations are never clipped into invented intervals. Therefore a tool returning days after the model response cannot inflate a short response-time percentage. Native web-search records without start timing and anonymous observations explicitly reduce timing coverage.

The report includes lifecycle counts and measurable-duration coverage. `N/A` means unavailable, not zero. Percentiles use eligible values. Session component ratios may overlap and exceed 100%; request tool-work ratios are intentionally absent. The capped-session block is a sum-of-components budget with each input gap limited to one hour, not measured engaged wall time.

Low subagent tool ratios can reflect fast calls, text-only guardian sessions, and long between-request gaps. Compare matched mains and worker/guardian populations using the same definitions before inferring behavioral differences.

## Run

```bash
uv run python artifacts/session/session_timing_distribution/analyze.py --db trace/syfi_coding_trace.duckdb
```

Outputs: `session_timing_distribution.md`, `.tex`, and `headline.json`. Rebuild on the intended capture; historical narrative values are not substituted for the current generated table.

## Sample populations

Every distribution row reports its own measurement N. Ratio cells or adjacent ratio columns report the eligible N used in both pooled numerator and denominator. Observed-span and known-work distributions retain finite recorded values, including captures unsuitable for wall ratios or with partial timing. These averages need not describe the stricter ratio population. The overall and subagent observed-span distributions use the same recorded-span definition. Request duration, attributed work, and bounded occupancy also expose their distinct sample populations.
