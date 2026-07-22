/**
 * account.js — 账户设置抽屉：查看账户信息、改密码、改邮箱（需验证新地址）。
 * 自包含模块，不依赖 app.js 内部实现，仅用 fetch + DOM。
 */

(function () {
  'use strict';

  const drawer = document.getElementById('account-drawer');
  const openBtn = document.getElementById('btn-account');
  const closeBtn = document.getElementById('account-drawer-close');
  if (!drawer || !openBtn) return;

  function setStatus(el, msg, ok) {
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' success' : ' error') : '');
  }

  function errOf(data, fallback) {
    const d = data && data.detail;
    if (Array.isArray(d)) return d.map(e => e.msg || JSON.stringify(e)).join('; ');
    return typeof d === 'string' ? d : fallback;
  }

  async function loadAccount() {
    try {
      const resp = await fetch('/api/auth/me');
      if (!resp.ok) return;
      const u = await resp.json();
      document.getElementById('acc-username').textContent = u.username || '—';
      document.getElementById('acc-email').textContent = u.email || '（未设置）';
      const limit = u.watchlist_limit ?? 24;
      const byMkt = u.watchlist_count_by_market || {};
      document.getElementById('acc-watchlist').textContent =
        `美股 ${byMkt.us_stock ?? 0}/${limit} · A股 ${byMkt.a_stock ?? 0}/${limit}`;
      document.getElementById('acc-role').textContent = u.role === 'admin' ? '管理员' : '普通用户';
    } catch (e) { console.warn('加载账户信息失败', e); }
  }

  function openDrawer() {
    resetForms();
    loadAccount();
    drawer.style.display = 'flex';
  }
  function closeDrawer() { drawer.style.display = 'none'; }

  function resetForms() {
    ['acc-old-pw', 'acc-new-pw', 'acc-new-email', 'acc-email-pw', 'acc-email-code'].forEach(id => {
      const el = document.getElementById(id); if (el) el.value = '';
    });
    setStatus(document.getElementById('acc-pw-status'), '');
    setStatus(document.getElementById('acc-email-status'), '');
    setStatus(document.getElementById('acc-email-status2'), '');
    document.getElementById('acc-email-step1').style.display = '';
    document.getElementById('acc-email-step2').style.display = 'none';
  }

  openBtn.addEventListener('click', openDrawer);
  closeBtn.addEventListener('click', closeDrawer);
  drawer.addEventListener('click', (e) => { if (e.target === drawer) closeDrawer(); });

  /* ── 退出登录 ─────────────────────────────────── */
  const logoutBtn = document.getElementById('acc-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      logoutBtn.disabled = true;
      setStatus(document.getElementById('acc-logout-status'), '正在退出…', true);
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } catch (_) { /* 忽略：无论成功与否都跳登录页 */ }
      window.location.href = '/login';
    });
  }

  /* ── 改密码 ─────────────────────────────────── */
  document.getElementById('acc-change-pw').addEventListener('click', async () => {
    const status = document.getElementById('acc-pw-status');
    const oldPw = document.getElementById('acc-old-pw').value;
    const newPw = document.getElementById('acc-new-pw').value;
    if (!oldPw || !newPw) { setStatus(status, '请填写完整', false); return; }
    if (newPw.length < 8) { setStatus(status, '新密码至少8位', false); return; }
    try {
      const resp = await fetch('/api/auth/change-password', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        setStatus(status, '密码已修改', true);
        document.getElementById('acc-old-pw').value = '';
        document.getElementById('acc-new-pw').value = '';
      } else setStatus(status, errOf(data, '修改失败'), false);
    } catch (e) { setStatus(status, '网络错误：' + e.message, false); }
  });

  /* ── 改邮箱：第一步 发码 ───────────────────── */
  document.getElementById('acc-request-email').addEventListener('click', async () => {
    const status = document.getElementById('acc-email-status');
    const newEmail = document.getElementById('acc-new-email').value.trim();
    const pw = document.getElementById('acc-email-pw').value;
    if (!newEmail || !pw) { setStatus(status, '请填写新邮箱和当前密码', false); return; }
    try {
      const resp = await fetch('/api/auth/request-email-change', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ new_email: newEmail, password: pw }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        setStatus(status, '', true);
        document.getElementById('acc-email-step1').style.display = 'none';
        document.getElementById('acc-email-step2').style.display = '';
        setStatus(document.getElementById('acc-email-status2'), `验证码已发送至 ${newEmail}`, true);
      } else setStatus(status, errOf(data, '发送失败'), false);
    } catch (e) { setStatus(status, '网络错误：' + e.message, false); }
  });

  /* ── 改邮箱：第二步 确认 ───────────────────── */
  document.getElementById('acc-confirm-email').addEventListener('click', async () => {
    const status = document.getElementById('acc-email-status2');
    const code = document.getElementById('acc-email-code').value.trim();
    if (!code) { setStatus(status, '请输入验证码', false); return; }
    try {
      const resp = await fetch('/api/auth/confirm-email-change', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
      });
      const data = await resp.json();
      if (resp.ok && data.ok) {
        setStatus(status, '邮箱已更新', true);
        loadAccount();
        setTimeout(resetForms, 1200);
      } else setStatus(status, errOf(data, '验证失败'), false);
    } catch (e) { setStatus(status, '网络错误：' + e.message, false); }
  });

  document.getElementById('acc-email-cancel').addEventListener('click', () => {
    document.getElementById('acc-email-step1').style.display = '';
    document.getElementById('acc-email-step2').style.display = 'none';
    setStatus(document.getElementById('acc-email-status2'), '');
  });
})();
