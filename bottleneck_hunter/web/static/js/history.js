/**
 * history.js — 历史分析记录的列表展示、加载、删除。
 */

import { showView } from './app.js';
import {
  renderChain, renderBottlenecks, renderSuppliers,
  renderValidation, renderPicks, renderShortlist,
} from './dashboard.js';
import { restorePanelFromHistory } from './panel.js';
import { buildAnalysisTag } from './analysis-tag.js';
import { showConfirm } from './utils/confirm.js';

const MARKET_LABELS = {
  a_stock: 'A 股',
  us_stock: '美股',
  all: '全部',
};

/* ── Init ───────────────────────────────────────────── */
export function initHistory() {
  const btn = document.getElementById('btn-refresh-history');
  if (btn) btn.addEventListener('click', () => fetchHistory());
}

/* ── Fetch & render history list ────────────────────── */
export async function fetchHistory() {
  const container = document.getElementById('history-list');
  if (!container) return;
  container.innerHTML = '<p class="loading-text">正在加载历史记录...</p>';

  try {
    const resp = await fetch('/api/history');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderHistoryList(data.analyses || []);
  } catch (err) {
    container.innerHTML = `<p class="empty-msg">加载失败: ${err.message}</p>`;
  }
}

/* ── Render history cards ───────────────────────────── */
function renderHistoryList(analyses) {
  const container = document.getElementById('history-list');
  if (!container) return;

  if (analyses.length === 0) {
    container.innerHTML = '<p class="empty-msg">暂无历史分析记录。运行一次分析后，结果将自动保存。</p>';
    return;
  }

  const cards = analyses.map(a => {
    const picks = (a.top_picks || []).join(', ') || '—';
    const depthLabel = a.max_depth ? `${a.max_depth}层` : '—';

    return `
      <div class="history-card" data-id="${esc(a.id)}">
        <div class="history-card-header">
          ${buildAnalysisTag(a)}
        </div>
        <div class="history-card-body">
          <div class="history-meta">
            <span class="history-tag">深度: ${esc(depthLabel)}</span>
            <span class="history-tag">瓶颈: ${a.bottleneck_count || 0}</span>
            <span class="history-tag">供应商: ${a.supplier_count || 0}</span>
          </div>
          <div class="history-picks">推荐: ${esc(picks)}</div>
        </div>
        <div class="history-card-actions">
          <button class="btn btn-primary btn-sm history-load" data-id="${esc(a.id)}">查看分析</button>
          <button class="btn btn-danger btn-sm history-delete" data-id="${esc(a.id)}">删除</button>
        </div>
      </div>`;
  }).join('');

  container.innerHTML = cards;

  // Bind events
  container.querySelectorAll('.history-load').forEach(btn => {
    btn.addEventListener('click', () => loadAnalysis(btn.dataset.id));
  });
  container.querySelectorAll('.history-delete').forEach(btn => {
    btn.addEventListener('click', () => deleteAnalysis(btn.dataset.id));
  });
}

/* ── Load a full analysis into dashboard ────────────── */
async function loadAnalysis(id) {
  try {
    const resp = await fetch(`/api/history/${id}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const record = await resp.json();
    const result = record.result_json;
    if (!result) throw new Error('No result data');

    // Store into appState so dashboard refresh works
    window.appState.results = {};
    window.appState.config = {
      market: record.market || 'us_stock',
      provider: record.provider || 'openai',
      model: record.model || '',
      language: record.language || 'zh',
      max_depth: record.max_depth || 4,
      top_n: record.top_n || 5,
      sector: record.sector || '',
      end_product: record.end_product || '',
    };

    restorePanelFromHistory(window.appState.config);

    // Render chain
    if (result.chain) {
      window.appState.results.decompose = result.chain;
      renderChain(result.chain);
    }

    // Render bottlenecks (also triggers is_bottleneck marking on chain)
    if (result.bottleneck_reports && result.bottleneck_reports.length > 0) {
      window.appState.results.bottleneck = result.bottleneck_reports;
      renderBottlenecks(result.bottleneck_reports);
    }

    // Render suppliers
    if (result.supplier_scorecards) {
      window.appState.results.supplier_eval = result.supplier_scorecards;
      renderSuppliers(result.supplier_scorecards);
    }

    // Render cross-validation
    window.appState.results.cross_validate = result.cross_validations || [];
    renderValidation(result.cross_validations || []);

    // Render top picks
    renderPicks(
      result.top_picks || [],
      result.supplier_scorecards || [],
      result.cross_validations || [],
    );

    // Render shortlist
    renderShortlist(result.supplier_scorecards || []);

    // Show dashboard actions
    window.appState.reportPath = record.report_path || '';
    window.appState.analysisId = id;
    const actionsEl = document.getElementById('dashboard-actions');
    if (actionsEl) actionsEl.style.display = '';

    // Mark pipeline steps as done
    ['decompose', 'bottleneck', 'supplier_search', 'supplier_eval', 'cross_validate', 'save'].forEach(step => {
      const el = document.querySelector(`.pipeline-step[data-step="${step}"]`);
      if (el) el.className = 'pipeline-step done';
    });
    document.querySelectorAll('.pipeline-connector').forEach(c => c.classList.add('done'));
    const statusEl = document.getElementById('pipeline-status');
    if (statusEl) statusEl.textContent = `已加载历史记录 (${record.sector} — ${formatDate(record.created_at)})`;

    // Switch to screen view
    showView('screen');

  } catch (err) {
    alert(`加载分析失败: ${err.message}`);
  }
}

/* ── Delete analysis ────────────────────────────────── */
async function deleteAnalysis(id) {
  if (!await showConfirm('确认删除该分析记录？此操作不可撤销。', { danger: true })) return;

  try {
    const resp = await fetch(`/api/history/${id}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    fetchHistory(); // 刷新列表
  } catch (err) {
    alert(`删除失败: ${err.message}`);
  }
}

/* ── Helpers ─────────────────────────────────────────── */
function esc(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function formatDate(isoStr) {
  if (!isoStr) return '';
  try {
    const d = new Date(isoStr);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return isoStr;
  }
}
