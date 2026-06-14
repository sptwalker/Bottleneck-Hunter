/**
 * dashboard.js — Render data into the 4 dashboard cards + picks card.
 */

import { renderDAG, renderBottleneckBars, renderRadar } from './charts.js';

/* ── Helpers ──────────────────────────────────────────── */
function removeSkeleton(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('skeleton');
}

function scoreClass(val) {
  if (val >= 8) return 'score-high';
  if (val >= 6) return 'score-mid';
  return 'score-low';
}

function consensusBadge(consensus) {
  if (consensus === 'pass') return '<span class="badge badge-pass">&#x2705; 通过</span>';
  if (consensus === 'concern') return '<span class="badge badge-concern">&#x26A0;&#xFE0F; 存疑</span>';
  return '<span class="badge badge-fail">&#x274C; 不通过</span>';
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/* ── Store bottleneck data for radar on click ─────────── */
let bottleneckReports = [];
let bottleneckViewMode = 'bar'; // 'bar' | 'radar'
let selectedBottleneckIdx = 0;

/* ── 瓶颈排名卡片切换按钮管理 ─────────────────────────── */
function ensureToggleBtn() {
  const card = document.getElementById('card-bottleneck');
  if (!card) return;
  let btn = card.querySelector('.bottleneck-toggle');
  if (!btn) {
    btn = document.createElement('button');
    btn.className = 'btn btn-sm bottleneck-toggle';
    btn.textContent = '雷达图';
    const title = card.querySelector('.dash-card-title');
    if (title) {
      title.style.display = 'flex';
      title.style.alignItems = 'center';
      title.style.justifyContent = 'space-between';
      title.appendChild(btn);
    }
    btn.addEventListener('click', () => {
      if (!bottleneckReports.length) return;
      if (bottleneckViewMode === 'bar') {
        bottleneckViewMode = 'radar';
        btn.textContent = '柱状图';
        renderRadar(bottleneckReports[selectedBottleneckIdx] || bottleneckReports[0]);
      } else {
        bottleneckViewMode = 'bar';
        btn.textContent = '雷达图';
        renderBottleneckBars(bottleneckReports);
      }
    });
  }
  return btn;
}

/* ── renderChain: decompose step_done ────────────────── */
export function renderChain(chainData) {
  removeSkeleton('chart-chain');
  renderDAG(chainData);
}

/* ── renderBottlenecks: bottleneck step_done ──────────── */
export function renderBottlenecks(reports) {
  removeSkeleton('chart-bottleneck');
  bottleneckReports = reports;
  bottleneckViewMode = 'bar';
  selectedBottleneckIdx = 0;

  renderBottleneckBars(reports);

  // 初始化切换按钮
  const btn = ensureToggleBtn();
  if (btn) btn.textContent = '雷达图';

  // 注册柱状图点击回调：联动切换到雷达图
  window._onBottleneckBarClick = (idx, report) => {
    selectedBottleneckIdx = idx;
    bottleneckViewMode = 'radar';
    const toggleBtn = document.querySelector('.bottleneck-toggle');
    if (toggleBtn) toggleBtn.textContent = '柱状图';
    renderRadar(report);
  };

  // 将瓶颈标记回写到产业链图谱数据，刷新图谱高亮
  const chainData = window.appState.results.decompose;
  if (chainData && chainData.nodes && reports && reports.length > 0) {
    const bnNames = new Set(reports.map(r => r.node_name || r.name));
    chainData.nodes.forEach(n => {
      n.is_bottleneck = bnNames.has(n.name);
    });
    renderDAG(chainData);
  }
}

/* ── renderSuppliers: supplier_eval step_done ─────────── */
export function renderSuppliers(scorecards) {
  removeSkeleton('table-suppliers');
  const container = document.getElementById('table-suppliers');
  if (!scorecards || scorecards.length === 0) {
    container.innerHTML = '<p class="empty-msg">未找到符合条件的供应商</p>';
    return;
  }

  const rows = scorecards.map((sc, i) => {
    const s = sc.supplier || {};
    const scores = sc.dimension_scores || {};
    const position = scores.position ?? '-';
    const customer = scores.customer ?? '-';
    const capacity = scores.capacity ?? '-';
    const financial = scores.financial ?? '-';
    const valuation = scores.valuation ?? '-';
    const overall = sc.overall_score ?? '-';

    return `
      <tr class="supplier-row" data-idx="${i}">
        <td>${i + 1}</td>
        <td>${escapeHtml(s.name || sc.company_name || '')}</td>
        <td class="ticker">${escapeHtml(s.ticker || sc.ticker || '')}</td>
        <td>${escapeHtml(sc.bottleneck_node || '')}</td>
        <td class="${scoreClass(position)}">${position}</td>
        <td class="${scoreClass(customer)}">${customer}</td>
        <td class="${scoreClass(capacity)}">${capacity}</td>
        <td class="${scoreClass(financial)}">${financial}</td>
        <td class="${scoreClass(valuation)}">${valuation}</td>
        <td class="overall ${scoreClass(overall)}"><strong>${overall}</strong></td>
      </tr>
      <tr class="supplier-detail" id="detail-${i}" style="display:none">
        <td colspan="10">
          <div class="detail-grid">
            <div class="detail-col">
              <h4>优势</h4>
              <ul>${(sc.strengths || []).map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
            </div>
            <div class="detail-col">
              <h4>风险</h4>
              <ul>${(sc.weaknesses || []).map(w => `<li>${escapeHtml(w)}</li>`).join('')}</ul>
            </div>
          </div>
        </td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>#</th><th>公司</th><th>代码</th><th>瓶颈环节</th>
          <th>地位</th><th>客户</th><th>产能</th><th>财务</th><th>估值</th><th>综合</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  // Click row to expand details
  container.querySelectorAll('.supplier-row').forEach(row => {
    row.addEventListener('click', () => {
      const idx = row.dataset.idx;
      const detail = document.getElementById(`detail-${idx}`);
      if (detail) {
        detail.style.display = detail.style.display === 'none' ? '' : 'none';
      }
    });
  });
}

/* ── renderValidation: cross_validate step_done ───────── */
export function renderValidation(validations) {
  removeSkeleton('table-validation');
  const container = document.getElementById('table-validation');
  if (!validations || validations.length === 0) {
    container.innerHTML = '<p class="empty-msg">无交叉验证结果</p>';
    return;
  }

  const rows = validations.map(v => `
    <tr>
      <td>${escapeHtml(v.company_name || '')}</td>
      <td class="ticker">${escapeHtml(v.ticker || '')}</td>
      <td>${consensusBadge(v.consensus)}</td>
      <td>${v.pass_rate != null ? (v.pass_rate * 100).toFixed(0) + '%' : '-'}</td>
      <td>${escapeHtml(v.summary || '')}</td>
    </tr>`).join('');

  container.innerHTML = `
    <table class="data-table">
      <thead>
        <tr><th>公司</th><th>代码</th><th>共识</th><th>通过率</th><th>摘要</th></tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/* ── renderPicks: complete event ──────────────────────── */
export function renderPicks(topPicks, scorecards, validations) {
  const card = document.getElementById('card-picks');
  const body = document.getElementById('picks-body');
  if (!topPicks || topPicks.length === 0) {
    body.innerHTML = '<p class="empty-msg">暂无推荐标的</p>';
    card.style.display = '';
    return;
  }

  // Build a lookup for scorecards and validations
  const scMap = {};
  (scorecards || []).forEach(sc => {
    const ticker = sc.supplier?.ticker || sc.ticker || '';
    if (ticker) scMap[ticker] = sc;
  });
  const cvMap = {};
  (validations || []).forEach(v => {
    if (v.ticker) cvMap[v.ticker] = v;
  });

  const pickCards = topPicks.map(ticker => {
    const sc = scMap[ticker] || {};
    const cv = cvMap[ticker];
    const name = sc.supplier?.name || sc.company_name || ticker;
    const score = sc.overall_score ?? '-';
    const badge = cv ? consensusBadge(cv.consensus) : '';

    return `
      <div class="pick-card">
        <div class="pick-ticker">${escapeHtml(ticker)}</div>
        <div class="pick-name">${escapeHtml(name)}</div>
        <div class="pick-score ${scoreClass(score)}">${score}</div>
        <div class="pick-consensus">${badge}</div>
      </div>`;
  }).join('');

  body.innerHTML = `<div class="picks-grid">${pickCards}</div>`;
  card.style.display = '';
}

/* ── showError: display error in the relevant card ────── */
export function showError(step, message) {
  const stepToCard = {
    decompose: 'card-chain',
    bottleneck: 'card-bottleneck',
    supplier_search: 'card-suppliers',
    supplier_eval: 'card-suppliers',
    cross_validate: 'card-validation',
  };
  const cardId = stepToCard[step];
  if (!cardId) return;

  const card = document.getElementById(cardId);
  if (!card) return;

  const body = card.querySelector('.dash-card-body');
  if (body) {
    const container = body.firstElementChild;
    if (container) container.classList.remove('skeleton');
    body.innerHTML = `<div class="error-banner">错误: ${escapeHtml(message)}</div>`;
  }
}
