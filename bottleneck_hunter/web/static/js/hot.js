/**
 * hot.js — Fetch and render hot sectors from GET /api/hot-sectors.
 */

import { showView } from './app.js';

let lastFetch = 0;
const DEBOUNCE_MS = 5000;

/* ── Fetch hot sectors data ──────────────────────────── */
export async function fetchHotSectors() {
  // Debounce rapid clicks
  const now = Date.now();
  if (now - lastFetch < DEBOUNCE_MS) return;
  lastFetch = now;

  const tableWrap = document.getElementById('hot-table-wrap');
  const emerging = document.getElementById('hot-emerging');

  // Show loading skeleton
  tableWrap.classList.add('skeleton');
  tableWrap.innerHTML = '';
  emerging.innerHTML = '';

  try {
    const res = await fetch('/api/hot-sectors');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    renderSectorTable(data.sectors || []);
    renderEmerging(data.emerging || []);
  } catch (err) {
    tableWrap.classList.remove('skeleton');
    tableWrap.innerHTML = `<div class="error-banner">加载失败: ${err.message}</div>`;
  }
}

/* ── Render sector ranking table ─────────────────────── */
function renderSectorTable(sectors) {
  const wrap = document.getElementById('hot-table-wrap');
  wrap.classList.remove('skeleton');

  if (sectors.length === 0) {
    wrap.innerHTML = '<p class="empty-msg">暂无热点数据</p>';
    return;
  }

  const rows = sectors.map((s, i) => {
    const chg = s.price_change_pct ?? 0;
    const chgClass = chg > 0 ? 'val-up' : chg < 0 ? 'val-down' : '';
    const inflow = s.main_net_inflow != null ? (s.main_net_inflow / 1e8).toFixed(2) : '-';
    const turnover = s.turnover_rate != null ? s.turnover_rate.toFixed(2) : '-';

    return `
      <tr>
        <td>${i + 1}</td>
        <td>${escapeHtml(s.name)}</td>
        <td>${escapeHtml(s.sector_type || '')}</td>
        <td class="${chgClass}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</td>
        <td>${inflow}</td>
        <td>${turnover}</td>
        <td>${s.signal_count ?? '-'}</td>
        <td><span class="heat-bar" style="width:${Math.min(100, (s.composite_score || 0) * 10)}%"></span>${(s.composite_score || 0).toFixed(1)}</td>
      </tr>`;
  }).join('');

  wrap.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th>排名</th><th>板块</th><th>类型</th><th>涨幅%</th>
          <th>资金流入(亿)</th><th>换手率%</th><th>信号</th><th>热度</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;
}

/* ── Render emerging themes ──────────────────────────── */
function renderEmerging(themes) {
  const container = document.getElementById('hot-emerging');
  if (themes.length === 0) return;

  const items = themes.map(t => `
    <div class="emerging-card">
      <div class="emerging-info">
        <span class="emerging-name">${escapeHtml(t.name)}</span>
        <span class="emerging-score">热度 ${(t.composite_score || 0).toFixed(1)}</span>
      </div>
      <button class="btn btn-sm btn-secondary" data-sector="${escapeHtml(t.name)}">分析此板块</button>
    </div>`).join('');

  container.innerHTML = `
    <h3 class="section-title">新兴主题</h3>
    <div class="emerging-list">${items}</div>`;

  // "Analyze this sector" buttons
  container.querySelectorAll('button[data-sector]').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.sector;
      const sectorSelect = document.getElementById('sector-select');
      sectorSelect.value = 'custom';
      sectorSelect.dispatchEvent(new Event('change'));
      document.getElementById('custom-sector').value = name;
      document.getElementById('end-product').value = name;
      showView('welcome');

      // Highlight the screen nav
      document.querySelectorAll('.nav-btn').forEach(nb => {
        nb.classList.toggle('active', nb.dataset.view === 'screen');
      });
    });
  });
}

/* ── Escape HTML helper ──────────────────────────────── */
function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* ── Init hot view ───────────────────────────────────── */
export function initHot() {
  const refreshBtn = document.getElementById('btn-refresh-hot');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      lastFetch = 0; // bypass debounce for explicit refresh
      fetchHotSectors();
    });
  }
}
