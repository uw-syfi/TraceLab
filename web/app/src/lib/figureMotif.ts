// Deterministic mini-motif previews for the figure gallery. Each figure's chart family is inferred
// from its filename/slug/title and rendered as a small themed SVG using palette tokens. The real
// figure still lives on the detail page; these previews give the gallery a consistent dark-mode
// native shape language while keeping each card close to the silhouette of its real chart.

export type MotifKey =
  | 'bars'
  | 'boxplot'
  | 'cdf'
  | 'dash'
  | 'hist'
  | 'providerCdf'
  | 'ranked'
  | 'ring'
  | 'scatter'
  | 'sessionSteps'
  | 'spindle'
  | 'stackedBins'
  | 'stepHist'
  | 'tail';

/** Infer a motif from a figure's filename (falling back to slug/title). Order: specific -> general. */
export function motifFor(card: { category?: string; src?: string; slug?: string; title?: string }): MotifKey {
  const s = [card.category, card.src, card.slug, card.title].filter(Boolean).join(' ').toLowerCase();
  if (/dashboard/.test(s)) return 'dash';
  if (/ring|donut|category_count/.test(s)) return 'ring';
  if (/spindle/.test(s)) return 'spindle';
  if (/weighted_bins|bucket_effects|normalized_overlap|mass bins/.test(s)) return 'stackedBins';
  if (/ranked/.test(s)) return 'ranked';
  if (/latency_by_tool|boxplot/.test(s)) return 'boxplot';
  if (/long_tail|imbalance/.test(s)) return 'tail';
  if (/steps|token_steps/.test(s)) return 'sessionSteps';
  if (/scatter|sample|_vs_/.test(s)) return 'scatter';
  if (/cdf/.test(s)) return /provider|by_provider/.test(s) ? 'providerCdf' : 'cdf';
  if (/prefix_append_distribution|distribution/.test(s)) return 'stepHist';
  if (/histogram|hist|bins/.test(s)) return 'hist';
  return 'bars'; // counts, by-kind, ratio, and the catch-all
}

// Tiny seeded PRNG (mulberry32-ish) so motif geometry is deterministic per card at build time.
function rng(seedStr: string): () => number {
  let h = 1779033703 ^ seedStr.length;
  for (let i = 0; i < seedStr.length; i++) {
    h = Math.imul(h ^ seedStr.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  let a = h >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const svg = (viewBox: string, inner: string) =>
  `<svg viewBox="${viewBox}" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">${inner}</svg>`;

type R = () => number;

const n = (value: number) => Number(value.toFixed(1));

function grid(width: number, height: number, xs: number[], ys: number[] = []): string {
  return [
    ...xs.map((x) => `<path d="M${x} 4 V${height - 5}" stroke="var(--line)" stroke-width="1" opacity="0.55"/>`),
    ...ys.map((y) => `<path d="M4 ${y} H${width - 4}" stroke="var(--line)" stroke-width="1" opacity="0.45"/>`),
  ].join('');
}

// Ranked horizontal bars - counts / by-tool / by-kind / ratio.
function bars(r: R): string {
  const ws = Array.from({ length: 7 }, () => 28 + Math.round(r() * 72)).sort((a, b) => b - a);
  const rows = ws
    .map((w, i) => {
      const y = 6 + i * 11;
      const color = i % 3 === 0 ? 'var(--terra)' : i % 3 === 1 ? 'var(--sage)' : 'var(--gold)';
      return (
        `<rect x="6" y="${y}" width="${w}" height="6.5" rx="3.25" fill="${color}" opacity="${i < 3 ? 0.95 : 0.74}"/>` +
        `<circle cx="${112 - i * 3}" cy="${y + 3.25}" r="1.6" fill="${color}" opacity="0.35"/>`
      );
    })
    .join('');
  return svg('0 0 120 86', grid(120, 86, [34, 64, 94]) + rows);
}

// Horizontal boxplots - latency by tool/provider.
function boxplot(r: R): string {
  const rows = Array.from({ length: 7 }, (_, i) => {
    const y = 9 + i * 10.6;
    const base = 10 + i * 7 + r() * 10;
    const q1 = Math.min(96, base + 10 + r() * 14);
    const q3 = Math.min(114, q1 + 12 + r() * 20);
    const low = Math.max(6, q1 - (8 + r() * 16));
    const high = Math.min(124, q3 + 8 + r() * 22);
    const med = q1 + (q3 - q1) * (0.35 + r() * 0.3);
    const color = i % 2 ? 'var(--sage)' : 'var(--terra)';
    return (
      `<path d="M${n(low)} ${n(y + 3.5)} H${n(high)}" stroke="var(--muted)" stroke-width="1.3" opacity="0.75"/>` +
      `<path d="M${n(low)} ${n(y + 1)} V${n(y + 6)} M${n(high)} ${n(y + 1)} V${n(y + 6)}" stroke="var(--muted)" stroke-width="1.3" opacity="0.75"/>` +
      `<rect x="${n(q1)}" y="${n(y)}" width="${n(q3 - q1)}" height="7" rx="1.7" fill="${color}" opacity="0.43" stroke="${color}" stroke-width="1.3"/>` +
      `<path d="M${n(med)} ${n(y)} V${n(y + 7)}" stroke="${color}" stroke-width="1.6" stroke-linecap="round"/>`
    );
  }).join('');
  return svg('0 0 132 86', grid(132, 86, [18, 38, 62, 90, 118]) + rows);
}

// Histogram / single distribution - a soft bell, taller bars terracotta, tails sage.
function hist(r: R): string {
  const count = 12;
  const shift = r() * 1.8 - 0.9;
  const hs = Array.from({ length: count }, (_, i) => {
    const x = (i - (count - 1) / 2 - shift) / 2.65;
    const shoulder = i > count * 0.65 ? 10 * r() : 0;
    return 10 + Math.round(56 * Math.exp(-(x * x) / 2) + shoulder);
  });
  const med = [...hs].sort((a, b) => a - b)[Math.floor(count / 2)];
  const barsHtml = hs
    .map((h, i) => {
      const fill = h >= med ? 'var(--terra)' : 'var(--sage)';
      return `<rect x="${5 + i * 9.5}" y="${78 - h}" width="6.8" height="${h}" rx="2.4" fill="${fill}" opacity="${h >= med ? 0.96 : 0.72}"/>`;
    })
    .join('');
  return svg('0 0 120 86', grid(120, 86, [24, 48, 72, 96], [25, 52, 78]) + barsHtml);
}

function steppedPath(values: number[], x0: number, y0: number, w: number, h: number): string {
  const step = w / values.length;
  let d = `M${x0} ${y0 + h}`;
  values.forEach((v, i) => {
    const x = x0 + i * step;
    const nx = x0 + (i + 1) * step;
    const y = y0 + h - v * h;
    d += ` L${n(x)} ${n(y)} L${n(nx)} ${n(y)}`;
  });
  return d;
}

// Two stepped histograms - prefix/append and output-token distributions.
function stepHist(r: R): string {
  const curve = (peak: number, skew: number) =>
    Array.from({ length: 13 }, (_, i) => {
      const x = (i - peak) / skew;
      return Math.max(0.04, Math.min(0.92, Math.exp(-(x * x) / 2) * (0.72 + r() * 0.26)));
    });
  const left = steppedPath(curve(9, 2.4), 9, 15, 58, 50);
  const right = steppedPath(curve(4.5, 2.1), 83, 15, 58, 50);
  return svg(
    '0 0 150 86',
    grid(150, 86, [24, 43, 62, 98, 117, 136], [26, 45, 64]) +
      `<path d="${left}" stroke="var(--sage)" stroke-width="2.4" stroke-linejoin="round"/>` +
      `<path d="${steppedPath(curve(8, 1.8), 9, 15, 58, 50)}" stroke="var(--terra)" stroke-width="2.4" stroke-linejoin="round" opacity="0.92"/>` +
      `<path d="${right}" stroke="var(--terra)" stroke-width="2.4" stroke-linejoin="round"/>` +
      `<path d="${steppedPath(curve(5.6, 2.7), 83, 15, 58, 50)}" stroke="var(--sage)" stroke-width="2.4" stroke-linejoin="round" opacity="0.92"/>` +
      `<path d="M75 10 V75" stroke="var(--line)" stroke-width="1.2"/>`,
  );
}

// Cumulative distribution - terracotta line + filled area, sage dashed second series.
function cdf(r: R): string {
  const x1 = 20 + Math.round(r() * 16);
  const x2 = 42 + Math.round(r() * 20);
  const main = `M5 78 C ${x1} 77, ${x2} 24, 116 12`;
  return svg(
    '0 0 120 86',
    grid(120, 86, [28, 56, 84, 112], [22, 50, 78]) +
      `<path d="${main} L116 82 L5 82 Z" fill="var(--terra-wash)" opacity="0.5"/>` +
      `<path d="${main}" stroke="var(--terra)" stroke-width="3.2" stroke-linecap="round"/>` +
      `<path d="M5 80 C ${x1 + 10} 79, ${x2 + 12} 48, 116 ${26 + Math.round(r() * 10)}" stroke="var(--sage)" stroke-width="2.5" stroke-dasharray="3 4" stroke-linecap="round"/>`,
  );
}

// Provider-split CDFs - multiple related cumulative curves on one axis.
function providerCdf(r: R): string {
  const curve = (start: number, bend: number, end: number) =>
    `M8 ${start} C ${30 + r() * 8} ${start - 2}, ${44 + r() * 10} ${bend}, 132 ${end}`;
  return svg(
    '0 0 140 86',
    grid(140, 86, [24, 48, 72, 96, 120], [20, 40, 60, 78]) +
      `<path d="${curve(77, 34, 11)}" stroke="var(--terra)" stroke-width="3" stroke-linecap="round"/>` +
      `<path d="${curve(80, 48, 21)}" stroke="var(--sage)" stroke-width="3" stroke-linecap="round"/>` +
      `<path d="${curve(78, 42, 15)}" stroke="var(--gold)" stroke-width="2.4" stroke-linecap="round" stroke-dasharray="5 4" opacity="0.9"/>` +
      `<circle cx="119" cy="19" r="3" fill="var(--terra)"/><circle cx="128" cy="29" r="3" fill="var(--sage)"/>`,
  );
}

// Scatter - a cloud with a trend line.
function scatter(r: R): string {
  const pts = Array.from({ length: 20 }, (_, i) => {
    const x = 11 + (i % 10) * 10.7 + (r() * 7 - 3.5);
    const row = Math.floor(i / 10);
    const y = Math.max(10, Math.min(74, 64 - (i % 10) * 3.6 + row * 10 + (r() * 20 - 10)));
    return [n(x), n(y)];
  });
  const dots = pts
    .map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="${i % 4 === 0 ? 3.3 : 2.6}" fill="${i % 3 === 0 ? 'var(--sage)' : 'var(--terra)'}" opacity="0.76"/>`)
    .join('');
  return svg(
    '0 0 120 86',
    grid(120, 86, [30, 55, 80, 105], [24, 50, 76]) +
      `<path d="M11 76 L114 76 M11 76 L11 8" stroke="var(--line)" stroke-width="2"/>` +
      `<path d="M13 65 C38 54, 68 43, 112 22" stroke="var(--muted)" stroke-width="2" stroke-linecap="round" opacity="0.82"/>` +
      dots,
  );
}

// Ranked scatter grids - multiple small panels with a rank curve and outliers.
function ranked(r: R): string {
  const panel = (x0: number, y0: number, color: string) => {
    const dots = Array.from({ length: 12 }, (_, i) => {
      const x = x0 + 5 + (i % 6) * 8.8 + (r() * 4 - 2);
      const y = y0 + 25 - Math.min(18, i * 1.1 + r() * 20);
      return `<circle cx="${n(x)}" cy="${n(Math.max(y0 + 5, y))}" r="1.6" fill="${color}" opacity="0.48"/>`;
    }).join('');
    return (
      `<rect x="${x0}" y="${y0}" width="58" height="30" rx="2.5" fill="var(--card)" stroke="var(--line)" stroke-width="1"/>` +
      `<path d="M${x0 + 4} ${y0 + 22} C${x0 + 22} ${y0 + 22}, ${x0 + 47} ${y0 + 19}, ${x0 + 54} ${y0 + 7}" stroke="var(--muted)" stroke-width="1.5" stroke-linecap="round"/>` +
      dots
    );
  };
  return svg(
    '0 0 140 86',
    panel(6, 8, 'var(--terra)') +
      panel(76, 8, 'var(--sage)') +
      panel(6, 48, 'var(--gold)') +
      panel(76, 48, 'var(--terra)'),
  );
}

// Dense session token steps - many vertical bars plus an accumulating total line and event ticks.
function sessionSteps(r: R): string {
  let total = 18;
  const barsHtml = Array.from({ length: 28 }, (_, i) => {
    total += 1.2 + r() * 2.7 + (i % 8 === 0 ? 5 : 0);
    const x = 7 + i * 4;
    const h = Math.min(62, total);
    const fill = i % 13 === 0 || i % 17 === 0 ? 'var(--terra)' : 'var(--sage)';
    return `<rect x="${n(x)}" y="${n(77 - h)}" width="2.8" height="${n(h)}" rx="1.2" fill="${fill}" opacity="${fill === 'var(--terra)' ? 0.98 : 0.78}"/>`;
  }).join('');
  const linePts = Array.from({ length: 8 }, (_, i) => {
    const x = 7 + i * 16;
    const y = 68 - i * 7 + (r() * 5 - 2.5);
    return `${n(x)} ${n(Math.max(12, y))}`;
  }).join(' L');
  const events = [13, 26, 42, 75, 96, 112]
    .map((x) => `<path d="M${x} 11 V78" stroke="var(--terra)" stroke-width="1.1" stroke-dasharray="3 3" opacity="0.45"/>`)
    .join('');
  return svg(
    '0 0 126 86',
    grid(126, 86, [25, 45, 65, 85, 105], [20, 40, 60, 78]) +
      events +
      barsHtml +
      `<path d="M${linePts}" stroke="var(--ink)" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round" opacity="0.72"/>`,
  );
}

// Donut / category share.
function ring(r: R): string {
  const C = 2 * Math.PI * 30;
  const a = 0.34 + r() * 0.18;
  const b = 0.22 + r() * 0.12;
  const c = 0.14 + r() * 0.08;
  const seg = (frac: number, color: string, offset: number) =>
    `<circle cx="43" cy="43" r="30" fill="none" stroke="${color}" stroke-width="12" stroke-dasharray="${C * frac} ${C}" stroke-dashoffset="${-C * offset}" transform="rotate(-90 43 43)"/>`;
  return svg(
    '0 0 86 86',
    `<circle cx="43" cy="43" r="30" fill="none" stroke="var(--line)" stroke-width="12"/>` +
      seg(a, 'var(--terra)', 0) +
      seg(b, 'var(--sage)', a) +
      seg(c, 'var(--gold)', a + b) +
      `<circle cx="43" cy="43" r="15" fill="var(--card)"/>` +
      `<circle cx="43" cy="43" r="4" fill="var(--ink)" opacity="0.25"/>`,
  );
}

// Long-tail bars - a couple of dominant bars then a long low tail.
function tail(r: R): string {
  const hs = [68 + Math.round(r() * 8), 38 + Math.round(r() * 8), 24, 16, 12, 9, 7, 6, 5, 4, 4, 3, 3, 3];
  return svg(
    '0 0 140 82',
    grid(140, 82, [24, 48, 72, 96, 120], [24, 50, 76]) +
      hs
        .map((h, i) => `<rect x="${4 + i * 9.5}" y="${76 - h}" width="6" height="${h}" rx="2" fill="${i < 2 ? 'var(--terra)' : 'var(--sage)'}" opacity="${i < 2 ? 1 : 0.68}"/>`)
        .join('') +
      `<path d="M15 15 C38 33, 76 65, 132 72" stroke="var(--muted)" stroke-width="1.8" stroke-dasharray="4 4" opacity="0.7"/>`,
  );
}

// Dashboard - a ring beside summary bars and a sparkline.
function dash(_r: R): string {
  const C = 2 * Math.PI * 22;
  return svg(
    '0 0 170 86',
    `<g transform="translate(2,8)"><circle cx="32" cy="32" r="22" fill="none" stroke="var(--line)" stroke-width="10"/>` +
      `<circle cx="32" cy="32" r="22" fill="none" stroke="var(--terra)" stroke-width="10" stroke-dasharray="${C * 0.46} ${C}" transform="rotate(-90 32 32)"/>` +
      `<circle cx="32" cy="32" r="22" fill="none" stroke="var(--sage)" stroke-width="10" stroke-dasharray="${C * 0.3} ${C}" stroke-dashoffset="${-C * 0.46}" transform="rotate(-90 32 32)"/></g>` +
      [74, 60, 48].map((w, i) => `<rect x="76" y="${12 + i * 13}" width="${w}" height="6.5" rx="3.25" fill="${i % 2 ? 'var(--sage)' : 'var(--terra)'}" opacity="0.9"/>`).join('') +
      `<path d="M76 69 C92 61, 102 66, 116 54 C128 44, 139 49, 156 34" stroke="var(--gold)" stroke-width="3" stroke-linecap="round"/>`,
  );
}

function stackedRow(y: number, segs: number[], colors: string[]): string {
  let x = 10;
  return segs
    .map((seg, i) => {
      const w = seg * 128;
      const out = `<rect x="${n(x)}" y="${y}" width="${n(w)}" height="9" rx="${i === 0 ? 4.5 : 0}" fill="${colors[i]}" opacity="${0.42 + i * 0.14}"/>`;
      x += w;
      return out;
    })
    .join('');
}

// Weighted/mass bins - paired 100% stacked bars with mapping connectors.
function stackedBins(_r: R): string {
  const colors = ['var(--sage)', 'var(--sage)', 'var(--terra)', 'var(--terra)', 'var(--gold)'];
  const top = [0.52, 0.29, 0.12, 0.05, 0.02];
  const bottom = [0.08, 0.16, 0.28, 0.31, 0.17];
  const boundaries = [0.52, 0.81, 0.93, 0.98].map((v) => 10 + v * 128);
  const bottomBoundaries = [0.08, 0.24, 0.52, 0.83].map((v) => 10 + v * 128);
  const connectors = boundaries
    .map((x, i) => `<path d="M${n(x)} 26 L${n(bottomBoundaries[i])} 52" stroke="var(--muted)" stroke-width="1.5" stroke-dasharray="4 4" opacity="0.72"/>`)
    .join('');
  return svg(
    '0 0 150 86',
    grid(150, 86, [10, 42, 74, 106, 138], [20, 46, 72]) +
      stackedRow(17, top, colors) +
      connectors +
      stackedRow(53, bottom, colors) +
      `<path d="M10 17 H138 M10 62 H138" stroke="var(--line)" stroke-width="1"/>`,
  );
}

// Mirrored density ribbons - token spindles.
function spindle(_r: R): string {
  const path = (y: number, amps: number[], color: string, opacity = 0.88) => {
    const x0 = 8;
    const step = 125 / (amps.length - 1);
    const upper = amps.map((a, i) => `${n(x0 + i * step)} ${n(y - a)}`).join(' L');
    const lower = [...amps].reverse().map((a, i) => `${n(x0 + (amps.length - 1 - i) * step)} ${n(y + a)}`).join(' L');
    return `<path d="M${upper} L${lower} Z" fill="${color}" opacity="${opacity}"/>`;
  };
  const ticks = [24, 48, 72, 96, 120]
    .map((x) => `<path d="M${x} 7 V79" stroke="var(--line)" stroke-width="1.5" stroke-dasharray="4 5"/>`)
    .join('');
  return svg(
    '0 0 142 86',
    ticks +
      path(18, [1, 1, 2, 4, 8, 13, 16, 12, 5, 2, 1], 'var(--sage)', 0.92) +
      path(43, [1, 4, 10, 13, 11, 8, 5, 3, 2, 1, 1], 'var(--terra)', 0.9) +
      path(68, [1, 12, 16, 13, 8, 4, 2, 1, 1, 1, 0.6], 'var(--gold)', 0.82) +
      `<path d="M8 18 H134 M8 43 H134 M8 68 H128" stroke="var(--ink)" stroke-width="1" opacity="0.22"/>`,
  );
}

const RENDERERS: Record<MotifKey, (r: R) => string> = {
  bars,
  boxplot,
  cdf,
  dash,
  hist,
  providerCdf,
  ranked,
  ring,
  scatter,
  sessionSteps,
  spindle,
  stackedBins,
  stepHist,
  tail,
};

/** Render the inferred motif as an inline SVG string (for set:html). `seed` varies the geometry. */
export function motifSvg(key: MotifKey, seed = ''): string {
  return RENDERERS[key](rng(seed || key));
}
