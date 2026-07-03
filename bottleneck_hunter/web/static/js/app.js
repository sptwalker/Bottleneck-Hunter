/**
 * app.js — Main entry point for BottleneckHunter web UI.
 */

import { resizeAll, initChartFullscreen, initChainTabs, initWizChainTabs, initWizFullscreen } from './charts.js';
import { initWizard } from './phases.js';
import { initReverse } from './reverse.js';
import { initWatchlist } from './watchlist.js';
import { initDecision } from './decision.js';
import { initSimTrading, ensureSimTradingLoaded } from './simtrading.js';
import { initAIConfig } from './ai-config.js';
import { initAutoUpdate } from './auto-update.js';
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

  if (viewName === 'simtrading') {
    ensureSimTradingLoaded();
    if (window.appState.tradingDirty) {
      window.appState.tradingDirty = false;
      window.dispatchEvent(new CustomEvent('st-refresh'));
    }
  }
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
      const avatarEl = document.getElementById('user-avatar');
      if (avatarEl) avatarEl.textContent = (user.display_name || user.username || '?').charAt(0).toUpperCase();
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
  initAdmin();
  initChartFullscreen();
  initChainTabs();
  initWizChainTabs();
  initWizFullscreen();
  initWizard();
  initReverse();
  initWatchlist();
  initDecision();
  initSimTrading();
  initAIConfig();
  initAutoUpdate();

  document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      showView(btn.dataset.view);
    });
  });

  requestAnimationFrame(() => resizeAll());
});
