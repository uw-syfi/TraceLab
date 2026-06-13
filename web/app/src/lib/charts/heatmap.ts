// Work-rhythm heatmap: hour-of-day (x) x weekday (y), colored by step count. "When do you code?"
import * as echarts from 'echarts';
import type { HourWeekday } from '../analytics/types';
import { mountChart, tooltipStyle, saveAsImage, type ThemeColors } from './theme';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const HOURS = Array.from({ length: 24 }, (_, h) => `${h}`);

function build(data: HourWeekday[], c: ThemeColors): echarts.EChartsCoreOption {
  const max = data.reduce((m, d) => Math.max(m, d.rounds), 0) || 1;
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: c.font, color: c.ink },
    toolbox: saveAsImage(c, 'work-rhythm'),
    grid: { left: 56, right: 20, top: 18, bottom: 30, containLabel: true },
    tooltip: {
      ...tooltipStyle(c),
      formatter: (p: any) =>
        `${WEEKDAYS[p.value[1]]} ${String(p.value[0]).padStart(2, '0')}:00<br/><b>${p.value[2]}</b> steps`,
    },
    xAxis: {
      type: 'category',
      data: HOURS,
      splitArea: { show: false },
      axisLine: { lineStyle: { color: c.line } },
      axisTick: { show: false },
      axisLabel: {
        color: c.muted,
        fontFamily: c.font,
        fontSize: c.fsTick,
        // label every 3h in plain am/pm so the hour axis reads clearly (12a · 3a · 6a · …)
        interval: (idx: number) => idx % 3 === 0,
        formatter: (h: string) => {
          const n = +h;
          return `${n % 12 === 0 ? 12 : n % 12}${n < 12 ? 'a' : 'p'}`;
        },
      },
    },
    yAxis: {
      type: 'category',
      data: WEEKDAYS,
      inverse: true,
      splitArea: { show: false },
      axisLine: { lineStyle: { color: c.line } },
      axisTick: { show: false },
      axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick },
    },
    // No legend/adjustment bar — color still maps by value, just hidden UI (single-hue terra ramp).
    visualMap: {
      show: false,
      min: 0,
      max,
      inRange: { color: [c.terraWash, c.terra] },
    },
    series: [
      {
        type: 'heatmap',
        data: data.map((d) => [d.hour, d.weekday, d.rounds]),
        label: { show: false },
        itemStyle: { borderColor: c.canvas, borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: c.ink, borderWidth: 1 } },
      },
    ],
  };
}

export function renderHourWeekday(el: HTMLElement, data: HourWeekday[]) {
  return mountChart(el, (node, c) => {
    const inst = echarts.init(node);
    inst.setOption(build(data, c));
    return inst;
  });
}
