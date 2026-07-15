/**
 * watchlist.js — 观察池前端模块（重构版）
 * 表格/卡片双模式 + 搜索/筛选/排序 + 详情抽屉 + UZI 分析
 */
import { showConfirm } from './utils/confirm.js';
import { fmtBJ } from './wizard-state.js';
import { buildDetailGrid } from './phase-views.js';

const API = '/api/watchlist';

function _debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

const wlState = {
  entries: [],
  counts: { focus: 0, normal: 0, track: 0 },
  total: 0,
  limits: { total: 24, focus: 6, normal: 6, track: 12 },
  budget: null,
  refreshing: false,
  refreshingIntel: false,
  refreshingStrategy: false,
  strategyCache: {},
  strategyCacheTime: 0,
  viewMode: 'table',
  filterTier: 'all',
  filterMarket: 'us_stock',
  sortBy: 'tier',
  searchQuery: '',
  drawerEntryId: null,
  drawerTab: 'info',
  uziRunning: null,
  pipeStatuses: [],
  staleTickers: new Set(),
  selectedIds: new Set(),
};

/* ── Toast 通知 ─────────────────────────────────────── */

function showToast(msg, type = 'success', duration = 3000) {
  let el = document.getElementById('bh-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'bh-toast';
    document.body.appendChild(el);
  }
  el.className = `bh-toast bh-toast--${type} bh-toast--show`;
  el.textContent = msg;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('bh-toast--show'), duration);
}

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

/* ── JSON → 可读 HTML 的格式化助手 ───────────────────────── */

const _fieldLabels = {
  current_price: '当前价格', prev_close: '昨收', change_pct: '涨跌幅',
  market_cap: '总市值', pe_ratio: 'P/E', pb_ratio: 'P/B',
  volume: '成交量', avg_volume: '平均成交量', high_52w: '52周最高',
  low_52w: '52周最低', rsi_14: 'RSI(14)', ma_20: 'MA20', ma_50: 'MA50',
  trend: '趋势', signal: '信号', action: '操作建议',
  entry_price: '建议入场价', stop_loss: '止损价', take_profit: '止盈价',
  position_size: '仓位比例', risk_level: '风险等级', risk_score: '风险分',
  title: '标题', summary: '摘要', description: '描述', source: '来源',
  date: '日期', type: '类型', sentiment: '情绪', impact: '影响',
  headline: '标题', publisher: '发布者', published: '发布时间',
  total_calls: '看涨期权总量', total_puts: '看跌期权总量',
  put_call_ratio: 'Put/Call 比率', implied_volatility: '隐含波动率',
  eps_estimate: 'EPS 预期', eps_actual: 'EPS 实际', revenue: '营收',
  quarter: '季度', beat: '是否超预期', score: '评分', reason: '理由',
  overall_score: '综合评分', moat_score: '护城河', growth_score: '成长性',
  quality_score: '质量评分', valuation_score: '估值评分',
  max_drawdown: '最大回撤', var_95: 'VaR(95%)',
  time_horizon: '时间周期', catalysts: '催化剂',
  key_risks: '关键风险', upside: '上行空间', downside: '下行空间',
};

function _renderJsonAsHtml(data) {
  if (data == null) return '<span class="wl-muted">—</span>';
  if (typeof data === 'string') return escHtml(data);
  if (typeof data === 'number') return String(data);
  if (typeof data === 'boolean') return data ? '是' : '否';

  if (Array.isArray(data)) {
    if (data.length === 0) return '<span class="wl-muted">无数据</span>';
    if (typeof data[0] === 'string' || typeof data[0] === 'number') {
      return `<ul class="wl-json-list">${data.map(v => `<li>${escHtml(String(v))}</li>`).join('')}</ul>`;
    }
    return data.map(item => `<div class="wl-json-card">${_renderJsonAsHtml(item)}</div>`).join('');
  }

  const keys = Object.keys(data);
  if (keys.length === 0) return '<span class="wl-muted">无数据</span>';

  let rows = '';
  for (const k of keys) {
    const label = _fieldLabels[k] || k.replace(/_/g, ' ');
    const val = data[k];
    if (val != null && typeof val === 'object') {
      rows += `<div class="wl-json-row wl-json-nested"><div class="wl-json-label">${escHtml(label)}</div><div class="wl-json-value">${_renderJsonAsHtml(val)}</div></div>`;
    } else {
      let display = val;
      if (typeof val === 'number') {
        display = Math.abs(val) > 1e6 ? (val / 1e6).toFixed(2) + 'M' : (Number.isInteger(val) ? val : val.toFixed(2));
      } else if (typeof val === 'boolean') {
        display = val ? '✓ 是' : '✗ 否';
      } else {
        display = escHtml(String(val ?? '—'));
      }
      rows += `<div class="wl-json-row"><div class="wl-json-label">${escHtml(label)}</div><div class="wl-json-value">${display}</div></div>`;
    }
  }
  return `<div class="wl-json-table">${rows}</div>`;
}

async function loadWatchlist() {
  try {
    const data = await apiFetch('');
    wlState.entries = data.entries || [];
    wlState.counts = data.counts || { focus: 0, normal: 0, track: 0 };
    wlState.total = data.total || 0;
    if (data.limits) wlState.limits = data.limits;
    render();
    checkDataHealth();
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

async function checkDataHealth() {
  try {
    const data = await apiFetch('/health');
    wlState.staleTickers = new Set((data.stale_tickers || []).map(t => t.ticker));
    if (wlState.staleTickers.size > 0) {
      showToast(`${wlState.staleTickers.size} 只股票数据已超过 48 小时未更新`, 'warning', 5000);
    }
  } catch (e) {
    console.error('Failed to check data health:', e);
  }
}

/* ── Filtering & sorting ──────────────────────────────── */

function getFilteredEntries() {
  let items = [...wlState.entries];
  if (wlState.filterMarket) {
    items = items.filter(e => (e.market || 'us_stock') === wlState.filterMarket);
  }
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
  if (wlState.strategyCacheTime && Date.now() - wlState.strategyCacheTime > 5 * 60 * 1000) {
    loadStrategySummaries();
  }

  const countBadge = document.getElementById('wl-count-badge');
  // 分市场独立限额：徽标只数当前切换市场的持仓
  const mktEntries = wlState.entries.filter(e => (e.market || 'us_stock') === wlState.filterMarket);
  if (countBadge) countBadge.textContent = `${mktEntries.length} / ${wlState.limits.total}`;

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
    updateBatchBar();
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
    tbody.innerHTML = '<tr class="wl-table-empty"><td colspan="12">无匹配结果</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(entry => {
    const snap = entry.latest_snapshot;
    const isA = (entry.market || '') === 'a_stock';
    const currSign = isA ? '¥' : '$';
    const price = snap ? `${currSign}${Number(snap.close).toFixed(2)}` : '--';
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
    const checked = wlState.selectedIds.has(entry.id) ? 'checked' : '';

    // 策略信号：与详情页策略页签一致——显示 信号 + v版本号（此前列表显示的是 confidence，
    // 与详情的 version 同位却是不同指标，易被误读为"版本不一致"）。信心移到 title 提示。
    const strat = wlState.strategyCache[entry.id];
    const stratSignal = strat ? strat.signal : '';
    const stratVer = strat ? strat.version : '';
    const stratConf = strat ? strat.confidence : '';
    const stratBadge = stratSignal
      ? `<span class="wl-strategy-badge wl-strategy-${stratSignal}" title="信心 ${stratConf}/10">${
          stratSignal === 'bullish' ? '看多' : stratSignal === 'bearish' ? '看空' : '中性'
        }${stratVer ? ` <span style="opacity:0.7">v${stratVer}</span>` : ''}</span>`
      : '<span style="color:var(--muted);font-size:var(--fs-xs)">--</span>';

    return `
      <tr data-id="${entry.id}" data-ticker="${entry.ticker}">
        <td class="wl-td-check"><input type="checkbox" class="wl-row-check" data-id="${entry.id}" ${checked}></td>
        <td><span class="wl-tier-badge wl-tier-badge--${entry.tier}"><span class="wl-tier-dot wl-dot-${entry.tier}"></span>${tierLabel(entry.tier)}</span></td>
        <td style="font-weight:600;color:var(--accent)">${entry.ticker}</td>
        <td>${name}</td>
        <td class="col-num">${price}</td>
        <td class="col-num ${changeClass}">${changeStr}</td>
        <td class="col-num">${rsi}</td>
        <td class="col-num">${macd.html}</td>
        <td class="col-num wl-score-cell ${scoreClass}">${score}</td>
        <td class="col-num">${stratBadge}</td>
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
  const tierLimits = wlState.limits;
  for (const tier of ['focus', 'normal', 'track']) {
    const grid = document.getElementById(`wl-grid-${tier}`);
    const countEl = document.getElementById(`wl-tier-count-${tier}`);
    if (!grid) continue;

    // 分市场独立限额：卡片只显示当前切换市场的持仓与计数
    const items = wlState.entries.filter(
      e => e.tier === tier && (e.market || 'us_stock') === wlState.filterMarket
    );
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
  const isA = (entry.market || '') === 'a_stock';
  const currSign = isA ? '¥' : '$';
  const price = snap ? `${currSign}${Number(snap.close).toFixed(2)}` : '--';
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

  // 诚实用量护栏：调用次数为真·实数；负载%=距每日失控熔断的比例（按调用规模估算，非美元）
  const calls = b.daily_calls || 0;
  const pct = Math.min(b.daily_pct || 0, 100);

  fill.style.width = `${pct}%`;
  fill.className = 'wl-budget-fill' +
    (pct > 90 ? ' wl-budget-danger' : pct > 70 ? ' wl-budget-warn' : '');
  text.textContent = `今日 ${calls} 次调用 · 负载 ${Math.round(pct)}%`;
  const bar = document.getElementById('wl-budget-bar');
  if (bar) bar.title = '今日 LLM 调用次数（真实）与失控熔断负载（按调用规模估算，达上限自动停用以防烧钱）';
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
      : p.last_status === 'partial' ? 'warn'
      : p.last_status === 'error' ? 'err' : 'stale';
    const label = { price: '价格', news: '新闻', sec: 'SEC', options: '期权', notice: '公告' }[p.pipeline_name] || p.pipeline_name;
    const errorTip = (p.last_status === 'error' || p.last_status === 'partial') && p.last_error
      ? ` title="${escHtml(p.last_error)}"` : '';
    return `<span${errorTip}><span class="wl-pipe-dot wl-pipe-dot--${dotClass}"></span>${label}: ${ago}</span>`;
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
  openCompanyDrawer({ entry });
}

// 统一企业详情抽屉入口：观察池(entry 有 id)走全量端点；分析/决策环节(只有 scorecard)走 scorecard + 兜底。
export function openCompanyDrawer(ctx) {
  const entry = ctx.entry || {
    id: ctx.entry_id || null,
    ticker: ctx.ticker,
    market: ctx.market || 'us_stock',
    company_name: ctx.name || ctx.ticker,
    company_name_cn: ctx.name_cn || '',
    tier: ctx.tier || '',
    latest_snapshot: ctx.snapshot || null,
  };
  if (ctx.scorecard) entry._scorecard = ctx.scorecard;
  // 无 entry_id 但有 ticker：匹配已入池的同标的 → 升级为全量视图
  if (!entry.id && entry.ticker) {
    const hit = (wlState.entries || []).find(e => e.ticker === entry.ticker);
    if (hit) {
      entry.id = hit.id; entry.tier = entry.tier || hit.tier;
      entry.latest_snapshot = entry.latest_snapshot || hit.latest_snapshot;
      if (hit.market) entry.market = hit.market;  // 采用池内条目的权威 market（纠正调用方可能传错的 market）
    }
  }

  wlState.drawerEntry = entry;
  wlState.drawerEntryId = entry.id;
  wlState.drawerTab = 'info';

  const drawer = document.getElementById('wl-drawer');
  const overlay = document.getElementById('wl-drawer-overlay');
  if (!drawer || !overlay) return;

  document.getElementById('wl-drawer-name').textContent = entry.company_name_cn || entry.company_name;
  document.getElementById('wl-drawer-ticker').textContent = entry.ticker || '';

  const snap = entry.latest_snapshot;
  const priceEl = document.getElementById('wl-drawer-price');
  const changeEl = document.getElementById('wl-drawer-change');
  if (snap) {
    const currSign = (entry.market || '') === 'a_stock' ? '¥' : '$';
    priceEl.textContent = `${currSign}${Number(snap.close).toFixed(2)}`;
    const cp = snap.change_pct;
    if (cp != null) {
      changeEl.textContent = `${cp >= 0 ? '+' : ''}${cp.toFixed(2)}%`;
      changeEl.className = 'wl-drawer-change ' + (cp >= 0 ? 'wl-change-up' : 'wl-change-down');
    } else { changeEl.textContent = ''; }
  } else { priceEl.textContent = ''; changeEl.textContent = ''; }

  const tierEl = document.getElementById('wl-drawer-tier');
  if (entry.tier) {
    tierEl.style.display = '';
    tierEl.className = `wl-drawer-tier-badge wl-tier-badge wl-tier-badge--${entry.tier}`;
    tierEl.innerHTML = `<span class="wl-tier-dot wl-dot-${entry.tier}"></span>${tierLabel(entry.tier)}`;
  } else { tierEl.style.display = 'none'; }

  // 观察池特有页签(持仓策略/UZI)：无 entry_id 时隐藏
  document.querySelectorAll('.wl-drawer-tab[data-wl-only]').forEach(t => {
    t.style.display = entry.id ? '' : 'none';
  });

  drawer.style.display = 'flex';
  overlay.style.display = 'block';
  requestAnimationFrame(() => drawer.classList.add('drawer-open'));

  setDrawerTab('info');
  loadDrawerTabData(entry, 'info');
}
// 暴露到全局：交叉验证/L2/L3/模拟持仓等模块双击列表时调用，避免跨模块循环 import
if (typeof window !== 'undefined') window.openCompanyDrawer = openCompanyDrawer;

// 全局委托：任意带 data-company-ticker 的行双击 → 统一详情抽屉（按 ticker 匹配观察池；命中即全量）
if (typeof document !== 'undefined') {
  document.addEventListener('dblclick', (e) => {
    const el = e.target.closest('[data-company-ticker]');
    if (!el || !window.openCompanyDrawer) return;
    const ticker = el.getAttribute('data-company-ticker');
    if (!ticker) return;
    window.openCompanyDrawer({
      ticker,
      name: el.getAttribute('data-company-name') || ticker,
      market: el.getAttribute('data-company-market') || window.appState?.market || 'us_stock',
    });
  });
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
  // 需要观察池存储(entry_id)的页签：分析环节无 id 时给友好兜底，不打空端点
  const needsEntry = ['price', 'news', 'capital', 'intelligence', 'strategy', 'uzi'];
  if (!entry.id && needsEntry.includes(tab)) {
    const pane = document.getElementById(`wl-tab-${tab}`);
    if (pane) pane.innerHTML = '<div class="wl-empty" style="padding:40px;text-align:center;color:var(--muted)">该企业未加入观察池<br><span style="font-size:12px">加入观察池后可查看实时行情/新闻/资金/情报/策略</span></div>';
    return;
  }
  switch (tab) {
    case 'info':
      await loadInfoTab(entry);
      break;
    case 'score':
      await loadScoreTab(entry);
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
    case 'intelligence':
      await loadIntelligenceTab(entry);
      break;
    case 'strategy':
      await loadStrategyTab(entry);
      break;
    case 'uzi':
      await loadUziTab(entry);
      break;
  }
}

/* ── 系统评分 Tab（五维/预期差/优劣，复用 phase-views buildDetailGrid）──── */
async function loadScoreTab(entry) {
  const pane = document.getElementById('wl-tab-score');
  if (!pane) return;
  let sc = entry._scorecard;
  if (!sc && entry.id) {
    pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:200px"></div>';
    const data = await fetchSourceScorecard(entry.id);
    sc = data && data.scorecard ? data.scorecard : null;
  }
  // 持久化档案兜底：按 ticker 取（评选/入围/反查过的企业均有档案，不依赖易失的 source 反查）
  if (!sc && entry.ticker) {
    pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:200px"></div>';
    try {
      const r = await fetch(`/api/company-archive?ticker=${encodeURIComponent(entry.ticker)}`);
      if (r.ok) {
        const a = (await r.json()).archive;
        if (a && a.scorecard && Object.keys(a.scorecard).length) sc = a.scorecard;
      }
    } catch { /* 忽略 */ }
  }
  if (sc) entry._scorecard = sc;
  if (!sc) {
    pane.innerHTML = '<div class="wl-empty" style="padding:40px;text-align:center;color:var(--muted)">暂无系统评分数据<br><span style="font-size:12px">该企业需经产业链分析(五维评分/预期差)或反向分析后才有评分</span></div>';
    return;
  }
  pane.innerHTML = `<div class="wl-score-wrap">${buildDetailGrid(sc)}</div>`;
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

  const isPhase4 = entry.source === 'phase4';
  const market = entry.market || 'us_stock';
  const sc = entry._scorecard || null;   // 分析环节的评分卡（含 supplier 简介）

  // 观察池(有 id)走全量 overview；否则按 ticker 拉取(新接口)，未入池企业也能看基本信息
  const overview = entry.id
    ? await apiFetch(`/${entry.id}/overview`).catch(() => ({}))
    : await apiFetch(`/company-overview?ticker=${encodeURIComponent(entry.ticker || '')}&market=${market}`).catch(() => ({}));
  const src = (isPhase4 && entry.id) ? await fetchSourceScorecard(entry.id).catch(() => null)
    : (sc ? { scorecard: sc } : null);

  const snap = overview.latest_snapshot || entry.latest_snapshot || {};
  const profile = overview.profile || {};
  const raw = profile.raw || {};

  let html = '';

  /* ── 报价卡片（简化版：名称 + 价格 + 涨跌 + 市值） ── */
  html += '<div class="wl-overview-cards">';
  html += _buildQuoteCard(entry, snap);
  html += '</div>';

  /* ── 来源 & 加入时间：仅观察池条目显示（入围/最终评选等分析环节不显示）── */
  if (entry.id) {
    const addedDate = entry.added_at ? fmtBJ(entry.added_at) : '未知';
    const sourceLabel = isPhase4 ? '系统推荐（Phase 4 产业链分析）' : '手动添加';
    html += `<div class="wl-info-meta">
      <div class="wl-info-meta-row"><span class="wl-info-meta-label">加入时间</span><span>${addedDate}</span></div>
      <div class="wl-info-meta-row"><span class="wl-info-meta-label">来源</span><span class="wl-info-source-badge ${isPhase4 ? 'wl-info-source--phase4' : 'wl-info-source--manual'}">${sourceLabel}</span></div>
    </div>`;
    if (isPhase4 && src?.scorecard) {
      html += buildRecommendationReason(src, entry);
      html += buildScorecardDetail(src.scorecard, entry);
    }
  }

  /* ── 公司概况（构建后统一移到页面最下方；中英对照）── */
  const desc = profile.description || raw.longBusinessSummary || sc?.supplier?.description || '';
  const sector = profile.sector || raw.sector || sc?.supplier?.sector || '';
  const industry = profile.industry || raw.industry || '';
  const country = profile.country || raw.country || '';
  const exchange = profile.exchange || raw.exchange || '';
  const currency = profile.currency || raw.currency || '';
  const website = profile.website || raw.website || '';
  const employees = profile.employees || raw.fullTimeEmployees || 0;

  let profileHtml = '';
  if (desc || sector || industry) {
    profileHtml += '<div class="wl-info-section wl-profile-section"><h4>公司概况</h4>';
    if (sector || industry || country || exchange) {
      profileHtml += '<div class="wl-profile-tags">';
      if (sector) profileHtml += `<span class="wl-profile-tag">${escHtml(sector)}</span>`;
      if (industry) profileHtml += `<span class="wl-profile-tag">${escHtml(industry)}</span>`;
      if (country) profileHtml += `<span class="wl-profile-tag">${escHtml(country)}</span>`;
      if (exchange) profileHtml += `<span class="wl-profile-tag">${escHtml(exchange)}</span>`;
      profileHtml += '</div>';
    }
    if (desc) {
      profileHtml += `<div class="wl-profile-desc">
        <p class="wl-profile-desc-text">${escHtml(desc)}</p>
        <div class="wl-profile-trans" data-tt="${escHtml(desc)}"></div>
      </div>`;
    }
    const metaItems = [];
    if (website) metaItems.push(`<span>官网: <a href="${escHtml(website)}" target="_blank">${escHtml(website)}</a></span>`);
    if (employees) metaItems.push(`<span>员工: ${Number(employees).toLocaleString()}</span>`);
    if (currency) metaItems.push(`<span>货币: ${escHtml(currency)}</span>`);
    if (metaItems.length) {
      profileHtml += `<div class="wl-profile-meta">${metaItems.join('')}</div>`;
    }
    profileHtml += '</div>';
  }

  /* ── 估值指标 ── */
  html += _buildProfileGrid('估值指标', [
    ['市盈率(TTM)', _fmtNum(raw.trailingPE, 2)],
    ['市盈率(预期)', _fmtNum(raw.forwardPE, 2)],
    ['市净率', _fmtNum(raw.priceToBook, 2)],
    ['市销率', _fmtNum(raw.priceToSalesTrailing12Months, 2)],
    ['EV/EBITDA', _fmtNum(raw.enterpriseToEbitda, 2)],
    ['PEG', _fmtNum(raw.trailingPegRatio, 2)],
    ['市值', _fmtBigNum(raw.marketCap)],
    ['企业价值', _fmtBigNum(raw.enterpriseValue)],
  ]);

  /* ── 盈利能力 ── */
  html += _buildProfileGrid('盈利能力', [
    ['毛利率', _fmtPct(raw.grossMargins)],
    ['营业利润率', _fmtPct(raw.operatingMargins)],
    ['净利率', _fmtPct(raw.profitMargins)],
    ['ROE', _fmtPct(raw.returnOnEquity)],
    ['ROA', _fmtPct(raw.returnOnAssets)],
    ['每股收益(TTM)', _fmtNum(raw.trailingEps, 2)],
    ['每股营收', _fmtNum(raw.revenuePerShare, 2)],
  ]);

  /* ── 财务健康 ── */
  html += _buildProfileGrid('财务健康', [
    ['资产负债率', _fmtNum(raw.debtToEquity, 1)],
    ['流动比率', _fmtNum(raw.currentRatio, 2)],
    ['速动比率', _fmtNum(raw.quickRatio, 2)],
    ['总现金', _fmtBigNum(raw.totalCash)],
    ['总负债', _fmtBigNum(raw.totalDebt)],
    ['总营收', _fmtBigNum(raw.totalRevenue)],
    ['自由现金流', _fmtBigNum(raw.freeCashflow)],
  ]);

  /* ── 增长 & 分红 ── */
  html += _buildProfileGrid('增长 & 分红', [
    ['营收增长', _fmtPct(raw.revenueGrowth)],
    ['利润增长', _fmtPct(raw.earningsGrowth)],
    ['季度利润增长', _fmtPct(raw.earningsQuarterlyGrowth)],
    ['股息率', _fmtPct(raw.dividendYield)],
    ['每股股息', _fmtNum(raw.dividendRate, 2)],
    ['派息率', _fmtPct(raw.payoutRatio)],
  ]);

  /* ── 风险指标 ── */
  html += _buildProfileGrid('风险指标', [
    ['Beta', _fmtNum(raw.beta, 2)],
    ['52周变动', _fmtPct(raw['52WeekChange'])],
    ['52周最高', _fmtNum(raw.fiftyTwoWeekHigh, 2)],
    ['52周最低', _fmtNum(raw.fiftyTwoWeekLow, 2)],
    ['做空比率', _fmtNum(raw.shortRatio, 2)],
    ['做空占比', _fmtPct(raw.shortPercentOfFloat)],
  ]);

  /* ── 备注 & 决策路径：仅观察池条目显示 ── */
  if (entry.id) {
    html += `<div class="wl-info-section wl-notes-section">
      <h4>备注</h4>
      <textarea class="wl-notes-input" id="wl-notes-textarea" rows="3" placeholder="添加备注...">${escHtml(entry.notes || '')}</textarea>
      <button class="btn btn-sm" id="wl-notes-save" style="margin-top:6px">保存备注</button>
    </div>`;
    html += `<div class="wl-info-section">
      <details class="wl-decision-trace">
        <summary style="cursor:pointer;font-weight:600;font-size:13px;color:var(--accent)">决策路径</summary>
        <div id="wl-decision-trace-body" style="margin-top:8px">
          <p style="color:var(--muted);font-size:12px">加载中...</p>
        </div>
      </details>
    </div>`;
  }

  html += profileHtml;   // 公司概况统一置于页面最下方
  pane.innerHTML = html;
  _fillTranslations(pane, market);   // 公司概况中英对照（异步回填，不阻塞首屏）

  /* ── 公司简介展开/收起 ── */
  const descToggle = document.getElementById('wl-profile-desc-toggle');
  if (descToggle && desc.length > 300) {
    let expanded = false;
    const descText = document.getElementById('wl-profile-desc-text');
    descToggle.addEventListener('click', () => {
      expanded = !expanded;
      descText.textContent = expanded ? desc : desc.substring(0, 300) + '...';
      descToggle.textContent = expanded ? '收起' : '展开全部';
    });
  }

  /* ── 备注保存 ── */
  const notesSaveBtn = document.getElementById('wl-notes-save');
  if (notesSaveBtn) {
    notesSaveBtn.addEventListener('click', async () => {
      const textarea = document.getElementById('wl-notes-textarea');
      if (!textarea) return;
      try {
        await apiFetch(`/${entry.id}`, {
          method: 'PATCH',
          body: JSON.stringify({ notes: textarea.value }),
        });
        notesSaveBtn.textContent = '已保存';
        setTimeout(() => { notesSaveBtn.textContent = '保存备注'; }, 1500);
        const e = wlState.entries.find(e => e.id === entry.id);
        if (e) e.notes = textarea.value;
      } catch (err) {
        showToast(`保存失败: ${err.message}`, 'error');
      }
    });
  }

  loadDecisionTrace(entry.ticker, entry.market);
}

/* ── Profile helper functions ── */

function _fmtNum(val, decimals) {
  if (val == null || val === '' || isNaN(val)) return '-';
  return Number(val).toFixed(decimals);
}

function _fmtPct(val) {
  if (val == null || val === '' || isNaN(val)) return '-';
  return (Number(val) * 100).toFixed(2) + '%';
}

function _fmtBigNum(val) {
  if (val == null || val === '' || isNaN(val)) return '-';
  const n = Number(val);
  if (Math.abs(n) >= 1e12) return (n / 1e12).toFixed(2) + 'T';
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toLocaleString();
}

function _buildProfileGrid(title, rows) {
  const validRows = rows.filter(r => r[1] !== '-');
  if (!validRows.length) return '';
  let html = `<div class="wl-info-section wl-profile-section"><h4>${title}</h4><div class="wl-profile-grid">`;
  validRows.forEach(([label, value]) => {
    let valClass = '';
    if (typeof value === 'string' && value.endsWith('%')) {
      const num = parseFloat(value);
      if (!isNaN(num)) valClass = num > 0 ? 'val-up' : num < 0 ? 'val-down' : '';
    }
    html += `<div class="wl-profile-grid-item">
      <span class="wl-profile-grid-label">${label}</span>
      <span class="wl-profile-grid-value ${valClass}">${value}</span>
    </div>`;
  });
  html += '</div></div>';
  return html;
}

/* ── Overview helper builders ── */

function _buildQuoteCard(entry, snap) {
  const price = snap.close;
  const change = snap.change_pct;
  const cap = snap.market_cap;
  const pe = snap.pe_ratio;
  const vol = snap.volume;

  if (!price && !cap) {
    const hint = entry && entry.id
      ? '暂无行情数据，请点击工具栏「刷新数据」获取'
      : '该企业未加入观察池，加入后可获取实时行情';
    return `<div class="wl-quote-card"><div class="wl-quote-empty">${hint}</div></div>`;
  }

  const changeClass = change > 0 ? 'val-up' : change < 0 ? 'val-down' : '';
  const changeSign = change > 0 ? '+' : '';
  const capStr = cap ? (cap >= 1e12 ? (cap / 1e12).toFixed(2) + 'T' : cap >= 1e9 ? (cap / 1e9).toFixed(2) + 'B' : cap >= 1e6 ? (cap / 1e6).toFixed(0) + 'M' : cap.toLocaleString()) : '-';

  return `<div class="wl-quote-card">
    <div class="wl-quote-price-row">
      <span class="wl-quote-price">${price != null ? Number(price).toFixed(2) : '-'}</span>
      <span class="wl-quote-change ${changeClass}">${change != null ? changeSign + change.toFixed(2) + '%' : '-'}</span>
    </div>
    <div class="wl-quote-details">
      <div class="wl-quote-item"><span class="wl-quote-label">市值</span><span>${capStr}</span></div>
      <div class="wl-quote-item"><span class="wl-quote-label">PE</span><span>${pe != null ? Number(pe).toFixed(1) : '-'}</span></div>
      <div class="wl-quote-item"><span class="wl-quote-label">成交量</span><span>${vol ? Number(vol).toLocaleString() : '-'}</span></div>
      <div class="wl-quote-item"><span class="wl-quote-label">日期</span><span>${(snap.date || '').slice(0, 10)}</span></div>
    </div>
  </div>`;
}

function _buildTechCard(snap) {
  const rsi = snap.rsi_14;
  const macd = snap.macd;
  const macdSig = snap.macd_signal;
  const sma20 = snap.sma_20;
  const sma50 = snap.sma_50;

  if (rsi == null && macd == null && sma20 == null) {
    return '';
  }

  let rsiClass = '';
  let rsiLabel = '';
  if (rsi != null) {
    if (rsi > 70) { rsiClass = 'val-down'; rsiLabel = '超买'; }
    else if (rsi < 30) { rsiClass = 'val-up'; rsiLabel = '超卖'; }
    else { rsiLabel = '中性'; }
  }

  const macdDiff = (macd != null && macdSig != null) ? macd - macdSig : null;
  const macdClass = macdDiff != null ? (macdDiff > 0 ? 'val-up' : 'val-down') : '';

  return `<div class="wl-tech-card">
    <h5>技术指标</h5>
    <div class="wl-tech-grid">
      <div class="wl-tech-item"><span class="wl-tech-label">RSI(14)</span><span class="wl-tech-val ${rsiClass}">${rsi != null ? rsi.toFixed(1) : '-'} <small>${rsiLabel}</small></span></div>
      <div class="wl-tech-item"><span class="wl-tech-label">MACD</span><span class="wl-tech-val ${macdClass}">${macd != null ? macd.toFixed(4) : '-'}</span></div>
      <div class="wl-tech-item"><span class="wl-tech-label">SMA 20</span><span class="wl-tech-val">${sma20 != null ? Number(sma20).toFixed(2) : '-'}</span></div>
      <div class="wl-tech-item"><span class="wl-tech-label">SMA 50</span><span class="wl-tech-val">${sma50 != null ? Number(sma50).toFixed(2) : '-'}</span></div>
    </div>
  </div>`;
}

/* 17F.1 决策路径追溯 */
async function loadDecisionTrace(ticker, market) {
  const body = document.getElementById('wl-decision-trace-body');
  if (!body) return;
  try {
    const mkt = market || 'us_stock';
    const res = await fetch(`/api/decision/trace/${encodeURIComponent(ticker)}?market=${mkt}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const layers = data.layers || [];
    if (layers.length === 0) {
      body.innerHTML = '<p style="color:var(--muted);font-size:12px">暂无决策数据，请先运行日常决策。</p>';
      return;
    }
    let html = '<div class="wl-trace-chain">';
    layers.forEach((layer, i) => {
      const ts = fmtBJ(layer.updated_at);
      html += `<div class="wl-trace-layer">
        <div class="wl-trace-level">${escHtml(layer.level)}</div>
        <div class="wl-trace-content">
          <div class="wl-trace-label">${escHtml(layer.label)}</div>
          <div class="wl-trace-summary">${escHtml(layer.summary)}</div>
          ${ts ? `<div class="wl-trace-time">${ts}</div>` : ''}
        </div>
      </div>`;
      if (i < layers.length - 1) {
        html += '<div class="wl-trace-arrow">&#x2193;</div>';
      }
    });
    html += '</div>';
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = `<p style="color:var(--muted);font-size:12px">决策路径加载失败: ${escHtml(e.message)}</p>`;
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
    const result = await apiFetch(`/${entry.id}/snapshots?days=90`);
    const snaps = result?.snapshots || result || [];
    renderPriceChart(snaps);
    renderIndicators(entry);
  } catch (e) {
    pane.innerHTML = `<div class="wl-empty-hint"><p>暂无价格数据</p><p>请先点击工具栏的 <b>刷新数据</b> 获取市场行情</p></div>`;
  }
}

function renderPriceChart(snapshots) {
  const container = document.getElementById('wl-price-chart-container');
  if (!container || !Array.isArray(snapshots) || snapshots.length === 0) {
    if (container) container.innerHTML = '<div class="wl-empty-hint"><p>暂无K线数据</p><p>请先点击 <b>刷新数据</b> 获取行情</p></div>';
    return;
  }
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

  html += '<div class="wl-news-section-header" style="margin-top:20px"><h4>实时新闻 <small style="color:var(--muted);font-weight:400">（中英对照）</small></h4></div>';
  if (news.length > 0) {
    html += `<div class="wl-news-list">${news.map(n => renderNewsItem(n)).join('')}</div>`;
  } else {
    html += '<div class="wl-empty-hint"><p>暂无新闻数据</p><p>请先点击 <b>刷新数据</b> 获取最新新闻</p></div>';
  }

  pane.innerHTML = html;
  _fillTranslations(pane, entry.market);   // 异步回填译文，不阻塞首屏
}

// 批量翻译带 data-tt 的元素并回填对照（新闻/公司概况共用；美股英→中，A股中→英）
async function _fillTranslations(pane, market) {
  const slots = pane.querySelectorAll('[data-tt]');
  if (!slots.length) return;
  const texts = [...new Set([...slots].map(s => s.getAttribute('data-tt')).filter(Boolean))];
  if (!texts.length) return;
  const target = market === 'a_stock' ? 'en' : 'zh';
  try {
    const resp = await fetch('/api/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts, target }),
    });
    if (!resp.ok) return;
    const map = (await resp.json()).translations || {};
    slots.forEach(s => {
      const t = map[s.getAttribute('data-tt')];
      if (t) s.textContent = t;
    });
  } catch { /* 翻译失败则只显示原文 */ }
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
        <div class="wl-news-trans" data-tt="${escHtml(item.title || '')}"></div>
        <div class="wl-news-meta">${item.source_name || ''} · ${ago}</div>
        ${summary}
        ${item.summary ? `<div class="wl-news-trans wl-news-trans-sm" data-tt="${escHtml(item.summary)}"></div>` : ''}
      </div>
    </div>`;
}

/* ── Capital Tab ─────────────────────────────────────── */

async function loadCapitalTab(entry) {
  const pane = document.getElementById('wl-tab-capital');
  if (!pane) return;

  pane.innerHTML = '<div class="skeleton skeleton-table" style="min-height:200px"></div>';

  try {
    const [insidersRes, optionsRes, filingsRes] = await Promise.all([
      apiFetch(`/${entry.id}/insider-trades?limit=10`).catch(() => ({})),
      apiFetch(`/${entry.id}/options?limit=5`).catch(() => ({})),
      apiFetch(`/${entry.id}/filings?limit=10`).catch(() => ({})),
    ]);

    const insiders = insidersRes?.trades || (Array.isArray(insidersRes) ? insidersRes : []);
    const options = optionsRes?.options || (Array.isArray(optionsRes) ? optionsRes : []);
    const filings = filingsRes?.filings || (Array.isArray(filingsRes) ? filingsRes : []);

    if (!insiders.length && !options.length && !filings.length) {
      pane.innerHTML = '<div class="wl-empty-hint"><p>暂无资金数据</p><p>请先点击 <b>刷新数据</b> 获取内部人交易、期权及 SEC 文件</p></div>';
      return;
    }

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
          if (data.kind === 'model_fallback' || data.event === 'model_fallback') { window.notifyFallback?.(data.message); continue; }
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
      showToast(`添加失败: ${e.message}`, 'error');
    }
  });
}

/* ── Table & card actions ─────────────────────────────── */

function updateBatchBar() {
  const bar = document.getElementById('wl-batch-bar');
  const countEl = document.getElementById('wl-batch-count');
  if (!bar) return;
  const n = wlState.selectedIds.size;
  bar.style.display = n > 0 ? 'flex' : 'none';
  if (countEl) countEl.textContent = `已选 ${n} 项`;
  const checkAll = document.getElementById('wl-check-all');
  if (checkAll) {
    const items = getFilteredEntries();
    checkAll.checked = items.length > 0 && items.every(e => wlState.selectedIds.has(e.id));
    checkAll.indeterminate = !checkAll.checked && items.some(e => wlState.selectedIds.has(e.id));
  }
}

function initBatchActions() {
  const checkAll = document.getElementById('wl-check-all');
  if (checkAll) {
    checkAll.addEventListener('change', () => {
      const items = getFilteredEntries();
      if (checkAll.checked) {
        items.forEach(e => wlState.selectedIds.add(e.id));
      } else {
        wlState.selectedIds.clear();
      }
      document.querySelectorAll('.wl-row-check').forEach(cb => { cb.checked = checkAll.checked; });
      updateBatchBar();
    });
  }

  async function batchSetTier(tier) {
    const ids = [...wlState.selectedIds];
    if (ids.length === 0) return;
    try {
      await apiFetch('/batch-tier', {
        method: 'PUT',
        body: JSON.stringify({ ids, tier }),
      });
      wlState.selectedIds.clear();
      await loadWatchlist();
    } catch (e) {
      showToast(`批量操作失败: ${e.message}`, 'error');
    }
  }

  document.getElementById('wl-batch-focus')?.addEventListener('click', () => batchSetTier('focus'));
  document.getElementById('wl-batch-normal')?.addEventListener('click', () => batchSetTier('normal'));
  document.getElementById('wl-batch-track')?.addEventListener('click', () => batchSetTier('track'));

  document.getElementById('wl-batch-delete')?.addEventListener('click', async () => {
    const ids = [...wlState.selectedIds];
    if (ids.length === 0) return;
    if (!await showConfirm(`确定移除选中的 ${ids.length} 只股票？`, { danger: true })) return;
    try {
      await apiFetch('/batch-delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids })
      });
      wlState.selectedIds.clear();
      await loadWatchlist();
    } catch (e) {
      showToast(`批量删除失败: ${e.message}`, 'error');
    }
  });
}

function initTableActions() {
  const tableView = document.getElementById('wl-table-view');
  if (!tableView) return;

  tableView.addEventListener('click', async (e) => {
    const deleteBtn = e.target.closest('.wl-row-btn--danger');
    if (deleteBtn) {
      e.stopPropagation();
      const id = deleteBtn.dataset.id;
      if (!await showConfirm('确定移除该股票？', { danger: true })) return;
      try {
        await apiFetch(`/${id}`, { method: 'DELETE' });
        await loadWatchlist();
        if (wlState.drawerEntryId === id) closeDrawer();
      } catch (err) {
        showToast(`移除失败: ${err.message}`, 'error');
      }
      return;
    }

    if (e.target.closest('.wl-row-tier-select')) return;
    if (e.target.closest('.wl-row-check')) return;

    const row = e.target.closest('tr[data-id]');
    if (row) {
      openDrawer(row.dataset.id);
    }
  });

  tableView.addEventListener('change', async (e) => {
    const cb = e.target.closest('.wl-row-check');
    if (cb) {
      const id = cb.dataset.id;
      if (cb.checked) wlState.selectedIds.add(id);
      else wlState.selectedIds.delete(id);
      updateBatchBar();
      return;
    }

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
        showToast(`更新失败: ${err.message}`, 'error');
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
      if (!await showConfirm('确定移除该股票？', { danger: true })) return;
      try {
        await apiFetch(`/${id}`, { method: 'DELETE' });
        await loadWatchlist();
      } catch (err) {
        showToast(`移除失败: ${err.message}`, 'error');
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
        showToast(`更新失败: ${err.message}`, 'error');
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
    const debouncedRender = _debounce(() => render(), 250);
    searchInput.addEventListener('input', () => {
      wlState.searchQuery = searchInput.value.trim();
      debouncedRender();
    });
  }

  const marketSelect = document.getElementById('wl-filter-market');
  if (marketSelect) {
    marketSelect.addEventListener('change', () => {
      wlState.filterMarket = marketSelect.value;
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
      // 用当前抽屉 entry（分析上下文的合成 entry 不在 wlState.entries 里）
      const entry = wlState.drawerEntry || wlState.entries.find(e => e.id === wlState.drawerEntryId);
      if (entry) loadDrawerTabData(entry, tabName);
    });
  });
}

/* ── Refresh — progress bar & timer ──────────────────── */

let _wlTimerStart = null;
let _wlTimerInterval = null;

function _startWlTimer() {
  _stopWlTimer();
  _wlTimerStart = Date.now();
  const el = document.getElementById('wl-refresh-timer');
  if (el) { el.style.display = ''; el.textContent = '00:00'; }
  _wlTimerInterval = setInterval(_updateWlTimerTick, 200);
}

function _updateWlTimerTick() {
  if (!_wlTimerStart) return;
  const elapsed = Math.floor((Date.now() - _wlTimerStart) / 1000);
  const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
  const s = String(elapsed % 60).padStart(2, '0');
  const el = document.getElementById('wl-refresh-timer');
  if (el) el.textContent = `${m}:${s}`;
}

function _stopWlTimer() {
  if (_wlTimerInterval) { clearInterval(_wlTimerInterval); _wlTimerInterval = null; }
  _wlTimerStart = null;
}

function _showRefreshBar(label) {
  const bar = document.getElementById('wl-refresh-bar');
  const msg = document.getElementById('wl-pipeline-status');
  const fill = document.getElementById('wl-refresh-fill');
  if (bar) bar.style.display = '';
  if (msg) msg.textContent = label || '';
  if (fill) { fill.style.width = '100%'; fill.className = 'wl-refresh-fill active'; }
}

function _updateRefreshStatus(text) {
  const msg = document.getElementById('wl-pipeline-status');
  if (msg && text) msg.textContent = text;
}

function _updateRefreshProgress(completed, total) {
  if (!total || total <= 0) return;
  const pct = Math.min(100, Math.round((completed / total) * 100));
  const fill = document.getElementById('wl-refresh-fill');
  if (fill) {
    fill.style.width = pct + '%';
    fill.className = 'wl-refresh-fill active';
  }
}

function _hideRefreshBar(delay) {
  _stopWlTimer();
  const fill = document.getElementById('wl-refresh-fill');
  if (fill) { fill.className = 'wl-refresh-fill'; fill.style.width = '100%'; }
  setTimeout(() => {
    const bar = document.getElementById('wl-refresh-bar');
    if (bar) bar.style.display = 'none';
  }, delay || 2000);
}

function _setAllRefreshBtnsDisabled(disabled) {
  ['wl-btn-refresh', 'wl-btn-refresh-intel', 'wl-btn-refresh-strategy', 'wl-btn-auto-refresh']
    .forEach(id => { const b = document.getElementById(id); if (b) b.disabled = disabled; });
}

/* ── Core refresh functions ───────────────────────────── */

async function _doRefreshData(opts = {}) {
  if (wlState.refreshing) return;
  wlState.refreshing = true;
  _setAllRefreshBtnsDisabled(true);
  if (!opts.skipTimer) _startWlTimer();
  _showRefreshBar(opts.label || '正在刷新市场数据...');
  let busy = false;
  try {
    // 只刷新当前市场：避免刷 A股 时触发美股 SEC/期权等数据源
    const _mkt = wlState.filterMarket || 'us_stock';
    const res = await fetch(`${API}/refresh?market=${encodeURIComponent(_mkt)}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n');
      buf = parts.pop();
      for (const line of parts) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.substring(5));
          if (d.kind === 'model_fallback' || d.event === 'model_fallback') { window.notifyFallback?.(d.message); continue; }
          if (d.refresh_busy) { busy = true; showToast(d.message, 'warning'); continue; }
          if (d.message) _updateRefreshStatus(d.message);
        } catch (_) {}
      }
    }
  } catch (e) {
    console.error('Refresh failed:', e);
    showToast(`数据刷新失败: ${e.message}`, 'error');
  } finally {
    wlState.refreshing = false;
    _setAllRefreshBtnsDisabled(false);
  }
  if (busy) { if (!opts.skipTimer) _hideRefreshBar(); return true; }
  _updateRefreshStatus('数据刷新完成');
  await loadWatchlist();
  await loadBudget();
  await loadPipelineStatus().then(() => {
    const errors = wlState.pipeStatuses.filter(p => p.last_status === 'error');
    const partials = wlState.pipeStatuses.filter(p => p.last_status === 'partial');
    if (errors.length > 0) {
      showToast(`${errors.length} 条管道刷新失败`, 'error');
    } else if (partials.length > 0) {
      showToast(`刷新完成，${partials.length} 条管道部分成功`, 'warning');
    } else {
      showToast('数据刷新完成', 'success');
    }
  });
  if (!opts.skipTimer) _hideRefreshBar();
}

async function _doRefreshIntel(opts = {}) {
  if (wlState.refreshingIntel) return;
  wlState.refreshingIntel = true;
  _setAllRefreshBtnsDisabled(true);
  if (!opts.skipTimer) _startWlTimer();
  _showRefreshBar(opts.label || '正在聚合情报信息...');
  let busy = false;
  try {
    const market = wlState.filterMarket || 'us_stock';
    const marketQs = `?market=${market}`;
    const res = await fetch(`${API}/refresh-intelligence${marketQs}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n');
      buf = parts.pop();
      for (const line of parts) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.substring(5));
          if (d.kind === 'model_fallback' || d.event === 'model_fallback') { window.notifyFallback?.(d.message); continue; }
          if (d.refresh_busy) { busy = true; showToast(d.message, 'warning'); continue; }
          if (d.message) _updateRefreshStatus(d.message);
          if (d.completed != null && d.total) {
            _updateRefreshProgress(d.completed, d.total);
          }
        } catch (_) {}
      }
    }
    if (busy) return true;
    await loadWatchlist();
    showToast('情报聚合完成', 'success');
  } catch (e) {
    console.error('Intel refresh failed:', e);
    showToast(`情报刷新失败: ${e.message}`, 'error');
  } finally {
    wlState.refreshingIntel = false;
    _updateRefreshStatus('情报聚合完成');
    _setAllRefreshBtnsDisabled(false);
    if (!opts.skipTimer) _hideRefreshBar();
  }
}

async function _doRefreshStrategy(opts = {}) {
  if (wlState.refreshingStrategy) return;
  wlState.refreshingStrategy = true;
  wlState.strategyCache = {};
  wlState.strategyCacheTime = 0;
  _setAllRefreshBtnsDisabled(true);
  if (!opts.skipTimer) _startWlTimer();
  _showRefreshBar(opts.label || '正在生成操作策略...');
  let busy = false;
  try {
    const market = wlState.filterMarket || 'us_stock';
    const marketQs = `?market=${market}`;
    const res = await fetch(`${API}/refresh-strategy${marketQs}`, { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n');
      buf = parts.pop();
      for (const line of parts) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.substring(5));
          if (d.kind === 'model_fallback' || d.event === 'model_fallback') { window.notifyFallback?.(d.message); continue; }
          if (d.refresh_busy) { busy = true; showToast(d.message, 'warning'); continue; }
          if (d.message) _updateRefreshStatus(d.message);
          if (d.completed != null && d.total) {
            _updateRefreshProgress(d.completed, d.total);
          }
        } catch (_) {}
      }
    }
    if (busy) return true;
    await loadStrategySummaries();
    showToast('策略生成完成', 'success');
  } catch (e) {
    console.error('Strategy refresh failed:', e);
    showToast(`策略刷新失败: ${e.message}`, 'error');
  } finally {
    wlState.refreshingStrategy = false;
    _updateRefreshStatus('策略生成完成');
    _setAllRefreshBtnsDisabled(false);
    if (!opts.skipTimer) _hideRefreshBar();
  }
}

/* ── Button init wrappers ─────────────────────────────── */

function initRefresh() {
  const btn = document.getElementById('wl-btn-refresh');
  if (!btn) return;
  btn.addEventListener('click', () => _doRefreshData());
}

function initRefreshIntel() {
  const btn = document.getElementById('wl-btn-refresh-intel');
  if (!btn) return;
  btn.addEventListener('click', () => _doRefreshIntel());
}

function initRefreshStrategy() {
  const btn = document.getElementById('wl-btn-refresh-strategy');
  if (!btn) return;
  btn.addEventListener('click', () => _doRefreshStrategy());
}

function initAutoRefresh() {
  const btn = document.getElementById('wl-btn-auto-refresh');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (wlState.refreshing || wlState.refreshingIntel || wlState.refreshingStrategy) return;
    const origText = btn.textContent;
    btn.textContent = '⟳ 刷新中...';
    _startWlTimer();
    _showRefreshBar('一键刷新：正在刷新数据...');
    const restore = () => { btn.textContent = origText; _hideRefreshBar(3000); };
    if (await _doRefreshData({ skipTimer: true, label: '一键刷新：正在刷新数据...' })) return restore();
    _showRefreshBar('一键刷新：正在聚合情报...');
    if (await _doRefreshIntel({ skipTimer: true, label: '一键刷新：正在聚合情报...' })) return restore();
    _showRefreshBar('一键刷新：正在生成策略...');
    if (await _doRefreshStrategy({ skipTimer: true, label: '一键刷新：正在生成策略...' })) return restore();
    btn.textContent = origText;
    _updateRefreshStatus('全部刷新完成');
    showToast('一键刷新全部完成', 'success');
    _hideRefreshBar(3000);
  });
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

/* ── Strategy Brain Functions ──────────────────────────── */

async function loadStrategySummaries() {
  try {
    const data = await apiFetch('/strategy-summaries');
    wlState.strategyCache = data.summaries || {};
    wlState.strategyCacheTime = Date.now();
    render();
  } catch (e) {
    console.error('Failed to load strategy summaries:', e);
  }
}

/* (initRefreshIntel / initRefreshStrategy 已在上方重构) */

/* ── Intelligence Tab (情报) ──────────────────────────────── */

async function loadIntelligenceTab(entry) {
  const pane = document.getElementById('wl-tab-intelligence');
  if (!pane) return;
  pane.innerHTML = '<div style="text-align:center;padding:24px;color:var(--muted)">加载中...</div>';

  try {
    const [intelData, historyData] = await Promise.all([
      apiFetch(`/${entry.id}/intelligence`).catch(() => ({ intelligence: null })),
      apiFetch(`/${entry.id}/intelligence/history?limit=10`).catch(() => ({ history: [] }))
    ]);

    const intel = intelData.intelligence;
    const history = historyData.history || [];

    if (!intel) {
      pane.innerHTML = '<div style="padding:20px;color:var(--muted)">暂无情报记录。请先点击"刷新信息"聚合数据源生成情报简报。</div>';
      return;
    }

    let summaryHtml = '';
    const sections = [
      { key: 'price_summary', label: '价格数据' },
      { key: 'news_summary', label: '新闻摘要' },
      { key: 'sec_summary', label: 'SEC文件' },
      { key: 'options_summary', label: '期权数据' },
      { key: 'earnings_summary', label: '财报数据' },
      { key: 'source_scorecard_summary', label: '来源评分卡' },
    ];

    for (const sec of sections) {
      let data = {};
      try { data = intel[sec.key] ? JSON.parse(intel[sec.key]) : {}; } catch { /* skip malformed */ }
      if (Object.keys(data).length > 0) {
        summaryHtml += `
          <div class="wl-intel-section">
            <h4>${sec.label}</h4>
            ${_renderJsonAsHtml(data)}
          </div>`;
      }
    }

    let keySignalsHtml = '';
    const signals = intel.key_signals ? JSON.parse(intel.key_signals) : [];
    if (signals.length > 0) {
      keySignalsHtml = `
        <div class="wl-intel-section">
          <h4>关键信号</h4>
          <ul style="font-size:var(--fs-sm);padding-left:20px;margin:0">
            ${signals.map(s => `<li>${escHtml(s)}</li>`).join('')}
          </ul>
        </div>`;
    }

    let historyHtml = '';
    if (history.length > 1) {
      historyHtml = `
        <div class="wl-intel-section" style="margin-top:24px">
          <h4>情报历史 <span style="font-weight:400;opacity:0.6;font-size:var(--fs-sm)">(${history.length} 个版本)</span></h4>
          <div class="wl-strategy-timeline">`;

      for (const h of history) {
        const isLatest = h.id === intel.id;
        const statusLabel = h.status === 'completed' ? '✓' : h.status === 'failed' ? '✗' : '⋯';
        historyHtml += `
          <div class="wl-timeline-item ${isLatest ? 'is-latest' : ''}" data-history-id="${h.id}">
            <div class="wl-timeline-marker"></div>
            <div class="wl-timeline-content">
              <div class="wl-timeline-header">
                <span style="font-size:var(--fs-xs);font-weight:600">v${h.version}</span>
                <span style="font-size:var(--fs-xs);opacity:0.6">${statusLabel} ${h.status}</span>
                ${isLatest ? '<span style="font-size:var(--fs-xs);color:var(--primary);font-weight:600">当前</span>' : ''}
              </div>
              <div style="font-size:var(--fs-xs);opacity:0.6;margin-top:4px">${fmtBJ(h.created_at)}</div>
              ${h.brief_text ? `<div style="font-size:var(--fs-sm);margin-top:6px;opacity:0.8">${escHtml(h.brief_text.substring(0, 100))}${h.brief_text.length > 100 ? '...' : ''}</div>` : ''}
            </div>
          </div>`;
      }

      historyHtml += `
          </div>
        </div>`;
    }

    pane.innerHTML = `
      <div class="wl-intel-detail">
        <div class="wl-intel-header">
          <div style="font-size:var(--fs-lg);font-weight:600;color:var(--accent)">情报简报 <span style="opacity:0.6;font-size:var(--fs-sm)">v${intel.version}</span></div>
          <div style="font-size:var(--fs-xs);opacity:0.6">${fmtBJ(intel.created_at)}</div>
        </div>
        ${intel.brief_text ? `
          <div class="wl-intel-section">
            <h4>简报文本</h4>
            <p>${escHtml(intel.brief_text)}</p>
          </div>` : ''}
        ${keySignalsHtml}
        ${summaryHtml}
        ${historyHtml}
      </div>`;
  } catch (e) {
    pane.innerHTML = `<div style="color:var(--danger);padding:16px">${escHtml(e.message)}</div>`;
  }
}

/* ── Strategy Tab (策略) ──────────────────────────────── */

async function loadStrategyTab(entry) {
  const pane = document.getElementById('wl-tab-strategy');
  if (!pane) return;
  pane.innerHTML = '<div style="text-align:center;padding:24px;color:var(--muted)">加载中...</div>';

  try {
    const [strategyData, historyData] = await Promise.all([
      apiFetch(`/${entry.id}/strategy`).catch(() => ({ strategy: null })),
      apiFetch(`/${entry.id}/strategy/history?limit=10`).catch(() => ({ history: [] }))
    ]);

    const strategy = strategyData.strategy;
    const history = historyData.history || [];

    if (!strategy) {
      pane.innerHTML = '<div style="padding:20px;color:var(--muted)">暂无策略记录。请先点击"刷新信息"聚合数据，再点击"刷新策略"生成操作策略。</div>';
      return;
    }

    const signalLabel = {bullish: '看多', neutral: '中性', bearish: '看空'}[strategy.signal] || '中性';
    const signalClass = `wl-strategy-signal-${strategy.signal}`;

    let historyHtml = '';
    if (history.length > 1) {
      historyHtml = `
        <div class="wl-strategy-section" style="margin-top:24px">
          <h4>策略历史 <span style="font-weight:400;opacity:0.6;font-size:var(--fs-sm)">(${history.length} 个版本)</span></h4>
          <div class="wl-strategy-timeline">`;

      for (const h of history) {
        const hSignal = {bullish: '看多', neutral: '中性', bearish: '看空'}[h.signal] || '中性';
        const hClass = `wl-strategy-signal-${h.signal}`;
        const isLatest = h.id === strategy.id;
        historyHtml += `
          <div class="wl-timeline-item ${isLatest ? 'is-latest' : ''}" data-history-id="${h.id}">
            <div class="wl-timeline-marker"></div>
            <div class="wl-timeline-content">
              <div class="wl-timeline-header">
                <span class="wl-strategy-signal ${hClass}" style="font-size:var(--fs-xs)">${hSignal}</span>
                <span style="font-size:var(--fs-xs);opacity:0.6">v${h.version}</span>
                <span style="font-size:var(--fs-xs);opacity:0.6">信心 ${h.confidence}/10</span>
                ${isLatest ? '<span style="font-size:var(--fs-xs);color:var(--primary);font-weight:600">当前</span>' : ''}
              </div>
              <div style="font-size:var(--fs-xs);opacity:0.6;margin-top:4px">${fmtBJ(h.created_at)}</div>
              ${h.core_logic ? `<div style="font-size:var(--fs-sm);margin-top:6px;opacity:0.8">${escHtml(h.core_logic.substring(0, 100))}${h.core_logic.length > 100 ? '...' : ''}</div>` : ''}
            </div>
          </div>`;
      }

      historyHtml += `
          </div>
        </div>`;
    }

    pane.innerHTML = `
      <div class="wl-strategy-detail">
        <div class="wl-strategy-header">
          <div class="wl-strategy-signal ${signalClass}">${signalLabel} <span style="opacity:0.6">v${strategy.version}</span></div>
          <div style="flex:1"></div>
          <div style="font-size:var(--fs-sm)">信心：${strategy.confidence}/10</div>
        </div>
        <div class="wl-strategy-section">
          <h4>情报摘要</h4>
          <p>${escHtml(strategy.intelligence_summary || '暂无')}</p>
        </div>
        <div class="wl-strategy-section">
          <h4>核心逻辑</h4>
          <p>${escHtml(strategy.core_logic || '暂无')}</p>
        </div>
        <div class="wl-strategy-section">
          <h4>操作策略</h4>
          ${_renderJsonAsHtml((() => { try { return JSON.parse(strategy.action_strategy || '{}'); } catch { return {}; } })())}
        </div>
        <div class="wl-strategy-section">
          <h4>风险控制</h4>
          ${_renderJsonAsHtml((() => { try { return JSON.parse(strategy.risk_control || '{}'); } catch { return {}; } })())}
        </div>
        ${historyHtml}
      </div>`;
  } catch (e) {
    pane.innerHTML = `<div style="color:var(--danger);padding:16px">${escHtml(e.message)}</div>`;
  }
}

/* ── Init ─────────────────────────────────────────────── */

/* ── Init ─────────────────────────────────────────────── */

export function initWatchlist() {
  initToolbar();
  initAddPanel();
  initTableActions();
  initCardActions();
  initBatchActions();
  initDrawer();
  initRefresh();
  initRefreshIntel();
  initRefreshStrategy();
  initAutoRefresh();
  loadWatchlist();
  loadBudget();
  loadPipelineStatus();
  loadStrategySummaries();
}

// 每次切换到观察池视图时调用：重新拉取列表，确保刚加入/删除的股票实时可见，无需手动刷新。
export async function refreshWatchlistOnEnter() {
  await loadWatchlist();
  loadPipelineStatus();
}
