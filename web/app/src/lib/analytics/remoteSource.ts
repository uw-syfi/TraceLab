// Server-computed analytics source for the LOCAL-executor path. Where AnalyticsSource builds the
// DuckDB and runs analyze.* in the browser (tens of seconds of WASM work), this one fetches the
// already-computed JSON from the sidecar (web/local_sidecar): the whole dashboard payload as ~60 KB,
// per-session drill-down, and per-round raw — so the browser does ZERO Pyodide compute for the
// dashboard. It satisfies the same DashboardSource interface, so dashboard.ts is unchanged.
//
// The sidecar materialized the DuckDB natively too; that file transfers in the BACKGROUND (see
// Analyze.astro) to prime the in-browser cache for the AI assistant — independent of this source.

import type { DashboardSource } from './source';
import type { AnalyticsPayload, RoundRaw, SessionDetail } from './types';

export interface RemoteAnalyticsOpts {
  /** rawAvailable + titles from the server's prepare meta. The sanitized DB the server computed over
   *  carries neither (titles are local-only; raw originals live outside the DB), so we graft them on
   *  exactly as AnalyticsSource.bulk() does for the in-browser path. */
  rawAvailable: boolean;
  titles: Record<string, string>;
  /** Browser-local UTC offset (minutes), passed to the server's bulk_json for local-time bucketing. */
  tzOffsetMin: number;
  /** Origin for the sidecar endpoints; defaults to same-origin. */
  baseUrl?: string;
}

export class RemoteAnalyticsSource implements DashboardSource {
  private readonly rawAvailable: boolean;
  private readonly titles: Record<string, string>;
  private readonly tzOffsetMin: number;
  private readonly base: string;

  constructor(opts: RemoteAnalyticsOpts) {
    this.rawAvailable = opts.rawAvailable;
    this.titles = opts.titles ?? {};
    this.tzOffsetMin = opts.tzOffsetMin;
    this.base = (opts.baseUrl ?? '').replace(/\/$/, '');
  }

  /** The whole payload, computed server-side. Graft the LOCAL-only signals (rawAvailable + titles) on
   *  top, since the server computed over the sanitized DB which carries neither. */
  async bulk(): Promise<AnalyticsPayload> {
    const res = await fetch(`${this.base}/api/local-trace/analytics?tz=${this.tzOffsetMin}`);
    if (!res.ok) throw new Error(`analytics fetch failed (${res.status})`);
    const payload = (await res.json()) as AnalyticsPayload;
    payload.rawAvailable = this.rawAvailable;
    for (const s of payload.sessions) {
      const title = this.titles[s.sessionId];
      if (title) s.title = title;
    }
    return payload;
  }

  /** One session's per-round timeline (fetched when a session is opened). */
  async sessionDetail(sessionId: string): Promise<SessionDetail> {
    const res = await fetch(
      `${this.base}/api/local-trace/session-detail?id=${encodeURIComponent(sessionId)}`,
    );
    if (!res.ok) throw new Error(`session-detail fetch failed (${res.status})`);
    return (await res.json()) as SessionDetail;
  }

  /** One round's LOCAL-only original text by trace_key — served by the sidecar's round_raw map. */
  async roundRaw(traceKey: string): Promise<RoundRaw | null> {
    if (!this.rawAvailable || !traceKey) return null;
    const res = await fetch(
      `${this.base}/api/local-trace/round-raw?key=${encodeURIComponent(traceKey)}`,
    );
    return res.ok ? ((await res.json()) as RoundRaw) : null;
  }

  dispose(): void {
    /* nothing to free — no worker, no Pyodide. */
  }
}
