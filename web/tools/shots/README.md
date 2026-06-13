# Mockup screenshots (headless)

A small, self-contained dev utility for rendering the `web/mockups/` pages with a headless
Chromium and capturing screenshots — handy for reviewing the Ask-the-trace assistant without
opening a browser by hand. Kept separate from the Astro app so Playwright never lands in the
app's dependency tree.

## Setup (once)

```bash
cd web/tools/shots
npm install            # installs playwright
npm run browser        # downloads Chromium into ~/.cache/ms-playwright (shared, reused)
```

## Use

```bash
npm run shoot                                  # default: ../../mockups/scheme-04-atelier.html
node shoot.mjs ../../mockups/scheme-04-atelier.html
node shoot.mjs https://example.com out2        # any page + a custom output dir
```

Or from `web/`: `just shots`.

Screenshots land in `out/` (gitignored). For the assistant mockup the script drives the real
flows and writes:

- `00-load.png` — the page on load
- `01-public-answer.png` — gallery teaser → public dock, a live answer rendered inline
- `02-expanded.png` — the dock expanded to full page (history sidebar pinned)
- `03-user-gate.png` — Analyze tab → launcher defaults to "your trace" → consent gate

It exits non-zero on any page error (or if the context-aware default source regresses), so it
doubles as a smoke test.
