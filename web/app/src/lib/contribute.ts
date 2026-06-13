// Client for the contribute sidecar. Upload is non-blocking: POST returns a job_id immediately,
// then we poll for the background validation/dedup outcome. The file is the same already-loaded,
// already-sanitized .gz the user analyzed locally — sent verbatim; the server rejects anything
// that still carries sensitive data.

export interface ContributeResult {
  accepted: number;
  duplicate: boolean;
  new_sessions: number;
  skipped_sessions: number;
  rows_added: number;
}

export type ContributePhase = 'uploading' | 'validating';

export interface UploadProgress {
  loaded: number; // bytes the server has actually acknowledged receiving
  total: number;
  percent: number; // 0..100
  bytesPerSec: number; // real throughput, measured against server acks
  etaSeconds: number | null;
}

// Upload tuning. We stream the .gz as fixed-size chunks over several concurrent fetch() requests.
// Each chunk's fetch resolves only when the server has *read and stored* that chunk (it responds
// after awaiting the request body), so summed chunk completions = bytes genuinely received by the
// server. That's the one thing xhr.upload.onprogress can't give us: it reports bytes flushed to the
// local OS socket buffer, which for a body that fits the buffer hits 100% at memory speed while the
// real transfer is still in flight (the bogus "93 MB/s, then stuck" the user saw).
const CHUNK_BYTES = 256 * 1024; // finer steps → smoother bar (vs. coarse 2 MB jumps)
const CONCURRENCY = 4;
const CHUNK_RETRIES = 3;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function createUploadId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();

  if (globalThis.crypto?.getRandomValues) {
    const bytes = new Uint8Array(16);
    globalThis.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  return `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export async function contribute(
  file: File,
  opts: {
    consent: boolean;
    onPhase?: (phase: ContributePhase) => void;
    onProgress?: (p: UploadProgress) => void;
  },
): Promise<ContributeResult> {
  opts.onPhase?.('uploading');
  const { job_id } = await upload(file, opts.consent, opts.onProgress);
  opts.onPhase?.('validating');
  return pollStatus(job_id);
}

async function upload(
  file: File,
  consent: boolean,
  onProgress?: (p: UploadProgress) => void,
): Promise<{ job_id: string }> {
  const total = file.size;
  const uploadId = createUploadId();
  const numChunks = Math.max(1, Math.ceil(total / CHUNK_BYTES));

  // Throughput from a sliding ~2s window over confirmed bytes — real sustained rate, not a
  // cumulative average skewed by the first burst. We hold the last good rate once it's established
  // (rather than dropping to 0 whenever a window is momentarily too short), so the speed/ETA stop
  // flickering on and off between chunk waves.
  const samples: Array<{ t: number; loaded: number }> = [{ t: performance.now(), loaded: 0 }];
  let confirmed = 0;
  let rate = 0; // bytes/sec, last established value
  const report = () => {
    if (!onProgress) return;
    const now = performance.now();
    samples.push({ t: now, loaded: confirmed });
    while (samples.length > 2 && now - samples[0].t > 2000) samples.shift();
    const span = now - samples[0].t;
    if (span >= 500 && confirmed > samples[0].loaded) {
      rate = ((confirmed - samples[0].loaded) / span) * 1000;
    }
    onProgress({
      loaded: confirmed,
      total,
      percent: total > 0 ? (confirmed / total) * 100 : 100,
      bytesPerSec: rate,
      etaSeconds: rate > 0 ? (total - confirmed) / rate : null,
    });
  };

  async function sendChunk(index: number): Promise<void> {
    const begin = index * CHUNK_BYTES;
    const end = Math.min(begin + CHUNK_BYTES, total);
    const blob = file.slice(begin, end);
    for (let attempt = 1; ; attempt++) {
      try {
        const res = await fetch('/api/contribute/chunk', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/octet-stream',
            'X-Upload-Id': uploadId,
            'X-Chunk-Offset': String(begin),
            'X-Total-Bytes': String(total),
          },
          body: blob,
        });
        if (!res.ok) throw new Error(await httpError(res));
        confirmed += end - begin; // the server has this chunk now — real progress
        report();
        return;
      } catch (err) {
        if (attempt >= CHUNK_RETRIES) throw err instanceof Error ? err : new Error('Upload failed.');
        await sleep(300 * attempt); // transient blip — back off and retry the same chunk
      }
    }
  }

  // Concurrent worker pool over the chunk indices, capped at CONCURRENCY in flight.
  report();
  let next = 0;
  const worker = async () => {
    for (let i = next++; i < numChunks; i = next++) await sendChunk(i);
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, numChunks) }, worker));

  // Every byte is acknowledged by the server; finalize (assemble + register the job).
  const res = await fetch('/api/contribute/finish', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ upload_id: uploadId, consent }),
  });
  if (!res.ok) throw new Error(await httpError(res));
  return res.json();
}

async function httpError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (body?.detail !== undefined) return formatDetail(body.detail);
  } catch {
    /* fall through */
  }
  if (res.status === 413) return 'That trace is too large to contribute.';
  if (res.status === 429) return 'Too many contributions just now — please try again later.';
  return `Contribution failed (${res.status}).`;
}

async function pollStatus(jobId: string): Promise<ContributeResult> {
  const deadline = Date.now() + 5 * 60_000;
  let delay = 600;
  while (Date.now() < deadline) {
    await sleep(delay);
    delay = Math.min(Math.round(delay * 1.3), 2500); // gentle backoff
    let job: { status: string; result?: ContributeResult; error?: unknown };
    try {
      const res = await fetch(`/api/contribute/status/${jobId}`);
      if (!res.ok) throw new Error(`status ${res.status}`);
      job = await res.json();
    } catch {
      continue; // transient network blip — keep polling until the deadline
    }
    if (job.status === 'done' && job.result) return job.result;
    if (job.status === 'rejected') throw new Error(formatDetail(job.error));
    if (job.status === 'error') {
      throw new Error(typeof job.error === 'string' ? job.error : 'Processing failed.');
    }
    // 'processing' → keep polling
  }
  throw new Error('Timed out waiting for validation.');
}

function formatDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail;
  const d = detail as { error?: string; paths?: string[] } | undefined;
  if (d?.error) return d.paths?.length ? `${d.error} (e.g. ${d.paths.join(', ')})` : d.error;
  return 'Contribution was rejected.';
}
