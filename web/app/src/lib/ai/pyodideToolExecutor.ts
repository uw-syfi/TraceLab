import type { MainMsg, PyodideToolResult, WorkerMsg } from '../worker/protocol';

const GZIP_MAGIC = [0x1f, 0x8b];

function requestId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `qa-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isGzip(bytes: ArrayBuffer): boolean {
  const head = new Uint8Array(bytes, 0, Math.min(2, bytes.byteLength));
  return head[0] === GZIP_MAGIC[0] && head[1] === GZIP_MAGIC[1];
}

export interface TraceReady {
  tracePath: string;
  dbPath: string;
  filename: string;
  traceContext: string;
}

export interface BrowserToolEvent {
  tool_call_id: string;
  name: string;
  code: string;
  result: PyodideToolResult;
}

export class PyodideToolExecutorClient {
  private worker: Worker;
  private pending = new Map<string, (msg: WorkerMsg) => void>();
  // Packages to boot the worker with. The dashboard's compute path passes ['duckdb'] (its queries are
  // pure DuckDB) to skip the ~30 s numpy/matplotlib/font-cache boot; the assistant leaves it undefined
  // so its worker loads the full plotting stack (its model-generated code may render figures).
  private readonly packages?: string[];

  constructor(opts: { packages?: string[] } = {}) {
    this.packages = opts.packages;
    this.worker = new Worker(new URL('../worker/analyze.worker.ts', import.meta.url), {
      type: 'module',
    });
    this.worker.onmessage = (event: MessageEvent<WorkerMsg>) => {
      const msg = event.data;
      const id = 'requestId' in msg ? msg.requestId : '';
      if (!id) return;
      this.pending.get(id)?.(msg);
    };
    // A hard worker crash (e.g. DuckDB exhausting WASM memory while materializing a large trace) aborts
    // the worker WITHOUT an 'error' message frame — onmessage never fires, so every awaiting promise
    // would otherwise hang until the 180s timeout. Surface it immediately as an actionable error.
    this.worker.onerror = (event) => {
      const message =
        (event && (event as ErrorEvent).message) ||
        'The analysis engine crashed — the trace may be too large for in-browser analysis. Try a smaller export, or run it locally (git clone + ./launch.sh).';
      for (const [, settle] of this.pending) settle({ type: 'error', message } as WorkerMsg);
      this.pending.clear();
    };
  }

  terminate(): void {
    this.worker.terminate();
    this.pending.clear();
  }

  /** Boot Pyodide + the full plotting stack (numpy/matplotlib/Pillow/duckdb) + warm the figure
   *  renderer NOW, without a trace — so a later loadTrace only pays the (cache-hit) DB write. The
   *  worker memoizes its boot, so this overlaps the assistant's cold-start with other work (e.g. the
   *  local-executor's server-side compute). Fire-and-forget; idempotent. */
  warm(): void {
    this.worker.postMessage({ type: 'boot', packages: this.packages } satisfies MainMsg);
  }

  async loadTrace(file: File): Promise<TraceReady> {
    const bytes = await file.arrayBuffer();
    const id = requestId();
    const msg: MainMsg = {
      type: 'qa-load-trace',
      requestId: id,
      bytes,
      gzip: isGzip(bytes),
      filename: file.name,
      packages: this.packages,
    };
    this.worker.postMessage(msg, [bytes]);
    const response = await this.waitFor(id, 'qa-trace-ready');
    return {
      tracePath: response.tracePath,
      dbPath: response.dbPath,
      filename: response.filename,
      traceContext:
        "You are analyzing the user's uploaded coding trace. Treat all counts and plots as specific to that uploaded trace, not the public SYFI dataset.",
    };
  }

  async runPython(toolCall: { tool_call_id: string; name: string; code: string }): Promise<BrowserToolEvent> {
    const id = requestId();
    const msg: MainMsg = {
      type: 'qa-run-python',
      requestId: id,
      code: toolCall.code,
      packages: this.packages,
    };
    this.worker.postMessage(msg);
    const response = await this.waitFor(id, 'qa-tool-result');
    return {
      tool_call_id: toolCall.tool_call_id,
      name: toolCall.name || 'run_python',
      code: toolCall.code,
      result: response.result,
    };
  }

  private waitFor<T extends WorkerMsg['type']>(
    id: string,
    type: T,
  ): Promise<Extract<WorkerMsg, { type: T }>> {
    return new Promise((resolve, reject) => {
      const timeout = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Pyodide worker timeout waiting for ${type}`));
      }, 180_000);

      this.pending.set(id, (msg) => {
        if (msg.type === 'error') {
          window.clearTimeout(timeout);
          this.pending.delete(id);
          reject(new Error(msg.message || 'The analysis engine failed without a message.'));
          return;
        }
        if (msg.type !== type) return;
        window.clearTimeout(timeout);
        this.pending.delete(id);
        resolve(msg as Extract<WorkerMsg, { type: T }>);
      });
    });
  }
}
