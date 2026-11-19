# Subagent timing

Uses the same shared session timing definitions as `session/session_timing_distribution`, not a separate meaning of tool time. The primary tool metric is **canonical attributed tool work**: sum positive internal duration when present, otherwise emitted-to-result wall duration. It includes interaction tools and is not exclusive compute or blocked time.

The report compares linked descendants with matched mains and splits native `worker`, `guardian`, and legacy `unknown` kinds. Cohort membership still comes from `subagent_cohorts.COHORT_SQL`. Each session is counted once. Unknown kinds are not guessed from behavior.

Observed elapsed span is the earliest through latest recorded event or valid tool endpoint, including gaps. It is not verified spawn-to-return duration. Distribution tables report Avg/P25/P50/P90/P99. The percentage table uses **pooled known tool work / pooled eligible observed span**, matching the overall session timing denominator and weighting. It does not report a fraction of a family's tool calls. Parallel attributed work can exceed elapsed time.

**Time with tools outstanding** remains a separately named extra wall metric: union emitted-to-result intervals, with overlap counted once. It requires complete valid endpoints. Missing timing, anonymous native tool observations, and positive internal durations with exactly equal endpoints explicitly reduce coverage and eligible populations. Known work remains reported as a partial recorded amount; unknown durations are not zero measurements. `N/A` means unavailable.

Low ratios can be plausible: shell/read calls are often fast, while observable response spans are longer. Text-only guardians and long between-request gaps also change the population. Missing native web-search starts mean their duration cannot be measured. None of this proves the unaccounted remainder is model compute. See the overall timing README for request lifecycle and timestamp rules.

```bash
uv run --no-sync python artifacts/subagents/timing/analyze.py --db trace/syfi_coding_trace.duckdb
uv run --no-sync python -m pytest -q scripts/tests/test_subagent_timing.py scripts/tests/test_request_timing.py
```

Outputs: `timing.md`, `headline.json`. No session identifiers or raw content are exported.

## Sample populations

Every distribution row reports its own measurement N. Ratio cells or adjacent ratio columns report the eligible N used in both pooled numerator and denominator. Observed-span and known-work distributions retain finite recorded values, including captures unsuitable for wall ratios or with partial timing. These averages need not describe the stricter ratio population. The overall and subagent observed-span distributions use the same recorded-span definition. Request duration, attributed work, and bounded occupancy also expose their distinct sample populations.
