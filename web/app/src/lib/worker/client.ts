// Main-thread wrapper around a POOL of Pyodide analyze workers. The work is sharded across
// workers (worker i renders REGULAR[i::N] + its slice of the session figures; only worker 0
// computes the summary). This module owns the pool lifecycle, gzip detection, fan-out, and
// stream aggregation. The island subscribes with a set of handlers.

import type { MainMsg, WorkerMsg, SummaryPayload } from './protocol';

export interface AnalyzeHandlers {
  onBootProgress?(message: string): void;
  onBooted?(): void; // fired once, when every worker has booted
  onProgress?(p: { done: number; total: number }): void;
  onSummary?(data: SummaryPayload): void;
  onFigure?(f: { experiment: string; name: string; png: Blob }): void;
  onDone?(): void; // fired once, when every worker has finished
  onError?(e: { message: string; experiment?: string }): void;
}

const GZIP_MAGIC = [0x1f, 0x8b];
const SESSION_TOTAL = 4; // top-N session figures, rendered across the pool
const REGULAR_FIGURES = 8; // non-session figure experiments

/** A small pool: one worker per spare core, capped (each worker is a full Pyodide instance).
 *  Measured (12-figure trace): render scales strongly with pool size (~18s @1 → ~7s @4 workers),
 *  while per-worker boot is parallel and off the first-figure critical path — so the cap stays at 4. */
function defaultPoolSize(): number {
  const hc = (typeof navigator !== 'undefined' && navigator.hardwareConcurrency) || 4;
  return Math.max(1, Math.min(hc - 1, 4));
}

export class AnalyzeClient {
  private workers: Worker[];
  private size: number;
  private handlers: AnalyzeHandlers = {};
  private bootedCount = 0;
  private bootAnnounced = false;
  private warmStarted = false;
  private figuresDone = 0;
  private finished = new Set<number>();

  readonly totalFigures = REGULAR_FIGURES + SESSION_TOTAL;

  constructor(size = defaultPoolSize()) {
    this.size = size;
    this.workers = Array.from({ length: size }, (_, i) => {
      const w = new Worker(new URL('./analyze.worker.ts', import.meta.url), { type: 'module' });
      w.onmessage = (e: MessageEvent<WorkerMsg>) => this.route(e.data, i);
      w.onerror = (e) => this.handlers.onError?.({ message: e.message || 'worker error' });
      return w;
    });
  }

  on(handlers: AnalyzeHandlers): this {
    this.handlers = handlers;
    return this;
  }

  /** Warm Pyodide early (e.g. when the Analyze tab first becomes visible). Worker 0 boots
   *  first so the rest hit the browser HTTP cache for the (large) Pyodide packages. */
  boot(): void {
    this.warm();
  }

  private warm(): void {
    if (this.warmStarted) return;
    this.warmStarted = true;
    this.workers[0]?.postMessage({ type: 'boot' });
  }

  /** Read the file once, detect gzip, then fan a shard out to each worker. */
  async analyze(file: File): Promise<void> {
    const bytes = await file.arrayBuffer();
    const head = new Uint8Array(bytes, 0, Math.min(2, bytes.byteLength));
    const gzip = head[0] === GZIP_MAGIC[0] && head[1] === GZIP_MAGIC[1];

    this.figuresDone = 0;
    this.finished.clear();
    this.warm(); // ensure the cache-warming boot has started

    this.workers.forEach((w, i) => {
      // Each worker needs its own copy — a transfer would detach the buffer for the others.
      w.postMessage({
        type: 'analyze',
        bytes: bytes.slice(0),
        gzip,
        filename: file.name,
        shardIndex: i,
        shardCount: this.size,
        sessionTotal: SESSION_TOTAL,
        includeOverview: i === 0,
      } satisfies MainMsg);
    });
  }

  terminate(): void {
    this.workers.forEach((w) => w.terminate());
  }

  private broadcast(msg: MainMsg): void {
    this.workers.forEach((w) => w.postMessage(msg));
  }

  private markFinished(i: number): void {
    if (this.finished.has(i)) return;
    this.finished.add(i);
    if (this.finished.size === this.size) this.handlers.onDone?.();
  }

  private route(msg: WorkerMsg, i: number): void {
    switch (msg.type) {
      case 'boot-progress':
        this.handlers.onBootProgress?.(msg.message);
        break;
      case 'booted':
        this.bootedCount += 1;
        // Worker 0 warmed the cache; boot the rest now (they'll load packages from cache).
        if (i === 0) {
          for (let k = 1; k < this.size; k += 1) this.workers[k].postMessage({ type: 'boot' });
        }
        if (this.bootedCount === this.size && !this.bootAnnounced) {
          this.bootAnnounced = true;
          this.handlers.onBooted?.();
        }
        break;
      case 'progress':
        this.handlers.onProgress?.({ done: this.figuresDone, total: this.totalFigures });
        break;
      case 'summary':
        this.handlers.onSummary?.(msg.data);
        break;
      case 'figure':
        this.figuresDone += 1;
        this.handlers.onFigure?.({
          experiment: msg.experiment,
          name: msg.name,
          png: new Blob([msg.png], { type: 'image/png' }),
        });
        this.handlers.onProgress?.({ done: this.figuresDone, total: this.totalFigures });
        break;
      case 'done':
        this.markFinished(i);
        break;
      case 'error':
        this.handlers.onError?.({ message: msg.message, experiment: msg.experiment });
        // A fatal worker error has no experiment attached; count it as finished so the run
        // can still complete. Per-experiment errors (with .experiment) are non-fatal.
        if (!msg.experiment) this.markFinished(i);
        break;
    }
  }
}
