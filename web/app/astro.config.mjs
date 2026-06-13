import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'astro/config';

// Repo root — the web app imports the single-source price table from artifacts/utils/pricing.json,
// which lives outside the Vite project root, so the dev server must be allowed to read it.
const repoRoot = fileURLToPath(new URL('../..', import.meta.url));

// Static site. The Pyodide payload + pre-rendered figures live under public/ and are
// copied verbatim into dist/. No server runtime for Phases 1+2.
// Public hostnames allowed by the dev/preview server come from env (ALLOWED_HOSTS, comma-separated)
// so no deployment hostname is baked into the repo. Empty default = localhost-only (Vite's default).
const allowedHosts = (process.env.ALLOWED_HOSTS || '')
  .split(',')
  .map((h) => h.trim())
  .filter(Boolean);

// Centralized service ports (config/services.json) so dev-proxy targets stay in one place; env vars
// (AI_API_ORIGIN / CONTRIB_API_ORIGIN) still override for non-default hosts/ports.
const services = JSON.parse(readFileSync(new URL('../../config/services.json', import.meta.url), 'utf8'));
const aiOrigin = process.env.AI_API_ORIGIN || `http://127.0.0.1:${services.ports.ai_backend}`;
const contribOrigin = process.env.CONTRIB_API_ORIGIN || `http://127.0.0.1:${services.ports.contribute_backend}`;
const hmrHost = process.env.VITE_HMR_HOST || process.env.HMR_HOST || '';
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT || process.env.HMR_CLIENT_PORT || 0);

export default defineConfig({
  output: 'static',
  // Warm linked pages on hover so gallery → /exp/<slug> navigation has nothing left to fetch
  // on click (the HTML/CSS land while the cursor is still on the card).
  prefetch: {
    prefetchAll: true,
    defaultStrategy: 'hover',
  },
  build: {
    assets: '_assets',
    inlineStylesheets: 'always',
  },
  server: {
    allowedHosts,
  },
  preview: {
    allowedHosts,
  },
  vite: {
    optimizeDeps: {
      // The floating Ask-the-data launcher mounts through askRender.ts. If these are optimized
      // lazily, a remote dev browser can hit Vite's "Outdated Optimize Dep" 504 and keep the
      // visible launcher without its click handler. echarts is large and triggers the same
      // mid-session re-optimize 504 on the /lab analytics page, so pre-bundle it too.
      include: ['dompurify', 'marked', 'echarts'],
    },
    worker: {
      // analyze.worker.ts is an ES module worker.
      format: 'es',
    },
    server: {
      // allow importing the single-source pricing.json from artifacts/ (outside the app root)
      fs: { allow: [repoRoot] },
      ...(hmrHost
        ? {
            hmr: {
              host: hmrHost,
              clientPort: hmrClientPort || 60990,
              protocol: process.env.VITE_HMR_PROTOCOL || 'ws',
            },
          }
        : {}),
      // Dev only: two same-origin /api backends (no CORS). They're separate services with very
      // different profiles, so the proxy splits by prefix — order matters, specific key first:
      //   /api/chat/ws         -> AI sidecar (web/ai_infra/app.py, :60980), `ws: true` upgrades it
      //   /api/pool, /api/contribute*  -> Contribute sidecar (web/server/app.py, :60981)
      // In production a reverse proxy serves dist/ and applies the same two-upstream split.
      // `xfwd: true` makes http-proxy APPEND the real client IP to X-Forwarded-For. The sidecars
      // bind 127.0.0.1 (reachable only through this proxy), so they trust the right-most XFF entry
      // as the genuine client IP — that's what rate limiting + the audit logs key on.
      proxy: {
        '/api/chat': { target: aiOrigin, changeOrigin: true, ws: true, xfwd: true },
        '/api': { target: contribOrigin, changeOrigin: true, xfwd: true },
      },
    },
  },
});
