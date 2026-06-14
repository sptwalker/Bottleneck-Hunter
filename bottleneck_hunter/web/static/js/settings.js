/**
 * settings.js — LLM API settings modal.
 */

import { syncProviderFromSettings } from './panel.js';

let providers = [];

function openModal() {
  document.getElementById('settings-modal').style.display = '';
  document.getElementById('settings-status').textContent = '';
  fetchSettings();
}

function closeModal() {
  document.getElementById('settings-modal').style.display = 'none';
}

async function fetchSettings() {
  const list = document.getElementById('provider-list');
  list.innerHTML = '<p style="color:var(--muted);font-size:var(--fs-sm)">加载中...</p>';

  try {
    const res = await fetch('/api/settings');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    providers = data.providers || [];
    renderProviders();
  } catch (err) {
    list.innerHTML = `<p style="color:var(--danger)">加载失败: ${err.message}</p>`;
  }
}

function renderProviders() {
  const list = document.getElementById('provider-list');
  list.innerHTML = providers.map((p, i) => {
    const placeholder = p.configured ? p.masked : (p.is_url ? 'http://localhost:11434' : '未配置');
    const inputType = p.is_url ? 'text' : 'password';
    return `
      <div class="provider-row">
        <span class="provider-status ${p.configured ? 'configured' : ''}"></span>
        <span class="provider-label">${escapeHtml(p.name)}</span>
        <div class="provider-input-wrap">
          <input type="${inputType}" data-env="${p.env_var}" placeholder="${escapeHtml(placeholder)}" autocomplete="off" spellcheck="false">
          ${p.is_url ? '' : '<button type="button" class="btn-toggle-vis" data-idx="' + i + '" title="显示/隐藏">👁</button>'}
        </div>
      </div>`;
  }).join('');

  list.querySelectorAll('.btn-toggle-vis').forEach(btn => {
    btn.addEventListener('click', () => {
      const row = btn.closest('.provider-input-wrap');
      const input = row.querySelector('input');
      input.type = input.type === 'password' ? 'text' : 'password';
    });
  });
}

async function saveSettings() {
  const inputs = document.querySelectorAll('#provider-list input[data-env]');
  const settings = {};
  inputs.forEach(input => {
    const val = input.value.trim();
    if (val) settings[input.dataset.env] = val;
  });

  if (Object.keys(settings).length === 0) {
    setStatus('未检测到修改', 'error');
    return;
  }

  const btn = document.getElementById('btn-save-settings');
  btn.disabled = true;
  btn.textContent = '保存中...';

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    providers = data.providers || providers;
    renderProviders();
    syncProviderFromSettings(providers);
    setStatus('已保存，即时生效', 'success');
  } catch (err) {
    setStatus(`保存失败: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

function setStatus(msg, cls) {
  const el = document.getElementById('settings-status');
  el.textContent = msg;
  el.className = 'settings-status ' + (cls || '');
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

export function initSettings() {
  document.getElementById('btn-settings').addEventListener('click', openModal);
  document.getElementById('btn-close-settings').addEventListener('click', closeModal);
  document.getElementById('btn-cancel-settings').addEventListener('click', closeModal);
  document.getElementById('btn-save-settings').addEventListener('click', saveSettings);

  document.getElementById('settings-modal').addEventListener('click', (e) => {
    if (e.target.classList.contains('modal-overlay')) closeModal();
  });
}
