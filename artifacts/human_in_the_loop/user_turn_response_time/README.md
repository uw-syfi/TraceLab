# Native completed request response time

Uses `artifacts/utils/request_timing.py`, shared with count, cost, decomposition, overall timing, and paper-summary consumers. Native Codex turn identity owns rows and lifecycle markers. Claude requires an observed response-triggering user input and an explicit final stop marker. Tool-use output is not completion.

A usable duration needs recorded start and native end, positive elapsed time, and consistent timestamp evidence. Aborted/open/missing-start/invalid requests and compressed captures are excluded. Legacy groups remain countable elsewhere but have no verified response duration. Neither next prompt nor EOF completes them. A positive internal tool duration with equal emitted/result timestamps is a wall-clock contradiction, even below one second.

Outputs preserve the existing provider summary CSV format. Empty coverage produces no duration samples, not zero-latency measurements. The decomposition audit reports lifecycle and timing coverage. Tool work is not divided by response duration; long-running calls can return beyond the model's response boundary.

Run with the standard `--db`, `--input`, and `--output-dir` CLI. Rebuild generated summaries for the intended capture.

## Request ownership coverage

The shared ownership audit reports recorded rounds that have no resolvable request owner, including their output tokens, tool-call count, known attributed tool work, and native lifecycle markers. Session and step totals retain these rows; request statistics exclude them. Their request identity and completion are not guessed. Count/cost tables include this coverage, and response-time/paper-macro outputs also write `request_ownership_coverage.md`. Session identity is always the pair `(provider, session_id)`.
