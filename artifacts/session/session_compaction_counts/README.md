# session_compaction_counts

**How many context compactions does a coding session undergo?**

A *compaction* is the behavioral event the paper distinguishes from the plain size buckets in
[`total_input_growth`](../total_input_growth): when the running context nears its limit it is
summarized/dropped to a short history and then slowly re-accumulates. We detect it structurally
from the per-step total input length (`prefix_tokens + newly_append_tokens`), ordered by
`round_pk` within each session. A step `i` is a compaction when **all three** hold:

1. **Great reduction** — `total[i-1] - total[i] >= 64k` (`--min-drop-tokens`, defaults to
   `growth.MAJOR_REDUCTION_MIN_TOKENS`). Every compaction is therefore also a *major reduction*;
   compactions are the strict subset that also satisfy (2) and (3).
2. **Near the context limit** — the pre-drop level `total[i-1]` is at least `--near-max-ratio`
   (0.75) of the session's observed max total input. The drop happens near the session's peak,
   not at a small early dip.
3. **Recovers slowly** — the context does *not* rebound to `--rebound-ratio` (0.75) of the
   pre-drop level within the next `--rebound-steps` (3) steps, and at least one step follows.
   A drop that immediately snaps back is a branch/edit artifact, not a compaction.

Each compaction is attributed to the trigger of step `i`, using the same
**user-initiated** / **tool-initiated** split the rest of the paper uses: `user_message` →
user-initiated (an explicit `/compact` or a new request that forced summarization);
`tool_result` → tool-initiated (auto-compaction mid-loop).

## Running it

```bash
# pinned public trace
uv run python artifacts/session/session_compaction_counts/analyze.py -i trace/syfi_coding_trace.jsonl

# default merged trace
uv run python artifacts/session/session_compaction_counts/analyze.py

# loosen/tighten the definition
uv run python artifacts/session/session_compaction_counts/analyze.py \
    --near-max-ratio 0.8 --rebound-steps 5
```

## Outputs

- `session_compaction_counts.tex` — the merged summary table (`tab:session_compaction`).
- stdout — merged + per-provider (Claude / Codex): total compactions, share of sessions with
  ≥1, the per-session distribution (avg / p25 / p50 / p90 / p99, over all sessions and over
  only those with ≥1), and the user-initiated-vs-tool-initiated trigger split.

## Headline numbers (public trace, default criteria)

- Of **1,630** major reductions (≥64k drops), **1,519** (93.2%) qualify as compactions.
- **9.7%** of the 4,265 sessions undergo at least one compaction.
- Overwhelmingly **tool-initiated** (86.5%, mid-loop) rather than user-initiated.
- Far more common in **Codex** (18.4% of sessions, 1,235 events) than **Claude** (4.5%, 284).
- Among sessions that compact at all, the mean is 3.7 and the tail is long (Codex p99 = 34).

No figures.
