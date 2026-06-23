/**
 * watchlist.js — 观察池前端模块（重构版）
 * 表格/卡片双模式 + 搜索/筛选/排序 + 详情抽屉 + UZI 分析
 */

const API = '/api/watchlist';

const wlState = {
  entries: [],
  counts: { focus: 0, normal: 0, track: 0 },
  total: 0,
  budget: null,
  refreshing: false,
  viewMode: 'table',
  filterTier: 'all',
  sortBy: 'tier',
  searchQuery: '',
  drawerEntryId: null,
  drawerTab: 'info',
  uziRunning: null,
  pipeStatuses: [],
};

/* ── API helpers ──────────────────────────────────────── */

async function apiFetch(path, opts = {}) {
  const url = `${API}${path}`;
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function loadWatchlist() {
  try {
    const data = await apiFetch('');
    wlState.entries = data.entries || [];
    wlState.counts = data.counts || { focus: 0, normal: 0, track: 0 };
    wlState.total = data.total || 0;
    render();
  } catch (e) {
    console.error('Failed to load watchlist:', e);
  }
}

async function loadBudget() {
  try {
    wlState.budget = await apiFetch('/budget');
    renderBudget();
  } catch (e) {
    console.error('Failed to load budget:', e);
  }
}

async function loadPipelineStatus() {
  try {
    const data = await apiFetch('/pipeline-status');
    wlState.pipeStatuses = data.pipelines || [];
    renderPipeStatus();
  } catch (e) {
    console.error('Failed to load pipeline status:', e);
  }
}

/* ── Filtering & sorting ──────────────────────────────── */

function getFilteredEntries() {
  let items = [...wlState.entries];
  if (wlState.filterTier !== 'all') {
    items = items.filter(e => e.tier === wlState.filterTier);
  }
  if (wlState.searchQuery) {
    const q = wlState.searchQuery.toLowerCase();
    items = items.filter(e =>
      e.ticker.toLowerCase().includes(q) ||
      (e.company_name || '').toLowerCase().includes(q) ||
      (e.company_name_cn || '').toLowerCase().includes(q)
    );
  }
  const tierOrder = { focus: 0, normal: 1, track: 2 };
  switch (wlState.sortBy) {
    case 'tier':
      items.sort((a, b) => (tierOrder[a.tier] ?? 3) - (tierOrder[b.tier] ?? 3));
      break;
    case 'change': {
      items.sort((a, b) => {
        const ca = a.latest_snapshot?.change_pct ?? -999;
        const cb = b.latest_snapshot?.change_pct ?? -999;
        return cb - ca;
      });
      break;
    }
    case 'score':
      items.sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0));
      break;
    case 'name':
      items.sort((a, b) => a.ticker.localeCompare(b.ticker));
      break;
  }
  return items;
}

/* ── Rendering ────────────────────────────────────────── */

function render() {
  const countBadge = document.getElementById('wl-count-badge');
  if (countBadge) countBadge.textContent = `${wlState.total} / 24`;

  const emptyEl = document.getElementById('wl-empty');
  const tableView = document.getElementById('wl-table-view');
  const cardsView = document.getElementById('wl-cards-view');

  if (wlState.total === 0) {
    if (emptyEl) emptyEl.style.display = 'block';
    if (tableView) tableView.style.display = 'none';
    if (cardsView) cardsView.style.display = 'none';
    return;
  }

  if (emptyEl) emptyEl.style.display = 'none';

  if (wlState.viewMode === 'table') {
    if (tableView) tableView.style.display = 'block';
    if (cardsView) cardsView.style.display = 'none';
    renderTable();
  } else {
    if (tableView) tableView.style.display = 'none';
    if (cardsView) cardsView.style.display = 'block';
    renderCards();
  }
}

function renderTable() {
  const tbody = document.getElementById('wl-table-body');
  if (!tbody) return;

  const items = getFilteredEntries();

  if (items.length === 0) {
    tbody.innerHTML = '<tr class="wl-table-empty"><td colspan="10">无匹配结果</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(entry => {
    const snap = entry.latest_snapshot;
    const price = snap ? `$${Number(snap.close).toFixed(2)}` : '--';
    const changePct = snap?.change_pct;
    const changeStr = changePct != null
      ? `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%` : '--';
    const changeClass = changePct != null
      ? (changePct >= 0 ? 'wl-change-up' : 'wl-change-down') : '';
    const rsi = snap?.rsi_14 != null ? snap.rsi_14.toFixed(0) : '--';
    const macd = getMacdSignal(snap);
    const score = entry.composite_score ? entry.composite_score.toFixed(1) : '--';
    const scoreClass = entry.composite_score
      ? (entry.composite_score >= 7 ? 'score-high' : entry.composite_score >= 5 ? 'score-mid' : 'score-low')
      : '';
    const name = entry.company_name_cn || entry.company_name || entry.ticker;

    return `
      <tr data-id="${entry.id}" data-ticker="${entry.ticker}">
        <td><span class="wl-tier-badge wl-tier-badge--${entry.tier}"><span class="wl-tier-dot wl-dot-${entry.tier}"></span>${tierLabel(entry.tier)}</span></td>
        <td style="font-weight:600;color:var(--accent)">${entry.ticker}</td>
        <td>${name}</td>
        <td class="col-num">${price}</td>
        <td class="col-num ${changeClass}">${changeStr}</td>
        <td class="col-num">${rsi}</td>
        <td class="col-num">${macd.html}</td>
        <td class="col-num wl-score-cell ${scoreClass}">${score}</td>
        <td style="font-size:var(--fs-xs);color:var(--muted)">${entry.sector || ''}</td>
        <td>
          <div class="wl-row-actions">
            <select class="wl-row-tier-select" data-id="${entry.id}" style="font-size:11px;padding:2px 4px;border:1px solid var(--border);border-radius:4px;background:var(--surface);">
              <option value="focus" ${entry.tier === 'focus' ? 'selected' : ''}>重点</option>
              <option value="normal" ${entry.tier === 'normal' ? 'selected' : ''}>一般</option>
              <option value="track" ${entry.tier === 'track' ? 'selected' : ''}>跟踪</option>
            </select>
            <button class="wl-row-btn wl-row-btn--danger" data-id="${entry.id}" data-action="delete" title="移除">✕</button>
          </div>
        </td>
      </tr>`;
  }).join('');
}

function renderCards() {
  const tierLimits = { focus: 6, normal: 6, track: 12 };
  for (const tier of ['focus', 'normal', 'track']) {
    const grid = document.getElementById(`wl-grid-${tier}`);
    const countEl = document.getElementById(`wl-tier-count-${tier}`);
    if (!grid) continue;

    const items = wlState.entries.filter(e => e.tier === tier);
    if (countEl) countEl.textContent = `${items.length} / ${tierLimits[tier]}`;

    if (items.length === 0) {
      grid.innerHTML = '<div class="wl-card-empty">暂无股票</div>';
      continue;
    }
    grid.innerHTML = items.map(entry => renderCard(entry)).join('');
  }
}

function renderCard(entry) {
  const snap = entry.latest_snapshot;
  const price = snap ? `$${Number(snap.close).toFixed(2)}` : '--';
  const changePct = snap?.change_pct;
  const changeStr = changePct != null
    ? `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%` : '';
  const changeClass = changePct != null
    ? (changePct >= 0 ? 'wl-up' : 'wl-down') : '';
  const rsi = snap?.rsi_14 != null ? `RSI ${snap.rsi_14.toFixed(0)}` : '';
  const score = entry.composite_score ? entry.composite_score.toFixed(1) : '--';

  return `
    <div class="wl-card" data-id="${entry.id}" data-ticker="${entry.ticker}">
      <div class="wl-card-header">
        <span class="wl-card-ticker">${entry.ticker}</span>
        <span class="wl-card-score">${score}</span>
      </div>
      <div class="wl-card-name">${entry.company_name_cn || entry.company_name}</div>
      <div class="wl-card-price">
        <span class="wl-card-price-val">${price}</span>
        <span class="wl-card-change ${changeClass}">${changeStr}</span>
      </div>
      <div class="wl-card-indicators">
        <span>${rsi}</span>
        <span class="wl-card-sector">${entry.sector || ''}</span>
      </div>
      <div class="wl-card-actions">
        <button class="wl-card-btn wl-card-btn-tier" data-id="${entry.id}" title="切换层级">▲▼</button>
        <button class="wl-card-btn wl-card-btn-remove" data-id="${entry.id}" title="移除">✕</button>
      </div>
    </div>`;
}

function renderBudget() {
  const b = wlState.budget;
  if (!b) return;
  const fill = document.getElementById('wl-budget-fill');
  const text = document.getElementById('wl-budget-text');
  if (!fill || !text) return;

  const dailyCost = b.daily_cost || 0;
  const dailyLimit = b.daily_limit || 2.0;
  const pct = Math.min((dailyCost / dailyLimit) * 100, 100);

  fill.style.width = `${pct}%`;
  fill.className = 'wl-budget-fill' +
    (pct > 90 ? ' wl-budget-danger' : pct > 70 ? ' wl-budget-warn' : '');
  text.textContent = `$${dailyCost.toFixed(2)} / $${dailyLimit.toFixed(2)}`;
}

function renderPipeStatus() {
  const el = document.getElementById('wl-pipe-summary');
  if (!el) return;
  if (wlState.pipeStatuses.length === 0) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = wlState.pipeStatuses.map(p => {
    const ago = p.last_run_at ? timeAgo(p.last_run_at) : '未运行';
    const dotClass = p.last_status === 'success' ? 'ok'
      : p.last_status === 'error' ? 'err' : 'stale';
    const label = { price: '价格', news: '新闻', sec: 'SEC', options: '期权' }[p.pipeline_name] || p.pipeline_name;
    return `<span><span class="wl-pipe-dot wl-pipe-dot--${dotClass}"></span>${label}: ${ago}</span>`;
  }).join('');
}

/* ── Helpers ──────────────────────────────────────────── */

function tierLabel(tier) {
  return { focus: '重点', normal: '一般', track: '跟踪' }[tier] || tier;
}

function getMacdSignal(snap) {
  if (!snap || snap.macd == null || snap.macd_signal == null) return { html: '<span class="wl-macd-flat">--</span>' };
  const hist = snap.macd_hist ?? (snap.macd - snap.macd_signal);
  if (hist > 0) return { html: '<span class="wl-macd-bull">金叉</span>' };
  if (hist < 0) return { html: '<span class="wl-macd-bear">死叉</span>' };
  return { html: '<span class="wl-macd-flat">平</span>' };
}

function timeAgo(isoStr) {
  if (!isoStr) return '未知';
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${mins}分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}小时前`;
  const days = Math.floor(hours / 24);
  return `${days}天前`;
}

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ── Drawer ───────────────────────────────────────────── */

function openDrawer(entryId) {
  const entry = wlState.entries.find(e => e.id === entryId);
  if (!entry) return;

  wlState.drawerEntryId = entryId;
  wlState.drawerTab = 'info';

  const drawer = document.getElementById('wl-drawer');
  const overlay = document.getElementById('wl-drawer-overlay');
  if (!drawer || !overlay) return;

  document.getElementById('wl-drawer-name').textContent = entry.company_name_cn || entry.company_name;
  document.getElementById('wl-drawer-ticker').textContent = entry.ticker;

  const snap = entry.latest_snapshot;
  const priceEl = document.getElementById('wl-drawer-price');
  const changeEl = document.getElementById('wl-drawer-change');
  if (snap) {
    priceEl.textContent = `$${Number(snap.close).toFixed(2)}`;
    const cp = snap.change_pct;
    if (cp != null) {
      changeEl.textContent = `${cp >= 0 ? '+' : ''}${cp.toFixed(2)}%`;
      changeEl.className = 'wl-drawer-change ' + (cp >= 0 ? 'wl-change-up' : 'wl-change-down');
    } else {
      changeEl.textContent = '';
    }
  } else {
    priceEl.textContent = '';
    changeEl.textContent = '';
  }

  const tierEl = document.getElementById('wl-drawer-tier');
  tierEl.className = `wl-drawer-tier-badge wl-tier-badge wl-tier-badge--${entry.tier}`;
  tierEl.innerHTML = `<span class="wl-tier-dot wl-dot-${entry.tier}"></span>${tierLabel(entry.tier)}`;

  drawer.style.display = 'flex';
  overlay.style.display = 'block';
  requestAnimationFrame(() => {
    drawer.classList.add('drawer-open');
  });

  setDrawerTab('info');
  loadDrawerTabData(entry, 'info');
}

function closeDrawer() {
  const drawer = document.getElementById('wl-drawer');
  const overlay = document.getElementById('wl-drawer-overlay');
  if (drawer) drawer.classList.remove('drawer-open');
  setTimeout(() => {
    if (drawer) drawer.style.display = 'none';
    if (overlay) overlay.style.display = 'none';
  }, 300);
  wlState.drawerEntryId = null;
}

function setDrawerTab(tab) {
  wlState.drawerTab = tab;
  document.querySelectorAll('.wl-drawer-tab').forEach(t => {
    t.classList.toggle('active', t.dataset.tab === tab);
  });
  document.querySelectorAll('.wl-tab-pane').forEach(p => {
    p.classList.toggle('active', p.id === `wl-tab-${tab}`);
  });
}

async function loadDrawerTabData(entry, tab) {
  switch (tab) {
    case 'info':
      await loadInfoTab(entry);
      break;
    case 'price':
      await loadPriceTab(entry);
      break;
    case 'news':
      await loadNewsTab(entry);
      break;
    case 'capital':
      await loadCapitalTab(entry);
      break;
    case 'uzi':
      await loadUziTab(entry);
      break;
  }
}

/* ── Info Tab (基本信息) ──────────────────────────────── */

let _sourceScorecardCache = {};

async function fetchSourceScorecard(entryId) {
  if (_sourceScorecardCache[entryId]) return _sourceScorecardCache[entryId];
  try {
    const data = await apiFetch(`/${entryId}/source-scorecard`);
    _sourceScorecardCache[entryId] = data;
    return data;
  } catch { return null; }
}

async function loadInfoTab(entry) {
  const pane = document.getElementById('wl-tab-info');
  if (!pane) return;

  pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:300px"></div>';

  let html = '';
  const addedDate = entry.added_at ? new Date(entry.added_at).toLocaleString('zh-CN') : '未知';
  const isPhase4 = entry.source === 'phase4';
  const sourceLabel = isPhase4 ? '系统推荐（Phase 4 产业链分析）' : '手动添加';

  html += `<div class="wl-info-meta">
    <div class="wl-info-meta-row"><span class="wl-info-meta-label">加入时间</span><span>${addedDate}</span></div>
    <div class="wl-info-meta-row"><span class="wl-info-meta-label">来源</span><span class="wl-info-source-badge ${isPhase4 ? 'wl-info-source--phase4' : 'wl-info-source--manual'}">${sourceLabel}</span></div>
  </div>`;

  if (isPhase4) {
    const src = await fetchSourceScorecard(entry.id);
    if (src && src.scorecard) {
      html += buildRecommendationReason(src, entry);
      html += buildScorecardDetail(src.scorecard, entry);
    } else {
      html += '<div class="wl-info-reason"><p style="color:var(--muted)">原始分析数据不可用（分析记录可能已清理）</p></div>';
    }
  } else {
    html += '<div class="wl-info-reason"><p style="color:var(--muted)">手动添加的股票暂无系统推荐信息。可使用 UZI 分析获取深度评估。</p></div>';
  }

  html += `<div class="wl-info-section"><h4>股价走势</h4><div id="wl-info-kline" style="height:280px;width:100%"></div></div>`;

  pane.innerHTML = html;

  const market = entry.market || 'us_stock';
  try {
    const kdata = await fetch(`/api/stock/${encodeURIComponent(entry.ticker)}/kline?market=${market}`).then(r => r.ok ? r.json() : null);
    const kDom = document.getElementById('wl-info-kline');
    if (kdata?.length && kDom && typeof echarts !== 'undefined') {
      renderInfoKline(kDom, kdata, entry.ticker);
    } else if (kDom) {
      kDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">暂无K线数据</p>';
    }
  } catch {
    const kDom = document.getElementById('wl-info-kline');
    if (kDom) kDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">K线数据加载失败</p>';
  }
}

function buildRecommendationReason(src, entry) {
  const meta = src.analysis_meta || {};
  const cv = src.cross_validation || {};
  const sc = src.scorecard || {};
  const alpha = sc.alpha || {};
  const fin = sc.final || {};
  const rank = src.rank;
  const totalSc = meta.total_scorecards || '?';

  let html = '<div class="wl-info-reason"><h4>推荐原因</h4><div class="wl-info-reason-grid">';

  if (meta.sector || meta.end_product) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">分析赛道</span><span class="wl-info-reason-value">${escHtml(meta.sector || '')} → ${escHtml(meta.end_product || '')} 方向瓶颈分析</span></div>`;
  }
  if (entry.bottleneck_node) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">瓶颈环节</span><span class="wl-info-reason-value">${escHtml(entry.bottleneck_node)}</span></div>`;
  }
  if (rank != null) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">最终排名</span><span class="wl-info-reason-value">第 ${rank} 名 / 共 ${totalSc} 家</span></div>`;
  }
  if (cv.consensus_score != null) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">交叉验证</span><span class="wl-info-reason-value">${cv.consensus_score.toFixed(1)} 分</span></div>`;
  }
  if (alpha.alpha_score != null) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">Alpha 得分</span><span class="wl-info-reason-value">${alpha.alpha_score.toFixed(1)}</span></div>`;
  }
  if (fin.final_score != null) {
    html += `<div class="wl-info-reason-item"><span class="wl-info-reason-label">最终推荐分</span><span class="wl-info-reason-value" style="font-weight:700;color:var(--accent)">${fin.final_score.toFixed(2)}</span></div>`;
  }

  html += '</div></div>';
  return html;
}

function buildScorecardDetail(sc, entry) {
  const supplier = sc.supplier || {};
  const snap = sc.financial_snapshot || {};
  const alpha = sc.alpha || {};
  const moat = sc.moat || {};
  const fin = sc.final || {};
  const q = sc.overall_score || fin.quality_score || 0;
  const a = alpha.alpha_score || fin.alpha_score || 0;
  const f = fin.final_score || entry.composite_score || 0;

  let html = '';

  html += `<div class="wl-info-section wl-info-scores">
    <div class="drawer-score-grid">
      <div class="drawer-score-box"><div class="val val-accent">${f.toFixed(2)}</div><div class="lbl">最终评分</div></div>
      <div class="drawer-score-box"><div class="val val-yellow">${q.toFixed(1)}</div><div class="lbl">质量分</div></div>
      <div class="drawer-score-box"><div class="val val-green">${a.toFixed(1)}</div><div class="lbl">预期差</div></div>
    </div>
  </div>`;

  if (supplier.description) {
    html += `<div class="wl-info-section"><h4>企业简介</h4><p style="font-size:13px;line-height:1.7">${escHtml(supplier.description)}</p></div>`;
  }

  const products = supplier.products || supplier.key_products || [];
  if (products.length) {
    html += `<div class="wl-info-section"><h4>核心产品</h4><div class="wl-info-product-tags">${products.map(p => `<span class="wl-info-product-tag">${escHtml(p)}</span>`).join('')}</div></div>`;
  }

  const capYi = snap.market_cap_yi ?? supplier.market_cap;
  if (capYi != null) {
    html += `<div class="wl-info-section"><h4>市值</h4><p style="font-size:15px;font-weight:600">${capYi.toFixed(1)} 亿</p></div>`;
  }

  const fRows = [];
  if (snap.revenue_yi != null) fRows.push(['营收', `${snap.revenue_yi.toFixed(2)} 亿`]);
  if (snap.revenue_yoy_pct != null) fRows.push(['营收同比', `${snap.revenue_yoy_pct > 0 ? '+' : ''}${snap.revenue_yoy_pct.toFixed(1)}%`]);
  if (snap.net_profit_yi != null) fRows.push(['净利润', `${snap.net_profit_yi.toFixed(2)} 亿`]);
  if (snap.net_profit_yoy_pct != null) fRows.push(['净利同比', `${snap.net_profit_yoy_pct > 0 ? '+' : ''}${snap.net_profit_yoy_pct.toFixed(1)}%`]);
  if (snap.gross_margin_pct != null) fRows.push(['毛利率', `${snap.gross_margin_pct.toFixed(1)}%`]);
  if (snap.roe_pct != null) fRows.push(['ROE', `${snap.roe_pct.toFixed(1)}%`]);
  if (snap.debt_ratio_pct != null) fRows.push(['负债率', `${snap.debt_ratio_pct.toFixed(1)}%`]);
  if (snap.consensus_pe != null) fRows.push(['预期PE', `${snap.consensus_pe.toFixed(1)}x`]);
  if (snap.analyst_report_count != null) fRows.push(['机构覆盖', `${snap.analyst_report_count} 家`]);

  if (fRows.length) {
    html += `<div class="wl-info-section"><h4>财务与市场</h4><div class="fin-grid">${fRows.map(([l, v]) => {
      const isUp = v.includes('+');
      const isDown = v.startsWith('-');
      return `<div class="fin-cell"><span class="fin-label">${l}</span><span class="fin-val${isUp ? ' val-up' : isDown ? ' val-down' : ''}">${v}</span></div>`;
    }).join('')}</div></div>`;
  }

  const dimLabels = { market_position: '市场地位', customer_validation: '客户验证', capacity: '产能状况', financial_health: '财务健康', valuation: '估值水平' };
  const dims = sc.dimension_scores || {};
  const dimKeys = ['market_position', 'customer_validation', 'capacity', 'financial_health', 'valuation'];
  const hasDims = dimKeys.some(k => (dims[k] ?? sc[k]) != null);
  if (hasDims) {
    html += '<div class="wl-info-section"><h4>五维评分</h4><div class="dim-bar-list">';
    dimKeys.forEach(k => {
      const val = dims[k] ?? sc[k] ?? sc.scores?.[k] ?? 0;
      html += `<div class="dim-bar-row"><span class="dim-bar-label">${dimLabels[k]}</span><div class="dim-bar-track"><div class="dim-bar-fill" style="width:${val * 10}%"></div></div><span class="dim-bar-val">${Number(val).toFixed(1)}</span></div>`;
    });
    html += '</div></div>';
  }

  if (alpha.alpha_score != null) {
    const alphaDims = [
      { key: 'dim_cap', label: '市值规模' }, { key: 'dim_analyst', label: '分析师覆盖' },
      { key: 'dim_volume', label: '成交量动量' }, { key: 'dim_price', label: '近期涨幅' },
      { key: 'dim_institution', label: '机构持仓' },
    ];
    html += '<div class="wl-info-section"><h4>预期差分析</h4><div class="dim-bar-list">';
    alphaDims.forEach(d => {
      const val = alpha[d.key] ?? 0;
      html += `<div class="dim-bar-row"><span class="dim-bar-label">${d.label}</span><div class="dim-bar-track"><div class="dim-bar-fill dim-bar-fill--alpha" style="width:${val * 11.1}%"></div></div><span class="dim-bar-val">${Number(val).toFixed(1)}</span></div>`;
    });
    html += '</div></div>';
  }

  if (sc.strengths?.length || sc.weaknesses?.length) {
    html += '<div class="wl-info-section"><h4>优势与风险</h4><div class="drawer-strengths">';
    html += `<div><h5 style="color:var(--success)">优势</h5>${(sc.strengths || []).map(s => `<div class="s-item s-good">✅ ${escHtml(s)}</div>`).join('')}</div>`;
    html += `<div><h5 style="color:var(--danger)">风险</h5>${(sc.weaknesses || sc.risks || []).map(r => `<div class="s-item s-bad">⚠️ ${escHtml(r)}</div>`).join('')}</div>`;
    html += '</div></div>';
  }

  return html;
}

function renderInfoKline(dom, data, title) {
  const chart = echarts.init(dom);
  const dates = data.map(d => d.date);
  const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
  const vols = data.map(d => d.volume);
  const colors = data.map(d => d.close >= d.open ? '#26a69a' : '#ef5350');

  chart.setOption({
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: [
      { left: 60, right: 20, top: 20, height: '55%' },
      { left: 60, right: 20, top: '78%', height: '16%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { fontSize: 10, color: '#888' } },
    ],
    yAxis: [
      { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } }, axisLabel: { fontSize: 10, color: '#888' } },
      { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
    ],
    series: [
      { type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
      { type: 'bar', data: vols.map((v, i) => ({ value: v, itemStyle: { color: colors[i] + '66' } })),
        xAxisIndex: 1, yAxisIndex: 1 },
    ],
  });
  new ResizeObserver(() => chart.resize()).observe(dom);
}

/* ── Price Tab ───────────────────────────────────────── */

async function loadPriceTab(entry) {
  const pane = document.getElementById('wl-tab-price');
  if (!pane) return;

  pane.innerHTML = `
    <div class="wl-price-chart" id="wl-price-chart-container"></div>
    <div class="wl-indicators" id="wl-price-indicators">
      <div class="skeleton skeleton-text" style="height:60px"></div>
      <div class="skeleton skeleton-text" style="height:60px"></div>
      <div class="skeleton skeleton-text" style="height:60px"></div>
    </div>`;

  try {
    const snapshots = await apiFetch(`/${entry.id}/snapshots?days=90`);
    renderPriceChart(snapshots);
    renderIndicators(entry);
  } catch (e) {
    pane.innerHTML = `<p style="color:var(--muted);text-align:center;padding:20px">暂无价格数据</p>`;
  }
}

function renderPriceChart(snapshots) {
  const container = document.getElementById('wl-price-chart-container');
  if (!container || !snapshots || snapshots.length === 0) return;
  if (typeof echarts === 'undefined') {
    container.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px">图表库未加载</p>';
    return;
  }

  const chart = echarts.init(container);
  const dates = snapshots.map(s => s.date);
  const prices = snapshots.map(s => [s.open, s.close, s.low, s.high]);
  const volumes = snapshots.map(s => s.volume || 0);

  chart.setOption({
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: [
      { left: '8%', right: '4%', top: '8%', height: '55%' },
      { left: '8%', right: '4%', top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { fontSize: 10 } },
      { type: 'category', data: dates, gridIndex: 1, show: false },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { type: 'dashed', color: '#eee' } } },
      { scale: true, gridIndex: 1, show: false },
    ],
    series: [
      {
        type: 'candlestick',
        data: prices,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: 'oklch(0.55 0.12 155)',
          color0: 'oklch(0.55 0.15 25)',
          borderColor: 'oklch(0.55 0.12 155)',
          borderColor0: 'oklch(0.55 0.15 25)',
        },
      },
      {
        type: 'bar',
        data: volumes,
        xAxisIndex: 1,
        yAxisIndex: 1,
        itemStyle: { color: 'oklch(0.55 0.15 250 / 0.3)' },
      },
    ],
  });

  const resizeObs = new ResizeObserver(() => chart.resize());
  resizeObs.observe(container);
}

function renderIndicators(entry) {
  const el = document.getElementById('wl-price-indicators');
  if (!el) return;
  const snap = entry.latest_snapshot;
  if (!snap) {
    el.innerHTML = '<p style="color:var(--muted)">暂无技术指标</p>';
    return;
  }

  const rsi = snap.rsi_14 != null ? snap.rsi_14.toFixed(1) : '--';
  const rsiColor = snap.rsi_14 > 70 ? 'score-low' : snap.rsi_14 < 30 ? 'score-high' : '';
  const macdVal = snap.macd != null ? snap.macd.toFixed(3) : '--';
  const macdSig = snap.macd_signal != null ? snap.macd_signal.toFixed(3) : '--';
  const sma20 = snap.sma_20 != null ? `$${snap.sma_20.toFixed(2)}` : '--';
  const sma50 = snap.sma_50 != null ? `$${snap.sma_50.toFixed(2)}` : '--';

  el.innerHTML = `
    <div class="wl-indicator-card">
      <div class="wl-indicator-label">RSI (14)</div>
      <div class="wl-indicator-value ${rsiColor}">${rsi}</div>
    </div>
    <div class="wl-indicator-card">
      <div class="wl-indicator-label">MACD</div>
      <div class="wl-indicator-value" style="font-size:var(--fs-sm)">${macdVal} / ${macdSig}</div>
    </div>
    <div class="wl-indicator-card">
      <div class="wl-indicator-label">SMA 20 / 50</div>
      <div class="wl-indicator-value" style="font-size:var(--fs-sm)">${sma20} / ${sma50}</div>
    </div>`;
}

/* ── News Tab ────────────────────────────────────────── */

async function loadNewsTab(entry) {
  const pane = document.getElementById('wl-tab-news');
  if (!pane) return;

  pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:200px"></div>';

  const [newsResult, srcResult] = await Promise.all([
    apiFetch(`/${entry.id}/news?limit=20`).catch(() => []),
    entry.source === 'phase4' ? fetchSourceScorecard(entry.id) : Promise.resolve(null),
  ]);

  const news = Array.isArray(newsResult) ? newsResult : (newsResult?.news || []);
  const catalystEvents = srcResult?.scorecard?.catalyst?.events || [];

  let html = '';

  html += '<div class="wl-news-section-header"><h4>重要事件（催化剂）</h4></div>';
  if (catalystEvents.length > 0) {
    const urgency = srcResult?.scorecard?.catalyst?.urgency_score;
    const window_ = srcResult?.scorecard?.catalyst?.investment_window;
    html += '<div class="wl-catalyst-list">';
    catalystEvents.forEach(ev => {
      const typeLabel = { policy: '政策', capacity: '产能', technology: '技术', order: '订单', earnings: '业绩' }[ev.event_type] || ev.event_type;
      const conf = ev.confidence != null ? `置信 ${ev.confidence.toFixed(0)}` : '';
      const imp = ev.impact_score != null ? `影响 ${ev.impact_score.toFixed(0)}` : '';
      const meta = [conf, imp].filter(Boolean).join('·');
      html += `<div class="wl-catalyst-item">
        <span class="wl-catalyst-type">[${typeLabel}]</span>
        <div class="wl-catalyst-body">
          <div>${escHtml(ev.description || '')}</div>
          <div class="wl-catalyst-meta">${ev.expected_date ? `预期: ${ev.expected_date}` : ''} ${meta ? `(${meta})` : ''}</div>
        </div>
      </div>`;
    });
    html += '</div>';
    if (urgency != null || window_) {
      html += `<div class="wl-catalyst-footer">`;
      if (urgency != null) html += `<span>紧迫度: <strong>${urgency.toFixed(1)}</strong></span>`;
      if (window_) html += `<span>投资窗口: ${escHtml(window_)}</span>`;
      html += '</div>';
    }
  } else {
    html += '<p style="color:var(--muted);font-size:var(--fs-xs);padding:8px 0">暂无催化剂事件数据</p>';
  }

  html += '<div class="wl-news-section-header" style="margin-top:20px"><h4>实时新闻</h4></div>';
  if (news.length > 0) {
    html += `<div class="wl-news-list">${news.map(n => renderNewsItem(n)).join('')}</div>`;
  } else {
    html += '<p style="color:var(--muted);font-size:var(--fs-xs);padding:8px 0">暂无新闻</p>';
  }

  pane.innerHTML = html;
}

function renderNewsItem(item) {
  const sentiment = item.sentiment || 'neutral';
  const sentLabel = { positive: '正面', neutral: '中性', negative: '负面' }[sentiment] || '中性';
  const sentClass = `wl-news-sentiment--${sentiment}`;
  const ago = timeAgo(item.date);
  const title = escHtml(item.title || '');
  const url = item.source_url ? ` href="${escHtml(item.source_url)}" target="_blank" rel="noopener"` : '';
  const summary = item.summary ? `<div class="wl-news-meta" style="margin-top:4px">${escHtml(item.summary)}</div>` : '';

  return `
    <div class="wl-news-item">
      <span class="wl-news-sentiment ${sentClass}">${sentLabel}</span>
      <div class="wl-news-body">
        <div class="wl-news-title"><a${url}>${title}</a></div>
        <div class="wl-news-meta">${item.source_name || ''} · ${ago}</div>
        ${summary}
      </div>
    </div>`;
}

/* ── Capital Tab ─────────────────────────────────────── */

async function loadCapitalTab(entry) {
  const pane = document.getElementById('wl-tab-capital');
  if (!pane) return;

  pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:200px"></div>';

  try {
    const [insiders, options, filings] = await Promise.all([
      apiFetch(`/${entry.id}/insider-trades?limit=10`).catch(() => []),
      apiFetch(`/${entry.id}/options?limit=5`).catch(() => []),
      apiFetch(`/${entry.id}/filings?limit=10`).catch(() => []),
    ]);

    let html = '';

    html += '<div class="wl-capital-section"><h4>内部人交易</h4>';
    if (insiders.length > 0) {
      html += `<table class="wl-insider-table">
        <thead><tr><th>日期</th><th>内部人</th><th>类型</th><th class="col-num">股数</th><th class="col-num">金额</th></tr></thead>
        <tbody>${insiders.map(t => `
          <tr>
            <td>${t.date || '--'}</td>
            <td>${escHtml(t.insider_name || '')} <span style="color:var(--muted);font-size:10px">${escHtml(t.insider_title || '')}</span></td>
            <td class="${t.transaction_type === 'buy' ? 'wl-trade-buy' : 'wl-trade-sell'}">${t.transaction_type === 'buy' ? '买入' : '卖出'}</td>
            <td class="col-num">${formatNum(t.shares)}</td>
            <td class="col-num">${t.total_value ? '$' + formatNum(t.total_value) : '--'}</td>
          </tr>`).join('')}
        </tbody></table>`;
    } else {
      html += '<p style="color:var(--muted);font-size:var(--fs-xs)">暂无内部人交易记录</p>';
    }
    html += '</div>';

    html += '<div class="wl-capital-section"><h4>期权活动</h4>';
    if (options.length > 0) {
      html += `<table class="wl-options-table">
        <thead><tr><th>日期</th><th class="col-num">P/C比</th><th>异常量</th><th>最大OI</th></tr></thead>
        <tbody>${options.map(o => `
          <tr>
            <td>${o.date || '--'}</td>
            <td class="col-num">${o.put_call_ratio != null ? o.put_call_ratio.toFixed(2) : '--'}</td>
            <td>${o.unusual_volume ? '<span style="color:var(--warning);font-weight:600">是</span>' : '否'}</td>
            <td>${o.max_oi_strike ? `$${o.max_oi_strike} ${o.max_oi_expiry || ''}` : '--'}</td>
          </tr>`).join('')}
        </tbody></table>`;
    } else {
      html += '<p style="color:var(--muted);font-size:var(--fs-xs)">暂无期权数据</p>';
    }
    html += '</div>';

    html += '<div class="wl-capital-section"><h4>SEC 文件</h4>';
    if (filings.length > 0) {
      html += `<table class="wl-sec-table">
        <thead><tr><th>日期</th><th>类型</th><th>链接</th></tr></thead>
        <tbody>${filings.map(f => `
          <tr>
            <td>${f.filed_date || '--'}</td>
            <td><span class="badge" style="font-size:10px">${escHtml(f.filing_type || '')}</span></td>
            <td>${f.url ? `<a href="${escHtml(f.url)}" target="_blank" rel="noopener" style="color:var(--accent);font-size:11px">查看</a>` : '--'}</td>
          </tr>`).join('')}
        </tbody></table>`;
    } else {
      html += '<p style="color:var(--muted);font-size:var(--fs-xs)">暂无SEC文件</p>';
    }
    html += '</div>';

    pane.innerHTML = html;
  } catch (e) {
    pane.innerHTML = '<p style="color:var(--muted);text-align:center;padding:20px">加载资金数据失败</p>';
  }
}

function formatNum(n) {
  if (n == null) return '--';
  return Number(n).toLocaleString();
}

/* ── UZI Tab ─────────────────────────────────────────── */

async function loadUziTab(entry) {
  const pane = document.getElementById('wl-tab-uzi');
  if (!pane) return;

  const isA = (entry.market || '').includes('cn') || /^\d{6}\.(SZ|SH|BJ)$/i.test(entry.ticker);

  pane.innerHTML = `
    <div class="wl-uzi-actions">
      <button class="wl-uzi-btn" data-type="deep-analysis">
        <span class="wl-uzi-btn-icon">🔬</span>
        <div class="wl-uzi-btn-text">
          <div class="wl-uzi-btn-title">深度分析</div>
          <div class="wl-uzi-btn-desc">22维数据 + 估值建模 + 投资者评审</div>
        </div>
      </button>
      <button class="wl-uzi-btn" data-type="investor-panel">
        <span class="wl-uzi-btn-icon">👥</span>
        <div class="wl-uzi-btn-text">
          <div class="wl-uzi-btn-title">投资者评审</div>
          <div class="wl-uzi-btn-desc">15位大佬 · 9大流派投票</div>
        </div>
      </button>
      <button class="wl-uzi-btn${isA ? '' : ' disabled'}" data-type="lhb-analyzer" ${isA ? '' : 'disabled title="仅限A股"'}>
        <span class="wl-uzi-btn-icon">📊</span>
        <div class="wl-uzi-btn-text">
          <div class="wl-uzi-btn-title">龙虎榜分析</div>
          <div class="wl-uzi-btn-desc">${isA ? '游资席位 · 机构博弈' : '仅限A股'}</div>
        </div>
      </button>
      <button class="wl-uzi-btn" data-type="trap-detector">
        <span class="wl-uzi-btn-icon">🛡</span>
        <div class="wl-uzi-btn-text">
          <div class="wl-uzi-btn-title">杀猪盘检测</div>
          <div class="wl-uzi-btn-desc">8信号扫描 · 风险评级</div>
        </div>
      </button>
    </div>
    <div id="wl-uzi-progress-area"></div>
    <div id="wl-uzi-result-area"></div>
    <div class="wl-uzi-history" id="wl-uzi-history">
      <h4>历史分析</h4>
      <div id="wl-uzi-history-list"><span style="color:var(--muted);font-size:var(--fs-xs)">暂无历史分析</span></div>
    </div>`;

  pane.querySelectorAll('.wl-uzi-btn:not([disabled])').forEach(btn => {
    btn.addEventListener('click', () => {
      const type = btn.dataset.type;
      triggerUziAnalysis(entry, type);
    });
  });

  loadUziHistory(entry);
}

async function loadUziHistory(entry) {
  const listEl = document.getElementById('wl-uzi-history-list');
  if (!listEl) return;

  try {
    const history = await apiFetch(`/${entry.id}/uzi/history`);
    if (!history || history.length === 0) {
      listEl.innerHTML = '<span style="color:var(--muted);font-size:var(--fs-xs)">暂无历史分析</span>';
      return;
    }
    listEl.innerHTML = history.map(h => {
      const typeLabel = {
        'deep-analysis': '深度分析', 'investor-panel': '投资者评审',
        'lhb-analyzer': '龙虎榜', 'trap-detector': '杀猪盘检测',
      }[h.analysis_type] || h.analysis_type;
      const resultBadge = h.trap_level || h.signal || (h.score != null ? h.score.toFixed(0) + '分' : '');
      return `
        <div class="wl-uzi-history-item">
          <span class="wl-uzi-history-type">${typeLabel}</span>
          <span class="wl-uzi-history-date">${h.completed_at ? timeAgo(h.completed_at) : h.status}</span>
          <span class="wl-uzi-history-result">${resultBadge}</span>
          ${h.status === 'completed' ? `<span class="wl-uzi-history-link" data-analysis-id="${h.id}">查看</span>` : ''}
        </div>`;
    }).join('');
  } catch (e) {
    // UZI history endpoint may not exist yet
  }
}

async function triggerUziAnalysis(entry, analysisType) {
  const progressArea = document.getElementById('wl-uzi-progress-area');
  if (!progressArea || wlState.uziRunning) return;

  wlState.uziRunning = analysisType;

  const typeLabel = {
    'deep-analysis': '深度分析', 'investor-panel': '投资者评审',
    'lhb-analyzer': '龙虎榜分析', 'trap-detector': '杀猪盘检测',
  }[analysisType] || analysisType;

  const btn = document.querySelector(`.wl-uzi-btn[data-type="${analysisType}"]`);
  if (btn) btn.classList.add('running');

  progressArea.innerHTML = `
    <div class="wl-uzi-progress">
      <div class="wl-uzi-progress-title">${typeLabel} 进行中...</div>
      <div class="wl-uzi-progress-bar"><div class="wl-uzi-progress-fill" id="wl-uzi-pbar" style="width:5%"></div></div>
      <div class="wl-uzi-progress-text" id="wl-uzi-ptext">初始化...</div>
    </div>`;

  try {
    const res = await fetch(`${API}/${entry.id}/uzi/${analysisType}`, { method: 'POST' });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6));
            handleUziEvent(data);
          } catch {}
        }
      }
    }
  } catch (e) {
    progressArea.innerHTML = `<div class="wl-uzi-progress" style="border-color:var(--danger)">
      <div class="wl-uzi-progress-title" style="color:var(--danger)">分析失败</div>
      <div class="wl-uzi-progress-text">${escHtml(e.message)}</div>
    </div>`;
  }

  wlState.uziRunning = null;
  if (btn) btn.classList.remove('running');
}

function handleUziEvent(data) {
  const pbar = document.getElementById('wl-uzi-pbar');
  const ptext = document.getElementById('wl-uzi-ptext');
  const resultArea = document.getElementById('wl-uzi-result-area');

  if (data.progress != null && pbar) {
    pbar.style.width = `${Math.min(data.progress, 100)}%`;
  }
  if (data.message && ptext) {
    ptext.textContent = data.message;
  }
  if (data.status === 'completed' && data.result) {
    const progressArea = document.getElementById('wl-uzi-progress-area');
    if (progressArea) progressArea.innerHTML = '';
    if (resultArea) renderUziResult(resultArea, data.analysis_type, data.result);
    const entry = wlState.entries.find(e => e.id === wlState.drawerEntryId);
    if (entry) loadUziHistory(entry);
  }
}

function renderUziResult(container, type, result) {
  switch (type) {
    case 'deep-analysis':
      renderDeepAnalysisResult(container, result);
      break;
    case 'investor-panel':
      renderInvestorPanelResult(container, result);
      break;
    case 'trap-detector':
      renderTrapResult(container, result);
      break;
    default:
      container.innerHTML = `<pre style="font-size:var(--fs-xs);overflow:auto;max-height:400px">${escHtml(JSON.stringify(result, null, 2))}</pre>`;
  }
}

function renderDeepAnalysisResult(container, result) {
  const dims = result.dimensions || {};
  const dimKeys = Object.keys(dims).sort();
  const scores = dimKeys.map(k => dims[k]?.score ?? 0);
  const labels = dimKeys.map(k => dims[k]?.label || k.replace(/^\d+_/, ''));
  const overall = result.overall_score ?? '--';

  let html = `
    <div class="wl-uzi-result">
      <div class="wl-uzi-result-header">
        <span class="wl-uzi-result-title">深度分析结果</span>
        <span style="font-size:var(--fs-lg);font-weight:700">${overall}</span>
      </div>
      <div class="wl-uzi-radar" id="wl-uzi-radar-chart"></div>
      <div class="wl-uzi-score-grid">
        ${dimKeys.map(k => {
          const d = dims[k];
          const s = d?.score ?? 0;
          const cls = s >= 7 ? 'score-high' : s >= 5 ? 'score-mid' : 'score-low';
          return `<div class="wl-uzi-score-item"><div class="wl-uzi-score-label">${escHtml(d?.label || k)}</div><div class="wl-uzi-score-value ${cls}">${s}</div></div>`;
        }).join('')}
      </div>`;

  if (result.risks && result.risks.length > 0) {
    html += `<h4 style="font-size:var(--fs-sm);font-weight:600;margin:12px 0 8px">风险提示</h4>
      <ul style="padding-left:18px;font-size:var(--fs-xs);color:var(--muted)">${result.risks.map(r => `<li>${escHtml(r)}</li>`).join('')}</ul>`;
  }
  html += '</div>';
  container.innerHTML = html;

  if (typeof echarts !== 'undefined' && dimKeys.length >= 3) {
    const radarEl = document.getElementById('wl-uzi-radar-chart');
    if (radarEl) {
      const chart = echarts.init(radarEl);
      chart.setOption({
        radar: {
          indicator: labels.map(l => ({ name: l, max: 10 })),
          radius: '65%',
          axisName: { fontSize: 10, color: '#888' },
        },
        series: [{
          type: 'radar',
          data: [{ value: scores, areaStyle: { opacity: 0.15 } }],
          lineStyle: { color: 'oklch(0.55 0.15 250)' },
          itemStyle: { color: 'oklch(0.55 0.15 250)' },
          areaStyle: { color: 'oklch(0.55 0.15 250)' },
        }],
      });
      new ResizeObserver(() => chart.resize()).observe(radarEl);
    }
  }
}

function renderInvestorPanelResult(container, result) {
  const consensus = result.panel_consensus ?? '--';
  const distribution = result.signal_distribution || {};
  const bull = distribution.bullish || 0;
  const neut = distribution.neutral || 0;
  const bear = distribution.bearish || 0;
  const total = bull + neut + bear || 1;

  container.innerHTML = `
    <div class="wl-uzi-result">
      <div class="wl-uzi-result-header">
        <span class="wl-uzi-result-title">投资者评审结果</span>
        <span style="font-size:var(--fs-lg);font-weight:700">${typeof consensus === 'number' ? consensus.toFixed(1) + '%' : consensus}</span>
      </div>
      <div class="wl-panel-vote-chart" id="wl-panel-vote-chart"></div>
      <div class="wl-panel-group-bars">
        <div class="wl-panel-group">
          <span class="wl-panel-group-name">看多</span>
          <div class="wl-panel-group-bar"><div class="wl-panel-bar-bull" style="width:${(bull/total*100).toFixed(1)}%"></div></div>
          <span style="font-size:11px;min-width:30px">${bull}</span>
        </div>
        <div class="wl-panel-group">
          <span class="wl-panel-group-name">中立</span>
          <div class="wl-panel-group-bar"><div class="wl-panel-bar-neut" style="width:${(neut/total*100).toFixed(1)}%"></div></div>
          <span style="font-size:11px;min-width:30px">${neut}</span>
        </div>
        <div class="wl-panel-group">
          <span class="wl-panel-group-name">看空</span>
          <div class="wl-panel-group-bar"><div class="wl-panel-bar-bear" style="width:${(bear/total*100).toFixed(1)}%"></div></div>
          <span style="font-size:11px;min-width:30px">${bear}</span>
        </div>
      </div>
    </div>`;

  if (typeof echarts !== 'undefined') {
    const chartEl = document.getElementById('wl-panel-vote-chart');
    if (chartEl) {
      const chart = echarts.init(chartEl);
      chart.setOption({
        series: [{
          type: 'pie',
          radius: ['40%', '70%'],
          data: [
            { name: '看多', value: bull, itemStyle: { color: 'oklch(0.55 0.12 155)' } },
            { name: '中立', value: neut, itemStyle: { color: 'oklch(0.6 0.14 85)' } },
            { name: '看空', value: bear, itemStyle: { color: 'oklch(0.55 0.15 25)' } },
          ],
          label: { formatter: '{b}: {c} ({d}%)', fontSize: 12 },
        }],
      });
      new ResizeObserver(() => chart.resize()).observe(chartEl);
    }
  }
}

function renderTrapResult(container, result) {
  const level = result.trap_level || '未知';
  const score = result.trap_score ?? '--';
  const signals = result.signals_hit || [];

  const levelClass = level.includes('安全') ? 'safe'
    : level.includes('注意') ? 'caution'
    : level.includes('警惕') ? 'warning' : 'danger';

  container.innerHTML = `
    <div class="wl-uzi-result">
      <div class="wl-uzi-result-header">
        <span class="wl-uzi-result-title">杀猪盘检测结果</span>
        <span class="wl-trap-badge wl-trap-badge--${levelClass}">${level}</span>
      </div>
      <div class="wl-trap-signals">
        ${signals.length > 0 ? signals.map(s => `
          <div class="wl-trap-signal wl-trap-signal--fail">
            <span class="wl-trap-signal-icon">⚠</span>
            <div>
              <div style="font-weight:600">${escHtml(s.name || '')}</div>
              <div style="color:var(--muted)">${escHtml(s.evidence || '')}</div>
            </div>
          </div>`).join('')
        : '<div class="wl-trap-signal wl-trap-signal--pass"><span class="wl-trap-signal-icon">✓</span><span>未检测到异常推广信号</span></div>'}
      </div>
      ${result.recommendation ? `<p style="margin-top:12px;font-size:var(--fs-xs);color:var(--muted)">${escHtml(result.recommendation)}</p>` : ''}
    </div>`;
}

/* ── Add stock ────────────────────────────────────────── */

function initAddPanel() {
  const btnAdd = document.getElementById('wl-btn-add');
  const panel = document.getElementById('wl-add-panel');
  const btnSubmit = document.getElementById('wl-add-submit');
  const btnCancel = document.getElementById('wl-add-cancel');
  if (!btnAdd || !panel) return;

  btnAdd.addEventListener('click', () => {
    panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
  });
  btnCancel.addEventListener('click', () => { panel.style.display = 'none'; });

  btnSubmit.addEventListener('click', async () => {
    const ticker = document.getElementById('wl-add-ticker').value.trim().toUpperCase();
    const name = document.getElementById('wl-add-name').value.trim() || ticker;
    const tier = document.getElementById('wl-add-tier').value;
    if (!ticker) return;

    try {
      await apiFetch('', {
        method: 'POST',
        body: JSON.stringify({ ticker, company_name: name, tier }),
      });
      panel.style.display = 'none';
      document.getElementById('wl-add-ticker').value = '';
      document.getElementById('wl-add-name').value = '';
      await loadWatchlist();
    } catch (e) {
      alert(`添加失败: ${e.message}`);
    }
  });
}

/* ── Table & card actions ─────────────────────────────── */

function initTableActions() {
  const tableView = document.getElementById('wl-table-view');
  if (!tableView) return;

  tableView.addEventListener('click', async (e) => {
    const deleteBtn = e.target.closest('.wl-row-btn--danger');
    if (deleteBtn) {
      e.stopPropagation();
      const id = deleteBtn.dataset.id;
      if (!confirm('确定移除该股票？')) return;
      try {
        await apiFetch(`/${id}`, { method: 'DELETE' });
        await loadWatchlist();
        if (wlState.drawerEntryId === id) closeDrawer();
      } catch (err) {
        alert(`移除失败: ${err.message}`);
      }
      return;
    }

    if (e.target.closest('.wl-row-tier-select')) return;

    const row = e.target.closest('tr[data-id]');
    if (row) {
      openDrawer(row.dataset.id);
    }
  });

  tableView.addEventListener('change', async (e) => {
    const select = e.target.closest('.wl-row-tier-select');
    if (select) {
      const id = select.dataset.id;
      const newTier = select.value;
      try {
        await apiFetch(`/${id}`, {
          method: 'PATCH',
          body: JSON.stringify({ tier: newTier }),
        });
        await loadWatchlist();
      } catch (err) {
        alert(`更新失败: ${err.message}`);
        await loadWatchlist();
      }
    }
  });
}

function initCardActions() {
  const cardsView = document.getElementById('wl-cards-view');
  if (!cardsView) return;

  cardsView.addEventListener('click', async (e) => {
    const removeBtn = e.target.closest('.wl-card-btn-remove');
    if (removeBtn) {
      const id = removeBtn.dataset.id;
      if (!confirm('确定移除该股票？')) return;
      try {
        await apiFetch(`/${id}`, { method: 'DELETE' });
        await loadWatchlist();
      } catch (err) {
        alert(`移除失败: ${err.message}`);
      }
      return;
    }

    const tierBtn = e.target.closest('.wl-card-btn-tier');
    if (tierBtn) {
      const id = tierBtn.dataset.id;
      const entry = wlState.entries.find(e => e.id === id);
      if (!entry) return;
      const tiers = ['focus', 'normal', 'track'];
      const nextTier = tiers[(tiers.indexOf(entry.tier) + 1) % tiers.length];
      try {
        await apiFetch(`/${id}`, { method: 'PATCH', body: JSON.stringify({ tier: nextTier }) });
        await loadWatchlist();
      } catch (err) {
        alert(`更新失败: ${err.message}`);
      }
      return;
    }

    const card = e.target.closest('.wl-card');
    if (card && !e.target.closest('.wl-card-actions')) {
      openDrawer(card.dataset.id);
    }
  });
}

/* ── Toolbar controls ─────────────────────────────────── */

function initToolbar() {
  const searchInput = document.getElementById('wl-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      wlState.searchQuery = searchInput.value.trim();
      render();
    });
  }

  const filterSelect = document.getElementById('wl-filter-tier');
  if (filterSelect) {
    filterSelect.addEventListener('change', () => {
      wlState.filterTier = filterSelect.value;
      render();
    });
  }

  const sortSelect = document.getElementById('wl-sort');
  if (sortSelect) {
    sortSelect.addEventListener('change', () => {
      wlState.sortBy = sortSelect.value;
      render();
    });
  }

  const viewToggle = document.getElementById('wl-view-toggle');
  if (viewToggle) {
    viewToggle.addEventListener('click', (e) => {
      const btn = e.target.closest('.wl-view-btn');
      if (!btn) return;
      wlState.viewMode = btn.dataset.mode;
      viewToggle.querySelectorAll('.wl-view-btn').forEach(b => b.classList.toggle('active', b === btn));
      render();
    });
  }
}

/* ── Drawer events ────────────────────────────────────── */

function initDrawer() {
  const closeBtn = document.getElementById('wl-drawer-close');
  const overlay = document.getElementById('wl-drawer-overlay');
  if (closeBtn) closeBtn.addEventListener('click', closeDrawer);
  if (overlay) overlay.addEventListener('click', closeDrawer);

  document.querySelectorAll('.wl-drawer-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const tabName = tab.dataset.tab;
      setDrawerTab(tabName);
      const entry = wlState.entries.find(e => e.id === wlState.drawerEntryId);
      if (entry) loadDrawerTabData(entry, tabName);
    });
  });
}

/* ── Refresh ──────────────────────────────────────────── */

function initRefresh() {
  const btn = document.getElementById('wl-btn-refresh');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    if (wlState.refreshing) return;
    wlState.refreshing = true;
    btn.textContent = '刷新中...';
    btn.disabled = true;

    const refreshBar = document.getElementById('wl-refresh-bar');
    const statusEl = document.getElementById('wl-pipeline-status');
    if (refreshBar) refreshBar.style.display = 'block';

    try {
      const es = new EventSource(`${API}/refresh`);

      es.addEventListener('step_start', (e) => {
        const d = JSON.parse(e.data);
        if (statusEl) statusEl.textContent = d.message || '';
      });

      es.addEventListener('step_done', (e) => {
        const d = JSON.parse(e.data);
        if (statusEl) statusEl.textContent = d.message || '';
      });

      es.addEventListener('refresh_done', () => {
        es.close();
        finishRefresh(btn, refreshBar, statusEl);
      });

      es.onerror = () => {
        es.close();
        finishRefresh(btn, refreshBar, statusEl);
      };
    } catch (e) {
      finishRefresh(btn, refreshBar, statusEl);
    }
  });
}

function finishRefresh(btn, refreshBar, statusEl) {
  wlState.refreshing = false;
  btn.textContent = '刷新数据';
  btn.disabled = false;
  if (statusEl) statusEl.textContent = '刷新完成';
  loadWatchlist();
  loadBudget();
  loadPipelineStatus();
  setTimeout(() => {
    if (refreshBar) refreshBar.style.display = 'none';
  }, 3000);
}

/* ── Phase 4 integration ─────────────────────────────── */

export function showP4WatchlistActions() {
  const el = document.getElementById('wl-p4-actions');
  if (el) el.style.display = 'flex';
}

export function hideP4WatchlistActions() {
  const el = document.getElementById('wl-p4-actions');
  if (el) el.style.display = 'none';
}

/* ── Init ─────────────────────────────────────────────── */

export function initWatchlist() {
  initToolbar();
  initAddPanel();
  initTableActions();
  initCardActions();
  initDrawer();
  initRefresh();
  loadWatchlist();
  loadBudget();
  loadPipelineStatus();
}
