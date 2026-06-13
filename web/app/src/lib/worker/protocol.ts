// Message contract between the Analyze island (main thread) and the Pyodide worker.
// The trace never leaves the worker: only derived figures (PNG bytes) and the summary
// object cross back.

import type { RoundRaw } from '../analytics/types';

/** Main thread -> worker. */
export type MainMsg =
  // Warm Pyodide early. `packages` lets a role-specific worker (e.g. the prepare worker) preload
  // only what it needs — omit for the full plotting stack used by figure/QA workers.
  | { type: 'boot'; packages?: string[] }
  | {
      // Format-aware ingest: sniff/normalize/sanitize whatever was dropped (raw .claude/.codex
      // sessions, archives, or an existing round-trace) into a sanitized .gz. When `buildDb`, also
      // materialize + cache the DuckDB so Analyze/QA reuse it. Raw bytes never leave the worker.
      type: 'prepare';
      requestId: string;
      bytes: ArrayBuffer;
      filename: string;
      buildDb: boolean;
      // Raises ingest's archive size/member guards. Set ONLY for the local self-deploy auto-load
      // (the sidecar streams the user's own on-disk trace); drag/drop stays false → strict caps.
      trusted: boolean;
    }
  | {
      // Fetch one round's LOCAL-only raw text by trace_key from the just-prepared trace. The raw map
      // lives only in the prepare worker's MEMFS (never sanitized/cached/uploaded); this reads it
      // lazily per round. Only meaningful right after a raw `prepare` (PrepareMeta.rawAvailable).
      type: 'prepare-get-raw';
      requestId: string;
      traceKey: string;
    }
  | {
      // Local-executor: prime the DuckDB cache with a server-built .duckdb so the assistant's
      // qa-load-trace is a cache HIT (skips the slow WASM materialize). Keyed by sha256(sanitizedGz),
      // exactly what qa-load-trace will hash. dbGz is the GZIPPED .duckdb the sidecar materialized; the
      // worker gunzips it (keeping that work off the main thread) before caching.
      type: 'prime-duckdb';
      requestId: string;
      sanitizedGz: ArrayBuffer;
      dbGz: ArrayBuffer;
    }
  | {
      // `packages` scopes the boot: the dashboard's compute path passes ['duckdb'] (bulk_json /
      // session_detail are pure DuckDB), so it skips the ~30 s numpy/matplotlib/font-cache tax. The
      // assistant omits it → the full plotting stack (its generated code may plot).
      type: 'qa-load-trace';
      requestId: string;
      bytes: ArrayBuffer;
      gzip: boolean;
      filename: string;
      packages?: string[];
    }
  | {
      type: 'qa-run-python';
      requestId: string;
      code: string;
      packages?: string[];
    }
  | {
      type: 'analyze';
      bytes: ArrayBuffer;
      gzip: boolean;
      filename: string;
      // sharding across the worker pool
      shardIndex: number;
      shardCount: number;
      sessionTotal: number;
      includeOverview: boolean;
    };

/** Worker -> main thread. */
export type WorkerMsg =
  | { type: 'boot-progress'; message: string }
  | { type: 'booted' }
  | { type: 'prepare-progress'; requestId: string; message: string }
  | {
      type: 'prepared';
      requestId: string;
      meta: PrepareMeta;
      sanitizedGz: ArrayBuffer;
      normalizedGz?: ArrayBuffer;
    }
  | { type: 'prepare-raw'; requestId: string; raw: RoundRaw | null }
  | { type: 'primed-duckdb'; requestId: string; ok: boolean }
  | { type: 'qa-trace-ready'; requestId: string; tracePath: string; dbPath: string; filename: string }
  | { type: 'qa-tool-result'; requestId: string; result: PyodideToolResult }
  | { type: 'progress'; experiment: string; index: number; total: number }
  | { type: 'summary'; data: SummaryPayload }
  | { type: 'figure'; experiment: string; name: string; png: ArrayBuffer }
  | { type: 'done' }
  | { type: 'error'; message: string; experiment?: string; requestId?: string };

/** Classification + counts returned by ingest.prepare(); drives the ingest result card. */
export interface PrepareMeta {
  kind: 'raw' | 'normalized' | 'sanitized';
  providers: string[];
  sessions: number;
  rounds: number;
  tools: number;
  // produced flags drive the download buttons: normalized shown only when extracted from raw;
  // sanitized shown only when we actually ran the sanitizer (raw or normalized input).
  produced: { normalized: boolean; sanitized: boolean };
  warnings: string[];
  // True only when this prepare extracted raw sessions and captured per-round originals — gates the
  // LOCAL-only raw viewer. False for normalized/sanitized inputs (no originals to show).
  rawAvailable: boolean;
  // LOCAL-only conversation titles, keyed by the SANITIZED session_id (so they line up with the
  // payload's sessions). Small, so it rides back in meta; never written into any artifact.
  titles: Record<string, string>;
}

/** The overview_summary bundle (same shape as the committed summary.json). */
export interface SummaryPayload {
  merged: unknown;
  [provider: string]: unknown;
}

export interface PyodideArtifact {
  path: string;
  size: number;
  type: string;
  mime?: string;
  is_image?: boolean;
  display?: boolean;
  data_url?: string;
  source?: string;
  text_preview?: string;
  inline_error?: string;
}

export interface PyodideToolResult {
  stdout: string[];
  stderr: string[];
  error: null | {
    name: string;
    value: string;
    traceback: string;
  };
  results: unknown[];
  artifacts: PyodideArtifact[];
  display_images: PyodideArtifact[];
}
