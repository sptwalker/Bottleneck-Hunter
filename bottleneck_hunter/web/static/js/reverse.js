/**
 * reverse.js — 反向分析控制器（入围筛选页下方）
 * 输入企业代码 → SSE 反向分析 → 持久化列表（与入围企业同款详情）
 * → 可加入观察池 / 引入交叉分析。
 */

import { readSSEStream } from './sse.js';
import { renderReverseTable, getReverseSelected } from './phase-views.js';

function currentMarket() {
  return document.getElementById('wiz-market')?.value
    || window.wizardState?.config?.market || 'us_stock';
}

function _scoreColor(s) {
  if (s >= 7.5) return '#16a34a';
  if (s >= 5) return '#ca8a04';
  if (s >= 3) return '#ea580c';
  return '#dc2626';
}

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

async function loadReverseList() {
  const market = currentMarket();
  try {
    const resp = await fetch(`/api/reverse/list?market=${encodeURIComponent(market)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    renderReverseTable(data.records || [], {
      market,
      onChange: loadReverseList,
      onSelectionChange: updateCrossBtn,
    });
  } catch (e) {
    console.warn('加载反向分析列表失败', e);
  }
}

function updateCrossBtn(selected) {
  const btn = document.getElementById('reverse-cross-btn');
  if (!btn) return;
  const n = selected ? selected.size : 0;
  btn.style.display = n > 0 ? '' : 'none';
  btn.textContent = `引入交叉分析（已选 ${n}）`;
}

async function runReverseAnalyze() {
  const input = document.getElementById('reverse-ticker-input');
  const status = document.getElementById('reverse-status');
  const btn = document.getElementById('reverse-analyze-btn');
  const ticker = (input?.value || '').trim();
  if (!ticker) { if (status) status.textContent = '请输入企业代码'; return; }

  if (btn) btn.disabled = true;
  if (status) status.textContent = '分析中...';
  const market = currentMarket();

  let ok = false;
  // 不传 provider/model：后端自动使用用户 AI 配置的「入围评估(pipeline_eval)」主模型
  await readSSEStream('/api/reverse/analyze',
    { ticker, market, language: 'zh' },
    {
      label: 'reverse-sse',
      onEvent: (data) => {
        const evt = data._sseEvent;
        if (evt === 'error') {
          if (status) status.textContent = '✗ ' + (data.message || '分析失败');
        } else if (evt === 'reverse_complete') {
          ok = true;
          if (status) status.textContent = '✓ 分析完成';
        } else if (data.message) {
          if (status) status.textContent = data.message;
        }
      },
      onError: (err) => { if (status) status.textContent = '✗ 连接失败: ' + err.message; },
    });

  if (btn) btn.disabled = false;
  if (ok) {
    if (input) input.value = '';
    await loadReverseList();
  }
}

function gatherValidationModels() {
  const checked = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  // 留空时后端自动使用用户 AI 配置的「交叉验证(pipeline_cross_val)」模型，避免误用默认模型
  return Array.from(checked).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model };
  });
}

async function runReverseCross() {
  const selected = [...getReverseSelected()];
  const result = document.getElementById('wiz-reverse-cross-result');
  if (!selected.length) return;
  const market = currentMarket();
  const vm = gatherValidationModels();
  if (result) result.innerHTML = '<p class="empty-text">交叉验证中...</p>';

  await readSSEStream('/api/reverse/cross-analyze',
    { ids: selected, market, validation_models: vm, language: 'zh' },
    {
      label: 'reverse-cross-sse',
      onEvent: (data) => {
        const evt = data._sseEvent;
        if (evt === 'error') {
          if (result) result.innerHTML = `<p class="empty-text">✗ ${_esc(data.message)}</p>`;
        } else if (evt === 'reverse_cross_complete') {
          renderReverseCross(data, market);
        } else if (data.message && result) {
          result.innerHTML = `<p class="empty-text">${_esc(data.message)}</p>`;
        }
      },
      onError: (err) => { if (result) result.innerHTML = `<p class="empty-text">✗ ${_esc(err.message)}</p>`; },
    });
}

function renderReverseCross(data, market) {
  const result = document.getElementById('wiz-reverse-cross-result');
  if (!result) return;
  const validations = data.validations || [];
  const ranked = data.ranked_results || [];
  if (!validations.length) { result.innerHTML = '<p class="empty-text">无交叉验证结果</p>'; return; }

  const modelNames = validations[0]?.validations?.map(v => v.model_name) || [];
  let html = `<div class="rev-cross-title">交叉验证结果</div>
    <table class="data-table"><thead><tr><th>公司</th><th>共识分</th>`;
  modelNames.forEach(m => { html += `<th>${_esc((m || '').split('/').pop())}</th>`; });
  html += '<th>观察池</th></tr></thead><tbody>';

  validations.forEach(cv => {
    const rk = ranked.find(r => (r.ticker || r.supplier?.ticker) === cv.ticker) || {};
    const name = cv.supplier_name || cv.ticker;
    const sector = rk.supplier?.sector || '';
    const bottleneck = rk.bottleneck_node || '';
    const score = (rk.final_score != null ? rk.final_score : cv.consensus_score) || 0;
    html += `<tr>
      <td class="col-name">${_esc(name)}</td>
      <td><span class="score-badge" style="background:${_scoreColor(cv.consensus_score)}">${(cv.consensus_score || 0).toFixed(1)}</span></td>`;
    (cv.validations || []).forEach(v => {
      html += `<td><span class="score-badge score-badge--sm" style="background:${_scoreColor(v.score)}">${(v.score || 0).toFixed(1)}</span></td>`;
    });
    html += `<td><button class="btn btn-sm rev-cross-add-btn"
      data-ticker="${_esc(cv.ticker)}" data-name="${_esc(name)}" data-score="${score.toFixed(2)}"
      data-sector="${_esc(sector)}" data-bottleneck="${_esc(bottleneck)}" data-market="${_esc(market)}">加入观察池</button></td></tr>`;
  });
  html += '</tbody></table>';
  result.innerHTML = html;

  result.querySelectorAll('.rev-cross-add-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const res = await fetch('/api/watchlist', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticker: btn.dataset.ticker, company_name: btn.dataset.name,
            market: btn.dataset.market, tier: 'track', sector: btn.dataset.sector,
            source: 'reverse_cross', bottleneck_node: btn.dataset.bottleneck,
          }),
        });
        if (res.ok) { btn.textContent = '已加入'; btn.classList.add('wl-p4-add-done'); }
        else {
          const err = await res.json().catch(() => ({}));
          btn.textContent = (err.detail || '').includes('already') ? '已存在' : '失败';
        }
      } catch (e) { btn.textContent = '失败'; }
    });
  });
}

export function initReverse() {
  const btn = document.getElementById('reverse-analyze-btn');
  if (btn) btn.addEventListener('click', runReverseAnalyze);

  const input = document.getElementById('reverse-ticker-input');
  if (input) input.addEventListener('keydown', e => { if (e.key === 'Enter') runReverseAnalyze(); });

  const crossBtn = document.getElementById('reverse-cross-btn');
  if (crossBtn) crossBtn.addEventListener('click', runReverseCross);

  const mkt = document.getElementById('wiz-market');
  if (mkt) mkt.addEventListener('change', loadReverseList);

  loadReverseList();
}
