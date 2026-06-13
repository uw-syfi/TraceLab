"""Run the local self-deploy sidecar: ``python -m web.local_sidecar`` (from the repo root).

The port comes from ``config/services.json`` (``ports.local_sidecar``), overridable with
``LOCAL_SIDECAR_PORT``. Bind host:

- **frontend role (default)**: ALWAYS loopback. This role serves the local-trace endpoint (raw
  ``~/.claude`` + ``~/.codex`` bytes), so a non-loopback ``LOCAL_SIDECAR_HOST`` is refused and forced
  to ``127.0.0.1`` — it must never face the network.
- **master-server role** (``LOCAL_MASTER_SERVER=1``): local-trace is disabled, so ``LOCAL_SIDECAR_HOST``
  is honored (e.g. ``0.0.0.0`` to serve the public). The local backends stay loopback behind it.

The master it proxies to comes from ``MASTER_SERVER_ADDRESS`` / ``config/services.json``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .app import SERVE_LOCAL_TRACE, UPSTREAM_DESC, app

# Loopback addresses that never face the network.
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _port(default: int = 60982) -> int:
    env = os.environ.get("LOCAL_SIDECAR_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    try:
        cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / "services.json").read_text())
        return int(cfg["ports"]["local_sidecar"])
    except Exception:
        return default


def main() -> None:
    import uvicorn

    host = os.environ.get("LOCAL_SIDECAR_HOST", "127.0.0.1")
    # Hard safety guard: the frontend role serves raw local sessions, so it is loopback-only. Refuse
    # any non-loopback bind and force 127.0.0.1 — defense in depth, independent of launch.sh.
    if SERVE_LOCAL_TRACE and host not in _LOOPBACK:
        print(
            f"Local trace sidecar: refusing to bind {host!r} in the frontend role (it serves your local "
            f"~/.claude + ~/.codex). Forcing 127.0.0.1. Use --master-server to expose a public deployment.",
            flush=True,
        )
        host = "127.0.0.1"
    port = _port()
    print(f"Local trace sidecar: http://{host}:{port}  ->  {UPSTREAM_DESC}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
