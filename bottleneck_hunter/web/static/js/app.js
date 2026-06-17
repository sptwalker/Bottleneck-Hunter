/**
 * app.js — Main entry point for BottleneckHunter web UI.
 * Manages view routing, global state, and module initialization.
 */

import { initPanel } from './panel.js';
import { initHot, fetchHotSectors } from './hot.js';
import { resizeAll, initChartFullscreen } from './charts.js';
import { initSettings } from './settings.js';
import { initHistory, fetchHistory } from './history.js';
import { initCvRefresh, initCvSave, initRefreshSuppliers, initComparePanel, initDetailDrawer } from './dashboard.js';

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
    const v = btn.dataset.view;
    const isActive = v === name ||
      (v === 'history' && name === 'welcome');
    btn.classList.toggle('active', isActive);
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
      } else if (view === 'history') {
        showView('welcome');
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
  initChartFullscreen();
  initCvRefresh();
  initCvSave();
  initRefreshSuppliers();
  initComparePanel();
  initDetailDrawer();
  initPanelToggle();
  showView('welcome');
});

/* ── Panel collapse toggle ─────────────────────────── */
function initPanelToggle() {
  const btn = document.getElementById('btn-panel-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const main = document.querySelector('.app-main');
    main.classList.toggle('panel-collapsed');
    btn.textContent = main.classList.contains('panel-collapsed') ? '▶' : '◀';
    btn.title = main.classList.contains('panel-collapsed') ? '展开侧栏' : '收起侧栏';
    setTimeout(() => resizeAll(), 350);
  });
}

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
