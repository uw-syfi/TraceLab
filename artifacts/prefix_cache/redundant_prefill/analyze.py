#!/usr/bin/env python3
"""Fresh prefill tokens vs. total appended (prefilled) tokens per session step.

Every uncached "append" token a step prefills is either (a) a *fresh* token the system has
never seen — a user prompt or tool result — or (b) a token it has produced/seen before (the
model's own prior output, plus re-sent context). Only the fresh tokens are an irreducible
prefill floor; the rest is, in principle, cache-serviceable. This experiment measures the
fresh fraction, which upper-bounds the achievable prefix-cache hit rate.

Definition (per step ``S`` paired with the previous step ``P`` in the same session):

  * ``append(S)``        = ``newly_append_tokens(S)`` — the uncached tokens prefilled at S.
  * ``context_growth(S)``= ``max(0, total_input(S) - total_input(P))`` where
    ``total_input = prefix_tokens + newly_append_tokens`` — net new context that survived into S.
  * ``prior_output(S)``  = ``output_tokens(P)`` — tokens the model generated at P that are now
    part of S's context (``output_tokens`` already *includes* reasoning tokens; see
    ``trace_facts/overview_summary`` — reasoning is a subset, not an additional count).
  * ``fresh(S)``         = ``context_growth(S) - prior_output(S)`` — the genuinely new
    user/tool tokens entering the context.

Aggregated per ``(scope, trigger)``:

  * ``total_fresh   = sum(context_growth) - sum(prior_output)``
  * ``total_append  = sum(append)``
  * ``fresh_pct_of_append = total_fresh / total_append``

Pairing follows ``session/total_input_growth`` exactly: a step qualifies when it is not the
first in its session and its FIRST timing event is a ``user_message`` or ``tool_result`` (the
*trigger*); ``P`` is whatever step was last seen for that session in file order (``round_pk``).
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # experiment -> category -> artifacts -> repo root

sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db  # noqa: E402
from growth import TRIGGER_LABELS  # noqa: E402  (user_message->user, tool_result->tool_result)

DEFAULT_OUTPUT_DIR = SCRIPT_DIR
SCOPES = ("merged", "claude", "codex")
TRIGGERS = ("all", "user", "tool_result")

# (scope key, column header) and (trigger key, row-group label) for the LaTeX table.
TABLE_SCOPES = (("claude", "Claude"), ("codex", "Codex"), ("merged", "Total"))
TABLE_TRIGGERS = (
    ("all", "Overall"),
    ("user", "User-initiated"),
    ("tool_result", "Tool-result"),
)


@dataclass
class FreshAccum:
    events: int = 0
    append_tokens: int = 0
    context_growth_tokens: int = 0
    prior_output_tokens: int = 0

    def add(self, append: int, context_growth: int, prior_output: int) -> None:
        self.events += 1
        self.append_tokens += append
        self.context_growth_tokens += context_growth
        self.prior_output_tokens += prior_output

    @property
    def fresh_tokens(self) -> int:
        return self.context_growth_tokens - self.prior_output_tokens

    @property
    def fresh_pct_of_append(self) -> float | None:
        return self.fresh_tokens / self.append_tokens if self.append_tokens else None

    @property
    def amplification(self) -> float | None:
        """Total prefilled (append) tokens per irreducible fresh token = 1 / fresh_pct."""
        return self.append_tokens / self.fresh_tokens if self.fresh_tokens else None


def _int_or_zero(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def read_accums(con) -> dict[tuple[str, str], FreshAccum]:
    """Walk rounds in file order, pairing each trigger step with its session predecessor.

    Mirrors ``session/total_input_growth``: one SQL pass in ``round_pk`` (= file) order with the
    per-round first timing event joined in, then Python keeps the ``last_by_session`` state so a
    step's predecessor is whatever round was last seen for that session — exactly the line-order
    sequencing the pre-DuckDB scan relied on.
    """
    rows = con.execute(
        """
        WITH first_ev AS (
            SELECT round_pk, event_type
            FROM timing_events
            WHERE event_index = 1
        )
        SELECT r.round_pk        AS round_pk,
               r.session_id      AS session_id,
               r.provider        AS provider,
               r.prefix_tokens   AS prefix_tokens,
               r.newly_append_tokens AS append_tokens,
               r.output_tokens   AS output_tokens,
               f.event_type      AS event_type
        FROM rounds r LEFT JOIN first_ev f USING (round_pk)
        ORDER BY r.round_pk
        """
    ).fetchall()

    accums: dict[tuple[str, str], FreshAccum] = defaultdict(FreshAccum)
    last_by_session: dict[str, dict[str, int]] = {}
    for round_pk, session_id, provider, prefix, append, output, event_type in rows:
        prefix = _int_or_zero(prefix)
        append = _int_or_zero(append)
        output = _int_or_zero(output)
        total_input = prefix + append

        if (
            isinstance(session_id, str)
            and session_id in last_by_session
            and event_type in TRIGGER_LABELS
        ):
            previous = last_by_session[session_id]
            raw_delta = total_input - previous["total_input"]
            context_growth = raw_delta if raw_delta > 0 else 0
            prior_output = previous["output_tokens"]
            trigger = TRIGGER_LABELS[event_type]
            scope_provider = provider if isinstance(provider, str) else "unknown"
            for scope in ("merged", scope_provider):
                accums[(scope, trigger)].add(append, context_growth, prior_output)
                accums[(scope, "all")].add(append, context_growth, prior_output)

        if isinstance(session_id, str):
            last_by_session[session_id] = {
                "total_input": total_input,
                "output_tokens": output,
            }
    return dict(accums)


def write_summary_csv(path: Path, accums: dict[tuple[str, str], FreshAccum]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "scope",
        "trigger",
        "events",
        "total_append_tokens",
        "total_context_growth_tokens",
        "total_prior_output_tokens",
        "total_fresh_tokens",
        "fresh_pct_of_append",
    ]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for scope in SCOPES:
            for trigger in TRIGGERS:
                acc = accums.get((scope, trigger), FreshAccum())
                pct = acc.fresh_pct_of_append
                writer.writerow(
                    {
                        "scope": scope,
                        "trigger": trigger,
                        "events": acc.events,
                        "total_append_tokens": acc.append_tokens,
                        "total_context_growth_tokens": acc.context_growth_tokens,
                        "total_prior_output_tokens": acc.prior_output_tokens,
                        "total_fresh_tokens": acc.fresh_tokens,
                        "fresh_pct_of_append": (
                            f"{pct * 100:.2f}%" if pct is not None else ""
                        ),
                    }
                )


def _fmt_millions(value: int) -> str:
    return f"{value / 1e6:,.1f}\\,M"


def _fmt_pct(value: float | None) -> str:
    return "--" if value is None else f"{value * 100:.1f}\\%"


def _fmt_amp(value: float | None) -> str:
    return "--" if value is None else f"{value:.1f}$\\times$"


def write_latex_table(path: Path, accums: dict[tuple[str, str], FreshAccum]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metric_rows = (
        ("Total append tokens", lambda a: _fmt_millions(a.append_tokens)),
        ("Total fresh tokens", lambda a: _fmt_millions(a.fresh_tokens)),
        ("Fresh \\% of append", lambda a: _fmt_pct(a.fresh_pct_of_append)),
        ("Prefill amplification", lambda a: _fmt_amp(a.amplification)),
    )
    lines = [
        "% ==========================================================================",
        "% Data source: TraceLab/artifacts/prefix_cache/redundant_prefill/analyze.py",
        "%   (fresh = per-step context growth - prior step output tokens; re-run to refresh).",
        "% ==========================================================================",
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Fresh prefill versus total prefilled (append) tokens, by provider and step "
        "trigger. \\emph{Fresh} tokens are the per-step context growth minus the prior step's "
        "output; \\emph{prefill amplification} is append${}/{}$fresh.}",
        "\\label{tab:redundant_prefill}",
        "\\small",
        "\\setlength{\\tabcolsep}{6pt}",
        "\\renewcommand{\\arraystretch}{1.15}",
        "\\begin{tabular}{l r r r}",
        "\\toprule",
        "\\textbf{Metric} & \\textbf{Claude} & \\textbf{Codex} & \\textbf{Total} \\\\",
        "\\midrule",
    ]
    for t_index, (trigger, group_label) in enumerate(TABLE_TRIGGERS):
        if t_index > 0:
            lines.append("\\addlinespace")
        lines.append(f"\\multicolumn{{4}}{{@{{}}l}}{{\\emph{{{group_label}}}}} \\\\")
        for metric_label, fn in metric_rows:
            cells = [
                fn(accums.get((scope, trigger), FreshAccum()))
                for scope, _ in TABLE_SCOPES
            ]
            lines.append(
                f"\\quad {metric_label} & {cells[0]} & {cells[1]} & {cells[2]} \\\\"
            )
    lines.extend(["\\bottomrule", "\\end{tabular}", "\\end{table}", ""])
    path.write_text("\n".join(lines))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    trace_db.add_db_args(parser, default_output_dir=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    con = trace_db.open_from_args(args)
    accums = read_accums(con)

    summary_csv = output_dir / "redundant_prefill_summary.csv"
    latex_table = output_dir / "redundant_prefill_table.tex"
    write_summary_csv(summary_csv, accums)
    write_latex_table(latex_table, accums)
    print(f"summary_csv={summary_csv}")
    print(f"latex_table={latex_table}")

    merged_all = accums.get(("merged", "all"), FreshAccum())
    print(
        f"events={merged_all.events} "
        f"append={merged_all.append_tokens} fresh={merged_all.fresh_tokens}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
