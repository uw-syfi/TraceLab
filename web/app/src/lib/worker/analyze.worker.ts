/// <reference lib="webworker" />
// Pyodide worker: boots CPython-in-WASM, mounts the curated toolkit payload into MEMFS,
// and runs driver.py over the user's trace. Everything here is off the main thread; the
// raw trace is written to MEMFS and never posted back — only figures and the summary are.

import type { MainMsg, WorkerMsg } from './protocol';
import type { RoundRaw } from '../analytics/types';

// Pyodide runtime served from the CDN. Vendor under /pyodide/ and flip this base for
// offline/deterministic builds (see web plan §6).
const PYODIDE_VERSION = 'v0.27.2';
const PYODIDE_BASE = `https://cdn.jsdelivr.net/pyodide/${PYODIDE_VERSION}/full/`;

const MOUNT = '/repo'; // in-MEMFS REPO_ROOT (matches driver.run's first arg)
const WORK_DIR = '/work';
const TRACE_PATH = `${WORK_DIR}/input.jsonl`;
const TRACE_DB_PATH = `${WORK_DIR}/trace.duckdb`;
const QA_OUT = '/out';

// Content-addressed cache of the materialized DuckDB. Building it from the JSONL is the slow part of
// loading a trace, and the same trace always yields a valid DB, so we keep it in the Cache API across
// reloads/sessions keyed by the trace's content hash. The cache *name* carries the Pyodide version
// plus the materializer's schema version (computed at runtime from trace_db) so any schema/format
// change auto-invalidates. The Analyze figures path and the assistant's QA path share this cache, so
// whichever materializes the DB first lets the other skip the rebuild.
const DUCKDB_CACHE_MAX_BYTES = 256 * 1024 * 1024; // don't cache absurdly large DBs
// Where driver.run materializes the shared trace DuckDB (output_root unset): tempdir +
// "coding_trace_driver" + "<trace stem>.duckdb". TRACE_PATH's stem is "input".
const DRIVER_DB_PATH = '/tmp/coding_trace_driver/input.duckdb';

const ctx = self as unknown as DedicatedWorkerGlobalScope;
const post = (msg: WorkerMsg, transfer?: Transferable[]) =>
  transfer ? ctx.postMessage(msg, transfer) : ctx.postMessage(msg);

// Turn any thrown value into a single, never-empty line for the UI. Pyodide PythonErrors carry the
// FULL traceback in `.message`: the first line is the useless "Traceback (most recent call last):"
// and the LAST line is often a hint continuation (DuckDB prints "Candidate Entries: …" / "LINE 1: …"
// AFTER the actual error), so neither end is reliable. The real cause is the "Pkg.SomeError: message"
// line — we return the last such match. Falls back to the last traceback line, then the first line,
// then the error's class name, so it never posts an empty string.
const EXC_LINE = /^[\w.]+(Error|Exception|Warning|Interrupt|Exit)\b.*:/;
function formatErr(err: unknown): string {
  if (err instanceof Error) {
    const lines = err.message.split('\n').map((l) => l.trim()).filter(Boolean);
    if (!lines.length) return err.name || 'Unknown error';
    for (let i = lines.length - 1; i >= 0; i--) {
      if (EXC_LINE.test(lines[i])) return lines[i];
    }
    const isTraceback = lines[0].startsWith('Traceback (most recent');
    return (isTraceback ? lines[lines.length - 1] : lines[0]) || err.name || 'Unknown error';
  }
  const s = String(err).trim();
  return s && s !== '[object Object]' ? s : 'Unknown error';
}

interface Manifest {
  packages: string[];
  files: string[];
}

let pyodidePromise: Promise<any> | null = null;
// Packages are loaded by *role*, not all-up-front: ingest is pure stdlib, materialize needs only
// duckdb, and the heavy plotting stack (numpy/matplotlib/Pillow) is needed only by figure/QA workers.
// matplotlib dominates package-load time, so loading it only where it's used roughly halves the
// prepare boot and cuts CDN contention with the figure pool.
const loadedPackages = new Set<string>();
let allPackages: string[] = ['numpy', 'matplotlib', 'Pillow', 'duckdb'];

function pkgLabel(pkgs: string[]): string {
  if (pkgs.includes('matplotlib')) return 'numpy, matplotlib, Pillow';
  if (pkgs.length === 1 && pkgs[0] === 'duckdb') return 'DuckDB';
  return pkgs.join(', ');
}

function mkdirp(FS: any, dir: string): void {
  let cur = '';
  for (const part of dir.split('/')) {
    if (!part) continue;
    cur += `/${part}`;
    try {
      FS.mkdir(cur);
    } catch {
      /* already exists */
    }
  }
}

async function mountPayload(pyodide: any, manifest: Manifest): Promise<void> {
  const FS = pyodide.FS;
  mkdirp(FS, MOUNT);
  for (const rel of manifest.files) {
    const res = await fetch(`/py/${rel}`);
    if (!res.ok) throw new Error(`payload fetch failed (${res.status}): ${rel}`);
    const bytes = new Uint8Array(await res.arrayBuffer());
    const dest = `${MOUNT}/${rel}`;
    mkdirp(FS, dest.slice(0, dest.lastIndexOf('/')));
    FS.writeFile(dest, bytes);
  }
}

// Load the Pyodide payload manifest with a clear failure when /py/ isn't actually serving JSON.
// Without this, a missing/misrouted payload (server returns the SPA HTML for /py/manifest.json) makes
// `.json()` throw the opaque `Unexpected token '<', "<!doctype "...` — which then surfaces in the UI.
async function fetchManifest(): Promise<Manifest> {
  const res = await fetch('/py/manifest.json', { headers: { Accept: 'application/json' } });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(
      `Analysis payload not found at /py/manifest.json (HTTP ${res.status}). The Pyodide payload isn't being served — run \`npm run build:payload\` (npm run dev/build do this automatically) and hard-refresh.`,
    );
  }
  try {
    return JSON.parse(text) as Manifest;
  } catch {
    const looksHtml = text.trimStart().startsWith('<');
    throw new Error(
      looksHtml
        ? "/py/manifest.json returned an HTML page instead of JSON — the analysis payload isn't being served at /py/. Make sure you're on the dev server (`npm run dev`) or a build that includes /py/, then hard-refresh."
        : '/py/manifest.json was not valid JSON.',
    );
  }
}

/** Idempotent, incremental package load — only fetches packages not already loaded by this worker. */
async function ensurePackages(pyodide: any, packages: string[]): Promise<void> {
  const need = packages.filter((p) => !loadedPackages.has(p));
  if (!need.length) return;
  post({ type: 'boot-progress', message: `Loading ${pkgLabel(need)}…` });
  await pyodide.loadPackage(need);
  need.forEach((p) => loadedPackages.add(p));
}

let rendererPrimed = false;
// matplotlib's first import + font-cache build + first Agg draw is a ~4s tax that's otherwise paid
// lazily on the FIRST real figure — on the critical path, and NOT hidden by warm-boot (loading the
// package doesn't import pyplot). Pay it up front, during warm-boot, with a throwaway text render at
// the real save DPI so the first actual figure is fast. Only meaningful where matplotlib is loaded.
async function primeRenderer(pyodide: any): Promise<void> {
  if (rendererPrimed || !loadedPackages.has('matplotlib')) return;
  rendererPrimed = true;
  post({ type: 'boot-progress', message: 'Warming the figure renderer…' });
  await pyodide.runPythonAsync(`
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as _plt
import io as _io
_f = _plt.figure(figsize=(2, 1.5), dpi=260)  # match artifacts/utils/style.SAVE_DPI
_ax = _f.add_subplot(111)
_ax.plot([0, 1, 2], [0, 1, 0.5])
_ax.set_title('warm'); _ax.set_xlabel('x'); _ax.set_ylabel('y')  # touch the font cache
_buf = _io.BytesIO()
_f.savefig(_buf, format='png')  # first Agg draw + PNG encode
_plt.close(_f)
del _f, _ax, _buf
`);
}

// Boot = runtime + mounted toolkit only; packages are loaded separately by role (see `packages`).
// `packages` defaults to the full plotting stack so figure/QA workers are unchanged; the prepare
// worker passes a slim subset (duckdb, or nothing) so it never pays for matplotlib it won't use.
async function boot(packages?: string[]): Promise<any> {
  if (!pyodidePromise) {
    pyodidePromise = (async () => {
      post({ type: 'boot-progress', message: 'Loading Python runtime…' });
      const { loadPyodide } = await import(/* @vite-ignore */ `${PYODIDE_BASE}pyodide.mjs`);
      const pyodide = await loadPyodide({ indexURL: PYODIDE_BASE });

      const manifest: Manifest = await fetchManifest();
      if (Array.isArray(manifest.packages) && manifest.packages.length) allPackages = manifest.packages;

      post({ type: 'boot-progress', message: 'Mounting analysis toolkit…' });
      await mountPayload(pyodide, manifest);

      pyodide.runPython(`
import os, sys
os.environ['MPLBACKEND'] = 'Agg'
os.environ.setdefault('MPLCONFIGDIR', '/tmp/mpl')
os.makedirs('/tmp/mpl', exist_ok=True)
if ${JSON.stringify(MOUNT)} not in sys.path:
    sys.path.insert(0, ${JSON.stringify(MOUNT)})
import driver  # warm the import so first analyze is faster (stdlib-only at import time)
`);
      // Announce 'booted' as soon as runtime+toolkit are up, BEFORE packages. The shard pool keys its
      // boot cascade (worker 0 → the rest) off this, so the siblings boot in parallel with worker 0's
      // package load. Measured: this parallel boot beats waiting for packages (the browser HTTP cache
      // coalesces the concurrent CDN fetches; serializing them is slower). analyze() still awaits
      // ensurePackages below, so work never starts before its packages are in.
      post({ type: 'booted' });
      return pyodide;
    })();
  }
  const pyodide = await pyodidePromise;
  await ensurePackages(pyodide, packages ?? allPackages);
  await primeRenderer(pyodide); // no-op unless matplotlib was loaded; runs during warm-boot
  return pyodide;
}

async function gunzip(data: Uint8Array): Promise<Uint8Array> {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

// ---- materialized-DuckDB cache (Cache API) -----------------------------------------------
async function sha256Hex(buf: ArrayBuffer): Promise<string | null> {
  try {
    const digest = await crypto.subtle.digest('SHA-256', buf);
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return null; // insecure context / unavailable → caller just rebuilds without caching
  }
}

function duckdbCacheKey(hash: string): string {
  // Synthetic same-origin key; never hits the network (we only use Cache.match/put directly).
  return `${self.location.origin}/__syfi_duckdb__/${hash}`;
}

// Cache name = pyodide version + the materializer's schema digest (trace_db._schema_version), so a
// schema change invalidates stale DBs automatically. Memoized; computes once per worker, then prunes
// entries from older names.
let duckdbCacheNameMemo: string | null = null;
async function duckdbCacheName(pyodide: any): Promise<string> {
  if (duckdbCacheNameMemo) return duckdbCacheNameMemo;
  let schemaVersion = 'x';
  try {
    schemaVersion = String(
      await pyodide.runPythonAsync(`
import sys
from pathlib import Path
__u = str(Path(${JSON.stringify(MOUNT)}) / "artifacts" / "utils")
if __u not in sys.path:
    sys.path.insert(0, __u)
import trace_db
trace_db._schema_version()
`),
    );
  } catch {
    /* fall back to a fixed tag — still correct, just coarser invalidation */
  }
  const name = `syfi-duckdb-${PYODIDE_VERSION}-${schemaVersion}`;
  duckdbCacheNameMemo = name;
  if (typeof caches !== 'undefined') {
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names.filter((n) => n.startsWith('syfi-duckdb-') && n !== name).map((n) => caches.delete(n)),
        ),
      )
      .catch(() => {});
  }
  return name;
}

async function readCachedDuckdb(cacheName: string, hash: string): Promise<ArrayBuffer | null> {
  if (typeof caches === 'undefined') return null;
  try {
    const cache = await caches.open(cacheName);
    const hit = await cache.match(duckdbCacheKey(hash));
    return hit ? await hit.arrayBuffer() : null;
  } catch {
    return null;
  }
}

async function writeCachedDuckdb(cacheName: string, hash: string, bytes: Uint8Array): Promise<void> {
  if (typeof caches === 'undefined' || bytes.byteLength > DUCKDB_CACHE_MAX_BYTES) return;
  try {
    const cache = await caches.open(cacheName);
    // Copy out of MEMFS into a standalone buffer so the Response owns its bytes.
    const body = bytes.slice().buffer;
    await cache.put(
      duckdbCacheKey(hash),
      new Response(body, { headers: { 'Content-Type': 'application/octet-stream' } }),
    );
  } catch {
    /* cache full / unavailable — non-fatal, we just rebuild next time */
  }
}

interface ShardOpts {
  shardIndex: number;
  shardCount: number;
  sessionTotal: number;
  includeOverview: boolean;
}

async function analyze(bytes: ArrayBuffer, gzip: boolean, shard: ShardOpts): Promise<void> {
  const pyodide = await boot();
  const FS = pyodide.FS;
  mkdirp(FS, WORK_DIR);

  // Reuse a cached materialized DuckDB when present: the prepare step builds+caches it before Analyze
  // starts, so every shard worker (and the assistant's QA path) hits the Cache API and skips both the
  // gunzip and the per-worker materialize. All v1 experiments are db-backed, so the JSONL isn't even
  // needed on a hit. Falls back to today's materialize-per-worker on a miss.
  const hash = await sha256Hex(bytes);
  const cacheName = hash ? await duckdbCacheName(pyodide) : null;
  let reuseDb = false;
  if (hash && cacheName) {
    const cached = await readCachedDuckdb(cacheName, hash);
    if (cached) {
      mkdirp(FS, DRIVER_DB_PATH.slice(0, DRIVER_DB_PATH.lastIndexOf('/')));
      FS.writeFile(DRIVER_DB_PATH, new Uint8Array(cached));
      reuseDb = true;
    }
  }
  if (!reuseDb) {
    let data = new Uint8Array(bytes);
    if (gzip) data = await gunzip(data);
    FS.writeFile(TRACE_PATH, data);
  }

  const driver = pyodide.pyimport('driver');
  const gen = driver.run.callKwargs(MOUNT, TRACE_PATH, {
    shard_index: shard.shardIndex,
    shard_count: shard.shardCount,
    session_total: shard.sessionTotal,
    include_overview: shard.includeOverview,
    reuse_db: reuseDb,
  });
  try {
    for (const ev of gen) {
      const kind = ev.get('type');
      if (kind === 'progress') {
        post({
          type: 'progress',
          experiment: ev.get('experiment'),
          index: ev.get('index'),
          total: ev.get('total'),
        });
      } else if (kind === 'summary') {
        const proxy = ev.get('data');
        const dataObj = proxy.toJs({ dict_converter: Object.fromEntries });
        proxy.destroy();
        post({ type: 'summary', data: dataObj });
      } else if (kind === 'figure') {
        const proxy = ev.get('png');
        const u8: Uint8Array = proxy.toJs();
        proxy.destroy();
        const buf = u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength);
        post(
          { type: 'figure', experiment: ev.get('experiment'), name: ev.get('name'), png: buf },
          [buf],
        );
      } else if (kind === 'error') {
        post({ type: 'error', experiment: ev.get('experiment'), message: ev.get('message') });
      }
      ev.destroy();
    }
    post({ type: 'done' });

    // Publish the materialized DuckDB so the assistant's QA executor can reuse it (no rebuild). Only
    // worker 0 builds the db (for the overview), so only it needs to; skip when we reused a cache hit
    // (already present). Best-effort and guarded.
    if (shard.includeOverview && !reuseDb && hash && cacheName) {
      try {
        const dbBytes = FS.readFile(DRIVER_DB_PATH) as Uint8Array; // throws if the db wasn't built
        await writeCachedDuckdb(cacheName, hash, dbBytes);
      } catch {
        /* db absent/unreadable — the assistant will just build (and cache) its own */
      }
    }
  } finally {
    gen.destroy();
    driver.destroy();
  }
}

// Format-aware ingest: sniff/normalize/sanitize whatever was dropped into a sanitized .gz, entirely
// in the worker (raw rows never cross back). When buildDb, also materialize + cache the DuckDB under
// the sanitized .gz content hash, so the Analyze shard pool and the assistant's QA path reuse it.
const PREPARE_DIR = `${WORK_DIR}/prepare`;
// LOCAL-only per-round originals sidecar (ingest writes it here; never sanitized/cached/uploaded).
// Read lazily per round by `prepare-get-raw`; wiped with PREPARE_DIR at the start of each prepare.
const ROUND_RAW_PATH = `${PREPARE_DIR}/round_raw.json`;

async function prepareTrace(
  requestId: string,
  bytes: ArrayBuffer,
  filename: string,
  buildDb: boolean,
  trusted: boolean,
): Promise<void> {
  // Ingest is pure stdlib; materialize needs only duckdb. Never load the plotting stack here —
  // this worker only ever prepares, so skipping matplotlib roughly halves its boot.
  const pyodide = await boot(buildDb ? ['duckdb'] : []);
  const FS = pyodide.FS;

  // Fresh work dir each run so a prior upload's members/artifacts can't leak in. Also drop any
  // resident raw map from a previous prepare — the sidecar path is reused, so a stale in-memory map
  // would otherwise shadow the new trace's originals.
  try {
    pyodide.runPython(
      `import shutil; shutil.rmtree(${JSON.stringify(PREPARE_DIR)}, ignore_errors=True)\n` +
        `globals().pop('__round_raw_map', None); globals().pop('__round_raw_src', None)`,
    );
  } catch {
    /* ignore */
  }
  mkdirp(FS, PREPARE_DIR);
  const uploadPath = `${PREPARE_DIR}/upload.bin`;
  FS.writeFile(uploadPath, new Uint8Array(bytes));

  // Per-stage hints: ingest.prepare calls this back at each conversion boundary (detecting /
  // extracting / sanitizing). Posts land on the main thread live, even while Python is running.
  const emitStage = (stage: unknown) =>
    post({ type: 'prepare-progress', requestId, message: String(stage) });
  pyodide.globals.set('__ingest_input', uploadPath);
  pyodide.globals.set('__ingest_work', PREPARE_DIR);
  pyodide.globals.set('__ingest_progress', emitStage);
  pyodide.globals.set('__ingest_trusted', trusted);
  let metaJson: string;
  try {
    metaJson = await pyodide.runPythonAsync(`
import json, sys
if ${JSON.stringify(MOUNT)} not in sys.path:
    sys.path.insert(0, ${JSON.stringify(MOUNT)})
import ingest
json.dumps(ingest.prepare(__ingest_input, __ingest_work, progress=__ingest_progress, trusted=__ingest_trusted))
`);
  } finally {
    try {
      pyodide.globals.delete('__ingest_input');
      pyodide.globals.delete('__ingest_work');
      pyodide.globals.delete('__ingest_progress');
      pyodide.globals.delete('__ingest_trusted');
    } catch {
      /* ignore */
    }
  }

  const full = JSON.parse(metaJson) as {
    kind: 'raw' | 'normalized' | 'sanitized';
    providers: string[];
    sessions: number;
    rounds: number;
    tools: number;
    produced: { normalized: boolean; sanitized: boolean };
    warnings: string[];
    rawAvailable: boolean;
    titles: Record<string, string>;
    files: {
      sanitized_jsonl: string;
      sanitized_gz: string;
      normalized_gz: string | null;
      round_raw_json: string | null;
    };
  };

  // Copy artifacts out of MEMFS into standalone buffers the message can transfer.
  const sanitizedGz = (FS.readFile(full.files.sanitized_gz) as Uint8Array).slice().buffer;
  let normalizedGz: ArrayBuffer | undefined;
  if (full.files.normalized_gz) {
    normalizedGz = (FS.readFile(full.files.normalized_gz) as Uint8Array).slice().buffer;
  }

  // Build + cache the DuckDB keyed by the sanitized .gz hash (what Analyze/QA will hash too).
  if (buildDb) {
    emitStage('Building your database');
    try {
      const hash = await sha256Hex(sanitizedGz);
      const cacheName = hash ? await duckdbCacheName(pyodide) : null;
      if (hash && cacheName) {
        const dbPath = `${PREPARE_DIR}/sanitized.duckdb`;
        pyodide.globals.set('__mat_src', full.files.sanitized_jsonl);
        pyodide.globals.set('__mat_db', dbPath);
        try {
          await pyodide.runPythonAsync(`
import sys
from pathlib import Path
__u = str(Path(${JSON.stringify(MOUNT)}) / "artifacts" / "utils")
if __u not in sys.path:
    sys.path.insert(0, __u)
import trace_db
__p = Path(__mat_db)
if __p.exists():
    __p.unlink()
trace_db.materialize(__mat_src, __p)
`);
        } finally {
          try {
            pyodide.globals.delete('__mat_src');
            pyodide.globals.delete('__mat_db');
          } catch {
            /* ignore */
          }
        }
        const dbBytes = FS.readFile(dbPath) as Uint8Array;
        await writeCachedDuckdb(cacheName, hash, dbBytes);
      }
    } catch {
      /* best-effort: Analyze/QA will materialize (and cache) on a miss */
    }
  }

  const meta = {
    kind: full.kind,
    providers: full.providers,
    sessions: full.sessions,
    rounds: full.rounds,
    tools: full.tools,
    produced: full.produced,
    warnings: full.warnings,
    rawAvailable: full.rawAvailable,
    titles: full.titles ?? {},
  };
  const transfer = normalizedGz ? [sanitizedGz, normalizedGz] : [sanitizedGz];
  post({ type: 'prepared', requestId, meta, sanitizedGz, normalizedGz }, transfer);
}

// Look up one round's LOCAL-only raw text by trace_key from the just-prepared trace. The sidecar
// (PREPARE_DIR/round_raw.json) can be tens of MB, so we parse it once into a resident dict (keyed
// by the *sanitized* trace_key, matching the payload) and serve subsequent lookups from memory. The
// map is dropped on the next prepare (see the wipe above) and never built unless a round is opened.
async function getRoundRawFromPrepare(requestId: string, traceKey: string): Promise<void> {
  const pyodide = await boot();
  pyodide.globals.set('__raw_path', ROUND_RAW_PATH);
  pyodide.globals.set('__raw_key', traceKey);
  let rawJson: string;
  try {
    rawJson = await pyodide.runPythonAsync(`
import json, os
_g = globals()
_m = _g.get('__round_raw_map')
if _m is None:
    if os.path.exists(__raw_path):
        with open(__raw_path, 'r', encoding='utf-8') as _f:
            _m = json.load(_f)
    else:
        _m = {}
    _g['__round_raw_map'] = _m
    _g['__round_raw_src'] = __raw_path
json.dumps(_m.get(__raw_key))
`);
  } finally {
    try {
      pyodide.globals.delete('__raw_path');
      pyodide.globals.delete('__raw_key');
    } catch {
      /* ignore */
    }
  }
  const raw = JSON.parse(rawJson) as RoundRaw | null;
  post({ type: 'prepare-raw', requestId, raw });
}

// Local-executor: prime the DuckDB cache with a server-built .duckdb so the assistant's later
// qa-load-trace is a cache HIT (no in-browser materialize). The key is sha256(sanitizedGz) — exactly
// what qa-load-trace hashes — and the cache name needs the schema digest, so we boot (duckdb only) to
// compute it. Best-effort: a miss just means the assistant materializes the DB itself, as today.
async function primeDuckdb(requestId: string, sanitizedGz: ArrayBuffer, dbGz: ArrayBuffer): Promise<void> {
  let ok = false;
  try {
    const pyodide = await boot(['duckdb']);
    const hash = await sha256Hex(sanitizedGz);
    if (hash) {
      const cacheName = await duckdbCacheName(pyodide);
      const dbBytes = await gunzip(new Uint8Array(dbGz)); // .duckdb.gz -> .duckdb, off the main thread
      await writeCachedDuckdb(cacheName, hash, dbBytes);
      ok = true;
    }
  } catch {
    /* best-effort — leave ok=false */
  }
  post({ type: 'primed-duckdb', requestId, ok });
}

async function loadTraceForQa(
  requestId: string,
  bytes: ArrayBuffer,
  gzip: boolean,
  filename: string,
  packages?: string[],
): Promise<void> {
  const pyodide = await boot(packages); // duckdb-only for the dashboard; full stack for the assistant
  const FS = pyodide.FS;
  mkdirp(FS, WORK_DIR);

  // Fast path: a byte-identical trace already has its DuckDB cached — write it straight to MEMFS and
  // skip both the gunzip and the (slow) materialize. QA only ever queries the DB, never the JSONL.
  const hash = await sha256Hex(bytes);
  const cacheName = hash ? await duckdbCacheName(pyodide) : null;
  if (hash && cacheName) {
    const cached = await readCachedDuckdb(cacheName, hash);
    if (cached) {
      FS.writeFile(TRACE_DB_PATH, new Uint8Array(cached));
      post({ type: 'qa-trace-ready', requestId, tracePath: TRACE_PATH, dbPath: TRACE_DB_PATH, filename });
      return;
    }
  }

  let data = new Uint8Array(bytes);
  if (gzip) data = await gunzip(data);
  FS.writeFile(TRACE_PATH, data);

  pyodide.globals.set('__qa_mount', MOUNT);
  pyodide.globals.set('__qa_trace_path', TRACE_PATH);
  pyodide.globals.set('__qa_db_path', TRACE_DB_PATH);
  try {
    await pyodide.runPythonAsync(`
import os, sys
from pathlib import Path

utils = str(Path(__qa_mount) / "artifacts" / "utils")
if utils not in sys.path:
    sys.path.insert(0, utils)

import trace_db

db_path = Path(__qa_db_path)
if db_path.exists():
    db_path.unlink()
trace_db.materialize(__qa_trace_path, db_path)
`);
  } catch (e) {
    // Name the failing stage so the dropzone shows something actionable instead of a bare traceback
    // line. Only append the "too large" hint for genuine memory exhaustion — a schema/binder error is
    // a different problem and that hint would mislead.
    const cause = formatErr(e);
    const oom = /memory|out of memory|allocate|alloc/i.test(cause);
    const hint = oom
      ? ' The trace may be too large for in-browser analysis; try a smaller export, or run it locally (git clone + ./launch.sh).'
      : '';
    throw new Error(`Couldn't build the trace database — ${cause}.${hint}`);
  } finally {
    try {
      pyodide.globals.delete('__qa_mount');
      pyodide.globals.delete('__qa_trace_path');
      pyodide.globals.delete('__qa_db_path');
    } catch {
      /* ignore */
    }
  }


  // materialize() closes the connection (checkpoints into a single self-contained file), so the DB on
  // disk is complete now — stash it for next time before reporting ready.
  if (hash && cacheName) {
    try {
      await writeCachedDuckdb(cacheName, hash, FS.readFile(TRACE_DB_PATH) as Uint8Array);
    } catch {
      /* ignore — caching is best-effort */
    }
  }

  post({ type: 'qa-trace-ready', requestId, tracePath: TRACE_PATH, dbPath: TRACE_DB_PATH, filename });
}

async function runQaPython(requestId: string, code: string, packages?: string[]): Promise<void> {
  const pyodide = await boot(packages); // dashboard: duckdb-only; assistant: full plotting stack
  pyodide.globals.set('__qa_code', code);
  pyodide.globals.set('__qa_out', QA_OUT);
  const raw = await pyodide.runPythonAsync(`
import base64
import contextlib
import io
import json
import mimetypes
import os
from pathlib import Path
import shutil
import traceback

out_dir = Path(__qa_out)
try:
    shutil.rmtree(out_dir)
except FileNotFoundError:
    pass
out_dir.mkdir(parents=True, exist_ok=True)

stdout = io.StringIO()
stderr = io.StringIO()
error = None
namespace = {"__name__": "__main__"}

try:
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exec(__qa_code, namespace, namespace)
except Exception as exc:
    error = {
        "name": type(exc).__name__,
        "value": str(exc),
        "traceback": traceback.format_exc(),
    }

def data_url(mime, raw):
    return "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")

artifacts = []
for path in sorted(p for p in out_dir.rglob("*") if p.is_file()):
    rel = "/" + str(path.relative_to(out_dir)).replace("\\\\", "/")
    full = str(path)
    size = path.stat().st_size
    mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
    item = {
        "path": str(path),
        "size": size,
        "type": "file",
        "mime": mime,
        "is_image": mime.startswith("image/"),
        "display": False,
    }
    try:
        if mime.startswith("image/") and size <= 2_000_000:
            item["data_url"] = data_url(mime, path.read_bytes())
            item["display"] = True
            item["source"] = "artifact"
        elif (mime.startswith("text/") or mime in {"application/json", "text/csv"}) and size <= 2_000_000:
            item["text_preview"] = path.read_text(errors="replace")[:10000]
    except Exception as exc:
        item["inline_error"] = type(exc).__name__ + ": " + str(exc)
    artifacts.append(item)

display_images = [
    {
        "path": item.get("path"),
        "mime": item.get("mime"),
        "size": item.get("size"),
        "data_url": item.get("data_url"),
        "source": item.get("source", "artifact"),
        "display": True,
    }
    for item in artifacts
    if item.get("display") and item.get("data_url")
]

json.dumps({
    "stdout": stdout.getvalue().splitlines(True),
    "stderr": stderr.getvalue().splitlines(True),
    "error": error,
    "results": [],
    "artifacts": artifacts,
    "display_images": display_images,
})
`);
  try {
    pyodide.globals.delete('__qa_code');
    pyodide.globals.delete('__qa_out');
  } catch {
    /* ignore */
  }
  post({ type: 'qa-tool-result', requestId, result: JSON.parse(raw) });
}

ctx.onmessage = async (e: MessageEvent<MainMsg>) => {
  const msg = e.data;
  // Frames carrying a requestId (prepare/qa-*) get it echoed on any error so the awaiting
  // main-thread promise rejects rather than hanging.
  const requestId = 'requestId' in msg ? msg.requestId : undefined;
  try {
    if (msg.type === 'boot') {
      await boot(msg.packages);
    } else if (msg.type === 'prepare') {
      await prepareTrace(msg.requestId, msg.bytes, msg.filename, msg.buildDb, msg.trusted);
    } else if (msg.type === 'prepare-get-raw') {
      await getRoundRawFromPrepare(msg.requestId, msg.traceKey);
    } else if (msg.type === 'prime-duckdb') {
      await primeDuckdb(msg.requestId, msg.sanitizedGz, msg.dbGz);
    } else if (msg.type === 'qa-load-trace') {
      await loadTraceForQa(msg.requestId, msg.bytes, msg.gzip, msg.filename, msg.packages);
    } else if (msg.type === 'qa-run-python') {
      await runQaPython(msg.requestId, msg.code, msg.packages);
    } else if (msg.type === 'analyze') {
      await analyze(msg.bytes, msg.gzip, {
        shardIndex: msg.shardIndex,
        shardCount: msg.shardCount,
        sessionTotal: msg.sessionTotal,
        includeOverview: msg.includeOverview,
      });
    }
  } catch (err) {
    post({ type: 'error', message: formatErr(err), requestId });
  }
};
