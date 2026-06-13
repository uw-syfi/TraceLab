# Contribute sidecar (`web/server`)

A small FastAPI service that lets visitors donate their **already-sanitized** trace to the
community pool. It is a separate process from the static Astro site (`web/app`) and from the
Ask-the-trace AI sidecar (`web/ai_infra`, port `60980`). In production a reverse proxy serves
`dist/` and splits `/api/*` between the two backends (`/api/chat` → AI, the rest → here); in dev
Vite proxies the same split. Ports live in `config/services.json`.

## Run

```bash
uv sync --extra server
# Reads its port (60981) from config/services.json:
uv run --extra server python -m web.server               # from the repo root
#   or, with hot-reload on an explicit port:
uv run uvicorn web.server.app:app --port 60981 --reload  # from the repo root
```

`CONTRIB_DIR` (default `web/server/data/contributed`) is where the pool lives. Other env knobs:
`CONTRIB_MAX_UPLOAD_BYTES`, `CONTRIB_RATE_LIMIT_MAX`, `CONTRIB_RATE_LIMIT_WINDOW_S`.

## Endpoints

- `POST /api/contribute` — multipart `file` (gzip of normalized, sanitized JSONL rows) + `consent`.
  Pipeline: size cap → gzip integrity → JSONL parse → schema sniff → **reject** (422, with the
  offending key-paths) if any sensitive key or `tools[].input` survives → content-hash idempotency
  → **session-fingerprint dedup** (skip already-seen sessions) → append the new rows as one
  immutable `uploads/<sha>.jsonl.gz` → fold subtotals into running totals → rewrite `summary.json`.
  Returns `{accepted, duplicate, new_sessions, skipped_sessions, rows_added}`.
- `GET /api/pool` — the cached `PoolPreview` snapshot the dashboard hydrates from.
- `GET /api/health` — liveness.

## Design notes

- **Validation is shared, not duplicated.** The gate imports the same rules the sanitizer uses
  from [`scripts/trace_privacy.py`](../../scripts/trace_privacy.py). It rejects leaks; it never
  scrubs.
- **Dedup is seed-invariant and whole-session.** See `fingerprint.py`: the sanitizer can mangle
  ids differently per run (`--random-seed`) but never alters token counts, so a per-session token
  series identifies a session across independent sanitizations. Already-seen sessions are skipped;
  new sessions are appended intact (rows are never dropped — duplicate `trace_key`s are legitimate).
- **Counters are bookkept, not rescanned.** `stats.py` keeps additive running totals in
  `index.json`; `rebuild_stats()` is the cold-path full recompute for recovery/audits only.
- **Storage is local now, S3 later.** Everything goes through the `Store` protocol in `store.py`;
  an `S3Store` is a drop-in replacement.

## Known limitations

- A session re-uploaded later with *more* rounds has a different fingerprint, so the overlap is not
  deduped (documented; a future prefix/length fingerprint can address it).
- `contributors` is the accepted-upload count — no real identity exists post-sanitization.
- Index writes are serialized with an `asyncio.Lock` (single worker). Use a file lock for multiple
  workers.
