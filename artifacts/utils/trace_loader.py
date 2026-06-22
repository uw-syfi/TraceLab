"""Single-pass trace loader plus the shared argparse/loader driver helpers."""

from __future__ import annotations

from typing import Any
from collections import Counter
from pathlib import Path
import argparse
from datetime import datetime
import json
import sys

from accumulators import (
    AppendTokenBinStats,
    ReservoirSampler,
    TokenGroup,
    ToolLatencyBinStats,
    ToolStats,
    group_key,
    in_half_open_bin,
    make_append_token_bins,
    make_tool_latency_bins,
    safe_float,
    tool_latency_ms,
    tool_name,
)
from timing import (
    human_input_wait_seconds_for_row,
    input_to_last_output_span_seconds,
    last_response_end_timestamp,
    response_trigger_user_message_timestamp,
)

MODULE_DIR = Path(__file__).resolve().parent  # artifacts/utils


REPO_ROOT = MODULE_DIR.parents[1]  # utils -> artifacts -> repo root


TRACE_DIR = REPO_ROOT / "trace"


ARTIFACTS_DIR = REPO_ROOT / "artifacts"


DEFAULT_INPUT = TRACE_DIR / "llm_round_trace.merged.all_users.jsonl"


def load_trace(
    input_path: Path,
    *,
    group_by: str,
    token_sample_size: int,
    pair_sample_size: int,
    per_tool_sample_size: int,
    seed: int,
    progress_every: int,
) -> dict[str, Any]:
    token_groups: dict[str, TokenGroup] = {}
    tool_stats: dict[str, ToolStats] = {}
    tool_stats_by_provider: dict[str, dict[str, ToolStats]] = {}
    provider_seed_offsets: dict[str, int] = {}
    pair_sampler = ReservoirSampler(pair_sample_size, seed + 100_000)
    append_token_bins = make_append_token_bins()
    append_token_bins_by_provider: dict[str, list[AppendTokenBinStats]] = {}
    tool_latency_bins = make_tool_latency_bins()
    tool_latency_bins_by_provider: dict[str, list[ToolLatencyBinStats]] = {}
    tool_latency_values_by_provider: dict[str, list[float]] = {}
    provider_counts: Counter[str] = Counter()
    model_counts: Counter[str] = Counter()
    human_input_wait_seconds_by_provider: dict[str, list[float]] = {"all": []}
    llm_generation_seconds_by_provider: dict[str, list[float]] = {}
    user_turn_response_seconds_by_provider: dict[str, list[float]] = {"all": []}
    last_event_at_by_session: dict[str, datetime] = {}
    current_user_turn_by_session: dict[str, dict[str, Any]] = {}

    rows = 0
    bad_json = 0
    rows_with_tools = 0
    tool_calls = 0

    def get_token_group(key: str) -> TokenGroup:
        if key not in token_groups:
            token_groups[key] = TokenGroup(
                token_sample_size, seed + len(token_groups) * 10
            )
        return token_groups[key]

    def get_tool_stats(name: str) -> ToolStats:
        if name not in tool_stats:
            tool_stats[name] = ToolStats(
                per_tool_sample_size, seed + 200_000 + len(tool_stats)
            )
        return tool_stats[name]

    def get_provider_tool_stats(provider: str, name: str) -> ToolStats:
        provider_stats = tool_stats_by_provider.setdefault(provider, {})
        if provider not in provider_seed_offsets:
            provider_seed_offsets[provider] = len(provider_seed_offsets) * 10_000
        if name not in provider_stats:
            provider_stats[name] = ToolStats(
                per_tool_sample_size,
                seed + 300_000 + provider_seed_offsets[provider] + len(provider_stats),
            )
        return provider_stats[name]

    def get_provider_append_bins(provider: str) -> list[AppendTokenBinStats]:
        if provider not in append_token_bins_by_provider:
            append_token_bins_by_provider[provider] = make_append_token_bins()
        return append_token_bins_by_provider[provider]

    def get_provider_tool_latency_bins(provider: str) -> list[ToolLatencyBinStats]:
        if provider not in tool_latency_bins_by_provider:
            tool_latency_bins_by_provider[provider] = make_tool_latency_bins()
        return tool_latency_bins_by_provider[provider]

    def close_user_turn(session_id: str) -> None:
        state = current_user_turn_by_session.pop(session_id, None)
        if not state:
            return
        start_at = state.get("start_at")
        last_output_at = state.get("last_output_at")
        provider = state.get("provider")
        if not isinstance(start_at, datetime) or not isinstance(
            last_output_at, datetime
        ):
            return
        if not isinstance(provider, str):
            provider = "<unknown-provider>"
        duration_seconds = (last_output_at - start_at).total_seconds()
        if duration_seconds <= 0:
            return
        user_turn_response_seconds_by_provider["all"].append(duration_seconds)
        user_turn_response_seconds_by_provider.setdefault(provider, []).append(
            duration_seconds
        )

    with input_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                bad_json += 1
                continue
            if not isinstance(row, dict):
                bad_json += 1
                continue

            rows += 1
            provider = str(row.get("provider") or "<unknown-provider>")
            model = str(row.get("model") or "<unknown-model>")
            provider_counts[provider] += 1
            model_counts[model] += 1

            generation_seconds = input_to_last_output_span_seconds(row)
            if generation_seconds is not None:
                llm_generation_seconds_by_provider.setdefault(provider, []).append(
                    generation_seconds
                )

            session_id = row.get("session_id")

            # Human wait: gap from the previous event of any type to each user_message in this row
            # (provider-agnostic; see timing.human_input_wait_seconds_for_row). Independent of the
            # turn state machine below, which still keys on the response-triggering user message.
            if isinstance(session_id, str):
                waits, _n_user, last_event_at = human_input_wait_seconds_for_row(
                    row, last_event_at_by_session.get(session_id)
                )
                for wait_seconds in waits:
                    human_input_wait_seconds_by_provider["all"].append(wait_seconds)
                    human_input_wait_seconds_by_provider.setdefault(provider, []).append(
                        wait_seconds
                    )
                if last_event_at is not None:
                    last_event_at_by_session[session_id] = last_event_at

            user_message_start_at = response_trigger_user_message_timestamp(row)
            if user_message_start_at is not None and isinstance(session_id, str):
                close_user_turn(session_id)
                current_user_turn_by_session[session_id] = {
                    "provider": provider,
                    "start_at": user_message_start_at,
                    "last_output_at": None,
                }

            prefix = safe_float(row.get("prefix_tokens"))
            append = safe_float(row.get("newly_append_tokens"))
            key = group_key(row, group_by)
            get_token_group("all").add(
                row.get("prefix_tokens"),
                row.get("newly_append_tokens"),
                row.get("output_tokens"),
            )
            get_token_group(key).add(
                row.get("prefix_tokens"),
                row.get("newly_append_tokens"),
                row.get("output_tokens"),
            )
            if (
                prefix is not None
                and prefix >= 0
                and append is not None
                and append >= 0
            ):
                pair_sampler.add((key, prefix, append))
                for bin_stats in append_token_bins:
                    if in_half_open_bin(
                        append, bin_stats.lo_tokens, bin_stats.hi_tokens
                    ):
                        bin_stats.add(append)
                        break
                for bin_stats in get_provider_append_bins(provider):
                    if in_half_open_bin(
                        append, bin_stats.lo_tokens, bin_stats.hi_tokens
                    ):
                        bin_stats.add(append)
                        break

            tools = row.get("tools")
            if isinstance(tools, list) and tools:
                rows_with_tools += 1
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    tool_calls += 1
                    name = tool_name(tool.get("tool_name"))
                    get_tool_stats(name).add(tool, provider)
                    get_provider_tool_stats(provider, name).add(tool, provider)
                    latency = tool_latency_ms(tool)
                    if latency is not None and latency > 0:
                        tool_latency_values_by_provider.setdefault(provider, []).append(
                            latency
                        )
                        for bin_stats in tool_latency_bins:
                            if in_half_open_bin(
                                latency, bin_stats.lo_ms, bin_stats.hi_ms
                            ):
                                bin_stats.add(
                                    latency,
                                    is_error=tool.get("is_error") is True,
                                )
                                break
                        for bin_stats in get_provider_tool_latency_bins(provider):
                            if in_half_open_bin(
                                latency, bin_stats.lo_ms, bin_stats.hi_ms
                            ):
                                bin_stats.add(
                                    latency,
                                    is_error=tool.get("is_error") is True,
                                )
                                break

            response_end_at = last_response_end_timestamp(row)
            if isinstance(session_id, str) and response_end_at is not None:
                user_turn = current_user_turn_by_session.get(session_id)
                if user_turn is not None:
                    current_last_output_at = user_turn.get("last_output_at")
                    if (
                        not isinstance(current_last_output_at, datetime)
                        or response_end_at > current_last_output_at
                    ):
                        user_turn["last_output_at"] = response_end_at

            if progress_every > 0 and line_no % progress_every == 0:
                print(
                    f"Read {line_no:,} lines, valid rows={rows:,}",
                    file=sys.stderr,
                    flush=True,
                )

    for session_id in list(current_user_turn_by_session):
        close_user_turn(session_id)

    return {
        "rows": rows,
        "bad_json": bad_json,
        "rows_with_tools": rows_with_tools,
        "tool_calls": tool_calls,
        "provider_counts": provider_counts,
        "model_counts": model_counts,
        "token_groups": token_groups,
        "tool_stats": tool_stats,
        "tool_stats_by_provider": tool_stats_by_provider,
        "pair_sample": pair_sampler,
        "append_token_bins": append_token_bins,
        "append_token_bins_by_provider": append_token_bins_by_provider,
        "tool_latency_bins": tool_latency_bins,
        "tool_latency_bins_by_provider": tool_latency_bins_by_provider,
        "tool_latency_values_by_provider": tool_latency_values_by_provider,
        "human_input_wait_seconds_by_provider": human_input_wait_seconds_by_provider,
        "llm_generation_seconds_by_provider": llm_generation_seconds_by_provider,
        "user_turn_response_seconds_by_provider": user_turn_response_seconds_by_provider,
    }


def json_ready(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    return value


def add_common_loader_args(
    parser: argparse.ArgumentParser,
    *,
    default_input: Path = DEFAULT_INPUT,
    default_output_dir: Path | None = None,
) -> argparse.ArgumentParser:
    """Add the standard trace-loader CLI options used by every figure driver.

    ``default_output_dir`` should be the experiment folder, so outputs land next
    to the script (``Path(__file__).resolve().parent``).
    """
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=default_input,
        help="Input normalized JSONL trace",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=default_output_dir,
        help="Output directory (defaults to this experiment folder)",
    )
    parser.add_argument(
        "--group-by",
        choices=["provider", "model", "provider_model"],
        default="provider",
        help="Grouping for token distribution plots",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=200_000,
        help="Reservoir sample size per token metric and group",
    )
    parser.add_argument(
        "--pair-sample-size",
        type=int,
        default=80_000,
        help="Reservoir sample size for prefix-vs-append scatter",
    )
    parser.add_argument(
        "--per-tool-sample-size",
        type=int,
        default=50_000,
        help="Reservoir sample size per tool for latency boxplots and quantiles",
    )
    parser.add_argument(
        "--max-groups",
        type=int,
        default=8,
        help="Maximum token groups to plot",
    )
    parser.add_argument(
        "--top-tools",
        type=int,
        default=30,
        help="Maximum number of tools to plot",
    )
    parser.add_argument(
        "--min-tool-calls-for-plot",
        type=int,
        default=20,
        help=(
            "Collapse tool names with fewer than this many provider-local calls "
            "into an Other bucket in PNG figures. CSV summaries keep full detail."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Deterministic sampling seed",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100_000,
        help="Print progress after this many input lines; set 0 to disable",
    )
    return parser


def load_trace_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Resolve the output dir, validate input, and run the single-pass loader."""
    if args.output_dir is None:
        raise SystemExit(
            "output_dir is unset; pass default_output_dir to add_common_loader_args"
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.input.exists():
        raise SystemExit(f"Input not found: {args.input}")
    print(f"Reading {args.input}", file=sys.stderr)
    result = load_trace(
        args.input,
        group_by=args.group_by,
        token_sample_size=args.sample_size,
        pair_sample_size=args.pair_sample_size,
        per_tool_sample_size=args.per_tool_sample_size,
        seed=args.seed,
        progress_every=args.progress_every,
    )
    print(
        f"Loaded rows={result['rows']:,}, tool_calls={result['tool_calls']:,}, "
        f"bad_json={result['bad_json']:,}",
        file=sys.stderr,
    )
    return result
