// Ask-the-trace WebSocket client. Runs one assistant turn over `WS /api/chat/ws`, which the dev
// server proxies (and a prod reverse proxy upgrades) to the FastAPI sidecar (web/ai_infra/app.py).
//
// The server owns the model -> tool loop and streams `event` frames as it works. The two data
// sources differ only in where `run_python` runs:
//   - public ('syfi') -> the server runs it in an E2B sandbox over the public DuckDB; we never see
//     a `tool_request`.
//   - user            -> the server emits `tool_request` frames; we run the code in the in-browser
//     Pyodide worker over the *local* trace and reply with `tool_result`. Only the generated code
//     and aggregated results cross the socket — the raw trace never leaves the browser.
//
// One fresh socket per turn keeps lifecycle trivial and matches the server's per-connection state.

import type { PyodideToolExecutorClient } from './pyodideToolExecutor';
import type { PyodideArtifact, PyodideToolResult } from '../worker/protocol';

export type AskSource = 'syfi' | 'user';

export interface AskMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface AskEvent {
  label: string;
  value: unknown;
}

export interface AskToolEvent {
  tool_call_id?: string;
  name?: string;
  code?: string;
  result?: PyodideToolResult;
}

export interface AskResult {
  content: string;
  display_images: PyodideArtifact[];
  tool_events: AskToolEvent[];
  turns?: number;
  forced?: boolean;
}

export interface AskTurnOptions {
  source: AskSource;
  sessionId: string;
  messages: AskMessage[];
  /** Optional model override; otherwise the server picks (frontend normally omits this). */
  model?: string;
  /** Required for the user source: runs `tool_request` code locally in Pyodide. */
  executor?: PyodideToolExecutorClient;
  dbPath?: string;
  outDir?: string;
  traceContext?: string;
  /** Streamed operational events (model_turn, tool_code, tool_result, e2b, retries, final, …). */
  onEvent?: (event: AskEvent) => void;
  /** Fired when a browser tool execution starts/finishes (user source only). */
  onToolStart?: (toolCallId: string) => void;
  onToolEnd?: (toolCallId: string, result: PyodideToolResult) => void;
  signal?: AbortSignal;
}

function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/api/chat/ws`;
}

function browserToolError(err: unknown): PyodideToolResult {
  return {
    stdout: [],
    stderr: [],
    error: { name: 'BrowserToolError', value: String((err as Error)?.message ?? err), traceback: '' },
    results: [],
    artifacts: [],
    display_images: [],
  };
}

export function askTurn(opts: AskTurnOptions): Promise<AskResult> {
  return new Promise<AskResult>((resolve, reject) => {
    let ws: WebSocket;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      reject(err);
      return;
    }

    let settled = false;
    const finish = (run: () => void) => {
      if (settled) return;
      settled = true;
      opts.signal?.removeEventListener('abort', onAbort);
      try {
        ws.close();
      } catch {
        /* ignore */
      }
      run();
    };
    const onAbort = () => finish(() => reject(new DOMException('ask turn aborted', 'AbortError')));
    if (opts.signal?.aborted) {
      reject(new DOMException('ask turn aborted', 'AbortError'));
      return;
    }
    opts.signal?.addEventListener('abort', onAbort, { once: true });

    ws.onopen = () => {
      const payload: Record<string, unknown> = {
        type: 'chat',
        source: opts.source,
        session_id: opts.sessionId,
        messages: opts.messages,
      };
      if (opts.model) payload.model = opts.model;
      if (opts.source === 'user') {
        payload.db_path = opts.dbPath || '/work/trace.duckdb';
        payload.out_dir = opts.outDir || '/out';
        if (opts.traceContext) payload.trace_context = opts.traceContext;
      }
      ws.send(JSON.stringify(payload));
    };

    ws.onmessage = async (event: MessageEvent) => {
      let frame: any;
      try {
        frame = JSON.parse(event.data as string);
      } catch {
        return;
      }

      switch (frame.type) {
        case 'event':
          opts.onEvent?.({ label: frame.label, value: frame.value });
          return;

        case 'tool_request': {
          // The server blocks its loop on this request, so no other frame arrives meanwhile.
          const id: string = frame.tool_call_id || '';
          opts.onToolStart?.(id);
          let result: PyodideToolResult;
          try {
            if (!opts.executor) throw new Error('no in-browser executor available for this turn');
            const toolEvent = await opts.executor.runPython({
              tool_call_id: id,
              name: 'run_python',
              code: frame.code || '',
            });
            result = toolEvent.result;
          } catch (err) {
            result = browserToolError(err);
          }
          opts.onToolEnd?.(id, result);
          if (settled) return;
          try {
            ws.send(JSON.stringify({ type: 'tool_result', tool_call_id: id, result }));
          } catch (err) {
            finish(() => reject(new Error(`failed to return tool result: ${String((err as Error)?.message ?? err)}`)));
          }
          return;
        }

        case 'done': {
          const r = frame.result || {};
          finish(() =>
            resolve({
              content: r.content || r.assistant_message?.content || '',
              display_images: (r.display_images as PyodideArtifact[]) || [],
              tool_events: (r.tool_events as AskToolEvent[]) || [],
              turns: r.turns,
              forced: r.forced,
            }),
          );
          return;
        }

        case 'error':
          finish(() => reject(new Error(frame.error || 'the assistant turn failed')));
          return;
      }
    };

    ws.onerror = () => finish(() => reject(new Error('WebSocket connection error')));
    ws.onclose = () => finish(() => reject(new Error('connection closed before the turn finished')));
  });
}

export function newSessionId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
