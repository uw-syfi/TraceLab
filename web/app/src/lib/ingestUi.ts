// Shared renderer for the ingest result card: what was detected + counts + the conditional download
// buttons. Used by both the Analyze dropzone and the Pool direct-contribute drop so the two surfaces
// stay consistent and the download logic lives in one place.
//
// Download buttons follow `meta.produced`:
//   raw        -> normalized + sanitized   (we built both)
//   normalized -> sanitized only           (user already has the normalized rows)
//   sanitized  -> none                      (nothing to convert)

import { intComma } from './format';
import type { PreparedTrace } from './worker/prepare';

const KIND_COPY: Record<PreparedTrace['meta']['kind'], { title: string; sub: string }> = {
  raw: {
    title: 'Raw sessions detected',
    sub: 'Normalized and sanitized right here — your raw trace never left this page.',
  },
  normalized: {
    title: 'Normalized trace',
    sub: 'Sanitized locally — pseudonymized and stripped of raw content and local paths.',
  },
  sanitized: {
    title: 'Already sanitized',
    sub: 'Ready to use as-is — nothing to convert.',
  },
};

const CHECK_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"></path></svg>';
const DL_SVG =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M7 11l5 5 5-5"></path><path d="M5 20h14"></path></svg>';

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!,
  );
}

function downloadLink(file: File, label: string, variant: 'normalized' | 'sanitized'): HTMLAnchorElement {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(file);
  a.download = file.name;
  a.dataset.igUrl = '1'; // marker so a re-render can revoke the URL
  a.className = `ig-dl ig-dl--${variant}`;
  a.innerHTML = `${DL_SVG}<span>${escapeHtml(label)}</span>`;
  return a;
}

/** Render the ingest summary + download buttons into `host`. Revokes any URLs from a prior render. */
export function renderIngestSummary(host: HTMLElement, prepared: PreparedTrace): void {
  host.querySelectorAll<HTMLAnchorElement>('a[data-ig-url]').forEach((a) => {
    try {
      URL.revokeObjectURL(a.href);
    } catch {
      /* ignore */
    }
  });

  const { meta, sanitizedFile, normalizedFile } = prepared;
  const copy = KIND_COPY[meta.kind];
  const chips = meta.providers
    .map((p) => `<span class="ig-prov${p === 'claude' ? ' sage' : ''}">${escapeHtml(p)}</span>`)
    .join('');

  host.innerHTML = `
    <div class="ingest-card" data-kind="${meta.kind}">
      <div class="ig-head">
        <span class="ig-ico" aria-hidden="true">${CHECK_SVG}</span>
        <div class="ig-titles">
          <div class="ig-title">${copy.title}</div>
          <div class="ig-sub">${copy.sub}</div>
        </div>
        <div class="ig-chips">${chips}</div>
      </div>
      <div class="ig-stats">
        <div class="ig-stat"><span class="ig-num">${intComma(meta.sessions)}</span><span class="ig-lbl">sessions</span></div>
        <div class="ig-stat"><span class="ig-num">${intComma(meta.rounds)}</span><span class="ig-lbl">agent steps</span></div>
        <div class="ig-stat"><span class="ig-num">${intComma(meta.tools)}</span><span class="ig-lbl">tool calls</span></div>
      </div>
      <div class="ig-downloads"></div>
      ${
        meta.warnings.length
          ? `<div class="ig-warn">${meta.warnings.slice(0, 5).map(escapeHtml).join('<br>')}</div>`
          : ''
      }
    </div>`;

  const dlHost = host.querySelector('.ig-downloads') as HTMLElement;
  if (meta.produced.normalized && normalizedFile) {
    dlHost.appendChild(downloadLink(normalizedFile, 'Download normalized .gz', 'normalized'));
  }
  if (meta.produced.sanitized) {
    dlHost.appendChild(downloadLink(sanitizedFile, 'Download sanitized .gz', 'sanitized'));
  }
  if (!meta.produced.normalized && !meta.produced.sanitized) {
    const note = document.createElement('span');
    note.className = 'ig-note';
    note.textContent = 'No conversion needed — analyzed and contributed as-is.';
    dlHost.appendChild(note);
  }
}
