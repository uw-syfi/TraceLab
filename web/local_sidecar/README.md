# Local self-deploy sidecar (`web/local_sidecar`)

Run the **Analyze** frontend against your **own** machine. In the frontend role this one process is a
**transparent reverse proxy to the master**: every page you open is the master's *real* page with real
data (overview, comparison, the contributed pool, the figure pages) — the sidecar only answers
`/api/sidecar-info` + `/api/local-trace/*` locally, so **Analyze** detects it and analyzes your local
`~/.claude` + `~/.codex` history right here. The browser stays same-origin (no CORS), the master
deployment is untouched, and there are **no redirects** to strand you on another origin.

Raw trace bytes only ever travel sidecar→browser on **localhost**. The sanitize runs *natively* on this
machine (the local executor, below); only the small sanitized `.gz` reaches the browser, and only
sanitized data is ever proxied onward to the master.

```
                 ┌─────────────── machine B (you) ───────────────┐
  browser ──────▶│  web/local_sidecar :60982                     │
   (same-origin) │   ├─ /api/sidecar-info, /api/local-trace/* ─┐ │  (answered LOCALLY)
                 │   │     native sanitize of ~/.claude+~/.codex│ │
                 │   └─ everything else (pages, /api/chat, pool)─┼─┼──▶ master server
                 └───────────────────────────────────────────────┘    (MASTER_SERVER_ADDRESS)
```

## Run

```bash
uv sync --extra local_sidecar
# Build the site first so dist/ exists (from the repo root):
just site
# Point at your master server and start (port 60982 from config/services.json):
MASTER_SERVER_ADDRESS=https://your-master.example uv run --extra local_sidecar python -m web.local_sidecar
#   or via just (leave master= empty to use config/services.json):
just local-serve master=https://your-master.example
```

Then open `http://127.0.0.1:60982/` and go to **Analyze** — it detects the local-trace endpoint and
analyzes your `~/.claude` + `~/.codex` automatically. (On the hosted master there is no such endpoint,
so the same build falls back to drag-and-drop.)

## Two roles

| role | trigger | local-trace | site + `/api/*` go to | bind |
|------|---------|-------------|------------------|------|
| **frontend** (default, machine B) | — | **served** (your `~/.claude`+`~/.codex`) | **whole site + `/api/*` reverse-proxied** to the remote master (only `/api/sidecar-info` + `/api/local-trace/*` answered locally) | **loopback only, enforced** |
| **master server** (machine A) | `LOCAL_MASTER_SERVER=1` (`launch.sh --master-server`) | **disabled** (never reads local sessions) | site served from local `dist/`; `/api/*` split to the local AI (`:60980`) + contribute (`:60981`) backends | `LOCAL_SIDECAR_HOST` honored (e.g. `0.0.0.0`) |

Because the frontend role serves raw local session bytes, it is **hard-wired to loopback**: a
non-loopback `LOCAL_SIDECAR_HOST` is refused and forced to `127.0.0.1` (enforced in the process, not
just the launcher). Only the master-server role — which does **not** serve local-trace — may bind a
public interface; the AI/contribute backends still stay on loopback behind it.

## Config

The master address resolves `env MASTER_SERVER_ADDRESS > config/services.json:master_server_address >
https://master.example.com` (placeholder fallback — set the real host via env or config). Other env knobs:

| env | default | meaning |
|-----|---------|---------|
| `LOCAL_SIDECAR_PORT` | `60982` (`config/services.json:ports.local_sidecar`) | bind port |
| `LOCAL_SIDECAR_HOST` | `127.0.0.1` | bind host — **master-server role only**; the frontend role forces loopback |
| `LOCAL_MASTER_SERVER` | off | run as the master: disable local-trace + split `/api` to local backends |
| `LOCAL_TRACE_CLAUDE_ROOT` | `~/.claude/projects` | Claude sessions root (→ `projects/` in the archive) |
| `LOCAL_TRACE_CODEX_ROOT` | `~/.codex/sessions` | Codex sessions root (→ `sessions/` in the archive) |
| `LOCAL_DIST_ROOT` | `web/app/dist` | the built site to serve (**master-server role only**; the frontend role proxies the site from the master, so its `dist/` is unused) |
| `LOCAL_EXPORT_DIR` | `~/.cache/syfi-trace/export` | (frontend role) where the native prepare writes `sanitized.jsonl.gz` + `normalized.jsonl.gz` + `round_raw.json` + `trace.duckdb`(`.gz`); a persisted, reusable export |
| `STRIP_IMAGE_BLOBS` | off | (frontend role) drop inline base64 screenshots from the stream (size-only; the browser discards them anyway) |
| `LOCAL_SIDECAR_PROXY_TIMEOUT` | `300` | upstream proxy timeout (s) |

## Endpoints

- `GET /api/local-trace` — streams a `.tar.gz` of `~/.claude/projects` (as `projects/…`) +
  `~/.codex/sessions` (as `sessions/…`), `*.jsonl` only — the same layout the browser ingest already
  understands. `HEAD` is a cheap presence probe (no walk) that drives the frontend's auto-load.
- `GET /api/local-trace/meta` — stat-only `{claude, codex, approx_uncompressed_bytes}`.
  (frontend role only — not registered in the master-server role.)

**Local executor (the "code to data" path).** Instead of shipping the raw ~1.3 GB trace to the browser
to sanitize + build the DB + analyze in Pyodide (tens of seconds of WASM work, painful over a tunnel),
the sidecar runs the *same* code **natively**: it sanitizes (`web/payload/ingest.py`), materializes the
DuckDB (`artifacts/utils/trace_db.py`), and computes the dashboard (`artifacts/web_analytics/analyze.py`)
— so the browser fetches a **~60 KB JSON** payload and does zero Pyodide compute for the dashboard. The
materialized `.duckdb` transfers in the background to prime the AI assistant's in-browser query cache.

- `POST /api/local-trace/prepare` (`?refresh=1`) — normalize+sanitize **and materialize the DuckDB**
  natively into `LOCAL_EXPORT_DIR`; cached by a cheap signature over the trace files. It **streams
  newline-delimited JSON progress** (`application/x-ndjson`): `{"stage":"…"}` lines (incl.
  `Building database`) then a final `{"meta":{kind, providers, sessions, rounds, tools, rawAvailable,
  titles, sanitizedBytes, dbAvailable}}` (or `{"error":"…"}`). The browser needs `rawAvailable`/`titles`
  from the meta because the sanitized `.gz` alone carries neither. ingest runs in an ephemeral work dir;
  only the final artifacts are copied into `LOCAL_EXPORT_DIR`.
- `GET /api/local-trace/analytics?tz=<offsetMinutes>` — the **whole dashboard payload**
  (`analyze.bulk_json` over the materialized DuckDB) as JSON (~250 KB raw / ~60 KB gz). Cached per `tz`.
  This is what the browser renders — no in-browser DB build, no Pyodide.
- `GET /api/local-trace/session-detail?id=<sessionId>` — one session's per-round timeline
  (`analyze.session_detail_json`), fetched when a session is opened.
- `GET /api/local-trace/duckdb` — stream the gzipped materialized DuckDB (`trace.duckdb.gz`). The browser
  fetches this in the **background** and primes its query cache (keyed by `sha256(sanitized.gz)`) so the
  assistant's in-browser DuckDB load is a cache hit (skips the slow WASM materialize).
- `GET /api/local-trace/sanitized` — stream `LOCAL_EXPORT_DIR/sanitized.jsonl.gz` (404 until prepared);
  fetched in the background for Contribute + the assistant handoff.
- `GET /api/local-trace/round-raw?key=<traceKey>` — one round's LOCAL-only original input/output text
  by sanitized `trace_key`, from the `round_raw.json` the native prepare wrote. This is the mapping the
  browser can't reconstruct from sanitized rows, so it drives the per-round raw drill-down.
- `WS /api/chat/ws`, `* /api/{path}` — reverse-proxied (chat socket pumped frame-by-frame; HTTP
  streamed both ways with `X-Forwarded-For` appended). Frontend role → the remote master; master-server
  role → `/api/chat*` to the AI backend, the rest to the contribute backend.
- `/` and everything else (declared LAST so the `/api/*` routes win):
  - **frontend role** — the *whole site* is reverse-proxied to the master, so every page is the master's
    real page with real data (no local `dist/`, no redirects). Only Analyze behaves differently, because
    it detects `/api/sidecar-info` + `/api/local-trace/*` and runs the local executor.
  - **master-server role** — the static site is served from local `dist/`.

## Security notes

- **Frontend role is loopback-only, enforced.** It serves your raw session bytes, so a non-loopback
  `LOCAL_SIDECAR_HOST` is refused and forced to `127.0.0.1` in the process itself — it can never face
  the network. Only the master-server role (local-trace disabled) may bind a public interface.
- **The master never reads local sessions.** In the master-server role the local-trace endpoints are
  not even registered — there is no code path that opens `~/.claude` / `~/.codex`.
- **Sanitize-in-browser is preserved.** The sidecar ships raw bytes only to *your* browser on
  localhost; normalization + sanitization run in Pyodide before any contribution leaves the machine.
- **The remote master is unchanged.** Cross-machine traffic is same-origin to the sidecar, which
  proxies it onward — no CORS config and no new exposure on the master side.
