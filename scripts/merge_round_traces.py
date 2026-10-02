#!/usr/bin/env python3
"""Union private normalized round traces by stable round identity.

Inputs are ordered from oldest to newest. When the same round occurs more than
once, the row from the last input wins so newer normalizer fields replace older
representations. Rows that only exist in an older trace are retained.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any


RoundIdentity = tuple[str, str, str]


def round_exclusion_reason(record: dict[str, Any]) -> str | None:
    """Identify normalized rows known to be extractor artifacts."""
    if record.get("provider") == "codex":
        model = record.get("model")
        if not isinstance(model, str) or not model:
            return "codex_missing_model"
        timing_events = record.get("timing_events")
        tools = record.get("tools")
        if (
            isinstance(timing_events, list)
            and len(timing_events) == 1
            and isinstance(timing_events[0], dict)
            and timing_events[0].get("event_type") == "usage_report"
            and (not isinstance(tools, list) or not tools)
        ):
            return "codex_usage_only_replay"
    return None


def stable_round_identity(record: dict[str, Any]) -> RoundIdentity:
    """Return the provider/session/round identity used by the extractors."""
    provider = record.get("provider")
    session_id = record.get("session_id")
    round_id = record.get("round_id")
    values = {
        "provider": provider,
        "session_id": session_id,
        "round_id": round_id,
    }
    invalid_fields = [
        field_name
        for field_name, value in values.items()
        if not isinstance(value, str) or not value
    ]
    if invalid_fields:
        raise ValueError(
            "round is missing non-empty stable identity field(s): "
            + ", ".join(invalid_fields)
        )
    # Older Windows runs wrote subagent session ids with backslashes; they name the same session.
    return provider, session_id.replace("\\", "/"), round_id


def iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="strict") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            yield line_number, record


def merge_round_traces(input_paths: list[Path], output_path: Path) -> dict[str, Any]:
    """Merge traces with last-input precedence and replace output on success."""
    if not input_paths:
        raise ValueError("at least one input trace is required")
    resolved_output = output_path.resolve()
    for input_path in input_paths:
        if input_path.resolve() == resolved_output:
            raise ValueError("output path must differ from every input path")
        if not input_path.is_file():
            raise FileNotFoundError(f"missing input trace: {input_path}")

    owner_by_identity: dict[RoundIdentity, tuple[int, int]] = {}
    input_rows = [0] * len(input_paths)
    excluded_by_input: list[dict[str, int]] = [{} for _ in input_paths]
    for input_index, input_path in enumerate(input_paths):
        for line_number, record in iter_jsonl(input_path):
            input_rows[input_index] += 1
            exclusion_reason = round_exclusion_reason(record)
            if exclusion_reason is not None:
                excluded_counts = excluded_by_input[input_index]
                excluded_counts[exclusion_reason] = (
                    excluded_counts.get(exclusion_reason, 0) + 1
                )
                continue
            identity = stable_round_identity(record)
            owner_by_identity[identity] = input_index, line_number

    temporary_directory = os.environ.get("TMPDIR")
    if not temporary_directory:
        raise RuntimeError("TMPDIR must be set for trace merge temporary files")
    Path(temporary_directory).mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written_by_input = [0] * len(input_paths)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f"{output_path.name}.",
            suffix=".tmp",
            dir=temporary_directory,
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            for input_index, input_path in enumerate(input_paths):
                for line_number, record in iter_jsonl(input_path):
                    if round_exclusion_reason(record) is not None:
                        continue
                    identity = stable_round_identity(record)
                    if owner_by_identity[identity] != (input_index, line_number):
                        continue
                    output_file.write(
                        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
                    written_by_input[input_index] += 1
        shutil.move(str(temporary_path), output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "inputs": [
            {
                "path": str(input_path),
                "rows": input_rows[input_index],
                "rows_excluded": sum(excluded_by_input[input_index].values()),
                "excluded_reasons": excluded_by_input[input_index],
                "rows_kept": written_by_input[input_index],
                "rows_superseded": (
                    input_rows[input_index]
                    - sum(excluded_by_input[input_index].values())
                    - written_by_input[input_index]
                ),
            }
            for input_index, input_path in enumerate(input_paths)
        ],
        "output": str(output_path),
        "unique_rounds": len(owner_by_identity),
        "excluded_rows": sum(
            sum(excluded_counts.values()) for excluded_counts in excluded_by_input
        ),
        "duplicate_rows_superseded": (
            sum(input_rows)
            - sum(sum(excluded_counts.values()) for excluded_counts in excluded_by_input)
            - len(owner_by_identity)
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="Private normalized JSONL traces, ordered oldest to newest.",
    )
    parser.add_argument("-o", "--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = merge_round_traces(arguments.inputs, arguments.output)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
