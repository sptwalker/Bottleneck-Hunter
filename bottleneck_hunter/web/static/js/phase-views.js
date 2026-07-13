/**
 * phase-views.js — 各 Phase 的渲染函数
 * 表格、图表、汇总等可视化组件。
 */

import { renderWizDAG } from './charts.js';

const LAYER_TYPE_LABELS = {
  end_product: '终端', assembly: '组件', component: '零部件',
  sub_component: '子部件', material: '材料', raw_material: '原材料', equipment: '设备',
};

let scatterChart = null;
let radarChart = null;
let barChart = null;
let stackChart = null;

/* ── Phase 1: 产业链 + 瓶颈 ─────────────── */
export function renderPhase1(data) {
  const chain = data.chain;
  const allReports = data.all_reports || data.top_reports || [];
  const topReports = data.top_reports || [];
  const bottleneckScoreMap = {};
  topReports.forEach(r => { if (r.node_name) bottleneckScoreMap[r.node_name] = r.overall_score; });
  renderWizDAG(chain, bottleneckScoreMap);
  renderBottleneckBars(allReports, chain?.nodes || []);
  renderBnStats(data);
}

function renderBottleneckBars(reports, chainNodes) {
  const container = document.getElementById('wiz-chart-bn');
  if (!container) return;
  container.classList.remove('skeleton');

  const nodeTypeMap = {};
  (chainNodes || []).forEach(n => { nodeTypeMap[n.name] = n.layer_type || ''; });

  const sorted = [...reports].sort((a, b) => b.overall_score - a.overall_score).slice(0, 12);

  const names = sorted.map(r => {
    const lt = nodeTypeMap[r.node_name] || '';
    const label = LAYER_TYPE_LABELS[lt] || lt || '其他';
    return `【${label}】${r.node_name}`;
  }).reverse();

  const barH = Math.max(18, Math.min(28, 280 / sorted.length));
  const h = Math.max(160, sorted.length * (barH + 6) + 30);
  container.style.height = h + 'px';

  const chart = echarts.getInstanceByDom(container) || echarts.init(container);
  chart.setOption({
    grid: { left: 280, right: 50, top: 8, bottom: 16 },
    xAxis: { type: 'value', max: 10, axisLabel: { fontSize: 10 } },
    yAxis: {
      type: 'category',
      data: names,
      axisLabel: { fontSize: 10, width: 260, overflow: 'truncate' },
    },
    series: [{
      type: 'bar',
      barMaxWidth: barH,
      data: sorted.map(r => ({
        value: r.overall_score,
        itemStyle: { color: scoreColor(r.overall_score) },
      })).reverse(),
      label: { show: true, position: 'right', fontSize: 10,
        formatter: p => p.value.toFixed(1) },
    }],
    tooltip: {
      formatter: p => {
        const r = sorted[sorted.length - 1 - p.dataIndex];
        const lt = nodeTypeMap[r.node_name] || '';
        const label = LAYER_TYPE_LABELS[lt] || lt || '';
        return `${r.node_name}<br/>层级: ${label} (L${r.layer})<br/>瓶颈分: ${r.overall_score.toFixed(1)}`;
      },
    },
  }, true);
}

function renderBnStats(data) {
  const el = document.getElementById('wiz-bn-stats');
  if (!el || !data.chain) return;
  const nodes = data.chain.nodes || [];
  const maxLayer = Math.max(...nodes.map(n => n.layer || 0), 0);
  const totalNodes = nodes.length;
  const topReports = data.top_reports || [];
  const allReports = data.all_reports || [];
  const analyzedCount = allReports.length || totalNodes;
  el.innerHTML = `本次共扫描 <b>${maxLayer + 1}</b> 层供应链，`
    + `分析了 <b>${analyzedCount}</b> 个产业环节的瓶颈得分。`;
  el.style.display = 'block';
}

/* ── Phase 2: 入围企业表格（可展开详情） ───────────────── */
let _p2SortField = 'final';
let _p2SortDir = 'desc';
let _p2Scorecards = null;
let _p2FailedTickers = [];
let _p2Selected = new Set();
let _p2OnSelectionChange = null;

export function setP2SelectionCallback(cb) { _p2OnSelectionChange = cb; }
export function getP2SelectedTickers() { return new Set(_p2Selected); }
export function resetP2Selection() { _p2Selected.clear(); }

function _getTicker(sc) { return sc.supplier?.ticker || sc.ticker || ''; }

// 双击企业行 → 统一企业详情抽屉（系统评分/基本信息等，来自 scorecard）。覆盖 Phase2 入围 / Phase3 最终评分表。
if (typeof document !== 'undefined') {
  document.addEventListener('dblclick', (e) => {
    const row = e.target.closest('.p2-summary-row');
    if (!row || !window.openCompanyDrawer) return;
    // 按 data-ticker 在评分卡里查找（行按 final_score 排序显示，data-idx 与未排序的 _p2Scorecards 不对应；
    // 且反查表也复用 .p2-summary-row 但不在 _p2Scorecards 中，按 ticker 查找自然跳过 → 交其自身处理）。
    const ticker = row.dataset.ticker;
    if (!ticker || !Array.isArray(_p2Scorecards)) return;
    const sc = _p2Scorecards.find(s => _getTicker(s) === ticker);
    if (!sc) return;
    const sup = sc.supplier || {};
    window.openCompanyDrawer({
      ticker: sup.ticker || ticker,
      name: sup.name || sup.ticker || ticker,
      market: sup.market || window.appState?.market || 'us_stock',
      scorecard: sc,
    });
  });
}

function _fireSelectionChange() {
  const countEl = document.getElementById('manual-pick-count');
  if (countEl) countEl.textContent = `已选 ${_p2Selected.size} / ${(_p2Scorecards || []).length} 家`;
  if (_p2OnSelectionChange) _p2OnSelectionChange(new Set(_p2Selected));
}

function _getScoreVal(sc, field) {
  if (field === 'quality') return sc.overall_score ?? 0;
  if (field === 'alpha') return sc.alpha?.alpha_score ?? 0;
  return sc.final?.final_score ?? sc.overall_score ?? 0;
}

function _sortIndicator(field) {
  if (field !== _p2SortField) return '<span class="sort-arrow sort-arrow--idle">⇅</span>';
  return _p2SortDir === 'desc'
    ? '<span class="sort-arrow sort-arrow--active">↓</span>'
    : '<span class="sort-arrow sort-arrow--active">↑</span>';
}

export function renderPhase2Table(scorecards, failedTickers) {
  const container = document.getElementById('wiz-p2-table');
  if (!container) return;

  _p2Scorecards = scorecards;
  _p2FailedTickers = failedTickers || [];

  if (_p2Selected.size === 0 && scorecards.length > 0) {
    scorecards.forEach(sc => { const t = _getTicker(sc); if (t) _p2Selected.add(t); });
  }

  const sorted = [...scorecards].sort((a, b) => {
    const va = _getScoreVal(a, _p2SortField);
    const vb = _getScoreVal(b, _p2SortField);
    return _p2SortDir === 'desc' ? vb - va : va - vb;
  });

  let html = '';
  const failed = _p2FailedTickers;
  if (failed.length > 0) {
    html += `<div class="p2-data-warning">
      <span>⚠ 以下 ${failed.length} 家企业的部分财务/市场数据获取失败，评分可能不准确：</span>
      <span class="p2-warn-tickers">${failed.slice(0, 10).join(', ')}${failed.length > 10 ? ' ...' : ''}</span>
      <button class="btn btn-sm btn-warn-refetch" id="btn-refetch-failed">重新获取</button>
    </div>`;
  }

  const allChecked = sorted.every(sc => _p2Selected.has(_getTicker(sc)));
  html += `<div class="p2-table-toolbar">
    <label class="p2-select-all-label"><input type="checkbox" id="p2-select-all" ${allChecked ? 'checked' : ''}> 全选</label>
    <button class="btn btn-sm p2-btn-toggle-all" id="btn-toggle-all-details">展开全部</button>
  </div>`;

  html += `<table class="data-table p2-expandable-table">
    <thead><tr>
      <th style="width:32px"></th><th style="width:36px"></th><th>#</th><th>公司</th><th>代码</th><th>瓶颈环节</th>
      <th class="p2-sortable-th" data-sort="quality">质量分 ${_sortIndicator('quality')}</th>
      <th class="p2-sortable-th" data-sort="alpha">预期差 ${_sortIndicator('alpha')}</th>
      <th class="p2-sortable-th" data-sort="final">推荐分 ${_sortIndicator('final')}</th>
    </tr></thead><tbody>`;

  sorted.forEach((sc, i) => {
    const alpha = sc.alpha || {};
    const fin = sc.final || {};
    const finalScore = fin.final_score ?? sc.overall_score ?? 0;
    const alphaScore = alpha.alpha_score ?? 0;
    const displayName = formatCompanyName(sc);
    const ticker = _getTicker(sc);
    const checked = _p2Selected.has(ticker);

    html += `<tr class="p2-summary-row${checked ? '' : ' p2-row-unchecked'}" data-idx="${i}" data-ticker="${ticker}">
      <td class="p2-arrow-cell"><span class="expand-arrow">▸</span></td>
      <td class="p2-check-cell"><input type="checkbox" class="p2-row-check" data-ticker="${ticker}" ${checked ? 'checked' : ''}></td>
      <td>${i + 1}</td>
      <td class="col-name">${displayName}</td>
      <td class="col-ticker">${ticker || '-'}</td>
      <td>${(sc.bottleneck_node || '').split(',')[0]}</td>
      <td><span class="score-badge" style="background:${scoreColor(sc.overall_score)}">${(sc.overall_score || 0).toFixed(1)}</span></td>
      <td><span class="score-badge score-badge--alpha" style="background:${scoreColor(alphaScore)}">${alphaScore.toFixed(1)}</span></td>
      <td><span class="score-badge score-badge--final" style="background:${scoreColor(finalScore)}">${finalScore.toFixed(2)}</span></td>
    </tr>`;

    html += `<tr class="p2-expand-row" data-idx="${i}"><td colspan="9" class="p2-expand-cell">`;
    html += buildDetailGrid(sc);
    html += `</td></tr>`;
  });

  html += '</tbody></table>';
  container.innerHTML = html;
  _fireSelectionChange();

  if (container._p2ClickHandler) {
    container.removeEventListener('click', container._p2ClickHandler);
  }
  container._p2ClickHandler = (e) => {
    const cb = e.target.closest('.p2-row-check');
    if (cb) {
      const ticker = cb.dataset.ticker;
      if (cb.checked) _p2Selected.add(ticker); else _p2Selected.delete(ticker);
      const row = cb.closest('.p2-summary-row');
      if (row) row.classList.toggle('p2-row-unchecked', !cb.checked);
      const selAll = container.querySelector('#p2-select-all');
      if (selAll) selAll.checked = _p2Scorecards.every(sc => _p2Selected.has(_getTicker(sc)));
      _fireSelectionChange();
      return;
    }

    const selAllCb = e.target.closest('#p2-select-all');
    if (selAllCb) {
      const check = selAllCb.checked;
      _p2Selected.clear();
      if (check) _p2Scorecards.forEach(sc => { const t = _getTicker(sc); if (t) _p2Selected.add(t); });
      container.querySelectorAll('.p2-row-check').forEach(c => { c.checked = check; });
      container.querySelectorAll('.p2-summary-row').forEach(r => r.classList.toggle('p2-row-unchecked', !check));
      _fireSelectionChange();
      return;
    }

    const sortTh = e.target.closest('.p2-sortable-th');
    if (sortTh) {
      const field = sortTh.dataset.sort;
      if (field === _p2SortField) {
        _p2SortDir = _p2SortDir === 'desc' ? 'asc' : 'desc';
      } else {
        _p2SortField = field;
        _p2SortDir = 'desc';
      }
      renderPhase2Table(_p2Scorecards, _p2FailedTickers);
      return;
    }

    const toggleBtn = e.target.closest('#btn-toggle-all-details');
    if (toggleBtn) {
      const rows = container.querySelectorAll('.p2-expand-row');
      const anyOpen = Array.from(rows).some(r => r.classList.contains('open'));
      rows.forEach(r => r.classList.toggle('open', !anyOpen));
      container.querySelectorAll('.expand-arrow').forEach(a => { a.textContent = anyOpen ? '▸' : '▾'; });
      toggleBtn.textContent = anyOpen ? '展开全部' : '收起全部';
      return;
    }

    const row = e.target.closest('.p2-summary-row');
    if (!row) return;
    const idx = row.dataset.idx;
    const detail = container.querySelector(`.p2-expand-row[data-idx="${idx}"]`);
    if (!detail) return;
    detail.classList.toggle('open');
    row.querySelector('.expand-arrow').textContent = detail.classList.contains('open') ? '▾' : '▸';

    const togBtn = container.querySelector('#btn-toggle-all-details');
    if (togBtn) {
      const anyOpen = Array.from(container.querySelectorAll('.p2-expand-row')).some(r => r.classList.contains('open'));
      togBtn.textContent = anyOpen ? '收起全部' : '展开全部';
    }
  };
  container.addEventListener('click', container._p2ClickHandler);
}

/* ── 反向分析列表 ───────────────────────────
 * 镜像 Phase2 可展开表，展开行复用 buildDetailGrid → 详情与入围企业完全一致。
 */
const _reverseSelected = new Set();
export function getReverseSelected() { return new Set(_reverseSelected); }

function _escRev(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function _reverseAddToPool(btn, records, market) {
  const id = btn.dataset.id;
  const r = records.find(x => x.id === id);
  if (!r) return;
  btn.disabled = true;
  try {
    const res = await fetch('/api/watchlist', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ticker: r.ticker, company_name: r.company_name || r.ticker,
        company_name_cn: r.company_name_cn || '', market: r.market || market,
        tier: 'track', sector: r.sector || '', source: 'reverse',
        bottleneck_node: r.bottleneck_node || '',
      }),
    });
    if (res.ok) { btn.textContent = '已加入'; btn.classList.add('wl-p4-add-done'); }
    else {
      const err = await res.json().catch(() => ({}));
      btn.textContent = (err.detail || '').includes('already') ? '已存在' : '失败';
    }
  } catch (e) { btn.textContent = '失败'; }
}

export function renderReverseTable(records, opts = {}) {
  const { market = 'us_stock', onChange, onSelectionChange } = opts;
  const container = document.getElementById('wiz-reverse-table');
  if (!container) return;

  if (!records || records.length === 0) {
    container.innerHTML = '<p class="empty-text">暂无反向分析记录，输入企业代码开始分析</p>';
    _reverseSelected.clear();
    if (onSelectionChange) onSelectionChange(new Set());
    return;
  }

  // 清理已不存在的选中项
  const ids = new Set(records.map(r => r.id));
  [..._reverseSelected].forEach(id => { if (!ids.has(id)) _reverseSelected.delete(id); });

  const srcLabel = s => (s === 'matched' ? '匹配链' : 'LLM');
  let html = `<table class="data-table p2-expandable-table"><thead><tr>
    <th style="width:32px"></th><th style="width:36px"></th><th>#</th><th>公司</th><th>代码</th><th>瓶颈环节</th>
    <th>质量分</th><th>预期差</th><th>推荐分</th><th>来源</th><th>操作</th>
  </tr></thead><tbody>`;

  records.forEach((r, i) => {
    const checked = _reverseSelected.has(r.id);
    html += `<tr class="p2-summary-row" data-idx="${i}" data-id="${_escRev(r.id)}" data-ticker="${_escRev(r.ticker)}">
      <td class="p2-arrow-cell"><span class="expand-arrow">▸</span></td>
      <td class="p2-check-cell"><input type="checkbox" class="rev-row-check" data-id="${_escRev(r.id)}" ${checked ? 'checked' : ''}></td>
      <td>${i + 1}</td>
      <td class="col-name">${_escRev(r.company_name || r.ticker)}</td>
      <td class="col-ticker">${_escRev(r.ticker)}</td>
      <td>${_escRev((r.bottleneck_node || '').split(',')[0])}</td>
      <td><span class="score-badge" style="background:${scoreColor(r.quality_score)}">${(r.quality_score || 0).toFixed(1)}</span></td>
      <td><span class="score-badge score-badge--alpha" style="background:${scoreColor(r.alpha_score)}">${(r.alpha_score || 0).toFixed(1)}</span></td>
      <td><span class="score-badge score-badge--final" style="background:${scoreColor(r.final_score)}">${(r.final_score || 0).toFixed(2)}</span></td>
      <td><span class="rev-src-badge rev-src-${_escRev(r.source)}">${srcLabel(r.source)}</span></td>
      <td class="rev-ops">
        <button class="btn btn-sm rev-add-btn" data-id="${_escRev(r.id)}">加入观察池</button>
        <button class="btn btn-sm rev-del-btn" data-id="${_escRev(r.id)}">删除</button>
      </td>
    </tr>`;
    html += `<tr class="p2-expand-row" data-idx="${i}" data-id="${_escRev(r.id)}"><td colspan="11" class="p2-expand-cell"><div class="rev-detail-slot">展开加载详情...</div></td></tr>`;
  });
  html += '</tbody></table>';
  container.innerHTML = html;

  if (container._revHandler) container.removeEventListener('click', container._revHandler);
  container._revHandler = async (e) => {
    const chk = e.target.closest('.rev-row-check');
    if (chk) {
      if (chk.checked) _reverseSelected.add(chk.dataset.id); else _reverseSelected.delete(chk.dataset.id);
      if (onSelectionChange) onSelectionChange(getReverseSelected());
      return;
    }
    const addBtn = e.target.closest('.rev-add-btn');
    if (addBtn) { e.stopPropagation(); await _reverseAddToPool(addBtn, records, market); return; }
    const delBtn = e.target.closest('.rev-del-btn');
    if (delBtn) {
      e.stopPropagation();
      if (!confirm('确定删除该反向分析记录？')) return;
      const res = await fetch(`/api/reverse/${encodeURIComponent(delBtn.dataset.id)}?market=${encodeURIComponent(market)}`, { method: 'DELETE' });
      if (res.ok && onChange) onChange();
      return;
    }
    const row = e.target.closest('.p2-summary-row');
    if (!row) return;
    const idx = row.dataset.idx;
    const detail = container.querySelector(`.p2-expand-row[data-idx="${idx}"]`);
    if (!detail) return;
    detail.classList.toggle('open');
    const arrow = row.querySelector('.expand-arrow');
    if (arrow) arrow.textContent = detail.classList.contains('open') ? '▾' : '▸';
    const slot = detail.querySelector('.rev-detail-slot');
    if (detail.classList.contains('open') && slot && !slot.dataset.loaded) {
      slot.innerHTML = '<p class="empty-text">加载详情...</p>';
      try {
        const resp = await fetch(`/api/reverse/${encodeURIComponent(row.dataset.id)}?market=${encodeURIComponent(market)}`);
        const rec = await resp.json();
        slot.innerHTML = buildDetailGrid(rec.result_json || {});
        slot.dataset.loaded = '1';
      } catch (err) {
        slot.innerHTML = `<p class="empty-text">加载失败: ${err.message}</p>`;
      }
    }
  };
  container.addEventListener('click', container._revHandler);
  if (onSelectionChange) onSelectionChange(getReverseSelected());
}

export function buildDetailGrid(sc) {
  const alpha = sc.alpha || {};
  const moat = sc.moat || {};
  const fin = sc.final || {};
  const cat = sc.catalyst || {};
  const dims = sc.dimension_scores || {};
  const snap = sc.financial_snapshot || {};
  const trend = snap.trend || {};
  const s = sc.supplier || {};

  let html = '';

  // ── 顶部：企业概览 ──
  html += '<div class="p2-detail-header">';
  html += '<div class="p2-header-main">';
  if (s.description) {
    const desc = s.description.length > 150 ? s.description.slice(0, 150) + '…' : s.description;
    html += `<div class="p2-header-desc">${desc}</div>`;
  }
  if (s.key_products && s.key_products.length > 0) {
    html += '<div class="p2-header-products">';
    s.key_products.forEach(p => { html += `<span class="p2-tag">${p}</span>`; });
    html += '</div>';
  }
  html += '</div>';
  html += '<div class="p2-header-meta">';
  if (s.market_cap != null) {
    const unit = s.market === 'us_stock' ? '$B' : '亿';
    html += `<span class="p2-meta-item">市值 <strong>${s.market_cap}${unit}</strong></span>`;
  }
  if (s.sector) html += `<span class="p2-meta-item">${s.sector}</span>`;
  if (snap.data_source) html += `<span class="p2-meta-item p2-meta-src">${snap.data_source}</span>`;
  html += '</div>';
  html += '</div>';

  // ── 三栏主体 ──
  html += '<div class="p2-detail-grid">';

  // ── 左栏：财务与市场（淡蓝底色） ──
  html += `<div class="p2-detail-section p2-section--blue">`;
  html += '<h4>财务与市场</h4>';
  const snapEmpty = !snap.revenue_yi && !snap.net_profit_yi && !snap.roe_pct && snap.data_source !== 'yfinance' && snap.data_source !== 'akshare';
  if (snapEmpty) {
    html += '<div class="p2-detail-missing">⚠ 部分数据因获取失败存在缺失</div>';
  }

  const fmtPct = v => v != null ? `${v >= 0 ? '+' : ''}${v.toFixed(1)}%` : '-';
  const fmtVal = (v, suffix) => v != null ? `${v.toFixed(2)}${suffix || ''}` : '-';
  const fmtValDir = v => {
    if (v == null) return '-';
    const cls = v > 0 ? 'p2-val-up' : v < 0 ? 'p2-val-down' : '';
    return `<span class="${cls}">${v >= 0 ? '+' : ''}${v.toFixed(1)}%</span>`;
  };

  html += '<div class="p2-fin-grid">';
  const finItems = [
    ['营收', snap.revenue_yi != null ? `${snap.revenue_yi.toFixed(2)}亿` : '-'],
    ['营收同比', fmtValDir(snap.revenue_yoy_pct)],
    ['净利润', snap.net_profit_yi != null ? `${snap.net_profit_yi.toFixed(2)}亿` : '-'],
    ['净利同比', fmtValDir(snap.net_profit_yoy_pct)],
    ['毛利率', snap.gross_margin_pct != null ? `${snap.gross_margin_pct.toFixed(1)}%` : '-'],
    ['ROE', snap.roe_pct != null ? `${snap.roe_pct.toFixed(1)}%` : '-'],
    ['负债率', snap.debt_ratio_pct != null ? `${snap.debt_ratio_pct.toFixed(1)}%` : '-'],
    ['研报覆盖', snap.analyst_report_count != null ? `${snap.analyst_report_count}` : '-'],
  ];
  finItems.forEach(([label, val]) => {
    html += `<div class="p2-fin-cell"><span class="p2-fin-label">${label}</span><span class="p2-fin-val">${val}</span></div>`;
  });
  html += '</div>';

  html += '<div class="p2-detail-divider">市场动态</div>';
  html += '<div class="p2-fin-grid">';
  const mktItems = [
    ['量比', snap.volume_ratio != null ? snap.volume_ratio.toFixed(2) : '-'],
    ['3月涨幅', snap.price_change_3m_pct != null ? fmtValDir(snap.price_change_3m_pct) : '-'],
    ['1月涨幅', snap.price_change_1m_pct != null ? fmtValDir(snap.price_change_1m_pct) : '-'],
    ['机构持仓', snap.institution_holding_pct != null ? `${snap.institution_holding_pct.toFixed(1)}%` : '-'],
  ];
  if (snap.consecutive_volume_days >= 3) mktItems.push(['连续放量', `${snap.consecutive_volume_days}天`]);
  if (snap.analyst_rating) mktItems.push(['评级', snap.analyst_rating]);
  mktItems.forEach(([label, val]) => {
    html += `<div class="p2-fin-cell"><span class="p2-fin-label">${label}</span><span class="p2-fin-val">${val}</span></div>`;
  });
  html += '</div>';

  if (trend.trend_summary) {
    html += '<div class="p2-detail-divider">财务趋势</div>';
    html += `<div class="p2-trend-summary">${trend.trend_summary}</div>`;
    html += '<div class="p2-fin-grid">';
    if (trend.revenue_acceleration != null) {
      const cls = trend.revenue_acceleration > 0 ? 'p2-val-up' : trend.revenue_acceleration < 0 ? 'p2-val-down' : '';
      html += `<div class="p2-fin-cell"><span class="p2-fin-label">营收加速</span><span class="p2-fin-val ${cls}">${trend.revenue_acceleration > 0 ? '+' : ''}${trend.revenue_acceleration.toFixed(1)}pp</span></div>`;
    }
    if (trend.gross_margin_trend != null) {
      const cls = trend.gross_margin_trend > 0 ? 'p2-val-up' : trend.gross_margin_trend < 0 ? 'p2-val-down' : '';
      html += `<div class="p2-fin-cell"><span class="p2-fin-label">毛利趋势</span><span class="p2-fin-val ${cls}">${trend.gross_margin_trend > 0 ? '+' : ''}${trend.gross_margin_trend.toFixed(1)}pp</span></div>`;
    }
    if (trend.consecutive_growth_quarters > 0) {
      html += `<div class="p2-fin-cell"><span class="p2-fin-label">连续增长</span><span class="p2-fin-val">${trend.consecutive_growth_quarters}季</span></div>`;
    }
    html += '</div>';
  }
  if (snap.report_date) html += `<div class="p2-detail-note">报告期: ${snap.report_date}</div>`;
  html += '</div>';

  // ── 中栏：质量评估（带柱状图） ──
  html += `<div class="p2-detail-section ${sectionBgClass(sc.overall_score)}">`;
  html += '<h4>质量评估</h4>';
  const qualityItems = [
    ['市场地位', dims.position ?? sc.market_position],
    ['客户验证', dims.customer ?? sc.customer_validation],
    ['产能状况', dims.capacity ?? sc.capacity_status],
    ['财务健康', dims.financial ?? sc.financial_health],
    ['估值水平', dims.valuation ?? sc.valuation],
  ];
  const baseScores = qualityItems.map(x => x[1] || 0);
  const baseAvg = baseScores.length ? baseScores.reduce((a, b) => a + b, 0) / baseScores.length : 0;
  qualityItems.forEach(([label, val]) => {
    const v = val != null ? val : 0;
    const pct = Math.round(v / 10 * 100);
    html += `<div class="p2-dim-row">
      <span class="p2-dim-label">${label}</span>
      <span class="p2-dim-bar-track"><span class="p2-dim-bar-fill" style="width:${pct}%;background:#10b981"></span></span>
      <span class="p2-dim-score">${v.toFixed(1)}</span>
    </div>`;
  });
  html += detailItem('基础均值', `${baseAvg.toFixed(1)} × 70%`, true);

  html += '<div class="p2-detail-divider">护城河</div>';
  [['专利壁垒', moat.patent_moat], ['转换成本', moat.switching_cost],
   ['产能优势', moat.capacity_lead_time], ['成本优势', moat.cost_advantage],
  ].forEach(([label, val]) => {
    const v = val != null ? val : 0;
    const pct = Math.round(v / 10 * 100);
    html += `<div class="p2-dim-row">
      <span class="p2-dim-label">${label}</span>
      <span class="p2-dim-bar-track"><span class="p2-dim-bar-fill" style="width:${pct}%;background:#f59e0b"></span></span>
      <span class="p2-dim-score">${v.toFixed(1)}</span>
    </div>`;
  });
  html += detailItem('护城河均值', `${(moat.overall_moat || 0).toFixed(1)} × 30%`, true);
  html += detailItem('质量分合计', fmtScore(sc.overall_score), true);
  if (moat.moat_reasoning) html += `<div class="p2-detail-note">${moat.moat_reasoning}</div>`;
  html += '</div>';

  // ── 右栏：预期差分析 ──
  html += `<div class="p2-detail-section ${sectionBgClass(alpha.alpha_score)}">`;
  html += '<h4>预期差分析</h4>';

  html += '<div class="p2-detail-divider">关注度 5 维拆解</div>';
  const dimItems = [
    ['市值规模', alpha.dim_cap, '15%'],
    ['分析师覆盖', alpha.dim_analyst, '20%'],
    ['成交量动量', alpha.dim_volume, '25%'],
    ['近3月涨幅', alpha.dim_price, '15%'],
    ['机构持仓', alpha.dim_institution, '25%'],
  ];
  dimItems.forEach(([label, score, weight]) => {
    const s = score != null ? score : 5;
    const pct = Math.round(s / 9 * 100);
    html += `<div class="p2-dim-row">
      <span class="p2-dim-label">${label}</span>
      <span class="p2-dim-bar-track"><span class="p2-dim-bar-fill" style="width:${pct}%"></span></span>
      <span class="p2-dim-score">${s.toFixed(0)}</span>
      <span class="p2-dim-weight">×${weight}</span>
    </div>`;
  });

  let adjustNotes = [];
  if (alpha.ipo_bonus > 0) adjustNotes.push(`新股+${alpha.ipo_bonus.toFixed(0)}`);
  if (alpha.vp_discount < 1) adjustNotes.push(`量价背离×${alpha.vp_discount}`);
  html += `<div class="p2-dim-summary">`;
  html += `<span>关注度 <strong>${fmtScore(alpha.market_attention)}</strong></span>`;
  html += `<span>信息差 <strong>${fmtScore(alpha.information_gap)}</strong></span>`;
  if (adjustNotes.length) html += `<span class="p2-dim-adjust">${adjustNotes.join(' | ')}</span>`;
  html += `</div>`;

  html += '<div class="p2-detail-divider">Alpha 计算</div>';
  const baseAlpha = alpha.alpha_score != null
    ? (alpha.alpha_score - (alpha.catalyst_bonus || 0) - (alpha.trend_bonus || 0) - (alpha.smart_money_bonus || 0)) / (alpha.vp_discount || 1)
    : null;
  if (baseAlpha != null) html += detailItem('基础Alpha', baseAlpha.toFixed(1));
  if (alpha.catalyst_bonus && alpha.catalyst_bonus > 0)
    html += detailItem('催化剂加分', `+${alpha.catalyst_bonus.toFixed(1)}`);
  if (alpha.trend_bonus)
    html += detailItem('趋势加分', `${alpha.trend_bonus > 0 ? '+' : ''}${alpha.trend_bonus.toFixed(1)}`);
  if (alpha.smart_money_bonus)
    html += detailItem('聪明钱加分', `${alpha.smart_money_bonus > 0 ? '+' : ''}${alpha.smart_money_bonus.toFixed(1)}`);
  html += detailItem('预期差合计', fmtScore(alpha.alpha_score), true);

  if (cat.events && cat.events.length > 0) {
    html += `<div class="p2-detail-divider">催化剂${cat.urgency_score != null ? ` (${cat.urgency_score.toFixed(1)})` : ''}</div>`;
    cat.events.forEach(ev => {
      const conf = ev.confidence != null ? `置信${ev.confidence.toFixed(0)}` : '';
      const imp = ev.impact_score != null ? `影响${ev.impact_score.toFixed(0)}` : '';
      const meta = [conf, imp].filter(Boolean).join(', ');
      html += `<div class="p2-detail-event">
        <span class="p2-event-type">[${ev.event_type}]</span>
        ${ev.description}${ev.expected_date ? ` — ${ev.expected_date}` : ''}
        ${meta ? `<span class="p2-event-meta">(${meta})</span>` : ''}
      </div>`;
    });
    if (cat.investment_window) html += `<div class="p2-detail-note">投资窗口: ${cat.investment_window}</div>`;
  }
  html += '</div>';

  html += '</div>'; // close p2-detail-grid

  // ── 底部：公式 + 优劣势 ──
  html += '<div class="p2-detail-bottom">';
  if (fin.final_score != null) {
    const wq = fin.quality_weight || 0.4;
    const wa = fin.alpha_weight || 0.6;
    html += `<div class="p2-detail-formula">推荐分 = 质量<sup>${wq}</sup> × 预期差<sup>${wa}</sup> = ${(fin.quality_score || 0).toFixed(1)}<sup>${wq}</sup> × ${(fin.alpha_score || 0).toFixed(1)}<sup>${wa}</sup> = <strong>${fin.final_score.toFixed(2)}</strong></div>`;
  }
  if ((sc.strengths && sc.strengths.length) || (sc.weaknesses && sc.weaknesses.length)) {
    html += '<div class="p2-detail-sw">';
    if (sc.strengths && sc.strengths.length) {
      html += '<div class="p2-sw-col">';
      sc.strengths.forEach(s => { html += `<span class="p2-detail-strength">+ ${s}</span>`; });
      html += '</div>';
    }
    if (sc.weaknesses && sc.weaknesses.length) {
      html += '<div class="p2-sw-col">';
      sc.weaknesses.forEach(w => { html += `<span class="p2-detail-weakness">- ${w}</span>`; });
      html += '</div>';
    }
    html += '</div>';
  }
  html += '</div>';

  return html;
}

function detailItem(label, value, highlight) {
  return `<div class="p2-detail-item${highlight ? ' p2-detail-item--highlight' : ''}"><span class="p2-detail-label">${label}</span><span class="p2-detail-value">${value}</span></div>`;
}

function fmtScore(v) {
  if (v == null) return '-';
  return typeof v === 'number' ? v.toFixed(1) : String(v);
}

function formatCompanyName(sc) {
  const s = sc.supplier || {};
  const name = s.name || '-';
  const nameCn = s.name_cn || '';
  const layer = sc.layer != null ? sc.layer : '';
  const layerTag = layer !== '' ? ` <span class="p2-layer-tag">L${layer}</span>` : '';
  const isUs = s.market === 'us_stock';

  if (isUs) {
    if (nameCn && nameCn !== name) {
      return `${name} <span class="p2-name-cn">(${nameCn})</span>${layerTag}`;
    }
    return `${name}${layerTag}`;
  }
  if (nameCn && nameCn !== name) {
    return `${name} <span class="p2-name-cn">(${nameCn})</span>${layerTag}`;
  }
  return `${name}${layerTag}`;
}

function sectionBgClass(score) {
  if (score == null) return '';
  if (score >= 7.5) return 'p2-section--excellent';
  if (score >= 6) return 'p2-section--good';
  if (score >= 4) return 'p2-section--average';
  return 'p2-section--poor';
}

/* ── Phase 3: 最终排名表 ─────────────────── */
export function renderPhase3Table(ranked, onDblClick) {
  const container = document.getElementById('wiz-p3-table');
  if (!container) return;

  let html = `<table class="data-table">
    <thead><tr>
      <th>排名</th><th>公司</th><th>最终分</th><th>质量分</th><th>预期差</th><th>关键因子</th>
    </tr></thead><tbody>`;

  ranked.forEach((r, i) => {
    const factors = [];
    const alphaScore = r.alpha?.alpha_score || r.alpha_val || 0;
    const qualityScore = r.overall_score || r.quality_score || 0;
    if (alphaScore >= 7) factors.push('高预期差');
    if (qualityScore >= 7) factors.push('高质量');
    if (r.catalyst?.urgency_score >= 7) factors.push('催化剂紧迫');
    if (r.smart_money?.signal_direction === 'bullish') factors.push('聪明钱看多');
    if (r.alpha?.dim_cap >= 7) factors.push('小市值');
    if (r.alpha?.dim_analyst >= 7) factors.push('低关注');
    if (r.alpha?.dim_volume >= 7) factors.push('量能放大');
    if (r.moat?.overall_moat >= 7) factors.push('护城河深');
    if (r.financial_health >= 7.5) factors.push('财务优良');
    if (r.valuation >= 7.5) factors.push('估值偏低');
    if (alphaScore < 7 && qualityScore < 7 && !factors.length) {
      if (qualityScore >= 5.5) factors.push('质量中等');
      if (alphaScore >= 5.5) factors.push('有预期差');
    }

    html += `<tr data-idx="${i}">
      <td class="col-rank">${r.rank}</td>
      <td class="col-name">${r.supplier?.name || '-'}</td>
      <td class="p3-score-cell" data-dim="final"><span class="score-badge score-badge--final" style="background:${scoreColor(r.final_score)}">${r.final_score.toFixed(2)}</span></td>
      <td class="p3-score-cell" data-dim="quality"><span class="score-badge" style="background:${scoreColor(r.quality_score)}">${r.quality_score.toFixed(1)}</span></td>
      <td class="p3-score-cell" data-dim="alpha"><span class="score-badge score-badge--alpha" style="background:${scoreColor(r.alpha_val)}">${r.alpha_val.toFixed(1)}</span></td>
      <td>${factors.map(f => `<span class="factor-tag">${f}</span>`).join('')}</td>
    </tr>`;
  });

  html += '</tbody></table>';
  container.innerHTML = html;

  // 双击打开抽屉
  if (onDblClick) {
    if (container._p3DblClick) container.removeEventListener('dblclick', container._p3DblClick);
    container._p3DblClick = (e) => {
      const row = e.target.closest('tbody tr');
      if (!row) return;
      const idx = parseInt(row.dataset.idx, 10);
      if (!isNaN(idx) && ranked[idx]) onDblClick(ranked[idx]);
    };
    container.addEventListener('dblclick', container._p3DblClick);
  }

  // 评分 Tooltip
  _attachScoreTooltips(container, ranked);
}

function _attachScoreTooltips(container, ranked) {
  const tip = document.getElementById('score-tooltip');
  if (!tip) return;

  if (container._tipEnter) {
    container.removeEventListener('mouseover', container._tipEnter);
    container.removeEventListener('mouseout', container._tipOut);
  }

  container._tipEnter = (e) => {
    const cell = e.target.closest('.p3-score-cell');
    if (!cell) return;
    const row = cell.closest('tr');
    const idx = parseInt(row?.dataset.idx, 10);
    const sc = ranked[idx];
    if (!sc) return;
    tip.innerHTML = _buildScoreTip(sc, cell.dataset.dim);
    tip.classList.add('visible');
    const rect = cell.getBoundingClientRect();
    tip.style.left = `${Math.min(rect.left, window.innerWidth - 380)}px`;
    tip.style.top = `${rect.bottom + 6}px`;
  };
  container._tipOut = (e) => {
    if (e.target.closest('.p3-score-cell')) {
      tip.classList.remove('visible');
    }
  };
  container.addEventListener('mouseover', container._tipEnter);
  container.addEventListener('mouseout', container._tipOut);
}

function _buildScoreTip(sc, dim) {
  const name = sc.supplier?.name || '-';
  if (dim === 'quality') {
    return `<b>${name}</b> 综合质量 ${sc.quality_score.toFixed(1)} 分<br>` +
      `市场地位 ${sc.market_position ?? '-'}、客户验证 ${sc.customer_validation ?? '-'}、` +
      `产能 ${sc.capacity_status ?? '-'}、财务 ${sc.financial_health ?? '-'}、估值 ${sc.valuation ?? '-'}<br>` +
      `<small>quality = 五维均值×0.7 + 护城河×0.3</small>`;
  }
  if (dim === 'alpha') {
    const a = sc.alpha || {};
    const cap = sc.financial_snapshot?.market_cap_yi ?? sc.supplier?.market_cap;
    let t = `<b>${name}</b> 预期差 ${(a.alpha_score || 0).toFixed(1)} 分<br>`;
    t += `市值规模 → ${a.dim_cap ?? '-'}分`;
    if (cap != null) t += `（${cap}亿）`;
    t += `<br>分析师覆盖 → ${a.dim_analyst ?? '-'}分<br>`;
    t += `成交量动量 → ${a.dim_volume ?? '-'}分<br>`;
    t += `近3月涨幅 → ${a.dim_price ?? '-'}分<br>`;
    t += `机构持仓 → ${a.dim_institution ?? '-'}分<br>`;
    t += `<small>关注度 = cap×15% + analyst×20% + vol×25% + price×15% + inst×25%</small>`;
    return t;
  }
  if (dim === 'final') {
    const wQ = sc.final?.quality_weight ?? 0.4;
    const wA = sc.final?.alpha_weight ?? 0.6;
    return `<b>${name}</b> 最终分 ${sc.final_score.toFixed(2)}<br>` +
      `= 质量<sup>${(wQ * 100).toFixed(0)}%</sup> × 预期差<sup>${(wA * 100).toFixed(0)}%</sup><br>` +
      `= ${sc.quality_score.toFixed(1)}^${wQ.toFixed(2)} × ${sc.alpha_val.toFixed(1)}^${wA.toFixed(2)}`;
  }
  return '';
}

/* ── Phase 3: 散点图 ──────────────────────── */
export function renderScatterPlot(ranked) {
  const container = document.getElementById('wiz-chart-scatter');
  if (!container) return;

  if (scatterChart) scatterChart.dispose();
  scatterChart = echarts.init(container);

  const data = ranked.map(r => ({
    value: [r.quality_score, r.alpha_val, r.final_score],
    name: r.supplier?.name || '',
    symbolSize: Math.max(12, r.final_score * 5),
    itemStyle: { color: scoreColor(r.final_score) },
  }));

  const xs = data.map(d => d.value[0]);
  const ys = data.map(d => d.value[1]);
  const pad = 0.2;
  const xRange = Math.max(0.5, Math.max(...xs) - Math.min(...xs));
  const yRange = Math.max(0.5, Math.max(...ys) - Math.min(...ys));
  const xMin = Math.max(0, Math.floor((Math.min(...xs) - xRange * pad) * 10) / 10);
  const xMax = Math.min(10, Math.ceil((Math.max(...xs) + xRange * pad) * 10) / 10);
  const yMin = Math.max(0, Math.floor((Math.min(...ys) - yRange * pad) * 10) / 10);
  const yMax = Math.min(10, Math.ceil((Math.max(...ys) + yRange * pad) * 10) / 10);

  scatterChart.setOption({
    grid: { left: 55, right: 30, top: 20, bottom: 40 },
    xAxis: {
      name: '质量分', min: xMin, max: xMax,
      nameLocation: 'middle', nameGap: 25,
    },
    yAxis: {
      name: '预期差', min: yMin, max: yMax,
      nameLocation: 'middle', nameGap: 35,
    },
    tooltip: {
      formatter: p => `${p.data.name}<br/>质量: ${p.data.value[0].toFixed(1)}<br/>Alpha: ${p.data.value[1].toFixed(1)}<br/>最终: ${p.data.value[2].toFixed(2)}`,
    },
    series: [{
      type: 'scatter', data,
      label: {
        show: true, fontSize: 10, position: 'top',
        formatter: p => p.data.name?.slice(0, 4),
      },
    }],
  });
}

/* ── Phase 3: 五维雷达对比 ───────────────── */
export function renderRadarChart(ranked) {
  const container = document.getElementById('wiz-chart-radar');
  if (!container) return;

  if (radarChart) radarChart.dispose();
  radarChart = echarts.init(container);

  const top = ranked.slice(0, 5);
  const dims = [
    { key: 'market_position', label: '市场地位' },
    { key: 'customer_validation', label: '客户验证' },
    { key: 'capacity_status', label: '产能状况' },
    { key: 'financial_health', label: '财务健康' },
    { key: 'valuation', label: '估值水平' },
  ];
  const colors = ['#6366f1', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6'];

  radarChart.setOption({
    legend: {
      bottom: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 10 },
      data: top.map(r => shortName(r)),
    },
    radar: {
      indicator: dims.map(d => ({ name: d.label, max: 10 })),
      radius: '60%', center: ['50%', '45%'],
      axisName: { fontSize: 10 },
    },
    tooltip: {},
    series: [{
      type: 'radar',
      data: top.map((r, i) => ({
        name: shortName(r),
        value: dims.map(d => r[d.key] || r.dimension_scores?.[d.key.replace('market_position', 'position').replace('customer_validation', 'customer').replace('capacity_status', 'capacity').replace('financial_health', 'financial')] || 0),
        lineStyle: { color: colors[i] },
        itemStyle: { color: colors[i] },
        areaStyle: { opacity: 0.08 },
      })),
    }],
  });
}

/* ── Phase 3: 评分因子横向对比 ───────────── */
export function renderBarCompare(ranked) {
  const container = document.getElementById('wiz-chart-bar');
  if (!container) return;

  if (barChart) barChart.dispose();
  barChart = echarts.init(container);

  const top = ranked.slice(0, 8);
  const names = top.map(r => shortName(r));

  barChart.setOption({
    grid: { left: 50, right: 20, top: 30, bottom: 35 },
    legend: { top: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 10 } },
    xAxis: { type: 'category', data: names, axisLabel: { fontSize: 10, rotate: 15 } },
    yAxis: { type: 'value', max: 10, axisLabel: { fontSize: 10 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => params.map(p => `${p.seriesName}: ${p.value.toFixed(2)}`).join('<br/>'),
    },
    series: [
      {
        name: '质量分', type: 'bar', barGap: '10%',
        data: top.map(r => r.quality_score || r.overall_score || 0),
        itemStyle: { color: '#6366f1' },
      },
      {
        name: '预期差', type: 'bar',
        data: top.map(r => r.alpha_val || r.alpha?.alpha_score || 0),
        itemStyle: { color: '#f59e0b' },
      },
      {
        name: '推荐分', type: 'bar',
        data: top.map(r => r.final_score || 0),
        itemStyle: { color: '#10b981' },
      },
    ],
  });
}

/* ── Phase 3: Alpha 因子拆解 ─────────────── */
export function renderAlphaStack(ranked) {
  const container = document.getElementById('wiz-chart-stack');
  if (!container) return;

  if (stackChart) stackChart.dispose();
  stackChart = echarts.init(container);

  const top = ranked.slice(0, 8);
  const names = top.map(r => shortName(r));

  const getAlpha = (r) => r.alpha || r.supplier_alpha || {};
  const baseAlphaArr = top.map(r => {
    const a = getAlpha(r);
    if (!a.alpha_score) return 0;
    const raw = a.alpha_score - (a.catalyst_bonus || 0) - (a.trend_bonus || 0) - (a.smart_money_bonus || 0);
    return Math.max(0, +(raw / (a.vp_discount || 1)).toFixed(2));
  });
  const catalystContrib = top.map(r => {
    const a = getAlpha(r);
    return +(a.catalyst_bonus || 0).toFixed(2);
  });

  stackChart.setOption({
    grid: { left: 50, right: 20, top: 30, bottom: 35 },
    legend: { top: 0, itemWidth: 10, itemHeight: 10, textStyle: { fontSize: 10 } },
    xAxis: { type: 'category', data: names, axisLabel: { fontSize: 10, rotate: 15 } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10 } },
    tooltip: {
      trigger: 'axis',
      formatter: params => {
        let total = 0;
        const lines = params.map(p => { total += p.value; return `${p.marker}${p.seriesName}: ${p.value >= 0 ? '+' : ''}${p.value.toFixed(2)}`; });
        return `${params[0].axisValue}<br/>${lines.join('<br/>')}<br/><b>合计: ${total.toFixed(2)}</b>`;
      },
    },
    series: [
      {
        name: '基础Alpha', type: 'bar', stack: 'alpha',
        data: baseAlphaArr,
        itemStyle: { color: '#6366f1' },
      },
      {
        name: '催化剂加分', type: 'bar', stack: 'alpha',
        data: catalystContrib,
        itemStyle: { color: '#ef4444' },
      },
      {
        name: '趋势加分', type: 'bar', stack: 'alpha',
        data: top.map(r => +(getAlpha(r).trend_bonus || 0).toFixed(2)),
        itemStyle: { color: '#10b981' },
      },
      {
        name: '聪明钱加分', type: 'bar', stack: 'alpha',
        data: top.map(r => +(getAlpha(r).smart_money_bonus || 0).toFixed(2)),
        itemStyle: { color: '#f59e0b' },
      },
    ],
  });
}

function shortName(r) {
  const n = r.supplier?.name || r.name || '';
  return n.length > 6 ? n.slice(0, 6) : n;
}

/* ── Phase 4: 交叉验证表 ─────────────────── */
export async function renderPhase4Table(validations, recommendations, rankedResults) {
  const container = document.getElementById('wiz-p4-table');
  if (!container) return;

  // 已在观察池的 ticker 集合 —— 用于把"加入"按钮替换为"已在观察池"标签
  let inWatchlist = new Set();
  try {
    const wl = await fetch('/api/watchlist').then(r => r.ok ? r.json() : { entries: [] });
    inWatchlist = new Set((wl.entries || []).map(e => e.ticker));
  } catch (_) { /* 拉取失败则全部显示按钮，不阻塞渲染 */ }

  // Phase 4 已重构为 FactCheck Gate：数据在 recommendations（validations 字段已废弃、恒空）
  const recs = (recommendations && recommendations.length) ? recommendations : [];
  if (!recs.length) {
    container.innerHTML = '<p class="empty-text">暂无验证结果</p>';
    return;
  }

  // 推荐加入观察池：推荐分 top3 或事实核查 PASS
  const sortedByFinal = [...recs].sort((a, b) => (b.final_score || 0) - (a.final_score || 0));
  const top3 = new Set(sortedByFinal.slice(0, 3).map(r => r.ticker));
  // 本次分析所属市场（推荐结构不含 market，从 ranked 结果派生；同一分析所有推荐同市场）
  const analysisMarket = (rankedResults || []).map(r => r.supplier?.market || r.market).find(Boolean) || '';
  const recLabel = { PASS: '通过', REVIEW: '存疑', REJECT: '否决' };

  let html = `<table class="data-table">
    <thead><tr>
      <th>公司</th><th>推荐分</th><th>可信度</th><th>事实核查</th><th>观察池</th>
    </tr></thead><tbody>`;

  recs.forEach(rec => {
    const ticker = rec.ticker || '';
    const name = rec.name || rec.supplier_name || ticker;
    const finalScore = rec.final_score || 0;
    const cred = (rec.credibility != null) ? rec.credibility : null;
    const badge = passBadge(rec.pass_fail || 'concern');
    const recTxt = recLabel[rec.recommendation] || rec.recommendation || '';
    const ranked = (rankedResults || []).find(r => (r.supplier?.ticker || r.ticker) === ticker);
    const sector = ranked?.supplier?.sector || '';
    const bottleneck = ranked?.bottleneck_node || '';
    const recommended = top3.has(ticker) || rec.pass_fail === 'pass';
    const analysisId = window.appState?.analysisId || '';

    html += `<tr data-company-ticker="${ticker}" data-company-name="${name}" data-company-market="${ranked?.supplier?.market || ''}" title="双击查看企业详情">
      <td class="col-name">${name}</td>
      <td><span class="score-badge" style="background:${scoreColor(finalScore)}">${finalScore.toFixed(1)}</span></td>
      <td>${cred != null ? `<span class="score-badge score-badge--sm" style="background:${scoreColor(cred)}">${cred.toFixed(1)}</span>` : '—'}</td>
      <td>${badge}${recTxt ? ` <span class="p4-rec-txt">${recTxt}</span>` : ''}</td>
      <td>
        ${inWatchlist.has(ticker)
          ? '<span class="wl-p4-in-badge">已在观察池</span>'
          : `<button class="btn btn-sm wl-p4-add-btn ${recommended ? 'wl-p4-add-recommended' : 'wl-p4-add-normal'}"
          data-ticker="${ticker}"
          data-name="${name}"
          data-score="${finalScore.toFixed(2)}"
          data-sector="${sector}"
          data-bottleneck="${bottleneck}"
          data-market="${ranked?.supplier?.market || analysisMarket || ''}"
          data-analysis-id="${analysisId}">加入观察池</button>`}
      </td></tr>`;
  });

  html += '</tbody></table>';
  container.innerHTML = html;

  // 绑定按钮事件
  container.querySelectorAll('.wl-p4-add-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const ticker = btn.dataset.ticker;
      const name = btn.dataset.name || ticker;
      const score = parseFloat(btn.dataset.score) || 0;
      const sector = btn.dataset.sector || '';
      const bottleneck_node = btn.dataset.bottleneck || '';
      const market = btn.dataset.market || undefined;  // A股需带市场，否则后端按 us_stock 存错池
      const source_analysis_id = btn.dataset.analysisId || null;
      try {
        const res = await fetch('/api/watchlist', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticker, company_name: name, tier: 'track',
            composite_score: score, sector, source: 'phase4', market,
            source_analysis_id: source_analysis_id || undefined,
            bottleneck_node,
          }),
        });
        if (res.ok) {
          btn.textContent = '已加入';
          btn.disabled = true;
          btn.classList.add('wl-p4-add-done');
        } else {
          const err = await res.json().catch(() => ({}));
          btn.textContent = err.detail?.includes('already') ? '已存在' : '失败';
          btn.disabled = true;
        }
      } catch (e) {
        btn.textContent = '失败';
      }
    });
  });
}

/* ── 工具函数 ──────────────────────────────── */
function scoreColor(score) {
  if (score >= 7.5) return '#16a34a';
  if (score >= 5) return '#ca8a04';
  if (score >= 3) return '#ea580c';
  return '#dc2626';
}

function trendBadge(trend) {
  if (!trend || !trend.trend_summary) return '<span class="trend-na">-</span>';
  const acc = trend.revenue_acceleration;
  if (acc > 2) return '<span class="trend-up">↑</span>';
  if (acc < -2) return '<span class="trend-down">↓</span>';
  return '<span class="trend-flat">→</span>';
}

function smBadge(sm) {
  if (!sm || !sm.signal_direction) return '-';
  if (sm.signal_direction === 'bullish') return '<span class="sm-bull">看多</span>';
  if (sm.signal_direction === 'bearish') return '<span class="sm-bear">看空</span>';
  return '<span class="sm-neutral">中性</span>';
}

function passBadge(pf) {
  if (pf === 'pass') return '<span class="cv-pass">推荐</span>';
  if (pf === 'fail') return '<span class="cv-fail">不推荐</span>';
  return '<span class="cv-concern">存疑</span>';
}
