/**
 * charts.js — ECharts/D3 wrappers for 3 supply-chain graph views,
 *              plus bar chart, radar chart, and mini-radar helpers.
 *
 *  Graph views:  1) ECharts Force  2) ECharts Radial Tree  3) D3 Force
 *  Each view has its own color config persisted to localStorage.
 */

/* ══════════════════════════════════════════════════════════════
   Color config — per-chart, persisted in localStorage
   ══════════════════════════════════════════════════════════════ */
const DEFAULT_COLORS = {
  node: {
    end_product: '#7c3aed', assembly: '#2563eb', component: '#0891b2',
    sub_component: '#059669', material: '#84cc16', raw_material: '#9ca3af', equipment: '#d97706',
  },
  dep: { critical: '#dc2626', high: '#d97706', medium: '#0ea5e9', low: '#c7ced6' },
  bottleneck: '#dc2626',
};

const LS_KEY_PREFIX = 'bh_chart_colors_';
const chartColors = { force: null, tree: null, d3force: null };

function _loadColors(chartKey) {
  if (chartColors[chartKey]) return chartColors[chartKey];
  try {
    const raw = localStorage.getItem(LS_KEY_PREFIX + chartKey);
    if (raw) { chartColors[chartKey] = JSON.parse(raw); return chartColors[chartKey]; }
  } catch {}
  chartColors[chartKey] = JSON.parse(JSON.stringify(DEFAULT_COLORS));
  return chartColors[chartKey];
}
function _saveColors(chartKey) {
  try { localStorage.setItem(LS_KEY_PREFIX + chartKey, JSON.stringify(chartColors[chartKey])); } catch {}
}

/* ── color helpers (read from a specific chart's config) ── */
function depColor(d, cfg) {
  if (d >= 0.9) return cfg.dep.critical;
  if (d >= 0.7) return cfg.dep.high;
  if (d >= 0.4) return cfg.dep.medium;
  return cfg.dep.low;
}
function depWidth(d) { return d >= 0.9 ? 2.8 : d >= 0.7 ? 1.8 : d >= 0.4 ? 1.0 : 0.4; }
function depOpacity(d) { return d >= 0.9 ? 0.85 : d >= 0.7 ? 0.5 : d >= 0.4 ? 0.3 : 0.15; }

/* ══════════════════════════════════════════════════════════════ */

const COLORS = {
  accent: '#3b6bcc', success: '#2d8a5e', warning: '#b58a1c', danger: '#c44832', muted: '#7a7d82',
};

const LAYER_LABELS = {
  end_product:'终端产品', assembly:'总成/组件', component:'零部件',
  sub_component:'子部件', material:'材料', raw_material:'原材料', equipment:'设备',
};
const DEP_LABELS = { critical:'关键 ≥0.9', high:'高 0.7–0.9', medium:'中 0.4–0.7', low:'低 <0.4' };

/* ── ECharts instance management ─────────────────────── */
const instances = new Map();

function getChart(containerId) {
  let inst = instances.get(containerId);
  const dom = document.getElementById(containerId);
  if (!dom) return null;
  if (inst) {
    try {
      if (inst.isDisposed() || inst.getDom() !== dom || !dom.querySelector('canvas, svg')) {
        try { inst.dispose(); } catch {}
        inst = null; instances.delete(containerId);
      }
    } catch { inst = null; instances.delete(containerId); }
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
  instances.forEach(inst => { try { inst.dispose(); } catch {} });
  instances.clear();
}

export function resizeAll() {
  instances.forEach(inst => { try { inst.resize(); } catch {} });
}
window.addEventListener('resize', () => resizeAll());

/* ── Fullscreen ──────────────────────────────────────── */
export function initChartFullscreen() {
  const btn = document.getElementById('btn-chart-fullscreen');
  const card = document.getElementById('card-chain');
  if (!btn || !card) return;
  btn.addEventListener('click', () => {
    if (!document.fullscreenElement) card.requestFullscreen().catch(() => {});
    else document.exitFullscreen();
  });
  document.addEventListener('fullscreenchange', () => {
    const isFs = !!document.fullscreenElement;
    const label = btn.querySelector('.fullscreen-label');
    if (label) label.textContent = isFs ? '还原' : '全屏';
    btn.querySelector('.fullscreen-expand').style.display = isFs ? 'none' : '';
    btn.querySelector('.fullscreen-shrink').style.display = isFs ? '' : 'none';
    setTimeout(() => { resizeAll(); _resizeActiveD3(); }, 100);
    setTimeout(() => { resizeAll(); _resizeActiveD3(); }, 400);
  });
}

/* ── Node info panel ─────────────────────────────────── */
const LAYER_TYPE_LABELS = {
  end_product:'终端产品', assembly:'总成/组件', component:'零部件',
  sub_component:'子组件', material:'材料', raw_material:'原材料', equipment:'设备',
};

function _showNodeInfoPanel(nodeData, allNodes) {
  const panel = document.getElementById('node-info-panel');
  if (!panel) return;
  const node = allNodes.find(n => n.name === nodeData.name);
  if (!node) { panel.style.display = 'none'; return; }
  const companies = _filterCompaniesByMarket(node.representative_companies || []);
  const ltLabel = LAYER_TYPE_LABELS[node.layer_type || 'component'] || node.layer_type;
  let html = `<div class="nip-header"><span class="nip-title">${_esc(node.name)}</span><span class="nip-type">${ltLabel} · L${node.layer}</span><button class="nip-close" title="关闭">&times;</button></div>`;
  if (node.description) html += `<div class="nip-desc">${_esc(node.description)}</div>`;
  if (companies.length > 0) {
    html += `<div class="nip-section-title">代表性企业</div><ul class="nip-companies">`;
    for (const c of companies) html += `<li>${_esc(c.name||'')} ${c.code?`<span class="nip-code">${_esc(c.code)}</span>`:''}</li>`;
    html += `</ul>`;
  } else html += `<div class="nip-empty">暂无代表性企业数据</div>`;
  panel.innerHTML = html;
  panel.style.display = 'block';
  panel.querySelector('.nip-close').addEventListener('click', () => { panel.style.display = 'none'; });
}
function _esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
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

/* ══════════════════════════════════════════════════════════════
   Tab switching for 3 chart views
   ══════════════════════════════════════════════════════════════ */
let _activeChartTab = 'force';
let _chartRendered = { force: false, tree: false, d3force: false };
let _d3State = null;

/* ── Wizard chart state ── */
let _wizActiveTab = 'force';
let _wizRendered = { force: false, tree: false, d3force: false };

export function initChainTabs() {
  const bar = document.getElementById('chain-tab-bar');
  if (!bar) return;
  bar.addEventListener('click', e => {
    const btn = e.target.closest('.chain-tab');
    if (!btn) return;
    const chart = btn.dataset.chart;
    if (chart === _activeChartTab) return;
    _activeChartTab = chart;
    bar.querySelectorAll('.chain-tab').forEach(b => b.classList.toggle('active', b === btn));
    document.querySelectorAll('.chain-chart-panel').forEach(p => p.classList.toggle('active', p.dataset.chart === chart));
    _renderActiveTab();
  });
}

function _renderActiveTab() {
  const chainData = window.appState?.results?.decompose;
  if (!chainData) return;
  if (_activeChartTab === 'force' && !_chartRendered.force) {
    _renderEchartsForce(chainData);
  } else if (_activeChartTab === 'force') {
    try { instances.get('chart-chain-force')?.resize(); } catch {}
  }
  if (_activeChartTab === 'tree' && !_chartRendered.tree) {
    _renderEchartsTree(chainData);
  } else if (_activeChartTab === 'tree') {
    try { instances.get('chart-chain-tree')?.resize(); } catch {}
  }
  if (_activeChartTab === 'd3force' && !_chartRendered.d3force) {
    _renderD3Force(chainData);
  }
}

function _resizeActiveD3() {
  if (_activeChartTab !== 'd3force' || !_chartRendered.d3force) return;
  _chartRendered.d3force = false;
  _d3State = null;
  const svgEl = document.getElementById('chart-chain-d3');
  if (svgEl) svgEl.innerHTML = '';
  _renderActiveTab();
}

function _invalidateAll() {
  _chartRendered = { force: false, tree: false, d3force: false };
  _d3State = null;
}

/* ══════════════════════════════════════════════════════════════
   Legend with color pickers (per chart)
   ══════════════════════════════════════════════════════════════ */
function _addLegend(panelDom, chartKey) {
  let ex = panelDom.querySelector('.chain-legend');
  if (ex) ex.remove();
  const cfg = _loadColors(chartKey);
  const legend = document.createElement('div');
  legend.className = 'chain-legend';

  let html = '<div class="chain-legend-title">图例 <span class="chain-legend-hint">点击色块调色</span></div>';
  html += '<div class="legend-section"><div class="legend-title">节点层级</div>';
  for (const [key, label] of Object.entries(LAYER_LABELS)) {
    const c = cfg.node[key];
    html += `<div class="legend-item"><div class="legend-swatch"><div class="legend-dot" style="background:${c}"></div><input type="color" value="${c}" data-type="node" data-key="${key}"></div>${label}</div>`;
  }
  html += `<div class="legend-item"><div class="legend-swatch"><div class="legend-bn-dot" style="border-color:${cfg.bottleneck};background:${cfg.bottleneck}33"></div><input type="color" value="${cfg.bottleneck}" data-type="bottleneck" data-key="bottleneck"></div><span style="color:${cfg.bottleneck}">瓶颈环节</span></div>`;
  html += '</div><div class="legend-section"><div class="legend-title">依赖强度（连线）</div>';
  const depWidths = { critical: 2.8, high: 2, medium: 1, low: 0.5 };
  for (const [key, label] of Object.entries(DEP_LABELS)) {
    const c = cfg.dep[key]; const w = depWidths[key];
    html += `<div class="legend-item"><div class="legend-swatch"><div class="legend-line-swatch"><div class="legend-line-bar" style="border-top:${w}px solid ${c}"></div></div><input type="color" value="${c}" data-type="dep" data-key="${key}"></div><span style="color:${c}">${label}</span></div>`;
  }
  html += '</div>';
  legend.innerHTML = html;

  legend.querySelectorAll('input[type="color"]').forEach(input => {
    input.addEventListener('input', e => {
      const t = e.target.dataset.type, k = e.target.dataset.key, v = e.target.value;
      if (t === 'node') { cfg.node[k] = v; e.target.previousElementSibling.style.background = v; }
      else if (t === 'dep') { cfg.dep[k] = v; const bar = e.target.previousElementSibling.querySelector('.legend-line-bar'); if (bar) bar.style.borderTopColor = v; const sp = e.target.closest('.legend-item').querySelector('span'); if (sp) sp.style.color = v; }
      else if (t === 'bottleneck') { cfg.bottleneck = v; const dot = e.target.previousElementSibling; dot.style.borderColor = v; dot.style.background = v + '33'; const sp = e.target.closest('.legend-item').querySelector('span'); if (sp) sp.style.color = v; }
    });
    input.addEventListener('change', () => {
      _saveColors(chartKey);
      const isWiz = !!panelDom.closest('#wiz-chart-panels');
      if (isWiz) {
        _wizRendered[chartKey] = false;
        if (_wizActiveTab === chartKey) {
          if (chartKey === 'd3force') { const s = document.getElementById('wiz-chart-d3'); if (s) s.innerHTML = ''; }
          else { const cid = chartKey === 'force' ? 'wiz-chart-force' : 'wiz-chart-tree'; const inst = instances.get(cid); if (inst) { try { inst.dispose(); } catch {} instances.delete(cid); } }
          _renderWizActiveTab();
        }
      } else {
        _chartRendered[chartKey] = false;
        if (_activeChartTab === chartKey) {
          if (chartKey === 'd3force') { document.getElementById('chart-chain-d3').innerHTML = ''; _d3State = null; }
          else { const inst = instances.get(chartKey === 'force' ? 'chart-chain-force' : 'chart-chain-tree'); if (inst) { try { inst.dispose(); } catch {} instances.delete(chartKey === 'force' ? 'chart-chain-force' : 'chart-chain-tree'); } }
          _renderActiveTab();
        }
      }
    });
  });
  panelDom.appendChild(legend);
}

/* ── Adjacency helper ── */
function _buildAdj(chainData) {
  const adj = {};
  (chainData.nodes || []).forEach(n => adj[n.name] = new Set());
  (chainData.links || []).forEach(l => {
    if (adj[l.upstream]) adj[l.upstream].add(l.downstream);
    if (adj[l.downstream]) adj[l.downstream].add(l.upstream);
  });
  return adj;
}

/* ══════════════════════════════════════════════════════════════
   renderDAG — main entry point (replaces the old single-chart)
   ══════════════════════════════════════════════════════════════ */
export function renderDAG(chainData) {
  _invalidateAll();
  _renderActiveTab();
}

/* ════════════════════════════════════════════════════════════
   1. ECharts Force
   ════════════════════════════════════════════════════════════ */
function _renderEchartsForce(chainData, containerId) {
  if (!containerId) containerId = 'chart-chain-force';
  const dom = document.getElementById(containerId);
  if (!dom) return;
  const isWiz = containerId.startsWith('wiz-');
  if (isWiz) _wizRendered.force = true; else _chartRendered.force = true;

  const cfg = _loadColors('force');
  const chart = getChart(containerId);
  if (!chart) return;

  const allNodes = chainData.nodes || [];
  const bnScoreMap = window.appState?.bottleneckScoreMap || {};
  const adj = _buildAdj(chainData);
  const maxLayer = Math.max(...allNodes.map(n => n.layer || 0), 1);
  const domW = dom.clientWidth || 800, domH = dom.clientHeight || 500;
  const layerCounts = {}, layerIdx = {};
  allNodes.forEach(n => { layerCounts[n.layer || 0] = (layerCounts[n.layer || 0] || 0) + 1; });

  const categories = Object.entries(LAYER_LABELS).map(([k, v]) => ({ name: v, itemStyle: { color: cfg.node[k] } }));

  // 横向分列初始布局：按层分配 x 列，y 随机散布，让 force 引擎通过弹力自然整理
  const mx = 80;
  function _hash(s) { let h = 0; for (let i = 0; i < s.length; i++) { h = ((h << 5) - h + s.charCodeAt(i)) | 0; } return h; }
  function _pr(seed) { let x = Math.sin(seed) * 10000; return x - Math.floor(x); }

  const ecNodes = allNodes.map(n => {
    const layer = n.layer || 0;
    if (!layerIdx[layer]) layerIdx[layer] = 0;
    const idx = layerIdx[layer]++;
    const total = layerCounts[layer] || 1;
    const isBn = !!n.is_bottleneck;
    const color = cfg.node[n.layer_type] || '#059669';
    const ltLabel = LAYER_TYPE_LABELS[n.layer_type] || n.layer_type || '';

    const h = _hash(n.name);
    // x: 按层均匀分列，加小量随机偏移
    const x = mx + (layer / maxLayer) * (domW - mx * 2) + (_pr(h + 1) - 0.5) * 60;
    // y: 在容器高度内随机散布
    const y = domH * 0.1 + _pr(h + 2) * domH * 0.8;

    return {
      id: n.name, name: n.name, x, y,
      symbolSize: layer === 0 ? 28 : (isBn ? 28 : 9),
      symbol: isBn ? 'diamond' : 'circle',
      category: LAYER_LABELS[n.layer_type] || '零部件',
      _isBn: isBn, _layer: layer,
      itemStyle: {
        color, borderColor: isBn ? cfg.bottleneck : 'rgba(0,0,0,.06)',
        borderWidth: isBn ? 3 : 0.5,
        shadowBlur: isBn ? 10 : 0, shadowColor: isBn ? cfg.bottleneck + '55' : 'transparent',
        opacity: isBn || layer === 0 ? 1 : 0.8,
      },
      label: {
        show: isBn || layer === 0,
        fontSize: layer === 0 ? 11 : (isBn ? 10 : 9),
        fontWeight: layer === 0 ? 'bold' : 'normal',
        color: '#1f2328', position: 'right', distance: 6,
        formatter: n.name.length > 12 ? n.name.slice(0, 11) + '…' : n.name,
      },
      emphasis: { label: { show: true, color: '#1f2328', fontSize: 11, fontWeight: 'bold', backgroundColor: 'rgba(255,255,255,.85)', padding: [2, 5], borderRadius: 3 } },
      tooltip: {
        formatter: '<b>' + n.name + '</b>'
          + (isBn ? ' <span style="color:' + cfg.bottleneck + '">★ ' + ((bnScoreMap[n.name] ?? '') + '') + '</span>' : '')
          + '<br/><span style="color:' + color + '">' + ltLabel + '</span> (L' + layer + ')'
          + '<br/>' + (n.description || ''),
      },
    };
  });

  const ecLinks = (chainData.links || []).map(l => ({
    source: l.upstream, target: l.downstream, _dep: l.dependency ?? 0.5,
    lineStyle: { width: depWidth(l.dependency ?? 0.5), color: depColor(l.dependency ?? 0.5, cfg), curveness: 0.15, opacity: depOpacity(l.dependency ?? 0.5) },
  }));

  chart.setOption({
    tooltip: { backgroundColor: 'rgba(255,255,255,.95)', borderColor: '#d0d7de', textStyle: { color: '#1f2328', fontSize: 12 }, confine: true },
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      data: ecNodes, links: ecLinks, categories,
      force: { initLayout: 'none', repulsion: 400, gravity: 0.01, edgeLength: [30, 120], friction: 0.2, layoutAnimation: true },
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: [0, 5],
      emphasis: { focus: 'adjacency', lineStyle: { width: 3, opacity: 0.9 } },
      scaleLimit: { min: 0.15, max: 6 },
    }],
  }, true);
  setTimeout(() => chart.resize(), 50);

  // Interactions
  function applyHL(nodeId) {
    const neighbors = adj[nodeId] || new Set();
    const hlSet = new Set([nodeId, ...neighbors]);
    ecNodes.forEach(nd => { nd.itemStyle.opacity = hlSet.has(nd.id) ? 1 : 0.06; nd.label.show = hlSet.has(nd.id); });
    ecLinks.forEach(lk => {
      const sId = typeof lk.source === 'object' ? lk.source.id || lk.source.name : lk.source;
      const tId = typeof lk.target === 'object' ? lk.target.id || lk.target.name : lk.target;
      const isHL = sId === nodeId || tId === nodeId;
      lk.lineStyle.opacity = isHL ? Math.max(depOpacity(lk._dep), 0.7) : 0.02;
      lk.lineStyle.width = isHL ? depWidth(lk._dep) + 1 : depWidth(lk._dep);
    });
    chart.setOption({ series: [{ data: ecNodes, links: ecLinks }] });
  }
  function clearHL() {
    selNode = null; locked = false;
    ecNodes.forEach(nd => { nd.itemStyle.opacity = (nd._isBn || nd._layer === 0) ? 1 : 0.8; nd.label.show = nd._isBn || nd._layer === 0; });
    ecLinks.forEach(lk => { lk.lineStyle.opacity = depOpacity(lk._dep); lk.lineStyle.width = depWidth(lk._dep); });
    chart.setOption({ series: [{ data: ecNodes, links: ecLinks }] });
  }

  let clickTimer = null, selNode = null, locked = false;
  chart.off('click'); chart.off('dblclick');
  chart.on('click', 'series.graph', params => {
    if (!params.data || !params.data.id) return;
    if (clickTimer) return;
    clickTimer = setTimeout(() => { clickTimer = null; if (locked && selNode !== params.data.id) return; selNode = params.data.id; locked = false; applyHL(params.data.id); }, 280);
  });
  chart.on('dblclick', 'series.graph', params => {
    if (!params.data || !params.data.id) return;
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    selNode = params.data.id; locked = true;
    applyHL(params.data.id);
    _showNodeInfoPanel(params.data, allNodes);
  });
  chart.getZr().off('click');
  chart.getZr().on('click', e => {
    if (!e.target) { if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; } clearHL(); const p = document.getElementById('node-info-panel'); if (p) p.style.display = 'none'; }
  });

  _addLegend(dom.closest('.chain-chart-panel'), 'force');
}

/* ════════════════════════════════════════════════════════════
   2. ECharts Radial Tree
   ════════════════════════════════════════════════════════════ */
function _renderEchartsTree(chainData, containerId) {
  if (!containerId) containerId = 'chart-chain-tree';
  const dom = document.getElementById(containerId);
  if (!dom) return;
  const isWiz = containerId.startsWith('wiz-');
  if (isWiz) _wizRendered.tree = true; else _chartRendered.tree = true;

  const cfg = _loadColors('tree');
  const chart = getChart(containerId);
  if (!chart) return;

  const allNodes = chainData.nodes || [];
  const nodeMap = {};
  allNodes.forEach(n => {
    nodeMap[n.name] = { name: n.name, value: n.description, children: [], _lt: n.layer_type, _bn: n.is_bottleneck, _score: (window.appState?.bottleneckScoreMap || {})[n.name], _layer: n.layer, _dep: 0 };
  });
  const downToUp = {};
  (chainData.links || []).forEach(l => {
    if (!downToUp[l.downstream]) downToUp[l.downstream] = [];
    downToUp[l.downstream].push({ name: l.upstream, dep: l.dependency ?? 0.5 });
  });

  const rootName = chainData.end_product || allNodes.find(n => n.layer === 0)?.name || allNodes[0]?.name;
  const root = nodeMap[rootName] || { name: rootName, children: [], _lt: 'end_product', _bn: false, _layer: 0, _dep: 0 };
  const attached = new Set([rootName]);
  const queue = [rootName];
  while (queue.length > 0) {
    const curr = queue.shift();
    const ups = downToUp[curr] || [];
    ups.sort((a, b) => ((nodeMap[b.name]?._bn ? 1 : 0) - (nodeMap[a.name]?._bn ? 1 : 0)));
    for (const u of ups) {
      if (attached.has(u.name) || !nodeMap[u.name]) continue;
      nodeMap[u.name]._dep = u.dep;
      nodeMap[curr].children.push(nodeMap[u.name]);
      attached.add(u.name); queue.push(u.name);
    }
  }
  allNodes.filter(n => n.is_bottleneck && !attached.has(n.name)).forEach(bn => {
    const parentLink = (chainData.links || []).find(l => l.upstream === bn.name && attached.has(l.downstream));
    if (parentLink && nodeMap[parentLink.downstream]) {
      nodeMap[bn.name]._dep = parentLink.dependency ?? 0.5;
      nodeMap[parentLink.downstream].children.push(nodeMap[bn.name]);
      attached.add(bn.name);
    }
  });

  // Build parent/children lookup for highlight
  const treeParent = {};
  const treeChildren = {};
  function buildLookup(node, parent) {
    if (parent) treeParent[node.name] = parent.name;
    treeChildren[node.name] = (node.children || []).map(c => c.name);
    (node.children || []).forEach(c => buildLookup(c, node));
  }
  buildLookup(root, null);

  // Collect all ancestors of a node
  function getAncestors(name) {
    const result = [];
    let cur = treeParent[name];
    while (cur) { result.push(cur); cur = treeParent[cur]; }
    return result;
  }

  function styleTree(node) {
    const lt = node._lt || 'component';
    const color = cfg.node[lt] || '#059669';
    const isBn = !!node._bn;
    const dep = node._dep || 0;
    const isLeaf = !node.children || node.children.length === 0;
    node.itemStyle = { color, borderColor: isBn ? cfg.bottleneck : color, borderWidth: isBn ? 3 : 1, shadowBlur: isBn ? 10 : 0, shadowColor: isBn ? cfg.bottleneck + '66' : 'transparent' };
    node.lineStyle = { color: depColor(dep, cfg), width: depWidth(dep), opacity: depOpacity(dep) };
    node.label = { show: isLeaf || isBn || node._layer === 0, fontSize: node._layer === 0 ? 14 : (isBn ? 10 : 7), fontWeight: (node._layer === 0 || isBn) ? 'bold' : 'normal', color: isBn ? cfg.bottleneck : '#1f2328', overflow: 'truncate', width: 70 };
    if (node.children) node.children.forEach(c => styleTree(c));
  }
  styleTree(root);

  chart.setOption({
    tooltip: {
      trigger: 'item', backgroundColor: 'rgba(255,255,255,.95)', borderColor: '#d0d7de', textStyle: { color: '#1f2328', fontSize: 12 }, confine: true,
      formatter: p => {
        if (!p.data) return '';
        const d = p.data, lt = d._lt || '', color = cfg.node[lt] || '#999';
        return '<b>' + d.name + '</b>' + (d._bn ? ' <span style="color:' + cfg.bottleneck + '">★ ' + (d._score || '') + '</span>' : '')
          + '<br/><span style="color:' + color + '">' + (LAYER_LABELS[lt] || '') + '</span>'
          + (d._dep ? '<br/>依赖度: ' + d._dep.toFixed(2) : '') + '<br/>' + (d.value || '');
      },
    },
    series: [{
      type: 'tree', data: [root], layout: 'radial', roam: true,
      symbol: (v, p) => p.data && p.data._bn ? 'diamond' : 'circle',
      symbolSize: (v, p) => { if (!p.data) return 5; if (p.data._layer === 0) return 28; if (p.data._bn) return 18; if (p.data.children && p.data.children.length > 3) return 10; return 5; },
      initialTreeDepth: -1, expandAndCollapse: false,
      emphasis: { disabled: true },
      animationDuration: 0,
    }],
  }, true);
  setTimeout(() => chart.resize(), 50);

  // Custom highlight: ancestors + one level children
  let _treeLocked = null;
  function _hlTreeNode(name) {
    const ancestors = getAncestors(name);
    const children = treeChildren[name] || [];
    const hlSet = new Set([name, ...ancestors, ...children]);
    function dim(node) {
      const isHL = hlSet.has(node.name);
      node.itemStyle.opacity = isHL ? 1 : 0.08;
      node.lineStyle.opacity = (hlSet.has(node.name) && treeParent[node.name] && hlSet.has(treeParent[node.name])) || (node._layer === 0 && isHL) ? 0.8 : 0.04;
      node.label.show = isHL;
      if (isHL) { node.label.fontSize = node._layer === 0 ? 14 : (node._bn ? 11 : 9); node.label.fontWeight = 'bold'; node.label.backgroundColor = 'rgba(255,255,255,.85)'; node.label.padding = [2, 5]; node.label.borderRadius = 3; }
      (node.children || []).forEach(c => dim(c));
    }
    dim(root);
    chart.setOption({ series: [{ data: [root] }] });
  }
  function _clrTreeHL() {
    _treeLocked = null;
    function restore(node) {
      const lt = node._lt || 'component';
      const isBn = !!node._bn;
      const isLeaf = !node.children || node.children.length === 0;
      node.itemStyle.opacity = 1;
      node.lineStyle.opacity = depOpacity(node._dep || 0);
      node.label.show = isLeaf || isBn || node._layer === 0;
      node.label.fontSize = node._layer === 0 ? 14 : (isBn ? 10 : 7);
      node.label.fontWeight = (node._layer === 0 || isBn) ? 'bold' : 'normal';
      node.label.backgroundColor = undefined;
      node.label.padding = undefined;
      node.label.borderRadius = undefined;
      (node.children || []).forEach(c => restore(c));
    }
    restore(root);
    chart.setOption({ series: [{ data: [root] }] });
  }

  chart.off('mouseover'); chart.off('mouseout'); chart.off('click'); chart.off('dblclick');

  chart.on('mouseover', 'series.tree', params => {
    if (_treeLocked || !params.data) return;
    _hlTreeNode(params.data.name);
  });
  chart.on('mouseout', 'series.tree', () => {
    if (_treeLocked) return;
    _clrTreeHL();
  });
  chart.on('dblclick', 'series.tree', params => {
    if (!params.data) return;
    if (_treeLocked === params.data.name) { _treeLocked = null; _clrTreeHL(); return; }
    _treeLocked = params.data.name;
    _hlTreeNode(params.data.name);
  });
  chart.getZr().off('click');
  chart.getZr().on('click', e => {
    if (!e.target && _treeLocked) { _clrTreeHL(); }
  });

  _addLegend(dom.closest('.chain-chart-panel'), 'tree');
}

/* ════════════════════════════════════════════════════════════
   3. D3 Force
   ════════════════════════════════════════════════════════════ */
function _renderD3Force(chainData, svgId) {
  if (!svgId) svgId = 'chart-chain-d3';
  const svgEl = document.getElementById(svgId);
  if (!svgEl) return;
  svgEl.innerHTML = '';
  const isWiz = svgId.startsWith('wiz-');
  if (isWiz) _wizRendered.d3force = true; else _chartRendered.d3force = true;

  const cfg = _loadColors('d3force');
  const allNodes = chainData.nodes || [];
  const w = svgEl.clientWidth || svgEl.parentElement?.clientWidth || 800;
  const h = svgEl.clientHeight || svgEl.parentElement?.clientHeight || 500;

  const svg = d3.select(svgEl).attr('viewBox', [0, 0, w, h]);
  const defs = svg.append('defs');
  [['critical', cfg.dep.critical], ['high', cfg.dep.high], ['medium', cfg.dep.medium], ['low', cfg.dep.low]].forEach(([cls, c]) => {
    defs.append('marker').attr('id', 'da-' + cls).attr('viewBox', '0 -3 6 6').attr('refX', 12).attr('refY', 0)
      .attr('markerWidth', 5).attr('markerHeight', 5).attr('orient', 'auto')
      .append('path').attr('d', 'M0,-3L6,0L0,3').attr('fill', c);
  });
  function arrowId(dep) { return dep >= 0.9 ? 'url(#da-critical)' : dep >= 0.7 ? 'url(#da-high)' : dep >= 0.4 ? 'url(#da-medium)' : 'url(#da-low)'; }

  const maxLayer = Math.max(...allNodes.map(n => n.layer || 0), 1);
  const nodeSet = new Set();
  const nodes = [];
  allNodes.forEach(n => {
    if (nodeSet.has(n.name)) return; nodeSet.add(n.name);
    nodes.push({ id: n.name, layer: n.layer || 0, layerType: n.layer_type, isBn: !!n.is_bottleneck, desc: n.description, bnScore: (window.appState?.bottleneckScoreMap || {})[n.name], r: (n.layer || 0) === 0 ? 18 : (n.is_bottleneck ? 11 : 3.5) });
  });
  const edgeSet = new Set(), links = [];
  (chainData.links || []).forEach(l => {
    const key = l.upstream + '->' + l.downstream;
    if (edgeSet.has(key) || !nodeSet.has(l.upstream) || !nodeSet.has(l.downstream)) return;
    edgeSet.add(key);
    links.push({ source: l.upstream, target: l.downstream, dep: l.dependency ?? 0.5 });
  });

  const adjList = new Map();
  nodes.forEach(n => adjList.set(n.id, []));
  links.forEach((l, i) => {
    const sId = typeof l.source === 'object' ? l.source.id : l.source;
    const tId = typeof l.target === 'object' ? l.target.id : l.target;
    adjList.get(sId)?.push({ neighbor: tId, linkIdx: i });
    adjList.get(tId)?.push({ neighbor: sId, linkIdx: i });
  });
  function bfs4(startId) {
    const vn = new Set([startId]), vl = new Set();
    let frontier = [startId];
    for (let d = 0; d < 2; d++) {
      const next = [];
      for (const nid of frontier) for (const { neighbor, linkIdx } of (adjList.get(nid) || [])) { vl.add(linkIdx); if (!vn.has(neighbor)) { vn.add(neighbor); next.push(neighbor); } }
      frontier = next; if (!frontier.length) break;
    }
    return { nodes: vn, links: vl };
  }

  const sim = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id(d => d.id).distance(25).strength(0.08))
    .force('y', d3.forceY(h / 2).strength(0.03))
    .force('collide', d3.forceCollide(d => d.r + 2)).stop();
  // Layer x-positions: from layer 2 onward, each gap is 50% wider than previous
  const layerX = {};
  const baseGap = (w - 120) / (maxLayer || 1);
  let cumX = 60;
  for (let L = 0; L <= maxLayer; L++) {
    layerX[L] = cumX;
    if (L >= 1) {
      const multiplier = L >= 2 ? Math.pow(1.5, L - 1) : 1;
      cumX += baseGap * multiplier;
    } else {
      cumX += baseGap;
    }
  }
  // Normalize to fit within available width
  const maxX = layerX[maxLayer] || 1;
  const availW = w - 120;
  for (let L = 0; L <= maxLayer; L++) {
    layerX[L] = 60 + ((layerX[L] - 60) / (maxX - 60 || 1)) * availW;
  }

  nodes.forEach(n => { n.fx = layerX[n.layer] || 60; n.x = n.fx; n.y = h / 2 + (Math.random() - 0.5) * h * 0.6; });
  for (let i = 0; i < 300; i++) sim.tick();

  const g = svg.append('g');
  svg.call(d3.zoom().scaleExtent([0.08, 6]).on('zoom', e => g.attr('transform', e.transform)));

  const link = g.append('g').selectAll('line').data(links).join('line')
    .attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y)
    .attr('stroke', d => depColor(d.dep, cfg)).attr('stroke-width', d => depWidth(d.dep))
    .attr('stroke-opacity', d => depOpacity(d.dep)).attr('marker-end', d => arrowId(d.dep));

  const nodeG = g.append('g');
  const circles = nodeG.selectAll('circle').data(nodes.filter(d => !d.isBn)).join('circle')
    .attr('cx', d => d.x).attr('cy', d => d.y).attr('r', d => d.r)
    .attr('fill', d => cfg.node[d.layerType] || '#059669').attr('stroke', 'rgba(0,0,0,.08)').attr('stroke-width', 0.5)
    .attr('opacity', d => d.layer === 0 ? 1 : 0.75).attr('cursor', 'pointer');
  const diamonds = nodeG.selectAll('path.bn').data(nodes.filter(d => d.isBn)).join('path').classed('bn', true)
    .attr('transform', d => 'translate(' + d.x + ',' + d.y + ')')
    .attr('d', d => { const s = d.r * 1.3; return 'M0,' + (-s) + ' L' + s + ',0 L0,' + s + ' L' + (-s) + ',0Z'; })
    .attr('fill', d => cfg.node[d.layerType] || '#059669').attr('stroke', cfg.bottleneck).attr('stroke-width', 2.5)
    .attr('opacity', 1).attr('cursor', 'pointer');

  const allLabels = g.append('g').selectAll('text').data(nodes).join('text')
    .text(d => d.id.length > 14 ? d.id.slice(0, 13) + '…' : d.id)
    .attr('x', d => d.x).attr('y', d => d.y).attr('dx', d => d.r + 5).attr('dy', 3)
    .attr('fill', '#1f2328').attr('font-size', d => d.layer === 0 ? 12 : (d.isBn ? 10 : 9))
    .attr('font-weight', d => (d.layer === 0 || d.isBn) ? 'bold' : 'normal')
    .attr('pointer-events', 'none').attr('opacity', d => (d.isBn || d.layer === 0) ? 1 : 0);

  let tooltipEl = document.getElementById('d3-chain-tip');
  if (!tooltipEl) { tooltipEl = document.createElement('div'); tooltipEl.id = 'd3-chain-tip'; tooltipEl.style.cssText = 'position:fixed;display:none;background:rgba(255,255,255,.95);border:1px solid #d0d7de;color:#1f2328;padding:8px 12px;border-radius:6px;font-size:12px;pointer-events:none;z-index:1000;max-width:320px;box-shadow:0 2px 12px rgba(0,0,0,.1)'; document.body.appendChild(tooltipEl); }

  function showTip(e, d) {
    const c = cfg.node[d.layerType] || '#999';
    tooltipEl.innerHTML = '<b>' + d.id + '</b>' + (d.isBn ? ' <span style="color:' + cfg.bottleneck + '">★ ' + (d.bnScore || '') + '</span>' : '')
      + '<br/><span style="color:' + c + '">' + (LAYER_LABELS[d.layerType] || '') + '</span> (L' + d.layer + ')' + '<br/>' + (d.desc || '')
      + '<br/><span style="color:#656d76;font-size:11px">点击锁定2层关联，再点空白取消</span>';
    tooltipEl.style.display = 'block'; tooltipEl.style.left = (e.clientX + 14) + 'px'; tooltipEl.style.top = (e.clientY - 10) + 'px';
  }
  function moveTip(e) { tooltipEl.style.left = (e.clientX + 14) + 'px'; tooltipEl.style.top = (e.clientY - 10) + 'px'; }
  function hideTip() { tooltipEl.style.display = 'none'; }

  let lockedNode = null;
  function hl4(d) {
    const { nodes: hn, links: hl } = bfs4(d.id);
    circles.attr('opacity', n => hn.has(n.id) ? 0.9 : 0.04);
    diamonds.attr('opacity', n => hn.has(n.id) ? 1 : 0.04);
    link.attr('stroke-opacity', (l, i) => hl.has(i) ? Math.max(depOpacity(l.dep), 0.5) : 0.02)
      .attr('stroke-width', (l, i) => hl.has(i) ? depWidth(l.dep) + 0.8 : depWidth(l.dep) * 0.3);
    allLabels.attr('opacity', n => hn.has(n.id) ? 1 : 0);
  }
  function clrHL() {
    circles.attr('opacity', d => d.layer === 0 ? 1 : 0.75); diamonds.attr('opacity', 1);
    link.attr('stroke-opacity', d => depOpacity(d.dep)).attr('stroke-width', d => depWidth(d.dep));
    allLabels.attr('opacity', d => (d.isBn || d.layer === 0) ? 1 : 0);
  }

  circles.on('mouseenter', (e, d) => { if (!lockedNode) hl4(d); showTip(e, d); }).on('mousemove', moveTip).on('mouseleave', () => { if (!lockedNode) clrHL(); hideTip(); });
  diamonds.on('mouseenter', (e, d) => { if (!lockedNode) hl4(d); showTip(e, d); }).on('mousemove', moveTip).on('mouseleave', () => { if (!lockedNode) clrHL(); hideTip(); });
  function handleClick(e, d) { e.stopPropagation(); if (lockedNode === d) { lockedNode = null; clrHL(); } else { lockedNode = d; hl4(d); } }
  circles.on('click', handleClick); diamonds.on('click', handleClick);
  svg.on('click', () => { if (lockedNode) { lockedNode = null; clrHL(); } });

  function updatePos() {
    circles.attr('cx', d => d.x).attr('cy', d => d.y);
    diamonds.attr('transform', d => 'translate(' + d.x + ',' + d.y + ')');
    allLabels.attr('x', d => d.x).attr('y', d => d.y);
    link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  }
  const drag = d3.drag().on('drag', (e, d) => { d.y = e.y; updatePos(); });
  circles.call(drag); diamonds.call(drag);

  _addLegend(svgEl.closest('.chain-chart-panel'), 'd3force');
}

/* ══════════════════════════════════════════════════════════════
   Remaining chart exports (bar, radar, etc.) — unchanged
   ══════════════════════════════════════════════════════════════ */

export function renderBottleneckBars(reports) {
  const chart = getChart('chart-bottleneck');
  if (!chart) return;
  const sorted = [...reports].sort((a, b) => (a.overall_score || 0) - (b.overall_score || 0));
  const names = sorted.map(r => r.node_name || r.name || '');
  const scores = sorted.map(r => r.overall_score || 0);
  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    title: undefined, radar: undefined,
    grid: { left: 120, right: 40, top: 10, bottom: 20 },
    xAxis: { type: 'value', max: 10, axisLabel: { color: '#999' }, splitLine: { lineStyle: { color: '#333' } } },
    yAxis: { type: 'category', data: names, axisLabel: { color: '#ccc', fontSize: 12 }, axisLine: { show: false }, axisTick: { show: false } },
    series: [{ type: 'bar', data: scores.map(v => ({ value: v, itemStyle: { color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [{ offset: 0, color: COLORS.muted }, { offset: 1, color: COLORS.accent }]) } })), barMaxWidth: 24, label: { show: true, position: 'right', color: '#ccc', formatter: '{c}' } }],
  }, true);
  setTimeout(() => chart.resize(), 50);
  chart.off('click');
  chart.on('click', params => {
    const idx = params.dataIndex;
    const report = sorted[idx];
    if (report) { if (typeof window._onBottleneckBarClick === 'function') window._onBottleneckBarClick(idx, report); else renderRadar(report); }
  });
  _hideBottleneckCompanyPanel();
}

function _showBottleneckCompanyPanel(companies, nodeName, report) {
  const panel = document.getElementById('bottleneck-company-panel');
  if (!panel) return;
  const filtered = _filterCompaniesByMarket(companies);
  let html = `<div class="nip-header"><span class="nip-title">${_esc(nodeName)}</span><span class="nip-type">代表企业</span></div>`;
  // 集中度来源徽章：真实计算 vs LLM 估算，让用户一眼分辨数据可信度
  if (report && (report.cr3_estimate != null || report.hhi_estimate != null)) {
    const cd = report.concentration_detail || {};
    const cr3 = report.cr3_estimate != null ? report.cr3_estimate : '?';
    const hhi = report.hhi_estimate != null ? report.hhi_estimate : '?';
    if (report.cr3_source === 'akshare') {
      const cnt = cd.company_count != null ? ` · A股${cd.company_count}家` : '';
      html += `<div class="nip-conc">CR3 ${cr3}% · HHI ${hhi}${cnt} <span class="src-badge src-real" title="来源：东方财富板块成分股真实市值计算">真实计算</span></div>`;
    } else {
      html += `<div class="nip-conc">CR3 ~${cr3}%(估) · HHI ~${hhi} <span class="src-badge src-est" title="LLM 世界知识估算，未经数据核实">LLM估算</span></div>`;
    }
  }
  if (filtered.length === 0) html += `<div class="nip-empty">该环节暂无代表企业数据</div>`;
  else { html += `<table class="nip-table"><thead><tr><th>企业名称</th><th>股票代码</th></tr></thead><tbody>`; for (const c of filtered) html += `<tr><td>${_esc(c.name||'')}</td><td>${c.code?`<span class="nip-code">${_esc(c.code)}</span>`:'-'}</td></tr>`; html += `</tbody></table>`; }
  panel.innerHTML = html; panel.style.display = 'block';
}
function _hideBottleneckCompanyPanel() { const p = document.getElementById('bottleneck-company-panel'); if (p) p.style.display = 'none'; }

export function renderRadar(report) {
  const chart = getChart('chart-bottleneck');
  if (!chart) return;
  const dims = [{ key:'scarcity',label:'稀缺性' },{ key:'irreplaceability',label:'不可替代性' },{ key:'supply_demand_gap',label:'供需缺口' },{ key:'pricing_power',label:'定价权' },{ key:'tech_barrier',label:'技术壁垒' }];
  let scoreMap = {};
  if (report.dimension_scores && typeof report.dimension_scores === 'object') scoreMap = report.dimension_scores;
  else if (Array.isArray(report.scores)) report.scores.forEach(s => { scoreMap[s.dimension] = s.score; });
  else scoreMap = report;
  const values = dims.map(d => scoreMap[d.key] ?? 0);
  const name = report.node_name || report.name || '';
  const chainData = window.appState.results?.decompose;
  const node = chainData?.nodes?.find(n => n.name === name);
  _showBottleneckCompanyPanel(node?.representative_companies || [], name, report);
  chart.setOption({
    tooltip: { formatter: params => { if (!params.value) return ''; return dims.map((d, i) => `${d.label}: <b>${params.value[i]}</b>`).join('<br/>'); } },
    title: { text: name, left: 'center', top: 8, textStyle: { color: '#e0e0e0', fontSize: 15, fontWeight: 600 } },
    radar: { indicator: dims.map((d, i) => ({ name: `${d.label}\n${values[i]}`, max: 10 })), shape: 'polygon', center: ['50%', '55%'], radius: '60%', axisName: { color: '#ccc', fontSize: 12 }, splitArea: { areaStyle: { color: ['rgba(59,107,204,0.05)', 'rgba(59,107,204,0.1)'] } }, splitLine: { lineStyle: { color: '#444' } } },
    grid: undefined, xAxis: undefined, yAxis: undefined,
    series: [{ type: 'radar', data: [{ value: values, name, areaStyle: { color: 'rgba(59,107,204,0.25)' }, lineStyle: { color: COLORS.accent }, itemStyle: { color: COLORS.accent }, label: { show: true, formatter: params => params.value, color: '#ccc', fontSize: 11 } }] }],
  }, true);
  chart.off('click');
  chart.on('click', () => { const reps = window.appState.results.bottleneck; if (reps) { if (typeof window._onRadarBackClick === 'function') window._onRadarBackClick(); renderBottleneckBars(reps); } });
}

const COMPARE_PALETTE = ['#5470c6', '#91cc75', '#fac858', '#ee6666'];
export function renderCompareRadar(scorecards) {
  const dom = document.getElementById('compare-radar');
  if (!dom) return;
  let chart = echarts.getInstanceByDom(dom);
  if (!chart) { chart = echarts.init(dom, 'dark'); window.addEventListener('resize', () => chart.resize()); }
  const dims = [{ key:'position',label:'市场地位',field:'market_position' },{ key:'customer',label:'客户验证',field:'customer_validation' },{ key:'capacity',label:'产能状况',field:'capacity_status' },{ key:'financial',label:'财务健康',field:'financial_health' },{ key:'valuation',label:'估值水平',field:'valuation' }];
  const series = scorecards.map((sc, i) => {
    const name = sc.supplier?.name || sc.company_name || `公司${i+1}`;
    const scores = sc.dimension_scores || {};
    const values = dims.map(d => scores[d.key] ?? sc[d.field] ?? 0);
    const color = COMPARE_PALETTE[i % COMPARE_PALETTE.length];
    return { value: values, name, areaStyle: { color: color + '30' }, lineStyle: { color, width: 2 }, itemStyle: { color }, label: { show: scorecards.length <= 2, formatter: params => params.value, color: '#ccc', fontSize: 10 } };
  });
  chart.setOption({
    tooltip: { formatter: params => { if (!params.value) return ''; return `<b>${params.name}</b><br/>` + dims.map((d, i) => `${d.label}: <b>${params.value[i]}</b>`).join('<br/>'); } },
    legend: { data: series.map(s => s.name), bottom: 8, textStyle: { color: '#bbb', fontSize: 12 } },
    radar: { indicator: dims.map(d => ({ name: d.label, max: 10 })), shape: 'polygon', center: ['50%','45%'], radius: '55%', axisName: { color: '#ccc', fontSize: 12 }, splitArea: { areaStyle: { color: ['rgba(59,107,204,0.03)','rgba(59,107,204,0.08)'] } }, splitLine: { lineStyle: { color: '#444' } } },
    series: [{ type: 'radar', data: series }],
  }, true);
}

const SUPPLIER_DIMS = [{ key:'position',label:'地位',field:'market_position' },{ key:'customer',label:'客户',field:'customer_validation' },{ key:'capacity',label:'产能',field:'capacity_status' },{ key:'financial',label:'财务',field:'financial_health' },{ key:'valuation',label:'估值',field:'valuation' }];

export function renderMiniRadar(dom, scorecard) {
  if (!dom) return;
  let chart = echarts.getInstanceByDom(dom); if (!chart) chart = echarts.init(dom);
  const scores = scorecard.dimension_scores || {};
  const values = SUPPLIER_DIMS.map(d => scores[d.key] ?? scorecard[d.field] ?? 0);
  chart.setOption({ animation: false, radar: { indicator: SUPPLIER_DIMS.map(d => ({ name: d.label, max: 10 })), shape: 'polygon', center: ['50%','50%'], radius: '70%', axisName: { color: '#666', fontSize: 9 }, splitNumber: 2, splitArea: { areaStyle: { color: ['rgba(84,112,198,0.03)','rgba(84,112,198,0.08)'] } }, splitLine: { lineStyle: { color: '#ddd' } }, axisLine: { lineStyle: { color: '#ccc' } } }, series: [{ type: 'radar', data: [{ value: values, areaStyle: { color: 'rgba(84,112,198,0.35)' }, lineStyle: { color: '#5470c6', width: 1.5 }, itemStyle: { color: '#5470c6' }, symbol: 'none' }] }] }, true);
}

export function renderDetailRadar(dom, scorecard) {
  if (!dom) return;
  let chart = echarts.getInstanceByDom(dom); if (!chart) chart = echarts.init(dom);
  const scores = scorecard.dimension_scores || {};
  const values = SUPPLIER_DIMS.map(d => scores[d.key] ?? scorecard[d.field] ?? 0);
  chart.setOption({ tooltip: { formatter: params => { if (!params.value) return ''; return SUPPLIER_DIMS.map((d, i) => `${d.label}: <b>${params.value[i]}</b>`).join('<br/>'); } }, radar: { indicator: SUPPLIER_DIMS.map((d, i) => ({ name: `${d.label}\n${values[i]}`, max: 10 })), shape: 'polygon', center: ['50%','55%'], radius: '60%', axisName: { color: '#555', fontSize: 11 }, splitArea: { areaStyle: { color: ['rgba(84,112,198,0.03)','rgba(84,112,198,0.08)'] } }, splitLine: { lineStyle: { color: '#ddd' } } }, series: [{ type: 'radar', data: [{ value: values, areaStyle: { color: 'rgba(84,112,198,0.35)' }, lineStyle: { color: '#5470c6', width: 2 }, itemStyle: { color: '#5470c6' } }] }] }, true);
}

export function renderAiScoreBar(dom, validations) {
  if (!dom || !validations || validations.length === 0) return;
  let chart = echarts.getInstanceByDom(dom); if (!chart) chart = echarts.init(dom);
  const models = validations.map(v => (v.model_name || '').split('/').pop());
  const scores = validations.map(v => v.score ?? 5);
  chart.setOption({ tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } }, grid: { left: 80, right: 20, top: 10, bottom: 24 }, xAxis: { type: 'value', max: 10, splitLine: { lineStyle: { color: '#eee' } } }, yAxis: { type: 'category', data: models, axisLabel: { color: '#555', fontSize: 11 } }, series: [{ type: 'bar', data: scores.map(s => ({ value: s, itemStyle: { color: s >= 7.5 ? '#4caf50' : (s >= 5 ? '#ffc107' : '#f44336') } })), barWidth: 16, label: { show: true, position: 'right', color: '#555', fontSize: 11, formatter: '{c}' } }] }, true);
}

/* ══════════════════════════════════════════════════════════════
   Wizard-mode chart support
   ══════════════════════════════════════════════════════════════ */

function _renderWizActiveTab() {
  const chainData = window._wizChainData;
  if (!chainData) return;
  if (_wizActiveTab === 'force' && !_wizRendered.force) {
    _renderEchartsForce(chainData, 'wiz-chart-force');
  } else if (_wizActiveTab === 'force') {
    try { instances.get('wiz-chart-force')?.resize(); } catch {}
  }
  if (_wizActiveTab === 'tree' && !_wizRendered.tree) {
    _renderEchartsTree(chainData, 'wiz-chart-tree');
  } else if (_wizActiveTab === 'tree') {
    try { instances.get('wiz-chart-tree')?.resize(); } catch {}
  }
  if (_wizActiveTab === 'd3force' && !_wizRendered.d3force) {
    _renderD3Force(chainData, 'wiz-chart-d3');
  }
}

export function initWizChainTabs() {
  const bar = document.getElementById('wiz-chain-tabs');
  if (!bar) return;
  bar.addEventListener('click', e => {
    const btn = e.target.closest('.chain-tab');
    if (!btn) return;
    const chart = btn.dataset.chart;
    if (chart === _wizActiveTab) return;
    _wizActiveTab = chart;
    bar.querySelectorAll('.chain-tab').forEach(b => b.classList.toggle('active', b === btn));
    const panels = document.getElementById('wiz-chart-panels');
    if (panels) panels.querySelectorAll('.chain-chart-panel').forEach(p => p.classList.toggle('active', p.dataset.chart === chart));
    _renderWizActiveTab();
  });
}

export function renderWizDAG(chainData, bottleneckScoreMap) {
  const bnMap = bottleneckScoreMap || {};
  if (chainData && chainData.nodes) {
    chainData.nodes.forEach(n => { n.is_bottleneck = n.name in bnMap; });
  }
  window._wizChainData = chainData;
  if (!window.appState) window.appState = {};
  window.appState.bottleneckScoreMap = bnMap;
  _wizRendered = { force: false, tree: false, d3force: false };
  const container = document.getElementById('wiz-chart-force');
  if (container) container.classList.remove('skeleton');
  _renderWizActiveTab();
}

export function initWizFullscreen() {
  const btn = document.getElementById('wiz-p1-fullscreen');
  if (!btn) return;
  const card = btn.closest('.wiz-card');
  if (!card) return;
  btn.addEventListener('click', () => {
    if (!document.fullscreenElement) card.requestFullscreen().catch(() => {});
    else document.exitFullscreen();
  });
  document.addEventListener('fullscreenchange', () => {
    if (document.fullscreenElement === card || (!document.fullscreenElement && btn.closest('.wiz-card') === card)) {
      setTimeout(() => { resizeAll(); }, 100);
      setTimeout(() => { resizeAll(); }, 400);
    }
  });
}
