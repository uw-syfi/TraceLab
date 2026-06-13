// Cost per model as a stacked bar: cached-read + fresh-input + output. Tooltip totals in USD.
import * as echarts from 'echarts';
import type { CostByModel } from '../analytics/types';
import { mountChart, baseOption, saveAsImage, type ThemeColors } from './theme';
import { fmtUsd } from '../format';

// Wrap a model id onto multiple lines at hyphen boundaries so labels stay upright (no rotation).
function wrapModel(s: string, max = 13): string {
  const lines: string[] = [];
  let cur = '';
  for (const p of s.split('-')) {
    const piece = cur ? `${cur}-${p}` : p;
    if (piece.length > max && cur) {
      lines.push(`${cur}-`);
      cur = p;
    } else {
      cur = piece;
    }
  }
  if (cur) lines.push(cur);
  return lines.join('\n');
}

function build(data: CostByModel[], c: ThemeColors): echarts.EChartsCoreOption {
  const labels = data.map((m) => (m.priced ? m.model : `${m.model} (unpriced)`));
  const usd = (v: number) => fmtUsd(v);
  return {
    ...baseOption(c),
    toolbox: saveAsImage(c, 'cost-by-model'),
    legend: { textStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel }, top: 4 },
    tooltip: {
      ...(baseOption(c).tooltip as object),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (v: any) => usd(Number(v)),
    },
    grid: { left: 60, right: 20, top: 40, bottom: 36, containLabel: true },
    xAxis: {
      type: 'category',
      data: labels,
      axisLine: { lineStyle: { color: c.line } },
      axisTick: { show: false },
      // upright, multi-row labels (wrap at hyphens) instead of inclined text
      axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, interval: 0, lineHeight: 16, formatter: (v: string) => wrapModel(v) },
    },
    yAxis: {
      type: 'value',
      name: 'USD',
      nameTextStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel },
      axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, formatter: (v: number) => usd(v) },
      axisLine: { lineStyle: { color: c.line } },
      splitLine: { lineStyle: { color: c.line, opacity: 0.6 } },
    },
    // single-hue ramp: three parts of one cost, not three unrelated categories (no orange-vs-green
    // clash). Darkest at the bottom of the stack (cached) fading up to lightest (output).
    series: [
      {
        name: 'Cached input',
        type: 'bar',
        stack: 'cost',
        itemStyle: { color: c.terra },
        data: data.map((m) => +m.cachedCost.toFixed(4)),
      },
      {
        name: 'Fresh input',
        type: 'bar',
        stack: 'cost',
        itemStyle: { color: c.terraSoft },
        data: data.map((m) => +m.inputCost.toFixed(4)),
      },
      {
        name: 'Output',
        type: 'bar',
        stack: 'cost',
        itemStyle: { color: c.terraWash },
        data: data.map((m) => +m.outputCost.toFixed(4)),
      },
    ],
  };
}

export function renderCostByModel(el: HTMLElement, data: CostByModel[]) {
  return mountChart(el, (node, c) => {
    const inst = echarts.init(node);
    inst.setOption(build(data, c));
    return inst;
  });
}
