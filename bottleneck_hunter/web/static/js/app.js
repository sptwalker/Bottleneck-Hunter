/**
 * app.js — Main entry point for BottleneckHunter web UI.
 * Manages view routing, global state, and module initialization.
 */

import { initPanel } from './panel.js';
import { initHot, fetchHotSectors } from './hot.js';
import { resizeAll } from './charts.js';
import { initSettings } from './settings.js';
import { initHistory, fetchHistory } from './history.js';

/* ── Global state ────────────────────────────────────── */
window.appState = {
  view: 'welcome',
  running: false,
  results: {},
};

/* ── View routing ────────────────────────────────────── */
export function showView(name) {
  document.querySelectorAll('.view').forEach(el => {
    el.classList.remove('active');
    el.style.display = 'none';
  });
  const target = document.getElementById(`view-${name}`);
  if (target) {
    target.classList.add('active');
    target.style.display = 'block';
  }

  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === name ||
      (btn.dataset.view === 'screen' && name === 'welcome'));
  });

  window.appState.view = name;

  // 切换到首页时自动加载历史记录
  if (name === 'welcome') {
    fetchHistory();
  }

  // Resize charts when switching views (they may have been hidden)
  requestAnimationFrame(() => resizeAll());
}

/* ── Nav button handlers ─────────────────────────────── */
function initNav() {
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const view = btn.dataset.view;
      if (view === 'screen') {
        // Show results if analysis ran, otherwise welcome
        if (window.appState.running || Object.keys(window.appState.results).length > 0) {
          showView('screen');
        } else {
          showView('welcome');
        }
      } else if (view === 'hot') {
        showView('hot');
        fetchHotSectors();
      }
    });
  });
}

/* ── Bootstrap ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initNav();
  initPanel();
  initHot();
  initSettings();
  initHistory();
  initExport();
  showView('welcome');
});

/* ── Export report button ────────────────────────────── */
function initExport() {
  const btn = document.getElementById('btn-export');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const reportPath = window.appState.reportPath;
    if (!reportPath) {
      alert('暂无可导出的报告');
      return;
    }
    window.open(`/api/report?path=${encodeURIComponent(reportPath)}`, '_blank');
  });
}
