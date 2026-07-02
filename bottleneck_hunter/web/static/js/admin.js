/**
 * admin.js — 管理后台面板（用户管理、邀请码、系统配置）
 */
import { showConfirm } from './utils/confirm.js';

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
    const wlLimit = u.watchlist_limit ?? 24;
    const wlDisplay = `${wlCount} / ${wlLimit}`;
    const created = u.created_at ? u.created_at.slice(0, 10) : '—';
    const lastLogin = u.last_login_at ? u.last_login_at.slice(0, 10) : '从未';

    // 操作按钮
    let actions = `<button class="btn btn-xs admin-action-btn" onclick="window._adminEditLimit('${u.id}','${esc(u.username)}',${wlLimit})">设上限</button>`;
    const isSelf = u.id === window.appState?.user?.id;
    if (!isSelf) {
      if (frozen) {
        actions += `<button class="btn btn-xs admin-action-btn" onclick="window._adminUnfreeze('${u.id}')">解冻</button>`;
      } else {
        actions += `<button class="btn btn-xs admin-action-btn admin-action-warn" onclick="window._adminFreeze('${u.id}')">冻结</button>`;
      }
      actions += `<button class="btn btn-xs admin-action-btn admin-action-danger" onclick="window._adminDelete('${u.id}','${esc(u.username)}')">删除</button>`;
    } else {
      actions += '<span style="color:var(--muted);font-size:12px;margin-left:6px">（当前用户）</span>';
    }

    return `<tr class="${frozen ? 'admin-row-frozen' : ''}">
      <td>${esc(u.username)}</td>
      <td>${esc(u.display_name || '')}</td>
      <td>${roleBadge}</td>
      <td>${statusBadge}</td>
      <td style="text-align:center">${wlDisplay}</td>
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
  if (!await showConfirm('确定冻结该用户？冻结后用户将无法登录。', { danger: true })) return;
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
  if (!await showConfirm(`确定删除用户 "${username}"？\n\n此操作将删除该用户的全部数据（观察池、分析记录、API KEY），且不可恢复！`, { danger: true })) return;
  try {
    const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || res.status); }
    loadUsers();
  } catch (err) { alert(`删除失败: ${err.message}`); }
};

window._adminEditLimit = async (userId, username, current) => {
  const val = prompt(`设置用户 "${username}" 的观察池上限（股票数）：`, String(current ?? 24));
  if (val === null) return;
  const limit = parseInt(val, 10);
  if (!Number.isInteger(limit) || limit < 1 || limit > 500) { alert('请输入 1–500 之间的整数'); return; }
  try {
    const res = await fetch(`/api/admin/users/${userId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ watchlist_limit: limit }),
    });
    if (!res.ok) { const d = await res.json(); throw new Error(d.detail || res.status); }
    loadUsers();
  } catch (err) { alert(`设置失败: ${err.message}`); }
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
  if (!await showConfirm(`确定作废邀请码 ${code}？`)) return;
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

    // 分档比例（后端存 0-1 小数，UI 用百分数）
    const focusInput = document.getElementById('admin-cfg-focus-pct');
    const normalInput = document.getElementById('admin-cfg-normal-pct');
    if (focusInput) focusInput.value = Math.round((config.tier_focus_pct ?? 0.25) * 100);
    if (normalInput) normalInput.value = Math.round((config.tier_normal_pct ?? 0.25) * 100);
    updateTierPreview();
    [limitInput, focusInput, normalInput].forEach(el => {
      if (el && !el._previewBound) { el._previewBound = true; el.addEventListener('input', updateTierPreview); }
    });

    // 保存按钮
    const saveBtn = document.getElementById('admin-cfg-save');
    if (saveBtn && !saveBtn._bound) {
      saveBtn._bound = true;
      saveBtn.addEventListener('click', saveConfig);
    }

    // SMTP 配置
    await loadSmtpConfig();
    const smtpSave = document.getElementById('admin-smtp-save');
    if (smtpSave && !smtpSave._bound) { smtpSave._bound = true; smtpSave.addEventListener('click', saveSmtpConfig); }
    const smtpTest = document.getElementById('admin-smtp-test');
    if (smtpTest && !smtpTest._bound) { smtpTest._bound = true; smtpTest.addEventListener('click', testSmtp); }
  } catch (err) {
    console.error('加载配置失败:', err);
  }
}

async function loadSmtpConfig() {
  try {
    const res = await fetch('/api/admin/smtp-config');
    if (!res.ok) return;
    const c = await res.json();
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    set('admin-smtp-host', c.host || '');
    set('admin-smtp-port', c.port || 587);
    set('admin-smtp-user', c.user || '');
    set('admin-smtp-from', c.sender || '');
    const tls = document.getElementById('admin-smtp-tls');
    if (tls) tls.checked = c.use_tls !== false;
    const pw = document.getElementById('admin-smtp-password');
    if (pw) pw.placeholder = c.password_set ? '已设置（留空保持不变）' : '未设置';
    const src = document.getElementById('admin-smtp-source');
    if (src) {
      src.textContent = c.configured
        ? (c.source === 'db' ? '当前使用：后台配置' : '当前使用：环境变量 (.env) 兜底')
        : '⚠ 尚未配置 SMTP，验证码将打印到服务器日志';
    }
  } catch (err) { console.error('加载 SMTP 配置失败:', err); }
}

async function saveSmtpConfig() {
  const status = document.getElementById('admin-smtp-status');
  const body = {
    host: document.getElementById('admin-smtp-host')?.value.trim() || '',
    port: parseInt(document.getElementById('admin-smtp-port')?.value || '587'),
    user: document.getElementById('admin-smtp-user')?.value.trim() || '',
    sender: document.getElementById('admin-smtp-from')?.value.trim() || '',
    use_tls: document.getElementById('admin-smtp-tls')?.checked ?? true,
  };
  const pw = document.getElementById('admin-smtp-password')?.value || '';
  if (pw) body.password = pw;  // 空则不改
  try {
    const res = await fetch('/api/admin/smtp-config', {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (!res.ok) { let d = `HTTP ${res.status}`; try { d = (await res.json()).detail || d; } catch { /* */ } throw new Error(d); }
    const pwEl = document.getElementById('admin-smtp-password'); if (pwEl) pwEl.value = '';
    setCfgStatus(status, '已保存', true);
    loadSmtpConfig();
  } catch (err) { setCfgStatus(status, `保存失败: ${err.message}`, false); }
}

async function testSmtp() {
  const status = document.getElementById('admin-smtp-status');
  const to = prompt('发送测试邮件到哪个邮箱？', document.getElementById('admin-smtp-user')?.value || '');
  if (!to) return;
  setCfgStatus(status, '发送中…', true);
  try {
    const res = await fetch('/api/admin/smtp-test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ to_email: to.trim() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    setCfgStatus(status, data.message || '测试邮件已发送', true);
  } catch (err) { setCfgStatus(status, `测试失败: ${err.message}`, false); }
}

function setCfgStatus(el, msg, ok) {
  if (!el) return;
  el.textContent = msg;
  el.className = 'admin-cfg-status ' + (ok ? 'success' : 'error');
  if (ok) setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 4000);
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

// 与后端 tier_limits.derive_tier_caps 同款：由总数 + 比例推导三档（track 取剩余）
function deriveTierCaps(total, focusPct, normalPct) {
  total = Math.max(0, Math.floor(total || 0));
  focusPct = Math.max(0, focusPct || 0);
  normalPct = Math.max(0, normalPct || 0);
  if (focusPct + normalPct > 1) { const s = 1 / (focusPct + normalPct); focusPct *= s; normalPct *= s; }
  const focus = Math.min(total, Math.round(total * focusPct));
  const normal = Math.min(total - focus, Math.round(total * normalPct));
  return { focus, normal, track: total - focus - normal };
}

function updateTierPreview() {
  const total = parseInt(document.getElementById('admin-cfg-wl-limit')?.value || '24');
  const fp = parseFloat(document.getElementById('admin-cfg-focus-pct')?.value || '25') / 100;
  const np = parseFloat(document.getElementById('admin-cfg-normal-pct')?.value || '25') / 100;
  const el = document.getElementById('admin-cfg-tier-preview');
  if (!el) return;
  if (fp + np >= 1) { el.textContent = '⚠ 重点+一般 需 < 100%'; el.className = 'admin-cfg-status error'; return; }
  const c = deriveTierCaps(total, fp, np);
  el.textContent = `重点 ${c.focus} / 一般 ${c.normal} / 跟踪 ${c.track}`;
  el.className = 'admin-cfg-status';
}

async function saveConfig() {
  const regToggle = document.getElementById('admin-cfg-registration');
  const limitInput = document.getElementById('admin-cfg-wl-limit');
  const focusInput = document.getElementById('admin-cfg-focus-pct');
  const normalInput = document.getElementById('admin-cfg-normal-pct');
  const statusEl = document.getElementById('admin-cfg-status');

  const body = {
    open_registration: regToggle?.checked ?? false,
    default_watchlist_limit: parseInt(limitInput?.value || '24'),
    tier_focus_pct: parseFloat(focusInput?.value || '25') / 100,
    tier_normal_pct: parseFloat(normalInput?.value || '25') / 100,
  };

  try {
    const res = await fetch('/api/admin/config', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
      throw new Error(detail);
    }
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
