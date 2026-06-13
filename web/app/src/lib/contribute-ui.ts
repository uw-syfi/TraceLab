// Shared driver for the contribute flow: consent modal → upload (with progress) → background
// validation → accepted / rejected dialog. Both the Analyze surface and the Contributed-pool
// entry point call `requestContribution(file)`; the dialog markup lives in ContributeDialog.astro
// and is wired once via `initContributeDialog()`.

import { contribute, type UploadProgress } from './contribute';
import { intComma, fmtBytes, fmtSeconds } from './format';
import { markAnalyzedTraceContributed } from './ai/traceStore';

const ICONS = {
  spin: '<span class="spinner"></span>',
  ok: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"></path></svg>',
  err: '<svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6L6 18M6 6l12 12"></path></svg>',
};
type ResultState = 'processing' | 'ok' | 'error';

let open: ((file: File) => void) | null = null;

/** Wire the dialog elements once. Safe to call repeatedly / on pages without the dialog. */
export function initContributeDialog(): void {
  if (open) return;
  const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T | null;
  const consent = $('cc-overlay');
  const result = $('cr-overlay');
  if (!consent || !result) return; // dialog not present on this page

  const ccConfirm = $<HTMLButtonElement>('cc-confirm')!;
  const ccCancel = $<HTMLButtonElement>('cc-cancel')!;
  const crIco = $('cr-ico')!;
  const crTitle = $('cr-title')!;
  const crBody = $('cr-body')!;
  const crProgress = $('cr-progress')!;
  const crFill = $('cr-fill')!;
  const crPct = $('cr-pct')!;
  const crSpeed = $('cr-speed')!;
  const crOk = $<HTMLButtonElement>('cr-ok')!;

  let pendingFile: File | null = null;
  let lastFocus: Element | null = null;

  const openConsent = () => {
    lastFocus = document.activeElement;
    consent.classList.add('open');
    consent.setAttribute('aria-hidden', 'false');
    ccConfirm.focus();
  };
  const closeConsent = () => {
    consent.classList.remove('open');
    consent.setAttribute('aria-hidden', 'true');
    (lastFocus as HTMLElement | null)?.focus?.();
  };
  const openResult = () => {
    result.classList.add('open');
    result.setAttribute('aria-hidden', 'false');
  };
  const closeResult = () => {
    result.classList.remove('open');
    result.setAttribute('aria-hidden', 'true');
    (lastFocus as HTMLElement | null)?.focus?.();
  };

  const showProgress = (on: boolean) => { crProgress.hidden = !on; };
  function updateProgress(p: UploadProgress) {
    if (p.percent >= 0) {
      crFill.style.width = `${p.percent.toFixed(0)}%`;
      crPct.textContent = `${p.percent.toFixed(0)}% · ${fmtBytes(p.loaded)} / ${fmtBytes(p.total)}`;
    } else {
      crFill.style.width = '40%';
      crPct.textContent = fmtBytes(p.loaded);
    }
    const parts: string[] = [];
    if (p.bytesPerSec > 0) parts.push(`${fmtBytes(p.bytesPerSec)}/s`);
    if (p.etaSeconds != null && p.etaSeconds > 0.5) parts.push(`~${fmtSeconds(p.etaSeconds)} left`);
    crSpeed.textContent = parts.join(' · ');
  }
  function setResult(state: ResultState, title: string, body: string) {
    crIco.className = 'm-ico' + (state === 'ok' ? ' ok' : state === 'error' ? ' err' : '');
    crIco.innerHTML = state === 'ok' ? ICONS.ok : state === 'error' ? ICONS.err : ICONS.spin;
    crTitle.textContent = title;
    crBody.textContent = body;
    const terminal = state !== 'processing';
    crOk.hidden = !terminal;
    if (terminal) crOk.focus();
  }

  async function run(file: File) {
    setResult('processing', 'Uploading your trace…', 'Transferring your sanitized .gz to the server.');
    crFill.style.width = '0%';
    crPct.textContent = '0%';
    crSpeed.textContent = '';
    showProgress(true);
    openResult();
    try {
      const r = await contribute(file, {
        consent: true,
        onProgress: updateProgress,
        onPhase: (phase) => {
          if (phase === 'validating') {
            // The bar reached a real 100% (every byte acknowledged by the server); now the
            // background validation/dedup runs.
            showProgress(false);
            setResult('processing', 'Validating your trace…',
              'Checking and deduplicating in the background — this can take a few seconds.');
          }
        },
      });
      showProgress(false);
      if (r.duplicate || (r.accepted === 0 && r.skipped_sessions > 0)) {
        setResult('ok', 'Already in the pool',
          'Thanks — this trace was already contributed, so there was nothing new to add.');
      } else if (r.accepted === 0) {
        setResult('ok', 'Nothing new to add', 'No new sessions were found in this trace.');
      } else {
        const sess = r.new_sessions === 1 ? '1 session' : `${r.new_sessions} sessions`;
        setResult('ok', 'Trace accepted — thank you!',
          `Contributed ${sess} (${intComma(r.rows_added)} agent steps) to the community pool.`);
      }
      markAnalyzedTraceContributed(file);
      window.dispatchEvent(new CustomEvent('contrib:success', { detail: { file } }));
    } catch (err) {
      showProgress(false);
      setResult('error', 'Couldn’t accept this trace',
        err instanceof Error ? err.message : 'Contribution failed.');
    }
  }

  ccCancel.addEventListener('click', () => { pendingFile = null; closeConsent(); });
  ccConfirm.addEventListener('click', () => {
    closeConsent();
    const file = pendingFile;
    pendingFile = null;
    if (file) void run(file);
  });
  consent.addEventListener('click', (e) => {
    if (e.target === consent) { pendingFile = null; closeConsent(); }
  });
  crOk.addEventListener('click', closeResult);
  result.addEventListener('click', (e) => {
    if (e.target === result && !crOk.hidden) closeResult();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (consent.classList.contains('open')) { pendingFile = null; closeConsent(); }
    else if (result.classList.contains('open') && !crOk.hidden) closeResult();
  });

  open = (file: File) => { pendingFile = file; openConsent(); };
}

/** Begin a contribution for `file`: shows the consent modal, then uploads on confirm. */
export function requestContribution(file: File): void {
  initContributeDialog();
  open?.(file);
}
