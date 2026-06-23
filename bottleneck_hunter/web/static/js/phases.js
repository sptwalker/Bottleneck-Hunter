/**
 * phases.js — Wizard 4-Phase 控制器 v2
 * 支持三态按钮、主分析模型、层级筛选、AI 解读弹窗、抽屉、赛道管理。
 */

import { renderPhase1, renderPhase2Table, renderPhase3Table, renderPhase4Table, renderScatterPlot, renderRadarChart, renderBarCompare, renderAlphaStack, setP2SelectionCallback, getP2SelectedTickers, resetP2Selection } from './phase-views.js';
import { fetchAndRender, testAll, saveAll, onProvidersChange, getProviders } from './settings.js';

/* ── Wizard 状态 ───────────────────────────── */
const state = {
  currentPhase: 0,
  currentPage: null,
  analysisId: null,
  seqNo: null,
  running: false,
  config: {},
  phase1: null,
  phase2: null,
  phase3: null,
  phase4: null,
  p1TriState: 'start',   // start | pause | resume | restart
  p2TriState: 'start',   // start | pause | resume | restart
  p3TriState: 'start',
  manualPicks: [],
  p1Error: false,
  p2Error: false,
  p4Error: false,
  p2NeedsUpdate: false,
  p3NeedsUpdate: false,
  p4NeedsUpdate: false,
  aiReports: {},
  autoMode: false,
};
window.wizardState = state;

/* ── SSE 流读取工具（含断连重试） ─────────────── */
async function readSSEStream(url, body, { onEvent, onTick, onError, label = 'sse', maxRetries = 3 } = {}) {
  const delays = [1000, 3000, 5000];
  let attempt = 0;
  let receivedAny = false;

  while (attempt <= maxRetries) {
    try {
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let sseEvent = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        receivedAny = true;
        attempt = 0;
        if (onTick) onTick();
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('event:')) {
            sseEvent = line.slice(6).trim();
          } else if (line.startsWith('data:')) {
            try {
              const data = JSON.parse(line.slice(5).trim());
              data._sseEvent = sseEvent;
              if (onEvent) onEvent(data);
            } catch (e) { console.warn(`[${label}] JSON解析失败:`, e.message, line.slice(0, 200)); }
            sseEvent = '';
          } else if (line.trim() === '') {
            sseEvent = '';
          }
        }
      }
      if (buffer.trim().startsWith('data:')) {
        try {
          const data = JSON.parse(buffer.trim().slice(5).trim());
          if (onEvent) onEvent(data);
        } catch (e) { console.warn(`[${label}] 尾部JSON解析失败:`, e.message); }
      }
      return;
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      attempt++;
      if (attempt > maxRetries) {
        if (onError) onError(err);
        return;
      }
      const delay = delays[attempt - 1] || 5000;
      logMsg(`[${label}] 连接中断，${delay / 1000}s 后重试 (${attempt}/${maxRetries})...`, 'warn');
      await new Promise(r => setTimeout(r, delay));

      if (state.analysisId) {
        try {
          const statusResp = await fetch(`/api/history/${state.analysisId}/phase-status`);
          if (statusResp.ok) {
            const statusData = await statusResp.json();
            if (statusData && statusData.completed) {
              logMsg(`[${label}] 后端已完成，使用缓存数据`, 'info');
              return;
            }
          }
        } catch (_) {}
      }
    }
  }
}

const SCORE_COLORS = {
  10: '#FFD700', 9: '#166534', 8: '#16a34a', 7: '#4ade80',
  6: '#f97316', 5: '#f59e0b', 4: '#eab308', 3: '#b45309',
  2: '#9ca3af', 1: '#991b1b',
};

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
  if (wrap) wrap.style.display = '';
  if (bar) bar.style.width = '0%';
  if (text) text.textContent = '';
}

function _updateP1Progress(pct, label) {
  const bar = document.getElementById('p1-progress-fill');
  const text = document.getElementById('p1-progress-text');
  if (bar) bar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  if (text) text.textContent = label || '';
}

function _startP1Timer() {
  _stopP1Timer();
  _p1StartTime = Date.now();
  const el = document.getElementById('p1-timer');
  if (el) el.style.display = '';
  _updateP1Timer();
  _p1TimerInterval = setInterval(_updateP1Timer, 1000);
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
  _p2TimerInterval = setInterval(_updateP2Timer, 1000);
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
  _p4TimerInterval = setInterval(_updateP4Timer, 1000);
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

/* ── SSE reader 引用（用于暂停） ─────────────── */
let _p1Reader = null;
let _p2Reader = null;

/* ── Phase 2 进度跟踪 ──────────────────────────── */
const P2_STEPS = ['supplier_search', 'financial_fetch', 'supplier_eval', 'catalyst'];
let _p2StepIndex = -1;
let _p2EvalTotal = 0;
let _p2EvalDone = 0;

/* ── 新分析重置 ──────────────────────────── */
function resetForNewAnalysis() {
  if (_p1Reader) { try { _p1Reader.cancel(); } catch {} _p1Reader = null; }
  if (_p2Reader) { try { _p2Reader.cancel(); } catch {} _p2Reader = null; }
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
    const el = document.getElementById(id); if (el) el.innerHTML = '';
  });
  const bnStats = document.getElementById('wiz-bn-stats');
  if (bnStats) { bnStats.innerHTML = ''; bnStats.style.display = 'none'; }
  hideP1Info(); hideP1Overlay(); _resetP1Progress(); _updateSeqBadge();
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
function _autoChainNext() {
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
    const provs = getProviders();
    const configured = provs.filter(p => p.configured && !p.is_url);
    loadCvModels(configured.length > 0 ? configured : undefined);
    setTimeout(() => runPhase4(), 300);
  } else if (state.phase4) {
    logMsg('全自动模式: 全部阶段完成!', 'done');
    state.autoMode = false;
  }
}

/* ── 日志面板 ─────────────────────────────── */
function logMsg(text, level = 'info') {
  const body = document.getElementById('wiz-log-body');
  const panel = document.getElementById('wiz-log-panel');
  if (!body || !panel) return;
  if (panel.style.display !== 'none') {
    panel.classList.remove('collapsed');
    const toggleBtn = document.getElementById('wiz-log-toggle');
    if (toggleBtn) toggleBtn.textContent = '▼';
  }
  const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const cls = level === 'error' ? 'log-error' : level === 'done' ? 'log-done' : level === 'warn' ? 'log-warn' : '';
  const line = document.createElement('div');
  line.className = `wiz-log-line ${cls}`;
  line.innerHTML = `<span class="log-ts">[${ts}]</span> ${text}`;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
  // 更新侧边栏按钮未读指示
  const logBtn = document.getElementById('sidebar-log-btn');
  if (logBtn && panel.style.display === 'none') logBtn.classList.add('has-unread');
}

function clearLog() {
  const body = document.getElementById('wiz-log-body');
  if (body) body.innerHTML = '';
  const panel = document.getElementById('wiz-log-panel');
  if (panel) panel.style.display = 'none';
}

/* ── 评分颜色 ─────────────────────────────── */
export function getScoreColor(score) {
  const s = Math.round(Math.max(1, Math.min(10, score)));
  return SCORE_COLORS[s] || '#9ca3af';
}

export function scoreNeedsDarkText(score) {
  return [10, 7, 4].includes(Math.round(Math.max(1, Math.min(10, score))));
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
  nav.querySelectorAll('.wiz-step').forEach(step => {
    const p = parseInt(step.dataset.phase);
    step.classList.remove('completed', 'active', 'pending');
    if (p < state.currentPhase) {
      step.classList.add('completed');
      step.querySelector('.wiz-step-dot').textContent = '✓';
    } else if (p === state.currentPhase) {
      step.classList.add('active');
      step.querySelector('.wiz-step-dot').textContent = p || '✓';
    } else {
      step.classList.add('pending');
      step.querySelector('.wiz-step-dot').textContent = p;
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
    _updatePhaseTargets();
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

function _updatePhaseTargets() {
  const market = state.config.market === 'a_stock' ? 'A股' : '美股';
  const sector = state.config.sector || '-';
  const product = state.config.product || '-';
  const { provider } = getMainModel();
  const labels = { deepseek: 'DeepSeek', openai: 'OpenAI', anthropic: 'Claude', qwen: 'Qwen', google: 'Gemini', glm: 'GLM', kimi: 'Kimi' };
  const modelLabel = labels[provider] || provider;
  const html = `<span>${market}</span><span class="phase-target-sep">·</span><span>${sector}</span><span class="phase-target-sep">·</span><span>${product}</span><span class="phase-target-sep">·</span><span>${modelLabel}</span>`;
  ['p1-target', 'p2-target', 'p3-target', 'p4-target'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
  });
}

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

function showSettingsPage() {
  state.currentPage = 'settings';
  updateSidebarActive();
  showPage('wizard-settings');
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
      } else if (item.dataset.page === 'settings') {
        showSettingsPage();
      }
    });
  }
}

/* ── 解析主分析模型 ──────────────────────── */
function getMainModel() {
  const sel = document.getElementById('wiz-main-model') || document.getElementById('wiz-p1-model');
  const val = sel?.value || 'deepseek::deepseek-chat';
  const [provider, model] = val.split('::');
  return { provider, model };
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

/* ── 分析编号徽章 ───────────────────────────── */
function _updateSeqBadge() {
  const el = document.getElementById('analysis-seq-badge');
  if (!el) return;
  if (state.seqNo) {
    el.textContent = `#${state.seqNo}`;
    el.style.display = '';
  } else {
    el.style.display = 'none';
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
  goToPhase(1);

  const nextBtn = document.getElementById('wiz-p1-next');
  nextBtn.disabled = true;
  nextBtn.style.display = 'none';
  setTriState('p1-tristate', 'p1TriState', 'pause');
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

  logMsg(`参数: 深度=${depth}, TopN=${topN}, 市场=${market}, 模型=${provider}/${model || '默认'}`);

  const body = {
    sector, end_product: product,
    max_depth: depth, top_n: topN,
    max_market_cap_yi: maxCap,
    language: 'zh', provider, model, market,
  };

  try {
    await readSSEStream('/api/phase1', body, {
      label: 'phase1-sse',
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
  _p1Reader = null;
  state.running = false;
  if (!state.phase1 && state.p1TriState === 'pause') {
    setTriState('p1-tristate', 'p1TriState', 'restart');
  }
  hideP1Overlay();
  updateSidebarStatus();
}

function handlePhase1Event(data) {
  _updateP1Timer();
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
    state.phase1 = data;
    state.phase2 = null;
    state.phase3 = null;
    state.phase4 = null;

    _stopP1Timer();
    _updateP1Progress(100, '分析完成');

    const reportCount = (data.top_reports || []).length;
    const seqTag = state.seqNo ? `#${state.seqNo} ` : '';
    logMsg(`Phase 1 完成 — ${seqTag}瓶颈报告 ${reportCount} 条`, 'done');
    logMsg('本次分析数据存储成功', 'done');
    _updateSeqBadge();

    renderPhase1(data);

    const nextBtn = document.getElementById('wiz-p1-next');
    nextBtn.disabled = false;
    nextBtn.style.display = '';
    showP1Info('本次分析数据存储成功');
    setTimeout(() => hideP1Info(), 3000);
    hideP1Overlay();
    setTriState('p1-tristate', 'p1TriState', 'restart');
    updateSidebarStatus();
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
    market: document.getElementById('wiz-p1-market')?.value || 'us_stock',
    max_market_cap_yi: parseFloat(document.getElementById('wiz-max-cap')?.value || '200'),
    max_suppliers: 20,
    language: 'zh', provider, model,
  };

  try {
    await readSSEStream('/api/phase2', body, {
      label: 'phase2-sse',
      onTick: _updateP2Timer,
      onEvent: (data) => handlePhase2Event(data, progress),
      onError: (err) => {
        progress.innerHTML = `<div class="progress-msg progress-error">连接失败: ${err.message}</div>`;
        logMsg(`Phase 2 连接失败: ${err.message}`, 'error');
        _stopP2Timer();
        state.p2Error = true;
        state.autoMode = false;
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
  _p2Reader = null;
  state.running = false;
  if (!state.phase2 && state.p2TriState === 'pause') {
    setTriState('p2-tristate', 'p2TriState', 'restart');
  }
  updateSidebarStatus();
}

function handlePhase2Event(data, progress) {
  _updateP2Timer();
  if (data._sseEvent === 'error' && data.step === 'init' && data.message) {
    progress.innerHTML = `<div class="progress-msg progress-error">${data.message}</div>`;
    logMsg(`[phase2] ${data.message}`, 'error');
    _stopP2Timer();
    state.p2Error = true;
    state.autoMode = false;
    setTriState('p2-tristate', 'p2TriState', 'restart');
    updateSidebarStatus();
    return;
  }

  if (data.message && _p2IsDuplicate(data.message)) return;

  if (data.index !== undefined && data.step) {
    _p2StepIndex = data.index;
    const stepPct = (data.index / P2_STEPS.length) * 100;
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
    updateSidebarStatus();
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
  if (bar) bar.style.width = `${rounded}%`;
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
  }).catch(() => {});

  _savePhaseStatus();
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
}

function handlePhase4Event(data, progress) {
  _updateP4Timer();
  if (data.message) {
    progress.innerHTML = `<div class="progress-msg">${data.message}</div>`;
    logMsg(`[phase4] ${data.message}`);
  }

  if (data.validations || data.recommendations) {
    state.phase4 = data;
    renderPhase4Table(data.validations || [], data.recommendations || [], state.phase3?.ranked_results || []);
    const vCount = (data.validations || []).length;
    logMsg(`Phase 4 完成 — 验证 ${vCount} 家公司`, 'done');
    progress.innerHTML = '<div class="progress-msg progress-done">交叉验证完成</div>';
    enableMeetingButton();
    updateSidebarStatus();
    _savePhaseStatus();
    _autoChainNext();
  }
}

/* ── AI 投研圆桌会议 ──────────────────────── */

const MEETING_ROLES = [
  { id: 'growth', name: '成长型投资者', letter: '成', color: '#10a37f' },
  { id: 'value',  name: '价值型投资者', letter: '价', color: '#d97706' },
  { id: 'risk',   name: '风险分析师',   letter: '风', color: '#dc2626' },
  { id: 'chain',  name: '产业链专家',   letter: '链', color: '#6366f1' },
];

function buildMeetingSetup() {
  const setup = document.getElementById('meeting-setup');
  const grid = document.getElementById('meeting-role-grid');
  if (!setup || !grid) return;

  const modelCheckboxes = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  const models = Array.from(modelCheckboxes).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model, label: `${provider}/${model}` };
  });

  if (models.length === 0) {
    setup.style.display = 'none';
    return;
  }

  grid.innerHTML = '';
  const optionsHtml = models.map((m, i) => `<option value="${m.provider}::${m.model}">${m.label}</option>`).join('');

  MEETING_ROLES.forEach((role, idx) => {
    const row = document.createElement('div');
    row.className = 'meeting-role-row';
    const defaultIdx = idx % models.length;
    const defaultVal = `${models[defaultIdx].provider}::${models[defaultIdx].model}`;
    row.innerHTML = `
      <div class="meeting-role-avatar" style="background:${role.color}">${role.letter}</div>
      <div class="meeting-role-name">${role.name}</div>
      <select class="meeting-role-select" data-role="${role.id}">
        ${optionsHtml}
      </select>
      <span class="meeting-role-status" id="preflight-${role.id}"></span>
    `;
    const select = row.querySelector('select');
    if (select) select.value = defaultVal;
    grid.appendChild(row);
  });

  setup.style.display = 'block';
  const preflightBtn = document.getElementById('btn-preflight');
  if (preflightBtn) preflightBtn.disabled = false;
}

function getSelectedRoleAssignments() {
  const assignments = {};
  MEETING_ROLES.forEach(role => {
    const select = document.querySelector(`select[data-role="${role.id}"]`);
    if (select && select.value) {
      const [provider, model] = select.value.split('::');
      assignments[role.id] = { provider, model };
    }
  });
  return assignments;
}

async function runPreflight() {
  const btn = document.getElementById('btn-preflight');
  const statusEl = document.getElementById('preflight-status');
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  if (statusEl) statusEl.textContent = '';

  const assignments = getSelectedRoleAssignments();
  const seen = new Set();
  const models = [];
  Object.values(assignments).forEach(m => {
    const key = `${m.provider}::${m.model}`;
    if (!seen.has(key)) {
      seen.add(key);
      models.push(m);
    }
  });

  MEETING_ROLES.forEach(role => {
    const el = document.getElementById(`preflight-${role.id}`);
    if (el) { el.textContent = '⏳'; el.className = 'meeting-role-status'; }
  });

  try {
    const resp = await fetch('/api/meeting/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models }),
    });
    const data = await resp.json();
    const resultMap = {};
    for (const r of (data.results || [])) {
      resultMap[`${r.provider}::${r.model}`] = r.success;
    }

    let allOk = true;
    MEETING_ROLES.forEach(role => {
      const m = assignments[role.id];
      const key = m ? `${m.provider}::${m.model}` : '';
      const ok = resultMap[key] === true;
      if (!ok) allOk = false;
      const el = document.getElementById(`preflight-${role.id}`);
      if (el) {
        el.textContent = ok ? '✅' : '❌';
        el.className = `meeting-role-status ${ok ? 'preflight-ok' : 'preflight-fail'}`;
      }
    });

    if (allOk) {
      if (statusEl) statusEl.textContent = '全部通过';
      const meetBtn = document.getElementById('btn-start-meeting');
      if (meetBtn) meetBtn.disabled = false;
    } else {
      if (statusEl) statusEl.textContent = '部分模型不可用';
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = `测试失败: ${err.message}`;
  }
  if (btn) { btn.disabled = false; btn.textContent = '测试连通性'; }
}

function enableMeetingButton() {
  buildMeetingSetup();
  const btn = document.getElementById('btn-start-meeting');
  if (btn) {
    btn.textContent = '启动会议';
  }
}

async function startMeeting() {
  if (!state.analysisId) return;
  const btn = document.getElementById('btn-start-meeting');
  const status = document.getElementById('meeting-status');
  const transcript = document.getElementById('meeting-transcript');
  const messages = document.getElementById('meeting-messages');
  const resultDiv = document.getElementById('meeting-result');

  if (btn) { btn.disabled = true; btn.textContent = '会议进行中...'; }
  if (status) status.textContent = '进行中';
  if (transcript) transcript.style.display = 'block';
  if (messages) messages.innerHTML = '';
  if (resultDiv) { resultDiv.style.display = 'none'; resultDiv.innerHTML = ''; }

  document.getElementById('ai-meeting-card')?.classList.add('meeting-active');

  const modelCheckboxes = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  const validationModels = Array.from(modelCheckboxes).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model };
  });

  const roleAssignments = getSelectedRoleAssignments();

  const body = {
    analysis_id: state.analysisId,
    validation_models: validationModels,
    role_assignments: Object.keys(roleAssignments).length > 0 ? roleAssignments : null,
    language: 'zh',
  };

  logMsg('圆桌会议启动', 'info');

  try {
    await readSSEStream('/api/phase4/meeting', body, {
      label: 'meeting-sse',
      onEvent: (data) => handleMeetingEvent(data),
      onError: (err) => {
        logMsg(`圆桌会议连接失败: ${err.message}`, 'error');
        if (status) status.textContent = '失败';
      },
    });
  } catch (err) {
    logMsg(`圆桌会议连接失败: ${err.message}`, 'error');
    if (status) status.textContent = '失败';
  }

  if (btn) { btn.textContent = '重新开会'; btn.disabled = false; }
  document.getElementById('ai-meeting-card')?.classList.remove('meeting-active');
}

function handleMeetingEvent(data) {
  if (data.meeting_error || data.message && !data.role && !data.participants) {
    if (data.meeting_error) {
      const msg = data.message || data.meeting_error || '未知错误';
      logMsg(`[会议错误] ${msg}`, 'error');
      const status = document.getElementById('meeting-status');
      if (status) status.textContent = '失败';
      return;
    }
    if (data.message && !data.role) {
      logMsg(`[会议] ${data.message}`, 'info');
      const status = document.getElementById('meeting-status');
      if (status) status.textContent = data.message;
      return;
    }
  }

  if (data.participants !== undefined && data.company_count !== undefined) {
    logMsg(`会议开始 — ${data.company_count} 家企业, ${data.participants.length} 位参会者`);
    state.meetingParticipants = data.participants;
    return;
  }

  if (data.round_num !== undefined && data.round_name !== undefined && !data.content) {
    renderMeetingRoundDivider(data.round_num, data.round_name);
    logMsg(`第 ${data.round_num} 轮: ${data.round_name}`);
    return;
  }

  if (data.content !== undefined && data.role !== undefined) {
    renderMeetingBubble(data);
    return;
  }

  if (data.ranking) {
    logMsg(`Borda 排名出炉 — 第一: ${data.ranking[0]?.name || '?'}`, 'done');
    return;
  }

  if (data.result) {
    state.meetingResult = data.result;
    renderMeetingResult(data.result);
    const status = document.getElementById('meeting-status');
    if (status) status.textContent = '已完成';
    logMsg('圆桌会议完成', 'done');
    return;
  }
}

function renderMeetingRoundDivider(roundNum, roundName) {
  const container = document.getElementById('meeting-messages');
  if (!container) return;
  const div = document.createElement('div');
  div.className = 'meeting-round-divider';
  div.innerHTML = `<span>第 ${roundNum} 轮: ${roundName}</span>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function renderMeetingBubble(msg) {
  const container = document.getElementById('meeting-messages');
  if (!container) return;

  const bubble = document.createElement('div');
  bubble.className = 'meeting-bubble';

  const avatarClass = `av-${msg.role}`;
  const avatarLetter = msg.avatar_letter || msg.participant_name?.charAt(0) || '?';
  const color = msg.color || '#64748b';

  bubble.innerHTML = `
    <div class="meeting-avatar ${avatarClass}" style="background:${color}">${avatarLetter}</div>
    <div class="meeting-bubble-body">
      <div class="meeting-name">${msg.participant_name}${msg.model_name ? ` <span class="meeting-model">${msg.model_name}</span>` : ''}</div>
      <div class="meeting-msg">${msg.content.replace(/\n/g, '<br>')}</div>
    </div>
  `;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

function renderMeetingResult(result) {
  const div = document.getElementById('meeting-result');
  if (!div) return;
  div.style.display = 'block';

  const roleMap = {};
  MEETING_ROLES.forEach(r => { roleMap[r.id] = r; });
  function roleIcon(roleId, size = 18) {
    const r = roleMap[roleId];
    if (!r) return '';
    return `<span class="vote-icon" style="background:${r.color}" title="${r.name}">${r.letter}</span>`;
  }

  let html = '<h4>最终排名</h4>';
  html += '<table class="meeting-ranking-table"><thead><tr><th>排名</th><th>企业</th><th>Borda</th><th>支持</th><th>反对</th><th>理由</th></tr></thead><tbody>';
  for (const r of (result.final_ranking || [])) {
    const supIcons = (r.supporters || []).map(id => roleIcon(id)).join('');
    const oppIcons = (r.opposers || []).map(id => roleIcon(id)).join('');
    html += `<tr>
      <td><strong>${r.rank}</strong></td>
      <td>${r.name} (${r.ticker})</td>
      <td>${r.borda_points}</td>
      <td class="vote-cell">${supIcons || '<span class="vote-none">—</span>'}</td>
      <td class="vote-cell">${oppIcons || '<span class="vote-none">—</span>'}</td>
      <td>${r.reasoning || ''}</td>
    </tr>`;
  }
  html += '</tbody></table>';

  if (result.investment_thesis) {
    html += `<div class="meeting-thesis"><strong>投资主线:</strong> ${result.investment_thesis}</div>`;
  }

  if (result.key_agreements?.length) {
    html += '<div class="meeting-section"><strong>共识:</strong><ul>' +
      result.key_agreements.map(a => `<li>${a}</li>`).join('') + '</ul></div>';
  }
  if (result.key_disagreements?.length) {
    html += '<div class="meeting-section"><strong>分歧:</strong><ul>' +
      result.key_disagreements.map(d => `<li>${d}</li>`).join('') + '</ul></div>';
  }
  if (result.risk_warnings?.length) {
    html += '<div class="meeting-section meeting-risk"><strong>风险警示:</strong><ul>' +
      result.risk_warnings.map(w => `<li>${w}</li>`).join('') + '</ul></div>';
  }

  div.innerHTML = html;
}

function restoreMeeting(meetingData) {
  if (!meetingData) return;
  const transcript = document.getElementById('meeting-transcript');
  const messages = document.getElementById('meeting-messages');
  if (transcript) transcript.style.display = 'block';
  if (messages) messages.innerHTML = '';

  let lastRound = -1;
  for (const msg of (meetingData.transcript || [])) {
    if (msg.round_num !== lastRound) {
      const roundNames = { 0: '开场', 1: '独立提名', 2: '辩论与质疑', 3: '会议总结' };
      renderMeetingRoundDivider(msg.round_num, roundNames[msg.round_num] || `第${msg.round_num}轮`);
      lastRound = msg.round_num;
    }
    const participant = (meetingData.participants || []).find(p => p.role === msg.role);
    renderMeetingBubble({
      ...msg,
      avatar_letter: participant?.avatar_letter || msg.participant_name?.charAt(0) || '?',
      color: participant?.color || '#64748b',
    });
  }

  if (meetingData.final_ranking?.length) {
    renderMeetingResult(meetingData);
  }

  enableMeetingButton();
  const status = document.getElementById('meeting-status');
  if (status) status.textContent = '已完成';
  const btn = document.getElementById('btn-start-meeting');
  if (btn) btn.textContent = '重新开会';
}

/* ── AI 评点权重指纹 ──────────────────────── */
function _aiScoringConfig() {
  return state.phase3?.scoring_config || { quality_weight: 0.5, alpha_weight: 0.5 };
}

function _aiConfigMatch(cached) {
  if (!cached?.scoring_config) return false;
  const cur = _aiScoringConfig();
  return cached.scoring_config.quality_weight === cur.quality_weight
      && cached.scoring_config.alpha_weight === cur.alpha_weight;
}

/* ── AI 解读 — 内嵌展开面板 ──────────────────── */

function _updateExpandMeta(panel, data) {
  const modelEl = panel.querySelector('.ai-expand-model');
  const timeEl = panel.querySelector('.ai-expand-time');
  if (modelEl && data.model) modelEl.textContent = data.model;
  if (timeEl && data.generated_at) {
    const ts = data.generated_at.replace('T', ' ').slice(0, 19);
    timeEl.textContent = ts;
  }
}

function _updateTriggerBtn(chartType, hasCache) {
  const btn = document.querySelector(`.ai-interp-trigger[data-chart-type="${chartType}"]`);
  if (btn) {
    btn.classList.toggle('has-cache', !!hasCache);
    btn.textContent = hasCache ? '查看解读' : 'AI 解读';
  }
}

let _aiInterpBusy = false;
function toggleAiInterp(chartType) {
  if (_aiInterpBusy) return;
  _aiInterpBusy = true;
  setTimeout(() => _aiInterpBusy = false, 300);

  const panel = document.getElementById(`ai-expand-${chartType}`);
  if (!panel || !state.analysisId) return;

  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }

  panel.style.display = '';
  const body = panel.querySelector('.ai-expand-body');
  const cached = state.aiReports[chartType];

  if (cached?.text) {
    if (_aiConfigMatch(cached)) {
      body.innerHTML = formatMarkdown(cached.text);
      _updateExpandMeta(panel, cached);
      return;
    }
    body.innerHTML = `<div class="ai-stale-notice">
      <p>⚠ 权重已调整，以下为旧评点，可能与当前图表不一致</p>
    </div>` + formatMarkdown(cached.text);
    _updateExpandMeta(panel, cached);
    const regenBtn = panel.querySelector('.ai-regen-btn');
    if (regenBtn) regenBtn.style.display = '';
    return;
  }

  _fetchAiInterp(chartType, false);
}

async function _fetchAiInterp(chartType, force) {
  const panel = document.getElementById(`ai-expand-${chartType}`);
  if (!panel) return;
  panel.style.display = '';
  const body = panel.querySelector('.ai-expand-body');
  const regenBtn = panel.querySelector('.ai-regen-btn');
  if (regenBtn) regenBtn.disabled = true;
  body.innerHTML = '<div class="ai-expand-loading">正在生成 AI 解读...</div>';

  try {
    const { provider, model } = getMainModel();
    const resp = await fetch('/api/ai-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analysis_id: state.analysisId,
        provider, model,
        report_type: 'chart_interp',
        chart_type: chartType,
        force,
      }),
    });
    if (!resp.ok) {
      const errText = await resp.text().catch(() => `HTTP ${resp.status}`);
      body.innerHTML = `<p style="color:var(--danger)">AI 解读请求失败 (${resp.status})</p>`;
      return;
    }
    body.innerHTML = '';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.slice(5).trim());
          if (d.text) body.innerHTML += d.text.replace(/\n/g, '<br>');
          if (d.full_text) {
            body.innerHTML = formatMarkdown(d.full_text);
            const reportData = {
              text: d.full_text,
              scoring_config: _aiScoringConfig(),
              model: d.model || model,
              provider: d.provider || provider,
              generated_at: d.generated_at || '',
            };
            state.aiReports[chartType] = reportData;
            _updateExpandMeta(panel, reportData);
            _updateTriggerBtn(chartType, true);
          }
          if (d.message) body.innerHTML = `<p style="color:var(--danger)">${d.message}</p>`;
        } catch {}
      }
    }
  } catch (e) {
    body.innerHTML = `<p style="color:var(--danger)">AI 解读失败: ${e.message}</p>`;
  } finally {
    if (regenBtn) { regenBtn.disabled = false; regenBtn.style.display = ''; }
  }
}

async function generateAiReport() {
  const body = document.getElementById('wiz-report-body');
  const btn = document.getElementById('wiz-gen-report');
  if (!body || !state.analysisId) return;

  const cached = state.aiReports['comparison'];
  const isStale = cached?.text && !_aiConfigMatch(cached);
  const isFresh = cached?.text && _aiConfigMatch(cached);

  if (isFresh && btn?.textContent !== '重新生成') {
    body.innerHTML = formatMarkdown(cached.text);
    if (btn) btn.textContent = '重新生成';
    return;
  }

  if (isStale && btn?.textContent !== '重新生成') {
    body.innerHTML = `<div class="ai-stale-notice"><p>⚠ 权重已调整，以下为旧报告</p>
      <button class="btn btn-sm ai-regen-btn" id="ai-regen-comparison">重新生成</button>
    </div>` + formatMarkdown(cached.text);
    document.getElementById('ai-regen-comparison')?.addEventListener('click', () => {
      _fetchAiReport(body, btn, true);
    });
    if (btn) btn.textContent = '重新生成';
    return;
  }

  _fetchAiReport(body, btn, !!isFresh);
}

async function _fetchAiReport(body, btn, force) {
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
  body.innerHTML = '<p style="color:var(--muted);text-align:center;padding:20px">正在生成横向对比报告...</p>';

  try {
    const { provider, model } = getMainModel();
    const resp = await fetch('/api/ai-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analysis_id: state.analysisId,
        provider, model,
        report_type: 'comparison',
        force,
      }),
    });
    if (!resp.ok) {
      body.innerHTML = `<p style="color:var(--danger)">AI 报告请求失败 (${resp.status})</p>`;
      return;
    }
    body.innerHTML = '';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.slice(5).trim());
          if (d.text) body.innerHTML += d.text.replace(/\n/g, '<br>');
          if (d.full_text) {
            body.innerHTML = formatMarkdown(d.full_text);
            state.aiReports['comparison'] = {
              text: d.full_text,
              scoring_config: _aiScoringConfig(),
              model: d.model || model,
              provider: d.provider || provider,
              generated_at: d.generated_at || '',
            };
          }
          if (d.message) body.innerHTML = `<p style="color:var(--danger)">${d.message}</p>`;
        } catch {}
      }
    }
  } catch (e) {
    body.innerHTML = `<p style="color:var(--danger)">报告生成失败: ${e.message}</p>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '重新生成'; }
  }
}

function formatMarkdown(text) {
  return text
    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>')
    .replace(/^/, '<p>').replace(/$/, '</p>');
}

/* ── 抽屉 ─────────────────────────────────── */
function openDrawer(company) {
  const drawer = document.getElementById('wiz-drawer');
  const title = document.getElementById('drawer-title');
  const body = document.getElementById('drawer-body');
  if (!drawer) return;

  const name = company.supplier?.name || company.name || '未知';
  const ticker = company.supplier?.ticker || company.ticker || '';
  const layer = company.supplier?.layer_label || company.layer_label || '';

  title.innerHTML = `${name} ${layer ? `<span class="layer-badge ${layer}">${layer}</span>` : ''} <span class="col-ticker">${ticker}</span>`;

  body.innerHTML = buildDrawerContent(company);
  drawer.style.display = 'flex';

  const klineDom = document.getElementById('drawer-kline');
  if (klineDom && ticker) {
    klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">加载K线数据…</p>';
    const market = state.config?.market || 'us_stock';
    fetch(`/api/stock/${encodeURIComponent(ticker)}/kline?market=${market}`)
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(data => {
        if (!data?.length) { klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">暂无K线数据</p>'; return; }
        renderKlineChart(klineDom, data, name);
      })
      .catch(() => { klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">K线数据加载失败</p>'; });
  }
}

function closeDrawer() {
  const drawer = document.getElementById('wiz-drawer');
  if (drawer) drawer.style.display = 'none';
  const overlay = document.getElementById('kline-fullscreen');
  if (overlay) overlay.remove();
}

function renderKlineChart(dom, data, title) {
  dom.innerHTML = '';
  const chart = echarts.init(dom);
  const dates = data.map(d => d.date);
  const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
  const vols = data.map(d => d.volume);
  const colors = data.map(d => d.close >= d.open ? '#26a69a' : '#ef5350');

  const option = {
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
      { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, borderColor: 'transparent', fillerColor: 'rgba(0,160,233,.18)' },
    ],
    series: [
      { type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
      { type: 'bar', data: vols.map((v, i) => ({ value: v, itemStyle: { color: colors[i] + '66' } })),
        xAxisIndex: 1, yAxisIndex: 1 },
    ],
  };
  chart.setOption(option);

  dom.addEventListener('click', () => openKlineFullscreen(data, title));
}

function openKlineFullscreen(data, title) {
  let overlay = document.getElementById('kline-fullscreen');
  if (overlay) overlay.remove();

  overlay = document.createElement('div');
  overlay.id = 'kline-fullscreen';
  overlay.className = 'kline-overlay';
  overlay.innerHTML = `
    <div class="kline-overlay-header">
      <span>${title || ''} 近一年K线</span>
      <button class="kline-overlay-close">&times;</button>
    </div>
    <div id="kline-full-chart" style="flex:1;width:100%"></div>
  `;
  document.body.appendChild(overlay);

  const closeBtn = overlay.querySelector('.kline-overlay-close');
  closeBtn.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); } });

  requestAnimationFrame(() => {
    const dom = document.getElementById('kline-full-chart');
    if (!dom) return;
    const chart = echarts.init(dom);
    const dates = data.map(d => d.date);
    const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
    const vols = data.map(d => d.volume);
    const colors = data.map(d => d.close >= d.open ? '#26a69a' : '#ef5350');

    chart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      grid: [
        { left: 80, right: 40, top: 30, height: '58%' },
        { left: 80, right: 40, top: '82%', height: '12%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false } },
        { type: 'category', data: dates, gridIndex: 1, axisLabel: { fontSize: 11, color: '#ccc' } },
      ],
      yAxis: [
        { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } }, axisLabel: { color: '#ccc' } },
        { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 30, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 8, height: 18, borderColor: 'transparent', fillerColor: 'rgba(0,160,233,.25)' },
      ],
      series: [
        { type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
        { type: 'bar', data: vols.map((v, i) => ({ value: v, itemStyle: { color: colors[i] + '66' } })),
          xAxisIndex: 1, yAxisIndex: 1 },
      ],
    });
    window.addEventListener('resize', () => chart.resize());
  });
}

function buildDrawerContent(c) {
  const q = c.quality_score || c.overall_score || 0;
  const a = c.alpha_val || c.alpha?.alpha_score || 0;
  const f = c.final_score || 0;
  const snap = c.financial_snapshot || {};
  const alpha = c.alpha || {};
  const capYi = snap.market_cap_yi ?? c.supplier?.market_cap;

  const dimTips = {
    market_position: `市场地位 ${(c.market_position||0).toFixed(1)} 分\nAI 综合评估市场份额、品牌影响力、行业排名`,
    customer_validation: `客户验证 ${(c.customer_validation||0).toFixed(1)} 分\nAI 评估客户质量、长期订单合同、复购率`,
    capacity: `产能状况 ${(c.capacity_status||c.capacity||0).toFixed(1)} 分\nAI 评估产能利用率、扩产计划、交付周期`,
    financial_health: `财务健康 ${(c.financial_health||0).toFixed(1)} 分\n${snap.roe_pct != null ? 'ROE: ' + snap.roe_pct.toFixed(1) + '%' : ''}${snap.debt_ratio_pct != null ? ' 负债率: ' + snap.debt_ratio_pct.toFixed(1) + '%' : ''}${snap.gross_margin_pct != null ? ' 毛利率: ' + snap.gross_margin_pct.toFixed(1) + '%' : ''}`,
    valuation: `估值水平 ${(c.valuation||0).toFixed(1)} 分\n${snap.consensus_pe != null ? '预期PE: ' + snap.consensus_pe.toFixed(1) + 'x' : ''}${capYi != null ? ' 市值: ' + capYi + '亿' : ''}`,
  };

  const alphaDims = [
    { key: 'dim_cap', label: '市值规模', tip: `市值规模得分 ${alpha.dim_cap ?? '-'}\n${capYi != null ? '市值: ' + capYi + '亿' : '市值未知'}\n市值越小，预期差越大` },
    { key: 'dim_analyst', label: '分析师覆盖', tip: `分析师覆盖得分 ${alpha.dim_analyst ?? '-'}\n覆盖机构数: ${snap.analyst_report_count ?? '未知'}\n覆盖越少，信息差越大` },
    { key: 'dim_volume', label: '成交量动量', tip: `成交量动量得分 ${alpha.dim_volume ?? '-'}\n量比(10日/60日): ${snap.volume_ratio?.toFixed(2) ?? '未知'}\n连续放量 ${snap.consecutive_volume_days || 0} 天` },
    { key: 'dim_price', label: '近期涨幅', tip: `近期涨幅得分 ${alpha.dim_price ?? '-'}\n近3月: ${snap.price_change_3m_pct?.toFixed(1) ?? '?'}%  近1月: ${snap.price_change_1m_pct?.toFixed(1) ?? '?'}%` },
    { key: 'dim_institution', label: '机构持仓', tip: `机构持仓得分 ${alpha.dim_institution ?? '-'}\n机构持仓: ${snap.institution_holding_pct?.toFixed(1) ?? '未知'}%` },
  ];

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

  return `
    <div class="drawer-score-grid">
      <div class="drawer-score-box"><div class="val val-accent">${f.toFixed(2)}</div><div class="lbl">最终评分</div></div>
      <div class="drawer-score-box"><div class="val val-yellow">${q.toFixed(1)}</div><div class="lbl">质量分</div></div>
      <div class="drawer-score-box"><div class="val val-green">${a.toFixed(1)}</div><div class="lbl">预期差</div></div>
    </div>
    <div class="drawer-section"><h4>企业简介</h4><p style="font-size:13px;line-height:1.7">${c.supplier?.description || c.description || '暂无企业简介'}</p></div>
    ${c.supplier?.products?.length ? `
    <div class="drawer-section"><h4>核心产品</h4><div class="p2-product-tags">${(c.supplier.products || []).map(p => `<span class="p2-product-tag">${p}</span>`).join('')}</div></div>
    ` : ''}
    <div class="drawer-section"><h4>五维评分 <small style="color:var(--muted);font-weight:400">（悬停查看详情）</small></h4><div class="dim-bar-list">
      ${['market_position', 'customer_validation', 'capacity', 'financial_health', 'valuation'].map(k => {
        const labels = { market_position: '市场地位', customer_validation: '客户验证', capacity: '产能状况', financial_health: '财务健康', valuation: '估值水平' };
        const val = k === 'capacity' ? (c.capacity_status ?? c.capacity ?? c.scores?.capacity ?? 0) : (c[k] ?? c.scores?.[k] ?? 0);
        const tip = dimTips[k] || '';
        return `<div class="dim-bar-row" title="${tip}"><span class="dim-bar-label">${labels[k]}</span><div class="dim-bar-track"><div class="dim-bar-fill" style="width:${val * 10}%"></div></div><span class="dim-bar-val">${val.toFixed(1)}</span></div>`;
      }).join('')}
    </div></div>
    ${alpha.alpha_score != null ? `
    <div class="drawer-section"><h4>预期差分析 <small style="color:var(--muted);font-weight:400">（悬停查看详情）</small></h4><div class="dim-bar-list">
      ${alphaDims.map(d => {
        const val = alpha[d.key] ?? 0;
        return `<div class="dim-bar-row" title="${d.tip}"><span class="dim-bar-label">${d.label}</span><div class="dim-bar-track"><div class="dim-bar-fill dim-bar-fill--alpha" style="width:${val * 11.1}%"></div></div><span class="dim-bar-val">${val.toFixed(1)}</span></div>`;
      }).join('')}
    </div></div>` : ''}
    ${fRows.length ? `
    <div class="drawer-section"><h4>财务与市场</h4><div class="fin-grid">
      ${fRows.map(([l, v]) => {
        const isUp = v.includes('+');
        const isDown = v.startsWith('-');
        return `<div class="fin-cell"><span class="fin-label">${l}</span><span class="fin-val${isUp ? ' val-up' : isDown ? ' val-down' : ''}">${v}</span></div>`;
      }).join('')}
    </div></div>` : ''}
    <div class="drawer-section"><h4>股价走势 <small style="color:var(--muted);font-weight:400">（点击放大）</small></h4>
      <div id="drawer-kline" style="height:280px;width:100%;cursor:pointer"></div>
    </div>
    ${c.strengths?.length || c.weaknesses?.length ? `
    <div class="drawer-section"><h4>优势与风险</h4><div class="drawer-strengths">
      <div><h5 style="color:var(--success)">优势</h5>${(c.strengths || []).map(s => `<div class="s-item s-good">✅ ${s}</div>`).join('')}</div>
      <div><h5 style="color:var(--danger)">风险</h5>${(c.weaknesses || c.risks || []).map(r => `<div class="s-item s-bad">⚠️ ${r}</div>`).join('')}</div>
    </div></div>` : ''}
  `;
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
    `<button class="sector-btn" data-idx="${i}" data-sector="${s.sector}" data-product="${s.product}" data-market="${s.market || 'us_stock'}">${s.name}</button>`
  ).join('') + (sectors.length < 8 ? '<button class="sector-btn sector-add" id="wiz-add-sector">+ 添加赛道</button>' : '');

  grid.querySelectorAll('.sector-btn:not(.sector-add)').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
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
    if (!sector || !product) { alert('请输入产业方向和终端产品'); return; }
    if (ctxTarget) {
      const idx = parseInt(ctxTarget.dataset.idx);
      const sectors = loadSectors();
      if (sectors[idx]) {
        sectors[idx].market = market;
        sectors[idx].sector = sector;
        sectors[idx].product = product;
        sectors[idx].name = sector;
        saveSectors(sectors);
        renderSectorButtons();
      }
    }
    hideCtxMenu();
    resetForNewAnalysis();
    applySectorConfig({ sector, product, market });
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
      if (_p1Reader) { try { _p1Reader.cancel(); } catch {} _p1Reader = null; }
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
      if (_p2Reader) { try { _p2Reader.cancel(); } catch {} _p2Reader = null; }
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
  document.getElementById('wiz-p3-next')?.addEventListener('click', () => {
    goToPhase(4);
    const provs = getProviders();
    const configured = provs.filter(p => p.configured && !p.is_url);
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
      _fetchAiInterp(btn.dataset.chartType, true);
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

/* ── AI 模型设置页面初始化 ──────────────── */
async function initWizardSettings() {
  const providers = await fetchAndRender('wiz-provider-list');
  if (providers.length > 0) refreshModelSelectors(providers);

  onProvidersChange(refreshModelSelectors);

  document.getElementById('wiz-save-settings')?.addEventListener('click', () => {
    saveAll('wiz-provider-list', 'wiz-settings-status');
    _saveAndSyncMainModel();
  });

  document.getElementById('wiz-test-providers')?.addEventListener('click', () => {
    const btn = document.getElementById('wiz-test-providers');
    btn.disabled = true;
    btn.textContent = '测试中...';
    testAll('wiz-provider-list', 'wiz-settings-status').finally(() => {
      btn.disabled = false;
      btn.textContent = '测试全部连接';
    });
  });

  document.getElementById('wiz-test-main-model')?.addEventListener('click', async () => {
    const provSel = document.getElementById('wiz-settings-provider');
    const modelInput = document.getElementById('wiz-settings-model');
    const statusEl = document.getElementById('wiz-main-model-status');
    const btn = document.getElementById('wiz-test-main-model');
    if (!provSel || !modelInput || !statusEl) return;

    const provider = provSel.value;
    const model = modelInput.value.trim();
    if (!provider || !model) {
      statusEl.className = 'provider-test-status test-fail';
      statusEl.innerHTML = '&#x2718;';
      statusEl.title = '请选择 Provider 并填写模型名称';
      return;
    }

    btn.disabled = true;
    btn.textContent = '测试中...';
    statusEl.className = 'provider-test-status test-loading';
    statusEl.innerHTML = '<span class="spinner"></span>';
    statusEl.title = '';

    try {
      const resp = await fetch('/api/validate-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ main_provider: provider, main_model: model, cv_models: [], market: 'us_stock' }),
      });
      const data = await resp.json();
      const mainResult = (data.results || []).find(r => r.label === '主分析模型');
      if (mainResult?.success) {
        statusEl.className = 'provider-test-status test-pass';
        statusEl.innerHTML = '&#x2714;';
        statusEl.title = '测试通过';
        _saveAndSyncMainModel();
      } else {
        statusEl.className = 'provider-test-status test-fail';
        statusEl.innerHTML = '&#x2718;';
        statusEl.title = mainResult?.error || '测试失败';
      }
    } catch (err) {
      statusEl.className = 'provider-test-status test-fail';
      statusEl.innerHTML = '&#x2718;';
      statusEl.title = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = '测试';
    }
  });
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
      list.innerHTML = '<tr><td colspan="10" class="empty-text">暂无历史记录</td></tr>';
      return;
    }

    const MARKET_MAP = { a_stock: 'A股', us_stock: '美股', all: '全部' };
    const PH = ['瓶颈', '筛选', '验证', '会议'];
    list.innerHTML = analyses.map(a => {
      const cp = a.completed_phases || 0;
      const dots = PH.map((n, i) => `<span class="phase-dot ${i < cp ? 'phase-done' : ''}">${n}</span>`).join('');
      return `
      <tr data-id="${a.id}" class="company-row-clickable">
        <td><span class="history-seq">${a.seq_no ? '#' + a.seq_no : ''}</span></td>
        <td><span class="hist-mkt">${MARKET_MAP[a.market] || a.market || '-'}</span></td>
        <td class="hist-sector">${a.sector}</td>
        <td>${a.end_product}</td>
        <td>${a.max_market_cap_yi ? '≤' + a.max_market_cap_yi + '亿' : '-'}</td>
        <td>${a.max_depth || '-'}层</td>
        <td class="hist-model">${a.model || '-'}</td>
        <td>${a.supplier_count || 0}</td>
        <td><span class="history-phase-progress">${dots}</span></td>
        <td>${_fmtHistDate(a)}</td>
      </tr>`;
    }).join('');

    list.querySelectorAll('tr[data-id]').forEach(row => {
      row.addEventListener('click', () => {
        const id = row.dataset.id;
        const sector = row.querySelector('.hist-sector')?.textContent || '';
        if (confirm(`是否载入该分析数据？\n\n赛道: ${sector}`)) {
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
      _updateSeqBadge();

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

    // ── AI 评点恢复 ──
    if (data.ai_reports) {
      state.aiReports = data.ai_reports;
      for (const key of ['scatter', 'radar', 'bar', 'stack']) {
        if (state.aiReports[key]?.text) {
          _updateTriggerBtn(key, true);
        }
      }
    }

    updateSidebarStatus();
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
      list.innerHTML = '<tr><td colspan="11" class="empty-text">暂无历史记录</td></tr>';
      return;
    }

    const MARKET_MAP = { a_stock: 'A股', us_stock: '美股', all: '全部' };
    const PH = ['瓶颈', '筛选', '验证', '会议'];
    list.innerHTML = analyses.map(a => {
      const cp = a.completed_phases || 0;
      const dots = PH.map((n, i) => `<span class="phase-dot ${i < cp ? 'phase-done' : ''}">${n}</span>`).join('');
      return `
      <tr data-id="${a.id}">
        <td><span class="history-seq">${a.seq_no ? '#' + a.seq_no : ''}</span></td>
        <td><span class="hist-mkt">${MARKET_MAP[a.market] || a.market || '-'}</span></td>
        <td class="hist-sector">${a.sector}</td>
        <td>${a.end_product}</td>
        <td>${a.max_market_cap_yi ? '≤' + a.max_market_cap_yi + '亿' : '-'}</td>
        <td>${a.max_depth || '-'}层</td>
        <td class="hist-model">${a.model || '-'}</td>
        <td>${a.supplier_count || 0}</td>
        <td><span class="history-phase-progress">${dots}</span></td>
        <td>${_fmtHistDate(a)}</td>
        <td>
          <button class="btn btn-primary btn-sm hist-load-btn" data-id="${a.id}">载入</button>
          <button class="btn btn-danger btn-sm hist-del-btn" data-id="${a.id}">删除</button>
        </td>
      </tr>`;
    }).join('');

    list.querySelectorAll('.hist-load-btn').forEach(btn => {
      btn.addEventListener('click', e => {
        e.stopPropagation();
        const id = btn.dataset.id;
        const row = btn.closest('tr');
        const sector = row.querySelector('.hist-sector')?.textContent || '';
        if (confirm(`是否载入该分析数据？\n\n赛道: ${sector}`)) {
          loadWizardAnalysis(id);
        }
      });
    });

    list.querySelectorAll('.hist-del-btn').forEach(btn => {
      btn.addEventListener('click', async e => {
        e.stopPropagation();
        if (!confirm('确认删除该分析记录？此操作不可撤销。')) return;
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
    list.innerHTML = `<tr><td colspan="9" class="empty-text">加载失败: ${e.message}</td></tr>`;
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

function refreshModelSelectors(providerList) {
  const configured = (providerList || []).filter(p => p.configured && !p.is_url);
  if (configured.length === 0) return;

  const options = configured.map(p => {
    const model = DEFAULT_MODELS[p.id] || '';
    const val = `${p.id}::${model}`;
    return `<option value="${val}">${_escapeHtml(p.name)}</option>`;
  }).join('');

  ['wiz-main-model', 'wiz-p1-model', 'wiz-auto-model'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = options;
    if (sel.querySelector(`option[value="${prev}"]`)) {
      sel.value = prev;
    }
  });

  const provSel = document.getElementById('wiz-settings-provider');
  const modelInput = document.getElementById('wiz-settings-model');
  if (provSel) {
    const prevProv = provSel.value;
    provSel.innerHTML = configured.map(p =>
      `<option value="${p.id}">${_escapeHtml(p.name)}</option>`
    ).join('');

    const saved = _loadMainModel();
    if (saved && provSel.querySelector(`option[value="${saved.provider}"]`)) {
      provSel.value = saved.provider;
      if (modelInput) modelInput.value = saved.model;
    } else if (provSel.querySelector(`option[value="${prevProv}"]`)) {
      provSel.value = prevProv;
      if (modelInput) modelInput.value = DEFAULT_MODELS[prevProv] || '';
    } else {
      if (modelInput) modelInput.value = DEFAULT_MODELS[provSel.value] || '';
    }

    provSel.onchange = () => {
      if (modelInput) modelInput.value = DEFAULT_MODELS[provSel.value] || '';
      _saveAndSyncMainModel();
    };
    if (modelInput) modelInput.onchange = () => _saveAndSyncMainModel();
  }

  _syncMainModelFromSettings();
  loadCvModels(configured);
}

function _saveAndSyncMainModel() {
  const provSel = document.getElementById('wiz-settings-provider');
  const modelInput = document.getElementById('wiz-settings-model');
  if (!provSel || !modelInput) return;
  const provider = provSel.value;
  const model = modelInput.value.trim();
  localStorage.setItem('bh_main_model', JSON.stringify({ provider, model }));
  _syncMainModelFromSettings();
}

function _loadMainModel() {
  try {
    const raw = localStorage.getItem('bh_main_model');
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

function _syncMainModelFromSettings() {
  const provSel = document.getElementById('wiz-settings-provider');
  const modelInput = document.getElementById('wiz-settings-model');
  if (!provSel || !modelInput) return;
  const val = `${provSel.value}::${modelInput.value.trim()}`;
  ['wiz-main-model', 'wiz-p1-model', 'wiz-auto-model'].forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    if (sel.querySelector(`option[value="${val}"]`)) {
      sel.value = val;
    }
  });
}

function _escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function loadCvModels(configuredProviders) {
  const container = document.getElementById('wiz-cv-models');
  if (!container) return;

  let models;
  if (configuredProviders && configuredProviders.length > 0) {
    models = configuredProviders.map(p => ({
      provider: p.id,
      model: DEFAULT_MODELS[p.id] || '',
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
      <span>${m.label}</span>
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

export { state as wizardState, goToPhase, openDrawer };
