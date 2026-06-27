/**
 * decision.js — 决策中心前端模块
 * L1 宏观 / L2 组合 / L3 战术 / L4 执行 / 投委会 / 模拟账户
 */

const DC_API = '/api/decision';

const dcState = {
  overview: null,
  loading: false,
  chartAlloc: null,
  chartEquity: null,
  catalystView: 'list',
  calendarMonth: null,
  riskChart: null,
};

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
  setProgress(0, text);
}

function hideProgress() {
  const el = document.getElementById('dc-progress');
  if (el) el.style.display = 'none';
}

function setProgress(pct, text) {
  const fill = document.getElementById('dc-progress-fill');
  const txt = document.getElementById('dc-progress-text');
  if (fill) fill.style.width = `${Math.min(100, pct)}%`;
  if (txt) txt.textContent = text || '';
}

/* ── 数据加载 ─────────────────────────────────────── */

async function loadOverview() {
  if (dcState.loading) return;
  dcState.loading = true;
  try {
    const data = await dcFetch('/overview');
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
  renderAccount(data.account, data.positions || []);
  renderCatalysts(data.upcoming_catalysts || []);
  loadRiskDashboard();
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
  if (!plan) {
    if (empty) empty.style.display = '';
    if (chartEl) chartEl.innerHTML = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  let rj = plan.result_json;
  if (typeof rj === 'string') {
    try { rj = JSON.parse(rj); } catch { rj = {}; }
  }
  rj = rj || {};

  const alloc = rj.target_allocation || [];
  if (chartEl && typeof echarts !== 'undefined' && alloc.length > 0) {
    const chart = dcState.chartAlloc || echarts.init(chartEl);
    dcState.chartAlloc = chart;
    const pieData = alloc.map(a => ({
      name: a.ticker || a.name,
      value: Math.round((a.weight || 0) * 100),
    }));
    const cashWeight = 100 - pieData.reduce((s, d) => s + d.value, 0);
    if (cashWeight > 0) pieData.push({ name: '现金', value: cashWeight });

    chart.setOption({
      tooltip: { trigger: 'item', formatter: '{b}: {c}%' },
      series: [{
        type: 'pie',
        radius: ['40%', '70%'],
        label: { color: 'var(--ink)', fontSize: 11 },
        data: pieData,
      }],
    });
  }

  const parent = chartEl?.parentElement;
  if (parent && alloc.length > 0) {
    let existing = parent.querySelector('.dc-alloc-list');
    if (!existing) {
      existing = document.createElement('ul');
      existing.className = 'dc-alloc-list';
      parent.appendChild(existing);
    }
    existing.innerHTML = alloc.map(a =>
      `<li><span>${escDC(a.ticker)} <small>${escDC(a.action || '')}</small></span><span>${Math.round((a.weight || 0) * 100)}%</span></li>`
    ).join('');
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

    return `<div class="dc-pending-item" data-plan-id="${escDC(ex.id)}">
      <div class="dc-pending-header">
        <span class="dc-pending-ticker">${escDC(ex.ticker)} ${actionBadge(action)}</span>
        <span style="font-size:12px;color:var(--muted)">${shares}股 @ ${price !== '--' ? fmtNum(Number(price), 2) : '--'}</span>
      </div>
      ${reasoning ? `<div class="dc-pending-detail">${escDC(reasoning)}</div>` : ''}
      <div class="dc-pending-actions">
        <button class="dc-btn-confirm" data-action="confirm" data-plan-id="${escDC(ex.id)}">确认执行</button>
        <button class="dc-btn-reject" data-action="reject" data-plan-id="${escDC(ex.id)}">拒绝</button>
      </div>
    </div>`;
  }).join('');
}

/* ── 确认 / 拒绝 ──────────────────────────────────── */

async function handlePendingAction(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const planId = btn.dataset.planId;
  const action = btn.dataset.action;

  btn.disabled = true;
  btn.textContent = action === 'confirm' ? '执行中...' : '处理中...';

  try {
    if (action === 'confirm') {
      const res = await dcFetch(`/executions/${planId}/confirm`, { method: 'POST' });
      const trade = res.trade || {};
      const msg = trade.error
        ? `执行失败: ${trade.error}`
        : `${trade.side === 'buy' ? '买入' : '卖出'} ${trade.ticker} ${trade.shares}股 @ ${fmtNum(trade.price, 2)}`;
      alert(msg);
    } else {
      const reason = prompt('拒绝原因（可选）:') || '';
      await dcFetch(`/executions/${planId}/reject`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      });
    }
    await loadOverview();
  } catch (e) {
    alert('操作失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = action === 'confirm' ? '确认执行' : '拒绝';
  }
}

/* ── 模拟账户 ──────────────────────────────────────── */

function renderAccount(account, positions) {
  if (!account) return;
  const el = (id) => document.getElementById(id);

  el('dc-equity') && (el('dc-equity').textContent = fmtNum(account.total_equity, 0));
  el('dc-cash') && (el('dc-cash').textContent = fmtNum(account.cash_balance, 0));
  const retEl = el('dc-return');
  if (retEl) {
    const ret = account.total_return_pct || 0;
    retEl.textContent = (ret >= 0 ? '+' : '') + fmtNum(ret, 2) + '%';
    retEl.className = `dc-stat-value ${pnlClass(ret)}`;
  }
  el('dc-winrate') && (el('dc-winrate').textContent = fmtNum(account.win_rate || 0, 1) + '%');

  renderPositions(positions);
  loadEquityChart();
}

function renderPositions(positions) {
  const tbody = document.getElementById('dc-positions-body');
  const empty = document.getElementById('dc-positions-empty');
  if (!tbody) return;

  if (!positions || positions.length === 0) {
    tbody.innerHTML = '';
    if (empty) empty.style.display = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  tbody.innerHTML = positions.map(p => {
    const pnl = p.unrealized_pnl || 0;
    const weight = p.weight_pct || 0;
    return `<tr>
      <td><strong>${escDC(p.ticker)}</strong></td>
      <td>${fmtNum(p.shares, 0)}</td>
      <td>${fmtNum(p.avg_cost, 2)}</td>
      <td class="${pnlClass(pnl)}">${pnl >= 0 ? '+' : ''}${fmtNum(pnl, 0)}</td>
      <td>${fmtNum(weight, 1)}%</td>
    </tr>`;
  }).join('');
}

async function loadEquityChart() {
  const chartEl = document.getElementById('dc-equity-chart');
  if (!chartEl || typeof echarts === 'undefined') return;

  try {
    const data = await dcFetch('/account/equity-history');
    const history = data.history || [];
    if (history.length === 0) {
      chartEl.innerHTML = '<p style="text-align:center;color:var(--muted);font-size:12px;padding-top:40px">暂无交易记录</p>';
      return;
    }

    const chart = dcState.chartEquity || echarts.init(chartEl);
    dcState.chartEquity = chart;

    chart.setOption({
      grid: { top: 10, right: 10, bottom: 24, left: 50 },
      xAxis: {
        type: 'category',
        data: history.map(h => h.date.slice(5)),
        axisLabel: { fontSize: 10, color: '#888' },
        axisLine: { lineStyle: { color: '#333' } },
      },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: '#888' },
        splitLine: { lineStyle: { color: '#222' } },
      },
      series: [{
        type: 'line',
        data: history.map(h => h.equity),
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 2 },
        areaStyle: { opacity: 0.1 },
      }],
      tooltip: {
        trigger: 'axis',
        formatter: p => `${p[0].name}<br/>权益: ${fmtNum(p[0].value, 0)}`,
      },
    });
  } catch (e) {
    console.error('Failed to load equity chart:', e);
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

function renderCommittee(reviews) {
  const body = document.getElementById('dc-committee-body');
  if (!body) return;

  if (!reviews || reviews.length === 0) {
    body.innerHTML = '<p class="dc-empty-hint">暂无评审记录</p>';
    return;
  }

  body.innerHTML = `<div class="dc-votes-grid">${reviews.map(r => {
    let rj = r.result_json;
    if (typeof rj === 'string') {
      try { rj = JSON.parse(rj); } catch { rj = {}; }
    }
    rj = rj || {};

    const decision = rj.decision || rj.vote || '--';
    const decClass = decision.includes('approve') || decision.includes('通过') ? 'approve'
      : decision.includes('reject') || decision.includes('否决') ? 'reject' : 'conditional';
    const reasoning = rj.reasoning || rj.summary || '';

    return `<div class="dc-member-vote">
      <div class="dc-member-name">${escDC(r.member_name || r.reviewer || '--')}</div>
      <div class="dc-member-decision ${decClass}">${escDC(decision)}</div>
      <div class="dc-member-reasoning">${escDC(String(reasoning).slice(0, 80))}</div>
    </div>`;
  }).join('')}</div>`;
}

/* ── 操作按钮 ──────────────────────────────────────── */

async function runDaily() {
  if (dcState.loading) return;
  dcState.loading = true;
  let step = 0;
  const totalSteps = 5;

  await dcSSE('/daily', {
    body: { scope: 'full' },
    onEvent(data) {
      const evt = data.event || '';
      const msg = data.message || data.error || evt;
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
    onError(e) {
      setProgress(0, `错误: ${e.message}`);
      dcState.loading = false;
    },
  });
  dcState.loading = false;
}

async function runFullRefresh() {
  if (dcState.loading) return;
  if (!confirm('全量刷新将重新运行 L1→L2→L3→L4→投委会全流程，确认继续？')) return;
  dcState.loading = true;
  let step = 0;

  await dcSSE('/full-refresh', {
    onEvent(data) {
      const evt = data.event || '';
      const msg = data.message || data.error || evt;
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
    onError(e) {
      setProgress(0, `错误: ${e.message}`);
      dcState.loading = false;
    },
  });
  dcState.loading = false;
}

async function scanCatalysts() {
  if (dcState.loading) return;
  dcState.loading = true;

  await dcSSE('/catalysts/scan', {
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
    onError(e) {
      setProgress(0, `扫描失败: ${e.message}`);
      dcState.loading = false;
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
  document.getElementById('dc-btn-review')?.addEventListener('click', openReviewPanel);
  document.getElementById('dc-btn-performance')?.addEventListener('click', openPerformance);
  document.getElementById('dc-btn-compare')?.addEventListener('click', openComparePanel);

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

  document.getElementById('dc-review-close')?.addEventListener('click', closeReviewPanel);
  document.getElementById('dc-review-modal-close')?.addEventListener('click', closeReviewPanel);
  document.getElementById('dc-btn-batch-review')?.addEventListener('click', runBatchReview);

  document.getElementById('dc-perf-close')?.addEventListener('click', closePerformance);
  document.getElementById('dc-perf-modal-close')?.addEventListener('click', closePerformance);
  document.getElementById('dc-btn-gen-tuning')?.addEventListener('click', generateTuning);

  const reviewModal = document.getElementById('dc-review-modal');
  if (reviewModal) {
    reviewModal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closeReviewPanel();
    });
  }

  document.getElementById('dc-ai-config-close')?.addEventListener('click', closeAIConfig);
  document.getElementById('dc-ai-config-cancel')?.addEventListener('click', closeAIConfig);
  document.getElementById('dc-ai-config-save')?.addEventListener('click', saveAIConfig);

  const aiModal = document.getElementById('dc-ai-config-modal');
  if (aiModal) {
    aiModal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closeAIConfig();
    });
  }

  const perfModal = document.getElementById('dc-perf-modal');
  if (perfModal) {
    perfModal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closePerformance();
    });
  }

  document.getElementById('dc-pending-list')?.addEventListener('click', handlePendingAction);

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
    perfState.equityChart?.resize();
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
  loadAIConfig();
}

function closeAIConfig() {
  document.getElementById('dc-ai-config-modal').style.display = 'none';
}

async function loadAIConfig() {
  const decList = document.getElementById('dc-ai-decision-list');
  const comList = document.getElementById('dc-ai-committee-list');
  if (!decList || !comList) return;

  decList.innerHTML = '<p style="color:var(--muted);font-size:12px">加载中...</p>';
  comList.innerHTML = '';

  try {
    const res = await fetch(`${DC_API}/ai-config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    aiConfigData = await res.json();

    const decision = aiConfigData.positions.filter(p => p.group === 'decision');
    const committee = aiConfigData.positions.filter(p => p.group === 'committee');
    const providers = aiConfigData.available_providers || [];

    decList.innerHTML = decision.map(p => renderAIConfigRow(p, providers)).join('');
    comList.innerHTML = committee.map(p => renderAIConfigRow(p, providers)).join('');

    bindAIConfigEvents();
  } catch (e) {
    decList.innerHTML = `<p style="color:var(--danger);font-size:12px">加载失败: ${e.message}</p>`;
  }
}

function renderAIConfigRow(pos, providers) {
  const currentProvider = pos.configured_provider || '';
  const currentModel = pos.configured_model || '';
  const isCustom = !!currentProvider;

  const providerOptions = providers
    .filter(p => p.configured)
    .map(p => {
      const sel = (currentProvider === p.id) ? 'selected' : '';
      return `<option value="${escDC(p.id)}" ${sel}>${escDC(p.name)}</option>`;
    }).join('');

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

/* ── 复盘面板 ────────────────────────────────────────── */

function openReviewPanel() {
  document.getElementById('dc-review-modal').style.display = '';
  document.getElementById('dc-review-status').textContent = '';
  loadReviewData();
}

function closeReviewPanel() {
  document.getElementById('dc-review-modal').style.display = 'none';
}

async function loadReviewData() {
  await Promise.all([loadReviews(), loadExperienceCards(), loadFeedbackHistory()]);
}

async function loadReviews() {
  const el = document.getElementById('dc-review-list');
  if (!el) return;
  try {
    const data = await dcFetch('/reviews?limit=20');
    const reviews = data.reviews || [];
    if (reviews.length === 0) {
      el.innerHTML = '<p class="text-muted">暂无复盘记录。执行卖出交易后可使用"批量复盘"功能。</p>';
      return;
    }
    el.innerHTML = reviews.map(r => {
      const rj = r.result_json || {};
      const right = (rj.what_went_right || []).map(s => `<li>${escDC(s)}</li>`).join('');
      const wrong = (rj.what_went_wrong || []).map(s => `<li>${escDC(s)}</li>`).join('');
      const lessons = (rj.key_lessons || []).map(s => `<li>${escDC(s)}</li>`).join('');
      const score = rj.trade_quality_score || '--';
      const pnlCls = r.return_pct > 0 ? 'dc-pnl-pos' : r.return_pct < 0 ? 'dc-pnl-neg' : 'dc-pnl-zero';
      return `
        <div class="dc-review-item">
          <div class="dc-review-header">
            <strong>${escDC(r.ticker)}</strong>
            <span class="${pnlCls}">${r.return_pct > 0 ? '+' : ''}${(r.return_pct || 0).toFixed(1)}%</span>
            <span class="dc-review-score">质量 ${score}/10</span>
            <span class="text-muted">${(r.created_at || '').slice(0, 10)}</span>
          </div>
          <div class="dc-review-body">
            ${right ? `<div class="dc-review-section"><strong>正确判断</strong><ul>${right}</ul></div>` : ''}
            ${wrong ? `<div class="dc-review-section"><strong>不足之处</strong><ul>${wrong}</ul></div>` : ''}
            ${lessons ? `<div class="dc-review-section"><strong>经验教训</strong><ul>${lessons}</ul></div>` : ''}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<p class="text-muted">加载失败: ${escDC(e.message)}</p>`;
  }
}

async function loadExperienceCards() {
  const el = document.getElementById('dc-experience-list');
  if (!el) return;
  try {
    const data = await dcFetch('/experience?limit=20');
    const cards = data.cards || [];
    if (cards.length === 0) {
      el.innerHTML = '<p class="text-muted">暂无经验卡片。完成交易复盘后会自动生成。</p>';
      return;
    }
    el.innerHTML = cards.map(c => {
      const scopeLabel = { global: '全局', sector: '行业', ticker: '个股' }[c.scope] || c.scope;
      const catLabel = { pattern: '模式', lesson: '教训', rule: '规则' }[c.category] || c.category;
      return `
        <div class="dc-exp-card">
          <div class="dc-exp-header">
            <span class="dc-exp-badge">${escDC(scopeLabel)}</span>
            ${c.scope_key ? `<span class="dc-exp-scope-key">${escDC(c.scope_key)}</span>` : ''}
            <span class="dc-exp-badge dc-exp-cat">${escDC(catLabel)}</span>
            <span class="dc-exp-confidence">置信度 ${((c.confidence || 0) * 100).toFixed(0)}%</span>
            <span class="text-muted">应用 ${c.applied_count || 0} 次</span>
            <button class="dc-exp-delete" data-card-id="${escDC(c.id)}" title="删除">✕</button>
          </div>
          <div class="dc-exp-title">${escDC(c.title)}</div>
          <div class="dc-exp-content">${escDC(c.content)}</div>
        </div>`;
    }).join('');

    el.querySelectorAll('.dc-exp-delete').forEach(btn => {
      btn.addEventListener('click', async () => {
        const cardId = btn.dataset.cardId;
        if (!confirm('确定删除这张经验卡片？')) return;
        try {
          await fetch(`${DC_API}/experience/${cardId}`, { method: 'DELETE' });
          await loadExperienceCards();
        } catch (e) { /* ignore */ }
      });
    });
  } catch (e) {
    el.innerHTML = `<p class="text-muted">加载失败: ${escDC(e.message)}</p>`;
  }
}

async function loadFeedbackHistory() {
  const el = document.getElementById('dc-feedback-list');
  if (!el) return;
  try {
    const data = await dcFetch('/feedback?limit=20');
    const feedback = data.feedback || [];
    if (feedback.length === 0) {
      el.innerHTML = '<p class="text-muted">暂无交易反馈记录。</p>';
      return;
    }
    let html = `<table class="dc-table"><thead><tr>
      <th>时间</th><th>股票</th><th>类型</th><th>原因</th>
    </tr></thead><tbody>`;
    for (const f of feedback) {
      const typeLabel = f.feedback_type === 'rejection' ? '否决' : f.feedback_type === 'confirmation' ? '确认' : f.feedback_type;
      html += `<tr>
        <td>${(f.created_at || '').slice(0, 10)}</td>
        <td><strong>${escDC(f.ticker)}</strong></td>
        <td>${escDC(typeLabel)}</td>
        <td>${escDC(f.reason || f.user_note || '--')}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<p class="text-muted">加载失败: ${escDC(e.message)}</p>`;
  }
}

async function runBatchReview() {
  const btn = document.getElementById('dc-btn-batch-review');
  const statusEl = document.getElementById('dc-review-status');
  if (!btn || !statusEl) return;

  btn.disabled = true;
  statusEl.textContent = '正在复盘...';
  statusEl.className = 'dc-review-status';

  try {
    await dcSSE('/reviews/run', {
      onEvent(evt) {
        const d = typeof evt.data === 'string' ? JSON.parse(evt.data) : evt.data;
        if (d.message) statusEl.textContent = d.message;
      },
      onDone() {
        statusEl.textContent = '复盘完成';
        statusEl.className = 'dc-review-status success';
        loadReviewData();
      },
      onError(err) {
        statusEl.textContent = `复盘失败: ${err}`;
        statusEl.className = 'dc-review-status error';
      },
    });
  } catch (e) {
    statusEl.textContent = `复盘失败: ${e.message}`;
    statusEl.className = 'dc-review-status error';
  } finally {
    btn.disabled = false;
  }
}

/* ── 绩效报告 ────────────────────────────────────── */

let perfState = { equityChart: null };

function openPerformance() {
  document.getElementById('dc-perf-modal').style.display = '';
  loadPerformanceData();
}

function closePerformance() {
  document.getElementById('dc-perf-modal').style.display = 'none';
}

async function loadPerformanceData() {
  try {
    const [perfResp, monthlyResp, tickersResp] = await Promise.all([
      dcFetch('/performance'),
      dcFetch('/performance/monthly?months=6'),
      dcFetch('/performance/tickers'),
    ]);

    renderPerfCards(perfResp.overview, perfResp.drawdown, perfResp.cost);
    renderPerfEquity();
    renderPerfMonthly(monthlyResp.monthly);
    renderPerfTickers(tickersResp.tickers);
    loadTuningProposals();
  } catch (e) {
    console.error('加载绩效数据失败:', e);
  }
}

function renderPerfCards(overview, drawdown, cost) {
  document.getElementById('dc-perf-return').textContent =
    `${overview.total_return_pct >= 0 ? '+' : ''}${overview.total_return_pct}%`;
  document.getElementById('dc-perf-return').className =
    `dc-perf-value ${pnlClass(overview.total_return_pct)}`;

  document.getElementById('dc-perf-winrate').textContent = `${overview.win_rate}%`;
  document.getElementById('dc-perf-drawdown').textContent = `-${drawdown.max_drawdown_pct}%`;
  document.getElementById('dc-perf-cost').textContent = `$${cost.monthly_cost.toFixed(2)}`;
}

async function renderPerfEquity() {
  const resp = await dcFetch('/account/equity-history?days=90');
  const container = document.getElementById('dc-perf-equity-chart');
  if (!container || !resp.history || resp.history.length === 0) return;

  if (!perfState.equityChart) {
    perfState.equityChart = echarts.init(container);
  }

  perfState.equityChart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 48, right: 24, top: 24, bottom: 32 },
    xAxis: { type: 'category', data: resp.history.map(h => h.date) },
    yAxis: { type: 'value' },
    series: [{
      type: 'line',
      data: resp.history.map(h => h.equity),
      smooth: true,
      lineStyle: { color: '#3b82f6', width: 2 },
      areaStyle: { color: 'rgba(59, 130, 246, 0.1)' },
    }],
  });
}

function renderPerfMonthly(monthly) {
  const container = document.getElementById('dc-perf-monthly');
  if (!monthly || monthly.length === 0) {
    container.innerHTML = '<p class="text-muted">暂无月度数据</p>';
    return;
  }

  const html = `
    <table class="dc-table dc-table-sm">
      <thead>
        <tr><th>月份</th><th>交易数</th><th>胜率</th><th>月收益率</th></tr>
      </thead>
      <tbody>
        ${monthly.map(m => `
          <tr>
            <td>${m.month}</td>
            <td>${m.trades}</td>
            <td>${m.win_rate}%</td>
            <td class="${pnlClass(m.total_return_pct)}">${m.total_return_pct >= 0 ? '+' : ''}${m.total_return_pct}%</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
  container.innerHTML = html;
}

function renderPerfTickers(tickers) {
  const container = document.getElementById('dc-perf-tickers');
  if (!tickers || tickers.length === 0) {
    container.innerHTML = '<p class="text-muted">暂无标的数据</p>';
    return;
  }

  const html = `
    <table class="dc-table dc-table-sm">
      <thead>
        <tr><th>Ticker</th><th>交易数</th><th>胜率</th><th>平均收益</th><th>最佳</th><th>最差</th></tr>
      </thead>
      <tbody>
        ${tickers.map(t => `
          <tr>
            <td><strong>${escDC(t.ticker)}</strong></td>
            <td>${t.trades}</td>
            <td>${t.win_rate}%</td>
            <td class="${pnlClass(t.avg_return_pct)}">${t.avg_return_pct >= 0 ? '+' : ''}${t.avg_return_pct}%</td>
            <td class="${pnlClass(t.best_pct)}">${t.best_pct >= 0 ? '+' : ''}${t.best_pct}%</td>
            <td class="${pnlClass(t.worst_pct)}">${t.worst_pct >= 0 ? '+' : ''}${t.worst_pct}%</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
  container.innerHTML = html;
}

async function loadTuningProposals() {
  try {
    const resp = await dcFetch('/tuning?status=proposed&limit=20');
    renderTuningList(resp.proposals);
  } catch (e) {
    console.error('加载调优建议失败:', e);
  }
}

function renderTuningList(proposals) {
  const container = document.getElementById('dc-tuning-list');
  if (!proposals || proposals.length === 0) {
    container.innerHTML = '<p class="text-muted">暂无调优建议，点击"生成调优建议"按钮</p>';
    return;
  }

  const html = proposals.map(p => `
    <div class="dc-tuning-item">
      <div class="dc-tuning-header">
        <span class="dc-tuning-type">${escDC(p.type)}</span>
        <strong>${escDC(p.parameter_name)}</strong>
      </div>
      <div class="dc-tuning-body">
        <div class="dc-tuning-change">
          <span class="text-muted">${escDC(p.old_value)}</span>
          <span> → </span>
          <span class="text-primary"><strong>${escDC(p.new_value)}</strong></span>
        </div>
        <p class="dc-tuning-reason">${escDC(p.reason)}</p>
        ${p.evidence && p.evidence.length > 0 ? `
          <details class="dc-tuning-evidence">
            <summary>证据 (${p.evidence.length})</summary>
            <ul>${p.evidence.map(e => `<li>${escDC(e)}</li>`).join('')}</ul>
          </details>
        ` : ''}
      </div>
      <div class="dc-tuning-actions">
        <button class="btn btn-sm btn-secondary" onclick="rejectTuning('${p.id}')">拒绝</button>
        <button class="btn btn-sm btn-primary" onclick="approveTuning('${p.id}')">批准</button>
      </div>
    </div>
  `).join('');
  container.innerHTML = html;
}

async function generateTuning() {
  const btn = document.getElementById('dc-btn-gen-tuning');
  const statusEl = document.getElementById('dc-tuning-status');
  if (!btn || !statusEl) return;

  btn.disabled = true;
  statusEl.textContent = '正在分析...';
  statusEl.className = 'dc-review-status';

  try {
    await dcSSE('/tuning/generate', {
      onEvent(evt) {
        const d = typeof evt.data === 'string' ? JSON.parse(evt.data) : evt.data;
        if (d.message) statusEl.textContent = d.message;
      },
      onDone() {
        statusEl.textContent = '调优建议已生成';
        statusEl.className = 'dc-review-status success';
        loadTuningProposals();
      },
      onError(err) {
        statusEl.textContent = `生成失败: ${err}`;
        statusEl.className = 'dc-review-status error';
      },
    });
  } catch (e) {
    statusEl.textContent = `生成失败: ${e.message}`;
    statusEl.className = 'dc-review-status error';
  } finally {
    btn.disabled = false;
  }
}

async function approveTuning(id) {
  try {
    await dcFetch(`/tuning/${id}/approve`, { method: 'POST' });
    loadTuningProposals();
  } catch (e) {
    alert(`批准失败: ${e.message}`);
  }
}

async function rejectTuning(id) {
  const reason = prompt('拒绝理由（可选）:');
  if (reason === null) return;
  try {
    await dcFetch(`/tuning/${id}/reject?reason=${encodeURIComponent(reason)}`, { method: 'POST' });
    loadTuningProposals();
  } catch (e) {
    alert(`拒绝失败: ${e.message}`);
  }
}

window.approveTuning = approveTuning;
window.rejectTuning = rejectTuning;

/* ── 17F.2 风险仪表盘 ──────────────────────────────────── */

async function loadRiskDashboard() {
  const body = document.getElementById('dc-risk-body');
  if (!body) return;
  try {
    const data = await dcFetch('/risk-dashboard');
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
    const qs = monthStr ? `?month=${monthStr}` : '';
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
