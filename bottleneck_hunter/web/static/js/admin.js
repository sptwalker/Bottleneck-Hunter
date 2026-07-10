/**
 * admin.js — 管理后台面板（用户管理、邀请码、系统配置）
 */
import { showConfirm } from './utils/confirm.js';

let _currentTab = 'users';

export function initAdmin() {
  const btn = document.getElementById('btn-admin');
  if (!btn) return;

  // 仅 admin 角色可见（该按钮 data-view="admin"，app.js 通用导航负责 showView）
  const checkRole = () => {
    const user = window.appState?.user;
    btn.style.display = user?.role === 'admin' ? '' : 'none';
  };
  checkRole();
  setTimeout(checkRole, 1000);

  // 进入用户管理视图即加载当前子标签数据
  btn.addEventListener('click', () => switchTab(_currentTab || 'users'));

  // 视图内子标签切换
  document.querySelectorAll('#view-admin .admin-tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  // 用户详情抽屉关闭（点关闭按钮 / 点遮罩）
  document.getElementById('admin-user-drawer-close')?.addEventListener('click', closeUserDrawer);
  const drawer = document.getElementById('admin-user-drawer');
  drawer?.addEventListener('click', (e) => { if (e.target === drawer) closeUserDrawer(); });
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
  else if (tab === 'envtest') loadEnvTest();
}

// ── 用户管理 ──────────────────────────────────────────

async function loadUsers() {
  const body = document.getElementById('admin-users-body');
  if (!body) return;
  body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted)">加载中...</td></tr>';

  try {
    const res = await fetch('/api/admin/users');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderUsers(data.users || []);
  } catch (err) {
    body.innerHTML = `<tr><td colspan="9" style="color:var(--danger)">${esc(err.message)}</td></tr>`;
  }
}

function renderUsers(users) {
  const body = document.getElementById('admin-users-body');
  if (!body) return;
  if (!users.length) {
    body.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted)">暂无用户</td></tr>';
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
    const anaCount = u.analysis_count ?? '—';
    const aiCount = u.ai_config_count ?? '—';
    const created = u.created_at ? u.created_at.slice(0, 10) : '—';
    const lastLogin = u.last_login_at ? u.last_login_at.slice(0, 10) : '从未';

    // 操作按钮
    let actions = `<button class="btn btn-xs admin-action-btn" onclick="window._adminOverview('${u.id}','${esc(u.username)}')">详情</button>`;
    actions += `<button class="btn btn-xs admin-action-btn" onclick="window._adminEditLimit('${u.id}','${esc(u.username)}',${wlLimit})">设上限</button>`;
    const isSelf = u.id === window.appState?.user?.id;
    if (!isSelf) {
      if (frozen) {
        actions += `<button class="btn btn-xs admin-action-btn" onclick="window._adminUnfreeze('${u.id}')">解冻</button>`;
      } else {
        actions += `<button class="btn btn-xs admin-action-btn admin-action-warn" onclick="window._adminFreeze('${u.id}')">冻结</button>`;
      }
      actions += `<button class="btn btn-xs admin-action-btn admin-action-danger" onclick="window._adminDelete('${u.id}','${esc(u.username)}')">删除</button>`;
    }

    return `<tr class="${frozen ? 'admin-row-frozen' : ''}">
      <td>${esc(u.username)}${isSelf ? ' <span style="color:var(--muted);font-size:11px">(我)</span>' : ''}</td>
      <td>${esc(u.display_name || '')}</td>
      <td>${roleBadge}</td>
      <td>${statusBadge}</td>
      <td style="text-align:center">${wlDisplay}</td>
      <td style="text-align:center">${anaCount}</td>
      <td style="text-align:center">${aiCount}</td>
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


// ── 用户数据详情抽屉 ──────────────────────────────────

window._adminOverview = (userId, username) => openUserDrawer(userId, username);

function closeUserDrawer() {
  const d = document.getElementById('admin-user-drawer');
  if (d) d.style.display = 'none';
}

async function openUserDrawer(userId, username) {
  const drawer = document.getElementById('admin-user-drawer');
  const title = document.getElementById('admin-user-drawer-title');
  const body = document.getElementById('admin-user-drawer-body');
  if (!drawer || !body) return;
  drawer.style.display = '';
  if (title) title.textContent = `用户详情 · ${username}`;
  body.innerHTML = '<p class="admin-empty">加载中…</p>';
  try {
    const res = await fetch(`/api/admin/users/${userId}/overview`);
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || `HTTP ${res.status}`); }
    renderOverview(userId, await res.json(), body);
  } catch (err) {
    body.innerHTML = `<p class="admin-empty" style="color:var(--danger)">加载失败: ${esc(err.message)}</p>`;
  }
}

const MKT_LABEL = { us_stock: '美股', a_stock: 'A股', hk_stock: '港股' };
const PHASE_LABEL = { 1: '瓶颈', 2: '筛选', 3: '评分', 4: '验证', 5: '会议' };
const _mkt = (m) => MKT_LABEL[m] || m || '—';
const _num = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: d });
const _pct = (v) => (v == null || isNaN(v)) ? '—' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(2)}%`;
const _pnlCls = (v) => v > 0 ? 'admin-pos' : v < 0 ? 'admin-neg' : '';

function renderOverview(userId, ov, body) {
  const sec = [];

  // 产业链分析文件
  const ana = ov.analyses || [];
  let anaHtml = `<div class="admin-ov-sec"><h4>产业链分析文件 <span class="admin-ov-count">${ana.length}</span></h4>`;
  if (ana.length) {
    anaHtml += '<table class="admin-table admin-ov-table"><thead><tr><th>#</th><th>赛道</th><th>终端产品</th><th>市场</th><th>模型</th><th>深度</th><th>TopN</th><th>市值上限(亿)</th><th>进度</th><th>时间</th></tr></thead><tbody>' +
      ana.map(a => `<tr><td>${a.seq_no ?? '—'}</td><td>${esc(a.sector)}</td><td>${esc(a.end_product)}</td><td>${_mkt(a.market)}</td><td>${esc(a.model || a.provider || '—')}</td><td>${a.max_depth ?? '—'}</td><td>${a.top_n ?? '—'}</td><td>${a.max_market_cap_yi ?? '—'}</td><td>${PHASE_LABEL[a.completed_phases] || a.completed_phases || '—'}</td><td>${(a.created_at || '').slice(0, 10)}</td></tr>`).join('') +
      '</tbody></table>';
  } else { anaHtml += '<p class="admin-empty">暂无分析记录</p>'; }
  sec.push(anaHtml + '</div>');

  // 观察池
  const wl = ov.watchlist || { count_by_tier: {}, items: [] };
  const tc = wl.count_by_tier || {};
  let wlHtml = `<div class="admin-ov-sec"><h4>观察池 <span class="admin-ov-count">${(wl.items || []).length}</span></h4>`;
  wlHtml += `<p class="admin-ov-sub">重点 ${tc.focus ?? 0} · 一般 ${tc.normal ?? 0} · 跟踪 ${tc.track ?? 0}</p>`;
  if ((wl.items || []).length) {
    wlHtml += '<div class="admin-ov-chips">' + wl.items.map(i => `<span class="admin-chip">${esc(i.ticker)}<small>${esc(i.sector || '')}</small></span>`).join('') + '</div>';
  }
  sec.push(wlHtml + '</div>');

  // 模拟账户（分市场）
  const accs = ov.accounts || [];
  let accHtml = '<div class="admin-ov-sec"><h4>模拟账户（分市场）</h4>';
  if (accs.length) {
    accHtml += '<div class="admin-ov-cards">' + accs.map(a => `
      <div class="admin-ov-card">
        <div class="admin-ov-card-title">${_mkt(a.market)}</div>
        <div class="admin-ov-kv"><span>总价值</span><b>${_num(a.total_equity)}</b></div>
        <div class="admin-ov-kv"><span>收益率</span><b class="${_pnlCls(a.total_return_pct)}">${_pct(a.total_return_pct)}</b></div>
        <div class="admin-ov-kv"><span>现金</span><b>${_num(a.cash_balance)}</b></div>
        <div class="admin-ov-kv"><span>胜率</span><b>${_num(a.win_rate, 1)}%</b></div>
        <div class="admin-ov-kv"><span>交易数</span><b>${a.total_trades ?? 0}</b></div>
      </div>`).join('') + '</div>';
  } else { accHtml += '<p class="admin-empty">暂无模拟账户</p>'; }
  sec.push(accHtml + '</div>');

  // 持仓（分市场）
  const posGroups = (ov.positions || []).filter(g => (g.items || []).length);
  let posHtml = '<div class="admin-ov-sec"><h4>持仓（分市场）</h4>';
  if (posGroups.length) {
    posHtml += posGroups.map(g => `<div class="admin-ov-sub">${_mkt(g.market)}</div>` +
      '<table class="admin-table admin-ov-table"><thead><tr><th>Ticker</th><th>股数</th><th>成本</th><th>现价</th><th>市值</th><th>盈亏</th><th>占比</th></tr></thead><tbody>' +
      g.items.map(p => `<tr><td>${esc(p.ticker)}</td><td>${p.shares ?? '—'}</td><td>${_num(p.avg_cost)}</td><td>${_num(p.current_price)}</td><td>${_num(p.market_value)}</td><td class="${_pnlCls(p.unrealized_pnl)}">${_num(p.unrealized_pnl)}</td><td>${_num(p.weight_pct, 1)}%</td></tr>`).join('') +
      '</tbody></table>').join('');
  } else { posHtml += '<p class="admin-empty">暂无持仓</p>'; }
  sec.push(posHtml + '</div>');

  // AI 配置 + 拷贝
  const ai = ov.ai_config || [];
  const isSelf = userId === window.appState?.user?.id;
  let aiHtml = `<div class="admin-ov-sec"><h4>AI 配置 <span class="admin-ov-count">${ai.length}</span></h4>`;
  if (ai.length) {
    const byRole = {};
    ai.forEach(c => { (byRole[c.role_key] = byRole[c.role_key] || { label: c.role_label || c.role_key, slots: [] }).slots.push(c); });
    aiHtml += '<table class="admin-table admin-ov-table"><thead><tr>' + (isSelf ? '' : '<th></th>') + '<th>角色</th><th>模型配置</th></tr></thead><tbody>' +
      Object.entries(byRole).map(([rk, g]) => {
        const models = g.slots.sort((a, b) => (a.slot_index || 0) - (b.slot_index || 0)).map(s => `${esc(s.provider)}/${esc(s.model)}`).join('、');
        const cb = isSelf ? '' : `<td><input type="checkbox" class="admin-ai-cb" value="${esc(rk)}" checked></td>`;
        return `<tr>${cb}<td>${esc(g.label)}</td><td>${models}</td></tr>`;
      }).join('') + '</tbody></table>';
    aiHtml += isSelf
      ? '<p class="admin-empty">这是你自己的配置</p>'
      : `<div class="admin-ov-copy"><button class="btn btn-sm btn-primary" onclick="window._adminCopyAiConfig('${userId}')">拷贝勾选的配置到我的账户</button><span class="admin-cfg-status" id="admin-copy-status"></span></div>`;
  } else { aiHtml += '<p class="admin-empty">暂无 AI 配置</p>'; }
  sec.push(aiHtml + '</div>');

  body.innerHTML = sec.join('');
}

window._adminCopyAiConfig = async (sourceUserId) => {
  const status = document.getElementById('admin-copy-status');
  const roleKeys = Array.from(document.querySelectorAll('.admin-ai-cb')).filter(b => b.checked).map(b => b.value);
  if (!roleKeys.length) { if (status) status.textContent = '请至少勾选一个角色'; return; }
  if (!await showConfirm(`确定把选中的 ${roleKeys.length} 个角色配置拷贝到你自己的账户？将覆盖你的同名配置。`)) return;
  if (status) status.textContent = '拷贝中…';
  try {
    const res = await fetch('/api/admin/ai-config/copy-to-me', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_user_id: sourceUserId, role_keys: roleKeys }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    let msg = `✓ 已拷贝 ${data.copied} 条`;
    if (data.missing_provider?.length) msg += `；注意 ${data.missing_provider.join('、')} 你尚未配置 API Key，需去 AI 配置补 key`;
    if (status) { status.textContent = msg; status.className = 'admin-cfg-status success'; }
  } catch (err) {
    if (status) { status.textContent = `拷贝失败: ${err.message}`; status.className = 'admin-cfg-status error'; }
  }
};


// ── 服务器环境测试（临时诊断）────────────────────────
function loadEnvTest() {
  const btn = document.getElementById('envtest-run');
  const logEl = document.getElementById('envtest-log');
  const statusEl = document.getElementById('envtest-status');
  if (!btn || btn._bound) return;
  btn._bound = true;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    if (statusEl) { statusEl.textContent = '测试中…'; statusEl.className = 'admin-cfg-status'; }
    logEl.textContent = '正在从服务器探测各站点连通性…\n';
    try {
      const res = await fetch('/api/admin/env-test', { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const lines = [];
      lines.push('===== 服务器环境测试 =====');
      lines.push(`时间: ${data.env?.server_time || ''}   TZ: ${data.env?.tz || ''}`);
      lines.push(`当前代理(HTTPS_PROXY): ${data.env?.proxy || ''}`);
      lines.push(`NO_PROXY: ${data.env?.no_proxy || ''}`);
      lines.push('');
      lines.push('--- 境外站点（境内需代理才通）---');
      (data.results || []).filter(r => r.overseas).forEach(r => lines.push(_fmtEnvRow(r)));
      lines.push('');
      lines.push('--- 境内站点（对照，应始终通）---');
      (data.results || []).filter(r => !r.overseas).forEach(r => lines.push(_fmtEnvRow(r)));
      logEl.textContent = lines.join('\n');
      if (statusEl) { statusEl.textContent = '完成'; statusEl.className = 'admin-cfg-status success'; }
    } catch (err) {
      logEl.textContent += `\n测试失败: ${err.message}`;
      if (statusEl) { statusEl.textContent = '失败'; statusEl.className = 'admin-cfg-status error'; }
    } finally {
      btn.disabled = false;
    }
  });
}

function _fmtEnvRow(r) {
  // r.status>0 = 网络可达（收到 HTTP 响应，哪怕 401/403/429/404）；status=0 = 连不上（被墙/超时）
  const reach = r.status > 0 ? '✓ 网络可达' : '✗ 连不上';
  const st = r.status ? `HTTP ${r.status}` : (r.error || '连接失败/超时');
  return `${reach}  ${r.label}  —  ${st} (${r.ms}ms)
    ${r.url}`;
}


// ── Utils ─────────────────────────────────────────────

function esc(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
