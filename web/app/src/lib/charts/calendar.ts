// Per-day activity as a GitHub-style calendar heatmap (ECharts `calendar` coordinate system).
import * as echarts from 'echarts';
import type { PerDay } from '../analytics/types';
import { mountChart, tooltipStyle, saveAsImage, type ThemeColors } from './theme';
import { compact, fmtUsd } from '../format';

function build(
  data: PerDay[],
  c: ThemeColors,
  layout: { top: number; bottom: number; cellH: number },
): echarts.EChartsCoreOption {
  const days = data.map((d) => d.day).sort();
  const range = days.length ? [days[0], days[days.length - 1]] : undefined;
  const max = data.reduce((m, d) => Math.max(m, d.rounds), 0) || 1;
  const byDay = new Map(data.map((d) => [d.day, d]));
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: c.font, color: c.ink },
    toolbox: saveAsImage(c, 'per-day-activity'),
    tooltip: {
      ...tooltipStyle(c),
      formatter: (p: any) => {
        const d = byDay.get(p.value[0]);
        if (!d) return String(p.value[0]);
        return `<b>${d.day}</b><br/>${d.rounds} steps<br/>${compact(d.inputTokens)} in · ${compact(
          d.outputTokens,
        )} out<br/>${fmtUsd(d.costUsd)}`;
      },
    },
    // No legend/adjustment bar — color still maps by value, just hidden UI (single-hue terra ramp).
    visualMap: {
      show: false,
      min: 0,
      max,
      inRange: { color: [c.terraWash, c.terra] },
    },
    calendar: {
      top: layout.top,
      bottom: layout.bottom,
      left: 58,
      right: 16,
      range,
      cellSize: ['auto', layout.cellH], // computed so the 7 rows fill the container height
      itemStyle: { color: c.card, borderColor: c.line, borderWidth: 1 },
      // only Mon/Wed/Fri labelled — avoids the ambiguous "M T W T F S S" run
      dayLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, firstDay: 1, nameMap: ['', 'Mon', '', 'Wed', '', 'Fri', ''] },
      monthLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, margin: 14 },
      yearLabel: { show: false },
      splitLine: { lineStyle: { color: c.line } },
    },
    series: [
      {
        type: 'heatmap',
        coordinateSystem: 'calendar',
        data: data.map((d) => [d.day, d.rounds]),
      },
    ],
  };
}

export function renderCalendar(el: HTMLElement, data: PerDay[]) {
  return mountChart(el, (node, c) => {
    const inst = echarts.init(node);
    // Fill the container: size the 7 weekday rows from the actual box height (matches the heatmap).
    const top = 34;
    const bottom = 12;
    const cellH = Math.max(20, Math.floor((node.clientHeight - top - bottom) / 7));
    inst.setOption(build(data, c, { top, bottom, cellH }));
    return inst;
  });
}
