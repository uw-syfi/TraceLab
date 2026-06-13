// Bridges the app's CSS design tokens (styles/tokens.css) into ECharts, and keeps every mounted
// chart in sync with the light/dark toggle.
//
// Charts never hardcode colors — they call mountChart(el, render) where `render(el, colors)` builds
// the option from the ThemeColors handed in. On a theme flip we re-read the tokens and re-render,
// so a single tokens.css edit reflows all charts. We also wire window-resize -> inst.resize().

import * as echarts from 'echarts';

export interface ThemeColors {
  canvas: string;
  card: string;
  card2: string;
  ink: string;
  muted: string;
  line: string;
  terra: string;
  terraSoft: string;
  sage: string;
  sageSoft: string;
  gold: string;
  terraWash: string;
  sageWash: string;
  font: string;
  /** Series palette (ordered). */
  palette: string[];
  /** Chart text sizes, parsed from the app's font tokens (so charts match the UI scale). */
  fsTick: number; // axis tick labels / captions  (--fs-sm)
  fsLabel: number; // axis names / legend / tooltip (--fs-base)
  /** True when the obsidian dark theme is active. */
  dark: boolean;
}

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Parse a px token like "16px" to a number, with a fallback. */
function pxVar(name: string, fallback: number): number {
  const n = parseInt(cssVar(name), 10);
  return Number.isFinite(n) ? n : fallback;
}

/** Snapshot the current token values. Re-read on every (re)render so theme flips are picked up. */
export function readTheme(): ThemeColors {
  const terra = cssVar('--terra');
  const sage = cssVar('--sage');
  const gold = cssVar('--gold');
  const terraSoft = cssVar('--terra-soft');
  const sageSoft = cssVar('--sage-soft');
  const muted = cssVar('--muted');
  return {
    canvas: cssVar('--canvas'),
    card: cssVar('--card'),
    card2: cssVar('--card-2'),
    ink: cssVar('--ink'),
    muted,
    line: cssVar('--line'),
    terra,
    terraSoft,
    sage,
    sageSoft,
    gold,
    terraWash: cssVar('--terra-wash'),
    sageWash: cssVar('--sage-wash'),
    font: cssVar('--sans') || 'system-ui, sans-serif',
    palette: [terra, sage, gold, terraSoft, sageSoft, muted],
    fsTick: pxVar('--fs-sm', 18), // axis tick labels / captions
    fsLabel: pxVar('--fs-base', 20), // axis names / legend / tooltip
    dark: document.documentElement.dataset.theme === 'dark',
  };
}

/** Provider -> stable accent (Claude=terra, Codex=sage), so the same provider reads the same color. */
export function providerColor(c: ThemeColors, provider: string): string {
  if (provider === 'claude') return c.terra;
  if (provider === 'codex') return c.sage;
  return c.gold;
}

/** Token-driven tooltip box styling (card surface, hairline border, ink text). */
export function tooltipStyle(c: ThemeColors): Record<string, unknown> {
  return {
    backgroundColor: c.card,
    borderColor: c.line,
    borderWidth: 1,
    textStyle: { color: c.ink, fontFamily: c.font, fontSize: c.fsLabel },
    extraCssText: 'border-radius:12px;box-shadow:var(--sh-2);',
  };
}

/** Cartesian base: transparent ground, token text, palette, grid, tooltip, legend. Spread into options. */
export function baseOption(c: ThemeColors): echarts.EChartsCoreOption {
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: c.font, color: c.ink, fontSize: c.fsTick },
    color: c.palette,
    grid: { left: 56, right: 24, top: 40, bottom: 48, containLabel: true },
    tooltip: { ...tooltipStyle(c) },
    legend: { textStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel }, inactiveColor: c.line, top: 4 },
  };
}

/** Axis styling helper (value/category) consistent across charts. The axis `name` is centered along
 *  the axis (`nameLocation:'middle'`) and pushed clear of the tick labels via nameGap — so on the
 *  x-axis it reads as a centered caption below the ticks rather than ECharts' default far-right
 *  ('end') placement, which crowded/clipped the bottom-right corner. (Distribution y-axes pass an
 *  empty name and use a top-left title instead, so this only affects x-axis captions.) */
export function axis(c: ThemeColors, name = ''): Record<string, unknown> {
  return {
    name,
    nameLocation: 'middle',
    nameGap: 30,
    nameTextStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel },
    axisLine: { lineStyle: { color: c.line } },
    axisTick: { lineStyle: { color: c.line } },
    axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick },
    splitLine: { lineStyle: { color: c.line, opacity: 0.6 } },
  };
}

/** Y-axis caption as a free `title`, left-aligned to the tick-label column and sitting above the
 *  plot. We use a title rather than the axis `name` on purpose: the axis name anchors to the axis
 *  *line* (right of the labels), so it reads as indented from the tick numbers. A title at `left`
 *  lines up with the labels instead. Pass the chart's `grid.left` as `left` — with `containLabel`
 *  that's where the tick labels' left edge sits, so it aligns for both narrow ("1") and wide
 *  ("10,000") labels. Pair with DIST_GRID, whose grid.top reserves the band it sits in. */
export function yTitle(c: ThemeColors, text: string, left: number): Record<string, unknown> {
  return {
    text,
    left,
    top: 2,
    textStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, fontWeight: 'normal' },
  };
}

/** ECharts toolbox with a high-res PNG export button (matches "download as PNG"). */
export function saveAsImage(c: ThemeColors, name: string) {
  return {
    feature: {
      saveAsImage: {
        type: 'png',
        name,
        pixelRatio: 3,
        backgroundColor: c.canvas,
        title: 'Download PNG',
        iconStyle: { borderColor: c.muted },
      },
    },
    right: 8,
    top: 2,
  };
}

type Renderer = (el: HTMLElement, colors: ThemeColors) => echarts.ECharts;

interface Entry {
  el: HTMLElement;
  render: Renderer;
  inst: echarts.ECharts;
}

const registry: Entry[] = [];
let wired = false;

/** Mount a chart and register it for auto re-theme (on toggle) + resize. Returns the instance.
 *  Idempotent per element: re-mounting into the same container disposes the previous instance and
 *  replaces it (so e.g. switching the selected session re-renders in place, no stacking/leak). */
export function mountChart(el: HTMLElement, render: Renderer): echarts.ECharts {
  wireGlobals();
  const existing = registry.find((e) => e.el === el);
  if (existing) existing.inst.dispose(); // free the old instance before re-init on the same dom
  const inst = render(el, readTheme());
  if (existing) {
    existing.render = render;
    existing.inst = inst;
  } else {
    registry.push({ el, render, inst });
  }
  return inst;
}

function wireGlobals(): void {
  if (wired) return;
  wired = true;

  // Re-render all charts when the theme attribute flips.
  new MutationObserver((records) => {
    if (!records.some((r) => r.attributeName === 'data-theme')) return;
    const colors = readTheme();
    for (const e of registry) {
      e.inst.dispose();
      e.inst = e.render(e.el, colors);
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });

  // Keep charts sized to their (responsive) containers.
  let raf = 0;
  window.addEventListener('resize', () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => registry.forEach((e) => e.inst.resize()));
  });
}

/** Dispose every mounted chart (e.g. before a fresh render pass). */
export function disposeAll(): void {
  for (const e of registry) e.inst.dispose();
  registry.length = 0;
}
