/**
 * pipeline.js — Manage SSE connection to POST /api/screen.
 * Uses fetch + ReadableStream to read the SSE text/event-stream response.
 */

import { setPanelDisabled } from './panel.js';
import {
  renderChain, renderBottlenecks, renderSuppliers,
  renderValidation, renderPicks, renderShortlist, showError,
} from './dashboard.js';
import { disposeAllCharts } from './charts.js';

let abortController = null;

/* ── Pipeline step state management ──────────────────── */
let timerInterval = null;
let timerStart = null;
let timerBaseText = '';

/* ── 实时日志面板 ─────────────────────────────────────── */
const STEP_LOG_CONTAINERS = {
  decompose: 'chart-chain',
  bottleneck: 'chart-bottleneck',
  supplier_search: 'table-suppliers',
  supplier_eval: 'table-suppliers',
};

function _ensureLogPanel(step) {
  const containerId = STEP_LOG_CONTAINERS[step];
  if (!containerId) return null;
  const chartEl = document.getElementById(containerId);
  if (!chartEl) return null;
  const parent = chartEl.parentElement;

  let panel = parent.querySelector('.progress-log');
  if (!panel) {
    chartEl.classList.remove('skeleton');
    chartEl.style.display = 'none';
    panel = document.createElement('div');
    panel.className = 'progress-log';
    parent.insertBefore(panel, chartEl);
  }
  return panel;
}

function _removeLogPanel(step) {
  const containerId = STEP_LOG_CONTAINERS[step];
  if (!containerId) return;
  const chartEl = document.getElementById(containerId);
  if (!chartEl) return;
  const parent = chartEl.parentElement;
  const panel = parent.querySelector('.progress-log');
  if (panel) panel.remove();
  chartEl.style.display = '';
}

function _appendLog(step, message) {
  const panel = _ensureLogPanel(step);
  if (!panel) return;
  const line = document.createElement('div');
  line.className = 'log-line';
  const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  line.textContent = `[${time}] ${message}`;
  // 根据前缀设置颜色
  if (message.startsWith('✓') || message.startsWith('──')) {
    line.classList.add('log-ok');
  } else if (message.startsWith('⚠')) {
    line.classList.add('log-warn');
  } else if (message.startsWith('✗')) {
    line.classList.add('log-err');
  }
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function setStepState(step, state) {
  const el = document.querySelector(`.pipeline-step[data-step="${step}"]`);
  if (!el) return;
  el.className = `pipeline-step ${state}`;

  // Mark preceding connectors as done when step completes
  if (state === 'done' || state === 'running') {
    const prev = el.previousElementSibling;
    if (prev && prev.classList.contains('pipeline-connector')) {
      prev.classList.toggle('done', state === 'done');
    }
  }

  // Start/stop elapsed timer for running step
  if (state === 'running') {
    startTimer();
  } else if (state === 'done' || state === 'error') {
    stopTimer();
  }
}

function startTimer() {
  stopTimer();
  timerStart = Date.now();
  timerBaseText = document.getElementById('pipeline-status').textContent;
  timerInterval = setInterval(() => {
    const sec = Math.floor((Date.now() - timerStart) / 1000);
    document.getElementById('pipeline-status').textContent = `${timerBaseText} (${sec}s)`;
  }, 1000);
}

function updateTimerText(newText) {
  timerBaseText = newText;
  if (timerStart) {
    const sec = Math.floor((Date.now() - timerStart) / 1000);
    document.getElementById('pipeline-status').textContent = `${newText} (${sec}s)`;
  } else {
    document.getElementById('pipeline-status').textContent = newText;
  }
}

function stopTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  timerStart = null;
}

function resetPipeline() {
  stopTimer();
  document.querySelectorAll('.pipeline-step').forEach(el => {
    el.className = 'pipeline-step';
  });
  document.querySelectorAll('.pipeline-connector').forEach(el => {
    el.classList.remove('done');
  });
  document.getElementById('pipeline-status').textContent = '';
  document.getElementById('dashboard-actions').style.display = 'none';
  document.getElementById('card-picks').style.display = 'none';
  const shortlistCard = document.getElementById('card-shortlist');
  if (shortlistCard) shortlistCard.style.display = 'none';

  disposeAllCharts();

  // Restore skeleton placeholders
  ['chart-chain', 'chart-bottleneck', 'table-suppliers'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.classList.add('skeleton'); el.innerHTML = ''; }
  });
}

/* ── Parse SSE text chunks ───────────────────────────── */
function parseSSEChunk(chunk) {
  const events = [];
  const blocks = chunk.split('\n\n');
  for (const block of blocks) {
    if (!block.trim()) continue;
    let eventType = 'message';
    let dataLines = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) {
        eventType = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trim());
      }
    }
    if (dataLines.length > 0) {
      try {
        const data = JSON.parse(dataLines.join('\n'));
        events.push({ event: eventType, data });
      } catch { /* skip malformed JSON */ }
    }
  }
  return events;
}

/* ── Handle an SSE event ─────────────────────────────── */
function handleEvent(evt, state) {
  const { event, data } = evt;
  console.log(`[SSE] ${event}:`, data.step || '', data.message || '');

  if (event === 'step_start') {
    document.getElementById('pipeline-status').textContent = data.message || '';
    setStepState(data.step, 'running');
    if (STEP_LOG_CONTAINERS[data.step]) {
      _appendLog(data.step, data.message || '');
    }
  }

  if (event === 'step_progress') {
    // log=true 的消息只进日志面板，不刷新计时器文字
    if (data.log && STEP_LOG_CONTAINERS[data.step]) {
      _appendLog(data.step, data.message || '');
    } else {
      updateTimerText(data.message || '');
      // 层级进度也追加到日志面板
      if (STEP_LOG_CONTAINERS[data.step]) {
        _appendLog(data.step, data.message || '');
      }
    }
  }

  if (event === 'step_done') {
    _removeLogPanel(data.step);
    setStepState(data.step, 'done');
    state.results[data.step] = data.result;

    if (data.step === 'decompose') renderChain(data.result);
    if (data.step === 'bottleneck') renderBottlenecks(data.result);
    if (data.step === 'supplier_eval') renderSuppliers(data.result);
    if (data.step === 'cross_validate') renderValidation(data.skipped ? [] : data.result);
  }

  if (event === 'complete') {
    stopTimer();
    // 确保所有步骤（含 save）都标记为完成
    document.querySelectorAll('.pipeline-step').forEach(el => {
      if (!el.classList.contains('error')) {
        el.className = 'pipeline-step done';
      }
    });
    document.querySelectorAll('.pipeline-connector').forEach(el => {
      el.classList.add('done');
    });

    // 显示完成状态（含失败统计）
    let statusText = '分析完成';
    const failures = data.llm_failures || 0;
    const retries = data.llm_retries || 0;
    if (failures > 0) {
      statusText += ` | LLM 调用: ${failures} 次失败, ${retries} 次重试`;
    }
    document.getElementById('pipeline-status').textContent = statusText;

    renderPicks(data.top_picks, state.results.supplier_eval, state.results.cross_validate);
    renderShortlist(state.results.supplier_eval);
    state.reportPath = data.report_path || '';
    window.appState.analysisId = data.analysis_id || '';
    document.getElementById('dashboard-actions').style.display = '';
    window.appState.running = false;
    setPanelDisabled(false);
  }

  if (event === 'error') {
    stopTimer();
    setStepState(data.step, 'error');
    document.getElementById('pipeline-status').textContent = `错误: ${data.message}`;
    showError(data.step, data.message);
    window.appState.running = false;
    setPanelDisabled(false);
  }
}

/* ── Start screening via POST SSE ────────────────────── */
export async function startScreening(config) {
  cancelScreening();
  resetPipeline();

  window.appState.running = true;
  window.appState.results = {};
  window.appState.config = { ...config };
  setPanelDisabled(true);

  abortController = new AbortController();
  const state = window.appState;
  document.getElementById('pipeline-status').textContent = '正在连接分析服务...';

  try {
    const response = await fetch('/api/screen', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
      signal: abortController.signal,
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${response.statusText}`);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Normalize \r\n to \n (sse-starlette uses \r\n)
      buffer = buffer.replace(/\r\n/g, '\n');

      // SSE events are separated by \n\n
      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        const events = parseSSEChunk(part + '\n\n');
        events.forEach(evt => handleEvent(evt, state));
      }
    }

    // Process any remaining buffer
    if (buffer.trim()) {
      const events = parseSSEChunk(buffer);
      events.forEach(evt => handleEvent(evt, state));
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      document.getElementById('pipeline-status').textContent = '已取消';
    } else {
      document.getElementById('pipeline-status').textContent = `连接失败: ${err.message}`;
    }
    window.appState.running = false;
    setPanelDisabled(false);
  }
}

/* ── Cancel in-flight screening ──────────────────────── */
export function cancelScreening() {
  if (abortController) {
    abortController.abort();
    abortController = null;
  }
}
