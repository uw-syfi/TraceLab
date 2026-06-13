// Service worker: persistently cache the heavy, version-immutable Pyodide runtime + packages so the
// in-browser analysis (Analyze figures + the Ask-the-trace QA executor) only downloads them once.
// Without this, every cold boot re-pulls ~40 MB from the CDN and the browser HTTP cache isn't a
// reliable backstop (eviction, no guaranteed persistence).
//
// Scope is deliberately narrow — ONLY the Pyodide CDN. App HTML/JS keep their normal network
// behavior, so deploys are never served stale; everything that isn't a Pyodide asset passes through.

const PYODIDE_CACHE = 'pyodide-cdn-v1';
const PYODIDE_HOST = 'cdn.jsdelivr.net';
const PYODIDE_PREFIX = '/pyodide/';

self.addEventListener('install', () => {
  // Take over as soon as installed so the cache helps the current session, not just the next load.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // Drop pyodide caches from an older cache-name bump.
      const names = await caches.keys();
      await Promise.all(
        names.filter((n) => n.startsWith('pyodide-cdn-') && n !== PYODIDE_CACHE).map((n) => caches.delete(n)),
      );
      await self.clients.claim();
    })(),
  );
});

function isPyodideAsset(url) {
  return url.host === PYODIDE_HOST && url.pathname.startsWith(PYODIDE_PREFIX);
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  let url;
  try {
    url = new URL(req.url);
  } catch {
    return;
  }
  if (!isPyodideAsset(url)) return; // pass through — only the Pyodide CDN is cached here

  event.respondWith(
    (async () => {
      const cache = await caches.open(PYODIDE_CACHE);
      // ignoreVary: jsdelivr sets `Vary: Accept-Encoding`, which would otherwise miss across boots.
      const hit = await cache.match(req, { ignoreVary: true });
      if (hit) return hit;
      const res = await fetch(req);
      // Cache only complete 200s (skip 206 range partials / errors / opaque responses).
      if (res && res.status === 200) cache.put(req, res.clone()).catch(() => {});
      return res;
    })(),
  );
});
