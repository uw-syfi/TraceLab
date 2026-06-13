# SyFI Trace Atlas — Web UI Plan

A web UI over the coding-trace analysis toolkit. Three jobs:
1. **Display what we have** — the curated public pool + its analysis figures.
2. **Analyze your own trace** — upload a sanitized `.gz`, get figures back.
3. **Contribute** — optionally donate your sanitized trace to a community pool.

## Key architectural decisions (locked)

- **Upload format:** users upload a gzip of normalized, **already-sanitized** round
  rows — byte-shape identical to the released `syfi_coding_trace.jsonl.gz`. No
  server-side extraction; no mandatory re-sanitize.
- **"Analyze your trace" runs in the browser (Pyodide / WASM).** The existing Python
  experiments only depend on `numpy` + `matplotlib` (+ `PIL` for png_sidecar) and use no
  `subprocess`/`multiprocessing`, so they run unchanged in Pyodide. The trace never leaves
  the user's machine. This removes the server job queue and per-user storage entirely.
- **The head/overview page is pre-rendered server-side once** over the full pool (too big
  to run in every visitor's browser).
- **Contributions are collected, not auto-merged.** On contribute: server validation gate
  (gzip integrity + schema sniff + reject if any sensitive key or `tools[].input` remains)
  → dedup on `trace_key` → append to a *contributed* pool → re-run the cheap
  `overview_summary` (counters only, no matplotlib) → show on a **separate Contributed
  dashboard**. The curated head page is untouched until a maintainer promotes.

## Phases

- **Phase 0 — Scheme selection (current):** 4 self-contained, fake-data HTML mockups in
  distinct visual directions. Pick one (or a mix); iterate on the winner. No backend.
- **Phase 1 — Head gallery (real data):** static site over the curated pool's
  `overview_summary` + existing figure gallery.
- **Phase 2 — Analyze-my-trace (Pyodide):** in-browser driver replacing `run_all.py`'s
  dispatcher; upload `.gz` → analyze in a Web Worker → personal gallery.
- **Phase 3 — Contribute + Contributed dashboard:** consent → POST `.gz` → validation gate
  → append + dedup → cheap re-count → separate dashboard.
- **Phase 4 — Promotion + polish:** maintainer promote contributed → head, periodic full
  re-render, release snapshot.

## Mockups (Phase 0)

Open `index.html` to compare. Both show the same three surfaces with the same fake data,
so the choice is about the *scheme*, not the numbers.

**Decision (iteration 2):** `scheme-04-atelier.html` is the **chosen direction**.
`scheme-03-control-room.html` is **kept as an alternate/draft** for reference (its
professional palette and data-dense layout may feed a future "pro/dark" mode).
Schemes 01 (terminal) and 02 (field-notes) were dropped.

- `scheme-04-atelier.html` — **SELECTED** — light, warm soft-luxury, organic; Fraunces
  headings + legible tabular numerals. Tabs: Overview · **Provider Comparison** (Claude vs
  Codex, terracotta/sage) · Analyze your trace · Contributed pool.
- `scheme-03-control-room.html` — kept draft — dark, glassy analytics cockpit; restrained
  professional palette (calm blue + muted teal).
