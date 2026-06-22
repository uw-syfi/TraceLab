# eviction_tradeoff

**As the prefix-cache eviction timeout `tau` grows, the achievable cache hit rate rises (fewer idle
gaps outlast `tau`) but the KV storage you must provision rises too (idle requests hold their KV
longer). This experiment sweeps `tau` and reports both, exposing the trade-off and its diminishing
returns.**

## Experiment overview

An idealised, per-step binary model (the paper's rule): a step's prefix is a **full hit** if the
idle gap before it is `<= tau`, and a **full miss** (re-prefilled) if the gap is `> tau`.

Per session step `S`, paired with its predecessor `P`, with idle gap `g` before `S` (human think
time for a user step; tool latency for a tool-result step):

- `L = prefix_tokens + newly_append_tokens` — total prompt tokens at `S`.
- `fresh = clip(max(0, L - L_prev) - output_tokens(P), 0, append)` — the truly new user/tool tokens
  (context growth minus the prior step's output; same definition as `redundant_prefill`, and
  `output_tokens` already includes reasoning per `trace_facts/overview_summary`). `fresh` is the
  irreducible prefill the cache can never serve.
- `cacheable = L - fresh` — reusable tokens a perfect, eviction-free cache would serve.

This is an **idealised** cache whose only imperfection is the eviction timeout: a retained step
(gap <= `tau`) prefills only its `fresh` tokens, an evicted step re-prefills its whole context `L`.
Two token-weighted quantities over all covered steps:

```
hit_rate(tau)      = sum_{g<=tau} cacheable / sum L         (-> 1 - fresh/L = optimal as tau->inf)
prefill(tau)       = sum fresh + sum_{g>tau} cacheable      (tokens prefilled at tau)
A(tau)             = prefill(tau) / sum fresh               (prefill amplification; floors at 1x)
redundant_ratio(tau)= 1 - 1 / A(tau)                        (equivalent share-of-prefill form)
```

**Prefill amplification** `A` is the bottom panel: how many times more tokens are prefilled than the
irreducible `fresh` minimum. An eviction-free perfect cache prefills only `fresh`, so `A` **floors
at 1x**; tighter eviction re-prefills evicted context and inflates it.

The **real deployed cache** is overlaid as a reference (it is *not* the idealised model): it prefills
`append` (amplification `sum append / sum fresh`) and serves `prefix` (hit `sum prefix / sum L`).
That operating point lands on the idealised curve at the **effective eviction time** --- the `tau`
where the idealised hit rate equals the real one. This is how the section reconciles with the static
`redundant_prefill` table (whose `1 / (fresh % of append)` *is* the real amplification).

**Storage axis (capped).** Idle is *capped by the eviction time*: before `tau` elapses you cannot
know a gap will outlast it, so the KV is held to `tau` regardless. With `gen_r` the per-round
input->last-output span (active decode time),

```
R(tau)             = sum_i min(g_i, tau) / sum_r gen_r       (suspended-KV / active-KV storage ratio)
kv_active_ratio(tau)= 1 / (1 + R(tau))
```

This `R` is the corrected storage ratio from the paper's §7.5 derivation (`R = (T_human + T_tool) /
T_generation`): idle time dwarfs generation time, so suspended KV dominates and `R` grows well above
1 at long timeouts.

Method and assumptions:

- **Gaps** reuse `cache_hit_idle_relationship`: a user step's gap is `first_activity -
  previous_round.last_activity`; a tool-result step's gap is the **max** leading tool-result
  duration (the request's wall idle while its tools run, which is what suspends its KV — note this
  differs from `kv_cache_active_ratio`, which sums every individual tool-call latency).
- **Generation span** reuses `kv_cache_active_ratio`'s `input->last-output` definition.
- **Coverage.** A step counts only when it has a predecessor (for `fresh`) and a measurable idle
  gap; session-first and gap-less steps are excluded. The hit rate is token-weighted over the
  covered steps, so it is an *achievable-cache* estimate, not the observed system hit rate.
- **Independence.** Each step's eviction is decided by its own gap; cascade effects (an eviction
  shrinking later steps' reusable context) are not modelled — matching the paper's binary rule.

## Code structure

- `load_step_arrays(con)` — session walk (reusing the `cache_hit_idle_relationship` gap logic, plus
  `output_tokens`) producing per-scope vectors `(gap, cacheable, total)` and the summed `fresh`.
- `load_generation_total_seconds(con)` — total active decode seconds per scope.
- `sweep_scope(...)` — one vectorised sweep over the shared `formatters` timeout grid: sorted-gap
  cumulative sums give `hit_rate` / `redundant_prefill` and the capped `R` in closed form.
- `plot_tradeoff_by_timeout(...)` / `plot_pareto(...)` — the two figures.

## Running it

```bash
uv run python artifacts/prefix_cache/eviction_tradeoff/analyze.py            # default merged trace
uv run python artifacts/prefix_cache/eviction_tradeoff/analyze.py --db /tmp/trace.duckdb -o /tmp/out
uv run python artifacts/prefix_cache/eviction_tradeoff/analyze.py --no-plots # CSV only
```

## Outputs (written to `-o`, default this folder)

- `eviction_tradeoff_by_scope.csv` — per `(scope, tau)`: achievable hit rate, prefill amplification,
  redundant prefill ratio, fresh floor, optimal hit rate, optimal amplification, storage ratio `R`,
  and KV active ratio.
- `eviction_tradeoff_by_timeout.{png,pdf}` — three stacked panels sharing the eviction-timeout
  x-axis: achievable hit rate, storage ratio `R`, and prefill amplification (each with its
  eviction-free optimum as a dotted reference).
- `eviction_tradeoff_pareto.{png,pdf}` — achievable hit rate vs storage ratio `R`, with eviction-time
  landmarks marked.

## SyFI result analysis

The trade-off is steep then flat. For the merged trace, raising the timeout from **1 min to 1 h**
lifts the achievable hit rate **85.4% -> 98.6%** but grows the storage ratio **R = 0.74 -> 5.07**
(~7x more suspended KV). Most of the gain is cheap: by **5 min** the hit rate is already ~94.3% at
`R ~ 1.9`; the remaining push to 1 h costs ~2.7x more storage for ~4 points.

Prefill amplification makes the waste concrete. The idealised curve **floors at 1x** (an
eviction-free perfect cache prefills only fresh) and inflates as eviction tightens: merged **18.9x at
1 min**, 7.4x at 5 min, 1.8x at 1 h. The **real** deployed cache prefills **~5.5x** the fresh minimum
(Claude 8.1x, Codex 4.0x) --- this is exactly `1 / (fresh % of append)` from the `redundant_prefill`
table. That real operating point sits on the idealised curve at an **effective eviction time of ~8
min** (Claude ~10 min, Codex ~5 min): the real prefix cache behaves like an ideal cache that evicts
after ~8 minutes of idle. So the gap-to-optimal of the prior section and the eviction sweep here are
two views of the same thing.
