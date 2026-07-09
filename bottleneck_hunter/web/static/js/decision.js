/**
 * decision.js — 决策中心前端模块
 * L1 宏观 / L2 组合 / L3 战术 / L4 执行 / 投委会 / 模拟账户
 */
import { showConfirm } from './utils/confirm.js';
import { openReport, buildMeetingReport, buildDecisionReport } from './report-export.js';

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

/* ── L1 宏观咨询抽屉 ─────────────────────────────── */
const CONSULT_ROLES = { macro_market: '🌐 宏观市场分析师', industry_trend: '🏭 产业动向分析师' };
const CONSULT_ROUND = { 0: '开场', 1: '', 2: '· 辩论' };
const dcConsult = { market: null, streaming: false, bubbles: {} };

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
            if (data.event === 'model_fallback' || data.kind === 'model_fallback') { window.notifyFallback?.(data.message); continue; }
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

/* ── 统一投票/结论 译名与样式 ────────────────────────
 * 后端 vote/verdict 取值不统一：approve / approved / approve_with_modification
 * / approved_with_modifications / reject / rejected / conditional / abstain，
 * 还可能已是中文。这里统一用子串匹配，所有渲染器复用，避免 4 处各写一份导致
 * 同一票在列表/抽屉/概览显示不同文字和颜色。 */
const ROLE_LABELS = {
  risk_officer: '风险控制官',
  growth_investor: '成长投资人',
  value_investor: '价值投资人',
  contrarian: '逆向投资人',
  consensus_builder: '共识构建者',
};
function roleLabel(role) { return ROLE_LABELS[role] || role; }

// CSS 颜色类：approve(绿)/reject(红)/conditional(黄)。有条件（modification）优先，避免误染绿。
function voteClass(v) {
  const s = String(v || '').toLowerCase();
  if (s.includes('modification') || s.includes('conditional') || s.includes('有条件')) return 'conditional';
  if (s.includes('approve') || s.includes('pass') || s.includes('通过') || s.includes('赞成')) return 'approve';
  if (s.includes('reject') || s.includes('fail') || s.includes('否决') || s.includes('反对')) return 'reject';
  return 'conditional';
}

// 个人委员票：赞成 / 反对 / 有条件赞成 / 弃权
function voteLabel(v) {
  const s = String(v || '').toLowerCase();
  if (!s || s === '--') return v || '--';
  if (s.includes('modification') || s.includes('conditional')) return '有条件赞成';
  if (s.includes('approve') || s.includes('赞成')) return '赞成';
  if (s.includes('reject') || s.includes('反对')) return '反对';
  if (s.includes('abstain') || s.includes('弃权')) return '弃权';
  return v || '--';
}

// 会议集体结论：通过 / 否决 / 有条件通过
function verdictLabel(v) {
  const s = String(v || '').toLowerCase();
  if (!s || s === '--') return v || '--';
  if (s.includes('modification') || s.includes('conditional') || s.includes('有条件')) return '有条件通过';
  if (s.includes('approve') || s.includes('pass') || s.includes('通过')) return '通过';
  if (s.includes('reject') || s.includes('fail') || s.includes('否决')) return '否决';
  if (s.includes('abstain') || s.includes('弃权')) return '弃权';
  return v || '--';
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
    const vCls = voteClass(meta.verdict || '');
    const date = (meta.created_at || '').replace('T', ' ').slice(0, 16);
    header = `<div class="dc-committee-meta">
      <span class="dc-committee-ticker">${escDC(meta.ticker || '')}</span>
      <span class="dc-member-decision ${vCls}">${escDC(verdictLabel(meta.verdict || '--'))}</span>
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
    const decClass = voteClass(decision);
    const reasoning = rj.overall_assessment || rj.reasoning || rj.summary || '';
    const conf = rj.confidence != null ? rj.confidence : null;
    const concerns = Array.isArray(rj.key_concerns) ? rj.key_concerns : [];
    // 因 LLM 调用失败而弃权：明确标注，区分于真实弃权
    const errAbstain = (decision === 'abstain' && rj.error) ? rj.error : '';

    return `<div class="dc-member-vote">
      <div class="dc-member-name">${escDC(r.member_name || r.reviewer || r.member_role || '--')}${conf != null && !errAbstain ? ` <span class="dc-tr-conf">信心 ${conf}/10</span>` : ''}</div>
      <div class="dc-member-decision ${errAbstain ? 'conditional' : decClass}">${errAbstain ? '弃权（系统错误）' : escDC(voteLabel(decision))}</div>
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
  document.getElementById('dc-btn-export')?.addEventListener('click', exportDecisionReport);

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
      closeConsultDrawer();   // 抽屉绑定打开时的市场，切换市场即关闭
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

  document.getElementById('dc-pending-list')?.addEventListener('click', handlePendingAction);
  document.getElementById('dc-blocked-section')?.addEventListener('click', handleBlockedAction);

  // 会议日期筛选
  document.getElementById('dc-meeting-date')?.addEventListener('change', () => loadMeetings());

  // 模型校准按钮
  document.getElementById('dc-btn-calibrate')?.addEventListener('click', runCalibration);

  // 会议详情抽屉
  const meetingDrawer = document.getElementById('dc-meeting-drawer');
  document.getElementById('dc-meeting-drawer-close')?.addEventListener('click', closeMeetingDrawer);
  document.getElementById('dc-meeting-export')?.addEventListener('click', exportMeetingReport);
  meetingDrawer?.addEventListener('click', (e) => {
    if (e.target === meetingDrawer) closeMeetingDrawer();
  });

  // L1 宏观咨询抽屉
  document.getElementById('dc-macro-consult-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();   // 不触发 L1 卡片折叠
    openConsultDrawer();
  });
  document.getElementById('dc-consult-close')?.addEventListener('click', closeConsultDrawer);
  const consultDrawer = document.getElementById('dc-consult-drawer');
  consultDrawer?.addEventListener('click', (e) => { if (e.target === consultDrawer) closeConsultDrawer(); });
  document.getElementById('dc-consult-send')?.addEventListener('click', sendConsult);
  document.getElementById('dc-consult-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendConsult(); }
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

/* ── 会议历史 ─────────────────────────────────────── */

async function loadMeetings() {
  const body = document.getElementById('dc-meetings-body');
  if (!body) return;

  const dateFilter = document.getElementById('dc-meeting-date')?.value || '';
  let qs = `?market=${encodeURIComponent(dcState.market)}&limit=50`;
  if (dateFilter) qs += `&date_filter=${encodeURIComponent(dateFilter)}`;

  try {
    const data = await dcFetch(`/meetings${qs}`);
    let meetings = data.meetings || [];

    // 前端日期筛选（如果后端不支持）
    if (dateFilter && meetings.length > 0) {
      const now = new Date();
      const filterDate = dateFilter === 'today'
        ? new Date(now.getFullYear(), now.getMonth(), now.getDate())
        : dateFilter === 'week'
        ? new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
        : dateFilter === 'month'
        ? new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000)
        : null;

      if (filterDate) {
        meetings = meetings.filter(m => {
          const meetingDate = new Date(m.created_at);
          return meetingDate >= filterDate;
        });
      }
    }

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

    // 翻译结论（统一复用 verdictLabel / voteClass）
    const verdictZh = verdictLabel(verdict);

    let verdictCls = '';
    const vc = verdict ? voteClass(verdict) : '';
    if (vc === 'approve') verdictCls = 'dc-verdict-pass';
    else if (vc === 'reject') verdictCls = 'dc-verdict-fail';

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
        <span class="dc-meeting-verdict ${verdictCls}">${escDC(verdictZh)}</span>
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

  dcState.currentMeetingId = recordId;
  drawer.style.display = '';
  if (title) title.textContent = '加载中...';
  body.innerHTML = '<p class="dc-empty-hint">加载会议详情...</p>';

  try {
    const resp = await dcFetch(`/meetings/${encodeURIComponent(recordId)}`);
    const data = resp.meeting || resp;  // 后端返回 {meeting: {...}}
    dcState.currentMeeting = data;
    if (title) title.textContent = data.title || '会议详情';
    renderMeetingDetail(data, body);
    _bindMeetingChallenge(body);
  } catch (e) {
    body.innerHTML = `<p class="dc-empty-hint">加载失败: ${escDC(e.message)}</p>`;
  }
}

/* ── 用户质询投委会成员 ───────────────────────────── */
function _bindMeetingChallenge(container) {
  if (!container) return;
  if (container._challengeHandler) container.removeEventListener('click', container._challengeHandler);
  container._challengeHandler = async (e) => {
    const toggle = e.target.closest('.dc-challenge-btn');
    if (toggle) {
      const role = toggle.dataset.role;
      const box = container.querySelector(`.dc-challenge-box[data-role-box="${role}"]`);
      if (box) box.style.display = box.style.display === 'none' ? '' : 'none';
      return;
    }
    const send = e.target.closest('.dc-challenge-send');
    if (send) {
      const role = send.dataset.role;
      const box = container.querySelector(`.dc-challenge-box[data-role-box="${role}"]`);
      const ta = box && box.querySelector('textarea');
      const status = box && box.querySelector('.dc-challenge-status');
      const msg = (ta && ta.value || '').trim();
      if (!msg) { if (status) status.textContent = '请输入质询内容'; return; }
      send.disabled = true;
      if (status) status.textContent = '委员思考中…';
      try {
        const res = await dcFetch('/committee/challenge', {
          method: 'POST',
          body: JSON.stringify({
            meeting_id: dcState.currentMeetingId, role, message: msg,
            market: dcState.market,
          }),
        });
        if (status) {
          status.textContent = res.vote_changed
            ? `✓ 委员改票（${res.old_vote}→${res.new_vote}），共识已更新为「${res.verdict}」`
            : '✓ 委员已回应（维持原票）';
        }
        // 重载抽屉 + 概览，反映更新后的 transcript / 共识 / gating
        await openMeetingDrawer(dcState.currentMeetingId);
        if (typeof loadOverview === 'function') await loadOverview().catch(() => {});
      } catch (err) {
        if (status) status.textContent = '质询失败: ' + err.message;
        send.disabled = false;
      }
    }
  };
  container.addEventListener('click', container._challengeHandler);
}

function closeMeetingDrawer() {
  const drawer = document.getElementById('dc-meeting-drawer');
  if (drawer) drawer.style.display = 'none';
}

/* ── L1 宏观咨询互动 ──────────────────────────────── */

function marketLabel(m) { return m === 'a_stock' ? 'A股' : m === 'hk_stock' ? '港股' : '美股'; }

// 无进度条副作用、无重试的 SSE 读取（对会创建消息的流更安全，避免断连重发重复生成）
async function consultStream(path, body, { onEvent, onDone, onError } = {}) {
  try {
    const res = await fetch(`${DC_API}${path}`, {
      method: 'POST',
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
          try { const data = JSON.parse(line.slice(6)); if (data.event === 'model_fallback' || data.kind === 'model_fallback') { window.notifyFallback?.(data.message); } else if (onEvent) onEvent(data); } catch {}
        }
      }
    }
    if (onDone) onDone();
  } catch (e) {
    if (onError) onError(e); else console.error('consult SSE error:', e);
  }
}

function setConsultSending(on) {
  dcConsult.streaming = on;
  const btn = document.getElementById('dc-consult-send');
  const ta = document.getElementById('dc-consult-input');
  if (btn) { btn.disabled = on; btn.textContent = on ? '思考中…' : '发送'; }
  if (ta) ta.disabled = on;
}

function _modelLabel(provider, model) {
  if (!provider && !model) return '';
  return provider && model ? `${provider}/${model}` : (model || provider);
}

function _consultBubbleEl(m) {
  const div = document.createElement('div');
  if (m.type === 'user') {
    div.className = 'dc-bubble dc-bubble-user';
    div.textContent = m.content || '';
  } else if (m.type === 'summary') {
    div.className = 'dc-bubble dc-bubble-summary';
    div.innerHTML = '<div class="dc-bubble-role">📋 历史摘要</div>';
    const c = document.createElement('div'); c.textContent = m.content || ''; div.appendChild(c);
  } else if (m.type === 'system') {
    div.className = 'dc-bubble dc-bubble-system';
    div.textContent = m.content || '';
  } else { // analyst
    div.className = `dc-bubble dc-bubble-analyst dc-bubble-${m.role}`;
    const name = CONSULT_ROLES[m.role] || m.name || m.role || '';
    const rmark = CONSULT_ROUND[m.round] || '';
    const head = document.createElement('div');
    head.className = 'dc-bubble-role';
    head.innerHTML = `${escDC(name)}<span class="dc-bubble-round">${escDC(rmark)}</span>`
      + `<span class="dc-bubble-model">${escDC(_modelLabel(m.provider, m.model))}</span>`;
    const c = document.createElement('div'); c.textContent = m.content || '';
    div.appendChild(head); div.appendChild(c);
  }
  return div;
}

// 时效分割抬头：自动更新跨天时插入，标注日期/时间 + 即时核心市场数据，便于识别历史信息时间
function _consultDividerEl(snap) {
  const div = document.createElement('div');
  div.className = 'dc-consult-divider';
  const d = new Date(Date.parse(snap.ts || '') || Date.now());
  const pad = (n) => String(n).padStart(2, '0');
  const when = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  // 即时核心市场数据：从 indices/sentiment/macro 精简取几项（名称+值+涨跌）
  const pick = [];
  const take = (obj, n) => Object.entries(obj || {})
    .filter(([k]) => k !== 'watchlist_breadth')
    .slice(0, n)
    .forEach(([k, v]) => {
      if (!v || typeof v !== 'object') return;
      const label = v.label || k;
      const val = v.value != null ? v.value : '';
      const chg = v.change_pct != null ? `${v.change_pct > 0 ? '+' : ''}${v.change_pct}%` : '';
      pick.push(`${escDC(label)} ${escDC(String(val))}${chg ? ` (${escDC(chg)})` : ''}`);
    });
  take(snap.indices, 2);
  take(snap.sentiment, 1);
  take(snap.macro, 2);
  const st = snap.strategy || {};
  const regime = st.regime ? ` · L1:${escDC(st.regime)}` : '';
  const dataLine = (pick.length || regime)
    ? `<div class="dc-divider-data">${pick.join(' · ')}${regime}</div>` : '';
  div.innerHTML = `<div class="dc-divider-time">${'&lt;'.repeat(7)} 🕒 ${escDC(when)} ${'&gt;'.repeat(8)}</div>${dataLine}`;
  return div;
}

function _createStreamBubble(role, round) {
  const log = document.getElementById('dc-consult-log');
  const div = _consultBubbleEl({ type: 'analyst', role, round, content: '' });
  log.appendChild(div);
  div._content = div.querySelector('div:last-child');
  div._modelEl = div.querySelector('.dc-bubble-model');  // msg_done 时回填具体模型
  log.scrollTop = log.scrollHeight;
  return div;
}

function appendConsultBubble(m) {
  const log = document.getElementById('dc-consult-log');
  if (!log) return;
  const now = Date.now();
  // 实时发送时若距上一条交互 >1 天，先插日期分割线
  if (m.type === 'user' && dcConsult.lastMsgTs && (now - dcConsult.lastMsgTs) > 864e5) {
    log.appendChild(_consultDividerEl({ ts: new Date().toISOString() }));
  }
  log.appendChild(_consultBubbleEl(m));
  if (m.type === 'user' || m.type === 'analyst') dcConsult.lastMsgTs = now;
  log.scrollTop = log.scrollHeight;
}

function renderConsultSnapshot(snap) {
  const el = document.getElementById('dc-consult-snapshot');
  if (!el || !snap) return;
  const fmtInd = (obj) => Object.entries(obj || {})
    .filter(([k]) => k !== 'watchlist_breadth')
    .map(([k, v]) => {
      const label = (v && v.label) || k;
      const val = v && v.value != null ? v.value : '';
      const chg = v && v.change_pct != null ? ` (${v.change_pct > 0 ? '+' : ''}${v.change_pct}%)` : '';
      return `${escDC(label)} ${escDC(String(val))}${escDC(chg)}`;
    }).join(' · ');
  const rows = [];
  const push = (label, val) => { if (val) rows.push(`<div class="dc-snap-row"><span class="dc-snap-label">${label}</span>${val}</div>`); };
  push('大盘', fmtInd(snap.indices));
  push('情绪', fmtInd(snap.sentiment));
  push('宏观', fmtInd(snap.macro));
  const sectors = Object.entries(snap.sectors || {})
    .map(([k, v]) => `${escDC(k)} ${v && v.avg_change != null ? (v.avg_change > 0 ? '+' : '') + v.avg_change + '%' : ''}`)
    .join(' · ');
  push('板块', sectors);
  const st = snap.strategy || {};
  if (st.regime || st.market_summary) {
    push('当前L1策略', `${escDC(st.regime || '')} / ${escDC(st.risk_appetite || '')} — ${escDC(st.market_summary || '')}`);
  }
  const pos = snap.positions;
  if (Array.isArray(pos)) {
    push('持仓', pos.length
      ? pos.map(p => `${escDC(p.ticker)}${p.pnl_pct != null ? ` <span style="color:${p.pnl_pct >= 0 ? 'var(--up,#16a34a)' : 'var(--down,#dc2626)'}">${p.pnl_pct > 0 ? '+' : ''}${p.pnl_pct}%</span>` : ''}`).join(' · ')
      : '空仓');
  }
  // 观察池数据已按需去除；新闻置于最后，展开显示中文摘要
  const news = snap.news || [];
  let newsHtml = '';
  if (news.length) {
    const items = news.map(n => {
      const title = escDC(n.title || '');
      const summary = escDC(n.summary || n.topic || '');   // topic=llm_analysis（中文分析/摘要）
      const meta = [n.date, n.source_name].filter(Boolean).map(escDC).join(' · ');
      return '<div class="dc-snap-news-item">'
        + (title ? `<div class="dc-snap-news-title">${title}</div>` : '')
        + (summary ? `<div class="dc-snap-news-summary">${summary}</div>` : '')
        + (meta ? `<div class="dc-snap-news-meta">${meta}</div>` : '')
        + '</div>';
    }).join('');
    newsHtml = '<details class="dc-snap-news" open>'
      + `<summary><span class="dc-snap-label">新闻</span> ${news.length} 条（点击展开/收起摘要）</summary>`
      + items + '</details>';
  }
  el.innerHTML = (rows.join('') + newsHtml) || '<div class="dc-snap-row">（暂无数据快照）</div>';
}

function renderConsultLog(transcript) {
  const log = document.getElementById('dc-consult-log');
  if (!log) return;
  log.innerHTML = '';
  const cutoff = Date.now() - 14 * 864e5;
  const DAY = 864e5;
  // 时间线纳入 snapshot（用于跨天分割）+ 对话消息
  const items = transcript.filter(m => ['user', 'analyst', 'summary', 'system', 'snapshot'].includes(m.type));
  const folded = [], live = [];
  for (const m of items) {
    const ts = Date.parse(m.ts || '') || 0;
    if ((m.type === 'user' || m.type === 'analyst') && ts && ts < cutoff) folded.push(m);
    else live.push(m);
  }
  // 渲染一段时间线：snapshot 仅在与上一渲染项跨天(>1天)时作为“时效分割抬头”出现，否则跳过（顶部快照区已展示当前）
  const renderSeq = (container, seq) => {
    let prevTs = 0;
    for (const m of seq) {
      const ts = Date.parse(m.ts || '') || 0;
      const crossed = prevTs && ts && (ts - prevTs) > DAY;
      if (m.type === 'snapshot') {
        if (crossed) container.appendChild(_consultDividerEl(m));
        if (ts) prevTs = ts;
        continue;
      }
      // 任意两条交互消息间隔 >1 天：插入日期/时间分割线
      if (crossed) container.appendChild(_consultDividerEl({ ts: m.ts }));
      container.appendChild(_consultBubbleEl(m));
      if (ts) prevTs = ts;
    }
    if (prevTs) dcConsult.lastMsgTs = prevTs;  // 供实时发送判定是否跨天
  };
  if (folded.length) {
    const det = document.createElement('details');
    const sum = document.createElement('summary');
    sum.textContent = `展开 ${folded.length} 条两周前的历史消息`;
    det.appendChild(sum);
    folded.forEach(m => det.appendChild(_consultBubbleEl(m)));
    log.appendChild(det);
  }
  renderSeq(log, live);
  log.scrollTop = log.scrollHeight;
}

function handleConsultEvent(data) {
  const evt = data.event || '';
  if (evt === 'snapshot') {
    renderConsultSnapshot(data);
    // 实时自动更新：与上次快照跨天(>1天) → 在日志插入时效分割抬头，便于识别历史时间
    const prev = dcConsult.lastSnapTs ? (Date.parse(dcConsult.lastSnapTs) || 0) : 0;
    const cur = Date.parse(data.ts || '') || 0;
    if (prev && cur && (cur - prev) > 864e5) {
      const log = document.getElementById('dc-consult-log');
      if (log) { log.appendChild(_consultDividerEl(data)); log.scrollTop = log.scrollHeight; }
    }
    if (data.ts) dcConsult.lastSnapTs = data.ts;
    return;
  }
  if (evt === 'system') { appendConsultBubble({ type: 'system', content: data.content }); return; }
  if (evt === 'error') { appendConsultBubble({ type: 'system', content: '⚠ ' + (data.message || '出错') }); return; }
  if (evt === 'chunk') {
    const key = `${data.role}-${data.round}`;
    let el = dcConsult.bubbles[key];
    if (!el) { el = _createStreamBubble(data.role, data.round); dcConsult.bubbles[key] = el; }
    el._content.textContent += data.text || '';
    const log = document.getElementById('dc-consult-log');
    if (log) log.scrollTop = log.scrollHeight;
    return;
  }
  if (evt === 'msg_done') {
    const el = dcConsult.bubbles[`${data.role}-${data.round}`];
    if (el && el._modelEl) el._modelEl.textContent = _modelLabel(data.provider, data.model);
    return;
  }
  // start / done：流式渲染无需额外处理
}

function _todayHasOpening(transcript) {
  const today = new Date().toISOString().slice(0, 10);
  const snaps = transcript.filter(m => m.type === 'snapshot' && (m.ts || '').slice(0, 10) === today);
  if (!snaps.length) return false;
  const lastSnapTs = snaps[snaps.length - 1].ts || '';
  return transcript.some(m => m.type === 'analyst' && m.round === 0 && (m.ts || '') >= lastSnapTs);
}

async function openConsultDrawer() {
  const drawer = document.getElementById('dc-consult-drawer');
  if (!drawer) return;
  dcConsult.market = dcState.market;
  dcConsult.bubbles = {};
  dcConsult.lastSnapTs = '';
  dcConsult.lastMsgTs = 0;
  drawer.style.display = '';
  const mkLabel = document.getElementById('dc-consult-market');
  if (mkLabel) mkLabel.textContent = '· ' + marketLabel(dcConsult.market);
  const log = document.getElementById('dc-consult-log');
  const snapEl = document.getElementById('dc-consult-snapshot');
  if (log) log.innerHTML = '';
  if (snapEl) snapEl.innerHTML = '<div class="dc-snap-row">加载中…</div>';

  let transcript = null;
  let newsStale = false;
  try {
    const resp = await dcFetch(`/macro/consult/history?market=${encodeURIComponent(dcConsult.market)}`);
    const session = resp.session;
    newsStale = !!resp.stale;
    if (session && Array.isArray(session.transcript_json) && session.transcript_json.length) {
      transcript = session.transcript_json;
      const snaps = transcript.filter(m => m.type === 'snapshot');
      if (snaps.length) { renderConsultSnapshot(snaps[snaps.length - 1]); dcConsult.lastSnapTs = snaps[snaps.length - 1].ts || ''; }
      renderConsultLog(transcript);
    }
  } catch (e) { /* 无历史，继续走 open 生成 */ }

  // 当日已有开场且无更新新闻 → 历史已展示，不再调用 open（省重复烧钱）；
  // 有更新新闻（如全量刷新/定时扫描后）→ 重开生成最新快照。
  if (transcript && _todayHasOpening(transcript) && !newsStale) return;
  // 将要重新生成 → 清掉刚回显的历史，避免与新流重复
  if (log) log.innerHTML = '';
  if (snapEl) snapEl.innerHTML = '<div class="dc-snap-row">加载中…</div>';
  dcConsult.bubbles = {};
  setConsultSending(true);
  await consultStream('/macro/consult/open', { market: dcConsult.market }, {
    onEvent: handleConsultEvent,
    onDone: () => setConsultSending(false),
    onError: (e) => { appendConsultBubble({ type: 'system', content: '⚠ 打开失败: ' + e.message }); setConsultSending(false); },
  });
}

function closeConsultDrawer() {
  const d = document.getElementById('dc-consult-drawer');
  if (d) d.style.display = 'none';
}

async function sendConsult() {
  const ta = document.getElementById('dc-consult-input');
  if (!ta) return;
  const q = (ta.value || '').trim();
  if (!q || dcConsult.streaming) return;
  appendConsultBubble({ type: 'user', content: q });
  ta.value = '';
  dcConsult.bubbles = {};   // 每轮重置流式气泡映射
  setConsultSending(true);
  await consultStream('/macro/consult/ask', { market: dcConsult.market, question: q }, {
    onEvent: handleConsultEvent,
    onDone: () => setConsultSending(false),
    onError: (e) => { appendConsultBubble({ type: 'system', content: '⚠ ' + e.message }); setConsultSending(false); },
  });
}

/* ── 导出报告（HTML / PDF）───────────────────────── */
function exportDecisionReport() {
  const data = dcState.overview;
  if (!data) { alert('暂无决策数据，请先加载决策中心'); return; }
  const mkt = dcState.market === 'a_stock' ? 'A股' : dcState.market === 'hk_stock' ? '港股' : '美股';
  const date = new Date().toISOString().slice(0, 10);
  openReport('决策中心总结报告', `决策总结_${mkt}_${date}.html`, buildDecisionReport(data, dcState.market));
}

function exportMeetingReport() {
  const m = dcState.currentMeeting;
  if (!m) { alert('暂无会议数据'); return; }
  const type = m.meeting_type === 'committee' ? '投委会纪要' : '圆桌会议纪要';
  const tk = (Array.isArray(m.tickers_discussed) ? m.tickers_discussed : []).join('-');
  const fname = `${type}${tk ? '_' + tk : ''}_${(m.created_at || '').slice(0, 10)}.html`;
  openReport(m.title || type, fname, buildMeetingReport(m));
}

function renderMeetingDetail(data, container) {
  // 角色/投票译名统一复用模块级 roleLabel / voteLabel / voteClass
  const translateRole = roleLabel;
  const translateVote = voteLabel;

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
        ${agreements.map(a => {
          if (typeof a === 'string') return `<div class="dc-mtg-list-item">${escDC(a)}</div>`;
          // 对象格式：提取关键字段
          const text = a.opinion || a.point || a.content || JSON.stringify(a);
          return `<div class="dc-mtg-list-item">${escDC(text)}</div>`;
        }).join('')}
      </div>`;
    }
    if (disagreements.length > 0) {
      html += `<div class="dc-mtg-list dc-mtg-disagree">
        <div class="dc-mtg-list-title">分歧</div>
        ${disagreements.map(d => {
          if (typeof d === 'string') return `<div class="dc-mtg-list-item">${escDC(d)}</div>`;
          // 对象格式：member + opinion + recommendation
          const member = d.member ? `<strong>${escDC(translateRole(d.member))}:</strong> ` : '';
          const opinion = escDC(d.opinion || d.point || '');
          const rec = d.recommendation ? `<div style="margin-top:4px;color:var(--muted);font-size:12px">💡 ${escDC(d.recommendation)}</div>` : '';
          return `<div class="dc-mtg-list-item">${member}${opinion}${rec}</div>`;
        }).join('')}
      </div>`;
    }
    html += `</div>`;
  }

  // 风险警示
  if (risks.length > 0) {
    html += `<div class="drawer-section">
      <h4>风险警示</h4>
      <div class="dc-mtg-list dc-mtg-risks">
        ${risks.map(r => {
          if (typeof r === 'string') return `<div class="dc-mtg-list-item dc-mtg-risk-item">${escDC(r)}</div>`;
          // 对象格式：提取关键字段
          const text = r.warning || r.risk || r.content || r.description || JSON.stringify(r);
          return `<div class="dc-mtg-list-item dc-mtg-risk-item">${escDC(text)}</div>`;
        }).join('')}
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
        const conf = typeof info === 'object' ? (info.confidence || 0) : 0;
        const vCls = voteClass(vote);

        // 信心指数仪表盘（0-10）
        const confPercent = Math.min(100, (conf / 10) * 100);
        const confColor = conf >= 8 ? '#22c55e' : conf >= 6 ? '#eab308' : '#ef4444';
        const gaugeHtml = `
          <div class="dc-confidence-gauge">
            <svg width="60" height="35" viewBox="0 0 60 35">
              <path d="M 5,30 A 25,25 0 0,1 55,30" fill="none" stroke="#e5e7eb" stroke-width="6" stroke-linecap="round"/>
              <path d="M 5,30 A 25,25 0 0,1 55,30" fill="none" stroke="${confColor}" stroke-width="6" stroke-linecap="round"
                    stroke-dasharray="${confPercent * 0.785} 100" style="transition: stroke-dasharray 0.3s ease"/>
              <text x="30" y="28" text-anchor="middle" font-size="11" font-weight="bold" fill="${confColor}">${conf}/10</text>
            </svg>
          </div>`;

        return `<div class="dc-member-vote">
          <div class="dc-member-name">${escDC(translateRole(role))}</div>
          <div class="dc-member-decision ${vCls}">${escDC(translateVote(vote))}</div>
          ${gaugeHtml}
        </div>`;
      }).join('')}</div>
    </div>`;
  }

  // 共识修改（投委会）
  const consensusMods = result.consensus_modifications || [];
  if (Array.isArray(consensusMods) && consensusMods.length > 0) {
    html += `<div class="drawer-section">
      <h4>共识修改</h4>
      <div class="dc-consensus-mods">
        ${consensusMods.map(mod => {
          const supporters = Array.isArray(mod.supporters) ? mod.supporters : [];
          const supportersZh = supporters.map(s => translateRole(s)).join('、');
          return `<div class="dc-consensus-mod-item">
            <div class="dc-mod-header">
              <span class="dc-mod-ticker">${escDC(mod.ticker || '')}</span>
              <span class="dc-mod-field">${escDC(mod.field || '')}</span>
            </div>
            <div class="dc-mod-change">
              <span class="dc-mod-original">${escDC(String(mod.original || ''))}</span>
              <span class="dc-mod-arrow">→</span>
              <span class="dc-mod-modified">${escDC(String(mod.modified || ''))}</span>
            </div>
            <div class="dc-mod-reason">
              <strong>支持者：</strong>${escDC(supportersZh || '—')}<br>
              ${escDC(mod.reason || '')}
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }

  // 会议总结
  const summary = result.summary || '';
  if (summary) {
    html += `<div class="drawer-section">
      <h4>会议总结</h4>
      <p class="dc-meeting-summary">${escDC(summary)}</p>
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
  return voteClass(vote);
}

function _voteLabel(vote) {
  return voteLabel(vote);
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
  const isMember = (t) => !String(t.role || '').startsWith('_');
  const members = transcript.filter(t => t.round === 1 && isMember(t));
  // 第 2 轮辩论后改票（仅有立场/理由变化的委员才落库）
  const revisions = transcript.filter(t => t.round === 2 && isMember(t));
  const discussion = transcript.find(t => t.role === '_discussion');

  let html = '<div class="drawer-section"><h4>讨论过程</h4>';

  // 用户质询记录（按委员分组）
  const challengesByRole = {};
  transcript.filter(t => t.type === 'challenge').forEach(t => {
    (challengesByRole[t.role] = challengesByRole[t.role] || []).push(t);
  });

  // 各委员独立评审（完整理由，不截断）+ 历史权重 + 用户质询入口
  html += '<div class="dc-transcript">';
  members.forEach(m => {
    const concerns = Array.isArray(m.key_concerns) ? m.key_concerns : [];
    const sugg = Array.isArray(m.suggestions) ? m.suggestions : [];
    const wBadge = (m.weight != null && Math.abs(Number(m.weight) - 1) > 0.001)
      ? `<span class="dc-tr-weight" title="历史可信权重">权重 ${Number(m.weight).toFixed(2)}x</span>` : '';
    const myCh = challengesByRole[m.role] || [];
    const chHtml = myCh.map(c => `
      <div class="dc-challenge-item">
        <div class="dc-challenge-q"><b>🙋 质询：</b>${escDC(c.user_message)}</div>
        <div class="dc-challenge-a"><b>${escDC(m.name || m.role)}：</b>${escDC(c.response)}
          ${c.vote_changed
            ? `<span class="dc-vote-change"><span class="dc-member-decision ${_voteCls(c.old_vote)}" style="opacity:.6">${escDC(_voteLabel(c.old_vote))}</span><span class="dc-mod-arrow">→</span><span class="dc-member-decision ${_voteCls(c.new_vote)}">${escDC(_voteLabel(c.new_vote))}</span></span>`
            : '<span class="dc-vote-keep">维持原票</span>'}
        </div>
      </div>`).join('');
    html += `<div class="dc-tr-turn">
      <div class="dc-tr-head">
        <span class="dc-tr-name">${escDC(m.name || m.role)}</span>
        <span class="dc-member-decision ${_voteCls(m.vote)}">${escDC(_voteLabel(m.vote))}</span>
        <span class="dc-tr-conf">信心 ${m.confidence != null ? m.confidence : '--'}/10</span>
        ${wBadge}
        <span class="dc-tr-model">${escDC(m.model || '')}</span>
        ${m.role ? `<button class="dc-challenge-btn" data-role="${escDC(m.role)}">质询</button>` : ''}
      </div>
      ${m.content ? `<div class="dc-tr-content">${escDC(m.content)}</div>` : ''}
      ${concerns.length ? `<div class="dc-tr-sub"><b>关注点：</b>${concerns.map(c => escDC(typeof c === 'string' ? c : JSON.stringify(c))).join('；')}</div>` : ''}
      ${sugg.length ? `<div class="dc-tr-sub"><b>建议：</b>${sugg.map(s => escDC(typeof s === 'object' ? `${s.field || ''} ${s.original ?? ''}→${s.suggested ?? ''} (${s.reason || ''})` : String(s))).join('；')}</div>` : ''}
      ${chHtml ? `<div class="dc-challenge-history">${chHtml}</div>` : ''}
      ${m.role ? `<div class="dc-challenge-box" data-role-box="${escDC(m.role)}" style="display:none">
        <textarea class="dc-challenge-input" rows="2" placeholder="向${escDC(m.name || m.role)}提出你的异议或论据，若被说服TA可改票…"></textarea>
        <div class="dc-challenge-actions"><button class="btn btn-sm dc-challenge-send" data-role="${escDC(m.role)}">提交质询</button><span class="dc-challenge-status"></span></div>
      </div>` : ''}
    </div>`;
  });
  html += '</div>';

  // 第 2 轮辩论后改票（首轮立场 → 终票）
  if (revisions.length) {
    html += '<div class="dc-tr-revisions"><div class="dc-tr-disc-title">🔁 辩论后改票</div>';
    revisions.forEach(m => {
      const concerns = Array.isArray(m.key_concerns) ? m.key_concerns : [];
      html += `<div class="dc-tr-turn">
        <div class="dc-tr-head">
          <span class="dc-tr-name">${escDC(m.name || m.role)}</span>
          ${m.prev_vote ? `<span class="dc-member-decision ${_voteCls(m.prev_vote)}" style="opacity:.6">${escDC(_voteLabel(m.prev_vote))}</span><span class="dc-mod-arrow">→</span>` : ''}
          <span class="dc-member-decision ${_voteCls(m.vote)}">${escDC(_voteLabel(m.vote))}</span>
          <span class="dc-tr-conf">信心 ${m.confidence != null ? m.confidence : '--'}/10</span>
          <span class="dc-tr-model">${escDC(m.model || '')}</span>
        </div>
        ${m.content ? `<div class="dc-tr-content">${escDC(m.content)}</div>` : ''}
        ${concerns.length ? `<div class="dc-tr-sub"><b>关注点：</b>${concerns.map(c => escDC(typeof c === 'string' ? c : JSON.stringify(c))).join('；')}</div>` : ''}
      </div>`;
    });
    html += '</div>';
  }

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
