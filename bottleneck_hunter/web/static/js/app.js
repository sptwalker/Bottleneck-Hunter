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
import { initDataReport } from './data-report.js';
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
      // 认证完成后再按角色设置管理员专属按钮，避免早于 auth 加载被误隐藏
      const addProviderBtn = document.getElementById('aic-add-custom');
      if (addProviderBtn) addProviderBtn.style.display = user.role === 'admin' ? '' : 'none';
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

/* ── 新用户必读 · 使用指南 ───────────────────────────── */
function initGuide() {
  const btn = document.getElementById('btn-guide');
  const modal = document.getElementById('guide-modal');
  const body = document.getElementById('guide-modal-body');
  if (!btn || !modal || !body) return;

  let loaded = false;
  const close = () => { modal.style.display = 'none'; };

  const render = (md) => {
    body.innerHTML = (typeof marked !== 'undefined')
      ? marked.parse(md || '')
      : `<pre style="white-space:pre-wrap">${(md || '').replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]))}</pre>`;
  };

  btn.addEventListener('click', async () => {
    modal.style.display = 'flex';
    // admin 才显示"上传替换"
    const uploadBtn = document.getElementById('guide-upload-btn');
    if (uploadBtn) uploadBtn.style.display = window.appState?.user?.role === 'admin' ? '' : 'none';
    if (loaded) return;
    body.innerHTML = '加载中…';
    try {
      const resp = await fetch('/api/settings/guide');
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      render(data.markdown);
      loaded = true;
    } catch (e) {
      body.innerHTML = `<p class="st-empty-hint">指南加载失败：${e.message}</p>`;
    }
  });

  // admin 上传 markdown 覆盖新手必读
  const uploadBtn = document.getElementById('guide-upload-btn');
  const uploadInput = document.getElementById('guide-upload-input');
  uploadBtn?.addEventListener('click', () => uploadInput?.click());
  uploadInput?.addEventListener('change', async () => {
    const file = uploadInput.files?.[0];
    if (!file) return;
    try {
      const markdown = await file.text();
      const resp = await fetch('/api/settings/guide', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ markdown }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      render(markdown);       // 立即预览新内容
      loaded = true;
      alert('新手必读已更新');
    } catch (e) {
      alert(`上传失败：${e.message}`);
    } finally {
      uploadInput.value = '';
    }
  });

  document.getElementById('guide-modal-close')?.addEventListener('click', close);
  modal.addEventListener('click', e => { if (e.target === modal) close(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && modal.style.display !== 'none') close(); });
}

/* ── Bootstrap ───────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initAuth();
  initAdmin();
  initGuide();
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
  initDataReport();

  document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      showView(btn.dataset.view);
    });
  });

  requestAnimationFrame(() => resizeAll());
});
