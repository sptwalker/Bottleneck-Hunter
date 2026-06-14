/**
 * pipeline.js — Manage SSE connection to POST /api/screen.
 * Uses fetch + ReadableStream to read the SSE text/event-stream response.
 */

import { setPanelDisabled } from './panel.js';
import {
  renderChain, renderBottlenecks, renderSuppliers,
  renderValidation, renderPicks, showError,
} from './dashboard.js';

let abortController = null;

/* ── Pipeline step state management ──────────────────── */
let timerInterval = null;
let timerStart = null;
let timerBaseText = '';

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

  // Restore skeleton placeholders
  ['chart-chain', 'chart-bottleneck', 'table-suppliers', 'table-validation'].forEach(id => {
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
  }

  if (event === 'step_progress') {
    updateTimerText(data.message || '');
  }

  if (event === 'step_done') {
    setStepState(data.step, 'done');
    state.results[data.step] = data.result;

    if (data.step === 'decompose') renderChain(data.result);
    if (data.step === 'bottleneck') renderBottlenecks(data.result);
    if (data.step === 'supplier_eval') renderSuppliers(data.result);
    if (data.step === 'cross_validate' && !data.skipped) renderValidation(data.result);
  }

  if (event === 'complete') {
    stopTimer();
    document.getElementById('pipeline-status').textContent = '分析完成';
    renderPicks(data.top_picks, state.results.supplier_eval, state.results.cross_validate);
    state.reportPath = data.report_path || '';
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
