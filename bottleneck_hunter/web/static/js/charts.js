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
  end_product:   { color: '#3b5998', label: '终端产品',   fontColor: '#1a1a1a', fontWeight: 'bold' },
  assembly:      { color: '#3d7a45', label: '总成/组件',  fontColor: '#1a1a1a', fontWeight: 'bold' },
  component:     { color: '#fac858', label: '零部件',     fontColor: '#1a1a1a', fontWeight: 'normal' },
  sub_component: { color: '#fac858', label: '零部件',     fontColor: '#1a1a1a', fontWeight: 'normal' },
  material:      { color: '#ee6666', label: '材料',       fontColor: '#1a1a1a', fontWeight: 'normal' },
  raw_material:  { color: '#ee6666', label: '原材料',     fontColor: '#1a1a1a', fontWeight: 'normal' },
  equipment:     { color: '#73c0de', label: '设备',       fontColor: '#1a1a1a', fontWeight: 'normal' },
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

  if (inst) {
    try {
      if (inst.isDisposed() || inst.getDom() !== dom || !dom.querySelector('canvas, svg')) {
        try { inst.dispose(); } catch {}
        inst = null;
        instances.delete(containerId);
      }
    } catch {
      inst = null;
      instances.delete(containerId);
    }
  }

  if (!inst) {
    const existing = echarts.getInstanceByDom(dom);
    if (existing) { try { existing.dispose(); } catch {} }
    inst = echarts.init(dom);
    instances.set(containerId, inst);
  }
  return inst;
}

export function disposeAllCharts() {
  instances.forEach((inst, id) => {
    try { inst.dispose(); } catch {}
  });
  instances.clear();
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

/* ── 全屏功能 ─────────────────────────────────────────── */
export function initChartFullscreen() {
  const btn = document.getElementById('btn-chart-fullscreen');
  const card = document.getElementById('card-chain');
  if (!btn || !card) return;

  btn.addEventListener('click', () => {
    if (!document.fullscreenElement) {
      card.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen();
    }
  });

  document.addEventListener('fullscreenchange', () => {
    const isFs = !!document.fullscreenElement;
    const label = btn.querySelector('.fullscreen-label');
    if (label) label.textContent = isFs ? '还原' : '全屏';
    btn.querySelector('.fullscreen-expand').style.display = isFs ? 'none' : '';
    btn.querySelector('.fullscreen-shrink').style.display = isFs ? '' : 'none';
    // 多次 resize 防止过渡期 canvas 尺寸为 0
    setTimeout(() => resizeAll(), 50);
    setTimeout(() => resizeAll(), 200);
    setTimeout(() => resizeAll(), 500);
  });
}

/* ── 节点信息面板（代表性企业）─────────────────────────── */
function _showNodeInfoPanel(nodeData, allNodes) {
  const panel = document.getElementById('node-info-panel');
  if (!panel) return;

  const node = allNodes.find(n => n.name === nodeData.name);
  if (!node) { panel.style.display = 'none'; return; }

  const companies = _filterCompaniesByMarket(node.representative_companies || []);
  const lt = node.layer_type || 'component';
  const ltLabel = LAYER_TYPE_LABELS[lt] || lt;

  let html = `<div class="nip-header">
    <span class="nip-title">${_esc(node.name)}</span>
    <span class="nip-type">${ltLabel} · L${node.layer}</span>
    <button class="nip-close" title="关闭">&times;</button>
  </div>`;

  if (node.description) {
    html += `<div class="nip-desc">${_esc(node.description)}</div>`;
  }

  if (companies.length > 0) {
    html += `<div class="nip-section-title">代表性企业</div><ul class="nip-companies">`;
    for (const c of companies) {
      const name = _esc(c.name || '');
      const code = c.code ? `<span class="nip-code">${_esc(c.code)}</span>` : '';
      html += `<li>${name} ${code}</li>`;
    }
    html += `</ul>`;
  } else {
    html += `<div class="nip-empty">暂无代表性企业数据</div>`;
  }

  panel.innerHTML = html;
  panel.style.display = 'block';

  panel.querySelector('.nip-close').addEventListener('click', () => {
    panel.style.display = 'none';
  });
}

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ── 市场过滤：按目标市场筛选代表企业 ──────────────────── */
function _filterCompaniesByMarket(companies) {
  const cfg = window.appState?.config || {};
  const market = cfg.market || document.querySelector('input[name="market"]:checked')?.value || 'all';
  if (market === 'all' || !companies.length) return companies;

  return companies.filter(c => {
    const code = (c.code || '').trim();
    if (!code) return true;
    if (market === 'a_stock') return /\.(SH|SZ|BJ)$/i.test(code) || /^\d{6}$/.test(code);
    if (market === 'us_stock') return /^[A-Z]{1,5}$/.test(code);
    return true;
  });
}

/* ── DAG 交互状态 ──────────────────────────────────────── */
const _dagState = { locked: false, lockedNode: null, highlighted: null };

function _getConnectedNodes(nodeName, links) {
  const adj = new Map();
  for (const l of links) {
    const s = l.source ?? l.upstream;
    const t = l.target ?? l.downstream;
    if (!adj.has(s)) adj.set(s, []);
    if (!adj.has(t)) adj.set(t, []);
    adj.get(s).push(t);
    adj.get(t).push(s);
  }
  const visited = new Set([nodeName]);
  const queue = [nodeName];
  while (queue.length > 0) {
    const cur = queue.shift();
    for (const nb of (adj.get(cur) || [])) {
      if (!visited.has(nb)) { visited.add(nb); queue.push(nb); }
    }
  }
  return visited;
}

function _getConnectedLinks(connectedSet, links) {
  return new Set(links.map((l, i) => {
    const s = l.source ?? l.upstream;
    const t = l.target ?? l.downstream;
    return (connectedSet.has(s) && connectedSet.has(t)) ? i : -1;
  }).filter(i => i >= 0));
}

function _applyHighlight(chart, nodes, links, connectedSet) {
  const connectedLinkSet = _getConnectedLinks(connectedSet, links);
  const updatedNodes = nodes.map(n => ({
    name: n.name,
    id: n.id || n.name,
    symbolSize: n.symbolSize,
    category: n.category,
    itemStyle: { ...n.itemStyle, opacity: connectedSet.has(n.name) ? 1 : 0.1 },
    label: { ...n.label, opacity: connectedSet.has(n.name) ? 1 : 0.15 },
    tooltip: n.tooltip,
  }));
  const updatedLinks = links.map((l, i) => ({
    source: l.source,
    target: l.target,
    lineStyle: { ...l.lineStyle, opacity: connectedLinkSet.has(i) ? 0.85 : 0.04 },
    tooltip: l.tooltip,
  }));
  chart.setOption({ animationDurationUpdate: 0, series: [{ data: updatedNodes, links: updatedLinks }] });
}

function _clearHighlight(chart, nodes, links) {
  const updatedNodes = nodes.map(n => ({
    name: n.name,
    id: n.id || n.name,
    symbolSize: n.symbolSize,
    category: n.category,
    itemStyle: { ...n.itemStyle, opacity: 1 },
    label: { ...n.label, opacity: 1 },
    tooltip: n.tooltip,
  }));
  const updatedLinks = links.map(l => ({
    source: l.source,
    target: l.target,
    lineStyle: { ...l.lineStyle, opacity: 0.75 },
    tooltip: l.tooltip,
  }));
  chart.setOption({ animationDurationUpdate: 0, series: [{ data: updatedNodes, links: updatedLinks }] });
  _dagState.highlighted = null;
}

/* ── 瓶颈得分 → 颜色映射 ────────────────────────────── */
function _scoreToColor(score) {
  if (score == null) return null;
  if (score >= 8) return '#f44336';
  if (score >= 6) return '#ff9800';
  if (score >= 4) return '#ffc107';
  return '#4caf50';
}

const SCORE_LEGEND = [
  { label: '瓶颈 8-10', color: '#f44336' },
  { label: '瓶颈 6-8',  color: '#ff9800' },
  { label: '瓶颈 4-6',  color: '#ffc107' },
  { label: '瓶颈 0-4',  color: '#4caf50' },
];

/* ── renderDAG: 分层产业链图谱 ────────────────────── */
export function renderDAG(chainData) {
  const dom = document.getElementById('chart-chain');
  if (!dom) return;

  const allNodes = chainData.nodes || [];
  if (allNodes.length === 0) return;

  const chart = getChart('chart-chain');
  if (!chart) return;

  // 重置交互状态
  _dagState.locked = false;
  _dagState.lockedNode = null;
  _dagState.highlighted = null;

  const totalNodes = allNodes.length;
  const scaleFactor = totalNodes > 25 ? 0.8 : 1;

  // 瓶颈得分映射
  const bnScoreMap = window.appState?.bottleneckScoreMap || {};

  // ── 拖拽状态：拖动时隐藏 tooltip ──────────────────
  let _isDragging = false;

  // ── 收集图例类别 ─────────────────────────────────
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
    const bnScore = bnScoreMap[n.name];

    // 节点大小：瓶颈按得分缩放，终端产品固定最大
    let size;
    if (n.layer === 0) {
      size = 48;
    } else if (isBn && bnScore != null) {
      size = 30 + bnScore * 2.5;
    } else {
      size = 28;
    }
    size = Math.round(size * scaleFactor);

    // 节点颜色：有瓶颈得分用梯度色，否则按 layer_type
    const scoreColor = isBn ? _scoreToColor(bnScore) : null;
    const nodeColor = scoreColor || p.color;
    const hasScore = isBn && bnScore != null;

    const ltLabel = LAYER_TYPE_LABELS[lt] || lt;
    const paramsStr = (n.key_parameters || []).length > 0
      ? `<br/><span style="color:#aaa">参数:</span> ${n.key_parameters.join(', ')}` : '';
    const funcStr = n.function
      ? `<br/><span style="color:#aaa">功能:</span> ${n.function}` : '';
    const bnBadge = hasScore
      ? ` <span style="color:${scoreColor};font-weight:700">★ ${bnScore.toFixed(1)}</span>`
      : (isBn ? ' <span style="color:#ee6666;font-weight:700">★ 瓶颈</span>' : '');
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
        color: nodeColor,
        borderColor: hasScore ? scoreColor : (isBn ? '#ff9999' : 'rgba(255,255,255,0.15)'),
        borderWidth: isBn ? 3 : 1,
        shadowBlur: isBn ? 16 : 0,
        shadowColor: hasScore ? scoreColor + '99' : (isBn ? 'rgba(238,102,102,0.6)' : 'transparent'),
        opacity: isBn ? 1 : 0.75,
      },
      label: {
        show: true,
        formatter: n.name.length > 7 ? n.name.slice(0, 6) + '…' : n.name,
        fontSize: Math.round((totalNodes > 20 ? 10 : 12) * scaleFactor),
        fontWeight: isBn ? 'bold' : (p.fontWeight || 'normal'),
        color: isBn ? '#fff' : (p.fontColor || '#1a1a1a'),
        backgroundColor: 'transparent',
        borderRadius: 3,
        padding: [2, 6],
        position: 'bottom',
        distance: 4,
      },
      tooltip: { formatter: () => _isDragging ? '' : tip },
      _layer: n.layer || 0,
    };
  });

  // ── 构建连线（连续宽度映射）─────────────────────────
  const linkCatSet = new Set();
  const links = (chainData.links || []).map(l => {
    const dep = l.dependency ?? 0.5;
    const alt = l.alternatives ?? 0;
    const cls = classifyLink(dep, alt);
    const style = LINK_STYLES[cls];
    linkCatSet.add(cls);

    const width = 1 + dep * 3;

    return {
      source: l.upstream,
      target: l.downstream,
      lineStyle: {
        width,
        color: style.color,
        type: style.type,
        curveness: 0.15,
        opacity: 0.75,
      },
      tooltip: {
        formatter: () => {
          if (_isDragging) return '';
          const depPct = (dep * 100).toFixed(0);
          return `${l.upstream} → ${l.downstream}<br/>`
            + `依赖度: <b>${depPct}%</b> | 替代方案: <b>${alt}</b><br/>`
            + `<span style="color:${style.color}">${style.label}</span>`;
        },
      },
    };
  });

  // ── 图例数据（类型 + 连线 + 瓶颈评分）──────────────
  const hasBottlenecks = Object.keys(bnScoreMap).length > 0;
  const legendData = [
    ...nodeCategories.map(c => c.name),
    ...[...linkCatSet].map(cls => LINK_STYLES[cls].label),
    ...(hasBottlenecks ? SCORE_LEGEND.map(s => s.label) : []),
  ];

  const categories = [
    ...nodeCategories,
    ...[...linkCatSet].map(cls => ({
      name: LINK_STYLES[cls].label,
      itemStyle: { color: LINK_STYLES[cls].color },
      symbol: 'rect',
      symbolSize: 10,
    })),
    ...(hasBottlenecks ? SCORE_LEGEND.map(s => ({
      name: s.label,
      itemStyle: { color: s.color },
      symbol: 'circle',
      symbolSize: 12,
    })) : []),
  ];

  // ── 分层布局：固定 X 按层级，Y 均匀分布 ──────────
  const maxLayer = Math.max(...allNodes.map(n => n.layer || 0), 1);
  const domW = dom.clientWidth || 800;
  const domH = dom.clientHeight || 500;
  const marginX = 100;
  const marginY = 40;
  const usableW = domW - marginX * 2;
  const usableH = domH - marginY * 2;

  const layerCounts = {};
  allNodes.forEach(n => {
    const layer = n.layer || 0;
    layerCounts[layer] = (layerCounts[layer] || 0) + 1;
  });

  const layerIndex = {};
  nodes.forEach(nd => {
    const layer = nd._layer || 0;
    if (!layerIndex[layer]) layerIndex[layer] = 0;
    const idx = layerIndex[layer]++;
    const total = layerCounts[layer] || 1;

    nd.x = marginX + (layer / maxLayer) * usableW;

    if (total <= 8) {
      const slotH = usableH / (total + 1);
      nd.y = marginY + slotH * (idx + 1);
    } else {
      const cols = 2;
      const col = idx % cols;
      const row = Math.floor(idx / cols);
      const rowCount = Math.ceil(total / cols);
      const slotH = usableH / (rowCount + 1);
      const colOffset = (col - 0.5) * 60;
      nd.x += colOffset;
      nd.y = marginY + slotH * (row + 1);
    }
  });

  // ── 层标签（graphic 组件）──────────────────────────
  const layerLabels = [];
  for (let layer = 0; layer <= maxLayer; layer++) {
    const x = marginX + (layer / maxLayer) * usableW;
    const ltForLayer = allNodes.find(n => (n.layer || 0) === layer)?.layer_type || '';
    const ltLabel = LAYER_TYPE_LABELS[ltForLayer] || `L${layer}`;
    layerLabels.push(
      { type: 'line', shape: { x1: x, y1: 8, x2: x, y2: domH - 8 }, style: { stroke: 'rgba(255,255,255,0.06)', lineWidth: 1, lineDash: [4, 4] }, silent: true, z: -1 },
      { type: 'text', style: { text: `L${layer} ${ltLabel}`, x: x, y: 14, fill: 'rgba(255,255,255,0.2)', fontSize: 10, textAlign: 'center' }, silent: true, z: -1 },
    );
  }

  chart.setOption({
    tooltip: {
      triggerOn: 'mousemove',
      enterable: false,
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
    graphic: layerLabels,
    animationDuration: 300,
    animationEasingUpdate: 'cubicOut',
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      data: nodes,
      links,
      categories,
      force: {
        initLayout: 'none',
        repulsion: totalNodes > 25 ? 100 : 140,
        gravity: 0,
        edgeLength: [80, 180],
        friction: 0.15,
        layoutAnimation: false,
      },
      lineStyle: { opacity: 0.75 },
      emphasis: {
        lineStyle: { width: 4 },
        itemStyle: { shadowBlur: 20, shadowColor: 'rgba(255,255,255,0.3)' },
      },
      edgeSymbol: ['none', 'arrow'],
      edgeSymbolSize: [0, 8],
      scaleLimit: { min: 0.4, max: 3 },
    }],
  }, true);

  setTimeout(() => chart.resize(), 50);

  // ── 拖拽时隐藏 tooltip ──────────────────────────────
  chart.off('mousedown');
  chart.off('mouseup');
  chart.off('globalout');
  chart.off('click');
  chart.off('dblclick');

  chart.on('mousedown', () => {
    _isDragging = true;
    chart.dispatchAction({ type: 'hideTip' });
  });
  chart.on('mouseup', () => { _isDragging = false; });
  chart.on('globalout', () => { _isDragging = false; });

  // ── 单击/双击区分 ──────────────────────────────────
  let _clickTimer = null;

  chart.on('click', params => {
    if (params.dataType !== 'node') return;
    if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; return; }
    _clickTimer = setTimeout(() => {
      _clickTimer = null;
      const name = params.data.name;
      if (_dagState.locked) return;
      if (_dagState.highlighted === name) {
        _clearHighlight(chart, nodes, links);
      } else {
        const connected = _getConnectedNodes(name, chainData.links || []);
        _applyHighlight(chart, nodes, links, connected);
        _dagState.highlighted = name;
      }
    }, 280);
  });

  chart.on('dblclick', params => {
    if (params.dataType !== 'node') return;
    if (_clickTimer) { clearTimeout(_clickTimer); _clickTimer = null; }
    const name = params.data.name;
    const connected = _getConnectedNodes(name, chainData.links || []);
    _applyHighlight(chart, nodes, links, connected);
    _dagState.locked = true;
    _dagState.lockedNode = name;
    _dagState.highlighted = name;
    _showNodeInfoPanel(params.data, allNodes);
  });

  // 点击空白区域：解除锁定/高亮
  chart.getZr().off('click');
  chart.getZr().on('click', e => {
    if (!e.target) {
      if (_dagState.locked || _dagState.highlighted) {
        _clearHighlight(chart, nodes, links);
        _dagState.locked = false;
        _dagState.lockedNode = null;
      }
      const panel = document.getElementById('node-info-panel');
      if (panel) panel.style.display = 'none';
    }
  });

  // 阻止 roam 的 dblclick 缩放（仅在节点上触发时）
  chart.getZr().off('dblclick');
  chart.getZr().on('dblclick', e => {
    if (e.target) {
      if (e.event) e.event.preventDefault();
      e.stop && e.stop();
    }
  });
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
    title: undefined,
    radar: undefined,
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
  }, true);

  setTimeout(() => chart.resize(), 50);

  // Click on bar → show radar for that bottleneck
  chart.off('click');
  chart.on('click', params => {
    const idx = params.dataIndex;
    const report = sorted[idx];
    if (report) {
      if (typeof window._onBottleneckBarClick === 'function') {
        window._onBottleneckBarClick(idx, report);
      } else {
        renderRadar(report);
      }
    }
  });

  _hideBottleneckCompanyPanel();
}

/* ── 瓶颈代表企业面板 ──────────────────────────────────── */
function _showBottleneckCompanyPanel(companies, nodeName) {
  const panel = document.getElementById('bottleneck-company-panel');
  if (!panel) return;

  const filtered = _filterCompaniesByMarket(companies);

  let html = `<div class="nip-header">
    <span class="nip-title">${_esc(nodeName)}</span>
    <span class="nip-type">代表企业</span>
  </div>`;

  if (filtered.length === 0) {
    html += `<div class="nip-empty">该环节暂无代表企业数据</div>`;
  } else {
    html += `<table class="nip-table"><thead><tr><th>企业名称</th><th>股票代码</th></tr></thead><tbody>`;
    for (const c of filtered) {
      const name = _esc(c.name || '');
      const code = c.code ? `<span class="nip-code">${_esc(c.code)}</span>` : '-';
      html += `<tr><td>${name}</td><td>${code}</td></tr>`;
    }
    html += `</tbody></table>`;
  }

  panel.innerHTML = html;
  panel.style.display = 'block';
}

function _hideBottleneckCompanyPanel() {
  const panel = document.getElementById('bottleneck-company-panel');
  if (panel) panel.style.display = 'none';
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

  // 查找该节点的代表性企业并显示面板
  const chainData = window.appState.results?.decompose;
  const node = chainData?.nodes?.find(n => n.name === name);
  const companies = node?.representative_companies || [];
  _showBottleneckCompanyPanel(companies, name);

  chart.setOption({
    tooltip: {
      formatter: params => {
        if (!params.value) return '';
        return dims.map((d, i) =>
          `${d.label}: <b>${params.value[i]}</b>`
        ).join('<br/>');
      },
    },
    title: {
      text: name,
      subtext: '',
      left: 'center',
      top: 8,
      textStyle: { color: '#e0e0e0', fontSize: 15, fontWeight: 600 },
      subtextStyle: { color: '#999', fontSize: 11, lineHeight: 18 },
    },
    radar: {
      indicator: dims.map((d, i) => ({
        name: `${d.label}\n${values[i]}`,
        max: 10,
      })),
      shape: 'polygon',
      center: ['50%', '55%'],
      radius: '60%',
      axisName: { color: '#ccc', fontSize: 12 },
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
        label: {
          show: true,
          formatter: params => params.value,
          color: '#ccc',
          fontSize: 11,
        },
      }],
    }],
  }, true);

  // Click radar to go back to bar chart
  chart.off('click');
  chart.on('click', () => {
    const reports = window.appState.results.bottleneck;
    if (reports) {
      if (typeof window._onRadarBackClick === 'function') {
        window._onRadarBackClick();
      }
      renderBottleneckBars(reports);
    }
  });
}

/* ── renderCompareRadar: 供应商对比雷达图 ──────────── */
const COMPARE_PALETTE = ['#5470c6', '#91cc75', '#fac858', '#ee6666'];

export function renderCompareRadar(scorecards) {
  const dom = document.getElementById('compare-radar');
  if (!dom) return;

  let chart = echarts.getInstanceByDom(dom);
  if (!chart) {
    chart = echarts.init(dom, 'dark');
    window.addEventListener('resize', () => chart.resize());
  }

  const dims = [
    { key: 'position',  label: '市场地位', field: 'market_position' },
    { key: 'customer',  label: '客户验证', field: 'customer_validation' },
    { key: 'capacity',  label: '产能状况', field: 'capacity_status' },
    { key: 'financial', label: '财务健康', field: 'financial_health' },
    { key: 'valuation', label: '估值水平', field: 'valuation' },
  ];

  const series = scorecards.map((sc, i) => {
    const name = sc.supplier?.name || sc.company_name || `公司${i + 1}`;
    const scores = sc.dimension_scores || {};
    const values = dims.map(d => scores[d.key] ?? sc[d.field] ?? 0);
    const color = COMPARE_PALETTE[i % COMPARE_PALETTE.length];

    return {
      value: values,
      name,
      areaStyle: { color: color + '30' },
      lineStyle: { color, width: 2 },
      itemStyle: { color },
      label: {
        show: scorecards.length <= 2,
        formatter: params => params.value,
        color: '#ccc',
        fontSize: 10,
      },
    };
  });

  chart.setOption({
    tooltip: {
      formatter: params => {
        if (!params.value) return '';
        return `<b>${params.name}</b><br/>` +
          dims.map((d, i) => `${d.label}: <b>${params.value[i]}</b>`).join('<br/>');
      },
    },
    legend: {
      data: series.map(s => s.name),
      bottom: 8,
      textStyle: { color: '#bbb', fontSize: 12 },
    },
    radar: {
      indicator: dims.map(d => ({ name: d.label, max: 10 })),
      shape: 'polygon',
      center: ['50%', '45%'],
      radius: '55%',
      axisName: { color: '#ccc', fontSize: 12 },
      splitArea: { areaStyle: { color: ['rgba(59,107,204,0.03)', 'rgba(59,107,204,0.08)'] } },
      splitLine: { lineStyle: { color: '#444' } },
    },
    series: [{ type: 'radar', data: series }],
  }, true);
}

/* ── renderMiniRadar: 入围企业 mini 雷达图 ───────────── */
const SUPPLIER_DIMS = [
  { key: 'position',  label: '地位', field: 'market_position' },
  { key: 'customer',  label: '客户', field: 'customer_validation' },
  { key: 'capacity',  label: '产能', field: 'capacity_status' },
  { key: 'financial', label: '财务', field: 'financial_health' },
  { key: 'valuation', label: '估值', field: 'valuation' },
];

export function renderMiniRadar(dom, scorecard) {
  if (!dom) return;
  let chart = echarts.getInstanceByDom(dom);
  if (!chart) {
    chart = echarts.init(dom);
  }
  const scores = scorecard.dimension_scores || {};
  const values = SUPPLIER_DIMS.map(d => scores[d.key] ?? scorecard[d.field] ?? 0);

  chart.setOption({
    animation: false,
    radar: {
      indicator: SUPPLIER_DIMS.map(d => ({ name: d.label, max: 10 })),
      shape: 'polygon',
      center: ['50%', '50%'],
      radius: '70%',
      axisName: { color: '#666', fontSize: 9 },
      splitNumber: 2,
      splitArea: { areaStyle: { color: ['rgba(84,112,198,0.03)', 'rgba(84,112,198,0.08)'] } },
      splitLine: { lineStyle: { color: '#ddd' } },
      axisLine: { lineStyle: { color: '#ccc' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: values,
        areaStyle: { color: 'rgba(84,112,198,0.35)' },
        lineStyle: { color: '#5470c6', width: 1.5 },
        itemStyle: { color: '#5470c6' },
        symbol: 'none',
      }],
    }],
  }, true);
}

/* ── renderDetailRadar: 详情抽屉 250px 雷达图 ─────────── */
export function renderDetailRadar(dom, scorecard) {
  if (!dom) return;
  let chart = echarts.getInstanceByDom(dom);
  if (!chart) {
    chart = echarts.init(dom);
  }
  const scores = scorecard.dimension_scores || {};
  const values = SUPPLIER_DIMS.map(d => scores[d.key] ?? scorecard[d.field] ?? 0);
  const name = scorecard.supplier?.name || '';

  chart.setOption({
    tooltip: {
      formatter: params => {
        if (!params.value) return '';
        return SUPPLIER_DIMS.map((d, i) => `${d.label}: <b>${params.value[i]}</b>`).join('<br/>');
      },
    },
    radar: {
      indicator: SUPPLIER_DIMS.map((d, i) => ({
        name: `${d.label}\n${values[i]}`,
        max: 10,
      })),
      shape: 'polygon',
      center: ['50%', '55%'],
      radius: '60%',
      axisName: { color: '#555', fontSize: 11 },
      splitArea: { areaStyle: { color: ['rgba(84,112,198,0.03)', 'rgba(84,112,198,0.08)'] } },
      splitLine: { lineStyle: { color: '#ddd' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: values,
        name,
        areaStyle: { color: 'rgba(84,112,198,0.35)' },
        lineStyle: { color: '#5470c6', width: 2 },
        itemStyle: { color: '#5470c6' },
      }],
    }],
  }, true);
}

/* ── renderAiScoreBar: 详情抽屉 AI 评分柱状图 ─────────── */
export function renderAiScoreBar(dom, validations) {
  if (!dom || !validations || validations.length === 0) return;
  let chart = echarts.getInstanceByDom(dom);
  if (!chart) {
    chart = echarts.init(dom);
  }

  const models = validations.map(v => (v.model_name || '').split('/').pop());
  const scores = validations.map(v => v.score ?? 5);

  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 80, right: 20, top: 10, bottom: 24 },
    xAxis: { type: 'value', max: 10, splitLine: { lineStyle: { color: '#eee' } } },
    yAxis: {
      type: 'category', data: models,
      axisLabel: { color: '#555', fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: scores.map(s => ({
        value: s,
        itemStyle: {
          color: s >= 7.5 ? '#4caf50' : (s >= 5 ? '#ffc107' : '#f44336'),
        },
      })),
      barWidth: 16,
      label: { show: true, position: 'right', color: '#555', fontSize: 11, formatter: '{c}' },
    }],
  }, true);
}
