# artifacts/utils

Shared "common helpers" imported by the per-experiment analysis/plotting scripts
under `artifacts/<category>/<experiment>/`. This folder holds **no experiments and
produces no outputs** — only reusable library code.

## What lives here vs. in the experiment

The **payload** of a figure — the function that renders one specific plot or writes one
specific CSV — lives **in that experiment's own `plot.py`/`analyze.py`**, not here. This
folder holds only the **primitives** that more than one experiment reuses (the loader,
accumulators, formatters, generic CDF renderers, …). A function used by exactly one
experiment belongs in that experiment; a function reused by several belongs here.

## How experiments import it

Every experiment script adds this folder to `sys.path` and imports the **specific names**
it needs from the cohesive modules (mirroring the repo's existing sibling-import style):

```python
import sys
from pathlib import Path

EXP_DIR = Path(__file__).resolve().parent
REPO_ROOT = EXP_DIR.parents[2]                 # exp -> category -> artifacts -> repo
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))

import png_sidecar                             # fuse README+data+code into PNGs
from trace_loader import add_common_loader_args, load_trace_from_args
from style import save_plot, plot_color, provider_order
from formatters import apply_binary_token_axis, format_latency_tick
from cdf import plot_count_cdf_by_provider     # generic, shared by several experiments
# from accumulators import ReservoirSampler, numeric_sample
```

By convention each experiment defaults to `trace/syfi_coding_trace.duckdb` and writes to its own
folder (`Path(__file__).resolve().parent`). JSONL is only a compatibility override for the
materialization boundary.

## Modules

Layered so there are no import cycles — the base modules have no intra-`utils`
dependencies; the composed modules import only from base ones.

**Base (Layer 0):**

- **`style.py`** — the single matplotlib gateway: runs `configure_matplotlib_cache()`
  *before* importing matplotlib (other modules pull `plt`/`mticker`/`mpatches` from
  here), the color palette + `rcParams`, and the shared plotting primitives `save_plot`,
  `polish_axes`, `plot_color`, `provider_title`, `provider_order`, `short_label`.
- **`accumulators.py`** — streaming accumulators/samplers (`TokenGroup`, `ToolStats`,
  `NumericTracker`, `ReservoirSampler`, `AppendTokenBinStats`, `ToolLatencyBinStats`),
  numeric helpers (`numeric_sample`, `sample_percentiles`, …), the token/latency bin
  tables + `make_*_bins`, `selected_token_groups`, and the field extractors
  `group_key`/`tool_name`/`tool_latency_ms`.
- **`timing.py`** — timing-event helpers (`parse_ts`,
  `response_trigger_user_message_timestamp`, `last_model_output_timestamp`,
  `input_to_last_output_span_seconds`, …) and the `*_EVENT_TYPES` sets.
- **`formatters.py`** — token log-axis (`format_token_label`, `apply_binary_token_axis`),
  latency/duration/count/hours tick formatters, and the bin/threshold generators
  (`fine_latency_bin_edges`, `kv_cache_timeout_thresholds_seconds`, …) + their constants.
- **`token_policies.py`** — shared provider/model eligibility for prior-output subtraction.
  Adjusted append, token spindles, and report-context changes keep their own formulas.
- **`tool_metrics.py`** — shared SQL aggregates for call counts, known/unknown outcomes,
  and positive/missing/nonpositive effective latency. Callers choose their cohort,
  grouping, name normalization, error-rate denominator, and empty-sum behavior.

**Composed (Layer 1):**

- **`tool_stats.py`** — provider/tool aggregation, rare-tool collapsing, and
  `plot_ready_tool_stats_by_provider`.
- **`cdf.py`** — the **generic** by-provider renderers shared across experiments:
  `plot_count_cdf_by_provider`, `plot_cumulative_duration_cdf_by_provider`, their
  `write_*` CSV twins, plus `plot_stacked_share_panels` / `active_bin_mask` /
  `annotate_cumulative_time_reference`.
- **`trace_loader.py`** — `load_trace(...)` (one streaming pass that builds every
  aggregate) and the shared driver helpers `add_common_loader_args` /
  `load_trace_from_args` / `json_ready` + the default path constants.
- **`command_chains.py`** — reconstructs Codex `exec_command` → `write_stdin` chains from the
  minimal normalized linkage fields and derives observed finish time, full wall time, and summed
  per-call tool time.

**Standalone:**

- **`subagent_cohorts.py`** — shared SQL for matched main/descendant families and
  percentile summaries that distinguish empty samples from measured zeros.
- **`png_sidecar.py`** — fuses an experiment's README, source CSV data, and plotting
  code into its PNGs as compressed text chunks, so each figure is self-contained.
  `make_self_contained(out_dir, code_files=[Path(__file__), *png_sidecar.util_code_files()],
  readme_path=...)` is the standard final step of every plotting driver — `util_code_files()`
  returns this whole shared library so every PNG carries its own script + every module it
  builds on. The CLI (`list`/`show`/`extract`) reads the embedded content back out.
- **`growth.py`** — same-session total-input growth stats (moved from
  `scripts/total_input_growth.py`); consumed by `trace_facts/overview_summary/` and
  `session/total_input_growth/`.

## Key metric definitions (single source of truth)

These are the definitions every experiment README points back to:

- **effective tool latency** = `tool_internal_latency_ms` when present, else
  `tool_wall_latency_ms` (= `result_at − emitted_at`).
- **time with tool calls outstanding** = the union of recorded `emitted_at` → `result_at`
  intervals; concurrent intervals count once. This wall time differs from summed effective
  latencies, which count each call separately and may use internal durations.
- **Codex command wall time** = initial `exec_command.emitted_at` → the first linked result whose
  `command_status` is `finished` (the terminal `write_stdin` for a continued command).
- **Codex command tool-call sum** = effective latency of the initial `exec_command` plus every
  linked `write_stdin`; unlike command wall time, this excludes gaps between polls.
- **observable generation time** = latest input event (`user_message`/`tool_result`)
  → last model-output event (`reasoning`/`text`/`tool_call`) in the round.
- **human input wait** = previous same-session model-output event → the next
  `user_message`.
- **user-turn response time** = response-triggering `user_message` → last
  response-end event before the next response-triggering `user_message`.
- **prefix hit ratio** = `prefix_tokens / (prefix_tokens + newly_append_tokens)`.
- **adjusted append** = `newly_append_tokens − prior-round output tokens`.
- **KV active ratio(T)** = `gen_total / (gen_total + tool_total≤T + human_wait_total≤T)`
  for an eviction timeout `T`.
- **growth buckets**: micro-reduction `< 1024` tokens, major-reduction `≥ 50000` tokens.
