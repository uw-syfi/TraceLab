# redundant_prefill

**Of every token a step must prefill (the uncached "append"), how much is genuinely *fresh* —
user prompts and tool results the system has never seen — versus the model's own prior output and
re-sent context that a perfect cache could serve? The fresh fraction is the irreducible prefill
floor, and so the upper bound on the achievable prefix-cache hit rate.**

## Experiment overview

Each row in the trace is one agent step with a cached `prefix_tokens` portion and a freshly
prefilled `newly_append_tokens` ("append") portion. Walking the steps of a session in order, this
experiment splits the append into **fresh** vs. **non-fresh** tokens and reports the totals per
provider and step trigger.

Method and assumptions:

- **Total input** for a step is `prefix_tokens + newly_append_tokens`. The per-step **context
  growth** is `max(0, total_input(S) - total_input(P))` for the previous step `P` seen in the same
  session — the net new context that survived into `S` (drops, i.e. compactions, contribute 0).
- **Fresh tokens.** Between `P` and `S` the context grows by the model's output at `P` (now part of
  the conversation) plus any genuinely new user/tool tokens. So
  `fresh(S) = context_growth(S) - output_tokens(P)`. `output_tokens` **already includes** reasoning
  tokens (`reasoning_output_tokens` is a subset, not an additional count — see
  `trace_facts/overview_summary`), and Codex reasoning is empirically carried into later context, so
  the full `output_tokens` is the right quantity to subtract.
- **Append (denominator).** `append(S) = newly_append_tokens(S)` — the uncached tokens actually
  prefilled at `S`. `fresh % of append = total_fresh / total_append`.
- **Pairing.** A step qualifies only when it is **not the first** step of its session and its
  **first timing event** is a `user_message` or `tool_result` (the step's *trigger*). `P` is
  whatever step was last seen for that session in trace order (`round_pk` = file order), regardless
  of its trigger — identical to `session/total_input_growth`. Session-first steps have no
  predecessor and so contribute to neither the fresh nor the append totals.
- **Triggers reported.** `all`, `user`, and `tool_result`, each per scope (`merged`, `claude`,
  `codex`).

The summed context-growth values reproduce `total_context_increase` from
`session/total_input_growth` exactly; this experiment adds the `output_tokens` subtraction and the
`append` denominator.

## Code structure

A **hybrid** experiment, like the other stateful ones: the trace DuckDB streams rows, Python keeps
the per-session sequencing.

- `read_accums(con)` — one SQL pass over `rounds` (with each round's first timing event joined in)
  ordered by `round_pk`, walked in Python with a `last_by_session` map to pair each trigger step
  with its predecessor and accumulate `append` / `context_growth` / `prior_output` per
  `(scope, trigger)`.
- `FreshAccum` — the per-group accumulator; `fresh_tokens = context_growth - prior_output`.
- `write_summary_csv(...)` / `write_latex_table(...)` — the CSV (raw integer source of truth) and
  the paper table (`\,M` token formatting, fresh % per provider).

The data layer lives in `artifacts/utils/trace_db.py` (see `artifacts/utils/DB_SCHEMA.md`); the
trigger mapping is shared with `artifacts/utils/growth.py`.

## Running it

```bash
# default merged trace (materialized to a temp DuckDB cache on first use)
uv run python artifacts/prefix_cache/redundant_prefill/analyze.py

# a prebuilt DB (run_all.py's build-db step passes this), into a chosen dir
uv run python artifacts/prefix_cache/redundant_prefill/analyze.py --db /tmp/trace.duckdb -o /tmp/out
```

## Outputs (written to `-o`, default this folder)

- `redundant_prefill_summary.csv` — per `(scope, trigger)`: events, total append, total context
  growth, total prior output, total fresh, and fresh % of append.
- `redundant_prefill_table.tex` — the paper table (`tab:redundant_prefill`), copied into the paper's
  `figure-tex/`.

## SyFI result analysis

Fresh tokens are a small slice of all prefill: only **~19%** of appended tokens are truly new
(`merged / all`), so ~81% of prefill is, in principle, cache-serviceable — the gap to optimal.
The split is sharply trigger-dependent: **user-initiated** steps are almost entirely
cache-serviceable (fresh is only ~1.7% Claude / ~4.5% Codex of their append — these steps re-send a
large window for a short new prompt), while **tool-result** steps carry the bulk of the fresh
content (~27% Claude / ~41% Codex). Codex runs hotter on fresh fraction than Claude across the
board, consistent with shorter resent windows and heavier tool output. The fresh % is the ceiling
on prefix-cache hit rate — compare it against the measured hit rates in `cache_hit_ratio`.
