/**
 * decision.js — 决策中心前端模块
 * L1 宏观 / L2 组合 / L3 战术 / L4 执行 / 投委会 / 模拟账户
 */
import { showConfirm } from './utils/confirm.js';

const DC_API = '/api/decision';

const dcState = {
  overview: null,
  loading: false,
  market: 'us_stock',
  chartAlloc: null,
  chartEquity: null,
  catalystView: 'list',
  calendarMonth: null,
  riskChart: null,
  lightErrors: new Set(),   // 本轮决策中失败的模块（保留红灯）
};

/* ── 面板信号灯 ─────────────────────────────────────── */

// SSE 事件 layer → 面板灯 id 后缀
const DC_LIGHT_LAYER_MAP = {
  L1: 'macro', L2: 'strategic', L3: 'tactical', L4: 'pending',
};
// 各模块数据的过期阈值（小时）：L1/L2 周级，L3/L4/催化剂 日级
const DC_LIGHT_STALE_HOURS = {
  macro: 24 * 8, strategic: 24 * 8,
  tactical: 30, pending: 30, committee: 30, catalysts: 24 * 8,
};
const DC_LIGHT_LABELS = {
  gray: '无数据', green: '决策完成，数据有效', blink: '正在分析',
  yellow: '有数据但已过期', red: '决策失败，数据无效',
};

function setLight(panel, state) {
  const el = document.getElementById(`dc-light-${panel}`);
  if (!el) return;
  el.className = `dc-light dc-light-${state}`;
  el.title = DC_LIGHT_LABELS[state] || '';
}

// 根据时间戳判断新鲜/过期
function _lightFreshness(panel, ts) {
  if (!ts) return 'green';
  try {
    const age = (Date.now() - new Date(ts.replace(' ', 'T')).getTime()) / 3600000;
    const limit = DC_LIGHT_STALE_HOURS[panel] || 30;
    return age > limit ? 'yellow' : 'green';
  } catch { return 'green'; }
}

// 从 overview 数据计算各面板静态灯态（绿/黄/灰/红）
function updateLightsFromData(data) {
  data = data || {};
  const macro = data.macro_strategy;
  const strat = data.strategic_plan;
  const tac = data.tactical_plans || [];
  const pend = data.pending_executions || [];
  const cats = data.upcoming_catalysts || [];

  const compute = (panel, present, ts) => {
    if (dcState.lightErrors.has(panel)) return 'red';
    if (!present) return 'gray';
    return _lightFreshness(panel, ts);
  };

  setLight('macro', compute('macro', !!macro, macro && (macro.updated_at || macro.created_at)));
  setLight('strategic', compute('strategic', !!strat, strat && (strat.updated_at || strat.created_at)));
  setLight('tactical', compute('tactical', tac.length > 0,
    tac.length ? (tac[0].created_at || tac[0].plan_date) : null));
  // L4：有待确认操作即为有效绿灯（无过期概念）
  setLight('pending', dcState.lightErrors.has('pending') ? 'red' : (pend.length ? 'green' : 'gray'));
  setLight('catalysts', compute('catalysts', cats.length > 0,
    cats.length ? cats[0].created_at : null));
  // 委员会灯态由 loadMeetings 根据最近会议设置；此处仅在失败时置红
  if (dcState.lightErrors.has('committee')) setLight('committee', 'red');
}

// SSE 事件 → 动态灯态（闪烁/绿/红）
function handleLightEvent(data) {
  const evt = data.event || '';
  const layer = data.layer || '';
  const panel = DC_LIGHT_LAYER_MAP[layer];

  // 决策层（L1-L4）
  if (panel) {
    if (evt === 'decision_error') { setLight(panel, 'red'); dcState.lightErrors.add(panel); }
    else if (evt === 'decision_done') { setLight(panel, 'green'); dcState.lightErrors.delete(panel); }
    else if (evt === 'decision_start' || evt === 'decision_progress') setLight(panel, 'blink');
  }
  // 投委会
  if (evt.startsWith('committee_')) {
    if (evt === 'committee_error') { setLight('committee', 'red'); dcState.lightErrors.add('committee'); }
    else if (evt === 'committee_done') { setLight('committee', 'green'); dcState.lightErrors.delete('committee'); }
    else if (evt === 'committee_start' || evt === 'committee_plan_start') setLight('committee', 'blink');
  }
  // 催化剂扫描
  if (evt === 'catalyst_scan_start' || evt === 'catalyst_judge_start') setLight('catalysts', 'blink');
  if (evt === 'catalyst_scan_done' || evt === 'catalyst_judge_done') setLight('catalysts', 'green');
}

// 决策开始时：把本轮 scope 内的模块灯重置为待运行（清除上轮红灯/绿灯，准备闪烁）
function resetLightsForRun(scope) {
  dcState.lightErrors.clear();
  const panels = scope === 'l3l4'
    ? ['tactical', 'pending', 'committee']
    : ['macro', 'strategic', 'tactical', 'pending', 'committee'];
  panels.forEach(p => setLight(p, 'gray'));
}

/* ── helpers ─────────────────────────────────────────── */

function escDC(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function fmtNum(n, digits = 0) {
  if (n == null) return '--';
  return Number(n).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pnlClass(v) {
  if (v > 0) return 'dc-pnl-pos';
  if (v < 0) return 'dc-pnl-neg';
  return 'dc-pnl-zero';
}

function actionBadge(action) {
  const map = {
    buy: ['买入', 'dc-badge-buy'],
    add: ['加仓', 'dc-badge-buy'],
    sell: ['卖出', 'dc-badge-sell'],
    reduce: ['减仓', 'dc-badge-sell'],
    hold: ['持有', 'dc-badge-hold'],
    wait_for_pullback: ['等待回调', 'dc-badge-hold'],
    wait_for_catalyst: ['等待催化', 'dc-badge-hold'],
  };
  const [label, cls] = map[action] || [action, 'dc-badge'];
  return `<span class="dc-badge ${cls}">${escDC(label)}</span>`;
}

async function dcFetch(path, opts = {}) {
  const res = await fetch(`${DC_API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

/* ── SSE 流式读取 ──────────────────────────────────── */

async function dcSSE(url, { onEvent, onDone, onError, method = 'POST', body = null }) {
  showProgress('连接中...');
  try {
    const res = await fetch(`${DC_API}${url}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : null,
    });
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
            if (onEvent) onEvent(data);
          } catch {}
        }
      }
    }
    if (onDone) onDone();
  } catch (e) {
    if (onError) onError(e);
    else console.error('DC SSE error:', e);
  }
  hideProgress();
}

/* ── 进度条 ───────────────────────────────────────── */

function showProgress(text) {
  const el = document.getElementById('dc-progress');
  if (el) el.style.display = '';
  dcState.progressStart = Date.now();
  if (dcState.progressTimer) clearInterval(dcState.progressTimer);
  dcState.progressTimer = setInterval(() => {
    const elapsed = Math.floor((Date.now() - dcState.progressStart) / 1000);
    const m = String(Math.floor(elapsed / 60)).padStart(2, '0');
    const s = String(elapsed % 60).padStart(2, '0');
    const timerEl = document.getElementById('dc-progress-timer');
    if (timerEl) timerEl.textContent = `${m}:${s}`;
  }, 1000);
  setProgress(0, text);
}

function hideProgress() {
  const el = document.getElementById('dc-progress');
  if (el) el.style.display = 'none';
  if (dcState.progressTimer) {
    clearInterval(dcState.progressTimer);
    dcState.progressTimer = null;
  }
}

function setProgress(pct, text) {
  const val = Math.min(100, Math.round(pct));
  const fill = document.getElementById('dc-progress-fill');
  const txt = document.getElementById('dc-progress-text');
  const pctEl = document.getElementById('dc-progress-pct');
  if (fill) fill.style.width = `${val}%`;
  if (txt && text) txt.textContent = text;
  if (pctEl) pctEl.textContent = `${val}%`;
}

/* ── 数据加载 ─────────────────────────────────────── */

async function loadOverview() {
  if (dcState.loading) return;
  dcState.loading = true;
  try {
    const data = await dcFetch(`/overview?market=${encodeURIComponent(dcState.market)}`);
    dcState.overview = data;
    renderAll(data);
  } catch (e) {
    console.error('Failed to load decision overview:', e);
  }
  dcState.loading = false;
}

function renderAll(data) {
  renderMacro(data.macro_strategy);
  renderStrategic(data.strategic_plan);
  renderTactical(data.tactical_plans || []);
  renderPending(data.pending_executions || []);
  loadBlocked();
  renderCatalysts(data.upcoming_catalysts || []);
  renderCommittee(data.committee || [], data.committee_meta);
  loadRiskDashboard();
  loadMeetings();
  loadModelRatings();
  updateLightsFromData(data);
}

/* ── L1 宏观 ──────────────────────────────────────── */

function renderMacro(macro) {
  const body = document.getElementById('dc-macro-body');
  const badge = document.getElementById('dc-macro-risk');
  if (!body) return;

  if (!macro) {
    body.innerHTML = '<p class="dc-empty-hint">尚未生成宏观策略，点击"一键日常决策"开始</p>';
    if (badge) badge.textContent = '--';
    return;
  }

  let rj = macro.result_json;
  if (typeof rj === 'string') {
    try { rj = JSON.parse(rj); } catch { rj = {}; }
  }
  rj = rj || {};

  const riskLevel = rj.risk_level || rj.market_risk || '--';
  if (badge) {
    badge.textContent = riskLevel;
    badge.className = 'dc-badge ' + (
      riskLevel === '高' ? 'dc-badge-sell' :
      riskLevel === '低' ? 'dc-badge-buy' : 'dc-badge-info'
    );
  }

  const fields = [
    ['市场总结', rj.market_summary],
    ['趋势判断', rj.trend_assessment || rj.trend],
    ['关键风险', rj.key_risks],
    ['建议仓位', rj.position_suggestion || rj.recommended_position],
  ].filter(([, v]) => v);

  let html = '<div class="dc-macro-content">';
  for (const [label, value] of fields) {
    const display = Array.isArray(value) ? value.join('、') : String(value);
    html += `<div class="dc-macro-field">
      <div class="dc-macro-field-label">${escDC(label)}</div>
      <div class="dc-macro-field-value">${escDC(display)}</div>
    </div>`;
  }
  const ts = macro.created_at || '';
  if (ts) html += `<div style="font-size:11px;color:var(--muted);margin-top:8px">更新于 ${escDC(ts.replace('T', ' ').slice(0, 16))}</div>`;
  html += '</div>';
  body.innerHTML = html;
}

/* ── L2 组合 ──────────────────────────────────────── */

function renderStrategic(plan) {
  const empty = document.getElementById('dc-strategic-empty');
  const chartEl = document.getElementById('dc-alloc-chart');
  const parent = chartEl?.parentElement;
  if (!plan) {
    if (empty) empty.style.display = '';
    if (chartEl) chartEl.innerHTML = '';
    if (parent) { const old = parent.querySelector('.dc-strategic-info'); if (old) old.remove(); }
    return;
  }
  if (empty) empty.style.display = 'none';

  let rj = plan.result_json;
  if (typeof rj === 'string') {
    try { rj = JSON.parse(rj); } catch { rj = {}; }
  }
  rj = rj || {};

  let ss = plan.stock_selection || rj.stock_selection;
  if (typeof ss === 'string') { try { ss = JSON.parse(ss); } catch { ss = {}; } }
  ss = ss || {};
  const core = Array.isArray(ss.core_holdings) ? ss.core_holdings : [];
  const tactical = Array.isArray(ss.tactical_holdings) ? ss.tactical_holdings : [];
  const holdings = core.concat(tactical).filter(h => h.ticker && h.target_weight_pct > 0);

  const alloc = rj.target_allocation || {};
  const stance = rj.overall_stance || rj.stance || '';

  if (chartEl && typeof echarts !== 'undefined' && holdings.length > 0) {
    const chart = dcState.chartAlloc || echarts.init(chartEl);
    dcState.chartAlloc = chart;
    const pieData = holdings.map(h => ({
      name: h.ticker,
      value: h.target_weight_pct,
    }));
    const usedPct = pieData.reduce((s, d) => s + d.value, 0);
    if (usedPct < 100) pieData.push({ name: '现金/其他', value: Math.round(100 - usedPct) });

    chart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c}%' },
      series: [{
        type: 'pie',
        radius: ['40%', '70%'],
        label: { color: 'var(--ink)', fontSize: 11 },
        data: pieData,
      }],
    });
  } else if (chartEl) {
    chartEl.innerHTML = '';
  }

  if (parent) {
    let infoEl = parent.querySelector('.dc-strategic-info');
    if (!infoEl) {
      infoEl = document.createElement('div');
      infoEl.className = 'dc-strategic-info';
      parent.appendChild(infoEl);
    }

    let html = '';
    if (stance) {
      const cls = stance.includes('防御') ? 'dc-badge-sell' : stance.includes('进攻') ? 'dc-badge-buy' : 'dc-badge-info';
      html += `<div style="margin-bottom:8px"><span class="dc-badge ${cls}">${escDC(stance)}</span></div>`;
    }

    if (typeof alloc === 'object' && !Array.isArray(alloc) && Object.keys(alloc).length > 0) {
      const labels = { equity_pct: '权益', cash_pct: '现金', hedge_pct: '对冲', bond_pct: '债券' };
      html += '<div class="dc-alloc-summary">';
      for (const [k, v] of Object.entries(alloc)) {
        if (typeof v === 'number') html += `<span class="dc-alloc-tag">${escDC(labels[k] || k)} ${v}%</span>`;
      }
      html += '</div>';
    }

    if (holdings.length > 0) {
      html += '<ul class="dc-alloc-list">';
      for (const h of holdings) {
        const tag = core.includes(h) ? '核心' : '战术';
        html += `<li><span>${escDC(h.ticker)} <small class="text-muted">${escDC(tag)}</small></span><span>${h.target_weight_pct}%</span></li>`;
      }
      html += '</ul>';
    }

    if (!holdings.length && !stance) {
      const text = rj.strategy_text || rj.summary || '';
      if (text) html += `<p class="dc-strategic-text">${escDC(text)}</p>`;
      else html += '<p class="dc-empty-hint">组合策略数据格式异常</p>';
    }

    const ts = plan.created_at || '';
    if (ts) html += `<div style="font-size:11px;color:var(--muted);margin-top:8px">更新于 ${escDC(ts.replace('T', ' ').slice(0, 16))}</div>`;

    infoEl.innerHTML = html;
  }
}

/* ── L3 战术 ──────────────────────────────────────── */

function renderTactical(plans) {
  const tbody = document.getElementById('dc-tactical-body');
  const empty = document.getElementById('dc-tactical-empty');
  const countBadge = document.getElementById('dc-tactical-count');

  if (countBadge) countBadge.textContent = plans.length;
  if (!tbody) return;

  if (plans.length === 0) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  tbody.innerHTML = plans.map(p => {
    let rj = p.result_json;
    if (typeof rj === 'string') {
      try { rj = JSON.parse(rj); } catch { rj = {}; }
    }
    rj = rj || {};

    const action = rj.action || p.action || '--';
    const entryPlan = rj.entry_plan || {};
    const exitPlan = rj.exit_plan || {};
    const riskAss = rj.risk_assessment || {};
    const entryPrice = entryPlan.ideal_price || rj.target_price || rj.entry_price || '--';
    const stopLoss = exitPlan.stop_loss?.price ?? '--';
    const targets = exitPlan.target_prices || [];
    const takeProfit = targets.length > 0 ? targets[0].price : '--';
    const confidence = riskAss.confidence ?? rj.confidence ?? '--';

    return `<tr>
      <td><strong>${escDC(p.ticker || rj.ticker)}</strong></td>
      <td>${actionBadge(action)}</td>
      <td>${entryPrice !== '--' ? fmtNum(entryPrice, 2) : '--'}</td>
      <td>${stopLoss !== '--' ? fmtNum(stopLoss, 2) : '--'}</td>
      <td>${takeProfit !== '--' ? fmtNum(takeProfit, 2) : '--'}</td>
      <td>${confidence !== '--' ? confidence + '/10' : '--'}</td>
    </tr>`;
  }).join('');
}

/* ── L4 待确认 ────────────────────────────────────── */

function renderPending(executions) {
  const list = document.getElementById('dc-pending-list');
  const empty = document.getElementById('dc-pending-empty');
  const countBadge = document.getElementById('dc-pending-count');

  if (countBadge) countBadge.textContent = executions.length;
  if (!list) return;

  if (executions.length === 0) {
    list.innerHTML = '<p class="dc-empty-hint" id="dc-pending-empty">暂无待确认计划</p>';
    return;
  }
  if (empty) empty.style.display = 'none';

  list.innerHTML = executions.map(ex => {
    let rj = ex.result_json;
    if (typeof rj === 'string') {
      try { rj = JSON.parse(rj); } catch { rj = {}; }
    }
    rj = rj || {};

    const action = ex.action || rj.action || '--';
    const shares = ex.shares || rj.shares || '--';
    const price = ex.target_price || rj.target_price || '--';
    const reasoning = rj.reasoning || '';
    let flags = '';
    if (rj.committee_modified) flags += '<span class="dc-pending-flag dc-flag-committee">投委会调整</span>';
    if (rj.auto_repaired) flags += '<span class="dc-pending-flag dc-flag-repair">自修正</span>';
    if (rj.auto_adjusted) flags += '<span class="dc-pending-flag dc-flag-adjust">已缩量</span>';

    return `<div class="dc-pending-item" data-plan-id="${escDC(ex.id)}">
      <div class="dc-pending-header">
        <span class="dc-pending-ticker">${escDC(ex.ticker)} ${actionBadge(action)} ${flags}</span>
        <span style="font-size:12px;color:var(--muted)">${shares}股 @ ${price !== '--' ? fmtNum(Number(price), 2) : '--'}</span>
      </div>
      ${reasoning ? `<div class="dc-pending-detail">${escDC(reasoning)}</div>` : ''}
      <div class="dc-pending-actions">
        <button class="dc-btn-confirm" data-action="confirm" data-plan-id="${escDC(ex.id)}">确认执行</button>
        <button class="dc-btn-reject" data-action="reject" data-plan-id="${escDC(ex.id)}">拒绝</button>
      </div>
    </div>`;
  }).join('');

  list.innerHTML += `<div style="text-align:right;padding:8px 4px 0">
    <button class="dc-btn-reject" id="dc-clear-all-pending" style="font-size:12px">清空所有操作</button>
  </div>`;
}

/* ── 已拦截区（被系统/投委会拦截）──────────────────── */

async function loadBlocked() {
  const container = document.getElementById('dc-blocked-section');
  if (!container) return;
  let executions = [];
  try {
    const data = await dcFetch(`/executions/blocked?market=${encodeURIComponent(dcState.market)}`);
    executions = data.executions || [];
  } catch { executions = []; }
  renderBlocked(executions);
}

function renderBlocked(executions) {
  const container = document.getElementById('dc-blocked-section');
  if (!container) return;

  if (!executions.length) {
    container.innerHTML = '';
    container.style.display = 'none';
    return;
  }
  container.style.display = '';

  const items = executions.map(ex => {
    let rj = ex.result_json;
    if (typeof rj === 'string') { try { rj = JSON.parse(rj); } catch { rj = {}; } }
    rj = rj || {};
    const action = ex.action || rj.action || '--';
    const shares = ex.shares || rj.shares || '--';
    const price = ex.target_price || rj.target_price || '--';
    const reason = ex.rejection_reason || '';
    const isCommittee = reason.indexOf('[投委会否决]') === 0;
    const tag = isCommittee
      ? '<span class="dc-blocked-tag dc-blocked-tag--committee">投委会否决</span>'
      : '<span class="dc-blocked-tag dc-blocked-tag--system">不合规拦截</span>';
    const cleanReason = reason.replace(/^\[(系统拦截|投委会否决)\]\s*/, '');
    return `<div class="dc-blocked-item" data-plan-id="${escDC(ex.id)}">
      <div class="dc-pending-header">
        <span class="dc-pending-ticker">${escDC(ex.ticker)} ${actionBadge(action)} ${tag}</span>
        <span style="font-size:12px;color:var(--muted)">${shares}股 @ ${price !== '--' ? fmtNum(Number(price), 2) : '--'}</span>
      </div>
      <div class="dc-blocked-reason">${escDC(cleanReason)}</div>
      <div class="dc-pending-actions">
        <button class="dc-btn-restore" data-action="restore" data-plan-id="${escDC(ex.id)}">恢复到待确认</button>
      </div>
    </div>`;
  }).join('');

  container.innerHTML = `
    <div class="dc-card-header" style="cursor:pointer" id="dc-blocked-toggle">
      <h3>已拦截操作 <span class="dc-card-toggle">▼</span></h3>
      <span class="dc-badge dc-badge-danger">${executions.length}</span>
    </div>
    <div class="dc-card-body" id="dc-blocked-list">${items}</div>`;
}

async function handleBlockedAction(e) {
  const btn = e.target.closest('[data-action="restore"]');
  if (!btn) return;
  const planId = btn.dataset.planId;
  btn.disabled = true;
  btn.textContent = '恢复中...';
  try {
    await dcFetch(`/executions/${encodeURIComponent(planId)}/restore`, { method: 'POST' });
    await loadOverview();
    await loadBlocked();
  } catch (err) {
    alert('恢复失败: ' + err.message);
    btn.disabled = false;
    btn.textContent = '恢复到待确认';
  }
}

/* ── 确认 / 拒绝 ──────────────────────────────────── */

async function handlePendingAction(e) {
  /* 清空所有操作 */
  const clearBtn = e.target.closest('#dc-clear-all-pending');
  if (clearBtn) {
    if (!confirm('确定清空所有待执行操作？此操作不可撤销。')) return;
    clearBtn.disabled = true;
    clearBtn.textContent = '清空中...';
    try {
      const res = await dcFetch('/executions/clear-all', { method: 'POST' });
      alert(`已清空 ${res.cleared} 条操作`);
      await loadOverview();
    } catch (err) {
      alert('清空失败: ' + err.message);
      clearBtn.disabled = false;
      clearBtn.textContent = '清空所有操作';
    }
    return;
  }

  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const planId = btn.dataset.planId;
  const action = btn.dataset.action;

  btn.disabled = true;
  btn.textContent = action === 'confirm' ? '执行中...' : '处理中...';

  try {
    if (action === 'confirm') {
      const res = await dcFetch(`/executions/${encodeURIComponent(planId)}/confirm`, { method: 'POST' });
      const trade = res.trade || {};
      const msg = trade.error
        ? `执行失败: ${trade.error}`
        : `${trade.side === 'buy' ? '买入' : '卖出'} ${trade.ticker} ${trade.shares}股 @ ${fmtNum(trade.price, 2)}`;
      alert(msg);
    } else {
      const reason = prompt('拒绝原因（可选）:') || '';
      await dcFetch(`/executions/${encodeURIComponent(planId)}/reject`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      });
    }
    await loadOverview();
    if (window.appState) window.appState.tradingDirty = true;
  } catch (e) {
    alert('操作失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = action === 'confirm' ? '确认执行' : '拒绝';
  }
}

/* ── 催化剂 ───────────────────────────────────────── */

function renderCatalysts(catalysts) {
  const body = document.getElementById('dc-catalysts-body');
  if (!body) return;

  if (!catalysts || catalysts.length === 0) {
    body.innerHTML = '<p class="dc-empty-hint">暂无催化剂事件</p>';
    return;
  }

  body.innerHTML = catalysts.map(c => {
    const date = (c.expected_date || c.date || '').slice(0, 10);
    return `<div class="dc-catalyst-item">
      <span class="dc-catalyst-date">${escDC(date)}</span>
      <span class="dc-catalyst-text">
        <span class="dc-catalyst-ticker">${escDC(c.ticker)}</span>
        ${escDC(c.event_type || c.catalyst_type || '')} — ${escDC(c.description || '')}
      </span>
    </div>`;
  }).join('');
}

/* ── 投委会 ───────────────────────────────────────── */

function renderCommittee(reviews, meta) {
  const body = document.getElementById('dc-committee-body');
  if (!body) return;

  if (!reviews || reviews.length === 0) {
    body.innerHTML = '<p class="dc-empty-hint">暂无评审记录</p>';
    return;
  }

  let header = '';
  if (meta && (meta.ticker || meta.verdict)) {
    const vCls = (meta.verdict || '').includes('approved') ? 'approve'
      : (meta.verdict || '').includes('rejected') ? 'reject' : 'conditional';
    const date = (meta.created_at || '').replace('T', ' ').slice(0, 16);
    header = `<div class="dc-committee-meta">
      <span class="dc-committee-ticker">${escDC(meta.ticker || '')}</span>
      <span class="dc-member-decision ${vCls}">${escDC(_voteLabel(meta.verdict || '--'))}</span>
      ${meta.approval_rate != null ? `<span class="dc-tr-conf">通过率 ${Math.round(meta.approval_rate)}%</span>` : ''}
      <span class="dc-tr-conf">${escDC(date)}</span>
    </div>`;
  }

  const votesHtml = `<div class="dc-votes-grid">${reviews.map(r => {
    let rj = r.result_json;
    if (typeof rj === 'string') {
      try { rj = JSON.parse(rj); } catch { rj = {}; }
    }
    rj = rj || {};

    const decision = rj.decision || rj.vote || '--';
    const decClass = decision.includes('approve') || decision.includes('通过') ? 'approve'
      : decision.includes('reject') || decision.includes('否决') ? 'reject' : 'conditional';
    const reasoning = rj.overall_assessment || rj.reasoning || rj.summary || '';
    const conf = rj.confidence != null ? rj.confidence : null;
    const concerns = Array.isArray(rj.key_concerns) ? rj.key_concerns : [];
    // 因 LLM 调用失败而弃权：明确标注，区分于真实弃权
    const errAbstain = (decision === 'abstain' && rj.error) ? rj.error : '';

    return `<div class="dc-member-vote">
      <div class="dc-member-name">${escDC(r.member_name || r.reviewer || r.member_role || '--')}${conf != null && !errAbstain ? ` <span class="dc-tr-conf">信心 ${conf}/10</span>` : ''}</div>
      <div class="dc-member-decision ${errAbstain ? 'conditional' : decClass}">${errAbstain ? '弃权（系统错误）' : escDC(_voteLabel(decision))}</div>
      ${errAbstain
        ? `<div class="dc-member-reasoning dc-muted">模型调用失败，本次未参与投票：${escDC(String(errAbstain).slice(0, 80))}</div>`
        : `<div class="dc-member-reasoning">${escDC(String(reasoning))}</div>
           ${concerns.length ? `<div class="dc-tr-sub"><b>关注：</b>${concerns.slice(0, 3).map(c => escDC(typeof c === 'string' ? c : JSON.stringify(c))).join('；')}</div>` : ''}`}
    </div>`;
  }).join('')}</div>`;

  // 集体结论
  let conclusion = '';
  if (meta && (meta.summary || (meta.modifications && meta.modifications.length) || (meta.risks && meta.risks.length))) {
    const mods = Array.isArray(meta.modifications) ? meta.modifications : [];
    const risks = Array.isArray(meta.risks) ? meta.risks : [];
    conclusion = `<div class="dc-committee-conclusion">
      <div class="dc-cc-title">📋 集体结论</div>
      ${meta.summary ? `<div class="dc-cc-summary">${escDC(meta.summary)}</div>` : ''}
      ${mods.length ? `<div class="dc-tr-sub"><b>共识修改：</b>${mods.map(m => escDC(typeof m === 'object'
        ? `${m.ticker || ''} ${m.field || ''}: ${m.original ?? ''}→${m.modified ?? ''}（${m.reason || ''}）`
        : String(m))).join('；')}</div>` : ''}
      ${risks.length ? `<div class="dc-tr-sub dc-cc-risks"><b>风险提示：</b>${risks.map(r => escDC(typeof r === 'string' ? r : JSON.stringify(r))).join('；')}</div>` : ''}
    </div>`;
  }

  body.innerHTML = header + votesHtml + conclusion;
}

/* ── 操作按钮 ──────────────────────────────────────── */

async function runDaily() {
  if (dcState.loading) return;
  dcState.loading = true;
  let step = 0;
  const totalSteps = 5;
  resetLightsForRun('full');

  await dcSSE('/daily', {
    body: { scope: 'full', market: dcState.market },
    onEvent(data) {
      const evt = data.event || '';
      const msg = data.message || data.error || evt;
      handleLightEvent(data);
      if (evt.includes('_start') || evt.includes('decision_start')) step++;
      if (evt.includes('_error')) {
        setProgress(step / totalSteps * 100, `[${data.layer || ''}] 错误: ${data.error || msg}`);
        return;
      }
      setProgress(Math.min(95, (step / totalSteps) * 100), msg);
    },
    async onDone() {
      setProgress(100, '完成');
      dcState.loading = false;
      await loadOverview();
    },
    async onError(e) {
      setProgress(0, `错误: ${e.message}`);
      dcState.loading = false;
      // 即使 SSE 流中途异常，也刷新面板展示已落库的 L1-L4/投委会结果，避免面板空白
      await loadOverview().catch(() => {});
    },
  });
  dcState.loading = false;
}

async function runFullRefresh() {
  if (dcState.loading) return;
  if (!await showConfirm('全量刷新将重新运行 L1→L2→L3→L4→投委会全流程，确认继续？')) return;
  dcState.loading = true;
  let step = 0;
  resetLightsForRun('full');

  await dcSSE('/full-refresh', {
    body: { market: dcState.market },
    onEvent(data) {
      const evt = data.event || '';
      const msg = data.message || data.error || evt;
      handleLightEvent(data);
      if (evt.includes('_start') || evt.includes('decision_start')) step++;
      if (evt.includes('_error')) {
        setProgress(step / 6 * 100, `[${data.layer || ''}] 错误: ${data.error || msg}`);
        return;
      }
      setProgress(Math.min(95, (step / 6) * 100), msg);
    },
    async onDone() {
      setProgress(100, '全量刷新完成');
      dcState.loading = false;
      await loadOverview();
    },
    async onError(e) {
      setProgress(0, `错误: ${e.message}`);
      dcState.loading = false;
      await loadOverview().catch(() => {});
    },
  });
  dcState.loading = false;
}

async function scanCatalysts() {
  if (dcState.loading) return;
  dcState.loading = true;

  await dcSSE('/catalysts/scan', {
    body: { market: dcState.market },
    onEvent(data) {
      const evt = data.event || '';
      const msg = data.message || data.error || '扫描中...';
      if (evt.includes('_error')) {
        setProgress(50, `扫描错误: ${data.error || msg}`);
        return;
      }
      setProgress(50, msg);
    },
    async onDone() {
      setProgress(100, '催化剂扫描完成');
      dcState.loading = false;
      await loadOverview();
    },
    async onError(e) {
      setProgress(0, `扫描失败: ${e.message}`);
      dcState.loading = false;
      await loadOverview().catch(() => {});
    },
  });
  dcState.loading = false;
}

/* ── 初始化 ───────────────────────────────────────── */

export function initDecision() {
  document.getElementById('dc-btn-daily')?.addEventListener('click', runDaily);
  document.getElementById('dc-btn-refresh')?.addEventListener('click', runFullRefresh);
  document.getElementById('dc-btn-catalysts')?.addEventListener('click', scanCatalysts);
  document.getElementById('dc-btn-ai-config')?.addEventListener('click', openAIConfig);
  document.getElementById('dc-btn-compare')?.addEventListener('click', openComparePanel);

  // 左列卡片折叠
  document.querySelectorAll('.dc-col-main .dc-card-header').forEach(header => {
    header.addEventListener('click', () => {
      header.closest('.dc-card').classList.toggle('collapsed');
    });
  });

  // 市场切换
  const marketSel = document.getElementById('dc-market-select');
  if (marketSel) {
    marketSel.addEventListener('change', (e) => {
      dcState.market = e.target.value;
      dcState.overview = null;
      loadOverview();
    });
  }

  // 催化剂视图切换
  document.getElementById('dc-catalyst-list-btn')?.addEventListener('click', () => {
    document.getElementById('dc-catalyst-list-btn')?.classList.add('active');
    document.getElementById('dc-catalyst-cal-btn')?.classList.remove('active');
    dcState.catalystView = 'list';
    if (dcState.overview) renderCatalysts(dcState.overview.upcoming_catalysts || []);
  });
  document.getElementById('dc-catalyst-cal-btn')?.addEventListener('click', () => {
    document.getElementById('dc-catalyst-cal-btn')?.classList.add('active');
    document.getElementById('dc-catalyst-list-btn')?.classList.remove('active');
    dcState.catalystView = 'calendar';
    dcState.calendarMonth = null; // 当前月
    loadCatalystCalendar();
  });

  document.getElementById('dc-ai-config-close')?.addEventListener('click', closeAIConfig);
  document.getElementById('dc-ai-config-cancel')?.addEventListener('click', closeAIConfig);
  document.getElementById('dc-ai-config-save')?.addEventListener('click', saveAIConfig);

  const aiModal = document.getElementById('dc-ai-config-modal');
  if (aiModal) {
    aiModal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closeAIConfig();
    });
  }

  document.getElementById('dc-pending-list')?.addEventListener('click', handlePendingAction);
  document.getElementById('dc-blocked-section')?.addEventListener('click', handleBlockedAction);

  // 会议类型筛选
  document.getElementById('dc-meeting-type')?.addEventListener('change', () => loadMeetings());

  // 模型校准按钮
  document.getElementById('dc-btn-calibrate')?.addEventListener('click', runCalibration);

  // 会议详情抽屉
  const meetingDrawer = document.getElementById('dc-meeting-drawer');
  document.getElementById('dc-meeting-drawer-close')?.addEventListener('click', closeMeetingDrawer);
  meetingDrawer?.addEventListener('click', (e) => {
    if (e.target === meetingDrawer) closeMeetingDrawer();
  });

  const navBtn = document.querySelector('.nav-btn[data-view="trading"]');
  if (navBtn) {
    navBtn.addEventListener('click', () => {
      if (!dcState.overview) loadOverview();
      loadSchedulerStatus();
    });
  }

  window.addEventListener('resize', () => {
    dcState.chartAlloc?.resize();
    dcState.chartEquity?.resize();
    dcState.riskChart?.resize();
  });
}

/* ── 调度状态栏 ──────────────────────────────────── */

async function loadSchedulerStatus() {
  const bar = document.getElementById('dc-scheduler-bar');
  if (!bar) return;
  try {
    const data = await dcFetch('/scheduler-status');
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      bar.style.display = 'none';
      return;
    }
    const nameMap = {
      'us_daily_decision': '决策',
      'us_catalyst_scan': '催化',
      'us_weekly_strategy': '周策略',
      'us_auto_review': '复盘',
      'cn_daily_decision': '决策',
      'cn_catalyst_scan': '催化',
      'cn_weekly_strategy': '周策略',
      'cn_auto_review': '复盘',
    };
    const decisionJobs = jobs.filter(j => nameMap[j.id]);
    if (decisionJobs.length === 0) {
      bar.style.display = 'none';
      return;
    }
    const fmtTime = j => j.next_run_at ? new Date(j.next_run_at).toLocaleString('zh-CN', {
      hour: '2-digit', minute: '2-digit', timeZone: 'UTC'
    }) + ' UTC' : '--';

    const usJobs = decisionJobs.filter(j => j.id.startsWith('us_'));
    const cnJobs = decisionJobs.filter(j => j.id.startsWith('cn_'));
    const groupStr = group => group.map(j => `${nameMap[j.id]} ${fmtTime(j)}`).join(' | ');
    const segments = [];
    if (usJobs.length) segments.push('美股: ' + groupStr(usJobs));
    if (cnJobs.length) segments.push('A股: ' + groupStr(cnJobs));
    bar.textContent = '自动调度 | ' + segments.join(' — ');
    bar.style.display = '';
  } catch {
    bar.style.display = 'none';
  }
}

/* ── AI 模型配置 ────────────────────────────────────── */

let aiConfigData = null;

function openAIConfig() {
  document.getElementById('dc-ai-config-modal').style.display = '';
  document.getElementById('dc-ai-status').textContent = '';
  _initAIConfigTabs();
  loadAIConfig();
  loadCustomProviders();
}

function closeAIConfig() {
  document.getElementById('dc-ai-config-modal').style.display = 'none';
}

function _initAIConfigTabs() {
  document.querySelectorAll('.dc-ai-tab').forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll('.dc-ai-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.dc-ai-tab-content').forEach(c => {
        c.style.display = 'none';
        c.classList.remove('active');
      });
      tab.classList.add('active');
      const target = document.getElementById(`dc-ai-tab-${tab.dataset.tab}`);
      if (target) { target.style.display = ''; target.classList.add('active'); }
    };
  });

  document.querySelectorAll('.dc-ai-collapsible').forEach(h3 => {
    h3.onclick = () => {
      const collapsed = h3.dataset.collapsed === 'true';
      const body = h3.nextElementSibling;
      const chevron = h3.querySelector('.dc-ai-chevron');
      if (collapsed) {
        body.style.display = '';
        h3.dataset.collapsed = 'false';
        if (chevron) chevron.textContent = '▾';
      } else {
        body.style.display = 'none';
        h3.dataset.collapsed = 'true';
        if (chevron) chevron.textContent = '▸';
      }
    };
  });
}

const _GROUP_CONTAINERS = {
  decision: 'dc-ai-decision-list',
  committee: 'dc-ai-committee-list',
  pipeline: 'dc-ai-pipeline-list',
  watchlist: 'dc-ai-watchlist-list',
};

async function loadAIConfig() {
  const firstContainer = document.getElementById('dc-ai-decision-list');
  if (!firstContainer) return;
  firstContainer.innerHTML = '<p style="color:var(--muted);font-size:12px">加载中...</p>';

  try {
    const res = await fetch(`${DC_API}/ai-config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    aiConfigData = await res.json();

    const providers = aiConfigData.available_providers || [];

    for (const [group, containerId] of Object.entries(_GROUP_CONTAINERS)) {
      const el = document.getElementById(containerId);
      if (!el) continue;
      const positions = aiConfigData.positions.filter(p => p.group === group);
      el.innerHTML = positions.map(p => renderAIConfigRow(p, providers)).join('');
    }

    bindAIConfigEvents();
  } catch (e) {
    firstContainer.innerHTML = `<p style="color:var(--danger);font-size:12px">加载失败: ${e.message}</p>`;
  }
}

function renderAIConfigRow(pos, providers) {
  const currentProvider = pos.configured_provider || '';
  const currentModel = pos.configured_model || '';
  const isCustom = !!currentProvider;

  const builtIn = providers.filter(p => !p.is_custom && p.configured);
  const custom = providers.filter(p => p.is_custom);

  let providerOptions = '';
  if (builtIn.length) {
    providerOptions += `<optgroup label="内置 Provider">`;
    providerOptions += builtIn.map(p => {
      const sel = (currentProvider === p.id) ? 'selected' : '';
      return `<option value="${escDC(p.id)}" ${sel}>${escDC(p.name)}</option>`;
    }).join('');
    providerOptions += `</optgroup>`;
  }
  if (custom.length) {
    providerOptions += `<optgroup label="自定义端点">`;
    providerOptions += custom.map(p => {
      const sel = (currentProvider === p.id) ? 'selected' : '';
      return `<option value="${escDC(p.id)}" ${sel}>${escDC(p.name)}</option>`;
    }).join('');
    providerOptions += `</optgroup>`;
  }

  const displayModel = currentModel || pos.default_model;
  const placeholder = `默认: ${pos.default_provider}/${pos.default_model}`;

  return `<div class="dc-ai-row" data-key="${escDC(pos.key)}">
    <span class="dc-ai-label">${escDC(pos.label)}</span>
    <select class="dc-ai-select" data-key="${escDC(pos.key)}">
      <option value="">默认</option>
      ${providerOptions}
    </select>
    <input type="text" class="dc-ai-model-input" data-key="${escDC(pos.key)}"
           value="${isCustom ? escDC(displayModel) : ''}"
           placeholder="${escDC(placeholder)}">
    <button class="dc-ai-test-btn" data-key="${escDC(pos.key)}" title="测试连接">测试</button>
    <span class="dc-ai-test-status" data-test-key="${escDC(pos.key)}"></span>
  </div>`;
}

function bindAIConfigEvents() {
  document.querySelectorAll('.dc-ai-select').forEach(sel => {
    sel.addEventListener('change', () => {
      const key = sel.dataset.key;
      const input = document.querySelector(`.dc-ai-model-input[data-key="${key}"]`);
      if (!input) return;

      const provider = sel.value;
      if (!provider) {
        input.value = '';
        return;
      }

      const prov = (aiConfigData?.available_providers || []).find(p => p.id === provider);
      if (prov && prov.default_model) {
        input.value = prov.default_model;
      }
    });
  });

  document.querySelectorAll('.dc-ai-test-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.key;
      const sel = document.querySelector(`.dc-ai-select[data-key="${key}"]`);
      const input = document.querySelector(`.dc-ai-model-input[data-key="${key}"]`);
      const statusEl = document.querySelector(`[data-test-key="${key}"]`);
      if (!sel || !input || !statusEl) return;

      const provider = sel.value;
      const model = input.value.trim();

      if (!provider || !model) {
        statusEl.className = 'dc-ai-test-status';
        statusEl.innerHTML = '';
        statusEl.title = '请先选择 Provider 和 Model';
        return;
      }

      testSingleModel(provider, model, statusEl, btn);
    });
  });
}

async function testSingleModel(provider, model, statusEl, btn) {
  btn.disabled = true;
  btn.textContent = '...';
  statusEl.className = 'dc-ai-test-status test-loading';
  statusEl.innerHTML = '<span class="spinner"></span>';

  try {
    const res = await fetch(`${DC_API}/ai-config/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    });
    const data = await res.json();

    if (data.success) {
      statusEl.className = 'dc-ai-test-status test-pass';
      statusEl.innerHTML = '&#x2714;';
      statusEl.title = '测试通过';
    } else {
      statusEl.className = 'dc-ai-test-status test-fail';
      statusEl.innerHTML = '&#x2718;';
      statusEl.title = data.error || '测试失败';
    }
  } catch (e) {
    statusEl.className = 'dc-ai-test-status test-fail';
    statusEl.innerHTML = '&#x2718;';
    statusEl.title = e.message;
  }

  btn.disabled = false;
  btn.textContent = '测试';
}

async function saveAIConfig() {
  const configs = {};
  document.querySelectorAll('.dc-ai-row').forEach(row => {
    const key = row.dataset.key;
    const sel = row.querySelector('.dc-ai-select');
    const input = row.querySelector('.dc-ai-model-input');
    if (!sel || !input) return;

    const provider = sel.value;
    const model = input.value.trim();

    if (provider && model) {
      configs[key] = `${provider}:${model}`;
    } else {
      configs[key] = '';
    }
  });

  const statusEl = document.getElementById('dc-ai-status');
  try {
    const res = await fetch(`${DC_API}/ai-config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ configs }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);

    if (statusEl) {
      statusEl.textContent = '已保存，即时生效';
      statusEl.className = 'dc-ai-status success';
    }
    setTimeout(() => closeAIConfig(), 1200);
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `保存失败: ${e.message}`;
      statusEl.className = 'dc-ai-status error';
    }
  }
}

/* ── 自定义端点管理 ──────────────────────────────────── */

async function loadCustomProviders() {
  const list = document.getElementById('dc-ai-custom-list');
  if (!list) return;
  list.innerHTML = '<p style="color:var(--muted);font-size:12px">加载中...</p>';

  try {
    const res = await fetch('/api/custom-providers');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const providers = data.providers || [];

    if (!providers.length) {
      list.innerHTML = '<p class="dc-ai-empty-hint">暂无自定义端点</p>';
      return;
    }

    list.innerHTML = providers.map(p => `
      <div class="dc-ai-custom-card" data-pid="${escDC(p.provider_id)}">
        <div class="dc-ai-custom-info">
          <strong>${escDC(p.display_name)}</strong>
          <span class="dc-ai-custom-url">${escDC(p.base_url)}</span>
          <span class="dc-ai-custom-model">默认模型: ${escDC(p.default_model)}</span>
          ${p.api_key_hint ? `<span class="dc-ai-custom-key-hint">KEY: ${escDC(p.api_key_hint)}</span>` : ''}
        </div>
        <div class="dc-ai-custom-actions">
          <button class="btn btn-sm btn-secondary dc-ai-custom-test-btn" data-pid="${escDC(p.provider_id)}">测试</button>
          <button class="btn btn-sm btn-secondary dc-ai-custom-edit-btn" data-pid="${escDC(p.provider_id)}"
                  data-name="${escDC(p.display_name)}" data-url="${escDC(p.base_url)}"
                  data-model="${escDC(p.default_model)}">编辑</button>
          <button class="btn btn-sm btn-danger dc-ai-custom-del-btn" data-pid="${escDC(p.provider_id)}">删除</button>
        </div>
      </div>
    `).join('');

    _bindCustomProviderEvents();
  } catch (e) {
    list.innerHTML = `<p style="color:var(--danger);font-size:12px">加载失败: ${e.message}</p>`;
  }
}

function _bindCustomProviderEvents() {
  document.querySelectorAll('.dc-ai-custom-test-btn').forEach(btn => {
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '...';
      try {
        const res = await fetch(`/api/custom-providers/${btn.dataset.pid}/test`, { method: 'POST' });
        const data = await res.json();
        btn.textContent = data.ok ? '✓' : '✗';
        btn.title = data.ok ? '连接成功' : (data.error || '失败');
      } catch (e) { btn.textContent = '✗'; btn.title = e.message; }
      setTimeout(() => { btn.disabled = false; btn.textContent = '测试'; }, 2000);
    };
  });

  document.querySelectorAll('.dc-ai-custom-edit-btn').forEach(btn => {
    btn.onclick = () => {
      const form = document.getElementById('dc-ai-custom-form');
      document.getElementById('dc-ai-custom-edit-id').value = btn.dataset.pid;
      document.getElementById('dc-ai-custom-id').value = btn.dataset.pid;
      document.getElementById('dc-ai-custom-id').disabled = true;
      document.getElementById('dc-ai-custom-name').value = btn.dataset.name;
      document.getElementById('dc-ai-custom-url').value = btn.dataset.url;
      document.getElementById('dc-ai-custom-key').value = '';
      document.getElementById('dc-ai-custom-model').value = btn.dataset.model;
      form.style.display = '';
    };
  });

  document.querySelectorAll('.dc-ai-custom-del-btn').forEach(btn => {
    btn.onclick = async () => {
      if (!confirm(`确定删除自定义端点 ${btn.dataset.pid}？`)) return;
      try {
        const res = await fetch(`/api/custom-providers/${btn.dataset.pid}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        loadCustomProviders();
        loadAIConfig();
      } catch (e) { alert(`删除失败: ${e.message}`); }
    };
  });
}

function _initCustomProviderForm() {
  const addBtn = document.getElementById('dc-ai-add-custom-btn');
  const cancelBtn = document.getElementById('dc-ai-custom-cancel');
  const saveBtn = document.getElementById('dc-ai-custom-save');
  const form = document.getElementById('dc-ai-custom-form');
  if (!addBtn || !form) return;

  addBtn.onclick = () => {
    document.getElementById('dc-ai-custom-edit-id').value = '';
    document.getElementById('dc-ai-custom-id').value = '';
    document.getElementById('dc-ai-custom-id').disabled = false;
    document.getElementById('dc-ai-custom-name').value = '';
    document.getElementById('dc-ai-custom-url').value = '';
    document.getElementById('dc-ai-custom-key').value = '';
    document.getElementById('dc-ai-custom-model').value = '';
    form.style.display = '';
  };

  cancelBtn.onclick = () => { form.style.display = 'none'; };

  saveBtn.onclick = async () => {
    const editId = document.getElementById('dc-ai-custom-edit-id').value;
    const pid = document.getElementById('dc-ai-custom-id').value.trim();
    const name = document.getElementById('dc-ai-custom-name').value.trim();
    const url = document.getElementById('dc-ai-custom-url').value.trim();
    const key = document.getElementById('dc-ai-custom-key').value;
    const model = document.getElementById('dc-ai-custom-model').value.trim();

    if (!pid || !name || !url || !model) {
      alert('请填写必填字段（名称、标识符、API 地址、默认模型）');
      return;
    }

    saveBtn.disabled = true; saveBtn.textContent = '保存中...';
    try {
      const body = { provider_id: pid, display_name: name, base_url: url, api_key: key, default_model: model };
      const isEdit = !!editId;
      const endpoint = isEdit ? `/api/custom-providers/${editId}` : '/api/custom-providers';
      const method = isEdit ? 'PUT' : 'POST';

      const res = await fetch(endpoint, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }

      form.style.display = 'none';
      loadCustomProviders();
      loadAIConfig();
    } catch (e) {
      alert(`保存失败: ${e.message}`);
    }
    saveBtn.disabled = false; saveBtn.textContent = '保存';
  };
}

_initCustomProviderForm();

/* ── 17F.2 风险仪表盘 ──────────────────────────────────── */

async function loadRiskDashboard() {
  const body = document.getElementById('dc-risk-body');
  if (!body) return;
  try {
    const data = await dcFetch(`/risk-dashboard?market=${encodeURIComponent(dcState.market)}`);
    renderRisk(data);
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">风险数据加载失败: ${escDC(e.message)}</p>`;
  }
}

function renderRisk(data) {
  const body = document.getElementById('dc-risk-body');
  if (!body) return;

  const warnings = data.warnings || [];
  const weights = data.weights || [];

  let html = `<div class="dc-stat-grid dc-risk-metrics">
    <div class="dc-stat">
      <span class="dc-stat-value">${fmtNum(data.var_95, 0)}</span>
      <span class="dc-stat-label">VaR(95%)</span>
    </div>
    <div class="dc-stat">
      <span class="dc-stat-value">${fmtNum(data.cvar_95, 0)}</span>
      <span class="dc-stat-label">CVaR(95%)</span>
    </div>
    <div class="dc-stat">
      <span class="dc-stat-value">${data.portfolio_beta != null ? data.portfolio_beta.toFixed(2) : '--'}</span>
      <span class="dc-stat-label">Beta</span>
    </div>
    <div class="dc-stat">
      <span class="dc-stat-value ${data.concentration_index > 0.25 ? 'dc-pnl-neg' : ''}">${data.concentration_index != null ? data.concentration_index.toFixed(3) : '--'}</span>
      <span class="dc-stat-label">HHI 集中度</span>
    </div>
  </div>`;

  // 持仓饼图容器
  if (weights.length > 0) {
    html += `<div id="dc-risk-pie" style="height:200px;margin-top:12px"></div>`;
  }

  // 预警列表
  if (warnings.length > 0) {
    html += `<div class="dc-risk-warnings" style="margin-top:12px">`;
    for (const w of warnings) {
      html += `<div class="dc-risk-warning-item">${escDC(w)}</div>`;
    }
    html += `</div>`;
  }

  // 相关性对
  const pairs = data.correlation_pairs || [];
  if (pairs.length > 0) {
    html += `<div style="margin-top:10px;font-size:12px;color:var(--muted)">`;
    html += `<strong>高相关持仓对:</strong>`;
    for (const p of pairs) {
      html += ` <span>${escDC(p.ticker_a)}-${escDC(p.ticker_b)} (${p.correlation.toFixed(2)})</span>`;
    }
    html += `</div>`;
  }

  body.innerHTML = html;

  // 渲染饼图
  const pieEl = document.getElementById('dc-risk-pie');
  if (pieEl && typeof echarts !== 'undefined' && weights.length > 0) {
    const chart = dcState.riskChart || echarts.init(pieEl);
    dcState.riskChart = chart;
    chart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c}%' },
      series: [{
        type: 'pie',
        radius: ['35%', '65%'],
        label: { fontSize: 11 },
        data: weights.map(w => ({ name: w.ticker, value: w.weight_pct })),
      }],
    });
  }
}

/* ── 17F.3 催化剂日历视图 ──────────────────────────────── */

async function loadCatalystCalendar(monthStr) {
  const body = document.getElementById('dc-catalysts-body');
  if (!body) return;
  body.innerHTML = '<p class="dc-empty-hint">加载日历...</p>';
  try {
    let qs = monthStr ? `?month=${monthStr}` : '';
    qs += (qs ? '&' : '?') + `market=${encodeURIComponent(dcState.market)}`;
    const data = await dcFetch(`/catalysts/calendar${qs}`);
    renderCatalystCalendar(data);
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">日历加载失败: ${escDC(e.message)}</p>`;
  }
}

function renderCatalystCalendar(data) {
  const body = document.getElementById('dc-catalysts-body');
  if (!body) return;

  const year = data.year;
  const month = data.month;
  const events = data.events || {};

  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;

  // 计算日历网格
  const firstDay = new Date(year, month - 1, 1);
  const lastDay = new Date(year, month, 0);
  const startDow = firstDay.getDay(); // 0=Sun
  const totalDays = lastDay.getDate();

  const monthNames = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];

  // 上月/下月
  const prevMonth = month === 1 ? `${year - 1}-12` : `${year}-${String(month - 1).padStart(2, '0')}`;
  const nextMonth = month === 12 ? `${year + 1}-01` : `${year}-${String(month + 1).padStart(2, '0')}`;

  let html = `<div class="dc-cal-nav">
    <button class="dc-cal-nav-btn" data-month="${prevMonth}">&lt;</button>
    <span class="dc-cal-nav-title">${year}年${monthNames[month - 1]}</span>
    <button class="dc-cal-nav-btn" data-month="${nextMonth}">&gt;</button>
  </div>`;

  html += `<div class="dc-cal-grid">
    <div class="dc-cal-dow">日</div><div class="dc-cal-dow">一</div><div class="dc-cal-dow">二</div>
    <div class="dc-cal-dow">三</div><div class="dc-cal-dow">四</div><div class="dc-cal-dow">五</div>
    <div class="dc-cal-dow">六</div>`;

  // 空格填充
  for (let i = 0; i < startDow; i++) {
    html += `<div class="dc-cal-cell dc-cal-empty"></div>`;
  }

  for (let d = 1; d <= totalDays; d++) {
    const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    const dayEvents = events[dateStr] || [];
    const isToday = dateStr === todayStr;

    // 计算倒数天数
    const dateParsed = new Date(year, month - 1, d);
    const diffDays = Math.ceil((dateParsed - today) / (1000 * 60 * 60 * 24));
    const isUrgent = diffDays >= 0 && diffDays <= 3 && dayEvents.length > 0;

    let cellClass = 'dc-cal-cell';
    if (isToday) cellClass += ' dc-cal-today';
    if (isUrgent) cellClass += ' dc-cal-urgent';

    html += `<div class="${cellClass}">
      <span class="dc-cal-day">${d}</span>`;
    for (const evt of dayEvents.slice(0, 3)) {
      html += `<div class="dc-cal-event" title="${escDC(evt.title)}">${escDC(evt.ticker)}</div>`;
    }
    if (dayEvents.length > 3) {
      html += `<div class="dc-cal-event dc-cal-more">+${dayEvents.length - 3}</div>`;
    }
    html += `</div>`;
  }

  html += `</div>`;
  body.innerHTML = html;

  // 绑定月份切换
  body.querySelectorAll('.dc-cal-nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const m = btn.dataset.month;
      dcState.calendarMonth = m;
      loadCatalystCalendar(m);
    });
  });
}

/* ── 17F.4 A/B 对比面板 ─────────────────────────────────── */

function openComparePanel() {
  let modal = document.getElementById('dc-compare-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'dc-compare-modal';
    modal.className = 'modal-overlay';
    modal.innerHTML = `<div class="modal-content" style="max-width:700px">
      <div class="modal-header">
        <h3>A/B 参数对比</h3>
        <button class="modal-close" id="dc-compare-close">&#10005;</button>
      </div>
      <div class="modal-body" style="padding:16px">
        <div class="dc-compare-actions" style="display:flex;gap:8px;margin-bottom:16px;align-items:center">
          <input type="text" id="dc-compare-label" placeholder="快照名称（可选）" class="dc-ai-model-input" style="max-width:200px">
          <button class="btn btn-sm btn-primary" id="dc-compare-save">保存当前快照</button>
          <span id="dc-compare-status" style="font-size:12px"></span>
        </div>
        <div id="dc-compare-snapshots" style="margin-bottom:16px"></div>
        <div id="dc-compare-result"></div>
      </div>
    </div>`;
    document.body.appendChild(modal);

    modal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closeComparePanel();
    });
    document.getElementById('dc-compare-close').addEventListener('click', closeComparePanel);
    document.getElementById('dc-compare-save').addEventListener('click', saveCompareSnapshot);
  }
  modal.style.display = '';
  loadCompareSnapshots();
}

function closeComparePanel() {
  const modal = document.getElementById('dc-compare-modal');
  if (modal) modal.style.display = 'none';
}

async function saveCompareSnapshot() {
  const labelEl = document.getElementById('dc-compare-label');
  const statusEl = document.getElementById('dc-compare-status');
  const label = labelEl?.value?.trim() || '';
  try {
    statusEl.textContent = '保存中...';
    statusEl.className = '';
    await dcFetch('/compare/snapshot', {
      method: 'POST',
      body: JSON.stringify({ label }),
    });
    statusEl.textContent = '已保存';
    statusEl.style.color = 'oklch(0.72 0.19 145)';
    if (labelEl) labelEl.value = '';
    await loadCompareSnapshots();
  } catch (e) {
    statusEl.textContent = `失败: ${e.message}`;
    statusEl.style.color = 'oklch(0.65 0.22 25)';
  }
}

async function loadCompareSnapshots() {
  const container = document.getElementById('dc-compare-snapshots');
  if (!container) return;
  try {
    const data = await dcFetch('/compare/snapshots');
    const snapshots = data.snapshots || [];
    if (snapshots.length === 0) {
      container.innerHTML = '<p style="color:var(--muted);font-size:12px">暂无快照。保存一个当前配置快照开始使用。</p>';
      return;
    }
    let html = '<div style="font-size:12px;color:var(--muted);margin-bottom:6px">选择两个快照进行对比:</div>';
    html += '<div class="dc-compare-select" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">';
    html += '<select id="dc-compare-a" class="dc-ai-select" style="width:auto;min-width:180px"><option value="">快照 A</option>';
    for (const s of snapshots) {
      const ts = (s.created_at || '').replace('T', ' ').slice(0, 16);
      html += `<option value="${escDC(s.id)}">${escDC(s.label)} (${ts})</option>`;
    }
    html += '</select><span style="color:var(--muted)">vs</span>';
    html += '<select id="dc-compare-b" class="dc-ai-select" style="width:auto;min-width:180px"><option value="">快照 B</option>';
    for (const s of snapshots) {
      const ts = (s.created_at || '').replace('T', ' ').slice(0, 16);
      html += `<option value="${escDC(s.id)}">${escDC(s.label)} (${ts})</option>`;
    }
    html += '</select>';
    html += '<button class="btn btn-sm btn-primary" id="dc-compare-run">对比</button>';
    html += '</div>';
    container.innerHTML = html;

    document.getElementById('dc-compare-run')?.addEventListener('click', runCompare);
  } catch (e) {
    container.innerHTML = `<p style="color:var(--danger);font-size:12px">加载失败: ${escDC(e.message)}</p>`;
  }
}

async function runCompare() {
  const selA = document.getElementById('dc-compare-a');
  const selB = document.getElementById('dc-compare-b');
  const resultEl = document.getElementById('dc-compare-result');
  if (!selA || !selB || !resultEl) return;

  const idA = selA.value;
  const idB = selB.value;
  if (!idA || !idB) {
    resultEl.innerHTML = '<p style="color:var(--danger);font-size:12px">请选择两个快照</p>';
    return;
  }
  if (idA === idB) {
    resultEl.innerHTML = '<p style="color:var(--danger);font-size:12px">请选择不同的快照</p>';
    return;
  }

  resultEl.innerHTML = '<p style="color:var(--muted);font-size:12px">对比中...</p>';

  try {
    const data = await dcFetch(`/compare/${idA}/${idB}`);
    renderCompareResult(data, resultEl);
  } catch (e) {
    resultEl.innerHTML = `<p style="color:var(--danger);font-size:12px">对比失败: ${escDC(e.message)}</p>`;
  }
}

function renderCompareResult(data, container) {
  const diffs = data.diffs || [];
  const a = data.snapshot_a || {};
  const b = data.snapshot_b || {};

  let html = `<div style="font-size:13px;margin-bottom:8px">
    <strong>${escDC(a.label)}</strong> vs <strong>${escDC(b.label)}</strong>
    — 共 ${data.total_params} 个参数，${data.changed_params} 个差异
  </div>`;

  if (diffs.length === 0) {
    html += '<p style="color:var(--muted);font-size:12px">两个快照参数完全一致，无差异。</p>';
  } else {
    html += `<table class="dc-table dc-table-sm"><thead><tr>
      <th>参数</th><th>A: ${escDC(a.label)}</th><th>B: ${escDC(b.label)}</th>
    </tr></thead><tbody>`;
    for (const d of diffs) {
      const valA = d.value_a != null ? String(d.value_a) : '--';
      const valB = d.value_b != null ? String(d.value_b) : '--';
      html += `<tr>
        <td><strong>${escDC(d.parameter)}</strong></td>
        <td>${escDC(valA)}</td>
        <td>${escDC(valB)}</td>
      </tr>`;
    }
    html += '</tbody></table>';
  }

  container.innerHTML = html;
}

/* ── 会议历史 ─────────────────────────────────────── */

async function loadMeetings() {
  const body = document.getElementById('dc-meetings-body');
  if (!body) return;

  const typeFilter = document.getElementById('dc-meeting-type')?.value || '';
  let qs = `?market=${encodeURIComponent(dcState.market)}&limit=10`;
  if (typeFilter) qs += `&type=${encodeURIComponent(typeFilter)}`;

  try {
    const data = await dcFetch(`/meetings${qs}`);
    const meetings = data.meetings || [];
    renderMeetings(meetings);
    // 投委会灯态：最近一次投委会会议新鲜→绿、过期→黄、无→灰（失败态由 SSE 置红，优先保留）
    if (!dcState.lightErrors.has('committee')) {
      const lastCommittee = meetings.find(m => m.meeting_type === 'committee');
      if (lastCommittee) {
        setLight('committee', _lightFreshness('committee', lastCommittee.created_at));
      } else {
        setLight('committee', 'gray');
      }
    }
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">加载失败: ${escDC(e.message)}</p>`;
  }
}

function renderMeetings(meetings) {
  const body = document.getElementById('dc-meetings-body');
  if (!body) return;

  if (!meetings || meetings.length === 0) {
    body.innerHTML = '<p class="dc-empty-hint">暂无会议记录</p>';
    return;
  }

  body.innerHTML = meetings.map(m => {
    const typeIcon = m.meeting_type === 'roundtable' ? '\u{1F535}' : '\u{1F7E2}';
    const typeLabel = m.meeting_type === 'roundtable' ? '圆桌' : '投委会';
    const date = (m.created_at || '').replace('T', ' ').slice(0, 16);
    const verdict = m.final_verdict || '';

    let verdictCls = '';
    if (verdict.includes('approved') || verdict.includes('pass')) verdictCls = 'dc-verdict-pass';
    else if (verdict.includes('rejected') || verdict.includes('fail')) verdictCls = 'dc-verdict-fail';

    let tickers = [];
    try {
      tickers = typeof m.tickers_discussed === 'string' ? JSON.parse(m.tickers_discussed) : (m.tickers_discussed || []);
    } catch { tickers = []; }

    let participants = [];
    try {
      participants = typeof m.participants === 'string' ? JSON.parse(m.participants) : (m.participants || []);
    } catch { participants = []; }

    return `<div class="dc-meeting-item" data-meeting-id="${escDC(m.id)}">
      <div class="dc-meeting-row">
        <span class="dc-meeting-icon">${typeIcon}</span>
        <div class="dc-meeting-info">
          <div class="dc-meeting-title">${escDC(m.title || `${typeLabel}会议`)}</div>
          <div class="dc-meeting-meta">
            <span class="dc-meeting-date">${escDC(date)}</span>
            <span class="dc-meeting-participants">${participants.length}位参会</span>
            ${tickers.length ? `<span class="dc-meeting-tickers">${tickers.map(t => escDC(t)).join(', ')}</span>` : ''}
          </div>
        </div>
        <span class="dc-meeting-verdict ${verdictCls}">${escDC(verdict)}</span>
      </div>
    </div>`;
  }).join('');

  body.querySelectorAll('.dc-meeting-item').forEach(el => {
    el.addEventListener('click', () => {
      const id = el.dataset.meetingId;
      if (id) openMeetingDrawer(id);
    });
  });
}

/* ── 会议详情抽屉 ─────────────────────────────────── */

async function openMeetingDrawer(recordId) {
  const drawer = document.getElementById('dc-meeting-drawer');
  const title = document.getElementById('dc-meeting-drawer-title');
  const body = document.getElementById('dc-meeting-drawer-body');
  if (!drawer || !body) return;

  drawer.style.display = '';
  if (title) title.textContent = '加载中...';
  body.innerHTML = '<p class="dc-empty-hint">加载会议详情...</p>';

  try {
    const resp = await dcFetch(`/meetings/${encodeURIComponent(recordId)}`);
    const data = resp.meeting || resp;  // 后端返回 {meeting: {...}}
    if (title) title.textContent = data.title || '会议详情';
    renderMeetingDetail(data, body);
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">加载失败: ${escDC(e.message)}</p>`;
  }
}

function closeMeetingDrawer() {
  const drawer = document.getElementById('dc-meeting-drawer');
  if (drawer) drawer.style.display = 'none';
}

function renderMeetingDetail(data, container) {
  let participants = data.participants || [];
  if (typeof participants === 'string') {
    try { participants = JSON.parse(participants); } catch { participants = []; }
  }

  let tickers = data.tickers_discussed || [];
  if (typeof tickers === 'string') {
    try { tickers = JSON.parse(tickers); } catch { tickers = []; }
  }

  let agreements = data.key_agreements || [];
  if (typeof agreements === 'string') {
    try { agreements = JSON.parse(agreements); } catch { agreements = []; }
  }

  let disagreements = data.key_disagreements || [];
  if (typeof disagreements === 'string') {
    try { disagreements = JSON.parse(disagreements); } catch { disagreements = []; }
  }

  let risks = data.risk_warnings || [];
  if (typeof risks === 'string') {
    try { risks = JSON.parse(risks); } catch { risks = []; }
  }

  let result = data.result_json || {};
  if (typeof result === 'string') {
    try { result = JSON.parse(result); } catch { result = {}; }
  }

  const typeLabel = data.meeting_type === 'roundtable' ? '圆桌会议' : '投委会审议';
  const date = (data.created_at || '').replace('T', ' ').slice(0, 16);

  let html = '';

  // 概要信息
  html += `<div class="drawer-section">
    <h4>概要</h4>
    <div class="dc-mtg-summary">
      <div class="dc-mtg-kv"><span>类型</span><span>${escDC(typeLabel)}</span></div>
      <div class="dc-mtg-kv"><span>时间</span><span>${escDC(date)}</span></div>
      <div class="dc-mtg-kv"><span>结论</span><span class="dc-mtg-verdict">${escDC(data.final_verdict || '--')}</span></div>
      ${tickers.length ? `<div class="dc-mtg-kv"><span>讨论标的</span><span>${tickers.map(t => escDC(t)).join(', ')}</span></div>` : ''}
      ${data.duration_seconds ? `<div class="dc-mtg-kv"><span>耗时</span><span>${Math.round(data.duration_seconds / 60)}分钟</span></div>` : ''}
    </div>
  </div>`;

  // 参会者
  if (participants.length > 0) {
    html += `<div class="drawer-section">
      <h4>参会者</h4>
      <div class="dc-mtg-participants">
        ${participants.map(p => `<div class="dc-mtg-participant">
          <span class="dc-mtg-p-role">${escDC(p.role || p.name || '--')}</span>
          <span class="dc-mtg-p-model">${escDC(p.model || '')}</span>
        </div>`).join('')}
      </div>
    </div>`;
  }

  // 共识与分歧
  if (agreements.length > 0 || disagreements.length > 0) {
    html += `<div class="drawer-section"><h4>共识与分歧</h4>`;
    if (agreements.length > 0) {
      html += `<div class="dc-mtg-list dc-mtg-agree">
        <div class="dc-mtg-list-title">共识</div>
        ${agreements.map(a => `<div class="dc-mtg-list-item">${escDC(typeof a === 'string' ? a : JSON.stringify(a))}</div>`).join('')}
      </div>`;
    }
    if (disagreements.length > 0) {
      html += `<div class="dc-mtg-list dc-mtg-disagree">
        <div class="dc-mtg-list-title">分歧</div>
        ${disagreements.map(d => `<div class="dc-mtg-list-item">${escDC(typeof d === 'string' ? d : JSON.stringify(d))}</div>`).join('')}
      </div>`;
    }
    html += `</div>`;
  }

  // 风险警示
  if (risks.length > 0) {
    html += `<div class="drawer-section">
      <h4>风险警示</h4>
      <div class="dc-mtg-list dc-mtg-risks">
        ${risks.map(r => `<div class="dc-mtg-list-item dc-mtg-risk-item">${escDC(typeof r === 'string' ? r : JSON.stringify(r))}</div>`).join('')}
      </div>
    </div>`;
  }

  // 排名结果（圆桌）
  let ranking = data.final_ranking || result.final_ranking || [];
  if (typeof ranking === 'string') {
    try { ranking = JSON.parse(ranking); } catch { ranking = []; }
  }
  if (Array.isArray(ranking) && ranking.length > 0) {
    html += `<div class="drawer-section">
      <h4>最终排名</h4>
      <table class="dc-table dc-table-sm">
        <thead><tr><th>#</th><th>Ticker</th><th>加权分</th><th>理由</th></tr></thead>
        <tbody>${ranking.map((r, i) => `<tr>
          <td>${r.rank || i + 1}</td>
          <td><strong>${escDC(r.ticker || '')}</strong></td>
          <td>${r.weighted_score || r.score || r.borda_points || '--'}</td>
          <td style="font-size:11px;color:var(--muted)">${escDC((r.reason || r.reasoning || '').slice(0, 80))}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>`;
  }

  // 投票详情（投委会）
  const voteDetail = result.vote_detail || {};
  if (Object.keys(voteDetail).length > 0) {
    html += `<div class="drawer-section">
      <h4>投票详情</h4>
      <div class="dc-votes-grid">${Object.entries(voteDetail).map(([role, info]) => {
        const vote = typeof info === 'object' ? (info.vote || '--') : String(info);
        const conf = typeof info === 'object' ? (info.confidence || '--') : '--';
        const vCls = vote.includes('approve') ? 'approve' : vote.includes('reject') ? 'reject' : 'conditional';
        return `<div class="dc-member-vote">
          <div class="dc-member-name">${escDC(role)}</div>
          <div class="dc-member-decision ${vCls}">${escDC(vote)}</div>
          <div class="dc-member-reasoning">信心: ${conf}/10</div>
        </div>`;
      }).join('')}</div>
    </div>`;
  }

  // 讨论过程（transcript：背景资料 + 各委员评审 + 圆桌讨论）
  let transcript = data.transcript_json || [];
  if (typeof transcript === 'string') {
    try { transcript = JSON.parse(transcript); } catch { transcript = []; }
  }
  if (Array.isArray(transcript) && transcript.length > 0) {
    html += renderCommitteeTranscript(transcript);
  }

  // 回溯结果
  if (data.outcome_recorded && data.outcome_summary) {
    html += `<div class="drawer-section">
      <h4>回溯结论</h4>
      <p style="font-size:13px;line-height:1.6">${escDC(data.outcome_summary)}</p>
    </div>`;
  }

  container.innerHTML = html;
}

/* ── 投委会讨论过程渲染（阶段 1.4）─────────────────── */

function _voteCls(vote) {
  const v = String(vote || '').toLowerCase();
  if (v.includes('approve') || v.includes('通过')) return 'approve';
  if (v.includes('reject') || v.includes('否决')) return 'reject';
  return 'conditional';
}

function _voteLabel(vote) {
  const v = String(vote || '').toLowerCase();
  if (v === 'approve') return '赞成';
  if (v === 'approve_with_modification') return '有条件赞成';
  if (v === 'reject') return '反对';
  if (v === 'abstain') return '弃权';
  return vote || '--';
}

function _fmtBgVal(v) {
  if (v == null) return '<span class="dc-muted">暂无</span>';
  if (typeof v === 'string') return escDC(v);
  if (Array.isArray(v)) {
    if (!v.length) return '<span class="dc-muted">暂无</span>';
    return v.map(x => typeof x === 'object'
      ? escDC(JSON.stringify(x, null, 0)) : escDC(String(x))).join('<br>');
  }
  if (typeof v === 'object') {
    return Object.entries(v).map(([k, val]) =>
      `<span class="dc-bg-k">${escDC(k)}</span>: ${escDC(typeof val === 'object' ? JSON.stringify(val) : String(val))}`
    ).join('<br>');
  }
  return escDC(String(v));
}

function renderCommitteeTranscript(transcript) {
  const bg = transcript.find(t => t.role === '_background');
  const members = transcript.filter(t => t.round === 1);
  const discussion = transcript.find(t => t.role === '_discussion');

  let html = '<div class="drawer-section"><h4>讨论过程</h4>';

  // 各委员独立评审（完整理由，不截断）
  html += '<div class="dc-transcript">';
  members.forEach(m => {
    const concerns = Array.isArray(m.key_concerns) ? m.key_concerns : [];
    const sugg = Array.isArray(m.suggestions) ? m.suggestions : [];
    html += `<div class="dc-tr-turn">
      <div class="dc-tr-head">
        <span class="dc-tr-name">${escDC(m.name || m.role)}</span>
        <span class="dc-member-decision ${_voteCls(m.vote)}">${escDC(_voteLabel(m.vote))}</span>
        <span class="dc-tr-conf">信心 ${m.confidence != null ? m.confidence : '--'}/10</span>
        <span class="dc-tr-model">${escDC(m.model || '')}</span>
      </div>
      ${m.content ? `<div class="dc-tr-content">${escDC(m.content)}</div>` : ''}
      ${concerns.length ? `<div class="dc-tr-sub"><b>关注点：</b>${concerns.map(c => escDC(typeof c === 'string' ? c : JSON.stringify(c))).join('；')}</div>` : ''}
      ${sugg.length ? `<div class="dc-tr-sub"><b>建议：</b>${sugg.map(s => escDC(typeof s === 'object' ? `${s.field || ''} ${s.original ?? ''}→${s.suggested ?? ''} (${s.reason || ''})` : String(s))).join('；')}</div>` : ''}
    </div>`;
  });
  html += '</div>';

  // 圆桌讨论（如有）
  if (discussion && (discussion.content || discussion.key_disagreement)) {
    html += `<div class="dc-tr-discussion">
      <div class="dc-tr-disc-title">🗣 圆桌讨论</div>
      ${discussion.content ? `<div class="dc-tr-content">${escDC(discussion.content)}</div>` : ''}
      ${discussion.key_agreement ? `<div class="dc-tr-sub"><b>共识：</b>${escDC(discussion.key_agreement)}</div>` : ''}
      ${discussion.key_disagreement ? `<div class="dc-tr-sub"><b>分歧：</b>${escDC(discussion.key_disagreement)}</div>` : ''}
    </div>`;
  }

  html += '</div>';

  // 会议输入资料（背景数据，可折叠）
  if (bg && bg.data) {
    const fields = [
      ['估值数据', bg.data.valuation_data],
      ['市场情绪', bg.data.sentiment_data],
      ['持仓拥挤度', bg.data.crowding_data],
      ['同业对比', bg.data.peer_comparison],
      ['催化剂', bg.data.catalyst_data],
      ['行业趋势', bg.data.sector_trends],
    ];
    html += `<div class="drawer-section"><details class="dc-bg-details">
      <summary>会议输入资料（各委员读入的背景数据）</summary>
      <div class="dc-bg-grid">
        ${fields.map(([label, val]) => `<div class="dc-bg-row">
          <div class="dc-bg-label">${label}</div>
          <div class="dc-bg-val">${_fmtBgVal(val)}</div>
        </div>`).join('')}
      </div>
    </details></div>`;
  }

  return html;
}

async function loadModelRatings() {
  const body = document.getElementById('dc-model-ratings-body');
  if (!body) return;

  try {
    const data = await dcFetch(`/model-accuracy?market=${encodeURIComponent(dcState.market)}`);
    renderModelRatings(data, body);
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">加载失败: ${escDC(e.message)}</p>`;
  }
}

function renderModelRatings(data, container) {
  const ratings = data.ratings || [];
  const stats = data.stats || [];

  if (ratings.length === 0 && stats.length === 0) {
    container.innerHTML = '<p class="dc-empty-hint">暂无模型评分数据</p>';
    return;
  }

  let html = '';

  if (ratings.length > 0) {
    html += `<div class="dc-model-list">`;
    const sorted = [...ratings].sort((a, b) => (b.calibration_weight || 1) - (a.calibration_weight || 1));
    html += sorted.map((r, i) => {
      const w = r.calibration_weight || 1.0;
      const accuracy = r.accuracy_rate != null ? (r.accuracy_rate * 100).toFixed(0) : '--';
      const barW = Math.min(100, Math.round(w / 3 * 100));
      const barCls = w >= 1.5 ? 'dc-bar-good' : w >= 0.8 ? 'dc-bar-mid' : 'dc-bar-low';

      return `<div class="dc-model-row">
        <span class="dc-model-rank">#${i + 1}</span>
        <div class="dc-model-info">
          <div class="dc-model-name">${escDC(r.model_provider)}/${escDC(r.model_name)}</div>
          <div class="dc-model-meta">
            ${r.role_context ? `<span class="dc-model-role">${escDC(r.role_context)}</span>` : ''}
            <span>准确率 ${accuracy}%</span>
            <span>样本 ${r.total_predictions || 0}</span>
          </div>
        </div>
        <div class="dc-model-weight">
          <div class="dc-model-bar"><div class="dc-model-bar-fill ${barCls}" style="width:${barW}%"></div></div>
          <span class="dc-model-w-val">${w.toFixed(2)}</span>
        </div>
      </div>`;
    }).join('');
    html += `</div>`;
  } else if (stats.length > 0) {
    html += `<div class="dc-model-list">`;
    html += stats.map(s => {
      const total = s.total || 0;
      const correct = s.correct || 0;
      const accuracy = total > 0 ? ((correct / total) * 100).toFixed(0) : '--';

      return `<div class="dc-model-row">
        <div class="dc-model-info">
          <div class="dc-model-name">${escDC(s.model_provider)}/${escDC(s.model_name)}</div>
          <div class="dc-model-meta">
            ${s.role_context ? `<span class="dc-model-role">${escDC(s.role_context)}</span>` : ''}
            <span>准确率 ${accuracy}%</span>
            <span>${correct}/${total}</span>
          </div>
        </div>
      </div>`;
    }).join('');
    html += `</div>`;
  }

  container.innerHTML = html;
}

async function runCalibration() {
  const btn = document.getElementById('dc-btn-calibrate');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = '校准中...';

  try {
    const data = await dcFetch('/model-accuracy/calibrate', { method: 'POST', body: JSON.stringify({ market: dcState.market }) });
    btn.textContent = `已校准 ${data.calibrated || 0} 个`;
    await loadModelRatings();
  } catch (e) {
    btn.textContent = '失败';
  }

  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = '校准';
  }, 2000);
}
