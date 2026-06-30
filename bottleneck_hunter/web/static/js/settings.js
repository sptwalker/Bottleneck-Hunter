/**
 * settings.js — LLM API settings (modal + inline).
 */

import { syncProviderFromSettings } from './panel.js';
import { showConfirm } from './utils/confirm.js';

let providers = [];
let _onProvidersChange = null;

function openModal() {
  document.getElementById('settings-modal').style.display = '';
  document.getElementById('settings-status').textContent = '';
  fetchAndRender('provider-list');
}

function closeModal() {
  document.getElementById('settings-modal').style.display = 'none';
}

export async function fetchAndRender(containerId) {
  const list = document.getElementById(containerId);
  if (!list) return [];
  list.innerHTML = '<p style="color:var(--muted);font-size:var(--fs-sm)">加载中...</p>';

  try {
    const res = await fetch('/api/settings');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    providers = data.providers || [];
    renderProviders(containerId);
    return providers;
  } catch (err) {
    list.innerHTML = `<p style="color:var(--danger)">加载失败: ${err.message}</p>`;
    return [];
  }
}

function renderProviders(containerId) {
  const list = document.getElementById(containerId);
  if (!list) return;
  list.innerHTML = providers.map((p, i) => {
    const placeholder = p.configured ? p.masked : (p.is_url ? 'http://localhost:11434' : '未配置');
    const inputType = p.is_url ? 'text' : 'password';
    const sourceTag = p.source === 'user' ? '<span class="key-source user">个人</span>'
                    : p.source === 'global' ? '<span class="key-source global">全局</span>'
                    : '';
    const deleteBtn = p.source === 'user'
      ? `<button type="button" class="btn-delete-key" data-provider="${p.id}" title="删除个人 KEY">✕</button>`
      : '';
    return `
      <div class="provider-row" data-provider-id="${p.id}">
        <span class="provider-status ${p.configured ? 'configured' : ''}"></span>
        <span class="provider-label">${escapeHtml(p.name)}${sourceTag}</span>
        <div class="provider-input-wrap">
          <input type="${inputType}" data-env="${p.env_var}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
          ${p.is_url ? '' : '<button type="button" class="btn-toggle-vis" data-idx="' + i + '" title="显示/隐藏">👁</button>'}
          ${deleteBtn}
        </div>
        <span class="provider-test-status" data-test-id="${p.id}"></span>
      </div>`;
  }).join('');

  list.querySelectorAll('.btn-toggle-vis').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.provider-input-wrap');
      const input = row.querySelector('input');
      input.type = input.type === 'password' ? 'text' : 'password';
    });
  });

  list.querySelectorAll('.btn-delete-key').forEach(btn => {
    btn.addEventListener('click', async () => {
      const provider = btn.dataset.provider;
      if (!await showConfirm(`确定删除 ${provider} 的个人 API KEY？将回退到全局 KEY（如有）。`)) return;
      try {
        const res = await fetch(`/api/user/api-keys/${provider}`, { method: 'DELETE' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        await fetchAndRender(containerId);
      } catch (err) {
        alert(`删除失败: ${err.message}`);
      }
    });
  });
}

export async function testAll(containerId, statusId, callback) {
  const statusEl = document.getElementById(statusId);
  const setStatus = (msg, cls) => {
    if (statusEl) { statusEl.textContent = msg; statusEl.className = 'settings-status ' + (cls || ''); }
  };

  setStatus('正在测试所有已配置的 Provider...', '');

  providers.forEach(p => {
    const el = document.querySelector(`#${containerId} [data-test-id="${p.id}"], [data-test-id="${p.id}"]`);
    if (!el) return;
    if (p.configured) {
      el.className = 'provider-test-status test-loading';
      el.innerHTML = '<span class="spinner"></span>';
      el.title = '';
    } else {
      el.className = 'provider-test-status';
      el.innerHTML = '';
    }
  });

  try {
    const res = await fetch('/api/test-providers', { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const results = data.results || [];

    const passedIds = new Set();
    let passCount = 0;
    let failCount = 0;

    results.forEach(r => {
      const el = document.querySelector(`[data-test-id="${r.id}"]`);
      if (!el) return;
      if (r.success) {
        el.className = 'provider-test-status test-pass';
        el.innerHTML = '&#x2714;';
        el.title = '测试通过';
        passedIds.add(r.id);
        passCount++;
      } else {
        el.className = 'provider-test-status test-fail';
        el.innerHTML = '&#x2718;';
        el.title = r.error || '测试失败';
        failCount++;
      }
    });

    providers.forEach(p => {
      if (!p.configured) {
        const el = document.querySelector(`[data-test-id="${p.id}"]`);
        if (el) { el.className = 'provider-test-status'; el.innerHTML = ''; }
      }
    });

    const msg = `测试完成: ${passCount} 个通过, ${failCount} 个失败`;
    setStatus(msg, failCount === 0 ? 'success' : 'error');

    syncProviderFromSettings(providers, passedIds);
    if (_onProvidersChange) _onProvidersChange(providers);
    if (callback) callback(providers);
  } catch (err) {
    setStatus(`测试失败: ${err.message}`, 'error');
    document.querySelectorAll('.provider-test-status').forEach(el => {
      if (el.classList.contains('test-loading')) {
        el.className = 'provider-test-status';
        el.innerHTML = '';
      }
    });
  }
}

export async function saveAll(containerId, statusId, callback) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const inputs = container.querySelectorAll('input[data-env]');
  const settings = {};
  inputs.forEach(input => {
    const val = input.value.trim();
    if (val) settings[input.dataset.env] = val;
  });

  if (Object.keys(settings).length === 0) {
    const statusEl = document.getElementById(statusId);
    if (statusEl) { statusEl.textContent = '未检测到修改'; statusEl.className = 'settings-status error'; }
    return;
  }

  const statusEl = document.getElementById(statusId);
  const setStatus = (msg, cls) => {
    if (statusEl) { statusEl.textContent = msg; statusEl.className = 'settings-status ' + (cls || ''); }
  };

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    providers = data.providers || providers;
    renderProviders(containerId);
    syncProviderFromSettings(providers);
    if (_onProvidersChange) _onProvidersChange(providers);
    if (callback) callback(providers);
    setStatus('已保存，即时生效', 'success');
  } catch (err) {
    setStatus(`保存失败: ${err.message}`, 'error');
  }
}

export function getProviders() {
  return providers;
}

export function onProvidersChange(fn) {
  _onProvidersChange = fn;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

export function initSettings() {
  const btnSettings = document.getElementById('btn-settings');
  if (btnSettings) btnSettings.addEventListener('click', openModal);

  const btnClose = document.getElementById('btn-close-settings');
  if (btnClose) btnClose.addEventListener('click', closeModal);

  const btnCancel = document.getElementById('btn-cancel-settings');
  if (btnCancel) btnCancel.addEventListener('click', closeModal);

  const btnSave = document.getElementById('btn-save-settings');
  if (btnSave) btnSave.addEventListener('click', () => saveAll('provider-list', 'settings-status'));

  const btnTest = document.getElementById('btn-test-providers');
  if (btnTest) btnTest.addEventListener('click', () => {
    btnTest.disabled = true;
    btnTest.textContent = '测试中...';
    testAll('provider-list', 'settings-status').finally(() => {
      btnTest.disabled = false;
      btnTest.textContent = '测试连接';
    });
  });

  const modal = document.getElementById('settings-modal');
  if (modal) {
    modal.addEventListener('click', (e) => {
      if (e.target.classList.contains('modal-overlay')) closeModal();
    });
  }
}
