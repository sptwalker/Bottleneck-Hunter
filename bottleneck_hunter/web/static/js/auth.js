/**
 * auth.js — 登录 / 注册页面交互逻辑
 */

(function () {
  'use strict';

  const errorEl = document.getElementById('auth-error');
  const successEl = document.getElementById('auth-success');
  const loginForm = document.getElementById('form-login');
  const registerForm = document.getElementById('form-register');

  /* ── Tab 切换 ──────────────────────────────────────── */
  document.querySelectorAll('.auth-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.auth-form').forEach(f => f.classList.remove('active'));
      tab.classList.add('active');
      const target = tab.dataset.tab;
      const form = document.getElementById(`form-${target}`);
      if (form) form.classList.add('active');
      hideMessages();
    });
  });

  function showError(msg) {
    errorEl.textContent = msg;
    errorEl.classList.add('visible');
    successEl.classList.remove('visible');
  }

  function showSuccess(msg) {
    successEl.textContent = msg;
    successEl.classList.add('visible');
    errorEl.classList.remove('visible');
  }

  function hideMessages() {
    errorEl.classList.remove('visible');
    successEl.classList.remove('visible');
  }

  /* ── 登录 ──────────────────────────────────────────── */
  loginForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideMessages();
    const btn = document.getElementById('btn-login');
    btn.disabled = true;
    btn.textContent = '登录中…';

    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('login-username').value.trim(),
          password: document.getElementById('login-password').value,
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        showSuccess('登录成功，正在跳转…');
        setTimeout(() => { window.location.href = '/'; }, 500);
      } else {
        const detail = data.detail;
        const msg = Array.isArray(detail)
          ? detail.map(e => e.msg || JSON.stringify(e)).join('; ')
          : (typeof detail === 'string' ? detail : '登录失败');
        showError(msg);
      }
    } catch (err) {
      showError('网络错误：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '登 录';
    }
  });

  /* ── 注册 ──────────────────────────────────────────── */
  registerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideMessages();
    const btn = document.getElementById('btn-register');
    btn.disabled = true;
    btn.textContent = '注册中…';

    try {
      const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('reg-username').value.trim(),
          password: document.getElementById('reg-password').value,
          display_name: document.getElementById('reg-display-name').value.trim(),
          invite_code: document.getElementById('reg-invite-code').value.trim(),
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        showSuccess('注册成功，正在跳转…');
        setTimeout(() => { window.location.href = '/'; }, 500);
      } else {
        const detail = data.detail;
        const msg = Array.isArray(detail)
          ? detail.map(e => e.msg || JSON.stringify(e)).join('; ')
          : (typeof detail === 'string' ? detail : '注册失败');
        showError(msg);
      }
    } catch (err) {
      showError('网络错误：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '注 册';
    }
  });

  /* ── 回车切换到密码框 ──────────────────────────────── */
  document.getElementById('login-username').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      document.getElementById('login-password').focus();
    }
  });

  /* ── 检查 URL 参数 ─────────────────────────────────── */
  const params = new URLSearchParams(window.location.search);
  if (params.get('expired') === '1') {
    showError('登录已过期，请重新登录');
  }
  if (params.get('tab') === 'register') {
    document.querySelector('.auth-tab[data-tab="register"]').click();
  }
})();
