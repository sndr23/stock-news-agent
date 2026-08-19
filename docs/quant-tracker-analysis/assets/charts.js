(function() {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // ---------- Chart 1: radar — current vs target ----------
  var radar = echarts.init(document.getElementById('chart-radar'), null, { renderer: 'svg' });
  radar.setOption({
    animation: false,
    color: [muted, accent],
    tooltip: { appendToBody: true, trigger: 'item' },
    legend: {
      bottom: 0,
      textStyle: { color: ink, fontSize: 13 },
      itemWidth: 18, itemHeight: 10
    },
    radar: {
      indicator: [
        { name: '宏观因子监测', max: 10 },
        { name: '事件资讯覆盖', max: 10 },
        { name: '资讯↔量化关联', max: 10 },
        { name: '个股级追踪', max: 10 },
        { name: '资金流监测', max: 10 },
        { name: '信号有效性验证', max: 10 }
      ],
      radius: '62%',
      center: ['50%', '48%'],
      axisName: { color: ink, fontSize: 13, fontWeight: 600 },
      splitLine: { lineStyle: { color: rule } },
      splitArea: { areaStyle: { color: [bg2, '#f2f4f6'] } },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [{
      type: 'radar',
      symbolSize: 5,
      data: [
        {
          value: [7, 8, 2, 1.5, 2, 0.5],
          name: '现状',
          lineStyle: { width: 2, color: muted },
          itemStyle: { color: muted },
          areaStyle: { color: muted + '26' }
        },
        {
          value: [9, 8.5, 9, 8, 8, 9],
          name: '目标(追踪量化工具)',
          lineStyle: { width: 2.5, color: accent },
          itemStyle: { color: accent },
          areaStyle: { color: accent + '22' }
        }
      ]
    }]
  });
  window.addEventListener('resize', function() { radar.resize(); });

  // ---------- Chart 2: bars — capability by phase ----------
  var phase = echarts.init(document.getElementById('chart-phase'), null, { renderer: 'svg' });
  phase.setOption({
    animation: false,
    color: [muted, accent2, '#a07414', accent],
    tooltip: { appendToBody: true, trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: {
      bottom: 0,
      textStyle: { color: ink, fontSize: 13 },
      itemWidth: 14, itemHeight: 10
    },
    grid: { left: 8, right: 12, top: 40, bottom: 70, containLabel: true },
    xAxis: {
      type: 'category',
      data: ['宏观因子监测', '事件资讯覆盖', '资讯↔量化关联', '个股级追踪', '资金流监测', '信号有效性验证'],
      axisLabel: { color: ink, fontSize: 12, interval: 0, rotate: 20 },
      axisLine: { lineStyle: { color: rule } },
      axisTick: { show: false }
    },
    yAxis: {
      type: 'value',
      max: 10,
      name: '能力评估分(0-10)',
      nameTextStyle: { color: muted, fontSize: 11 },
      axisLabel: { color: muted },
      splitLine: { lineStyle: { color: rule } }
    },
    series: [
      {
        name: '现状', type: 'bar', barGap: 0.18, barMaxWidth: 26,
        itemStyle: { color: muted, opacity: 0.55 },
        data: [7, 8, 2, 1.5, 2, 0.5]
      },
      {
        name: 'P0 融合展示后', type: 'bar', barMaxWidth: 26,
        itemStyle: { color: accent2 },
        data: [7, 8, 7, 1.5, 2, 1]
      },
      {
        name: 'P1 个股+资金流后', type: 'bar', barMaxWidth: 26,
        itemStyle: { color: '#a07414' },
        data: [7.5, 8, 7.5, 7, 7, 2]
      },
      {
        name: 'P2 验证闭环后', type: 'bar', barMaxWidth: 26,
        itemStyle: { color: accent },
        data: [9, 8.5, 9, 8, 8.5, 8.5]
      }
    ]
  });
  window.addEventListener('resize', function() { phase.resize(); });

  // ---------- mermaid ----------
  if (window.mermaid) {
    mermaid.initialize({ startOnLoad: true, theme: 'neutral', securityLevel: 'loose' });
  }
})();
