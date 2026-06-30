/**
 * simtrading.js — 模拟交易独立模块
 */
import { showConfirm } from './utils/confirm.js';

const ST_API = '/api/trading';

const stState = {
  account: null,
  positions: [],
  loading: false,
  market: 'us_stock',
  chartEquity: null,
  equityDays: 30,
  tradesPage: 0,
  tradesLimit: 50,
  activeTab: 'account',
  tabLoaded: {},
};

/* ── 工具函数 ── */
function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function fmtNum(n, digits = 0) {
  if (n == null || isNaN(n)) return '--';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function pnlClass(v) {
  if (v > 0) return 'st-pnl-pos';
  if (v < 0) return 'st-pnl-neg';
  return 'st-pnl-zero';
}

function actionBadge(side) {
  if (side === 'buy') return '<span class="st-badge st-badge-buy">买入</span>';
  if (side === 'sell') return '<span class="st-badge st-badge-sell">卖出</span>';
  return `<span class="st-badge">${esc(side)}</span>`;
}

async function stFetch(path, opts = {}) {
  const sep = path.includes('?') ? '&' : '?';
  const url = `${ST_API}${path}${sep}market=${stState.market}`;
  const resp = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

async function stSSE(url, { onEvent, onDone, onError, method = 'POST', body = null }) {
  try {
    const resp = await fetch(`${ST_API}${url}`, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : null,
    });
    if (!resp.ok) { onError?.(new Error(`${resp.status}`)); return; }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try { onEvent?.(JSON.parse(line.slice(6))); }
          catch { onEvent?.({ raw: line.slice(6) }); }
        }
      }
    }
    onDone?.();
  } catch (e) { onError?.(e); }
}

/* ── 标签页切换 ── */
function switchTab(tab) {
  stState.activeTab = tab;
  document.querySelectorAll('#view-simtrading .st-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('#view-simtrading .st-tab-pane').forEach(p => p.classList.toggle('active', p.id === `st-pane-${tab}`));
  if (!stState.tabLoaded[tab]) {
    stState.tabLoaded[tab] = true;
    loadTabData(tab);
  }
}

/** 进入模拟交易视图时确保当前标签页已加载（修复首次切入空白问题） */
export function ensureSimTradingLoaded() {
  const tab = stState.activeTab || 'account';
  if (!stState.tabLoaded[tab]) {
    stState.tabLoaded[tab] = true;
    loadTabData(tab);
  }
}

function loadTabData(tab) {
  switch (tab) {
    case 'account': loadAccountData(); break;
    case 'positions': loadPositionsTab(); break;
    case 'history': loadTradesTab(); break;
    case 'reviews': loadReviewData(); break;
    case 'operations': loadOperationsData(); break;
  }
}

/* ── 账户盈亏 ── */
async function loadAccountData() {
  try {
    const data = await stFetch('/account');
    stState.account = data.account;
    stState.positions = data.positions || [];
    renderAccountStats(data.account, data.positions);
    loadEquityChart(stState.equityDays);
    loadPerfMonthly();
    loadPerfTickers();
  } catch (e) {
    console.error('加载账户数据失败:', e);
  }
}

function renderAccountStats(account, positions) {
  if (!account) return;
  const posVal = positions.reduce((s, p) => s + (p.market_value || 0), 0);
  const el = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = val; };
  el('st-total-equity', '$' + fmtNum(account.total_equity || account.current_capital, 2));
  el('st-stock-value', '$' + fmtNum(posVal, 2));
  el('st-cash-value', '$' + fmtNum(account.cash_balance, 2));
  const ret = account.total_return_pct || 0;
  const retEl = document.getElementById('st-total-return');
  if (retEl) {
    retEl.textContent = (ret >= 0 ? '+' : '') + fmtNum(ret, 2) + '%';
    retEl.className = 'st-stat-value ' + pnlClass(ret);
  }
  el('st-winrate', fmtNum(account.win_rate || 0, 1) + '%');
  el('st-total-trades', String(account.total_trades || 0));
}

async function loadEquityChart(days) {
  const container = document.getElementById('st-equity-chart');
  if (!container) return;
  try {
    const data = await stFetch(`/account/equity-history?days=${days}`);
    const history = data.history || [];
    if (!history.length) {
      container.innerHTML = '<p class="st-empty-hint">暂无权益数据</p>';
      return;
    }
    if (!stState.chartEquity) {
      stState.chartEquity = echarts.init(container);
    }
    const dates = history.map(h => h.date);
    const values = history.map(h => h.equity);
    const initial = data.initial_capital || values[0];
    const color = values[values.length - 1] >= initial ? '#22c55e' : '#ef4444';
    stState.chartEquity.setOption({
      grid: { top: 20, right: 20, bottom: 30, left: 60 },
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'value', axisLabel: { fontSize: 11 }, splitLine: { lineStyle: { type: 'dashed' } } },
      tooltip: { trigger: 'axis', formatter: p => `${p[0].name}<br/>权益: $${fmtNum(p[0].value, 2)}` },
      series: [{
        type: 'line', data: values, smooth: true,
        lineStyle: { color, width: 2 },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: color + '30' }, { offset: 1, color: color + '05' }] } },
        itemStyle: { color },
      }],
    });
  } catch (e) {
    container.innerHTML = '<p class="st-empty-hint">加载失败</p>';
  }
}

async function loadPerfMonthly() {
  const el = document.getElementById('st-perf-monthly');
  if (!el) return;
  try {
    const data = await stFetch('/performance/monthly?months=6');
    const rows = data.monthly || [];
    if (!rows.length) { el.innerHTML = '<p class="st-empty-hint">暂无数据</p>'; return; }
    el.innerHTML = `<table class="st-table"><thead><tr><th>月份</th><th>交易</th><th>胜率</th><th>收益率</th></tr></thead><tbody>` +
      rows.map(r => `<tr><td>${esc(r.month)}</td><td>${r.trades}</td><td>${fmtNum(r.win_rate, 1)}%</td><td class="${pnlClass(r.total_return_pct)}">${(r.total_return_pct >= 0 ? '+' : '') + fmtNum(r.total_return_pct, 2)}%</td></tr>`).join('') +
      `</tbody></table>`;
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

async function loadPerfTickers() {
  const el = document.getElementById('st-perf-tickers');
  if (!el) return;
  try {
    const data = await stFetch('/performance/tickers');
    const rows = data.tickers || [];
    if (!rows.length) { el.innerHTML = '<p class="st-empty-hint">暂无数据</p>'; return; }
    el.innerHTML = `<table class="st-table"><thead><tr><th>Ticker</th><th>交易</th><th>胜率</th><th>平均收益</th><th>最佳</th><th>最差</th></tr></thead><tbody>` +
      rows.map(r => `<tr><td style="font-weight:600">${esc(r.ticker)}</td><td>${r.trades}</td><td>${fmtNum(r.win_rate, 1)}%</td><td class="${pnlClass(r.avg_return_pct)}">${fmtNum(r.avg_return_pct, 2)}%</td><td class="st-pnl-pos">${fmtNum(r.best_pct, 2)}%</td><td class="st-pnl-neg">${fmtNum(r.worst_pct, 2)}%</td></tr>`).join('') +
      `</tbody></table>`;
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

/* ── 当前持仓 ── */
async function loadPositionsTab() {
  try {
    const showZero = document.getElementById('st-show-zero')?.checked ?? true;
    const data = await stFetch(`/positions?include_zero=${showZero}`);
    const positions = data.positions || [];
    stState.positions = positions;
    renderPositions(positions);
  } catch (e) {
    console.error('加载持仓失败:', e);
  }
}

function renderPositions(positions) {
  const tbody = document.getElementById('st-positions-body');
  const empty = document.getElementById('st-positions-empty');
  if (!tbody) return;
  if (!positions.length) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  tbody.innerHTML = positions.map(p => {
    const isZero = (p.shares || 0) === 0;
    const cls = isZero ? 'st-pos-zero' : '';
    const pnl = p.unrealized_pnl || 0;
    const deleteBtn = isZero ? `<button class="st-btn-delete" data-pos-id="${esc(p.id)}" title="删除记录">✕</button>` : '';
    return `<tr class="${cls}" data-ticker="${esc(p.ticker)}">` +
      `<td><span class="st-pos-expand" data-ticker="${esc(p.ticker)}">▶</span></td>` +
      `<td style="font-weight:600">${esc(p.ticker)}</td>` +
      `<td>${fmtNum(p.shares)}</td>` +
      `<td>$${fmtNum(p.avg_cost, 2)}</td>` +
      `<td>$${fmtNum(p.current_price, 2)}</td>` +
      `<td>$${fmtNum(p.market_value, 2)}</td>` +
      `<td class="${pnlClass(pnl)}">${(pnl >= 0 ? '+' : '') + '$' + fmtNum(Math.abs(pnl), 2)}</td>` +
      `<td>${fmtNum(p.weight_pct, 1)}%</td>` +
      `<td>${deleteBtn}</td>` +
      `</tr>`;
  }).join('');
}

async function loadPositionHistory(ticker) {
  const tbody = document.getElementById('st-positions-body');
  if (!tbody) return;
  const existingRows = tbody.querySelectorAll(`.st-pos-history[data-parent="${ticker}"]`);
  if (existingRows.length) {
    existingRows.forEach(r => r.remove());
    return;
  }
  try {
    const data = await stFetch(`/positions/${encodeURIComponent(ticker)}/trades`);
    const trades = data.trades || [];
    if (!trades.length) return;
    const parentRow = tbody.querySelector(`tr[data-ticker="${ticker}"]`);
    if (!parentRow) return;
    const html = trades.map(t => {
      const ts = (t.created_at || '').replace('T', ' ').slice(0, 16);
      return `<tr class="st-pos-history" data-parent="${esc(ticker)}">` +
        `<td colspan="2">${ts}</td>` +
        `<td>${actionBadge(t.side)}</td>` +
        `<td>${fmtNum(t.shares)}</td>` +
        `<td>$${fmtNum(t.price, 2)}</td>` +
        `<td colspan="2">$${fmtNum(t.amount, 2)}</td>` +
        `<td colspan="2">${esc(t.reasoning || '')}</td>` +
        `</tr>`;
    }).join('');
    parentRow.insertAdjacentHTML('afterend', html);
  } catch (e) {
    console.error('加载交易历史失败:', e);
  }
}

async function removePosition(posId) {
  if (!await showConfirm('确定删除此已清仓记录？', { danger: true })) return;
  try {
    await stFetch(`/positions/${posId}`, { method: 'DELETE' });
    loadPositionsTab();
  } catch (e) {
    alert('删除失败: ' + e.message);
  }
}

/* ── 历史交易 ── */
async function loadTradesTab(page = 0) {
  stState.tradesPage = page;
  const ticker = document.getElementById('st-history-ticker')?.value?.trim() || '';
  const side = document.getElementById('st-history-side')?.value || '';
  try {
    let url = `/trades?limit=${stState.tradesLimit}&offset=${page * stState.tradesLimit}`;
    if (ticker) url += `&ticker=${encodeURIComponent(ticker)}`;
    if (side) url += `&side=${side}`;
    const data = await stFetch(url);
    const trades = data.trades || [];
    renderTrades(trades);
  } catch (e) {
    console.error('加载交易记录失败:', e);
  }
}

function renderTrades(trades) {
  const tbody = document.getElementById('st-trades-body');
  const empty = document.getElementById('st-trades-empty');
  if (!tbody) return;
  if (!trades.length) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';
  tbody.innerHTML = trades.map(t => {
    const ts = (t.created_at || '').replace('T', ' ').slice(0, 16);
    const reasoning = t.reasoning || '';
    const brief = reasoning.length > 40 ? reasoning.slice(0, 40) + '...' : reasoning;
    return `<tr>` +
      `<td>${esc(ts)}</td>` +
      `<td style="font-weight:600">${esc(t.ticker)}</td>` +
      `<td>${actionBadge(t.side)}</td>` +
      `<td>${fmtNum(t.shares)}</td>` +
      `<td>$${fmtNum(t.price, 2)}</td>` +
      `<td>$${fmtNum(t.amount, 2)}</td>` +
      `<td title="${esc(reasoning)}">${esc(brief)}</td>` +
      `</tr>`;
  }).join('');
}

/* ── 复盘记录 ── */
async function loadReviewData() {
  loadReviews();
  loadExperienceCards();
  loadFeedbackHistory();
}

async function loadReviews() {
  const el = document.getElementById('st-review-list');
  if (!el) return;
  try {
    const data = await stFetch('/reviews?limit=20');
    const reviews = data.reviews || [];
    if (!reviews.length) { el.innerHTML = '<p class="st-empty-hint">暂无复盘记录</p>'; return; }
    el.innerHTML = reviews.map(r => {
      const ret = r.return_pct || 0;
      let body = '';
      try {
        const result = typeof r.result_json === 'string' ? JSON.parse(r.result_json) : r.result_json;
        if (result) {
          if (result.correct_judgments) body += `<div><strong>正确判断：</strong>${esc(result.correct_judgments)}</div>`;
          if (result.mistakes) body += `<div><strong>不足之处：</strong>${esc(result.mistakes)}</div>`;
          if (result.lessons) body += `<div><strong>经验教训：</strong>${esc(result.lessons)}</div>`;
        }
      } catch {}
      return `<div class="st-review-item"><div class="st-review-header"><span class="st-review-ticker">${esc(r.ticker)}</span><span class="st-review-return ${pnlClass(ret)}">${(ret >= 0 ? '+' : '') + fmtNum(ret, 2)}%</span></div><div class="st-review-body">${body || esc(r.lessons_learned || '无详情')}</div></div>`;
    }).join('');
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

async function loadExperienceCards() {
  const el = document.getElementById('st-experience-list');
  if (!el) return;
  try {
    const data = await stFetch('/experience?limit=20');
    const cards = data.cards || [];
    if (!cards.length) { el.innerHTML = '<p class="st-empty-hint">暂无经验卡片</p>'; return; }
    el.innerHTML = cards.map(c => {
      const scopeMap = { global: '全局', sector: '行业', ticker: '个股' };
      const catMap = { pattern: '模式', lesson: '教训', rule: '规则' };
      return `<div class="st-exp-card"><button class="st-btn-delete-card" data-card-id="${esc(c.id)}" title="删除">&times;</button><div class="st-exp-title">${esc(c.title)}</div><div class="st-exp-meta">${esc(scopeMap[c.scope] || c.scope)} · ${esc(catMap[c.category] || c.category)} · 置信度 ${fmtNum((c.confidence || 0) * 100, 0)}%</div><div class="st-exp-content">${esc(c.content)}</div></div>`;
    }).join('');
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

async function loadFeedbackHistory() {
  const el = document.getElementById('st-feedback-list');
  if (!el) return;
  try {
    const data = await stFetch('/feedback?limit=50');
    const items = data.feedback || [];
    if (!items.length) { el.innerHTML = '<p class="st-empty-hint">暂无反馈记录</p>'; return; }
    el.innerHTML = items.map(f => {
      const ts = (f.created_at || '').replace('T', ' ').slice(0, 16);
      const typeLabel = f.feedback_type === 'rejection' ? '否决' : '确认';
      return `<div class="st-review-item"><div class="st-review-header"><span>${esc(ts)} · <strong>${esc(f.ticker)}</strong> · ${typeLabel}</span></div><div class="st-review-body">${esc(f.reason || f.user_note || '无备注')}</div></div>`;
    }).join('');
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

async function runBatchReview() {
  const statusEl = document.getElementById('st-review-status');
  if (statusEl) statusEl.textContent = '正在复盘...';
  await stSSE(`/reviews/run?market=${stState.market}`, {
    onEvent(ev) {
      if (statusEl && ev.status) statusEl.textContent = ev.status;
    },
    onDone() {
      if (statusEl) statusEl.textContent = '复盘完成';
      loadReviews();
      loadExperienceCards();
    },
    onError(e) {
      if (statusEl) statusEl.textContent = '复盘失败: ' + e.message;
    },
  });
}

/* ── 账户操作 ── */
async function loadOperationsData() {
  loadFundHistory();
  loadTuningProposals();
}

async function loadFundHistory() {
  const el = document.getElementById('st-fund-history');
  if (!el) return;
  try {
    const data = await stFetch('/account/fund-ops?limit=20');
    const ops = data.ops || [];
    if (!ops.length) { el.innerHTML = '<p style="font-size:12px;color:var(--muted)">暂无资金操作记录</p>'; return; }
    el.innerHTML = '<h4 style="font-size:12px;font-weight:600;margin-bottom:8px">操作记录</h4>' +
      ops.map(o => {
        const ts = (o.created_at || '').replace('T', ' ').slice(0, 16);
        const sign = o.op_type === 'deposit' ? '+' : '-';
        const cls = o.op_type === 'deposit' ? 'st-pnl-pos' : 'st-pnl-neg';
        return `<div class="st-fund-item"><span>${ts}</span><span class="${cls}">${sign}$${fmtNum(o.amount, 2)}</span><span>${esc(o.note || '')}</span></div>`;
      }).join('');
  } catch {}
}

async function adjustFunds() {
  const type = document.getElementById('st-fund-type')?.value;
  const amount = parseFloat(document.getElementById('st-fund-amount')?.value);
  const note = document.getElementById('st-fund-note')?.value?.trim() || '';
  if (!amount || amount <= 0) { alert('请输入有效金额'); return; }
  try {
    await stFetch('/account/adjust-funds', {
      method: 'POST',
      body: JSON.stringify({ type, amount, note }),
    });
    document.getElementById('st-fund-amount').value = '';
    document.getElementById('st-fund-note').value = '';
    loadFundHistory();
    loadAccountData();
  } catch (e) {
    alert('操作失败: ' + e.message);
  }
}

async function refreshPrices() {
  const statusEl = document.getElementById('st-refresh-status');
  if (statusEl) statusEl.textContent = '刷新中...';
  try {
    await stFetch('/account/refresh-prices', { method: 'POST' });
    if (statusEl) statusEl.textContent = '刷新完成';
    loadAccountData();
    loadPositionsTab();
  } catch (e) {
    if (statusEl) statusEl.textContent = '刷新失败: ' + e.message;
  }
}

async function loadTuningProposals() {
  const el = document.getElementById('st-tuning-list');
  if (!el) return;
  try {
    const data = await stFetch('/tuning?status=proposed&limit=20');
    const proposals = data.proposals || [];
    if (!proposals.length) { el.innerHTML = '<p class="st-empty-hint">暂无调优建议</p>'; return; }
    el.innerHTML = proposals.map(p => {
      return `<div class="st-tuning-item"><div class="st-tuning-header"><span class="st-tuning-param">${esc(p.parameter_name)}</span><span class="st-tuning-change">${esc(String(p.old_value))} → ${esc(String(p.new_value))}</span></div><div class="st-tuning-reason">${esc(p.reason || '')}</div><div class="st-tuning-actions"><button class="btn btn-sm btn-primary st-btn-approve-tuning" data-id="${esc(p.id)}">批准</button><button class="btn btn-sm btn-secondary st-btn-reject-tuning" data-id="${esc(p.id)}">拒绝</button></div></div>`;
    }).join('');
  } catch { el.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

async function generateTuning() {
  const statusEl = document.getElementById('st-tuning-status');
  if (statusEl) statusEl.textContent = '生成中...';
  await stSSE(`/tuning/generate?market=${stState.market}`, {
    onEvent(ev) {
      if (statusEl && ev.status) statusEl.textContent = ev.status;
    },
    onDone() {
      if (statusEl) statusEl.textContent = '生成完成';
      loadTuningProposals();
    },
    onError(e) {
      if (statusEl) statusEl.textContent = '生成失败: ' + e.message;
    },
  });
}

async function approveTuning(id) {
  try {
    await stFetch(`/tuning/${id}/approve`, { method: 'POST' });
    loadTuningProposals();
  } catch (e) { alert('批准失败: ' + e.message); }
}

async function rejectTuning(id) {
  const reason = prompt('拒绝原因（可选）：') || '';
  try {
    await stFetch(`/tuning/${id}/reject?reason=${encodeURIComponent(reason)}`, { method: 'POST' });
    loadTuningProposals();
  } catch (e) { alert('拒绝失败: ' + e.message); }
}

/* ── 删除经验卡片 ── */
async function deleteExperienceCard(cardId) {
  if (!await showConfirm('确定删除此经验卡片？', { danger: true })) return;
  try {
    await stFetch(`/experience/${cardId}`, { method: 'DELETE' });
    loadExperienceCards();
  } catch (e) { alert('删除失败: ' + e.message); }
}

/* ── 初始化 ── */
export function initSimTrading() {
  // 市场切换
  const marketSel = document.getElementById('st-market-select');
  if (marketSel) {
    marketSel.addEventListener('change', (e) => {
      stState.market = e.target.value;
      stState.tabLoaded = {};
      loadAccountData();
    });
  }

  // 标签页切换
  document.querySelectorAll('#view-simtrading .st-tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  // 权益曲线范围切换
  document.querySelectorAll('#view-simtrading .st-range-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#view-simtrading .st-range-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      stState.equityDays = parseInt(btn.dataset.days);
      loadEquityChart(stState.equityDays);
    });
  });

  // 持仓展开/折叠 + 删除 (事件委托)
  document.getElementById('st-positions-body')?.addEventListener('click', e => {
    const expand = e.target.closest('.st-pos-expand');
    if (expand) {
      expand.classList.toggle('open');
      loadPositionHistory(expand.dataset.ticker);
      return;
    }
    const del = e.target.closest('.st-btn-delete');
    if (del) {
      removePosition(del.dataset.posId);
      return;
    }
  });

  // 显示/隐藏已清仓
  document.getElementById('st-show-zero')?.addEventListener('change', () => loadPositionsTab());

  // 交易历史搜索
  document.getElementById('st-history-search')?.addEventListener('click', () => loadTradesTab(0));

  // 批量复盘
  document.getElementById('st-btn-batch-review')?.addEventListener('click', runBatchReview);

  // 经验卡片删除 (事件委托)
  document.getElementById('st-experience-list')?.addEventListener('click', e => {
    const btn = e.target.closest('.st-btn-delete-card');
    if (btn) deleteExperienceCard(btn.dataset.cardId);
  });

  // 资金操作
  document.getElementById('st-btn-adjust-funds')?.addEventListener('click', adjustFunds);
  document.getElementById('st-btn-refresh-prices')?.addEventListener('click', refreshPrices);

  // 调优
  document.getElementById('st-btn-gen-tuning')?.addEventListener('click', generateTuning);
  document.getElementById('st-tuning-list')?.addEventListener('click', e => {
    const approve = e.target.closest('.st-btn-approve-tuning');
    if (approve) { approveTuning(approve.dataset.id); return; }
    const reject = e.target.closest('.st-btn-reject-tuning');
    if (reject) { rejectTuning(reject.dataset.id); return; }
  });

  // 跨模块刷新事件
  window.addEventListener('st-refresh', () => {
    stState.tabLoaded = {};
    if (stState.activeTab) loadTabData(stState.activeTab);
  });

  // 图表resize
  window.addEventListener('resize', () => {
    stState.chartEquity?.resize();
  });
}
