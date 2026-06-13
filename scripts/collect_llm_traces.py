#!/usr/bin/env python3
"""Collect Claude Code and Codex history counts and normalized round traces."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, TextIO

from extract_codex_rounds import extract_codex_session
from extract_claude_rounds import extract_session_with_key, load_existing_round_keys, round_key


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_TRACE_DIR = REPO_ROOT / "trace"
DEFAULT_ROUND_TRACE_NAME = "llm_round_trace.jsonl"
DEFAULT_CLAUDE_TRACE_NAME = "claude_round_trace.jsonl"
DEFAULT_CODEX_TRACE_NAME = "codex_round_trace.jsonl"
DEFAULT_ALL_USERS_TRACE_NAME = "llm_round_trace.all_users.jsonl"
DEFAULT_ALL_USERS_CLAUDE_TRACE_NAME = "claude_round_trace.all_users.jsonl"
DEFAULT_ALL_USERS_CODEX_TRACE_NAME = "codex_round_trace.all_users.jsonl"
DEFAULT_PATH_SENTINEL = "__default__"


def resolve_extract_path(value: str | None, default_path: Path) -> Path | None:
    if value is None:
        return None
    if value == DEFAULT_PATH_SENTINEL:
        return default_path
    return Path(value)


def iter_home_dirs(home_root: Path) -> list[Path]:
    homes: list[Path] = []
    try:
        entries = sorted(home_root.iterdir(), key=lambda p: str(p))
    except OSError:
        return homes
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        if is_dir:
            homes.append(entry)
    return homes


def selected_home_dirs(home_root: Path, all_user: bool) -> list[Path]:
    if all_user:
        return iter_home_dirs(home_root)
    home = Path.home()
    try:
        return [home.resolve()]
    except OSError:
        return [home]


def safe_exists(path: Path) -> tuple[bool, str | None]:
    try:
        return path.exists(), None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def read_jsonl(path: Path):
    try:
        with path.open("r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def write_jsonl_row(out: TextIO, row: dict[str, Any]) -> None:
    out.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def count_completed_tool_results(round_obj: dict[str, Any]) -> int:
    return sum(1 for tool in round_obj.get("tools", []) if tool.get("result_at") is not None)


def remove_extract_outputs(paths: list[Path | None]) -> None:
    seen: set[str] = set()
    for path in paths:
        if path is None:
            continue
        expanded = path.expanduser()
        key = str(expanded.resolve()) if expanded.exists() else str(expanded)
        if key in seen:
            continue
        seen.add(key)
        try:
            expanded.unlink()
        except FileNotFoundError:
            pass


def progress(message: str, enabled: bool) -> None:
    if enabled:
        print(message, file=sys.stderr, flush=True)


def iter_claude_project_dirs(projects_dir: Path) -> list[Path]:
    try:
        children = sorted(projects_dir.iterdir(), key=lambda p: str(p))
    except OSError:
        return []
    project_dirs: list[Path] = []
    for child in children:
        try:
            if child.is_dir() and any(child.rglob("*.jsonl")):
                project_dirs.append(child)
        except OSError:
            continue
    return project_dirs


def count_claude_store(projects_dir: Path) -> dict[str, Any]:
    session_files = sorted(projects_dir.glob("*/*.jsonl"))
    session_ids = {p.stem for p in session_files}

    raw_assistant_records = 0
    real_invocation_ids: set[str] = set()
    synthetic_ids: set[str] = set()
    assistant_records_without_api_id = 0
    models = Counter()
    jsonl_files = 0

    for path in sorted(projects_dir.rglob("*.jsonl")):
        jsonl_files += 1
        file_seen: set[str] = set()
        try:
            session_key = str(path.relative_to(projects_dir).with_suffix(""))
        except ValueError:
            session_key = path.stem
        for obj in read_jsonl(path):
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            raw_assistant_records += 1
            message_id = msg.get("id")
            if not isinstance(message_id, str):
                assistant_records_without_api_id += 1
                continue
            model = msg.get("model") or "<missing>"
            if message_id == "<synthetic>" or model == "<synthetic>":
                synthetic_ids.add(f"{session_key}:{message_id}")
                continue
            invocation_id = f"{session_key}:{message_id}"
            if invocation_id in file_seen:
                continue
            file_seen.add(invocation_id)
            models[model] += 1
            real_invocation_ids.add(invocation_id)

    return {
        "exists": projects_dir.exists(),
        "jsonl_files": jsonl_files,
        "sessions": len(session_ids),
        "raw_assistant_records": raw_assistant_records,
        "llm_invocations": len(real_invocation_ids),
        "synthetic_records": len(synthetic_ids),
        "assistant_records_without_api_id": assistant_records_without_api_id,
        "models": dict(models),
        "session_ids": session_ids,
        "invocation_ids": real_invocation_ids,
    }


def count_codex_sessions(sessions_dir: Path) -> dict[str, Any]:
    session_files = sorted(sessions_dir.rglob("*.jsonl"))
    session_ids: set[str] = set()
    raw_usage_events = 0
    duplicate_usage_echoes = 0
    llm_invocations = 0
    models = Counter()

    for path in session_files:
        session_id = None
        model = None
        last_total_sig = None

        for obj in read_jsonl(path):
            typ = obj.get("type")
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}

            if typ == "session_meta":
                session_id = payload.get("id") or session_id
                model = payload.get("model") or model
            elif typ == "turn_context":
                model = payload.get("model") or model

            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last_usage = info.get("last_token_usage")
            total_usage = info.get("total_token_usage")
            if not isinstance(last_usage, dict) or not isinstance(total_usage, dict):
                continue

            raw_usage_events += 1
            total_sig = json.dumps(total_usage, sort_keys=True)
            if total_sig == last_total_sig:
                duplicate_usage_echoes += 1
                continue
            last_total_sig = total_sig
            llm_invocations += 1
            models[model or "<unknown>"] += 1

        if session_id is None:
            session_id = path.stem
        session_ids.add(session_id)

    return {
        "exists": sessions_dir.exists(),
        "jsonl_files": len(session_files),
        "sessions": len(session_ids),
        "raw_token_count_usage_events": raw_usage_events,
        "duplicate_usage_echoes_removed": duplicate_usage_echoes,
        "llm_invocations": llm_invocations,
        "models": dict(models),
        "session_ids": session_ids,
    }


def strip_sets(value: Any) -> Any:
    if isinstance(value, set):
        return len(value)
    if isinstance(value, dict):
        return {
            key: strip_sets(child)
            for key, child in value.items()
            if key not in {"session_ids", "invocation_ids"}
        }
    if isinstance(value, list):
        return [strip_sets(v) for v in value]
    return value


def append_claude_round_trace(
    *,
    output_path: Path,
    sources: list[dict[str, Any]],
    project_filters: list[str],
    progress_enabled: bool,
) -> dict[str, Any]:
    progress(f"extract: loading existing dedup keys from {output_path}", progress_enabled)
    existing_keys = load_existing_round_keys(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats: dict[str, Any] = {
        "output": str(output_path),
        "existing_rounds": len(existing_keys),
        "project_dirs": 0,
        "session_files": 0,
        "candidate_rounds": 0,
        "written_rounds": 0,
        "skipped_duplicate_rounds": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "errors": [],
    }

    filtered_sources = [
        source
        for source in sources
        if not project_filters
        or any(value in source["project_dir"].name for value in project_filters)
    ]
    progress(
        "extract: "
        f"{len(filtered_sources)} Claude project directorie(s), "
        f"{len(existing_keys)} existing round key(s)",
        progress_enabled,
    )

    with output_path.open("a", encoding="utf-8") as out:
        for project_index, source in enumerate(filtered_sources, start=1):
            project_dir = source["project_dir"]
            project_name = project_dir.name
            stats["project_dirs"] += 1
            try:
                session_files = sorted(project_dir.rglob("*.jsonl"))
            except OSError as exc:
                stats["errors"].append(
                    {"project_dir": str(project_dir), "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            stats["session_files"] += len(session_files)
            progress(
                "extract: "
                f"[{project_index}/{len(filtered_sources)}] "
                f"{source['user']} {source['store']} {project_name}: "
                f"{len(session_files)} session file(s)",
                progress_enabled,
            )
            for session_index, session_file in enumerate(session_files, start=1):
                try:
                    session_key = str(session_file.relative_to(project_dir).with_suffix(""))
                    rounds = extract_session_with_key(session_file, project_name, session_key)
                except Exception as exc:
                    stats["errors"].append(
                        {"session_file": str(session_file), "error": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
                stats["candidate_rounds"] += len(rounds)
                for round_obj in rounds:
                    round_obj["home"] = source["home"]
                    round_obj["user"] = source["user"]
                    round_obj["store"] = source["store"]
                    key = round_key(round_obj)
                    round_obj["trace_key"] = key
                    if key in existing_keys:
                        stats["skipped_duplicate_rounds"] += 1
                        continue
                    existing_keys.add(key)
                    stats["tool_calls"] += len(round_obj.get("tools", []))
                    stats["tool_results"] += count_completed_tool_results(round_obj)
                    write_jsonl_row(out, round_obj)
                    stats["written_rounds"] += 1
                if (
                    session_index == len(session_files)
                    or session_index == 1
                    or session_index % 10 == 0
                ):
                    progress(
                        "extract: "
                        f"{project_name} sessions {session_index}/{len(session_files)}, "
                        f"candidates={stats['candidate_rounds']}, "
                        f"written={stats['written_rounds']}, "
                        f"dupes={stats['skipped_duplicate_rounds']}",
                        progress_enabled,
                    )

    stats["final_rounds"] = len(existing_keys)
    progress(
        "extract: "
        f"done candidates={stats['candidate_rounds']} "
        f"written={stats['written_rounds']} "
        f"dupes={stats['skipped_duplicate_rounds']}",
        progress_enabled,
    )
    return stats


def append_codex_round_trace(
    *,
    output_path: Path,
    sources: list[dict[str, Any]],
    progress_enabled: bool,
) -> dict[str, Any]:
    progress(f"extract: loading existing dedup keys from {output_path}", progress_enabled)
    existing_keys = load_existing_round_keys(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    stats: dict[str, Any] = {
        "output": str(output_path),
        "existing_rounds": len(existing_keys),
        "session_dirs": 0,
        "session_files": 0,
        "candidate_rounds": 0,
        "written_rounds": 0,
        "skipped_duplicate_rounds": 0,
        "tool_calls": 0,
        "tool_results": 0,
        "errors": [],
    }

    progress(
        "extract: "
        f"{len(sources)} Codex session directorie(s), "
        f"{len(existing_keys)} existing round key(s)",
        progress_enabled,
    )
    with output_path.open("a", encoding="utf-8") as out:
        for source_index, source in enumerate(sources, start=1):
            sessions_dir = source["sessions_dir"]
            stats["session_dirs"] += 1
            try:
                session_files = sorted(sessions_dir.rglob("*.jsonl"))
            except OSError as exc:
                stats["errors"].append(
                    {"sessions_dir": str(sessions_dir), "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            stats["session_files"] += len(session_files)
            progress(
                "extract: "
                f"[{source_index}/{len(sources)}] {source['user']} .codex: "
                f"{len(session_files)} session file(s)",
                progress_enabled,
            )
            for session_index, session_file in enumerate(session_files, start=1):
                try:
                    rounds = extract_codex_session(session_file)
                except Exception as exc:
                    stats["errors"].append(
                        {"session_file": str(session_file), "error": f"{type(exc).__name__}: {exc}"}
                    )
                    continue
                stats["candidate_rounds"] += len(rounds)
                for round_obj in rounds:
                    round_obj["home"] = source["home"]
                    round_obj["user"] = source["user"]
                    round_obj["store"] = ".codex"
                    key = round_key(round_obj)
                    round_obj["trace_key"] = key
                    if key in existing_keys:
                        stats["skipped_duplicate_rounds"] += 1
                        continue
                    existing_keys.add(key)
                    stats["tool_calls"] += len(round_obj.get("tools", []))
                    stats["tool_results"] += count_completed_tool_results(round_obj)
                    write_jsonl_row(out, round_obj)
                    stats["written_rounds"] += 1
                if (
                    session_index == len(session_files)
                    or session_index == 1
                    or session_index % 25 == 0
                ):
                    progress(
                        "extract: "
                        f"{source['user']} .codex sessions "
                        f"{session_index}/{len(session_files)}, "
                        f"candidates={stats['candidate_rounds']}, "
                        f"written={stats['written_rounds']}, "
                        f"dupes={stats['skipped_duplicate_rounds']}",
                        progress_enabled,
                    )

    stats["final_rounds"] = len(existing_keys)
    progress(
        "extract: "
        f"codex done candidates={stats['candidate_rounds']} "
        f"written={stats['written_rounds']} "
        f"dupes={stats['skipped_duplicate_rounds']}",
        progress_enabled,
    )
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect Claude Code and Codex history counts and normalized LLM round traces."
    )
    parser.add_argument(
        "--all-user",
        action="store_true",
        help=(
            "Scan every user home under --home-root. By default only the "
            "launching user's home is scanned."
        ),
    )
    parser.add_argument(
        "--home-root",
        type=Path,
        default=Path("/home"),
        help="Directory containing user home directories when --all-user is set.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a text table.")
    parser.add_argument(
        "--no-claude-back",
        action="store_true",
        help="Skip .claude.back directories.",
    )
    parser.add_argument(
        "--quiet-host-progress",
        action="store_true",
        help="Suppress progress messages from host home parsing and round extraction.",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=DEFAULT_TRACE_DIR,
        help="Default directory for extracted normalized trace JSONL files.",
    )
    parser.add_argument(
        "--extract-claude-rounds",
        nargs="?",
        const=DEFAULT_PATH_SENTINEL,
        metavar="PATH",
        help=(
            "Append deduped Claude LLM round rows to PATH, or to "
            f"trace/{DEFAULT_CLAUDE_TRACE_NAME} if PATH is omitted."
        ),
    )
    parser.add_argument(
        "--extract-codex-rounds",
        nargs="?",
        const=DEFAULT_PATH_SENTINEL,
        metavar="PATH",
        help=(
            "Append deduped Codex LLM round rows to PATH, or to "
            f"trace/{DEFAULT_CODEX_TRACE_NAME} if PATH is omitted."
        ),
    )
    parser.add_argument(
        "--extract-rounds",
        nargs="?",
        const=DEFAULT_PATH_SENTINEL,
        metavar="PATH",
        help=(
            "Append deduped normalized Claude and Codex round rows to PATH, "
            f"or to trace/{DEFAULT_ROUND_TRACE_NAME} if PATH is omitted."
        ),
    )
    parser.add_argument(
        "--extract-project-filter",
        action="append",
        default=[],
        help=(
            "Only extract Claude projects whose directory name contains this "
            "text. Can be repeated."
        ),
    )
    parser.add_argument(
        "--fresh-extract",
        action="store_true",
        help="Remove selected extraction output files before writing fresh traces.",
    )
    args = parser.parse_args()

    home_root = args.home_root.expanduser()
    trace_dir = args.trace_dir.expanduser()
    default_round_name = (
        DEFAULT_ALL_USERS_TRACE_NAME if args.all_user else DEFAULT_ROUND_TRACE_NAME
    )
    default_claude_name = (
        DEFAULT_ALL_USERS_CLAUDE_TRACE_NAME if args.all_user else DEFAULT_CLAUDE_TRACE_NAME
    )
    default_codex_name = (
        DEFAULT_ALL_USERS_CODEX_TRACE_NAME if args.all_user else DEFAULT_CODEX_TRACE_NAME
    )
    extract_round_path = resolve_extract_path(
        args.extract_rounds,
        trace_dir / default_round_name,
    )
    extract_claude_arg_path = resolve_extract_path(
        args.extract_claude_rounds,
        trace_dir / default_claude_name,
    )
    extract_codex_arg_path = resolve_extract_path(
        args.extract_codex_rounds,
        trace_dir / default_codex_name,
    )
    users = []
    totals = {
        "claude_sessions": 0,
        "claude_llm_invocations": 0,
        "codex_sessions": 0,
        "codex_llm_invocations": 0,
    }
    skipped_paths: list[dict[str, str]] = []
    claude_extract_sources: list[dict[str, Any]] = []
    codex_extract_sources: list[dict[str, Any]] = []
    host_progress = not args.quiet_host_progress
    extract_claude_path = extract_round_path or extract_claude_arg_path
    extract_codex_path = extract_round_path or extract_codex_arg_path
    if args.fresh_extract:
        remove_extract_outputs([extract_claude_path, extract_codex_path])

    homes = selected_home_dirs(home_root, args.all_user)
    if args.all_user:
        progress(f"host: found {len(homes)} home directorie(s) under {home_root}", host_progress)
    else:
        progress(f"host: scanning launcher home {homes[0] if homes else '<none>'}", host_progress)

    for home_index, home in enumerate(homes, start=1):
        progress(f"host: [{home_index}/{len(homes)}] scanning {home}", host_progress)
        user_result: dict[str, Any] = {
            "user": home.name,
            "home": str(home),
            "claude": {},
            "codex": {},
        }

        claude_stores = [(".claude", home / ".claude" / "projects")]
        if not args.no_claude_back:
            claude_stores.append((".claude.back", home / ".claude.back" / "projects"))

        claude_session_union: set[str] = set()
        claude_invocation_union: set[str] = set()
        for label, projects_dir in claude_stores:
            exists, error = safe_exists(projects_dir)
            if error:
                skipped_paths.append(
                    {"user": home.name, "store": label, "path": str(projects_dir), "error": error}
                )
                continue
            if not exists:
                continue
            progress(f"host: {home.name} counting {label} at {projects_dir}", host_progress)
            result = count_claude_store(projects_dir)
            user_result["claude"][label] = result
            claude_session_union.update(result["session_ids"])
            claude_invocation_union.update(result["invocation_ids"])
            if extract_claude_path:
                project_dirs = iter_claude_project_dirs(projects_dir)
                progress(
                    "host: "
                    f"{home.name} {label} has {len(project_dirs)} "
                    "Claude project directorie(s) queued for extraction",
                    host_progress,
                )
                for project_dir in project_dirs:
                    claude_extract_sources.append(
                        {
                            "user": home.name,
                            "home": str(home),
                            "store": label,
                            "project_dir": project_dir,
                        }
                    )

        user_result["claude"]["combined"] = {
            "sessions": len(claude_session_union),
            "llm_invocations": len(claude_invocation_union),
        }

        codex_dir = home / ".codex" / "sessions"
        exists, error = safe_exists(codex_dir)
        if error:
            skipped_paths.append(
                {"user": home.name, "store": ".codex", "path": str(codex_dir), "error": error}
            )
            user_result["codex"] = {"exists": False, "sessions": 0, "llm_invocations": 0}
        elif exists:
            progress(f"host: {home.name} counting codex at {codex_dir}", host_progress)
            user_result["codex"] = count_codex_sessions(codex_dir)
            if extract_codex_path:
                codex_extract_sources.append(
                    {
                        "user": home.name,
                        "home": str(home),
                        "store": ".codex",
                        "sessions_dir": codex_dir,
                    }
                )
        else:
            user_result["codex"] = {"exists": False, "sessions": 0, "llm_invocations": 0}

        totals["claude_sessions"] += user_result["claude"]["combined"]["sessions"]
        totals["claude_llm_invocations"] += user_result["claude"]["combined"]["llm_invocations"]
        totals["codex_sessions"] += user_result["codex"]["sessions"]
        totals["codex_llm_invocations"] += user_result["codex"]["llm_invocations"]
        users.append(user_result)
        progress(
            f"host: [{home_index}/{len(homes)}] done {home.name}: "
            f"claude={user_result['claude']['combined']['llm_invocations']} "
            f"codex={user_result['codex']['llm_invocations']}",
            host_progress,
        )

    extraction: dict[str, Any] | None = None
    if extract_claude_path or extract_codex_path:
        extraction = {}
    if extract_claude_path:
        extraction["claude"] = append_claude_round_trace(
            output_path=extract_claude_path.expanduser(),
            sources=claude_extract_sources,
            project_filters=args.extract_project_filter,
            progress_enabled=host_progress,
        )
    if extract_codex_path:
        extraction["codex"] = append_codex_round_trace(
            output_path=extract_codex_path.expanduser(),
            sources=codex_extract_sources,
            progress_enabled=host_progress,
        )

    output = {
        "scan_mode": "all_user" if args.all_user else "current_user",
        "home_root": str(home_root) if args.all_user else None,
        "scanned_homes": [str(home) for home in homes],
        "totals": totals,
        "users": users,
        "skipped_paths": skipped_paths,
        "extraction": extraction,
    }
    if args.json:
        print(json.dumps(strip_sets(output), indent=2, sort_keys=True))
        return 0

    if args.all_user:
        print(f"scan_mode: all_user")
        print(f"home_root: {home_root}")
    else:
        print("scan_mode: current_user")
        print(f"home: {homes[0] if homes else '<none>'}")
    print()
    print("user\tclaude_sessions\tclaude_llm_invocations\tcodex_sessions\tcodex_llm_invocations")
    for item in users:
        claude = item["claude"]["combined"]
        codex = item["codex"]
        has_rows = (
            claude["sessions"]
            or claude["llm_invocations"]
            or codex["sessions"]
            or codex["llm_invocations"]
        )
        if not has_rows:
            continue
        print(
            f"{item['user']}\t{claude['sessions']}\t{claude['llm_invocations']}"
            f"\t{codex['sessions']}\t{codex['llm_invocations']}"
        )
    print()
    print(
        "TOTAL\t"
        f"{totals['claude_sessions']}\t{totals['claude_llm_invocations']}"
        f"\t{totals['codex_sessions']}\t{totals['codex_llm_invocations']}"
    )
    if skipped_paths:
        print()
        print("Skipped unreadable paths:")
        for item in skipped_paths:
            print(f"{item['user']}\t{item['store']}\t{item['path']}\t{item['error']}")
    if extraction:
        print()
        print("Round extraction:")
        for provider, provider_stats in extraction.items():
            print(f"[{provider}]")
            print(f"output\t{provider_stats['output']}")
            print(f"existing_rounds\t{provider_stats['existing_rounds']}")
            if provider == "claude":
                print(f"project_dirs\t{provider_stats['project_dirs']}")
            if provider == "codex":
                print(f"session_dirs\t{provider_stats['session_dirs']}")
            print(f"session_files\t{provider_stats['session_files']}")
            print(f"candidate_rounds\t{provider_stats['candidate_rounds']}")
            print(f"written_rounds\t{provider_stats['written_rounds']}")
            print(f"skipped_duplicate_rounds\t{provider_stats['skipped_duplicate_rounds']}")
            print(f"tool_calls\t{provider_stats['tool_calls']}")
            print(f"tool_results\t{provider_stats['tool_results']}")
            print(f"final_rounds\t{provider_stats['final_rounds']}")
            if provider_stats["errors"]:
                print(f"{provider}_extraction_errors")
                for item in provider_stats["errors"]:
                    print(json.dumps(item, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
