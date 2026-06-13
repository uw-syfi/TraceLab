// Trace handoff between Analyze and the Ask-the-trace assistant.
//
// When the user analyzes a trace, Analyze calls `setAnalyzedTrace(file)`, which stashes the File and
// dispatches `trace:ready`. The assistant listens, unlocks its "Your trace" source, and feeds the
// File to its own Pyodide executor (`pyodideToolExecutor.loadTrace`) on the first user-source
// question. The File rides in the event detail too, so the handoff works even if the two islands end
// up in separate bundles (no reliance on shared module state). This replaces the mockup's
// `MutationObserver` on `#dropzone`.

export const TRACE_READY_EVENT = 'trace:ready';
export const TRACE_CLEARED_EVENT = 'trace:cleared';
export const TRACE_CONTRIBUTED_EVENT = 'trace:contributed';
// Fired the instant a LOCAL-executor analysis starts — long before the trace file exists. The
// assistant boots its Pyodide executor (incl. the matplotlib/font-cache tax) in the background so it
// overlaps the server-side sanitize+compute, and is fully warm by the time the user opens it.
export const ASSISTANT_PREWARM_EVENT = 'assistant:prewarm';

/** Signal the assistant to start booting its executor now (see ASSISTANT_PREWARM_EVENT). */
export function prewarmAssistant(): void {
  window.dispatchEvent(new CustomEvent(ASSISTANT_PREWARM_EVENT));
}

export interface AnalyzedTrace {
  file: File;
  filename: string;
  traceId: string;
  contributed: boolean;
}

export interface TraceReadyDetail {
  file: File;
  filename: string;
  traceId: string;
  contributed: boolean;
}

export interface TraceContributedDetail {
  file: File;
  filename: string;
  traceId: string;
}

let current: AnalyzedTrace | null = null;

export function traceFileId(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

/** Called by Analyze once a trace has been analyzed. Unlocks the assistant's "Your trace" source. */
export function setAnalyzedTrace(file: File): void {
  const traceId = traceFileId(file);
  const contributed = current?.traceId === traceId ? current.contributed : false;
  current = { file, filename: file.name, traceId, contributed };
  window.dispatchEvent(
    new CustomEvent<TraceReadyDetail>(TRACE_READY_EVENT, {
      detail: { file, filename: file.name, traceId, contributed },
    }),
  );
}

/** Latest analyzed trace, or null. Lets a late-mounting listener catch up on the current trace. */
export function getAnalyzedTrace(): AnalyzedTrace | null {
  return current;
}

export function markAnalyzedTraceContributed(file: File): void {
  const traceId = traceFileId(file);
  if (!current || current.traceId !== traceId) return;
  current = { ...current, contributed: true };
  window.dispatchEvent(
    new CustomEvent<TraceContributedDetail>(TRACE_CONTRIBUTED_EVENT, {
      detail: { file: current.file, filename: current.filename, traceId },
    }),
  );
}

export function clearAnalyzedTrace(): void {
  current = null;
  window.dispatchEvent(new CustomEvent(TRACE_CLEARED_EVENT));
}
