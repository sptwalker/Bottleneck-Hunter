/**
 * app.js — Main entry point for BottleneckHunter web UI.
 */

import { resizeAll, initChartFullscreen, initChainTabs, initWizChainTabs, initWizFullscreen } from './charts.js';
import { initSettings } from './settings.js';
import { initWizard } from './phases.js';
import { initWatchlist } from './watchlist.js';
import { initDecision } from './decision.js';
import { initAdmin } from './admin.js';

/* ── Global state ────────────────────────────────────── */
window.appState = {
  view: 'wizard',
  running: false,
  results: {},
  user: null,
};

export function showView(viewName) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-btn[data-view]').forEach(b => b.classList.remove('active'));

  const viewId = viewName === 'screen' ? 'view-wizard' : `view-${viewName}`;
  const viewEl = document.getElementById(viewId);
  if (viewEl) viewEl.classList.add('active');

  const navBtn = document.querySelector(`.nav-btn[data-view="${viewName}"]`);
  if (navBtn) navBtn.classList.add('active');

  document.body.classList.toggle('wizard-active', viewName === 'screen');
  window.appState.view = viewName === 'screen' ? 'wizard' : viewName;
}

/* ── Auth ───────────────────────────────────────────── */
async function initAuth() {
  try {
    const resp = await fetch('/api/auth/me');
    if (resp.ok) {
      const user = await resp.json();
      window.appState.user = user;
      const nameEl = document.getElementById('user-display-name');
      if (nameEl) nameEl.textContent = user.display_name || user.username;
    }
  } catch (e) {
    console.warn('Auth check failed:', e);
  }

  // 退出按钮
  const logoutBtn = document.getElementById('btn-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      window.location.href = '/login';
    });
  }
}

/* ── Bootstrap ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initAuth();
  initSettings();
  initAdmin();
  initChartFullscreen();
  initChainTabs();
  initWizChainTabs();
  initWizFullscreen();
  initWizard();
  initWatchlist();
  initDecision();

  document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      showView(btn.dataset.view);
    });
  });

  requestAnimationFrame(() => resizeAll());
});
