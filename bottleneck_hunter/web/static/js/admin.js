/**
 * admin.js — 管理后台面板（用户管理、邀请码、系统配置）
 */

let _currentTab = 'users';

export function initAdmin() {
  const btn = document.getElementById('btn-admin');
  if (!btn) return;

  // 仅 admin 角色可见
  const checkRole = () => {
    const user = window.appState?.user;
    if (user?.role === 'admin') {
      btn.style.display = '';
    } else {
      btn.style.display = 'none';
    }
  };
  // initAuth 可能还没完成，延迟检查
  checkRole();
  setTimeout(checkRole, 1000);

  btn.addEventListener('click', openAdmin);
}

function openAdmin() {
  const modal = document.getElementById('admin-modal');
  if (!modal) return;
  modal.style.display = '';
  switchTab('users');

  // 关闭按钮
  document.getElementById('admin-close')?.addEventListener('click', closeAdmin);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeAdmin();
  });

  // Tab 切换
  modal.querySelectorAll('.admin-tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
}

function closeAdmin() {
  const modal = document.getElementById('admin-modal');
  if (modal) modal.style.display = 'none';
}

function switchTab(tab) {
  _currentTab = tab;
  document.querySelectorAll('.admin-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.admin-tab-pane').forEach(p =>
    p.classList.toggle('active', p.id === `admin-tab-${tab}`));

  if (tab === 'users') loadUsers();
  else if (tab === 'invites') loadInviteCodes();
  else if (tab === 'config') loadConfig();
}

// ── 用户管理 ──────────────────────────────────────────

async function loadUsers() {
  const body = document.getElementById('admin-users-body');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">加载中...</td></tr>';

  try {
    const res = await fetch('/api/admin/users');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUsers(data.users || []);
  } catch (err) {
    body.innerHTML = `<tr><td colspan="7" style="color:var(--danger)">${esc(err.message)}</td></tr>`;
  }
}

function renderUsers(users) {
  const body = document.getElementById('admin-users-body');
  if (!body) return;
  if (!users.length) {
    body.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">暂无用户</td></tr>';
    return;
  }

  body.innerHTML = users.map(u => {
    const isAdmin = u.role === 'admin';
    const frozen = u.is_active === false || u.is_active === 0;
    const roleBadge = isAdmin
      ? '<span class="admin-badge admin-badge-admin">管理员</span>'
      : '<span class="admin-badge admin-badge-user">用户</span>';
    const statusBadge = frozen
      ? '<span class="admin-badge admin-badge-frozen">已冻结</span>'
      : '<span class="admin-badge admin-badge-active">活跃</span>';
    const wlCount = u.watchlist_count ?? '—';
    const created = u.created_at ? u.created_at.slice(0, 10) : '—';
    const lastLogin = u.last_login_at ? u.last_login_at.slice(0, 10) : '从未';

    // 操作按钮
    let actions = '';
    const isSelf = u.id === window.appState?.user?.id;
    if (!isSelf) {
      if (frozen) {
        actions += `<button class="btn btn-xs admin-action-btn" onclick="window._adminUnfreeze('${u.id}')">解冻</button>`;
      } else {
        actions += `<button class="btn btn-xs admin-action-btn admin-action-warn" onclick="window._adminFreeze('${u.id}')">冻结</button>`;
      }
      actions += `<button class="btn btn-xs admin-action-btn admin-action-danger" onclick="window._adminDelete('${u.id}','${esc(u.username)}')">删除</button>`;
    } else {
      actions = '<span style="color:var(--muted);font-size:12px">当前用户</span>';
    }

    return `<tr class="${frozen ? 'admin-row-frozen' : ''}">
      <td>${esc(u.username)}</td>
      <td>${esc(u.display_name || '')}</td>
      <td>${roleBadge}</td>
      <td>${statusBadge}</td>
      <td style="text-align:center">${wlCount}</td>
      <td>${created}<br><span style="font-size:11px;color:var(--muted)">最近: ${lastLogin}</span></td>
      <td class="admin-actions-cell">${actions}</td>
    </tr>`;
  }).join('');

  // 统计
  const statsEl = document.getElementById('admin-user-stats');
  if (statsEl) {
    const total = users.length;
    const active = users.filter(u => u.is_active !== false && u.is_active !== 0).length;
    const admins = users.filter(u => u.role === 'admin').length;
    statsEl.textContent = `共 ${total} 用户 · ${active} 活跃 · ${admins} 管理员`;
  }
}

// 全局回调
window._adminFreeze = async (userId) => {
  if (!confirm('确定冻结该用户？冻结后用户将无法登录。')) return;
  try {
    const res = await fetch(`/api/admin/users/${userId}/freeze`, { method: 'POST' });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || res.status); }
    loadUsers();
  } catch (err) { alert(`操作失败: ${err.message}`); }
};

window._adminUnfreeze = async (userId) => {
  try {
    const res = await fetch(`/api/admin/users/${userId}/unfreeze`, { method: 'POST' });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || res.status); }
    loadUsers();
  } catch (err) { alert(`操作失败: ${err.message}`); }
};

window._adminDelete = async (userId, username) => {
  if (!confirm(`确定删除用户 "${username}"？\n\n此操作将删除该用户的全部数据（观察池、分析记录、API KEY），且不可恢复！`)) return;
  try {
    const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || res.status); }
    loadUsers();
  } catch (err) { alert(`删除失败: ${err.message}`); }
};


// ── 邀请码管理 ────────────────────────────────────────

async function loadInviteCodes() {
  const body = document.getElementById('admin-invites-body');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted)">加载中...</td></tr>';

  try {
    const res = await fetch('/api/admin/invite-codes');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderInviteCodes(data.codes || []);
  } catch (err) {
    body.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">${esc(err.message)}</td></tr>`;
  }

  // 生成按钮
  const genBtn = document.getElementById('admin-gen-invites');
  if (genBtn && !genBtn._bound) {
    genBtn._bound = true;
    genBtn.addEventListener('click', async () => {
      const countInput = document.getElementById('admin-invite-count');
      const count = parseInt(countInput?.value || '5');
      genBtn.disabled = true;
      genBtn.textContent = '生成中...';
      try {
        const res = await fetch('/api/admin/invite-codes', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ count, expires_days: 30 }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        loadInviteCodes();
      } catch (err) {
        alert(`生成失败: ${err.message}`);
      } finally {
        genBtn.disabled = false;
        genBtn.textContent = '批量生成';
      }
    });
  }
}

function renderInviteCodes(codes) {
  const body = document.getElementById('admin-invites-body');
  if (!body) return;
  if (!codes.length) {
    body.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted)">暂无邀请码</td></tr>';
    return;
  }

  body.innerHTML = codes.map(c => {
    let statusBadge;
    if (c.used_by) {
      statusBadge = '<span class="admin-badge admin-badge-used">已使用</span>';
    } else if (!c.is_active) {
      statusBadge = '<span class="admin-badge admin-badge-revoked">已作废</span>';
    } else if (c.expires_at && c.expires_at < new Date().toISOString()) {
      statusBadge = '<span class="admin-badge admin-badge-expired">已过期</span>';
    } else {
      statusBadge = '<span class="admin-badge admin-badge-available">可用</span>';
    }

    const created = c.created_at ? c.created_at.slice(0, 10) : '—';
    const expires = c.expires_at ? c.expires_at.slice(0, 10) : '永不';
    const usedBy = c.used_by || '—';
    const canRevoke = !c.used_by && c.is_active;
    const revokeBtn = canRevoke
      ? `<button class="btn btn-xs admin-action-btn admin-action-warn" onclick="window._adminRevokeCode('${esc(c.code)}')">作废</button>`
      : '';

    return `<tr>
      <td><code class="admin-code">${esc(c.code)}</code></td>
      <td>${statusBadge}</td>
      <td>${created} ~ ${expires}</td>
      <td>${usedBy}</td>
      <td>${revokeBtn}</td>
    </tr>`;
  }).join('');

  // 统计
  const statsEl = document.getElementById('admin-invite-stats');
  if (statsEl) {
    const total = codes.length;
    const available = codes.filter(c => c.is_active && !c.used_by).length;
    const used = codes.filter(c => c.used_by).length;
    statsEl.textContent = `共 ${total} 个 · ${available} 可用 · ${used} 已使用`;
  }
}

window._adminRevokeCode = async (code) => {
  if (!confirm(`确定作废邀请码 ${code}？`)) return;
  try {
    const res = await fetch(`/api/admin/invite-codes/${code}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    loadInviteCodes();
  } catch (err) { alert(`操作失败: ${err.message}`); }
};


// ── 系统配置 ──────────────────────────────────────────

async function loadConfig() {
  const pane = document.getElementById('admin-tab-config');
  if (!pane) return;

  try {
    // 加载配置和统计
    const [configRes, statsRes] = await Promise.all([
      fetch('/api/admin/config'),
      fetch('/api/admin/stats'),
    ]);
    const config = configRes.ok ? await configRes.json() : {};
    const stats = statsRes.ok ? await statsRes.json() : {};

    // 渲染统计
    renderStats(stats);

    // 渲染配置
    const regToggle = document.getElementById('admin-cfg-registration');
    if (regToggle) regToggle.checked = !!config.open_registration;

    const limitInput = document.getElementById('admin-cfg-wl-limit');
    if (limitInput) limitInput.value = config.default_watchlist_limit || 24;

    // 保存按钮
    const saveBtn = document.getElementById('admin-cfg-save');
    if (saveBtn && !saveBtn._bound) {
      saveBtn._bound = true;
      saveBtn.addEventListener('click', saveConfig);
    }
  } catch (err) {
    console.error('加载配置失败:', err);
  }
}

function renderStats(stats) {
  const el = document.getElementById('admin-stats-grid');
  if (!el) return;
  el.innerHTML = `
    <div class="admin-stat-card">
      <div class="admin-stat-value">${stats.total_users ?? 0}</div>
      <div class="admin-stat-label">注册用户</div>
    </div>
    <div class="admin-stat-card">
      <div class="admin-stat-value">${stats.total_watchlist ?? 0}</div>
      <div class="admin-stat-label">观察池条目</div>
    </div>
    <div class="admin-stat-card">
      <div class="admin-stat-value">${stats.total_analyses ?? 0}</div>
      <div class="admin-stat-label">分析记录</div>
    </div>
    <div class="admin-stat-card">
      <div class="admin-stat-value">${stats.total_invites ?? 0}</div>
      <div class="admin-stat-label">邀请码</div>
    </div>
  `;
}

async function saveConfig() {
  const regToggle = document.getElementById('admin-cfg-registration');
  const limitInput = document.getElementById('admin-cfg-wl-limit');
  const statusEl = document.getElementById('admin-cfg-status');

  const body = {
    open_registration: regToggle?.checked ?? false,
    default_watchlist_limit: parseInt(limitInput?.value || '24'),
  };

  try {
    const res = await fetch('/api/admin/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = '已保存';
      statusEl.className = 'admin-cfg-status success';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
    }
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = `保存失败: ${err.message}`;
      statusEl.className = 'admin-cfg-status error';
    }
  }
}


// ── Utils ─────────────────────────────────────────────

function esc(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
