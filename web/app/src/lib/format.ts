// Display formatters shared across surfaces.

/** Thousands-separated integer, e.g. 351029 -> "351,029". */
export function intComma(n: number): string {
  return Math.round(n).toLocaleString('en-US');
}

/** Compact token/count magnitude split into number + unit, e.g. 55_246_666_332 -> {num:"55.2", unit:"B"}. */
export function magnitude(n: number): { num: string; unit: string } {
  if (n >= 1e9) {
    const v = n / 1e9;
    return { num: v >= 10 ? v.toFixed(1) : v.toFixed(2), unit: 'B' };
  }
  if (n >= 1e6) {
    const v = n / 1e6;
    return { num: v >= 10 ? v.toFixed(1) : v.toFixed(2), unit: 'M' };
  }
  if (n >= 1e3) {
    const v = n / 1e3;
    return { num: v >= 100 ? v.toFixed(0) : v.toFixed(1), unit: 'K' };
  }
  return { num: String(Math.round(n)), unit: '' };
}

/** Compact single-string magnitude, e.g. 351029 -> "351K". */
export function compact(n: number): string {
  const m = magnitude(n);
  return `${m.num}${m.unit}`;
}

/** Ratio 0..1 -> integer percent. */
export function pct(ratio: number): number {
  return Math.round(ratio * 100);
}

/** One-decimal percent, e.g. 0.9611 -> "96.1%". */
export function pct1(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}

/** ISO range -> "Sep 23 2025 — Jun 2 2026". */
export function fmtRange(a: string, b: string): string {
  const f = (iso: string) =>
    new Date(iso)
      .toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
      .replace(/,/g, '');
  return `${f(a)} — ${f(b)}`;
}

/** Byte count -> compact "9.7 MB" / "812 KB". */
export function fmtBytes(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} KB`;
  return `${Math.round(n)} B`;
}

/** Seconds -> compact "1.8s" / "320ms". */
export function fmtSeconds(s: number | null): string {
  if (s == null) return '—';
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  return `${s.toFixed(1)}s`;
}

function _ago(iso: string | null): number | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  return Math.max(0, (Date.now() - then) / 1000);
}

/** ISO timestamp -> KPI parts relative to now, e.g. {value:"2", unit:"h ago"}. */
export function relativeParts(iso: string | null): { value: string; unit: string } {
  const s = _ago(iso);
  if (s == null) return { value: '—', unit: '' };
  if (s < 60) return { value: 'just', unit: 'now' };
  const m = s / 60;
  if (m < 60) return { value: String(Math.floor(m)), unit: 'm ago' };
  const h = m / 60;
  if (h < 24) return { value: String(Math.floor(h)), unit: 'h ago' };
  const d = h / 24;
  if (d < 30) return { value: String(Math.floor(d)), unit: 'd ago' };
  return { value: String(Math.floor(d / 30)), unit: 'mo ago' };
}

/** ISO timestamp -> table phrasing, e.g. "2 hours ago" / "yesterday" / "just now". */
export function relativeWhen(iso: string | null): string {
  const s = _ago(iso);
  if (s == null) return '—';
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} minute${m === 1 ? '' : 's'} ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hour${h === 1 ? '' : 's'} ago`;
  const d = Math.floor(h / 24);
  if (d < 2) return 'yesterday';
  if (d < 30) return `${d} days ago`;
  return new Date(iso as string)
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
    .replace(/,/g, '');
}

/** Compact USD, e.g. 12.4 -> "$12.40", 1840 -> "$1.84K", 0.0021 -> "$0.0021". */
export function fmtUsd(n: number): string {
  if (n === 0) return '$0';
  if (n >= 1000) return `$${(n / 1000).toFixed(2)}K`;
  if (n >= 1) return `$${n.toFixed(2)}`;
  if (n >= 0.01) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(4)}`;
}
