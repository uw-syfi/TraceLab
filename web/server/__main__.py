"""Run the contribute sidecar: ``python -m web.server`` (from the repo root).

The port comes from the centralized ``config/services.json`` (``ports.contribute_backend``). For
hot-reload during development, prefer the uvicorn CLI directly:
``uvicorn web.server.app:app --port <port> --reload``.
"""

from __future__ import annotations

import json
from pathlib import Path

from .app import app


def _port(default: int = 60981) -> int:
    try:
        cfg = json.loads((Path(__file__).resolve().parents[2] / "config" / "services.json").read_text())
        return int(cfg["ports"]["contribute_backend"])
    except Exception:
        return default


def main() -> None:
    import uvicorn

    host, port = "127.0.0.1", _port()
    print(f"Contribute API: http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
