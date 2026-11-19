# Subagent tokens and reports

Compare recorded tokens in a main session and its subagents, together called a **family**. Input totals count repeated context across rounds; output already includes reasoning.

Input changes around subagent replies **are not exact report-token counts**. Claude and Codex record different kinds of reply evidence, explained beside those tables.

**Avg** is the average; P25/P50/P90/P99 are percentiles, with P50 the median. **N/A** means unavailable, not zero.

## Run and outputs

The script writes `tokens_and_reports.md` and `headline.json`. Headline labels identify each provider's median input or output tokens per complete family, using the existing `{label, value}` structure. It exports no report
bodies, identifiers, per-transition records, or text-derived counts.

```bash
uv run python artifacts/subagents/tokens_and_reports/analyze.py --db trace/syfi_coding_trace.duckdb
# Or materialize a normalized trace through the shared CLI:
uv run python artifacts/subagents/tokens_and_reports/analyze.py -i trace/sample.jsonl -o /tmp/subagent-tokens
```

## Six questions

1. **How much recorded input do mains and descendants use per family?** Sum
   `input_tokens_total` over each role's retained rounds, then sum the two role
   totals for the family. Show pooled totals and separate per-family Avg/P25/P50/P90/P99 columns with provider rows. The complete-family count appears in coverage, not in every distribution.
2. **How much recorded output does each role use per family?** Apply the same
   calculation to `output_tokens`. Reasoning is already included; never add
   `reasoning_output_tokens` again or substitute child output for a returned report.
3. **How often do descendants account for most usage?** Separately for input and
   output, count complete families where descendant total > main total, divided
   by complete families whose combined total is positive. Ties are not majorities;
   zero-total families are excluded from this fraction, not from total statistics.
4. **How do first and peak observed input sizes compare by role?** Each session
   contributes its first retained round's input, ordered by `round_index` then
   `ingest_seq`, nulls last. A missing/negative first input stays unavailable; it is
   not replaced by a later valid value. Peak is the maximum available nonnegative
   input. Each distribution's Sessions column counts available measurements; compare it with the total sessions in coverage to find exclusions. Also show valid/retained input rounds and sessions with incomplete input coverage, whose observed peaks may understate actual peaks.
5. **What is the input change at a report-associated round?** For
   a possible or completed reply described under Report evidence and consumed by a later recipient round, compute
   `delta = I_current - I_previous`, using the immediately preceding recipient
   round, not the spawn/tool-emitting round. Show signed distributions; the Round pairs column counts eligible consecutive round pairs.
6. **What is the input change after subtracting prior output?**
   Compute `residual = delta - O_previous` only under the existing
   `adjusted_prefix_append` **claude-and-gpt55** policy: Claude, or Codex with model
   exactly `gpt-5.5`. Both inputs and previous output must be positive, with valid
   recorded usage. Other Codex models retain eligible deltas but have unavailable
   residuals. Negative deltas and residuals are never clipped.

In these reference formulas, `I_current` is the consuming recipient round's recorded input,
`I_previous` is the immediately preceding recipient round's input, and `O_previous` is that
preceding round's output, including reasoning. `delta` is the input difference;
`residual` is that difference after subtracting prior output. Neither isolates report content.

**Exact report-only token counts are not identifiable from retained usage.** The
last two metrics are derived from recorded usage, not report-body token counts.
They include other context changes; even an apparently single return does not
establish report-only isolation. No tokenizer, character count, token-count API,
or characters-to-tokens estimate is used.

## Cohort, denominators, and missing values

Use the shared `subagent_cohorts.COHORT_SQL`: positively known delegating mains
with resolved self-roots and their resolved descendants. Unknown-origin root anchors and unresolved
families are not silently promoted to mains; a linked descendant may itself have unknown origin. A family is a provider/root pair;
all metrics and headline entries remain provider-specific.

Main origin comes from provider source or file layout, not incoming links. Each main must be confirmed as its own root and have at least one linked subagent. Descendants include subagents launched through other subagents. One recorded round is one retained LLM row, not a user request.

Rounds retain their surrogate `round_pk`; duplicate source IDs or round indices
are **not** deduplicated for usage totals. Tools are not joined into those sums,
so tool fanout cannot multiply tokens. A family's input total is eligible only
when **every retained family round** has a nonnegative recorded input value;
output eligibility is independent and requires every output value to be valid.
The main, descendant, and combined distributions therefore use the same complete
families for each metric. Tables show excluded families and invalid/missing round
counts. First/peak statistics instead use the session-level coverage described
above, not the complete-family filter.

Input sums measure repeated recorded context processing, not unique tokens,
uncached-only input, or cost. A real recorded zero remains zero. Empty samples
produce `N/A` statistics (JSON headline value `N/A`), never a fabricated zero.
Family completeness concerns **retained** rounds only; dropped/unrecorded rounds
and missing sessions cannot be recovered.

## Report evidence and actual recipients

- **Codex — supported wait receipt:** `report_source=codex_wait_completed`, with
  a nonempty, unique list of `codex:<canonical lowercase UUID>` child IDs in
  `report_session_ids`. This denotes
  the supported legacy `wait` completed-status receipt, not spawn, timeout,
  running state, generic task completion, or mailbox readiness.
- **Claude — candidate only:** `report_source=claude_agent_task_result` identifies
  paired Agent/Task results. They are not confirmed completed reports. Child IDs
  must be null; no ID or completion state is fabricated. Candidate-only
  transitions are displayed alongside, never pooled with, Codex receipts.
- The actual recipient is the session owning the tool call **and** consuming the
  linked `tool_result`. Known delegating-main recipients are primary; descendant
  recipients are reported separately as intermediate. The ultimate family root
  is never used to relabel an intermediate recipient as main. Recipients outside
  the matched cohort receive coverage counts only.

Analysis reuses `trace_privacy.validate_report_fields` for these two annotations,
including direct `--input` materialization that bypasses upstream sanitization.
Invalid sources, wrong-provider IDs, non-UUID or duplicate Codex IDs, and nonnull
Claude candidate IDs are excluded with **invalid report metadata** coverage, not
a failed analysis. Self-references are additionally excluded. This check does
not validate or reinterpret all lineage fields.

Link a result through `timing_events.event_type='tool_result'` and `tool_call_id`
within the **same provider and session**, joining both tools and timing events
through their owning `round_pk`. Require a nonempty call ID occurring exactly
once in that recipient session and exactly one distinct consuming round.
Repeated markers in the same round do not create another consumer, but must agree
on their arrival timestamp. A result in
the emitting round, an earlier round, or without indexed ordering is ineligible;
no global timestamp heuristic or adjacency-to-spawn assumption is used.

Association also requires actual timing evidence: the linked `tool_result`
timestamp, tool emission time, and the consumer's first model-output timestamp
(`reasoning`, `text`, or `tool_call`, using the earliest timestamp). All consumer
model-output events must have timestamps so the earliest is established. Missing,
unparseable, or non-finite times are unavailable; conflicting result times, a result before
tool emission, or model output before tool emission are impossible timelines.
Report arrival must be **at or before** first model output. A later arrival is
not input evidence for a response already underway. The timing list's ordering
is not evidence: Codex extraction can prepend pending results to model events
that actually occurred earlier. Failed timing checks retain receipt coverage but
produce no delta/residual, and never shift the receipt to another round.

For a consuming round of index `k`, require exactly one round at `k` and exactly
one at `k-1`. Both must have the same nonempty model and consistent nonnegative
`I=input_tokens_total`, `P=prefix_tokens`, `A=newly_append_tokens`, with `I=P+A`.
The split between cached prefix and append may change without affecting delta.
Residual eligibility additionally requires positive inputs and positive previous
`output_tokens`; absent usage represented as zero is not residual evidence.
Output is deducted once, including recorded reasoning, under the policy above.

Known `is_error=true` on the tool or its linked result excludes the return from
statistics, with coverage retained. Multiple receipts, child IDs, or tool results
in one consuming round contribute **one transition**, not one sample per entry.
Other messages/results, previous output, context removal, truncation, and
compaction may affect context change. Claude mixed-input markers are incomplete,
so there is no supposedly isolated single-input subset.

## Coverage and recording limits

Coverage distinguishes inspected tools without evidence, annotated entries,
invalid report metadata, known errors, missing/ambiguous call
IDs, missing/ambiguous consumers, nonlater consumers, unmatched recipients,
unavailable or impossible/conflicting consumption timing, arrivals after first model output,
unique transitions, missing/duplicate consecutive rounds, model mismatches,
invalid input usage, and residual policy/positive-usage exclusions. Entry counts
use the first failing check; transition-level checks follow deduplication.
These denominators differ intentionally. A receipt without a consuming round
remains visible in coverage but has no measured context delta.

The DB supplies nullable report columns even for legacy data. All-null or zero
annotated entries means **No report evidence**, not proof of zero returns; merely
having columns does not establish extraction coverage. Extraction may need to be
rerun to add evidence. Supported receipt annotations still do not prove the full
stored report text equals the model's actual input, nor expose its exact usage.
Timing checks use materialized DB values; they cannot reconstruct timestamps
already changed by upstream JSON inference or coercion.
These synthetic checks validate accounting, not real-corpus representativeness.

## Validation

```bash
uv run --no-sync --with pytest python -m pytest scripts/tests/test_subagent_tokens.py
```

Tests materialize compact normalized fixtures with valid timestamps and canonical
Codex IDs through the shared DB, covering
provider separation, complete-family filtering, retained duplicate rounds,
first/peak coverage, late consumption, batching, ambiguous evidence, nesting,
signed residuals, cache splits, exact-model policy, and empty versus zero.
Synthetic raw Codex fixtures also exercise extractor → sanitizer → materializer →
analysis for both correctly ordered returns and reports arriving after model output
has begun. Direct materialization regressions cover invalid report annotations.
