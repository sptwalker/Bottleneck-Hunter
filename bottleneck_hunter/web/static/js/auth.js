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
      // 复位注册两阶段：切换 tab 时回到第一步
      const rf = document.getElementById('form-register');
      const vf = document.getElementById('form-verify');
      if (rf) rf.style.display = '';
      if (vf) vf.style.display = 'none';
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

  /* ── 注册（第一阶段：发验证码）───────────────────── */
  let pendingEmail = '';
  let resendTimer = null;
  const verifyForm = document.getElementById('form-verify');

  registerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideMessages();
    const btn = document.getElementById('btn-register');
    btn.disabled = true;
    btn.textContent = '发送中…';

    const email = document.getElementById('reg-email').value.trim();
    try {
      const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: document.getElementById('reg-username').value.trim(),
          password: document.getElementById('reg-password').value,
          email,
          display_name: document.getElementById('reg-display-name').value.trim(),
          invite_code: document.getElementById('reg-invite-code').value.trim(),
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.pending) {
        pendingEmail = data.email || email;
        document.getElementById('verify-hint').textContent =
          `验证码已发送至 ${pendingEmail}，请查收（10 分钟内有效）`;
        registerForm.style.display = 'none';
        verifyForm.style.display = 'block';
        showSuccess('验证码已发送，请查收邮箱');
        startResendCooldown();
      } else {
        showError(extractError(data, '注册失败'));
      }
    } catch (err) {
      showError('网络错误：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '获取验证码';
    }
  });

  /* ── 注册（第二阶段：验证码 → 建号）───────────────── */
  verifyForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    hideMessages();
    const btn = document.getElementById('btn-verify');
    btn.disabled = true;
    btn.textContent = '验证中…';
    try {
      const resp = await fetch('/api/auth/verify-registration', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: pendingEmail,
          code: document.getElementById('verify-code').value.trim(),
        }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        showSuccess('注册成功，正在跳转…');
        setTimeout(() => { window.location.href = '/'; }, 500);
      } else {
        showError(extractError(data, '验证失败'));
      }
    } catch (err) {
      showError('网络错误：' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '完成注册';
    }
  });

  /* ── 重发验证码（60s 冷却）───────────────────────── */
  document.getElementById('btn-resend').addEventListener('click', async () => {
    hideMessages();
    try {
      const resp = await fetch('/api/auth/resend-code', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: pendingEmail, purpose: 'register' }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) { showSuccess('验证码已重新发送'); startResendCooldown(); }
      else showError(extractError(data, '重发失败'));
    } catch (err) { showError('网络错误：' + err.message); }
  });

  document.getElementById('btn-back-register').addEventListener('click', () => {
    verifyForm.style.display = 'none';
    registerForm.style.display = 'block';
    hideMessages();
    if (resendTimer) { clearInterval(resendTimer); resendTimer = null; }
  });

  function startResendCooldown() {
    const btn = document.getElementById('btn-resend');
    let left = 60;
    btn.disabled = true;
    if (resendTimer) clearInterval(resendTimer);
    const tick = () => {
      btn.textContent = `重新发送 (${left}s)`;
      if (left <= 0) { clearInterval(resendTimer); resendTimer = null; btn.disabled = false; btn.textContent = '重新发送验证码'; }
      left -= 1;
    };
    tick();
    resendTimer = setInterval(tick, 1000);
  }

  function extractError(data, fallback) {
    const detail = data && data.detail;
    if (Array.isArray(detail)) return detail.map(e => e.msg || JSON.stringify(e)).join('; ');
    return typeof detail === 'string' ? detail : fallback;
  }

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
