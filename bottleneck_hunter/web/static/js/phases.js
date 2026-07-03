/**
 * phases.js — Wizard 4-Phase 控制器 v2
 * 支持三态按钮、主分析模型、层级筛选、AI 解读弹窗、抽屉、赛道管理。
 */

import { renderPhase1, renderPhase2Table, renderPhase3Table, renderPhase4Table, renderScatterPlot, renderRadarChart, renderBarCompare, renderAlphaStack, setP2SelectionCallback, getP2SelectedTickers, resetP2Selection } from './phase-views.js';
import { onProvidersChange, getProviders } from './settings.js';
import { state, logMsg, clearLog, getScoreColor, scoreNeedsDarkText, SCORE_COLORS, getMainModel, formatMarkdown } from './wizard-state.js';
import { readSSEStream } from './sse.js';
import { buildMeetingSetup, startMeeting, handleMeetingEvent, enableMeetingButton, restoreMeeting, runPreflight, toggleAiInterp, generateAiReport, fetchAiInterp, updateTriggerBtn, exportMeeting, MEETING_ROLES } from './ai-features.js';
import { openDrawer, closeDrawer } from './drawer.js';
import { buildAnalysisTag } from './analysis-tag.js';
import { showConfirm } from './utils/confirm.js';



/* ── Phase 1 计时器 ──────────────────────────── */
let _p1TimerInterval = null;
let _p1StartTime = null;

/* ── Phase 1 进度跟踪 ──────────────────────────── */
let _p1Step = '';     // 'decompose' | 'bottleneck'
let _p1BnTotal = 0;
let _p1BnDone = 0;
let _p1DecompDepth = 0;
let _p1DecompMax = 4;

function _resetP1Progress() {
  _p1Step = '';
  _p1BnTotal = 0;
  _p1BnDone = 0;
  _p1DecompDepth = 0;
  _p1DecompMax = 4;
  const wrap = document.getElementById('p1-progress-bar');
  const bar = document.getElementById('p1-progress-fill');
  const text = document.getElementById('p1-progress-text');
  if (wrap) wrap.style.display = 'none';
  if (bar) bar.style.width = '0%';
  if (text) text.textContent = '';
}

function _updateP1Progress(pct, label) {
  const wrap = document.getElementById('p1-progress-bar');
  const bar = document.getElementById('p1-progress-fill');
  const text = document.getElementById('p1-progress-text');
  if (wrap) wrap.style.display = '';
  if (bar) {
    bar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    bar.classList.toggle('active', pct > 0 && pct < 100);
  }
  if (text) text.textContent = label || '';
}

function _startP1Timer() {
  _stopP1Timer();
  _p1StartTime = Date.now();
  const el = document.getElementById('p1-timer');
  if (el) el.style.display = '';
  _updateP1Timer();
  _p1TimerInterval = setInterval(_updateP1Timer, 200);
}

function _stopP1Timer() {
  if (_p1TimerInterval) { clearInterval(_p1TimerInterval); _p1TimerInterval = null; }
}

function _updateP1Timer() {
  if (!_p1StartTime) return;
  const elapsed = Math.floor((Date.now() - _p1StartTime) / 1000);
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  const val = document.getElementById('p1-timer-val');
  if (val) val.textContent = `${String(m).padStart(2, '0')}分${String(s).padStart(2, '0')}秒`;
}

/* ── Phase 2 计时器 ──────────────────────────── */
let _p2TimerInterval = null;
let _p2StartTime = null;

function _startP2Timer() {
  _stopP2Timer();
  _p2StartTime = Date.now();
  const el = document.getElementById('p2-timer');
  if (el) el.style.display = '';
  _updateP2Timer();
  _p2TimerInterval = setInterval(_updateP2Timer, 200);
}

function _stopP2Timer() {
  if (_p2TimerInterval) { clearInterval(_p2TimerInterval); _p2TimerInterval = null; }
}

function _updateP2Timer() {
  if (!_p2StartTime) return;
  const elapsed = Math.floor((Date.now() - _p2StartTime) / 1000);
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  const val = document.getElementById('p2-timer-val');
  if (val) val.textContent = `${String(m).padStart(2, '0')}分${String(s).padStart(2, '0')}秒`;
}

/* ── Phase 4 计时器 ──────────────────────────── */
let _p4TimerInterval = null;
let _p4StartTime = null;

function _startP4Timer() {
  _stopP4Timer();
  _p4StartTime = Date.now();
  const el = document.getElementById('p4-timer');
  if (el) el.style.display = '';
  _updateP4Timer();
  _p4TimerInterval = setInterval(_updateP4Timer, 200);
}

function _stopP4Timer() {
  if (_p4TimerInterval) { clearInterval(_p4TimerInterval); _p4TimerInterval = null; }
}

function _updateP4Timer() {
  if (!_p4StartTime) return;
  const elapsed = Math.floor((Date.now() - _p4StartTime) / 1000);
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  const val = document.getElementById('p4-timer-val');
  if (val) val.textContent = `${String(m).padStart(2, '0')}分${String(s).padStart(2, '0')}秒`;
}

/* ── SSE AbortController（用于暂停） ─────────────── */
let _p1Abort = null;
let _p2Abort = null;
let _p1LaunchTime = 0;
let _p2LaunchTime = 0;
const LAUNCH_GUARD_MS = 800;

/* ── Phase 2 进度跟踪 ──────────────────────────── */
const P2_STEPS = ['supplier_search', 'financial_fetch', 'supplier_eval', 'catalyst'];
let _p2StepIndex = -1;
let _p2EvalTotal = 0;
let _p2EvalDone = 0;

/* ── 新分析重置 ──────────────────────────── */
function resetForNewAnalysis() {
  if (_p1Abort) { try { _p1Abort.abort(); } catch {} _p1Abort = null; }
  if (_p2Abort) { try { _p2Abort.abort(); } catch {} _p2Abort = null; }
  _stopP1Timer();
  _stopP2Timer();
  _stopP4Timer();

  state.analysisId = null;
  state.seqNo = null;
  state.running = false;
  state.config = {};
  state.phase1 = null; state.phase2 = null;
  state.phase3 = null; state.phase4 = null;
  state.p1TriState = 'start'; state.p2TriState = 'start'; state.p3TriState = 'start';
  state.manualPicks = []; state.failedTickers = [];
  state.p1Error = false; state.p2Error = false; state.p4Error = false;
  state.p2NeedsUpdate = false; state.p3NeedsUpdate = false; state.p4NeedsUpdate = false;
  state.aiReports = {};
  state.autoMode = false;
  resetP2Selection();

  setTriState('p1-tristate', 'p1TriState', 'start');
  setTriState('p2-tristate', 'p2TriState', 'start');
  setTriState('p3-tristate', 'p3TriState', 'start');

  ['wiz-p1-next', 'wiz-p2-next'].forEach(id => {
    const b = document.getElementById(id); if (b) { b.disabled = true; b.style.display = 'none'; }
  });
  const p3next = document.getElementById('wiz-p3-next');
  if (p3next) p3next.style.display = 'none';

  ['wiz-chart-force','wiz-chart-tree','wiz-chart-d3','wiz-chart-bn'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      const inst = echarts.getInstanceByDom(el);
      if (inst) inst.dispose();
      el.innerHTML = '';
    }
  });
  const bnStats = document.getElementById('wiz-bn-stats');
  if (bnStats) { bnStats.innerHTML = ''; bnStats.style.display = 'none'; }
  hideP1Info(); hideP1Overlay(); _resetP1Progress(); _updateAnalysisTags();
  const p1Timer = document.getElementById('p1-timer');
  if (p1Timer) p1Timer.style.display = 'none';
  const p1Prog = document.getElementById('wiz-p1-progress');
  if (p1Prog) p1Prog.innerHTML = '';

  const p2Table = document.getElementById('wiz-p2-table');
  if (p2Table) p2Table.innerHTML = '';
  const p2Stats = document.getElementById('wiz-p2-stats');
  if (p2Stats) p2Stats.textContent = '';
  const pickBar = document.getElementById('manual-pick-bar');
  if (pickBar) pickBar.style.display = 'none';
  const weightCtrl = document.getElementById('p2-weight-ctrl');
  if (weightCtrl) weightCtrl.style.display = 'none';
  _resetP2Progress();
  const p2Timer = document.getElementById('p2-timer');
  if (p2Timer) p2Timer.style.display = 'none';

  const p3Table = document.getElementById('wiz-p3-table');
  if (p3Table) p3Table.innerHTML = '';
  ['wiz-chart-scatter','wiz-chart-radar','wiz-chart-bar','wiz-chart-stack'].forEach(id => {
    const el = document.getElementById(id); if (el) el.innerHTML = '';
  });
  // AI 图表解读面板：隐藏 + 清空 body + 复位触发按钮（防切换分析后旧解读文本残留在已展开的面板里）
  document.querySelectorAll('.ai-expand').forEach(p => {
    p.style.display = 'none';
    const b = p.querySelector('.ai-expand-body');
    if (b) b.innerHTML = '';
  });
  document.querySelectorAll('.ai-interp-trigger').forEach(btn => {
    btn.classList.remove('has-cache');
    btn.textContent = 'AI 解读';
  });
  const aiReport = document.getElementById('wiz-ai-report');
  if (aiReport) aiReport.style.display = 'none';
  const reportBody = document.getElementById('wiz-report-body');
  if (reportBody) reportBody.innerHTML = '';

  const p4Table = document.getElementById('wiz-p4-table');
  if (p4Table) p4Table.innerHTML = '';
  const p4Prog = document.getElementById('wiz-p4-progress');
  if (p4Prog) p4Prog.innerHTML = '';
  const p4Timer = document.getElementById('p4-timer');
  if (p4Timer) p4Timer.style.display = 'none';

  clearLog();
  updateSidebarStatus();
}

/* ── 全自动模式: 阶段完成后自动推进 ────────── */
async function _autoChainNext() {
  if (!state.autoMode) return;
  if (state.phase1 && !state.phase2) {
    logMsg('全自动: 自动启动 Phase 2', 'info');
    runPhase2();
  } else if (state.phase2 && !state.phase3) {
    logMsg('全自动: 自动启动 Phase 3', 'info');
    const slider = document.getElementById('wiz-p3-weight-slider');
    const wQ = (slider?.value || 40) / 100;
    runPhase3(wQ, 1 - wQ);
  } else if (state.phase3 && !state.phase4) {
    logMsg('全自动: 自动启动 Phase 4', 'info');
    goToPhase(4);
    const configured = await fetchConfiguredProviders();
    loadCvModels(configured.length > 0 ? configured : undefined);
    setTimeout(() => runPhase4(), 300);
  } else if (state.phase4) {
    logMsg('全自动模式: 全部阶段完成!', 'done');
    state.autoMode = false;
  }
}



/* ── Phase 导航 ───────────────────────────── */
function showPage(page) {
  document.querySelectorAll('#view-wizard .wizard-page').forEach(p => {
    p.classList.remove('active');
    p.style.display = 'none';
  });
  const el = document.getElementById(page);
  if (el) {
    el.classList.add('active');
    el.style.display = 'block';
  }
}

function updateNav() {
  const nav = document.getElementById('wizard-nav');
  if (!nav) return;
  nav.style.display = state.currentPhase > 0 ? 'flex' : 'none';
  const ps = _getPhaseStatus();
  const statusMap = { 1: ps.p1, 2: ps.p2, 3: ps.p3, 4: ps.p4 };

  nav.querySelectorAll('.wiz-step').forEach(step => {
    const p = parseInt(step.dataset.phase);
    const dot = step.querySelector('.wiz-step-dot');
    step.classList.remove('completed', 'active', 'pending', 'step-green', 'step-red', 'step-yellow');

    // 选题(phase 0) 始终可点：任何时候都能返回起始页换课题。
    if (p === 0) {
      step.classList.add('completed');
      dot.textContent = '✓';
      return;
    }

    const status = statusMap[p] || 'gray';
    if (p === state.currentPhase) {
      step.classList.add('active');
      if (status === 'green') { step.classList.add('step-green'); dot.textContent = '✓'; }
      else if (status === 'red') { step.classList.add('step-red'); dot.textContent = '!'; }
      else { dot.textContent = p; }
    } else if (status === 'green') {
      step.classList.add('completed', 'step-green');
      dot.textContent = '✓';
    } else if (status === 'red') {
      step.classList.add('step-red');
      dot.textContent = '!';
    } else if (status === 'yellow') {
      step.classList.add('step-yellow');
      dot.textContent = '⟳';
    } else {
      step.classList.add('pending');
      dot.textContent = p;
    }
  });
}

function goToPhase(phase) {
  state.currentPhase = phase;
  state.currentPage = null;
  updateNav();
  updateSidebarActive();
  updateSidebarStatus();
  if (phase === 0) {
    showPage('wizard-start');
  } else {
    showPage(`wizard-phase${phase}`);
    _updateAnalysisTags();
  }
  // 面板从 display:none 切换为可见后，ECharts 需要 resize
  requestAnimationFrame(() => {
    const page = document.getElementById(phase === 0 ? 'wizard-start' : `wizard-phase${phase}`);
    if (page) page.querySelectorAll('.wiz-chart').forEach(dom => {
      const inst = echarts.getInstanceByDom(dom);
      if (inst) inst.resize();
    });
  });
}

/* _updatePhaseTargets — 旧函数已被 _updateAnalysisTags 取代 */

function _getPhaseStatus() {
  const s = (p, err, needs, data) => {
    if (err) return 'red';
    if (needs && data) return 'yellow';
    if (data) return 'green';
    return 'gray';
  };
  return {
    p1: s(1, state.p1Error, false, state.phase1),
    p2: s(2, state.p2Error, state.p2NeedsUpdate, state.phase2),
    p3: s(3, false, state.p3NeedsUpdate, state.phase3),
    p4: s(4, state.p4Error, state.p4NeedsUpdate, state.phase4),
  };
}

function _savePhaseStatus() {
  if (!state.analysisId) return;
  const ps = _getPhaseStatus();
  fetch(`/api/history/${state.analysisId}/phase-status`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ phase_status: ps }),
  }).catch(() => {});
}

/* ── 侧边栏状态更新 ──────────────────────── */
function updateSidebarActive() {
  document.querySelectorAll('.wiz-sidebar-item').forEach(item => {
    const p = item.dataset.phase;
    const pg = item.dataset.page;
    if (p !== undefined) {
      item.classList.toggle('active', parseInt(p) === state.currentPhase && !state.currentPage);
    } else if (pg) {
      item.classList.toggle('active', state.currentPage === pg);
    }
  });
}

function updateSidebarStatus() {
  document.querySelectorAll('.wiz-sidebar-item').forEach(item => {
    const p = parseInt(item.dataset.phase);
    const dot = item.querySelector('.wiz-status-dot');
    if (!dot) return;
    dot.className = 'wiz-status-dot';
    let status = 'status-gray';
    if (p === 0) {
      status = state.config.sector ? 'status-green' : 'status-gray';
    } else if (p === 1) {
      if (state.p1Error) status = 'status-red';
      else if (state.running && state.currentPhase === 1) status = 'status-running';
      else if (state.phase1) status = 'status-green';
    } else if (p === 2) {
      if (state.p2Error) status = 'status-red';
      else if (state.running && state.currentPhase === 2) status = 'status-running';
      else if (state.p2NeedsUpdate && state.phase2) status = 'status-yellow';
      else if (state.phase2) status = 'status-green';
    } else if (p === 3) {
      if (state.p3NeedsUpdate && state.phase3) status = 'status-yellow';
      else if (state.phase3) status = 'status-green';
    } else if (p === 4) {
      if (state.p4Error) status = 'status-red';
      else if (state.running && state.currentPhase === 4) status = 'status-running';
      else if (state.p4NeedsUpdate && state.phase4) status = 'status-yellow';
      else if (state.phase4) status = 'status-green';
    }
    dot.classList.add(status);
  });
}

function toggleLogPanel() {
  const panel = document.getElementById('wiz-log-panel');
  if (!panel) return;
  const visible = panel.style.display !== 'none';
  panel.style.display = visible ? 'none' : 'block';
  if (!visible) {
    panel.classList.remove('collapsed');
    const tb = document.getElementById('wiz-log-toggle');
    if (tb) tb.textContent = '▼';
  }
  const logBtn = document.getElementById('sidebar-log-btn');
  if (logBtn) logBtn.classList.remove('has-unread');
}

function initSidebar() {
  const sidebar = document.getElementById('wiz-sidebar');
  const toggle = document.getElementById('wiz-sidebar-toggle');
  if (!sidebar || !toggle) return;

  if (localStorage.getItem('wiz-sidebar-collapsed') === '1') {
    sidebar.classList.add('collapsed');
  }

  toggle.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
    localStorage.setItem('wiz-sidebar-collapsed', sidebar.classList.contains('collapsed') ? '1' : '0');
  });

  const menu = sidebar.querySelector('.wiz-sidebar-menu');
  if (menu) {
    menu.addEventListener('click', (e) => {
      const item = e.target.closest('.wiz-sidebar-item');
      if (!item) return;
      if (item.id === 'sidebar-log-btn') {
        toggleLogPanel();
      } else if (item.dataset.phase !== undefined) {
        goToPhase(parseInt(item.dataset.phase));
      }
    });
  }
}


/* ── 四态按钮 ─────────────────────────────── */
const TRISTATE_LABELS = {
  'p1-tristate': { start: '开始分析', pause: '暂停分析', resume: '继续分析', restart: '重新分析' },
  'p2-tristate': { start: '开始筛选', pause: '暂停筛选', resume: '继续筛选', restart: '重新筛选' },
  'p3-tristate': { start: '开始评选', pause: '暂停评选', resume: '继续评选', restart: '重新评选' },
};

function setTriState(btnId, stateKey, newState) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  state[stateKey] = newState;
  btn.classList.remove('state-start', 'state-pause', 'state-resume', 'state-restart');
  btn.classList.add(`state-${newState}`);
  const labels = TRISTATE_LABELS[btnId] || TRISTATE_LABELS['p1-tristate'];
  btn.textContent = labels[newState] || labels.start;
}

/* ── 分析记录标签（替换旧的 seq badge + phase target） ── */
function _updateAnalysisTags() {
  const completedPhases = state.config.completed_phases || 0;

  const data = {
    seq_no: state.seqNo,
    market: state.config.market || 'us_stock',
    sector: state.config.sector || '',
    end_product: state.config.product || '',
    provider: '',
    model: '',
    completed_phases: completedPhases,
    created_at: state.config.created_at || '',
    run_count: state.config.run_count || 1,
  };
  const { provider, model } = getMainModel();
  data.provider = provider || '';
  data.model = model || '';

  const hasData = data.sector || data.seq_no;
  const fullHtml = hasData ? buildAnalysisTag(data) : '';

  ['p1-analysis-tag', 'p2-analysis-tag', 'p3-analysis-tag', 'p4-analysis-tag'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = fullHtml;
  });
  const sidebar = document.getElementById('sidebar-analysis-tag');
  if (sidebar) {
    if (hasData) {
      const seq = data.seq_no ? `#${data.seq_no}` : '';
      const rc = String(data.run_count || 1).padStart(3, '0');
      const sector = data.sector || '';
      const mkt = data.market === 'a_stock' ? 'A股' : '美股';
      const cp = data.completed_phases || 0;
      const dots = [0,1,2,3,4].map(i => `<span style="display:inline-block;width:5px;height:5px;border-radius:50%;background:${i < cp ? 'oklch(0.55 0.12 155)' : '#ccc'};margin:0 1px"></span>`).join('');
      sidebar.innerHTML =
        `<div style="display:flex;border:1px solid var(--border,#ddd);border-radius:8px;overflow:hidden;width:100%">` +
          `<div style="display:flex;align-items:center;justify-content:center;padding:0 8px;font:800 16px/1 monospace;background:var(--surface,#f8f8f8);border-right:1px solid var(--border,#ddd)">${seq}</div>` +
          `<div style="display:flex;flex-direction:column;flex:1;min-width:0">` +
            `<div style="display:flex;align-items:center;justify-content:space-between;padding:3px 7px;border-bottom:1px solid #eee;background:var(--panel-bg,#fafafa)">` +
              `<span style="font:600 11px/1 sans-serif">${mkt}</span>` +
              `<span style="display:flex;align-items:center;gap:2px">${dots}</span>` +
            `</div>` +
            `<div style="display:flex;align-items:center;justify-content:space-between;padding:3px 7px;background:var(--surface,#f8f8f8)">` +
              `<span style="font:600 11px/1 sans-serif;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">${sector}</span>` +
              `<span style="font:600 10px/1 monospace;color:oklch(0.50 0.10 155);background:oklch(0.95 0.03 155);padding:1px 4px;border-radius:3px;flex-shrink:0">${rc}</span>` +
            `</div>` +
          `</div>` +
        `</div>`;
      sidebar.style.display = '';
    } else {
      sidebar.innerHTML = `<div style="border:1px dashed var(--border,#ccc);border-radius:8px;padding:8px 10px;text-align:center;font-size:11px;color:var(--muted,#999);width:100%;box-sizing:border-box">待载入分析数据</div>`;
      sidebar.style.display = '';
    }
  }
}

/* ── 进度信息栏 ───────────────────────────── */
function showP1Info(text) {
  const bar = document.getElementById('p1-info-bar');
  const txt = document.getElementById('p1-info-text');
  if (bar) bar.style.display = 'flex';
  if (txt) txt.textContent = text;
}
function hideP1Info() {
  const bar = document.getElementById('p1-info-bar');
  if (bar) bar.style.display = 'none';
}

function showP1Overlay(text) {
  const overlay = document.getElementById('p1-progress-overlay');
  const detail = document.getElementById('p1-progress-detail');
  if (overlay) overlay.style.display = 'flex';
  if (detail) detail.innerHTML = `<div class="step-current">${text}</div>`;
}
function hideP1Overlay() {
  const overlay = document.getElementById('p1-progress-overlay');
  if (overlay) overlay.style.display = 'none';
}

/* ── Phase 1: SSE 连接 ──────────────────── */
async function runPhase1(sector, product) {
  state.config.sector = sector;
  state.config.product = product;
  state.running = true;
  state.p1Error = false;
  if (state.phase2) state.p2NeedsUpdate = true;
  if (state.phase3) state.p3NeedsUpdate = true;
  if (state.phase4) state.p4NeedsUpdate = true;

  // 异步获取同赛道的累计分析次数（不阻塞启动流程）
  fetch('/api/history').then(r => r.json()).then(d => {
    const count = (d.analyses || []).filter(a => a.sector === sector && a.end_product === product).length;
    state.config.run_count = count + 1; // +1 算上本次
    _updateAnalysisTags();
  }).catch(() => {});

  goToPhase(1);

  const nextBtn = document.getElementById('wiz-p1-next');
  nextBtn.disabled = true;
  nextBtn.style.display = 'none';
  setTriState('p1-tristate', 'p1TriState', 'pause');
  _p1LaunchTime = Date.now();
  showP1Info('正在初始化...');
  showP1Overlay('正在初始化分析...');

  clearLog();
  _startP1Timer();
  _resetP1Progress();
  _updateP1Progress(0, '初始化...');
  logMsg(`Phase 1 启动 — 产业: ${sector}, 产品: ${product}`);

  const depth = parseInt(document.getElementById('wiz-depth')?.value || '4');
  const topN = parseInt(document.getElementById('wiz-topn')?.value || '5');
  const { provider, model } = getMainModel();
  const market = state.config.market || 'us_stock';

  const maxCap = parseFloat(document.getElementById('wiz-max-cap')?.value || '200');
  state.config.max_market_cap_yi = maxCap;

  logMsg(`参数: 深度=${depth}, TopN=${topN}, 市场=${market}, 模型=${provider}/${model || '默认'}`);

  const body = {
    sector, end_product: product,
    max_depth: depth, top_n: topN,
    max_market_cap_yi: maxCap,
    language: 'zh', provider, model, market,
  };

  _p1Abort = new AbortController();
  try {
    await readSSEStream('/api/phase1', body, {
      label: 'phase1-sse',
      logFn: logMsg,
      signal: _p1Abort.signal,
      getAnalysisId: () => state.analysisId,
      onTick: _updateP1Timer,
      onEvent: (data) => handlePhase1Event(data),
      onError: (err) => {
        showP1Info(`连接失败: ${err.message}`);
        logMsg(`Phase 1 连接失败: ${err.message}`, 'error');
        _stopP1Timer();
        state.p1Error = true;
        state.autoMode = false;
        setTriState('p1-tristate', 'p1TriState', 'restart');
      },
    });
  } catch (err) {
    if (err.name !== 'AbortError') {
      showP1Info(`连接失败: ${err.message}`);
      logMsg(`Phase 1 连接失败: ${err.message}`, 'error');
      _stopP1Timer();
      state.p1Error = true;
      state.autoMode = false;
      setTriState('p1-tristate', 'p1TriState', 'restart');
    }
  }
  _p1Abort = null;
  state.running = false;
  if (!state.phase1 && state.p1TriState === 'pause') {
    setTriState('p1-tristate', 'p1TriState', 'restart');
  }
  hideP1Overlay();
  updateSidebarStatus();
  updateNav();
}

function handlePhase1Event(data) {
  // step_start: 有 index 和 step 字段
  if (data.index !== undefined && data.step && !data.result) {
    _p1Step = data.step;
    if (data.step === 'decompose') {
      _updateP1Progress(2, '正在拆解产业链...');
    } else if (data.step === 'bottleneck') {
      _updateP1Progress(40, '正在分析瓶颈...');
    }
  }

  if (data.step === 'decompose' || data.step === 'bottleneck') {
    if (data.message) {
      showP1Info(data.message);
      showP1Overlay(data.message);
      logMsg(`[${data.step}] ${data.message}`);
    }
  }

  // 拆解进度: 解析 "第 X/Y 层"
  if (data.step === 'decompose' && data.message) {
    const layerMatch = data.message.match(/第\s*(\d+)\s*\/\s*(\d+)\s*层/);
    if (layerMatch) {
      _p1DecompDepth = parseInt(layerMatch[1]);
      _p1DecompMax = parseInt(layerMatch[2]);
      const pct = 2 + (_p1DecompDepth / _p1DecompMax) * 38;
      _updateP1Progress(pct, `拆解第 ${_p1DecompDepth}/${_p1DecompMax} 层`);
    }
  }

  // step_done: 有 result 字段
  if (data.result && data.step === 'decompose') {
    _updateP1Progress(40, '产业链拆解完成');
  }

  // 瓶颈分析进度: 解析 "(X/Y)"
  if (data.step === 'bottleneck' && data.message) {
    const m = data.message.match(/\((\d+)\/(\d+)\)/);
    if (m) {
      _p1BnDone = parseInt(m[1]);
      _p1BnTotal = parseInt(m[2]);
      const pct = 40 + (_p1BnDone / _p1BnTotal) * 58;
      _updateP1Progress(pct, `瓶颈分析 ${_p1BnDone}/${_p1BnTotal}`);
    }
  }

  if (data.result && data.step === 'bottleneck') {
    _updateP1Progress(100, '分析完成');
  }

  if (data.analysis_id) {
    state.p1Error = false;
    state.analysisId = data.analysis_id;
    if (data.seq_no) state.seqNo = data.seq_no;
    if (data.run_count) state.config.run_count = data.run_count;
    if (data.completed_phases) state.config.completed_phases = data.completed_phases;
    state.phase1 = data;
    if (state.phase2) state.p2NeedsUpdate = true; else state.phase2 = null;
    if (state.phase3) state.p3NeedsUpdate = true; else state.phase3 = null;
    if (state.phase4) state.p4NeedsUpdate = true; else state.phase4 = null;

    _stopP1Timer();
    _updateP1Progress(100, '分析完成');

    const reportCount = (data.top_reports || []).length;
    const seqTag = state.seqNo ? `#${state.seqNo} ` : '';
    logMsg(`Phase 1 完成 — ${seqTag}瓶颈报告 ${reportCount} 条`, 'done');
    logMsg('本次分析数据存储成功', 'done');
    _updateAnalysisTags();

    renderPhase1(data);

    const nextBtn = document.getElementById('wiz-p1-next');
    nextBtn.disabled = false;
    nextBtn.style.display = '';
    showP1Info('本次分析数据存储成功');
    setTimeout(() => hideP1Info(), 3000);
    hideP1Overlay();
    setTriState('p1-tristate', 'p1TriState', 'restart');
    updateSidebarStatus();
    updateNav();
    // 新建正向记录后，刷新反向分析列表使其归属该新记录（清掉上一条记录的列表）
    if (window.reloadReverseList) window.reloadReverseList();
    _savePhaseStatus();
    _autoChainNext();
  }

  if (data._sseEvent === 'error' || (data.message && data.message.includes('失败'))) {
    showP1Info(`错误: ${data.message}`);
    logMsg(`Phase 1 错误: ${data.message}`, 'error');
    _stopP1Timer();
    setTriState('p1-tristate', 'p1TriState', 'restart');
    state.p1Error = true;
    state.autoMode = false;
    updateSidebarStatus();
    updateNav();
  }
}

/* ── Phase 2: 去重 ──────────────────────────── */
let _p2LastMsgs = [];
const P2_DEDUP_WINDOW = 3000;

function _p2IsDuplicate(msg) {
  if (!msg) return false;
  const now = Date.now();
  _p2LastMsgs = _p2LastMsgs.filter(e => now - e.ts < P2_DEDUP_WINDOW);
  if (_p2LastMsgs.some(e => e.msg === msg)) return true;
  _p2LastMsgs.push({ msg, ts: now });
  return false;
}

/* ── Phase 2: SSE 连接 ──────────────────── */
async function runPhase2() {
  if (!state.analysisId) return;
  state.running = true;
  state.p2Error = false;
  state.p2NeedsUpdate = false;
  if (state.phase3) state.p3NeedsUpdate = true;
  if (state.phase4) state.p4NeedsUpdate = true;
  goToPhase(2);
  clearLog();

  const tableWrap = document.getElementById('wiz-p2-table');
  if (tableWrap) tableWrap.innerHTML = '';
  const p2Next = document.getElementById('wiz-p2-next');
  if (p2Next) { p2Next.disabled = true; p2Next.style.display = 'none'; }
  const pickBar = document.getElementById('manual-pick-bar');
  if (pickBar) pickBar.style.display = 'none';
  const statsEl = document.getElementById('wiz-p2-stats');
  if (statsEl) statsEl.textContent = '';

  _p2StepIndex = -1;
  _p2EvalTotal = 0;
  _p2EvalDone = 0;
  _p2LastMsgs = [];

  const progress = document.getElementById('wiz-p2-progress');
  const nextBtn = document.getElementById('wiz-p2-next');
  nextBtn.disabled = true;

  setTriState('p2-tristate', 'p2TriState', 'pause');
  _p2LaunchTime = Date.now();

  _resetP2Progress();
  _updateP2Progress(0, '初始化...');
  _startP2Timer();
  logMsg('Phase 2 启动 — 搜索供应商并评估');

  const { provider, model } = getMainModel();

  const l1 = parseInt(document.getElementById('wiz-l1-topn')?.value || '8');
  const l2 = parseInt(document.getElementById('wiz-l2-topn')?.value || '7');
  const l3 = parseInt(document.getElementById('wiz-l3-topn')?.value || '5');
  const minScore = parseFloat(document.getElementById('wiz-min-score')?.value || '0');
  const maxCount = l1 + l2 + l3;

  logMsg(`参数: L1=${l1}, L2=${l2}, L3=${l3}, 最低分=${minScore}, LLM=${provider}/${model || '默认'}`);

  const body = {
    analysis_id: state.analysisId,
    shortlist_config: {
      per_layer_top_n: l1,
      layer_top_n: { "1": l1, "2": l2, "3": l3 },
      min_overall_score: minScore,
      max_shortlist_count: maxCount,
    },
    market: state.config.market || 'us_stock',
    max_market_cap_yi: state.config.max_market_cap_yi || parseFloat(document.getElementById('wiz-max-cap')?.value || '200'),
    max_suppliers: 20,
    language: 'zh', provider, model,
  };

  _p2Abort = new AbortController();
  try {
    await readSSEStream('/api/phase2', body, {
      label: 'phase2-sse',
      logFn: logMsg,
      signal: _p2Abort.signal,
      getAnalysisId: () => state.analysisId,
      onTick: _updateP2Timer,
      onEvent: (data) => handlePhase2Event(data, progress),
      onError: (err) => {
        progress.innerHTML = `<div class="progress-msg progress-error">连接失败: ${err.message}</div>`;
        logMsg(`Phase 2 连接失败: ${err.message}`, 'error');
        _stopP2Timer();
        state.p2Error = true;
        state.autoMode = false;
        setTriState('p2-tristate', 'p2TriState', 'restart');
      },
    });
  } catch (err) {
    if (err.name !== 'AbortError' && state.running) {
      progress.innerHTML = `<div class="progress-msg progress-error">连接失败: ${err.message}</div>`;
      logMsg(`Phase 2 连接失败: ${err.message}`, 'error');
      _stopP2Timer();
      state.p2Error = true;
      state.autoMode = false;
    }
  }
  _p2Abort = null;
  state.running = false;
  if (!state.phase2 && state.p2TriState === 'pause') {
    setTriState('p2-tristate', 'p2TriState', 'restart');
  }
  updateSidebarStatus();
  updateNav();
}

function handlePhase2Event(data, progress) {
  if (data._sseEvent === 'error' && data.step === 'init' && data.message) {
    progress.innerHTML = `<div class="progress-msg progress-error">${data.message}</div>`;
    logMsg(`[phase2] ${data.message}`, 'error');
    _stopP2Timer();
    state.p2Error = true;
    state.autoMode = false;
    setTriState('p2-tristate', 'p2TriState', 'restart');
    updateSidebarStatus();
    updateNav();
    return;
  }

  if (data.message && _p2IsDuplicate(data.message)) return;

  if (data.index !== undefined && data.step) {
    _p2StepIndex = data.index;
    const isDone = data._sseEvent === 'step_done' || data.result !== undefined;
    const stepPct = ((data.index + (isDone ? 1 : 0)) / P2_STEPS.length) * 100;
    _updateP2Progress(stepPct, data.message || data.step);
  }

  if (data.message) {
    progress.innerHTML = `<div class="progress-msg">${data.message}</div>`;

    const isCatHeartbeat = data.message.startsWith('▸') && _p2StepIndex === 3;
    const isStarting = data.message.startsWith('▸') && _p2StepIndex !== 3;

    if (isCatHeartbeat) {
      logMsg(`[phase2] ${data.message}`, 'info');
    } else if (!isStarting) {
      logMsg(`[phase2] ${data.message}`);
    }

    const m = data.message.match(/\((\d+)\/(\d+)(?:，预计还需 (\d+)s)?\)/);
    if (m) {
      const done = parseInt(m[1]);
      const total = parseInt(m[2]);
      const etaSec = m[3] ? parseInt(m[3]) : null;
      const stepIdx = _p2StepIndex >= 0 ? _p2StepIndex : 2;
      const stepBase = (stepIdx / P2_STEPS.length) * 100;
      const stepRange = (1 / P2_STEPS.length) * 100;
      const pct = stepBase + (done / total) * stepRange;
      const pctDisplay = Math.round(pct);

      let label;
      const etaStr = etaSec !== null ? (etaSec >= 60 ? `${Math.floor(etaSec / 60)}m${etaSec % 60}s` : `${etaSec}s`) : '';
      if (stepIdx === 3) {
        label = `催化剂 ${done}/${total} (${pctDisplay}%)`;
        if (etaStr) label += ` ≈${etaStr}`;
      } else {
        label = `评估中 ${done}/${total} (${pctDisplay}%)`;
        if (etaStr) label += ` ≈${etaStr}`;
      }
      _updateP2Progress(pct, label);
    }
  }

  if (data.scorecards) {
    state.phase2 = data;
    state.phase3 = null;
    state.phase4 = null;
    state.manualPicks = [];
    state.p2Error = false;
    if (data.completed_phases) state.config.completed_phases = data.completed_phases;
    resetP2Selection();
    state.failedTickers = data.failed_tickers || [];

    renderPhase2Table(data.scorecards, state.failedTickers);
    showP2WeightCtrl();
    _updateP2Progress(100, '筛选完成');

    const stats = data.stats || {};
    const statsEl = document.getElementById('wiz-p2-stats');
    if (statsEl) {
      statsEl.textContent = `共搜索 ${stats.total_searched || '?'} 家，评估后 ${stats.after_eval || '?'} 家，筛选后 ${stats.after_filter || data.scorecards.length} 家`;
    }

    const pickBar = document.getElementById('manual-pick-bar');
    if (pickBar) pickBar.style.display = 'flex';
    updateManualPickCount();

    logMsg(`Phase 2 完成 — 入围 ${data.scorecards.length} 家`, 'done');
    _stopP2Timer();

    const nextBtn = document.getElementById('wiz-p2-next');
    nextBtn.disabled = false;
    nextBtn.style.display = '';
    progress.innerHTML = '<div class="progress-msg progress-done">Phase 2 完成</div>';
    setTriState('p2-tristate', 'p2TriState', 'restart');
    _updateAnalysisTags();
    updateSidebarStatus();
    updateNav();
    _savePhaseStatus();
    _autoChainNext();
  }
}

/* ── Phase 2 进度条 ──────────────────────────── */
function _resetP2Progress() {
  const bar = document.getElementById('p2-progress-fill');
  const text = document.getElementById('p2-progress-text');
  const wrap = document.getElementById('p2-progress-bar');
  if (wrap) wrap.style.display = '';
  if (bar) { bar.style.width = '0%'; }
  if (text) text.textContent = '';
}

function _updateP2Progress(pct, label) {
  const bar = document.getElementById('p2-progress-fill');
  const text = document.getElementById('p2-progress-text');
  const rounded = Math.min(100, Math.max(0, Math.round(pct)));
  if (bar) {
    bar.style.width = `${rounded}%`;
    bar.classList.toggle('active', rounded > 0 && rounded < 100);
  }
  if (text) text.textContent = label || `${rounded}%`;
}

/* ── Phase 2 手动入选 ─────────────────────── */
function updateManualPickCount() {
  const countEl = document.getElementById('manual-pick-count');
  if (countEl) countEl.textContent = `已选 ${state.manualPicks.length} / 3 家`;
}

/* ── Phase 2 层级合计 ─────────────────────── */
function updateP2Total() {
  const l1 = parseInt(document.getElementById('wiz-l1-topn')?.value || '8');
  const l2 = parseInt(document.getElementById('wiz-l2-topn')?.value || '7');
  const l3 = parseInt(document.getElementById('wiz-l3-topn')?.value || '5');
  const el = document.getElementById('p2-total');
  if (el) el.textContent = `合计: ${l1 + l2 + l3} 家`;
}

function showP2WeightCtrl() {
  const ctrl = document.getElementById('p2-weight-ctrl');
  if (ctrl) ctrl.style.display = '';
}

function recalcP2Final(scorecards, wQ, wA) {
  for (const sc of scorecards) {
    const quality = Math.max(0.1, Math.min(10, sc.overall_score || 0));
    const alpha = Math.max(0.1, Math.min(10, sc.alpha?.alpha_score || 0.1));
    const finalScore = Math.max(0, Math.min(10,
      Math.pow(quality, wQ) * Math.pow(alpha, wA)));
    sc.final = {
      quality_score: Math.round(quality * 100) / 100,
      alpha_score: Math.round(alpha * 100) / 100,
      final_score: Math.round(finalScore * 100) / 100,
      quality_weight: wQ,
      alpha_weight: wA,
    };
  }
}

/* ── Phase 3: 即时计算 ──────────────────── */
function runPhase3(wQ, wA) {
  if (!state.phase2) return;
  state.p3NeedsUpdate = false;
  if (state.phase4) state.p4NeedsUpdate = true;
  goToPhase(3);
  setTriState('p3-tristate', 'p3TriState', 'restart');

  const topN = parseInt(document.getElementById('wiz-p3-topn')?.value || '5', 10);

  const allScorecards = state.phase2.scorecards;
  const selected = getP2SelectedTickers();
  const scorecards = selected.size > 0
    ? allScorecards.filter(sc => selected.has(sc.supplier?.ticker || sc.ticker || ''))
    : allScorecards;
  logMsg(`Phase 3 评选 — 已选${scorecards.length}/${allScorecards.length}家, 质量权重=${Math.round(wQ * 100)}%, 预期差权重=${Math.round(wA * 100)}%, Top-${topN}`);
  const allRanked = recalcPhase3(scorecards, wQ, wA);
  const ranked = allRanked.slice(0, topN);
  state.phase3 = { ranked_results: ranked, scoring_config: { quality_weight: wQ, alpha_weight: wA, top_n: topN } };

  renderPhase3Table(ranked, openDrawer);
  renderScatterPlot(ranked);
  renderRadarChart(ranked);
  renderBarCompare(ranked);
  renderAlphaStack(ranked);
  updateSidebarStatus();
  updateNav();

  const p3Next = document.getElementById('wiz-p3-next');
  if (p3Next) p3Next.style.display = '';

  // 显示 AI 报告区域
  const reportCard = document.getElementById('wiz-ai-report');
  if (reportCard) reportCard.style.display = '';

  logMsg(`Phase 3 完成 — 排名 ${ranked.length} 家, Top1: ${ranked[0]?.supplier?.name || ranked[0]?.name || '-'} (${ranked[0]?.final_score?.toFixed(2) || '-'})`, 'done');

  fetch('/api/phase3/score', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      analysis_id: state.analysisId,
      scoring_config: { quality_weight: wQ, alpha_weight: wA, top_n: topN },
    }),
  }).then(() => {
    if ((state.config.completed_phases || 0) < 3) {
      state.config.completed_phases = 3;
      _updateAnalysisTags();
    }
  }).catch(() => {});

  _savePhaseStatus();
  _updateAnalysisTags();
  _autoChainNext();
}

function recalcPhase3(scorecards, wQ, wA) {
  return scorecards.map(sc => {
    const quality = Math.max(0.1, Math.min(10, sc.overall_score || 0));
    const alpha = Math.max(0.1, Math.min(10, sc.alpha?.alpha_score || 0.1));
    const finalScore = Math.max(0, Math.min(10,
      Math.pow(quality, wQ) * Math.pow(alpha, wA)));
    return {
      ...sc,
      quality_score: Math.round(quality * 100) / 100,
      alpha_val: Math.round(alpha * 100) / 100,
      final_score: Math.round(finalScore * 100) / 100,
    };
  }).sort((a, b) => b.final_score - a.final_score)
    .map((c, i) => ({ ...c, rank: i + 1 }));
}

/* ── Phase 4: SSE 连接 ──────────────────── */
async function runPhase4() {
  if (!state.analysisId) return;
  state.running = true;
  state.p4Error = false;
  state.p4NeedsUpdate = false;
  goToPhase(4);

  const progress = document.getElementById('wiz-p4-progress');
  progress.innerHTML = '<div class="progress-msg">正在启动交叉验证...</div>';
  _startP4Timer();
  logMsg('Phase 4 启动 — 交叉验证');

  const topN = parseInt(document.getElementById('wiz-cv-topn')?.value || '10');

  const modelCheckboxes = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  const validationModels = Array.from(modelCheckboxes).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model };
  });

  if (validationModels.length === 0) {
    progress.innerHTML = '<div class="progress-msg progress-error">请至少选择一个验证模型</div>';
    logMsg('Phase 4 错误: 未选择验证模型', 'error');
    _stopP4Timer();
    state.running = false;
    state.p4Error = true;
    state.autoMode = false;
    updateSidebarStatus();
    return;
  }

  logMsg(`参数: TopN=${topN}, 模型=[${validationModels.map(m => m.provider + '/' + m.model).join(', ')}]`);

  const body = {
    analysis_id: state.analysisId,
    top_n: topN,
    validation_models: validationModels,
    language: 'zh',
  };

  try {
    await readSSEStream('/api/phase4', body, {
      label: 'phase4-sse',
      logFn: logMsg,
      getAnalysisId: () => state.analysisId,
      onTick: _updateP4Timer,
      onEvent: (data) => handlePhase4Event(data, progress),
      onError: (err) => {
        progress.innerHTML = `<div class="progress-msg progress-error">连接失败: ${err.message}</div>`;
        logMsg(`Phase 4 连接失败: ${err.message}`, 'error');
        _stopP4Timer();
        state.p4Error = true;
        state.autoMode = false;
      },
    });
  } catch (err) {
    progress.innerHTML = `<div class="progress-msg progress-error">连接失败: ${err.message}</div>`;
    logMsg(`Phase 4 连接失败: ${err.message}`, 'error');
    _stopP4Timer();
    state.p4Error = true;
    state.autoMode = false;
  }
  _stopP4Timer();
  state.running = false;
  updateSidebarStatus();
  updateNav();
}

function handlePhase4Event(data, progress) {
  if (data.message) {
    progress.innerHTML = `<div class="progress-msg">${data.message}</div>`;
    logMsg(`[phase4] ${data.message}`);
  }

  if (data.validations || data.recommendations) {
    state.phase4 = data;
    state.p4Error = false;
    if (data.completed_phases) state.config.completed_phases = data.completed_phases;
    renderPhase4Table(data.validations || [], data.recommendations || [], state.phase3?.ranked_results || []);
    const vCount = (data.validations || []).length;
    logMsg(`Phase 4 完成 — 验证 ${vCount} 家公司`, 'done');
    progress.innerHTML = '<div class="progress-msg progress-done">交叉验证完成</div>';
    enableMeetingButton();
    updateSidebarStatus();
    updateNav();
    _savePhaseStatus();
    _updateAnalysisTags();
    _autoChainNext();
  }
}

/* ── 赛道管理 ──────────────────────────────── */
const DEFAULT_SECTORS = [
  { name: 'GPU / AI算力', sector: 'GPU/AI算力', product: 'GPU', market: 'us_stock' },
  { name: '人形机器人', sector: '人形机器人', product: '人形机器人', market: 'us_stock' },
  { name: '商业航天', sector: '商业航天', product: '商业运载火箭', market: 'us_stock' },
  { name: '新能源车', sector: '新能源车', product: '电动汽车', market: 'us_stock' },
];

function loadSectors() {
  try {
    const saved = localStorage.getItem('bh_sectors');
    return saved ? JSON.parse(saved) : [...DEFAULT_SECTORS];
  } catch { return [...DEFAULT_SECTORS]; }
}

function saveSectors(sectors) {
  localStorage.setItem('bh_sectors', JSON.stringify(sectors));
}

function renderSectorButtons() {
  const grid = document.getElementById('wizard-sectors');
  if (!grid) return;
  const sectors = loadSectors();
  grid.innerHTML = sectors.map((s, i) =>
    `<button class="sector-btn" data-idx="${i}" data-sector="${s.sector}" data-product="${s.product}" data-market="${s.market || 'us_stock'}">${s.name}<span class="sector-menu-btn" title="配置赛道">⋯</span></button>`
  ).join('') + (sectors.length < 8 ? '<button class="sector-btn sector-add" id="wiz-add-sector">+ 添加赛道</button>' : '');

  grid.querySelectorAll('.sector-btn:not(.sector-add)').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (e.target.closest('.sector-menu-btn')) { showCtxMenu(e, btn); return; }
      const s = loadSectors()[parseInt(btn.dataset.idx)];
      if (!s?.sector || !s?.product) { showCtxMenu(e, btn); return; }
      resetForNewAnalysis();
      applySectorConfig(s);
      goToPhase(1);
    });
    btn.addEventListener('contextmenu', e => {
      e.preventDefault();
      showCtxMenu(e, btn);
    });
  });

  grid.querySelector('#wiz-add-sector')?.addEventListener('click', () => {
    const sectors = loadSectors();
    if (sectors.length >= 8) return;
    sectors.push({ name: '新赛道', sector: '新赛道', product: '产品', market: 'a_stock' });
    saveSectors(sectors);
    renderSectorButtons();
  });
}

function applySectorConfig(s) {
  state.config.sector = s.sector;
  state.config.product = s.product;
  state.config.market = s.market || 'us_stock';

  const marketSel = document.getElementById('wiz-p1-market');
  if (marketSel) marketSel.value = s.market || 'us_stock';
  const startMarketSel = document.getElementById('wiz-market');
  if (startMarketSel) startMarketSel.value = s.market || 'us_stock';
  const sectorEl = document.getElementById('wiz-sector');
  if (sectorEl) sectorEl.value = s.sector || '';
  const productEl = document.getElementById('wiz-product');
  if (productEl) productEl.value = s.product || '';
  // 赛道自带的主分析模型（在“配置赛道参数”详细设置里选）→ 同步到 getMainModel() 读取的 wiz-main-model
  const mainModel = document.getElementById('wiz-main-model');
  if (s.model && mainModel) {
    if (!Array.from(mainModel.options).some(o => o.value === s.model)) {
      mainModel.add(new Option(s.model, s.model));
    }
    mainModel.value = s.model;
  }
  updateSidebarStatus();
}

/* ── 右键菜单 ──────────────────────────────── */
let ctxTarget = null;

function showCtxMenu(e, btn) {
  const menu = document.getElementById('ctx-menu');
  if (!menu) return;
  ctxTarget = btn;
  const idx = parseInt(btn.dataset.idx);
  const sectors = loadSectors();
  const s = sectors[idx] || {};

  document.getElementById('ctx-market').value = s.market || 'us_stock';
  document.getElementById('ctx-sector').value = s.sector || '';
  document.getElementById('ctx-product').value = s.product || '';
  const ctxModel = document.getElementById('ctx-model');
  if (ctxModel && s.model) {
    if (!Array.from(ctxModel.options).some(o => o.value === s.model)) {
      ctxModel.add(new Option(s.model, s.model));
    }
    ctxModel.value = s.model;
  } else if (ctxModel) {
    ctxModel.selectedIndex = 0;  // 未设置则回到默认
  }

  menu.style.display = 'block';
  if (e.type === 'click') {
    const rect = btn.getBoundingClientRect();
    menu.style.left = Math.min(rect.left, window.innerWidth - 300) + 'px';
    menu.style.top = (rect.bottom + 6) + 'px';
  } else {
    menu.style.left = Math.min(e.clientX, window.innerWidth - 300) + 'px';
    menu.style.top = Math.min(e.clientY, window.innerHeight - 200) + 'px';
  }
}

function hideCtxMenu() {
  const menu = document.getElementById('ctx-menu');
  if (menu) menu.style.display = 'none';
  ctxTarget = null;
}

function initCtxMenu() {
  document.getElementById('ctx-cancel')?.addEventListener('click', hideCtxMenu);
  document.getElementById('ctx-save')?.addEventListener('click', () => {
    if (!ctxTarget) return;
    const idx = parseInt(ctxTarget.dataset.idx);
    const sectors = loadSectors();
    if (sectors[idx]) {
      sectors[idx].market = document.getElementById('ctx-market').value;
      sectors[idx].sector = document.getElementById('ctx-sector').value;
      sectors[idx].product = document.getElementById('ctx-product').value;
      sectors[idx].model = document.getElementById('ctx-model')?.value || '';
      sectors[idx].name = sectors[idx].sector;
      saveSectors(sectors);
      renderSectorButtons();
    }
    hideCtxMenu();
  });
  document.getElementById('ctx-start')?.addEventListener('click', () => {
    const sector = document.getElementById('ctx-sector')?.value?.trim();
    const product = document.getElementById('ctx-product')?.value?.trim();
    const market = document.getElementById('ctx-market')?.value || 'us_stock';
    const model = document.getElementById('ctx-model')?.value || '';
    if (!sector || !product) { alert('请输入产业方向和终端产品'); return; }
    if (ctxTarget) {
      const idx = parseInt(ctxTarget.dataset.idx);
      const sectors = loadSectors();
      if (sectors[idx]) {
        sectors[idx].market = market;
        sectors[idx].sector = sector;
        sectors[idx].product = product;
        sectors[idx].model = model;
        sectors[idx].name = sector;
        saveSectors(sectors);
        renderSectorButtons();
      }
    }
    hideCtxMenu();
    resetForNewAnalysis();
    applySectorConfig({ sector, product, market, model });
    goToPhase(1);
  });
  document.getElementById('ctx-delete')?.addEventListener('click', () => {
    if (!ctxTarget) return;
    const idx = parseInt(ctxTarget.dataset.idx);
    const sectors = loadSectors();
    sectors.splice(idx, 1);
    saveSectors(sectors);
    renderSectorButtons();
    hideCtxMenu();
  });
  document.addEventListener('click', e => {
    const menu = document.getElementById('ctx-menu');
    if (menu && !menu.contains(e.target)) hideCtxMenu();
  });
}

/* ── 初始化 ──────────────────────────────── */
export function initWizard() {
  renderSectorButtons();
  initCtxMenu();
  initSidebar();

  setP2SelectionCallback((selectedTickers) => {
    if (state.phase3) state.p3NeedsUpdate = true;
    updateSidebarStatus();
  });

  // 自定义开始
  document.getElementById('wiz-start-custom')?.addEventListener('click', () => {
    const sector = document.getElementById('wiz-sector')?.value?.trim();
    const product = document.getElementById('wiz-product')?.value?.trim();
    if (!sector || !product) { alert('请输入产业方向和终端产品'); return; }
    const market = document.getElementById('wiz-market')?.value || 'us_stock';
    resetForNewAnalysis();
    applySectorConfig({ sector, product, market });
    goToPhase(1);
  });

  // 全自动模式
  document.getElementById('wiz-auto-start')?.addEventListener('click', () => {
    const sectors = loadSectors();
    if (!sectors.length || !sectors[0].sector || !sectors[0].product) {
      alert('请先配置至少一个赛道'); return;
    }
    const s = sectors[0];
    resetForNewAnalysis();
    state.autoMode = true;
    applySectorConfig(s);
    logMsg('全自动模式启动 — 将自动完成全部阶段', 'info');
    goToPhase(1);
    runPhase1(s.sector, s.product);
  });

  // Phase 导航点击
  document.querySelectorAll('.wiz-step').forEach(step => {
    step.addEventListener('click', () => {
      const p = parseInt(step.dataset.phase);
      if (step.classList.contains('completed') || step.classList.contains('active')) {
        goToPhase(p);
      }
    });
  });

  // Phase 1 四态按钮
  document.getElementById('p1-tristate')?.addEventListener('click', () => {
    if (state.p1TriState === 'start' || state.p1TriState === 'restart' || state.p1TriState === 'resume') {
      if (state.config.sector && state.config.product) {
        runPhase1(state.config.sector, state.config.product);
      }
    } else if (state.p1TriState === 'pause') {
      if (Date.now() - _p1LaunchTime < LAUNCH_GUARD_MS) return;
      if (_p1Abort) { try { _p1Abort.abort(); } catch {} _p1Abort = null; }
      _stopP1Timer();
      state.running = false;
      logMsg('分析已暂停');
      hideP1Overlay();
      setTriState('p1-tristate', 'p1TriState', 'resume');
    }
  });

  // Phase 1 下一步
  document.getElementById('wiz-p1-next')?.addEventListener('click', () => goToPhase(2));

  // Phase 2 按钮
  document.getElementById('wiz-p2-next')?.addEventListener('click', () => goToPhase(3));
  document.getElementById('wiz-p2-back')?.addEventListener('click', () => goToPhase(1));
  // Phase 2 四态按钮
  document.getElementById('p2-tristate')?.addEventListener('click', () => {
    if (state.p2TriState === 'start' || state.p2TriState === 'restart' || state.p2TriState === 'resume') {
      if (state.analysisId) {
        runPhase2();
      }
    } else if (state.p2TriState === 'pause') {
      if (Date.now() - _p2LaunchTime < LAUNCH_GUARD_MS) return;
      if (_p2Abort) { try { _p2Abort.abort(); } catch {} _p2Abort = null; }
      _stopP2Timer();
      state.running = false;
      logMsg('筛选已暂停');
      setTriState('p2-tristate', 'p2TriState', 'resume');
    }
  });

  // Phase 2 层级数量变动
  ['wiz-l1-topn', 'wiz-l2-topn', 'wiz-l3-topn'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', updateP2Total);
  });

  // Phase 3 三态按钮
  document.getElementById('p3-tristate')?.addEventListener('click', () => {
    if (state.p3TriState === 'start' || state.p3TriState === 'restart') {
      if (state.phase2) {
        const slider = document.getElementById('wiz-p3-weight-slider');
        const wQ = (slider?.value || 40) / 100;
        runPhase3(wQ, 1 - wQ);
      }
    }
  });

  // Phase 3 下一步
  document.getElementById('wiz-p3-next')?.addEventListener('click', async () => {
    goToPhase(4);
    const configured = await fetchConfiguredProviders();
    loadCvModels(configured.length > 0 ? configured : undefined);
  });
  document.getElementById('wiz-p3-back')?.addEventListener('click', () => goToPhase(2));

  // 权重滑块（在 Phase 2 中）— 仅更新 Phase 2 表格
  const slider = document.getElementById('wiz-weight-slider');
  if (slider) {
    const update = () => {
      const wQ = slider.value / 100;
      const wA = 1 - wQ;
      const label = document.getElementById('wiz-weight-values');
      if (label) label.textContent = `${Math.round(wQ * 100)}% / ${Math.round(wA * 100)}%`;
      if (state.phase2) {
        recalcP2Final(state.phase2.scorecards, wQ, wA);
        renderPhase2Table(state.phase2.scorecards, state.failedTickers);
      }
    };
    slider.addEventListener('input', update);
  }

  // Phase 3 权重滑块 — 仅更新 label
  const p3Slider = document.getElementById('wiz-p3-weight-slider');
  if (p3Slider) {
    p3Slider.addEventListener('input', () => {
      const wQ = p3Slider.value / 100;
      const lbl = document.getElementById('wiz-p3-weight-values');
      if (lbl) lbl.textContent = `${Math.round(wQ * 100)}% / ${Math.round((1 - wQ) * 100)}%`;
    });
  }

  // Phase 3 图表 AI 解读 — 事件委托
  document.querySelectorAll('.ai-interp-trigger').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleAiInterp(btn.dataset.chartType);
    });
  });
  document.querySelectorAll('.ai-collapse-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const panel = document.getElementById(`ai-expand-${btn.dataset.chartType}`);
      if (panel) panel.style.display = 'none';
    });
  });
  document.querySelectorAll('.p3-chart-card .ai-regen-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      fetchAiInterp(btn.dataset.chartType, true);
    });
  });

  // AI 横向对比报告
  document.getElementById('wiz-gen-report')?.addEventListener('click', generateAiReport);

  // 抽屉关闭
  document.getElementById('drawer-close')?.addEventListener('click', closeDrawer);
  document.getElementById('wiz-drawer')?.addEventListener('click', e => {
    if (e.target.id === 'wiz-drawer') closeDrawer();
  });

  // Phase 4 按钮
  document.getElementById('wiz-p4-run')?.addEventListener('click', () => runPhase4());
  document.getElementById('wiz-p4-back')?.addEventListener('click', () => goToPhase(3));
  document.getElementById('wiz-p4-save')?.addEventListener('click', () => {
    alert('分析结果已自动保存');
    goToPhase(0);
  });

  // 圆桌会议按钮
  document.getElementById('btn-start-meeting')?.addEventListener('click', () => startMeeting());
  document.getElementById('btn-preflight')?.addEventListener('click', () => runPreflight());
  document.getElementById('btn-export-meeting')?.addEventListener('click', () => exportMeeting());

  // 加载历史
  loadWizardHistory();

  // 日志面板控制
  document.getElementById('wiz-log-clear')?.addEventListener('click', clearLog);
  document.getElementById('wiz-log-toggle')?.addEventListener('click', () => {
    const panel = document.getElementById('wiz-log-panel');
    const btn = document.getElementById('wiz-log-toggle');
    if (!panel) return;
    panel.classList.toggle('collapsed');
    btn.textContent = panel.classList.contains('collapsed') ? '▲' : '▼';
  });
  const sideLogBtn = document.getElementById('sidebar-log-btn');
  if (sideLogBtn) sideLogBtn.onclick = toggleLogPanel;

  // 日志面板拖拽
  const logHeader = document.getElementById('wiz-log-header');
  if (logHeader) {
    let dragging = false, startX = 0, startY = 0, startLeft = 0, startTop = 0;
    logHeader.addEventListener('mousedown', e => {
      if (e.target.closest('.wiz-log-btn')) return;
      dragging = true;
      const panel = document.getElementById('wiz-log-panel');
      const rect = panel.getBoundingClientRect();
      startX = e.clientX; startY = e.clientY;
      startLeft = rect.left; startTop = rect.top;
      panel.style.bottom = 'auto';
      panel.style.right = 'auto';
      panel.style.left = startLeft + 'px';
      panel.style.top = startTop + 'px';
      logHeader.style.cursor = 'grabbing';
      e.preventDefault();
    });
    document.addEventListener('mousemove', e => {
      if (!dragging) return;
      const panel = document.getElementById('wiz-log-panel');
      panel.style.left = (startLeft + e.clientX - startX) + 'px';
      panel.style.top = (startTop + e.clientY - startY) + 'px';
    });
    document.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      logHeader.style.cursor = '';
    });
  }

  // 热点扫描 Modal
  document.getElementById('btn-hot-scan')?.addEventListener('click', openHotModal);
  document.getElementById('hot-modal-close')?.addEventListener('click', closeHotModal);
  document.getElementById('hot-modal-refresh')?.addEventListener('click', () => fetchHotScan(true));
  document.getElementById('hot-modal-apply')?.addEventListener('click', applyHotScanSelections);
  document.getElementById('hot-modal')?.addEventListener('click', e => { if (e.target.id === 'hot-modal') closeHotModal(); });

  // 全部历史记录
  document.getElementById('wiz-all-history')?.addEventListener('click', e => {
    e.preventDefault();
    showPage('wizard-history-all');
    loadAllHistory();
  });
  document.getElementById('wiz-history-back')?.addEventListener('click', () => {
    showPage('wizard-start');
  });

  // AI模型设置页面
  initWizardSettings();
}

/* ── 模型选择器填充（旧「AI 模型设置」页面已移除，统一在顶部「AI 配置」中心配置；
 *    此处仅保留筛选流程所需的主模型/CV 模型下拉填充）── */
async function initWizardSettings() {
  await refreshModelSelectors();
  onProvidersChange(refreshModelSelectors);
}

/* ── 历史记录 ─────────────────────────────── */
function _fmtHistDate(a) {
  const ts = a.updated_at || a.created_at || '';
  if (!ts) return '-';
  const date = ts.slice(0, 10);
  const time = ts.slice(11, 16);
  return time ? `${date} ${time}` : date;
}

async function loadWizardHistory() {
  try {
    const resp = await fetch('/api/history');
    const data = await resp.json();
    const list = document.getElementById('wizard-history-list');
    if (!list) return;

    const analyses = (data.analyses || []).slice(0, 8);
    if (analyses.length === 0) {
      list.innerHTML = '<tr><td colspan="6" class="empty-text">暂无历史记录</td></tr>';
      return;
    }

    list.innerHTML = analyses.map(a => {
      return `
      <tr data-id="${a.id}" class="company-row-clickable">
        <td>${buildAnalysisTag(a)}</td>
        <td>${a.max_market_cap_yi ? '≤' + a.max_market_cap_yi + '亿' : '-'}</td>
        <td>${a.max_depth || '-'}层</td>
        <td>${a.supplier_count || 0}</td>
        <td>${_fmtHistDate(a)}</td>
      </tr>`;
    }).join('');

    list.querySelectorAll('tr[data-id]').forEach(row => {
      row.addEventListener('click', async () => {
        const id = row.dataset.id;
        const sector = row.querySelector('.at-sector')?.textContent || '';
        if (await showConfirm(`是否载入该分析数据？\n\n赛道: ${sector}`)) {
          loadWizardAnalysis(id);
        }
      });
    });
  } catch (e) {
    console.warn('加载历史失败', e);
  }
}

/* ── 载入历史分析到 Wizard ─────────────────── */
async function loadWizardAnalysis(analysisId) {
  try {
    logMsg('正在载入历史分析...');
    const resp = await fetch(`/api/history/${analysisId}/restore`, { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    resetForNewAnalysis();

    state.analysisId = data.analysis_id;
    if (data.seq_no) state.seqNo = data.seq_no;
    state.config = {
      sector: data.sector,
      product: data.end_product,
      market: data.market,
      created_at: data.created_at,
      completed_phases: data.completed_phases || 0,
      run_count: data.run_count || 0,
    };
    state.phase1 = null;
    state.phase2 = null;
    state.phase3 = null;
    state.phase4 = null;

    const p1 = data.phases?.['1'];
    if (p1) {
      state.phase1 = { ...p1, analysis_id: data.analysis_id };
      goToPhase(1);
      renderPhase1(state.phase1);
      _updateAnalysisTags();

      // ── 恢复模型选择器 ──
      const p1Model = document.getElementById('wiz-p1-model');
      const mainModel = document.getElementById('wiz-main-model');
      const autoModel = document.getElementById('wiz-auto-model');
      if (data.provider && data.model) {
        const modelVal = `${data.provider}::${data.model}`;
        [p1Model, mainModel, autoModel].forEach(sel => {
          if (!sel) return;
          if (!Array.from(sel.options).some(o => o.value === modelVal)) {
            const opt = document.createElement('option');
            opt.value = modelVal;
            opt.textContent = `${data.provider}/${data.model}`;
            sel.appendChild(opt);
          }
          sel.value = modelVal;
        });
      }

      // ── 恢复市场 ──
      const p1Market = document.getElementById('wiz-p1-market');
      const p0Market = document.getElementById('wiz-market');
      if (p1Market) p1Market.value = data.market || 'us_stock';
      if (p0Market) p0Market.value = data.market || 'us_stock';

      // ── 恢复 Phase 0 表单 ──
      const sectorEl = document.getElementById('wiz-sector');
      const productEl = document.getElementById('wiz-product');
      if (sectorEl) sectorEl.value = data.sector || '';
      if (productEl) productEl.value = data.end_product || '';

      // ── 恢复深度、TopN、市值上限 ──
      const depthEl = document.getElementById('wiz-depth');
      if (depthEl && data.max_depth) depthEl.value = String(data.max_depth);
      const topnEl = document.getElementById('wiz-topn');
      if (topnEl && data.top_n) topnEl.value = String(data.top_n);
      const maxCapEl = document.getElementById('wiz-max-cap');
      if (maxCapEl && data.max_market_cap_yi != null) maxCapEl.value = String(data.max_market_cap_yi);

      setTriState('p1-tristate', 'p1TriState', 'restart');
      const nextBtn = document.getElementById('wiz-p1-next');
      if (nextBtn) { nextBtn.disabled = false; nextBtn.style.display = ''; }

      logMsg(`Phase 1 已载入 — ${data.sector} / ${data.end_product}`, 'done');
    }

    const p2 = data.phases?.['2'];
    if (p2 && p2.scorecards?.length) {
      state.phase2 = p2;
      state.failedTickers = p2.failed_tickers || [];
      renderPhase2Table(p2.scorecards, state.failedTickers);
      showP2WeightCtrl();

      const statsEl = document.getElementById('wiz-p2-stats');
      const stats = p2.stats || {};
      if (statsEl) {
        statsEl.textContent = `共搜索 ${stats.total_searched || '?'} 家，筛选后 ${stats.after_filter || p2.scorecards.length} 家`;
      }

      const nextBtn2 = document.getElementById('wiz-p2-next');
      if (nextBtn2) { nextBtn2.disabled = false; nextBtn2.style.display = ''; }

      logMsg(`Phase 2 已载入 — ${p2.scorecards.length} 家入围`, 'done');
      setTriState('p2-tristate', 'p2TriState', 'restart');
    }

    // ── Phase 3 恢复 ──
    const p3 = data.phases?.['3'];
    if (p3?.ranked_results?.length && state.phase2?.scorecards) {
      const wQ = p3.scoring_config?.quality_weight ?? 0.4;
      const wA = p3.scoring_config?.alpha_weight ?? 0.6;
      const topN = p3.scoring_config?.top_n ?? 5;
      const allRanked = recalcPhase3(state.phase2.scorecards, wQ, wA);
      const ranked = allRanked.slice(0, topN);
      state.phase3 = { ranked_results: ranked, scoring_config: p3.scoring_config };
      renderPhase3Table(ranked, openDrawer);
      renderScatterPlot(ranked);
      renderRadarChart(ranked);
      renderBarCompare(ranked);
      renderAlphaStack(ranked);
      // 恢复 Phase 2 滑块
      const slider = document.getElementById('wiz-weight-slider');
      if (slider) {
        slider.value = Math.round(wQ * 100);
        const label = document.getElementById('wiz-weight-values');
        if (label) label.textContent = `${Math.round(wQ * 100)}% / ${Math.round((1 - wQ) * 100)}%`;
      }
      // 恢复 Phase 3 独立滑块
      const p3Slider = document.getElementById('wiz-p3-weight-slider');
      if (p3Slider) {
        p3Slider.value = Math.round(wQ * 100);
        const p3Label = document.getElementById('wiz-p3-weight-values');
        if (p3Label) p3Label.textContent = `${Math.round(wQ * 100)}% / ${Math.round((1 - wQ) * 100)}%`;
      }
      // 恢复 top-N
      const topnInput = document.getElementById('wiz-p3-topn');
      if (topnInput) topnInput.value = topN;

      const reportCard = document.getElementById('wiz-ai-report');
      if (reportCard) reportCard.style.display = '';

      setTriState('p3-tristate', 'p3TriState', 'restart');
      const nextBtn3 = document.getElementById('wiz-p3-next');
      if (nextBtn3) { nextBtn3.disabled = false; nextBtn3.style.display = ''; }

      logMsg(`Phase 3 已载入 — 排名 ${p3.ranked_results.length} 家`, 'done');
    }

    // ── Phase 4 恢复 ──
    const p4 = data.phases?.['4'];
    if (p4?.validations?.length) {
      state.phase4 = p4;
      renderPhase4Table(p4.validations, p4.recommendations || [], state.phase3?.ranked_results || []);
      enableMeetingButton();
      logMsg(`Phase 4 已载入 — 验证 ${p4.validations.length} 家`, 'done');
    }

    // ── 圆桌会议恢复 ──
    if (data.meeting_result) {
      restoreMeeting(data.meeting_result);
      logMsg('圆桌会议结果已载入', 'done');
    }

    // ── 信号灯状态恢复 ──
    const ps = data._phase_status || {};
    state.p2NeedsUpdate = ps.p2 === 'yellow';
    state.p3NeedsUpdate = ps.p3 === 'yellow';
    state.p4NeedsUpdate = ps.p4 === 'yellow';

    // ── AI 评点恢复（重置 + 盖 analysis_id 防串台）──
    ['scatter', 'radar', 'bar', 'stack'].forEach(k => updateTriggerBtn(k, false));
    state.aiReports = {};
    if (data.ai_reports) {
      for (const [key, val] of Object.entries(data.ai_reports)) {
        state.aiReports[key] = { ...val, analysis_id: state.analysisId };
      }
      for (const key of ['scatter', 'radar', 'bar', 'stack']) {
        if (state.aiReports[key]?.text) {
          updateTriggerBtn(key, true);
        }
      }
      // 横向对比报告：有持久化则直接回显（后端已存，避免用户以为没存、每次重新生成）
      const cmp = state.aiReports['comparison'];
      if (cmp?.text) {
        const rb = document.getElementById('wiz-report-body');
        if (rb) rb.innerHTML = '<div class="md-body">' + formatMarkdown(cmp.text) + '</div>';
        const gb = document.getElementById('wiz-gen-report');
        if (gb) gb.textContent = '重新生成';
        const rc = document.getElementById('wiz-ai-report');
        if (rc) rc.style.display = '';
      }
    }

    updateSidebarStatus();
    updateNav();
    // 切换到该正向记录后，刷新为其专属的反向分析列表（每条记录独立）
    if (window.reloadReverseList) window.reloadReverseList();
    logMsg('历史分析载入完成', 'done');
  } catch (err) {
    logMsg(`载入失败: ${err.message}`, 'error');
    alert(`载入分析失败: ${err.message}`);
  }
}

/* ── 全部历史记录 ─────────────────────────── */
async function loadAllHistory() {
  const list = document.getElementById('wiz-all-history-list');
  if (!list) return;
  list.innerHTML = '<tr><td colspan="9" class="empty-text">正在加载...</td></tr>';

  try {
    const resp = await fetch('/api/history');
    const data = await resp.json();
    const analyses = data.analyses || [];

    if (analyses.length === 0) {
      list.innerHTML = '<tr><td colspan="6" class="empty-text">暂无历史记录</td></tr>';
      return;
    }

    list.innerHTML = analyses.map(a => {
      return `
      <tr data-id="${a.id}">
        <td>${buildAnalysisTag(a)}</td>
        <td>${a.max_market_cap_yi ? '≤' + a.max_market_cap_yi + '亿' : '-'}</td>
        <td>${a.max_depth || '-'}层</td>
        <td>${a.supplier_count || 0}</td>
        <td>${_fmtHistDate(a)}</td>
        <td>
          <button class="btn btn-primary btn-sm hist-load-btn" data-id="${a.id}">载入</button>
          <button class="btn btn-danger btn-sm hist-del-btn" data-id="${a.id}">删除</button>
        </td>
      </tr>`;
    }).join('');

    list.querySelectorAll('.hist-load-btn').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        const id = btn.dataset.id;
        const row = btn.closest('tr');
        const sector = row.querySelector('.at-sector')?.textContent || '';
        if (await showConfirm(`是否载入该分析数据？\n\n赛道: ${sector}`)) {
          loadWizardAnalysis(id);
        }
      });
    });

    list.querySelectorAll('.hist-del-btn').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        if (!await showConfirm('确认删除该分析记录？此操作不可撤销。', { danger: true })) return;
        try {
          const resp = await fetch(`/api/history/${btn.dataset.id}`, { method: 'DELETE' });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          loadAllHistory();
          loadWizardHistory();
        } catch (err) {
          alert(`删除失败: ${err.message}`);
        }
      });
    });
  } catch (e) {
    list.innerHTML = `<tr><td colspan="6" class="empty-text">加载失败: ${e.message}</td></tr>`;
  }
}

/* ── CV 模型列表 ──────────────────────────── */
/* ── 动态模型选择器 ─────────────────────── */
const DEFAULT_MODELS = {
  openai: 'gpt-4o',
  anthropic: 'claude-sonnet-4-6',
  deepseek: 'deepseek-chat',
  google: 'gemini-2.5-flash',
  qwen: 'qwen-plus',
  glm: 'glm-4-plus',
  minimax: 'MiniMax-Text-01',
  openrouter: 'deepseek/deepseek-chat',
  siliconflow: 'deepseek-ai/DeepSeek-V3',
  agnes: 'agnes-2.0-flash',
  kimi: 'moonshot-v1-8k',
};

async function fetchConfiguredProviders() {
  try {
    const resp = await fetch('/api/ai-config/providers');
    if (resp.ok) {
      const data = await resp.json();
      return data.providers || [];
    }
  } catch (e) { /* fallback */ }
  const provs = getProviders();
  return provs.filter(p => p.configured && !p.is_url);
}

async function refreshModelSelectors(fallbackProviderList) {
  let configured;
  try {
    const resp = await fetch('/api/ai-config/providers');
    if (resp.ok) {
      const data = await resp.json();
      configured = data.providers || [];
    }
  } catch (e) { /* fallback below */ }

  if (!configured || configured.length === 0) {
    configured = (fallbackProviderList || []).filter(p => p.configured && !p.is_url);
  }
  if (configured.length === 0) return;

  const options = configured.map(p => {
    const model = p.default_model || DEFAULT_MODELS[p.id] || '';
    const val = `${p.id}::${model}`;
    return `<option value="${val}">${_escapeHtml(p.name)}</option>`;
  }).join('');

  ['wiz-main-model', 'wiz-p1-model', 'wiz-auto-model', 'ctx-model'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = options;
    if (sel.querySelector(`option[value="${prev}"]`)) {
      sel.value = prev;
    }
  });

  loadCvModels(configured);
}

function _escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function loadCvModels(configuredProviders) {
  const container = document.getElementById('wiz-cv-models');
  if (!container) return;

  let models;
  if (configuredProviders && configuredProviders.length > 0) {
    models = configuredProviders.map(p => ({
      provider: p.id,
      model: p.default_model || DEFAULT_MODELS[p.id] || '',
      label: p.name,
    }));
  } else {
    models = [
      { provider: 'openai', model: 'gpt-4o', label: 'OpenAI' },
      { provider: 'anthropic', model: 'claude-sonnet-4-6', label: 'Anthropic' },
      { provider: 'deepseek', model: 'deepseek-chat', label: 'DeepSeek' },
      { provider: 'google', model: 'gemini-2.5-flash', label: 'Google' },
      { provider: 'qwen', model: 'qwen-plus', label: 'Qwen' },
    ];
  }

  container.innerHTML = models.map(m => `
    <label class="cv-check">
      <input type="checkbox" value="${m.provider}::${m.model}" checked>
      <span>${m.label}${m.model ? ' (' + _escapeHtml(m.model) + ')' : ''}</span>
    </label>
  `).join('');
}

/* ── 热点扫描 Modal ─────────────────────────── */
let _hotScanCache = null;
let _hotScanTime = 0;

async function fetchHotScan(force = false) {
  if (!force && _hotScanCache && (Date.now() - _hotScanTime < 30 * 60 * 1000)) {
    renderHotModal(_hotScanCache);
    return;
  }
  const body = document.getElementById('hot-modal-body');
  body.innerHTML = '<div class="hot-modal-loading"><div class="spinner"></div><p>正在扫描热门赛道...</p></div>';
  const { provider, model } = getMainModel();
  try {
    const resp = await fetch('/api/hot-scan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model, top_n: 8 }),
    });
    const data = await resp.json();
    _hotScanCache = data.recommendations || [];
    _hotScanTime = Date.now();
    renderHotModal(_hotScanCache);
    populateDataLists(_hotScanCache);
  } catch (e) {
    body.innerHTML = `<p class="empty-msg">扫描失败: ${e.message}</p>`;
  }
}

function renderHotModal(recs) {
  const body = document.getElementById('hot-modal-body');
  if (!recs || recs.length === 0) {
    body.innerHTML = '<p class="empty-msg">暂无推荐赛道</p>';
    return;
  }
  body.innerHTML = recs.map((r, i) => `
    <label class="hot-scan-item">
      <input type="checkbox" data-idx="${i}" data-sector="${r.sector}" data-product="${r.end_product}" data-market="${r.market || 'a_stock'}">
      <span class="hot-scan-sector">${r.sector}</span>
      <span class="hot-scan-product">${r.end_product}</span>
      <span class="hot-scan-reason">${r.reason || ''}</span>
    </label>
  `).join('');
}

function populateDataLists(recs) {
  const dlS = document.getElementById('dl-sectors');
  const dlP = document.getElementById('dl-products');
  if (dlS) dlS.innerHTML = recs.map(r => `<option value="${r.sector}">`).join('');
  if (dlP) dlP.innerHTML = recs.map(r => `<option value="${r.end_product}">`).join('');
}

function openHotModal() {
  document.getElementById('hot-modal').style.display = '';
  fetchHotScan();
}

function closeHotModal() {
  document.getElementById('hot-modal').style.display = 'none';
}

function applyHotScanSelections() {
  const body = document.getElementById('hot-modal-body');
  if (!body) return;
  const checked = body.querySelectorAll('input[type="checkbox"]:checked');
  if (checked.length === 0) { alert('请至少勾选一个推荐赛道'); return; }

  const sectors = loadSectors();
  checked.forEach(cb => {
    const exists = sectors.some(s => s.sector === cb.dataset.sector);
    if (!exists && sectors.length < 8) {
      sectors.push({
        name: cb.dataset.sector,
        sector: cb.dataset.sector,
        product: cb.dataset.product,
        market: cb.dataset.market || 'a_stock',
      });
    }
  });

  saveSectors(sectors);
  renderSectorButtons();
  closeHotModal();
  logMsg(`已加入 ${checked.length} 个热点赛道`, 'done');
}

/* ── 数据补拉：重新获取失败的外部数据 ── */
function mergeRefetchedData(scorecards, result) {
  if (!scorecards || !result) return;
  const fin = result.financial || {};
  const sm = result.smart_money || {};
  for (const sc of scorecards) {
    const ticker = sc.supplier?.ticker;
    if (!ticker) continue;
    if (fin[ticker]) sc.financial_snapshot = fin[ticker];
    if (sm[ticker]) sc.smart_money = sm[ticker];
  }
}

document.addEventListener('click', async (e) => {
  const btn = e.target.closest('#btn-refetch-failed');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = '获取中...';
  try {
    const market = state.phase2?.config?.market || 'us_stock';
    const resp = await fetch('/api/refetch-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tickers: state.failedTickers, market }),
    });
    const result = await resp.json();
    mergeRefetchedData(state.phase2.scorecards, result);
    state.failedTickers = result.still_failed || [];
    renderPhase2Table(state.phase2.scorecards, state.failedTickers);
    if (state.failedTickers.length === 0) {
      logMsg('数据补拉成功，所有缺失数据已恢复', 'done');
    } else {
      logMsg(`数据补拉完成，仍有 ${state.failedTickers.length} 家获取失败`, 'warn');
    }
  } catch (err) {
    logMsg(`数据补拉失败: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '重新获取';
  }
});

export { state as wizardState, goToPhase, openDrawer, getScoreColor, scoreNeedsDarkText };
