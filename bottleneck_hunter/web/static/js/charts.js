/**
 * charts.js — ECharts wrappers for force-directed DAG, bar chart, and radar chart.
 */

const COLORS = {
  accent:  '#3b6bcc',
  success: '#2d8a5e',
  warning: '#b58a1c',
  danger:  '#c44832',
  muted:   '#7a7d82',
};

/* ── 节点类型配色（按 layer_type）────────────────────── */
const NODE_PALETTE = {
  end_product:   { color: '#5470c6', label: '终端产品' },
  assembly:      { color: '#91cc75', label: '总成/组件' },
  component:     { color: '#fac858', label: '零部件' },
  sub_component: { color: '#fac858', label: '零部件' },
  material:      { color: '#ee6666', label: '材料' },
  raw_material:  { color: '#ee6666', label: '原材料' },
  equipment:     { color: '#73c0de', label: '设备' },
};

/* ── 连线类型分类 ───────────────────────────────────── */
const LINK_STYLES = {
  critical: { color: '#c44832', width: 3.5, type: 'solid', label: '关键依赖（不可替代）' },
  strong:   { color: '#b58a1c', width: 2,   type: 'solid', label: '强依赖' },
  weak:     { color: '#555960', width: 1,   type: 'dashed', label: '弱依赖' },
};

function classifyLink(dep, alt) {
  if (dep >= 0.9 && alt === 0) return 'critical';
  if (dep >= 0.7) return 'strong';
  return 'weak';
}

const instances = new Map();

/* ── Chart instance management ───────────────────────── */
function getChart(containerId) {
  let inst = instances.get(containerId);
  const dom = document.getElementById(containerId);
  if (!dom) return null;
  if (inst && inst.getDom() !== dom) {
    inst.dispose();
    inst = null;
  }
  if (!inst) {
    inst = echarts.init(dom);
    instances.set(containerId, inst);
  }
  return inst;
}

/* ── Resize all charts ───────────────────────────────── */
export function resizeAll() {
  instances.forEach(inst => {
    try { inst.resize(); } catch { /* disposed */ }
  });
}

window.addEventListener('resize', () => resizeAll());

/* ── Layer type 中文标签 ────────────────────────────── */
const LAYER_TYPE_LABELS = {
  end_product: '终端产品', assembly: '总成/组件',
  component: '零部件', sub_component: '子组件',
  material: '材料', raw_material: '原材料', equipment: '设备',
};

/* ── renderDAG: 力导向产业链图谱 ────────────────────── */
export function renderDAG(chainData) {
  const dom = document.getElementById('chart-chain');
  if (!dom) return;

  const allNodes = chainData.nodes || [];
  if (allNodes.length === 0) return;

  const chart = getChart('chart-chain');
  if (!chart) return;
  chart.resize();

  const totalNodes = allNodes.length;

  // ── 收集有哪些节点类别和连线类别（用于图例）─────────
  const nodeCategories = [];
  const nodeCatSet = new Set();
  allNodes.forEach(n => {
    const lt = n.layer_type || 'component';
    const p = NODE_PALETTE[lt] || NODE_PALETTE.component;
    if (!nodeCatSet.has(p.label)) {
      nodeCatSet.add(p.label);
      nodeCategories.push({ name: p.label, itemStyle: { color: p.color } });
    }
  });

  // ── 构建节点 ────────────────────────────────────────
  const nodes = allNodes.map(n => {
    const lt = n.layer_type || 'component';
    const p = NODE_PALETTE[lt] || NODE_PALETTE.component;
    const isBn = !!n.is_bottleneck;

    // 节点大小：瓶颈 > 终端 > 普通
    let size = totalNodes > 20 ? 28 : 36;
    if (n.layer === 0) size = totalNodes > 20 ? 38 : 48;
    if (isBn) size = totalNodes > 20 ? 42 : 50;

    // Tooltip 内容
    const ltLabel = LAYER_TYPE_LABELS[lt] || lt;
    const paramsStr = (n.key_parameters || []).length > 0
      ? `<br/><span style="color:#aaa">参数:</span> ${n.key_parameters.join(', ')}` : '';
    const funcStr = n.function
      ? `<br/><span style="color:#aaa">功能:</span> ${n.function}` : '';
    const bnBadge = isBn ? ' <span style="color:#ee6666;font-weight:700">★ 瓶颈</span>' : '';
    const tip = `<b style="font-size:14px">${n.name}</b>${bnBadge}`
      + `<br/><span style="color:#aaa">类型:</span> ${ltLabel} (L${n.layer})`
      + `<br/>${n.description || ''}`
      + funcStr + paramsStr;

    return {
      id: n.id || n.name,
      name: n.name,
      symbolSize: size,
      category: p.label,
      itemStyle: {
        color: isBn ? '#ee6666' : p.color,
        borderColor: isBn ? '#ff9999' : 'rgba(255,255,255,0.15)',
        borderWidth: isBn ? 3 : 1,
        shadowBlur: isBn ? 18 : 0,
        shadowColor: isBn ? 'rgba(238,102,102,0.6)' : 'transparent',
      },
      label: {
        show: true,
        formatter: n.name.length > 7 ? n.name.slice(0, 6) + '…' : n.name,
        fontSize: totalNodes > 20 ? 10 : 12,
        color: isBn ? '#ee6666' : p.color,
        backgroundColor: 'transparent',
        borderRadius: 3,
        padding: [2, 6],
        position: 'bottom',
        distance: 4,
      },
      tooltip: { formatter: () => tip },
      // 传递给力导向的层级信息，用于初始 x 偏移
      _layer: n.layer || 0,
    };
  });

  // ── 构建连线（修复: upstream→source, downstream→target, dependency→width）
  const linkCatSet = new Set();
  const links = (chainData.links || []).map(l => {
    const dep = l.dependency ?? 0.5;
    const alt = l.alternatives ?? 0;
    const cls = classifyLink(dep, alt);
    const style = LINK_STYLES[cls];
    linkCatSet.add(cls);

    return {
      source: l.upstream,
      target: l.downstream,
      lineStyle: {
        width: style.width,
        color: style.color,
        type: style.type,
        curveness: 0.2,
        opacity: 0.75,
      },
      tooltip: {
        formatter: () => {
          const depPct = (dep * 100).toFixed(0);
          return `${l.upstream} → ${l.downstream}<br/>`
            + `依赖度: <b>${depPct}%</b> | 替代方案: <b>${alt}</b><br/>`
            + `<span style="color:${style.color}">${style.label}</span>`;
        },
      },
    };
  });

  // ── 图例数据 ────────────────────────────────────────
  const legendData = [
    ...nodeCategories.map(c => c.name),
    ...[...linkCatSet].map(cls => LINK_STYLES[cls].label),
  ];

  // categories 数组给 ECharts（节点 + 连线的伪 category）
  const categories = [
    ...nodeCategories,
    ...[...linkCatSet].map(cls => ({
      name: LINK_STYLES[cls].label,
      itemStyle: { color: LINK_STYLES[cls].color },
      symbol: 'rect',
      symbolSize: 10,
    })),
  ];

  // ── 力导向参数（根据节点数动态调整）────────────────
  const repulsion = totalNodes > 25 ? 200 : (totalNodes > 15 ? 280 : 360);
  const edgeMin = totalNodes > 25 ? 80 : 120;
  const edgeMax = totalNodes > 25 ? 180 : 260;

  chart.setOption({
    tooltip: {
      triggerOn: 'mousemove',
      backgroundColor: 'rgba(20,20,24,0.92)',
      borderColor: '#444',
      textStyle: { color: '#e0e0e0', fontSize: 12 },
    },
    legend: {
      data: legendData,
      orient: 'vertical',
      right: 12,
      top: 12,
      textStyle: { color: '#bbb', fontSize: 11 },
      backgroundColor: 'rgba(20,20,24,0.6)',
      borderRadius: 6,
      padding: [8, 12],
      itemWidth: 14,
      itemHeight: 14,
      itemGap: 8,
    },
    animationDuration: 800,
    animationEasingUpdate: 'quinticInOut',
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      data: nodes,
      links,
      categories,
      force: {
        initLayout: 'circular',
        repulsion,
        gravity: 0.08,
        edgeLength: [edgeMin, edgeMax],
        friction: 0.6,
        layoutAnimation: true,
      },
      lineStyle: { opacity: 0.75 },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 4 },
        itemStyle: { shadowBlur: 20, shadowColor: 'rgba(255,255,255,0.3)' },
      },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 8],
      scaleLimit: { min: 0.4, max: 3 },
    }],
  }, true);
}

/* ── renderBottleneckBars: horizontal bar chart ───────── */
export function renderBottleneckBars(reports) {
  const chart = getChart('chart-bottleneck');
  if (!chart) return;

  const sorted = [...reports].sort((a, b) => (a.overall_score || 0) - (b.overall_score || 0));
  const names = sorted.map(r => r.node_name || r.name || '');
  const scores = sorted.map(r => r.overall_score || 0);

  chart.setOption({
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
    },
    grid: { left: 120, right: 40, top: 10, bottom: 20 },
    xAxis: {
      type: 'value',
      max: 10,
      axisLabel: { color: '#999' },
      splitLine: { lineStyle: { color: '#333' } },
    },
    yAxis: {
      type: 'category',
      data: names,
      axisLabel: { color: '#ccc', fontSize: 12 },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      data: scores.map(v => ({
        value: v,
        itemStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
            { offset: 0, color: COLORS.muted },
            { offset: 1, color: COLORS.accent },
          ]),
        },
      })),
      barMaxWidth: 24,
      label: {
        show: true,
        position: 'right',
        color: '#ccc',
        formatter: '{c}',
      },
    }],
  });

  // Click on bar → show radar for that bottleneck
  chart.off('click');
  chart.on('click', params => {
    const idx = params.dataIndex;
    const report = sorted[idx];
    if (report) {
      // 通知 dashboard 更新切换按钮状态
      if (typeof window._onBottleneckBarClick === 'function') {
        window._onBottleneckBarClick(idx, report);
      } else {
        renderRadar(report);
      }
    }
  });
}

/* ── renderRadar: 5-dimension radar for a bottleneck ──── */
export function renderRadar(report) {
  const chart = getChart('chart-bottleneck');
  if (!chart) return;

  const dims = [
    { key: 'scarcity',          label: '稀缺性' },
    { key: 'irreplaceability',  label: '不可替代性' },
    { key: 'supply_demand_gap', label: '供需缺口' },
    { key: 'pricing_power',     label: '定价权' },
    { key: 'tech_barrier',      label: '技术壁垒' },
  ];

  // 从 scores 数组中提取各维度分值（兼容 dimension_scores 扁平结构）
  let scoreMap = {};
  if (report.dimension_scores && typeof report.dimension_scores === 'object') {
    scoreMap = report.dimension_scores;
  } else if (Array.isArray(report.scores)) {
    report.scores.forEach(s => {
      scoreMap[s.dimension] = s.score;
    });
  } else {
    scoreMap = report;
  }

  const values = dims.map(d => scoreMap[d.key] ?? 0);
  const name = report.node_name || report.name || '';

  chart.setOption({
    tooltip: {},
    radar: {
      indicator: dims.map(d => ({ name: d.label, max: 10 })),
      shape: 'polygon',
      axisName: { color: '#ccc' },
      splitArea: { areaStyle: { color: ['rgba(59,107,204,0.05)', 'rgba(59,107,204,0.1)'] } },
      splitLine: { lineStyle: { color: '#444' } },
    },
    grid: undefined,
    xAxis: undefined,
    yAxis: undefined,
    series: [{
      type: 'radar',
      data: [{
        value: values,
        name,
        areaStyle: { color: 'rgba(59,107,204,0.25)' },
        lineStyle: { color: COLORS.accent },
        itemStyle: { color: COLORS.accent },
      }],
    }],
  }, true); // true = not merge, replace all options

  // Click radar to go back to bar chart
  chart.off('click');
  chart.on('click', () => {
    const reports = window.appState.results.bottleneck;
    if (reports) renderBottleneckBars(reports);
  });
}
