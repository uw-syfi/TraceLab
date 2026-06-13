"""Local self-deploy sidecar: serve the Analyze frontend against the user's own machine.

A user runs ``python -m web.local_sidecar`` on their laptop (machine B). It:

  1. statically serves the built site (``web/app/dist``);
  2. exposes ``GET /api/local-trace`` — streams the local ``~/.claude/projects`` + ``~/.codex/sessions``
     as one ``.tar.gz`` (top-level ``projects/`` + ``sessions/``) so the browser auto-analyzes it;
  3. reverse-proxies the rest of ``/api/*`` (the chat WebSocket, ``/api/pool``, ``/api/contribute*``)
     to the master server, so the browser stays same-origin (no CORS) and the master is unchanged.

Raw trace bytes only ever travel sidecar↔browser on localhost; sanitization still happens in the
browser (Pyodide). See ``web/local_sidecar/README.md``.
"""
