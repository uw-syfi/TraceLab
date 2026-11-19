#!/usr/bin/env python3
"""Audit shared native request lifecycle and conservative timing eligibility."""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "artifacts" / "utils"))
import trace_db

def main() -> int:
    import request_timing as rt
    parser = argparse.ArgumentParser(description=__doc__)
    trace_db.add_db_args(parser, default_output_dir=Path(__file__).resolve().parent)
    args = parser.parse_args()
    con = trace_db.open_from_args(args)
    try:
        report = rt.render_request_audit(con)
    finally:
        con.close()
    output = Path(args.output_dir) / "result_analysis.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
