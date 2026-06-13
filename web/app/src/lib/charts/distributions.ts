// Renders the distribution figures from data (not PNG). One entry point dispatches on
// SeriesSpec.kind: cdf (lines) | histogram (bars) | boxplot | barh (ranked horizontal bars) |
// scatter (x/y point cloud).
import * as echarts from 'echarts';
import type { SeriesSpec, CdfSpec, HistogramSpec, BoxplotSpec, BarhSpec, ScatterSpec } from '../analytics/types';
import { mountChart, baseOption, axis, yTitle, saveAsImage, type ThemeColors } from './theme';

const compactNum = (v: number) =>
  v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : String(Math.round(v));

// millisecond value -> human duration tick (ms / s / min) so a log latency axis reads naturally.
// Minutes are only used when the tick lands on a whole minute (e.g. 60000 -> "1 min"); otherwise we
// stay in seconds rather than print a fractional "1.7 min".
function fmtDurMs(v: number): string {
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v >= 60000) {
    const min = v / 60000;
    const r = Math.round(min);
    if (Math.abs(min - r) < 0.02 * r) return `${r} min`;
  }
  return `${+(v / 1000).toFixed(v < 10000 ? 1 : 0)} s`;
}

// wrap a category label across lines (greedy, ~maxChars/line) instead of rotating it.
function wrapLabel(s: string, maxChars = 11): string {
  const lines: string[] = [];
  let cur = '';
  for (const w of s.split(' ')) {
    const next = cur ? `${cur} ${w}` : w;
    if (next.length > maxChars && cur) { lines.push(cur); cur = w; }
    else cur = next;
  }
  if (cur) lines.push(cur);
  return lines.join('\n');
}

// Shared grid for the value-y charts (cdf/histogram/boxplot): a slim top band for the y-axis
// caption (a title left-aligned to grid.left — see yTitle) so it clears the canvas top, plus a
// touch more right room so the last x-tick (e.g. "10,000") isn't clipped. The card itself is
// trimmed (see .dist-card) so the plot keeps its size rather than getting squeezed by this band.
const DIST_GRID = { left: 54, right: 30, top: 44, bottom: 54, containLabel: true };
// Left edge for the y-axis caption: a hair left of grid.left so it sits flush with the tick numbers
// (containLabel leaves a small inset before the label text).
const Y_TITLE_LEFT = DIST_GRID.left - 8;

function cdf(s: CdfSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  return {
    ...baseOption(c),
    grid: DIST_GRID,
    title: yTitle(c, s.yLabel, Y_TITLE_LEFT),
    toolbox: saveAsImage(c, name),
    tooltip: { ...(baseOption(c).tooltip as object), trigger: 'axis' },
    xAxis: { type: s.xLog ? 'log' : 'value', ...axis(c, s.xLabel) },
    yAxis: { type: 'value', ...axis(c) },
    series: s.series.map((ser) => ({
      name: ser.name,
      type: 'line',
      showSymbol: false,
      smooth: false,
      data: ser.points,
    })),
  };
}

function histogram(s: HistogramSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  // Line variant: one frequency polygon per series over a numeric (optionally log) x-axis.
  if (s.series) {
    return {
      ...baseOption(c),
      grid: DIST_GRID,
      title: yTitle(c, s.yLabel, Y_TITLE_LEFT),
      toolbox: saveAsImage(c, name),
      tooltip: { ...(baseOption(c).tooltip as object), trigger: 'axis' },
      xAxis: { type: s.xLog ? 'log' : 'value', ...axis(c, s.xLabel), axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, formatter: compactNum } },
      yAxis: { type: 'value', ...axis(c) },
      series: s.series.map((ser) => ({ name: ser.name, type: 'line', showSymbol: false, smooth: true, areaStyle: { opacity: 0.07 }, data: ser.points })),
    };
  }
  // Category bars (single series).
  return {
    ...baseOption(c),
    grid: DIST_GRID,
    title: yTitle(c, s.yLabel, Y_TITLE_LEFT),
    toolbox: saveAsImage(c, name),
    legend: undefined,
    tooltip: { ...(baseOption(c).tooltip as object), trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'category', data: (s.bins ?? []).map((b) => b.label), ...axis(c, s.xLabel) },
    yAxis: { type: 'value', ...axis(c) },
    series: [{ type: 'bar', itemStyle: { color: c.terra }, data: (s.bins ?? []).map((b) => b.count) }],
  };
}

function boxplot(s: BoxplotSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  return {
    ...baseOption(c),
    grid: DIST_GRID,
    title: yTitle(c, s.yLabel, Y_TITLE_LEFT),
    toolbox: saveAsImage(c, name),
    legend: undefined,
    tooltip: { ...(baseOption(c).tooltip as object), trigger: 'item' },
    // category labels never tilt — they wrap to multiple lines when they don't fit
    xAxis: { type: 'category', data: s.groups.map((g) => g.name), ...axis(c, ''), axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, interval: 0, lineHeight: c.fsTick + 3, formatter: (v: string) => wrapLabel(v) } },
    yAxis: { type: s.yLog ? 'log' : 'value', ...axis(c), axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, ...(s.yUnit === 'ms' ? { formatter: fmtDurMs } : {}) } },
    series: [
      {
        type: 'boxplot',
        itemStyle: { color: c.sageWash, borderColor: c.sage },
        data: s.groups.map((g) => [g.min, g.q1, g.median, g.q3, g.max]),
      },
    ],
  };
}

function scatter(s: ScatterSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  const palette = [c.terra, c.sage, c.gold, c.terraSoft];
  return {
    ...baseOption(c),
    grid: DIST_GRID,
    title: yTitle(c, s.yLabel, Y_TITLE_LEFT),
    toolbox: saveAsImage(c, name),
    tooltip: {
      ...(baseOption(c).tooltip as object),
      trigger: 'item',
      formatter: (p: any) =>
        `${p.marker} ${p.seriesName}<br>prefix ${compactNum(p.value[0])} · append ${compactNum(p.value[1])}`,
    },
    xAxis: { type: s.xLog ? 'log' : 'value', ...axis(c, s.xLabel), axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, formatter: compactNum } },
    yAxis: { type: s.yLog ? 'log' : 'value', ...axis(c), axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, formatter: compactNum } },
    series: s.series.map((ser, i) => ({
      name: ser.name,
      type: 'scatter',
      symbolSize: 7,
      itemStyle: { color: palette[i % palette.length], opacity: 0.5, borderColor: 'transparent' },
      data: ser.points,
    })),
  };
}

function barh(s: BarhSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  const items = [...s.items].sort((a, b) => a.value - b.value); // ascending -> largest on top
  return {
    ...baseOption(c),
    toolbox: saveAsImage(c, name),
    legend: undefined,
    tooltip: { ...(baseOption(c).tooltip as object), trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 28, top: 24, bottom: 46, containLabel: true },
    xAxis: { type: 'value', ...axis(c, s.xLabel) },
    yAxis: { type: 'category', data: items.map((i) => i.name), ...axis(c, '') },
    series: [
      {
        type: 'bar',
        itemStyle: { color: c.sage, borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: c.muted, fontFamily: c.font, fontSize: c.fsTick },
        data: items.map((i) => i.value),
      },
    ],
  };
}

function build(spec: SeriesSpec, c: ThemeColors, name: string): echarts.EChartsCoreOption {
  switch (spec.kind) {
    case 'cdf':
      return cdf(spec, c, name);
    case 'histogram':
      return histogram(spec, c, name);
    case 'boxplot':
      return boxplot(spec, c, name);
    case 'barh':
      return barh(spec, c, name);
    case 'scatter':
      return scatter(spec, c, name);
  }
}

export function renderDistribution(el: HTMLElement, spec: SeriesSpec, name: string) {
  return mountChart(el, (node, c) => {
    const inst = echarts.init(node);
    inst.setOption(build(spec, c, name));
    return inst;
  });
}
