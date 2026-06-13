// Real analytics data source: turns an uploaded trace into the same AnalyticsPayload shape the mock
// emits, with ALL computation in Python. The frontend just calls load() → bulk() → render.
//
// Wiring (no new worker protocol — reuses the assistant's QA RPC, see the plan's Option A):
//   load(file)  → prepareTrace(file)  normalizes/sanitizes raw .claude/.codex in the prepare worker
//                 AND materializes+caches the DuckDB (keyed by the sanitized .gz content hash);
//               → client.loadTrace(sanitizedFile)  hashes that same file → Cache-API HIT → the
//                 executor worker writes the cached DB to MEMFS and returns its path (no re-materialize).
//   bulk()      → client.runPython(<snippet importing analyze + print(bulk_json(...))>) → parse stdout.
//   sessionDetail(id) → same RPC, analyze.session_detail_json(...).
//
// stdout comes back as an ARRAY of lines (worker splits on newlines), and exec runs in an EMPTY
// namespace, so each snippet sets sys.path itself and we join+JSON.parse the printed line.

import { PyodideToolExecutorClient } from '../ai/pyodideToolExecutor';
import { prepareTrace, getRoundRaw, type PreparedTrace } from '../worker/prepare';
import type { AnalyticsPayload, RoundRaw, SessionDetail } from './types';

/** Fetch one round's raw original text by sanitized trace_key (e.g. from the local-executor sidecar). */
export type RawFetch = (traceKey: string) => Promise<RoundRaw | null>;

/** The minimal surface the dashboard needs from a "source" after it has the payload: lazy session
 *  drill-down + per-round raw, plus dispose. Both AnalyticsSource (in-browser compute) and
 *  RemoteAnalyticsSource (server-computed, local-executor) satisfy it. */
export interface DashboardSource {
  sessionDetail(sessionId: string): Promise<SessionDetail>;
  roundRaw(traceKey: string): Promise<RoundRaw | null>;
  dispose(): void;
}

/** Optional wiring for the local-executor path, where sanitize runs server-side. The browser then
 *  re-ingests an already-sanitized .gz (which carries no raw originals or titles), so we take those
 *  LOCAL-only signals from the server's prepare meta and route raw lookups back to the sidecar. */
export interface LocalTraceOpts {
  /** rawAvailable + titles from the server-side native prepare (override the empty re-ingest meta). */
  localTrace?: { rawAvailable: boolean; titles: Record<string, string> };
  /** Where roundRaw() goes instead of the prepare worker's MEMFS sidecar. */
  rawFetch?: RawFetch;
}

// In-MEMFS payload root (worker MOUNT) — web_analytics first so a bare `import analyze` resolves to
// it (NOT trace_facts/overview_summary/analyze, which _overview.py loads by path under another name).
const SYS_PATH = 'import sys; sys.path[:0] = ["/repo/artifacts/web_analytics", "/repo/artifacts/utils"]';

/** A JS string -> a Python string literal (handles quotes/backslashes/newlines). */
function pyStr(s: string): string {
  return JSON.stringify(s); // JSON string syntax is a valid Python str literal for our ids/paths
}

export class AnalyticsSource {
  // The dashboard's queries (bulk_json / session_detail) are pure DuckDB, so this compute worker boots
  // DuckDB ONLY — skipping the ~30 s numpy/matplotlib/font-cache tax the full stack would pay. (The AI
  // assistant keeps its own full-stack executor for plotting.)
  private client = new PyodideToolExecutorClient({ packages: ['duckdb'] });
  private dbPath: string | null = null;
  // local = utc + offset; getTimezoneOffset() is the inverse sign, so negate it.
  private readonly tzOffsetMin = -new Date().getTimezoneOffset();
  // LOCAL-only signals from the prepare step (never from the sanitized DB): whether this upload
  // carried per-round originals, and the conversation titles keyed by sanitized session_id. Both
  // are merged into the payload at bulk() time; raw text itself is fetched per round via roundRaw().
  private rawAvailable = false;
  private titles: Record<string, string> = {};
  // Local-executor only: when set, roundRaw() fetches from here (the sidecar) rather than the prepare
  // worker's MEMFS — the sanitized .gz the browser ingested carries no raw originals.
  private rawFetch: RawFetch | null = null;

  /** Normalize+sanitize+materialize the upload, then hand the executor worker the cached DB path.
   *  Returns the PreparedTrace so callers can reuse the sanitized file + ingest meta (the Analyze
   *  surface needs them for its ingest summary, Contribute, and the assistant's "Your trace" handoff).
   *
   *  `opts` wires the local-executor path: sanitize already ran server-side, so the re-ingested .gz is
   *  `kind==='sanitized'` (no raw, no titles) — we take rawAvailable/titles from opts.localTrace and
   *  send roundRaw() to opts.rawFetch. Omitted for drag/drop, which keeps the in-browser behavior. */
  async load(
    file: File,
    onStage?: (message: string) => void,
    trusted = false,
    opts: LocalTraceOpts = {},
  ): Promise<PreparedTrace> {
    // Boot the compute worker NOW so its (duckdb-only) cold start overlaps prepareTrace's extract +
    // materialize below, instead of landing serially after it.
    this.client.warm();
    const prepared = await prepareTrace(file, { buildDb: true, onStage, trusted });
    this.rawAvailable = opts.localTrace ? opts.localTrace.rawAvailable : prepared.meta.rawAvailable;
    this.titles = opts.localTrace ? opts.localTrace.titles ?? {} : prepared.meta.titles ?? {};
    this.rawFetch = opts.rawFetch ?? null;
    const ready = await this.client.loadTrace(prepared.sanitizedFile);
    this.dbPath = ready.dbPath;
    return prepared;
  }

  get loaded(): boolean {
    return this.dbPath !== null;
  }

  private async run<T>(code: string): Promise<T> {
    const ev = await this.client.runPython({ tool_call_id: 'wa', name: 'run_python', code });
    const r = ev.result;
    if (r.error) {
      throw new Error(`${r.error.name}: ${r.error.value}\n${r.error.traceback}`);
    }
    const text = r.stdout.join('').trim();
    if (!text) {
      const tail = r.stderr.length ? `\n${r.stderr.join('')}` : '';
      throw new Error(`analytics produced no output${tail}`);
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Error(`analytics output was not valid JSON:\n${text.slice(0, 500)}`);
    }
  }

  /** The whole payload (KPIs, cost, per-day, providers, stats, facts, sessions list, distributions).
   *  The DB carries no titles or raw-availability (those are LOCAL-only), so we graft them on here:
   *  set `rawAvailable` from the prepare step and merge titles into the session rows. */
  async bulk(): Promise<AnalyticsPayload> {
    if (!this.dbPath) throw new Error('AnalyticsSource.bulk(): no trace loaded');
    const code =
      `${SYS_PATH}\nimport analyze\n` +
      `print(analyze.bulk_json(${pyStr(this.dbPath)}, tz_offset_min=${this.tzOffsetMin}))`;
    const payload = await this.run<AnalyticsPayload>(code);
    payload.rawAvailable = this.rawAvailable;
    for (const s of payload.sessions) {
      const title = this.titles[s.sessionId];
      if (title) s.title = title;
    }
    return payload;
  }

  /** One round's LOCAL-only raw text, by trace_key (SessionRoundPoint.traceKey). Returns null when
   *  this trace carried no originals (rawAvailable false) — callers gate on that before showing the
   *  raw section. Served from the prepare worker's MEMFS sidecar; never touches the DB or QA worker. */
  async roundRaw(traceKey: string): Promise<RoundRaw | null> {
    if (!this.rawAvailable || !traceKey) return null;
    if (this.rawFetch) return this.rawFetch(traceKey); // local executor: served by the sidecar
    return getRoundRaw(traceKey);
  }

  /** On-demand per-round timeline for one session (fetched when a session is opened). */
  async sessionDetail(sessionId: string): Promise<SessionDetail> {
    if (!this.dbPath) throw new Error('AnalyticsSource.sessionDetail(): no trace loaded');
    const code =
      `${SYS_PATH}\nimport analyze\n` +
      `print(analyze.session_detail_json(${pyStr(this.dbPath)}, ${pyStr(sessionId)}))`;
    return this.run<SessionDetail>(code);
  }

  dispose(): void {
    this.client.terminate();
    this.dbPath = null;
  }
}
