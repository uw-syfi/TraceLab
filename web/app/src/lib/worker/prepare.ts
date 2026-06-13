// Shared client for the format-aware ingest step. Both drop locations — the Analyze dropzone and the
// Pool surface's direct-contribute drop — call prepareTrace(file) so raw .claude/.codex uploads are
// normalized + sanitized in the browser before anything downstream (Analyze figures, the assistant's
// QA, Contribute). Backed by a single lazily-created analyze.worker instance, reused across both
// surfaces (a module singleton — one extra Pyodide instance total, separate from the shard pool and
// the QA worker). Raw bytes never leave the worker; only the derived .gz artifacts come back.

import type { MainMsg, WorkerMsg, PrepareMeta } from './protocol';
import type { RoundRaw } from '../analytics/types';

export interface PreparedTrace {
  meta: PrepareMeta;
  /** Canonical sanitized .gz — fed to Analyze, the assistant's QA, and Contribute (one content hash). */
  sanitizedFile: File;
  /** Present only when we extracted from raw sessions (download-only; never auto-uploaded). */
  normalizedFile?: File;
}

const PREPARE_TIMEOUT_MS = 600_000; // generous: large raw dumps + Pyodide cold boot

type StageFn = (message: string) => void;
type Pending = {
  resolve: (m: WorkerMsg) => void;
  reject: (e: Error) => void;
  timer: number;
  onStage?: StageFn;
};

let worker: Worker | null = null;
const pending = new Map<string, Pending>();
// Latest in-flight stage callback — boot-progress frames have no requestId, and this worker only
// ever runs prepare, so its boot stages belong to whichever prepare is currently active.
let activeOnStage: StageFn | null = null;

function ensureWorker(): Worker {
  if (worker) return worker;
  const w = new Worker(new URL('./analyze.worker.ts', import.meta.url), { type: 'module' });
  w.onmessage = (event: MessageEvent<WorkerMsg>) => {
    const msg = event.data;
    // Cold-boot stages ("Loading Python runtime…", "Loading numpy…", "Mounting toolkit…").
    if (msg.type === 'boot-progress') {
      activeOnStage?.(msg.message);
      return;
    }
    const id = 'requestId' in msg ? (msg as { requestId?: string }).requestId : undefined;
    if (!id) return;
    const p = pending.get(id);
    if (!p) return;
    if (msg.type === 'prepare-progress') {
      p.onStage?.(msg.message);
    } else if (msg.type === 'error') {
      settle(id, () => p.reject(new Error(msg.message || 'Preparing this trace failed without a message.')));
    } else if (msg.type === 'prepared' || msg.type === 'prepare-raw' || msg.type === 'primed-duckdb') {
      settle(id, () => p.resolve(msg));
    }
  };
  w.onerror = (event) => {
    const err = new Error(event.message || 'prepare worker error');
    for (const id of [...pending.keys()]) settle(id, () => pending.get(id)?.reject(err));
  };
  worker = w;
  return w;
}

function settle(id: string, run: () => void): void {
  const p = pending.get(id);
  if (p) {
    window.clearTimeout(p.timer);
    if (activeOnStage === p.onStage) activeOnStage = null;
  }
  pending.delete(id);
  run();
}

function reqId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `prep-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** Strip the container/extension so artifact names read e.g. `my-trace.sanitized.jsonl.gz`. */
function baseName(filename: string): string {
  return (
    filename
      .replace(/\.gz$/i, '')
      .replace(/\.(tgz|zip|tar|jsonl|ndjson|json)$/i, '')
      .replace(/\.tar$/i, '') || 'trace'
  );
}

// Boot the prepare worker ahead of the drop (e.g. on tab-show) so its Pyodide cold-start overlaps
// with the user picking a file instead of landing on the critical path afterwards. We preload only
// DuckDB (materialize); ingest itself is pure stdlib. Idempotent — the worker memoizes its boot.
let warmed = false;
export function warmPrepare(buildDb = true): void {
  if (warmed) return;
  warmed = true;
  const w = ensureWorker();
  w.postMessage({ type: 'boot', packages: buildDb ? ['duckdb'] : [] } satisfies MainMsg);
}

export async function prepareTrace(
  file: File,
  opts: { buildDb: boolean; onStage?: StageFn; trusted?: boolean },
): Promise<PreparedTrace> {
  const w = ensureWorker();
  const bytes = await file.arrayBuffer();
  const id = reqId();
  activeOnStage = opts.onStage ?? null; // catch boot-progress (no requestId) for this prepare
  const done = new Promise<WorkerMsg>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      settle(id, () => reject(new Error('Timed out preparing this trace.')));
    }, PREPARE_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer, onStage: opts.onStage });
  });

  const msg: MainMsg = {
    type: 'prepare',
    requestId: id,
    bytes,
    filename: file.name,
    buildDb: opts.buildDb,
    trusted: opts.trusted ?? false,
  };
  w.postMessage(msg, [bytes]); // transfer: we don't need the buffer on this thread anymore

  const res = await done;
  if (res.type !== 'prepared') throw new Error('Unexpected prepare response.');

  const base = baseName(file.name);
  const sanitizedFile = new File([res.sanitizedGz], `${base}.sanitized.jsonl.gz`, {
    type: 'application/gzip',
  });
  const normalizedFile = res.normalizedGz
    ? new File([res.normalizedGz], `${base}.normalized.jsonl.gz`, { type: 'application/gzip' })
    : undefined;
  return { meta: res.meta, sanitizedFile, normalizedFile };
}

/**
 * Local-executor: prime the in-browser DuckDB cache with the server-built `.duckdb` (already unzipped)
 * so the AI assistant's later `loadTrace` is a cache HIT and skips the slow WASM materialize. Keyed by
 * sha256(sanitizedGz) — the same bytes the assistant hashes. Best-effort; resolves false on any miss.
 * Runs in the (already warm) prepare worker, which has DuckDB loaded.
 */
export async function primeDuckdb(sanitizedGz: ArrayBuffer, dbGz: ArrayBuffer): Promise<boolean> {
  const w = ensureWorker();
  const id = reqId();
  const done = new Promise<WorkerMsg>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      settle(id, () => reject(new Error('Timed out priming the database cache.')));
    }, PREPARE_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
  });
  w.postMessage({ type: 'prime-duckdb', requestId: id, sanitizedGz, dbGz } satisfies MainMsg, [
    sanitizedGz,
    dbGz,
  ]);
  const res = await done;
  return res.type === 'primed-duckdb' && res.ok;
}

/**
 * Fetch one round's LOCAL-only raw text by trace_key from the most recently prepared trace. Backed
 * by the same singleton prepare worker; the raw map lives only in its MEMFS (never sanitized,
 * cached, or uploaded). Returns null when the round has no captured original (e.g. the trace wasn't
 * a raw upload, or the worker has since prepared a different trace). Keyed by the *sanitized*
 * trace_key — the same value carried on SessionRoundPoint.traceKey in the payload.
 */
export async function getRoundRaw(traceKey: string): Promise<RoundRaw | null> {
  const w = ensureWorker();
  const id = reqId();
  const done = new Promise<WorkerMsg>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      settle(id, () => reject(new Error('Timed out fetching round detail.')));
    }, PREPARE_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
  });
  w.postMessage({ type: 'prepare-get-raw', requestId: id, traceKey } satisfies MainMsg);
  const res = await done;
  if (res.type !== 'prepare-raw') throw new Error('Unexpected raw response.');
  return res.raw;
}
