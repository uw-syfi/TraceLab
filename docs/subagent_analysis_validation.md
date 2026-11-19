# Subagent analysis follow-up validation

This note records the local validation for issues [44](https://github.com/uw-syfi/TraceLab/issues/44),
[45](https://github.com/uw-syfi/TraceLab/issues/45), and
[46](https://github.com/uw-syfi/TraceLab/issues/46) on PR
[37](https://github.com/uw-syfi/TraceLab/pull/37). The implementation was checked against the
corresponding overall analysis sections, not against an expectation that their numbers must match.

## Retained capture and extraction invariants

The validation re-extracted the same 562 native files used by
`trace/collections/upstream-parser-20261103-c28qbua6/selected-files.private.json` into the separate
ignored collection `trace/collections/analysis-fixes-20261118/`.

- Both collections contain **106,250 model rounds and 531 sessions**.
- A row-by-row comparison found **zero changes to existing round scalars, existing tool records,
  or existing timing events**. The comparison excludes only the new `session_kind` scalar and the
  added lifecycle/web-observation events and tool records.
- Native source hashes were unchanged before and after extraction.
- Tool records increased from **140,486 to 141,713**. The additional 1,227 records preserve native
  web-search call identity and known results; missing starts and durations remain null.
- Anonymous web observations remain events, not invented calls. They must not be added to call
  totals or interpreted as measured zero-duration work.
- The fresh trace retains 5,098 request-start, 5,870 request-end, and 219 abort markers. Unattached
  native evidence is counted in ingest diagnostics rather than assigned to an unrelated round.
- The new tail diagnostic reports **98 output entries across 51 files**: 53 reasoning, 44 text,
  and one tool-call entry after the last accepted usage report. These are not 98 missing rounds.
  The earlier 90-record scan reset its boundary at every syntactically valid usage record and did
  not include tool-call entries; replayed usage can be rejected by the actual extractor.

Private manifests, raw drill-down text, generated traces, and source hashes stay in ignored local
storage. No source identities or raw message text are needed to explain the aggregate results.

## One metric definition, different populations

Canonical tool work is the sum of positive per-call **internal duration when present, otherwise
call-to-result wall duration**. Both overall and subagent analyses use the shared helpers. A
runner-reported zero remains zero under that rule; it is not proof of zero physical execution.

Time with tools outstanding is a separately named union of recorded call-to-result intervals.
Concurrent calls count once in that wall metric, but separately in canonical attributed work.
Neither is a measurement of model-blocked time or accelerator computation.

Request counts and costs share request ownership with timing. Native completed request boundaries
can support a response duration. Legacy, aborted, open, conflicting, or timestamp-inconsistent
records do not acquire a fabricated completion. Known attributed work remains available even when
an exclusive wall-clock interpretation is unavailable. A tool returning after a response does not
make its entire duration a percentage of that shorter response.

The reports expose eligibility and missing coverage. A distribution and a ratio can have different
eligible samples; their sample sizes must be shown rather than inferred from adjacent averages.
Unassigned rows remain in session and step totals, and request coverage must identify their
exclusion rather than invent ownership.

In the refreshed capture, request groups own 100,459 of 106,250 rounds. The remaining 5,791
Codex rows retain 4,482 tools and 1,828.392 seconds of known tool work in session/step totals.
Request reports identify their exclusion and 33 unowned abort markers. Their 3,639,367 output
tokens are not silently assigned to a fabricated request.

## Why the count differences remain

The overall session tables include all retained sessions, including children. The detailed
subagent comparison selects delegating mains and their linked children. New population tables
also show all mains and mains outside those matched families.

| Retained population | Claude sessions | Mean rounds | Median rounds | Codex sessions | Mean rounds | Median rounds |
|---|---:|---:|---:|---:|---:|---:|
| All recorded sessions | 170 | 94.04 | 23.50 | 361 | 250.04 | 28.00 |
| All recorded mains | 45 | 270.51 | 30.00 | 167 | 505.90 | 90.00 |
| Delegating mains | 6 | 1,694.50 | 146.50 | 33 | 841.61 | 156.00 |
| Linked children | 125 | 30.51 | 23.00 | 194 | 29.78 | 21.00 |

Each main counts only its own rows. Claude's six delegating mains contain 10,167 rounds, of which
one persistent session supplies 8,322 (81.85%). Removing that one observation gives a mean of 369
for the other five. Its native message IDs confirm the count; duplicate snapshots are combined
into one round. A persistent conversation and a delegated session are not equivalent task units.

The Codex child group contains **155 recorded workers and 39 guardians**. Guardians can remain
open across many prompt/response episodes and long gaps. They should not silently be treated as
short worker tasks. Kind comes from matching native session metadata, not from session length or
whether tools appear. Legacy or conflicting kind evidence remains unknown.

Token totals use the same input accounting and shared residual policy as the corresponding
overall sections. On this capture, input totals reconcile to prefix plus append; no token-policy
change was needed to explain the different family/cohort totals.

## Why timing can still look unusual

Fast tools, runner-reported zeros, long recorded response spans, and long gaps all affect time
ratios. Native web operations can occur inside an apparent model-response span. When their starts
are absent, their duration cannot be recovered by subtracting unrelated timestamps.

The former 6.48% statistic was an average of per-child outstanding-time percentages. It was not
the child's share of family calls, and it was not directly comparable to overall summed tool
work. New reports use canonical work for the corresponding comparison, name extra wall metrics
separately, pool ratios consistently, and expose eligible workers/guardians and missing coverage.
Excluding incomplete timing also changes the sample; a new ratio is not a corrected estimate of
the full population's physical execution time.

The chart now selects call panels by call counts and time panels by summed positive tool time.
The time-ranked list includes **99.864% of known Claude matched-main tool time**, rather than
omitting roughly 88% with the old call-ranked list. Omitted call/time shares are displayed, and
axes use the displayed categories. Large `AskUserQuestion` work is therefore visible instead of
silently disappearing; it remains an interaction-tool wait, not CPU work.

## Validation procedure

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest scripts/tests web/server/tests -q -p no:cacheprovider
node --experimental-strip-types --test web/app/src/lib/*.test.ts
.venv/bin/python artifacts/run_all.py --db trace/collections/analysis-fixes-20261118/sanitized.duckdb --jobs 4
.venv/bin/python validators/run_all.py --db trace/collections/analysis-fixes-20261118/sanitized.duckdb --jobs 2
npm --prefix web/app run build
```

The full analysis run completed **51/51 experiments** and all **four validator entry points**.
The integrated test run passed **524 Python tests** and **10 web tests**. The static build produced
**69 pages**. All ten timing-dependent scripts were rerun after review fixes. Independent reviews
for extraction, timing, and comparisons found issues that were fixed and rechecked: duplicate
Claude final markers, malformed native turn IDs, hidden timing eligibility, unowned request rows,
provider-isolated session keys, and population wording. A capture-wide check confirmed that all
5,514 measurable bounded tool-occupancy intervals fit within their response duration.

The committed paper-macro snapshot belongs to a larger public corpus and is intentionally not
replaced with this local capture's output. The macro generator and its request-coverage logic were
tested locally; publishing refreshed paper values requires the corresponding full source corpus.
The static web build completed **69 pages**. The tool figure was visually inspected. The native
pipeline and read-only legacy DB paths are covered by regression tests; the browser payload copies
the same parser and shared utility sources.

Each issue owner used a separate reviewer. Reviews found and corrected repeated Claude terminal
markers, malformed turn-ID state changes, an overly broad workload introduction, hidden timing
sample populations, missing request-ownership coverage, and provider-key isolation. These checks
supplement tests; agreement between reports that share a helper is not independent proof.

## Remaining limits

Missing start timestamps, absent usage, interrupted captures, and unresolved native identity cannot
be reconstructed into exact execution measurements. Reports expose these limits instead of forcing
plausible-looking percentages. This validation covers the retained local capture, not the newer
full collection or autonomous serving-optimization traces. No dataset release or deployment is
part of these changes.
