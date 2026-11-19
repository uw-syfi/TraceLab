# Request lifecycle and timing audit

Uses the shared `artifacts/utils/request_timing.py` request ownership used by count, cost, response-time, overall timing, and paper-summary consumers. Native completed lifecycles yield durations; legacy, open, aborted, missing-start, invalid, and compressed captures remain visible as coverage categories. Next prompt and EOF do not fabricate completion.

Canonical attributed tool work sums positive internal-else-wall durations and includes interaction tools. It is separate from bounded tool-outstanding wall occupancy. Missing endpoints and anonymous native tool observations make occupancy unavailable. Work may outlive a model response or overlap other calls; subtracting summed work from response time is not a physical compute decomposition.

`result_analysis.md` reports request statuses, measurable durations, missing tool timing/observations, and compressed captures. It intentionally replaces the old residual formula based on last model output. Raw source content and session IDs are not exported.

Run with the standard `--db`, `--input`, and `--output-dir` CLI. The small per-round loading helpers remain available to existing analysis consumers.

## Request ownership coverage

The shared ownership audit reports recorded rounds that have no resolvable request owner, including their output tokens, tool-call count, known attributed tool work, and native lifecycle markers. Session and step totals retain these rows; request statistics exclude them. Their request identity and completion are not guessed. Count/cost tables include this coverage, and response-time/paper-macro outputs also write `request_ownership_coverage.md`. Session identity is always the pair `(provider, session_id)`.
