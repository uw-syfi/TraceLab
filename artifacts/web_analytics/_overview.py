"""Bridge to the existing ``overview_summary`` aggregator, loaded under an unambiguous name.

``trace_facts/overview_summary/analyze.py`` is ALSO named ``analyze`` — the same bare name as this
folder's top-level module. Under the QA snippet, ``/repo/artifacts/web_analytics`` sits first on
``sys.path``, so a bare ``import analyze`` resolves to THIS folder. To reuse the proven summary
computation (token splits, context-growth shares, generation timing, tool latency — already byte-for-
byte parity-checked native↔wasm) we load that file by path under a distinct module name instead.

Its own bare imports resolve fine: the module self-inserts ``artifacts/utils`` onto ``sys.path`` at
import time (for ``trace_db`` / ``growth``), so we don't depend on the caller's path for those.

Re-exported: ``read_summary_from_db(con) -> SummaryBundle`` and ``percentile(values, q)`` — the only
two symbols the stats/distribution builders need. ``SummaryBundle.merged`` is a ``Summary`` exposing
both ``.as_dict()`` (the rich nested metrics) and raw per-round lists (e.g.
``observable_generation_time_seconds``) for percentiles the dict doesn't pre-compute (p99, etc.).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = (
    Path(__file__).resolve().parent.parent
    / "trace_facts" / "overview_summary" / "analyze.py"
)

_spec = importlib.util.spec_from_file_location("overview_summary_analyze", _PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError(f"cannot load overview_summary aggregator at {_PATH}")
_mod = importlib.util.module_from_spec(_spec)
# Register BEFORE exec: the module defines @dataclass classes, and dataclasses resolves string
# annotations via sys.modules[cls.__module__] — which is None unless the module is registered first.
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

read_summary_from_db = _mod.read_summary_from_db
percentile = _mod.percentile
Summary = _mod.Summary
SummaryBundle = _mod.SummaryBundle
