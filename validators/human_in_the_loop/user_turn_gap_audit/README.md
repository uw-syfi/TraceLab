# Shared request lifecycle validation

This validator runs the same request lifecycle and coverage audit as `artifacts/human_in_the_loop/user_turn_decomposition`. Native turn identity and completion evidence determine request duration. Legacy user-triggered groups stay countable but do not receive fabricated completion at the next prompt or EOF.

Canonical tool work is attributed internal-else-wall work, including interaction tools. It is not a wall-clock partition or an algebraic explanation of response time. Missing endpoints, anonymous tool observations, and contradictory timestamps remain explicit coverage gaps. The audit emits `result_analysis.md` using the standard `--db`, `--input`, and `--output-dir` CLI.
