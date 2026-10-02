// Session timeline. Two stacked grids sharing the step axis:
//   • top strip — 5-minute wall-clock blocks (alternating shade) with "Xm" labels every 30 min and
//     "+Nm" markers where empty buckets mean a real time jump (steps are step-indexed, not time-indexed).
//   • main — per-round input context (stacked cached + fresh bars) + an output-token line.
// Rounds that started from a human message are flagged with dashed gold verticals + a legend entry.
import * as echarts from 'echarts';
import type { SessionDetail } from '../analytics/types';
import { mountChart, baseOption, saveAsImage, type ThemeColors } from './theme';
import { compact } from '../format';
import { browserCopy, template } from '../../i18n/browser';

const BLOCK_US = 5 * 60 * 1_000_000; // 5-minute wall-clock blocks
const MAJOR_BUCKETS = 6; // label every 30 minutes (= 6 blocks)

function build(detail: SessionDetail, c: ThemeColors): echarts.EChartsCoreOption {
  const t = browserCopy().charts;
  const ts = t.session;
  const zh = browserCopy().locale === 'zh';
  const minuteLabel = (m: number) => (browserCopy().locale === 'zh' ? `${m}分` : `${m}m`);
  const secondLabel = (s: number) => (zh ? `${s} 秒` : `${s}s`);
  const decodeLabel = (n: number) => (zh ? `${n} token/秒` : `${n} tok/s`);
  const rounds = detail.rounds;
  const x = rounds.map((r) => r.seq);
  // On a CATEGORY axis, markLine `{ xAxis }` is the category INDEX (0-based), not the seq value — so
  // these must be data indices, not r.seq (which is 1-based and would shift every marker one step
  // right). Same reason the markArea blocks below use firstIdx/lastIdx.
  const userIdx = rounds.flatMap((r, i) => (r.isUserInput ? [i] : []));
  const tok = (v: any) => compact(Number(v));

  // --- 5-minute wall-clock buckets for the top strip ---
  // Each block must SPAN every round that falls in its 5-minute window (variable width), not be one
  // fixed-width tick per round. So we record each bucket's first/last round index and draw a markArea
  // band across that range. Blocks alternate by bucket ORDER (not bucket number) so two buckets across
  // a wall-clock gap still differ in shade.
  const t0 = rounds.reduce((m, r) => Math.min(m, r.tsUs), Infinity);
  const bucketOf = (tsUs: number) => Math.floor((tsUs - t0) / BLOCK_US);
  const bucketInfo = new Map<number, { firstIdx: number; lastIdx: number; firstSeq: number }>();
  rounds.forEach((r, i) => {
    const b = bucketOf(r.tsUs);
    const info = bucketInfo.get(b);
    if (!info) bucketInfo.set(b, { firstIdx: i, lastIdx: i, firstSeq: r.seq });
    else info.lastIdx = i;
  });
  const occupied = [...bucketInfo.keys()].sort((a, b) => a - b);

  // alternating bands covering each bucket's full round range (±0.5 category so blocks tile seamlessly)
  const blocks = occupied.map((b, ord) => {
    const info = bucketInfo.get(b)!;
    return [
      { coord: [info.firstIdx - 0.5, 0], itemStyle: { color: ord % 2 === 0 ? c.sageSoft : c.sageWash, opacity: 1 } },
      { coord: [info.lastIdx + 0.5, 1] },
    ];
  });

  // major time labels at the first step of every 30-minute bucket
  const labelAtSeq = new Map<number, string>();
  for (const b of occupied) {
    if (b % MAJOR_BUCKETS === 0) labelAtSeq.set(bucketInfo.get(b)!.firstSeq, minuteLabel(b * 5));
  }

  // wall-clock gap markers: missing buckets between two occupied ones => time elapsed with no steps
  const gaps: { idx: number; label: string }[] = [];
  for (let k = 1; k < occupied.length; k++) {
    const missing = occupied[k] - occupied[k - 1] - 1;
    // idx (category index) for the markLine, not firstSeq — same axis gotcha as the user verticals.
    if (missing > 0) gaps.push({ idx: bucketInfo.get(occupied[k])!.firstIdx, label: `+${minuteLabel(missing * 5)}` });
  }

  const mainNames = [t.series.cachedPrefix, t.series.freshAppend, t.series.output];
  return {
    ...baseOption(c),
    animationDuration: 600,
    animationDurationUpdate: 0, // instant on dataZoom so the dashed markers don't flash while scaling
    toolbox: saveAsImage(c, `session-${detail.sessionId}`),
    legend: {
      data: [...mainNames, t.series.userInput],
      textStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel },
      top: 2,
    },
    tooltip: {
      ...(baseOption(c).tooltip as object),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      // rich per-round readout: tokens + timing + the tools that round called
      formatter: (ps: any[]) => {
        const anchor = ps.find((p) => mainNames.includes(p.seriesName));
        const r = anchor ? rounds[anchor.dataIndex] : undefined;
        if (!r) return '';
        const total = r.prefixTokens + r.appendTokens;
        const hitPct = total ? Math.round((r.prefixTokens / total) * 100) : 0;
        const decode = r.inferenceS ? Math.round(r.outputTokens / r.inferenceS) : 0;
        const lines = [
          `<b>${template(ts.round, { seq: r.seq })}</b> · ${r.isUserInput ? ts.userInput : ts.toolStep}`,
          `${ts.cached} ${tok(r.prefixTokens)} · ${ts.fresh} ${tok(r.appendTokens)} · ${ts.out} ${tok(r.outputTokens)}` +
            (r.reasoningTokens ? ` · ${ts.think} ${tok(r.reasoningTokens)}` : ''),
          `${hitPct}% ${ts.cacheHit} · ${secondLabel(r.inferenceS)} · ${decodeLabel(decode)}`,
        ];
        if (r.tools.length) {
          lines.push(`<span style="opacity:.7">${ts.tools}</span> ${r.tools.map((tool) => `${echarts.format.encodeHTML(tool.name)} ${tool.ms}ms${tool.error ? ' ⚠' : ''}`).join(', ')}`);
        } else if (!r.isUserInput) {
          lines.push(`<span style="opacity:.7">${ts.noToolCalls}</span>`);
        }
        return lines.join('<br>');
      },
    },
    grid: [
      { left: 64, right: 24, top: 60, height: 14 }, // strip (time labels sit above it)
      { left: 64, right: 24, top: 98, bottom: 76 }, // main
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], minValueSpan: 8 },
      {
        type: 'slider',
        xAxisIndex: [0, 1],
        height: 18,
        bottom: 12,
        borderColor: c.line,
        fillerColor: c.sageWash,
        handleStyle: { color: c.sage },
        textStyle: { color: c.muted, fontSize: c.fsTick },
        dataBackground: { lineStyle: { color: c.line }, areaStyle: { color: c.card2 } },
      },
    ],
    xAxis: [
      {
        gridIndex: 0,
        type: 'category',
        data: x,
        position: 'top', // absolute time marks render ABOVE the time-block bar (no overlap)
        axisLine: { show: false },
        axisTick: { show: false },
        // wall-clock labels at 30-minute boundaries; blank elsewhere
        axisLabel: {
          color: c.ink,
          fontFamily: c.font,
          fontSize: c.fsTick,
          interval: 0,
          margin: 6,
          formatter: (v: string) => labelAtSeq.get(Number(v)) ?? '',
        },
      },
      {
        gridIndex: 1,
        type: 'category',
        data: x,
        name: ts.axisRound,
        nameTextStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel },
        axisLine: { lineStyle: { color: c.line } },
        axisTick: { show: false },
        axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick },
      },
    ],
    yAxis: [
      { gridIndex: 0, type: 'value', min: 0, max: 1, show: false },
      {
        gridIndex: 1,
        type: 'value',
        name: ts.axisTokens,
        // right-align + left padding so the name sits in the y-axis margin, clear of the time strip
        nameTextStyle: { color: c.muted, fontFamily: c.font, fontSize: c.fsLabel, align: 'right', padding: [0, 8, 0, 0] },
        axisLabel: { color: c.muted, fontFamily: c.font, fontSize: c.fsTick, formatter: (v: number) => compact(v) },
        axisLine: { lineStyle: { color: c.line } },
        splitLine: { lineStyle: { color: c.line, opacity: 0.6 } },
      },
    ],
    series: [
      // top strip: contiguous 5-minute wall-clock blocks (each spans its bucket's whole round range)
      {
        name: 'time',
        type: 'line',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: rounds.map(() => 0),
        symbol: 'none',
        lineStyle: { opacity: 0 },
        silent: true,
        markArea: { silent: true, itemStyle: { opacity: 1 }, data: blocks as any },
      },
      // main: stacked input context
      {
        name: t.series.cachedPrefix,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        stack: 'in',
        itemStyle: { color: c.sage }, // darker on the bottom of the stack
        data: rounds.map((r) => r.prefixTokens),
        markLine: userIdx.length
          ? {
              symbol: 'none',
              silent: true,
              lineStyle: { color: c.gold, type: 'dashed', opacity: 0.75 },
              label: { show: false },
              data: userIdx.map((i) => ({ xAxis: i })),
            }
          : undefined,
      },
      {
        name: t.series.freshAppend,
        type: 'bar',
        xAxisIndex: 1,
        yAxisIndex: 1,
        stack: 'in',
        itemStyle: { color: c.sageSoft }, // lighter on top
        data: rounds.map((r) => r.appendTokens),
        // wall-clock gap markers (dotted vertical + "+Nm" at top)
        markLine: gaps.length
          ? {
              symbol: 'none',
              silent: true,
              lineStyle: { color: c.muted, type: 'dotted', opacity: 0.5 },
              label: { show: true, position: 'end', color: c.muted, fontFamily: c.font, fontSize: c.fsTick },
              data: gaps.map((g) => ({ xAxis: g.idx, label: { formatter: g.label } })),
            }
          : undefined,
      },
      {
        name: t.series.output,
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        smooth: true,
        showSymbol: false,
        lineStyle: { color: c.terra, width: 2 },
        itemStyle: { color: c.terra },
        data: rounds.map((r) => r.outputTokens),
      },
      // legend-only proxy so "User input" (the dashed verticals) is documented
      {
        name: t.series.userInput,
        type: 'line',
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: [],
        lineStyle: { color: c.gold, type: 'dashed' },
        itemStyle: { color: c.gold },
      },
    ],
  };
}

export function renderSessionTimeline(
  el: HTMLElement,
  detail: SessionDetail,
  onRoundClick?: (round: SessionDetail['rounds'][number], index: number) => void,
) {
  return mountChart(el, (node, c) => {
    const inst = echarts.init(node);
    inst.setOption(build(detail, c));
    // click anywhere in a round's column (strip or main grid) to pin that round in the inspector
    if (onRoundClick) {
      inst.getZr().on('click', (ev: any) => {
        const pt = [ev.offsetX, ev.offsetY];
        if (!inst.containPixel({ gridIndex: 1 }, pt) && !inst.containPixel({ gridIndex: 0 }, pt)) return;
        const conv = inst.convertFromPixel({ gridIndex: 1 }, pt) as number[];
        const i = Math.round(conv?.[0] ?? -1);
        if (i >= 0 && i < detail.rounds.length) onRoundClick(detail.rounds[i], i);
      });
    }
    return inst;
  });
}
