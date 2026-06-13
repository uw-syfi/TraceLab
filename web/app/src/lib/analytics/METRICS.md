# Analytics Metrics Catalog

Spec for the **personal trace analytics** surface (prototyped at `/lab`, to land in the Analyze flow).
Design goal: a **strict superset of Provider Comparison** (`lib/compare.ts`) for a single user's own
trace, **plus** cost, temporal, session drill-down, and superlative "facts" that Compare doesn't have.

Contract lives in [`types.ts`](./types.ts) (`AnalyticsPayload`, `Stats`, `SessionRow`, `Fact`, …);
pricing in the single-source [`pricing.json`](../../../../../artifacts/utils/pricing.json) (resolved in [`cost.ts`](./cost.ts)). Mock in [`../mock/analytics.ts`](../mock/analytics.ts).

## Legend

- **Type**: `total` · `avg` · `p50`/`p90`/`p99` · `ratio` (0–1 / %) · `rate` (X per Y) · `super` (superlative / argmax)
- **Grain**: `step` (LLM round) · `req` (user turn) · `session` · `day` · `trace` (whole)
- **Tag**: `[cmp]` already in Provider Comparison · `[gap]` in Compare but **missing from our first brainstorm — add to be a superset** · `[new]` our addition beyond Compare
- **Source** fields are DuckDB columns: `rounds` (`prefix_tokens`, `newly_append_tokens`, `output_tokens`, `reasoning_output_tokens`, `model`, `provider`, `session_id`, `round_index`), `tool_calls` (`tool_name`, latencies, `is_error`, `emitted_at`/`result_at`), `timing_events` (`event_type`, `timestamp`). "started_with_user_message" vs "started_with_tool_result" = the step-trigger slice.

---

## 1. Headline totals (KPIs)

| Metric | Type · Grain | Source / formula | Tag |
|---|---|---|---|
| Sessions | total · trace | distinct `session_id` | `[cmp]` |
| Distinct users | total · trace | provenance | `[cmp]` |
| Agent steps | total · trace | row count | `[cmp]` |
| **Requests (user turns)** | total · trace | `rounds_with_visible_user_message` | `[gap]` |
| **Tool-triggered steps** (count + %) | total+ratio | `rounds_started_from_tool_result` | `[gap]` |
| Total input tokens | total | Σ(`prefix`+`append`) | `[cmp]` |
| Total cached input | total | Σ `prefix_tokens` | `[cmp]` |
| Total uncached (fresh) input | total | Σ `newly_append_tokens` | `[cmp]` |
| Total output tokens | total | Σ `output_tokens` (incl. reasoning) | `[cmp]` |
| Total reasoning tokens | total | Σ `reasoning_output_tokens` | `[cmp]` |
| Total cost (USD) | total | pricing × token split | `[new]` |
| Cache saved (USD) | total | `prefix × (input−cachedInput) rate` | `[new]` |
| Trace span / active days | total · trace | min/max `timing_events.timestamp` | `[cmp]` |
| Models represented (count) + top model | total+ratio | distinct `model` | `[gap]` |

---

## 2. Aggregate stats — averages & percentiles

### 2a. Tokens per step
| Metric | Type | Source | Tag |
|---|---|---|---|
| Avg total input / step | avg·step | mean total input | `[cmp]` |
| Avg cached-read input / step | avg·step | mean `prefix` | `[cmp]` |
| Avg append (fresh) input / step | avg·step | mean `newly_append` | `[cmp]` |
| Avg output / step | avg·step | mean `output` | `[cmp]` |
| Avg reasoning / reasoning-step | avg·step | reasoning Σ ÷ steps with reasoning | `[cmp]` |
| Input∶output amplification | ratio | total input ÷ total output | `[new]` |
| Fresh-token share | ratio | `append ÷ total_input` (lower = more reuse) | `[new]` |
| Reasoning share of output | ratio | reasoning ÷ output | `[new]` |

### 2b. Input by step trigger `[gap]` (whole block missing)
| Metric | Type | Source |
|---|---|---|
| User-initiated avg total input | avg·step | started_with_user_message |
| User-initiated avg append input | avg·step | started_with_user_message |
| Tool-triggered avg total input | avg·step | started_with_tool_result |
| Tool-triggered avg append input | avg·step | started_with_tool_result |

### 2c. Context growth `[gap]` (only "avg growth" was in our brainstorm)
| Metric | Type | Source |
|---|---|---|
| Total context increase | total | `total_context_increase_tokens` |
| User-initiated context delta avg / **p50 / p90** | avg+p50+p90 | user slice |
| Tool-triggered context delta avg / **p50 / p90** | avg+p50+p90 | tool slice |
| **context-increase ∶ append** (overall + by trigger) | ratio | how much appended actually grew context |
| **Growth share** (by trigger) | ratio | positive_growth_share |
| **Reduction share** (by trigger) | ratio | negative_growth_share |
| **Major-compaction share** (by trigger) | ratio | major_compact_share — how often auto-compaction fires |

### 2d. Cache efficiency
| Metric | Type | Source | Tag |
|---|---|---|---|
| Overall prefix hit rate | ratio | `prefix_hit_rate` | `[cmp]` |
| Hit rate p50 / p90 | p50/p90 | per-step hit ratio | `[new]` |
| **User-initiated hit rate** | ratio | by trigger | `[gap]` |
| **Tool-triggered hit rate** | ratio | by trigger | `[gap]` |
| Hit-rate decay across a session | trend | slope vs round_index | `[new]` |

### 2e. Request timing (per round wall time)
| Metric | Type | Source | Tag |
|---|---|---|---|
| Avg / **p50 / p90 / p99** request time | avg+pctl | round wall time | `[new]` (Compare only has gen-time p50/p90) |
| Generation time p50 / p90 | p50/p90 | `observable_generation_time` | `[cmp]` |
| **Total generation time** | total | Σ | `[gap]` |
| Output decode throughput (tok/s) | rate | normalized decode speed | `[cmp]` |
| **Post-reasoning decode throughput** | rate | post_reasoning_tpot | `[gap]` |
| **Estimated TTFT** (from reasoning tokens) | avg | estimated_ttft | `[gap]` |

### 2f. Tools
| Metric | Type | Source | Tag |
|---|---|---|---|
| Avg tool calls / step | avg·step | `tool_calls` ÷ steps | `[cmp]` |
| **Tool calls / request** | rate·req | ÷ requests | `[gap]` |
| Steps with tool calls (%) | ratio | rounds_with_tool_calls | `[cmp]` |
| Tool latency p50 / p90 | p50/p90 | effective latency | `[cmp]` |
| **Total attributed tool time** | total | Σ effective latency | `[gap]` |
| **Tool error rate** | ratio | `is_error` share | `[new]` (Compare has none) |
| **Read∶write ratio** | ratio | Read vs Edit/Write | `[new]` |
| Per-tool latency + count + error | avg+ratio | group by `tool_name` | `[new]` |
| Avg tool-result size (chars) / step | avg | `result_chars` | `[new]` |
| Top tool share | ratio | most-used ÷ all | `[new]` |

### 2g. Human-in-the-loop
| Metric | Type | Source | Tag |
|---|---|---|---|
| **Total human wait time** | total | Σ waiting_for_human | `[gap]` |
| Human wait avg / **p50 / p90** | avg+p50+p90 | waiting_for_human | `[cmp]` (avg/p50/p90) |
| Human-in-loop share | ratio | wait time ÷ total time | `[new]` |
| Session time split: generation / tool / waiting | ratio | three buckets | `[new]` |

### 2h. Per-session (agentic shape)
| Metric | Type | Source | Tag |
|---|---|---|---|
| Avg agent steps / session | avg·session | rounds ÷ sessions | `[new]` |
| Avg tool calls / session | avg·session | — | `[new]` |
| Avg session duration | avg·session | last−first ts | `[new]` |
| Avg cost / session | avg·session | — | `[new]` |
| **Autonomy depth** avg / p90 | avg+p90 | run-length of consecutive tool-result steps | `[new]` |
| Avg human interjections / session | avg·session | user-message count | `[new]` |
| Avg models per session / mid-session switches | avg | distinct `model` within session | `[new]` |
| Typical session profile (median) | super | "X steps · Y min · $Z" | `[new]` |

### 2i. Per-day / relatable rates `[new]`
Avg steps/day · avg sessions/day · avg active hours/day · avg cost/day · **$/hour of agent work** · **tokens per $** · **steps per $** · **tool calls per $** · blended $/Mtok · cache-saved % · "reasoning tax" $/step.

### 2j. Models & providers
| Metric | Type | Source | Tag |
|---|---|---|---|
| Steps on Claude / Codex (share) | ratio | rounds by `provider` | `[new]` |
| Cost on Claude / Codex (share) | ratio | cost by `provider` | `[new]` |
| Models represented (count) | total | distinct `model` | `[gap]` |
| Top model + step share | ratio | argmax `model` rounds | `[gap]` |
| Steps on Opus-tier (expensive model) | ratio | rounds where model is opus | `[new]` |

> Provider share is the personal-view counterpart to Compare's whole-job model mix: "how much do I lean on Claude vs Codex" (by steps and by cost). Candidate to also render as a small donut, not just stat rows.

---

## 3. Cost (whole block is `[new]` — Compare has no cost)

Total cost · cost by model (cached/fresh/output stacked) · cost per step/session/day · reasoning cost &
share · cache savings (USD + %) · blended $/Mtok · tokens-per-$ · $/hour · priced vs unpriced steps.
Pricing model + cache-read split: see the single-source [`pricing.json`](../../../../../artifacts/utils/pricing.json) (resolved in [`cost.ts`](./cost.ts)).

---

## 4. Temporal `[new]`

- **Per-day activity** — calendar heatmap (steps/day; tokens, cost on hover)
- **Work rhythm** — hour-of-day × weekday heatmap (local time)
- Longest streak · busiest day · peak hour (also surfaced as facts)

---

## 5. Sessions `[new]`

**List row**: id, provider, primary model, steps, duration, input tokens, cost, tool calls, errors.
**Filters**: by provider (vendor), sort (steps / duration / cost / recency), text search.
**Drill-down** (`SessionDetail`, fetched on demand): per-round timeline (cached/fresh stacked + output
line + dataZoom + user-input markers), tool stats (count / p50 / errors per tool).

---

## 6. Superlative facts (clickable Highlights; `super`, deep-link to session)

Grouped by dimension; each is an argmax/argmin → `{title, value, sessionId?, roundIndex?}`.

**Time & cadence** — longest continuous session · longest unattended run (consecutive tool-result
steps) · busiest day · longest daily streak · longest human gap · fastest/slowest generation · longest
single think · peak hour · first/last use (span).
**Cost** — total spend · cache savings · priciest session · priciest round · per-model spend · reasoning
share · avg per round/session.
**Conversations** — longest conversation (steps) · context peak (max single input) · largest prefix reuse
· biggest single output · avg length.
**Reasoning** — deepest single think · most think-heavy round (reasoning∶output) · total reasoning.
**Tools** — most-used · most tool calls in one round · slowest (p50/p90) · most error-prone · common
tool sequences (n-grams).
**Cache & efficiency** — overall hit rate · in-session decay · most/least efficient session.
**Models** — usage share · mid-session switches · max autonomy depth · human-in-loop share.

---

## 7. Coverage check vs Provider Comparison

Every `lib/compare.ts` row is covered (`[cmp]`) or explicitly added back (`[gap]`). Items flagged
`[gap]` above are the ones our first brainstorm **neglected** — fold them into `Stats` to guarantee the
personal view is a strict superset:

1. Requests (user turns) as a first-class count + per-request rates
2. Input split by step trigger (user vs tool: avg total / avg append)
3. Context growth p50/p90 + growth / reduction / **major-compaction** shares by trigger
4. context-increase ∶ append ratio (overall + by trigger)
5. Post-reasoning decode throughput + estimated TTFT
6. Prefix hit rate by trigger (user vs tool)
7. Time totals: total generation time · total tool time · total human wait
8. Tool calls / request
9. Models represented (count) + top model

**On top of the superset** (Compare has none of these): all of §3 Cost, §4 Temporal, §5 Sessions,
§6 Facts, plus tool error rate, read∶write, per-tool breakdown, autonomy depth.
