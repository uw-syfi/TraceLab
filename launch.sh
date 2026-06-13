#!/usr/bin/env bash
# Launch the SyFI Trace Atlas web app in one of two roles. Both serve the BUILT site on the sidecar
# port (default 60982) and keep the browser same-origin (no CORS):
#
#   --frontend-only  (default)  Machine B. Serve the Analyze frontend + the local sidecar, auto-analyze
#                               this machine's local ~/.claude + ~/.codex, and reverse-proxy /api to a
#                               remote master (its LLM + contribute backends). Runs NO backend here.
#   --master-server             This machine IS the master. Run the COMPLETE stack — the AI backend
#                               (web/ai_infra) + the contribute backend (web/server) + the sidecar
#                               serving the built site and splitting /api between them. The local-trace
#                               endpoint is DISABLED: the master never reads the operator's local
#                               sessions (visitors get drag-and-drop, exactly like the hosted app).
#
# Usage:
#   ./launch.sh [--frontend-only | --master-server] [--master URL] [--port N] [--host H]
#               [--build] [--no-sync] [--strip-images]
#
#   --frontend-only  Frontend + sidecar, proxy /api to a master (default).
#   --master-server  Frontend + sidecar + local AI/contribute backends (this machine is the master).
#   --master URL     (frontend-only) Master to proxy /api to. Else $MASTER_SERVER_ADDRESS, else
#                    config/services.json:master_server_address, else the built-in default.
#                    Ignored in --master-server (backends are local).
#   --port N         Sidecar bind port (default 60982; sets LOCAL_SIDECAR_PORT).
#   --host H         (--master-server only) Sidecar bind host, e.g. 0.0.0.0 to serve the public.
#                    The frontend role is ALWAYS loopback-only (it serves your local sessions) —
#                    a non-loopback host there is refused and forced to 127.0.0.1.
#   --build          Force-rebuild the site even if web/app/dist exists.
#   --no-sync        Skip `uv sync` (use the current environment as-is).
#   --strip-images   (frontend-only) Drop inline base64 screenshots from the streamed trace (size-only).
#
# Examples:
#   ./launch.sh --master https://master.example.com            # machine B: frontend, proxy to master
#   ./launch.sh --master-server                                # this machine: the whole master stack
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

DIST_DIR="web/app/dist"
MODE=frontend
FORCE_BUILD=0
DO_SYNC=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --frontend-only) MODE=frontend; shift ;;
    --master-server) MODE=master; shift ;;
    --master)        export MASTER_SERVER_ADDRESS="$2"; shift 2 ;;
    --master=*)      export MASTER_SERVER_ADDRESS="${1#*=}"; shift ;;
    --port)          export LOCAL_SIDECAR_PORT="$2"; shift 2 ;;
    --port=*)        export LOCAL_SIDECAR_PORT="${1#*=}"; shift ;;
    --host)          export LOCAL_SIDECAR_HOST="$2"; shift 2 ;;
    --host=*)        export LOCAL_SIDECAR_HOST="${1#*=}"; shift ;;
    --build)         FORCE_BUILD=1; shift ;;
    --no-sync)       DO_SYNC=0; shift ;;
    --strip-images)  export STRIP_IMAGE_BLOBS=1; shift ;;
    -h|--help)       sed -n '2,${/^#/!q;p;}' "$0"; exit 0 ;;
    *) echo "launch.sh: unknown argument '$1' (try --help)" >&2; exit 2 ;;
  esac
done

have() { command -v "$1" >/dev/null 2>&1; }

# 1) Build the static site if missing (or forced). Both roles serve it via the sidecar. Install web
#    deps first if they're missing (a fresh checkout has no node_modules, so `astro` isn't on PATH).
if [[ "$FORCE_BUILD" == 1 || ! -f "$DIST_DIR/index.html" ]]; then
  if ! have npm; then
    echo "launch.sh: npm not found — install Node.js, or build elsewhere and copy into $DIST_DIR." >&2
    exit 1
  fi
  if [[ ! -x web/app/node_modules/.bin/astro ]]; then
    echo ">> Installing web dependencies (npm) …"
    if [[ -f web/app/package-lock.json ]]; then npm --prefix web/app ci; else npm --prefix web/app install; fi
  fi
  echo ">> Building the site ($DIST_DIR) …"
  npm --prefix web/app run build
fi

if ! have uv; then
  echo "launch.sh: uv not found — install uv (https://docs.astral.sh/uv/)." >&2
  exit 1
fi

# 2) + 3) Sync deps and run, per role.
if [[ "$MODE" == master ]]; then
  if [[ "$DO_SYNC" == 1 ]]; then
    echo ">> Syncing Python deps (uv sync --extra ai --extra server --extra local_sidecar) …"
    uv sync --extra ai --extra server --extra local_sidecar
  fi
  # The backends read keys (E2B / LLM) from the environment; pick them up from ~/.bashrc as `just
  # dev-all` does, and map E2B_KEY -> E2B_API_KEY if only the former is set.
  if [[ -f "$HOME/.bashrc" ]]; then set +u; source "$HOME/.bashrc"; set -u; fi
  if [[ -z "${E2B_API_KEY:-}" && -n "${E2B_KEY:-}" ]]; then export E2B_API_KEY="$E2B_KEY"; fi

  echo ">> Starting MASTER stack — AI backend + contribute backend + sidecar (this machine is the master)."
  trap 'kill 0' EXIT
  uv run --extra ai python web/ai_infra/app.py &
  uv run --extra server python -m web.server &
  LOCAL_MASTER_SERVER=1 uv run --extra local_sidecar python -m web.local_sidecar &
  wait
else
  if [[ "$DO_SYNC" == 1 ]]; then
    echo ">> Syncing Python deps (uv sync --extra local_sidecar) …"
    uv sync --extra local_sidecar
  fi
  echo ">> Starting frontend + sidecar — open the printed URL and go to Analyze."
  exec uv run --extra local_sidecar python -m web.local_sidecar
fi
