/**
 * vip.js — VIP 私人财务顾问工作台：多页签（账户仪表盘 / 导入中心 / 最近持仓 / 历史交易 / 报告复盘 / 咨询解读）。
 * 复用模拟交易页的 activeTab/tabLoaded/switchTab 骨架；fetch + DOM + 全局 echarts，无第三方依赖。
 */
import { showConfirm, showChoice } from './utils/confirm.js';
import { fmtBJ } from './wizard-state.js';

const VIP_ALL_ACCOUNTS = '__all__';
const VIP_PRIMARY_OVERVIEW = 'overview';
const VIP_PRIMARY_IMPORT = 'import';
const VIP_ACCOUNT_SUBTABS = [
  { key: 'dashboard', label: '账户总览' },
  { key: 'positions', label: '最近持仓' },
  { key: 'history', label: '历史交易' },
  { key: 'mandate', label: '投资纲领' },
  { key: 'advisory', label: '顾问决策' },
  { key: 'recommend', label: '荐新' },
  { key: 'reports', label: '报告复盘' },
  { key: 'chat', label: '咨询解读' },
];
const vipState = {
  primaryTab: VIP_PRIMARY_OVERVIEW,
  activeTab: 'dashboard',
  accountSubtab: 'dashboard',
  shownKey: {},   // activeTab → 当前面板已渲染的 cacheKey（面板在各账户页签间共用，切换必须比对重渲）
  sessionId: '',
  inited: false,
  charts: {},
  accounts: [],
  accountsError: '',
};

function market() { return document.getElementById('vip-market')?.value || 'us_stock'; }
function selectedAccountOption() { return (document.getElementById('vip-account-ref')?.value || VIP_ALL_ACCOUNTS).trim(); }
function isAllAccountsSelected() { return selectedAccountOption() === VIP_ALL_ACCOUNTS; }
function selectedAccountRef() { return isAllAccountsSelected() ? '' : selectedAccountOption(); }
function accountRef() { return selectedAccountRef(); }
function selectedDashboardScope() { return isAllAccountsSelected() ? 'all' : 'account'; }
function selectedImportAccountRef() { return (document.getElementById('vip-import-account')?.value || '').trim(); }
function activeAccountLabel() {
  const select = document.getElementById('vip-account-ref');
  return select?.options[select.selectedIndex]?.text || '全部账户';
}
function primaryTabKeyForAccount(ref) { return `account:${ref}`; }
function isAccountPrimaryTab() { return vipState.primaryTab.startsWith('account:'); }
function currentPrimaryAccountRef() { return isAccountPrimaryTab() ? vipState.primaryTab.slice('account:'.length) : ''; }
function currentAccount() {
  const ref = currentPrimaryAccountRef() || selectedAccountRef();
  return (vipState.accounts || []).find(a => (a.account_ref || '') === ref) || null;
}
function setSelectedAccountRef(ref) {
  const select = document.getElementById('vip-account-ref');
  if (!select) return;
  select.value = ref ? ref : VIP_ALL_ACCOUNTS;
}
function currentTabCacheKey() {
  if (vipState.activeTab === 'dashboard') return `dashboard|${selectedDashboardScope()}|${selectedAccountRef()}`;
  if (vipState.activeTab === 'import') return `import|${selectedImportAccountRef()}`;
  return `${vipState.activeTab}|${selectedAccountRef()}`;
}

function explainVipHttpError(status, detail = '') {
  if (status === 401) return detail || '401（请先登录当前服务）';
  if (status === 404) return detail || '404：接口不存在——若刚更新代码，请重启服务（bottleneck-hunter serve）后强制刷新页面';
  return detail ? `${status}（${detail}）` : `${status}`;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtNum(n, d = 2) {
  if (n == null || isNaN(n)) return '--';
  return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
}
function setStatus(id, msg, ok) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'account-status' + (msg ? (ok ? ' success' : ' error') : '');
}
function vipApiUrl(path) {
  const [base, rawQuery = ''] = path.split('?');
  const params = new URLSearchParams(rawQuery);
  params.set('market', market());
  const qs = params.toString();
  return `/api/vip${base}${qs ? `?${qs}` : ''}`;
}
async function vipGet(path) {
  const r = await fetch(vipApiUrl(path));
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(explainVipHttpError(r.status, data.detail || ''));
  }
  return r.json();
}
function vipAccountLabel(a) {
  const name = a.display_name || a.account_ref || '未命名账户';
  const kind = a.account_kind === 'bank' ? '银行' : '券商';
  const inst = a.institution_name ? ` · ${a.institution_name}` : '';
  const suffix = a.is_default ? '（首选）' : '';
  return `${name} · ${kind}${inst}${suffix}`;
}
function clearVipContext() {
  vipState.sessionId = '';
  const report = document.getElementById('vip-report');
  if (report) report.innerHTML = '';
  const viewer = document.getElementById('vip-report-viewer');
  if (viewer) viewer.innerHTML = '';
  const log = document.getElementById('vip-chat-log');
  if (log) log.innerHTML = '';
}
function requireConcreteAccount(tab) {
  if (!isAllAccountsSelected()) return false;
  const map = {
    positions: '请先进入具体子账户，再查看最近持仓。',
    history: '请先进入具体子账户，再查看历史交易。',
    mandate: '请先进入具体子账户，再设定该账户的投资纲领。',
    advisory: '请先进入具体子账户，再生成该账户的顾问决策建议。',
    recommend: '请先进入具体子账户，再生成该账户的荐新建议。',
    reports: '请先进入具体子账户，再生成或查看报告。',
    chat: '请先进入具体子账户，再进行咨询解读。',
  };
  const pane = document.getElementById(`vip-pane-${tab}`);
  if (!pane) return true;
  pane.querySelectorAll('tbody').forEach(el => { el.innerHTML = ''; });
  pane.querySelectorAll('.st-empty-hint').forEach(el => { el.style.display = 'none'; });
  const ids = ['vip-report-list', 'vip-report-viewer', 'vip-report', 'vip-chat-log'];
  ids.forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = '';
  });
  let hint = pane.querySelector('.vip-scope-hint');
  if (!hint) {
    hint = document.createElement('p');
    hint.className = 'st-empty-hint vip-scope-hint';
    pane.prepend(hint);
  }
  hint.textContent = map[tab] || '请先进入具体子账户继续。';
  hint.style.display = '';
  return true;
}
function clearScopeHint(tab) {
  const pane = document.getElementById(`vip-pane-${tab}`);
  pane?.querySelectorAll('.vip-scope-hint').forEach(el => { el.style.display = 'none'; });
}
function setPrimaryTab(tab, { statusMsg = '', resetContext = true } = {}) {
  vipState.primaryTab = tab;
  if (tab === VIP_PRIMARY_OVERVIEW) {
    setSelectedAccountRef('');
    vipState.activeTab = 'dashboard';
  } else if (tab === VIP_PRIMARY_IMPORT) {
    vipState.activeTab = 'import';
  } else if (tab.startsWith('account:')) {
    const ref = tab.slice('account:'.length);
    setSelectedAccountRef(ref);
    vipState.activeTab = vipState.accountSubtab || 'dashboard';
  }
  if (resetContext) clearVipContext();
  renderPrimaryTabs();
  renderAccountSubtabs();
  updateAccountHeading();
  showActivePane();
  if (statusMsg) setStatus('vip-account-status', statusMsg, true);
  ensureVipLoaded();
}
function switchAccountSubtab(tab) {
  vipState.accountSubtab = tab;
  vipState.activeTab = tab;
  renderAccountSubtabs();
  showActivePane();
  ensureVipLoaded();
}
function showActivePane() {
  document.querySelectorAll('#view-vip .st-tab-pane').forEach(p => p.classList.toggle('active', p.id === `vip-pane-${vipState.activeTab}`));
}
function renderPrimaryTabs() {
  const wrap = document.getElementById('vip-primary-tabs');
  if (!wrap) return;
  const tabs = [
    { key: VIP_PRIMARY_OVERVIEW, label: '个人资产总览' },
    { key: VIP_PRIMARY_IMPORT, label: '导入中心' },
    ...(vipState.accounts || []).map(a => ({ key: primaryTabKeyForAccount(a.account_ref || ''), label: a.display_name || a.account_ref || '未命名账户' })),
  ];
  wrap.innerHTML = tabs.map(t => `<button class="st-tab${vipState.primaryTab === t.key ? ' active' : ''}" data-primary-tab="${esc(t.key)}" type="button">${esc(t.label)}</button>`).join('');
  wrap.querySelectorAll('[data-primary-tab]').forEach(btn => btn.addEventListener('click', () => setPrimaryTab(btn.dataset.primaryTab)));
}
function renderAccountSubtabs() {
  const wrap = document.getElementById('vip-account-subtabs');
  if (!wrap) return;
  if (!isAccountPrimaryTab()) {
    wrap.style.display = 'none';
    wrap.innerHTML = '';
    return;
  }
  wrap.style.display = '';
  wrap.innerHTML = VIP_ACCOUNT_SUBTABS.map(t => `<button class="st-tab${vipState.accountSubtab === t.key ? ' active' : ''}" data-account-subtab="${esc(t.key)}" type="button">${esc(t.label)}</button>`).join('');
  wrap.querySelectorAll('[data-account-subtab]').forEach(btn => btn.addEventListener('click', () => switchAccountSubtab(btn.dataset.accountSubtab)));
}
function updateAccountHeading() {
  const heading = document.getElementById('vip-account-heading');
  if (!heading) return;
  const acc = currentAccount();
  if (!isAccountPrimaryTab() || !acc) {
    heading.style.display = 'none';
    heading.textContent = '';
    return;
  }
  const kind = acc.account_kind === 'bank' ? '银行' : '券商';
  const inst = acc.institution_name ? ` · ${acc.institution_name}` : '';
  heading.textContent = `${acc.display_name || acc.account_ref || '未命名账户'} · ${kind}${inst}`;
  heading.style.display = '';
}
function renderOverviewAccounts() {
  const box = document.getElementById('vip-overview-accounts');
  if (!box) return;
  const accounts = vipState.accounts || [];
  if (vipState.accountsError) {
    box.innerHTML = `<p class="st-empty-hint" style="color:#ef4444">账户列表加载失败：${esc(vipState.accountsError)}</p>`;
    return;
  }
  if (!accounts.length) {
    box.innerHTML = '<p class="st-empty-hint">暂无子账户，先去管理账户里新建一个。</p>';
    return;
  }
  box.innerHTML = '<table class="st-table"><thead><tr><th>账户</th><th>机构</th><th>类型</th><th>默认</th></tr></thead><tbody>' +
    accounts.map(a => `<tr><td><button class="btn btn-sm" type="button" data-open-account="${esc(a.account_ref || '')}">${esc(a.display_name || a.account_ref || '未命名账户')}</button></td><td>${esc(a.institution_name || '—')}</td><td>${esc(a.account_kind === 'bank' ? '银行' : '券商')}</td><td>${a.is_default ? '✓' : '—'}</td></tr>`).join('') +
    '</tbody></table>';
  box.querySelectorAll('[data-open-account]').forEach(btn => btn.addEventListener('click', () => {
    const ref = btn.dataset.openAccount || '';
    if (ref) setPrimaryTab(primaryTabKeyForAccount(ref));
  }));
}
function reloadVipView(statusMsg = '') {
  vipState.shownKey = {};
  clearVipContext();
  renderPrimaryTabs();
  renderAccountSubtabs();
  updateAccountHeading();
  showActivePane();
  if (statusMsg) setStatus('vip-account-status', statusMsg, true);
  ensureVipLoaded();
}
async function loadVipAccounts(preferredRef = '', { reloadActive = false } = {}) {
  const select = document.getElementById('vip-account-ref');
  const importSelect = document.getElementById('vip-import-account');
  if (!select) return;
  const prev = preferredRef || select.value || currentPrimaryAccountRef() || VIP_ALL_ACCOUNTS;
  try {
    const data = await vipGet('/accounts');
    const accounts = data.accounts || [];
    vipState.accounts = accounts;
    vipState.accountsError = '';
    const options = [`<option value="${VIP_ALL_ACCOUNTS}">全部账户</option>`];
    options.push(...accounts.map(a => `<option value="${esc(a.account_ref || '')}">${esc(vipAccountLabel(a))}</option>`));
    select.innerHTML = options.join('');
    const next = prev === VIP_ALL_ACCOUNTS
      ? VIP_ALL_ACCOUNTS
      : accounts.find(a => (a.account_ref || '') === prev)?.account_ref
        ?? data.default_account?.account_ref
        ?? accounts[0]?.account_ref
        ?? VIP_ALL_ACCOUNTS;
    select.value = next || VIP_ALL_ACCOUNTS;
    if (importSelect) {
      const prevImport = importSelect.value;   // 重建 innerHTML 会清空选中值，先存下来以保持用户上次选择
      importSelect.innerHTML = ['<option value="">自动识别并归户</option>']
        .concat(accounts.map(a => `<option value="${esc(a.account_ref || '')}">${esc(vipAccountLabel(a))}</option>`))
        .join('');
      const preferredImport = prevImport || currentPrimaryAccountRef();
      if (preferredImport && accounts.some(a => (a.account_ref || '') === preferredImport)) importSelect.value = preferredImport;
      else importSelect.value = '';
    }
    if (isAccountPrimaryTab() && !accounts.some(a => (a.account_ref || '') === currentPrimaryAccountRef())) {
      vipState.primaryTab = accounts[0]?.account_ref ? primaryTabKeyForAccount(accounts[0].account_ref) : VIP_PRIMARY_OVERVIEW;
    }
    renderPrimaryTabs();
    renderAccountSubtabs();
    updateAccountHeading();
    renderOverviewAccounts();
    if (reloadActive) reloadVipView(`已切换到账户：${activeAccountLabel()}`);
  } catch (e) {
    vipState.accounts = [];
    vipState.accountsError = e.message || '加载失败';
    select.innerHTML = `<option value="${VIP_ALL_ACCOUNTS}">全部账户</option>`;
    select.value = VIP_ALL_ACCOUNTS;
    if (importSelect) importSelect.innerHTML = '<option value="">自动识别并归户</option>';
    renderPrimaryTabs();
    renderAccountSubtabs();
    updateAccountHeading();
    renderOverviewAccounts();
    setStatus('vip-account-status', `✗ 账户列表加载失败：${e.message}`, false);
  }
}
// ── 账户管理抽屉 ─────────────────────────────────────────────────────────
let editingRef = null;   // null=新建；非空=编辑该 account_ref

function acctMarketParam() { return `market=${encodeURIComponent(market())}`; }

function resetAccountForm() {
  editingRef = null;
  const q = id => document.getElementById(id);
  q('vip-account-form-title').textContent = '新建账户';
  q('vip-af-ref').value = '';
  q('vip-af-ref').disabled = false;
  q('vip-af-name').value = '';
  q('vip-af-inst').value = '';
  q('vip-af-kind').value = 'broker';
  q('vip-af-default').checked = false;
  q('vip-af-reset').style.display = 'none';
  setStatus('vip-af-status', '', true);
}

function fillAccountForm(a) {
  editingRef = a.account_ref || '';
  const q = id => document.getElementById(id);
  q('vip-account-form-title').textContent = `编辑账户：${a.display_name || a.account_ref || '兼容账户'}`;
  q('vip-af-ref').value = a.account_ref || '';
  q('vip-af-ref').disabled = true;   // 标识是事实层锚点，不可改
  q('vip-af-name').value = a.display_name || '';
  q('vip-af-inst').value = a.institution_name || '';
  q('vip-af-kind').value = a.account_kind === 'bank' ? 'bank' : 'broker';
  q('vip-af-default').checked = !!a.is_default;
  q('vip-af-reset').style.display = '';
}

async function moveAccount(ref, delta) {
  const accounts = vipState.accounts || [];
  const idx = accounts.findIndex(a => (a.account_ref || '') === ref);
  const nextIdx = idx + delta;
  if (idx < 0 || nextIdx < 0 || nextIdx >= accounts.length) return;
  const ordered = accounts.slice();
  const [picked] = ordered.splice(idx, 1);
  ordered.splice(nextIdx, 0, picked);
  setStatus('vip-af-status', '保存排序中…', true);
  try {
    const r = await fetch(`/api/vip/accounts/order?${acctMarketParam()}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account_refs: ordered.map(a => a.account_ref).filter(Boolean) }),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    vipState.accounts = data.accounts || ordered;
    renderAccountsTable();
    await loadVipAccounts(accountRef());
    setStatus('vip-af-status', '✓ 排序已保存', true);
  } catch (e) {
    setStatus('vip-af-status', `✗ 排序失败：${e.message}`, false);
  }
}

async function renderAccountsTable() {
  const tbody = document.getElementById('vip-accounts-tbody');
  if (!tbody) return;
  if (vipState.accountsError) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:#ef4444">账户列表加载失败：${esc(vipState.accountsError)}</td></tr>`;
    return;
  }
  const accounts = vipState.accounts || [];
  if (!accounts.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">当前没有可见账户</td></tr>';
    return;
  }
  tbody.innerHTML = accounts.map((a, idx) => {
    const ref = a.account_ref || '';
    const kind = a.account_kind === 'bank' ? '银行' : '券商';
    const isDefault = a.is_default ? '✓' : '';
    const delBtn = (a.is_default || !ref)
      ? '<span style="color:var(--muted)">—</span>'
      : `<button class="btn btn-sm btn-danger" data-del="${esc(ref)}">删除</button>`;
    const upBtn = idx === 0
      ? '<span style="color:var(--muted)">↑</span>'
      : `<button class="btn btn-sm" data-move-up="${esc(ref)}">上移</button>`;
    const downBtn = idx === accounts.length - 1
      ? '<span style="color:var(--muted)">↓</span>'
      : `<button class="btn btn-sm" data-move-down="${esc(ref)}">下移</button>`;
    return `<tr><td>${esc(a.display_name || ref || '未命名账户')}</td><td>${esc(ref || '（兼容桶）')}</td>` +
      `<td>${esc(a.institution_name || '—')}</td><td>${kind}</td><td>${isDefault}</td>` +
      `<td style="white-space:nowrap"><button class="btn btn-sm" data-edit="${esc(ref)}">编辑</button> ${upBtn} ${downBtn} ${delBtn}</td></tr>`;
  }).join('');
  tbody.querySelectorAll('[data-edit]').forEach(b =>
    b.addEventListener('click', () => {
      const a = accounts.find(x => (x.account_ref || '') === b.dataset.edit);
      if (a) fillAccountForm(a);
    }));
  tbody.querySelectorAll('[data-del]').forEach(b =>
    b.addEventListener('click', () => deleteAccount(b.dataset.del)));
  tbody.querySelectorAll('[data-move-up]').forEach(b =>
    b.addEventListener('click', () => moveAccount(b.dataset.moveUp, -1)));
  tbody.querySelectorAll('[data-move-down]').forEach(b =>
    b.addEventListener('click', () => moveAccount(b.dataset.moveDown, 1)));
}

async function openAccountsDrawer() {
  const drawer = document.getElementById('vip-accounts-drawer');
  if (!drawer) return;
  const mk = document.getElementById('vip-accounts-market');
  if (mk) mk.textContent = market() === 'a_stock' ? '（A股）' : '（美股）';
  await loadVipAccounts(accountRef());   // 拉最新列表进 vipState.accounts
  resetAccountForm();
  renderAccountsTable();
  drawer.style.display = 'flex';
}
function closeAccountsDrawer() {
  const drawer = document.getElementById('vip-accounts-drawer');
  if (drawer) drawer.style.display = 'none';
}

async function openAccountLogDrawer() {
  const drawer = document.getElementById('vip-log-drawer');
  if (!drawer) return;
  const label = document.getElementById('vip-log-account');
  if (label) label.textContent = isAllAccountsSelected() ? '（全部账户）' : `（${activeAccountLabel()}）`;
  drawer.style.display = 'flex';
  await loadAccountLog();
}
function closeAccountLogDrawer() {
  const drawer = document.getElementById('vip-log-drawer');
  if (drawer) drawer.style.display = 'none';
}
async function loadAccountLog() {
  const box = document.getElementById('vip-log-list');
  if (!box) return;
  box.innerHTML = '<p class="st-empty-hint">加载中…</p>';
  const ref = selectedAccountRef();
  const type = document.getElementById('vip-log-filter')?.value || '';
  const params = new URLSearchParams({ limit: '200' });
  if (ref) params.set('account_ref', ref);
  if (type) params.set('event_type', type);
  try {
    const { log } = await vipGet(`/account/log?${params.toString()}`);
    box.innerHTML = renderAccountLog(log || []);
  } catch (e) {
    box.innerHTML = `<p class="st-empty-hint">加载失败：${esc(e.message || e)}</p>`;
  }
}
const VIP_LOG_TYPE_LABEL = { projection: '推算', calibration: '校准', anomaly: '异常', settlement: '结算' };
const VIP_LOG_SEV_CLASS = { info: 'vip-log-info', warn: 'vip-log-warn', alert: 'vip-log-alert' };
function renderAccountLog(rows) {
  if (!rows.length) return '<p class="st-empty-hint">暂无日志；系统每日推算与结算单校准的记录会自动出现在这里。</p>';
  return rows.map(r => {
    const sevCls = VIP_LOG_SEV_CLASS[r.severity] || 'vip-log-info';
    const typeLabel = VIP_LOG_TYPE_LABEL[r.event_type] || r.event_type || '';
    return `<div class="vip-log-item ${sevCls}">` +
      `<div class="vip-log-head">` +
        `<span class="vip-log-type">${esc(typeLabel)}</span>` +
        `<span class="vip-log-title">${esc(r.title || '')}</span>` +
        `<span class="vip-log-ts">${esc(fmtBJ(r.ts))}</span>` +
      `</div>` +
      (r.detail ? `<div class="vip-log-detail">${esc(r.detail)}</div>` : '') +
    `</div>`;
  }).join('');
}

async function saveAccountForm() {
  const q = id => document.getElementById(id);
  const account_ref = q('vip-af-ref').value.trim();
  const display_name = q('vip-af-name').value.trim();
  const institution_name = q('vip-af-inst').value.trim();
  const account_kind = q('vip-af-kind').value === 'bank' ? 'bank' : 'broker';
  const is_default = q('vip-af-default').checked;
  if (editingRef === null && !account_ref) { setStatus('vip-af-status', '✗ 请填写账户标识', false); return; }
  setStatus('vip-af-status', '保存中…', true);
  try {
    let r;
    if (editingRef !== null) {
      const target = editingRef === ''
        ? `/api/vip/accounts?${acctMarketParam()}&account_ref=`
        : `/api/vip/accounts/${encodeURIComponent(editingRef)}?${acctMarketParam()}`;
      r = await fetch(target, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name, institution_name, account_kind, is_default }),
      });
    } else {
      r = await fetch(`/api/vip/accounts?${acctMarketParam()}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ account_ref, display_name, institution_name, account_kind, is_default }),
      });
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    const savedRef = editingRef !== null ? editingRef : account_ref;
    setStatus('vip-af-status', '✓ 已保存', true);
    resetAccountForm();
    await loadVipAccounts(savedRef);   // 刷新顶部下拉，保持/切到该账户
    renderAccountsTable();
    reloadVipView(`✓ 账户已更新：${data.account?.display_name || savedRef || '兼容账户'}`);
  } catch (e) {
    setStatus('vip-af-status', `✗ 保存失败：${e.message}`, false);
  }
}

async function deleteAccount(ref) {
  const acc = (vipState.accounts || []).find(a => (a.account_ref || '') === ref);
  const label = acc ? (acc.display_name || ref) : ref;
  const ok = await showConfirm(`确定删除账户「${label}」吗？仅可删除无数据的空账户，该操作不可撤销。`,
    { title: '删除账户', confirmText: '删除', danger: true });
  if (!ok) return;
  setStatus('vip-af-status', '删除中…', true);
  try {
    const r = await fetch(`/api/vip/accounts/${encodeURIComponent(ref)}?${acctMarketParam()}`, { method: 'DELETE' });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
    const wasActive = accountRef() === ref;
    setStatus('vip-af-status', '✓ 已删除', true);
    if (editingRef === ref) resetAccountForm();
    await loadVipAccounts(wasActive ? '' : accountRef());
    renderAccountsTable();
    if (wasActive) reloadVipView('✓ 账户已删除，已切换到全部账户');
  } catch (e) {
    setStatus('vip-af-status', `✗ 删除失败：${e.message}`, false);
  }
}

function switchTab(tab) {
  if (tab === 'import') {
    setPrimaryTab(VIP_PRIMARY_IMPORT);
    return;
  }
  if (tab === 'dashboard' && !isAccountPrimaryTab()) {
    setPrimaryTab(VIP_PRIMARY_OVERVIEW);
    return;
  }
  switchAccountSubtab(tab);
}
function loadTab(tab) {
  switch (tab) {
    case 'dashboard': loadDashboard(); break;
    case 'import': loadImportCenter(); break;
    case 'positions': loadPositions(); break;
    case 'history': loadTransactions(); break;
    case 'mandate': loadMandate(); break;
    case 'advisory': loadAdvisory(); break;
    case 'recommend': loadRecommend(); break;
    case 'reports': loadReports(); break;
    case 'chat': break;
    default: break;
  }
}
export function ensureVipLoaded() {
  const key = currentTabCacheKey();
  // 面板在各账户页签间共用，只保存一份 DOM；切换到不同 key 必须重渲，否则残留上一个账户的数值
  if (vipState.shownKey[vipState.activeTab] !== key) {
    vipState.shownKey[vipState.activeTab] = key;
    loadTab(vipState.activeTab);
  }
  // 图表可能在视图隐藏态(宽度 0)就已渲染(如启动时预渲染总览)，且 cacheKey 命中不会重渲；
  // 视图/页签此刻刚变可见，下一帧布局定稿后统一校正一次宽度，避免必须切页签才恢复
  requestAnimationFrame(() => {
    Object.values(vipState.charts).forEach(c => { if (c && !c.isDisposed?.()) c.resize(); });
  });
}

function vipChart(id) {
  const el = document.getElementById(id);
  if (!el || typeof echarts === 'undefined') return null;
  let c = echarts.getInstanceByDom(el);
  // 空态曾用 innerHTML 写占位文本，会冲掉 echarts canvas；此时旧实例已废，须重建
  if (c && (c.isDisposed?.() || el.querySelector('.st-empty-hint'))) { c.dispose(); c = null; }
  if (!c) c = echarts.init(el);
  vipState.charts[id] = c;
  c.resize();   // 容器可能在隐藏态(宽度 0)初始化过，每次渲染按当前容器宽度校正
  // 首屏首次渲染时 grid 轨道宽度可能尚未定稿(压缩态)，此刻 resize 量到的是过渡宽度；
  // 下一帧布局已定稿后再校正一次，避免必须切换页签才恢复正常宽度
  requestAnimationFrame(() => { if (!c.isDisposed?.()) c.resize(); });
  return c;
}

async function loadDashboard() {
  const scope = selectedDashboardScope();
  const ref = selectedAccountRef();
  const params = new URLSearchParams({ scope });
  if (scope === 'account' && ref) params.set('account_ref', ref);

  let overview = null;
  try {
    const r = await vipGet(`/account/overview?${params.toString()}`);
    overview = r.overview;
    const el = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    const setLabel = (id, v) => { const e = document.querySelector(`#${id} + .st-stat-label`); if (e) e.textContent = v; };
    if (scope === 'all') {
      el('vip-total-equity', '$' + fmtNum(overview.total_equity));
      el('vip-cash-balance', '$' + fmtNum(overview.cash_balance));
      el('vip-holdings-count', String(overview.n_accounts ?? 0));
      el('vip-transaction-count', String(overview.n_holdings ?? 0));
      el('vip-net-inflow', overview.total_loan_limit == null ? '--' : '$' + fmtNum(overview.total_loan_limit));
      el('vip-fee-total', String(overview.transaction_count ?? 0));
      setLabel('vip-total-equity', '账户总价值');
      setLabel('vip-cash-balance', '现金总价值');
      setLabel('vip-holdings-count', '子账户数');
      setLabel('vip-transaction-count', '持仓总数');
      setLabel('vip-net-inflow', '总贷款额度');
      setLabel('vip-fee-total', '交易总笔数');
      const total = overview.total_equity || 0;
      const cashPct = total ? (overview.cash_balance || 0) / total * 100 : 0;
      el('vip-risk-top5', fmtNum(overview.top5_concentration_pct || 0, 1) + '%');
      el('vip-risk-cash', fmtNum(cashPct, 1) + '%');
      renderHoldingsPie((overview.accounts || []).map(a => ({ ticker: a.display_name || a.account_ref || '兼容账户', market_value: a.total_equity || 0 })));
      renderDerivList([], overview.accounts || []);
      renderOverviewAccounts();
    } else {
      el('vip-total-equity', '$' + fmtNum(overview.total_equity));
      el('vip-cash-balance', '$' + fmtNum(overview.cash_balance));
      el('vip-holdings-count', String(overview.n_holdings ?? 0));
      el('vip-transaction-count', String(overview.transaction_count ?? 0));
      el('vip-net-inflow', '$' + fmtNum((overview.net_inflow || 0) - (overview.net_outflow || 0)));
      el('vip-fee-total', '$' + fmtNum(overview.fee_total));
      setLabel('vip-total-equity', '总权益');
      setLabel('vip-cash-balance', '可投资现金');
      setLabel('vip-holdings-count', '持仓数');
      setLabel('vip-transaction-count', '交易笔数');
      setLabel('vip-net-inflow', '累计净流入');
      setLabel('vip-fee-total', '累计费用');
      const total = overview.total_equity || 0;
      const cashPct = total ? (overview.cash_balance || 0) / total * 100 : 0;
      el('vip-risk-top5', fmtNum(overview.top5_concentration_pct || 0, 1) + '%');
      el('vip-risk-cash', fmtNum(cashPct, 1) + '%');
      renderHoldingsPie(overview.holdings || []);
      const box = document.getElementById('vip-overview-accounts');
      if (box) box.innerHTML = '<p class="st-empty-hint">当前页为子账户视图；账户管理请回到个人资产总览。</p>';
    }
  } catch (_) {}

  try {
    const vs = await vipGet(`/account/value-series?${params.toString()}`);
    renderValueChart(vs.series || [], vs.benchmark);
    renderReturnsChart(vs.returns || []);
    const dd = maxDrawdown((vs.series || []).map(s => s.total_equity));
    const el = document.getElementById('vip-risk-drawdown');
    if (el) el.textContent = fmtNum(dd, 1) + '%';
  } catch (_) {}

  try {
    const { items } = await vipGet(`/derivatives?${params.toString()}`);
    const el = document.getElementById('vip-risk-deriv');
    if (el) el.textContent = String(items?.length || 0);
    if (scope === 'account') renderDerivList(items || []);
  } catch (_) {}

  renderStaleness(ref);
}

async function renderStaleness(ref) {
  const badge = document.getElementById('vip-staleness-badge');
  if (!badge) return;
  try {
    const params = new URLSearchParams();
    if (ref) params.set('account_ref', ref);
    const st = await vipGet(`/account/staleness?${params.toString()}`);
    const days = st.days_since_calibration;
    if (days == null) {
      badge.style.display = 'none';
      return;
    }
    let cls = 'vip-stale-ok';
    if (days >= 45) cls = 'vip-stale-alert';
    else if (days >= 20) cls = 'vip-stale-warn';
    badge.className = `vip-stale-badge ${cls}`;
    badge.textContent = `本账户已 ${days} 天未有结算单校准`;
    badge.title = `最近校准日 ${st.last_calibrated_date || '—'}；` +
      `最近推算日 ${st.latest_projection_date || '—'}；待校准推算 ${st.pending_projection_count || 0} 条`;
    badge.style.display = '';
  } catch (_) {
    badge.style.display = 'none';
  }
}

function maxDrawdown(vals) {
  let peak = -Infinity, mdd = 0;
  for (const v of vals) {
    if (v > peak) peak = v;
    if (peak > 0) mdd = Math.max(mdd, (peak - v) / peak * 100);
  }
  return mdd;
}

function renderValueChart(series, benchmark) {
  const el = document.getElementById('vip-value-chart');
  if (!el) return;
  if (!series.length) {
    el.innerHTML = '<p class="st-empty-hint">暂无价值数据；仅月结单/持仓报告会生成，交易确认和衍生品不会。</p>';
    return;
  }
  const c = vipChart('vip-value-chart');
  if (!c) { el.innerHTML = '<p class="st-empty-hint">图表库未加载，请刷新</p>'; return; }
  const hint = series.length < 2 ? '（仅 1 期，继续导入月结单/持仓报告以形成曲线）' : '';
  const projIdx = series.findIndex(s => s.is_projected);
  // 实值折线：推算点置 null 断开；推算尾段用单独的虚线 series 从最后一个实值点接到推算点
  const realData = series.map(s => (s.is_projected ? null : s.total_equity));
  const projData = series.map((s, i) => {
    if (s.is_projected) return s.total_equity;
    if (projIdx > 0 && i === projIdx - 1) return s.total_equity;  // 虚线起点=最后一个实值点
    return null;
  });
  // A: 大盘基准对照线——后端已 rebase 到起始权益(同轴可比)；指数无历史→benchmark 缺省→不画假线
  const benchData = benchmark ? series.map(s => (s.benchmark_value ?? null)) : null;
  const dateSet = new Set(series.filter(s => s.is_projected).map(s => s.as_of_date));
  const seriesOpt = [
    { name: '实值', type: 'line', data: realData, smooth: true, symbolSize: 7, connectNulls: false,
      lineStyle: { color: '#6366f1', width: 2 }, itemStyle: { color: '#6366f1' },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: '#6366f130' }, { offset: 1, color: '#6366f105' }] } } },
    { name: '推算', type: 'line', data: projData, smooth: true, symbolSize: 8, symbol: 'diamond', connectNulls: true,
      lineStyle: { color: '#f59e0b', width: 2, type: 'dashed' }, itemStyle: { color: '#f59e0b' } },
  ];
  if (benchData) {
    seriesOpt.push({ name: '基准', type: 'line', data: benchData, smooth: true, symbol: 'none', connectNulls: true,
      lineStyle: { color: '#94a3b8', width: 1.5, type: 'dashed' }, itemStyle: { color: '#94a3b8' } });
  }
  // notMerge=true：切换账户时基准线有/无会变，全量替换避免残留上一账户的灰线
  c.setOption({
    legend: { data: benchData ? ['实值', '推算', '基准'] : ['实值', '推算'], top: 0, right: 8,
              itemWidth: 18, itemHeight: 8, textStyle: { fontSize: 11 } },
    grid: { top: 30, right: 20, bottom: 30, left: 64 },
    xAxis: { type: 'category', data: series.map(s => s.as_of_date), axisLabel: { fontSize: 11 }, name: hint, nameGap: 6 },
    yAxis: { type: 'value', axisLabel: { fontSize: 11 }, splitLine: { lineStyle: { type: 'dashed' } } },
    tooltip: { trigger: 'axis', formatter: p => {
      const eq = p.find(x => (x.seriesName === '实值' || x.seriesName === '推算') && x.value != null);
      const bench = p.find(x => x.seriesName === '基准' && x.value != null);
      const name = (eq || bench || p[0]).name;
      const tag = dateSet.has(name) ? '<br/><span style="color:#f59e0b">系统推算·待校准</span>' : '';
      let out = name;
      if (eq) out += `<br/>总权益: $${fmtNum(eq.value)}`;
      if (bench) out += `<br/>基准(${benchmark.label}): $${fmtNum(bench.value)}`;
      return out + tag;
    } },
    series: seriesOpt,
  }, true);
}

function renderReturnsChart(returns) {
  const el = document.getElementById('vip-returns-chart');
  if (!el) return;
  if (!returns.length) { el.innerHTML = '<p class="st-empty-hint">至少 2 期月结单或持仓报告才有逐期收益率</p>'; return; }
  const c = vipChart('vip-returns-chart');
  if (!c) { el.innerHTML = '<p class="st-empty-hint">图表库未加载，请刷新</p>'; return; }
  c.setOption({
    grid: { top: 20, right: 20, bottom: 30, left: 50 },
    xAxis: { type: 'category', data: returns.map(r => r.period), axisLabel: { fontSize: 11 } },
    yAxis: { type: 'value', axisLabel: { formatter: '{value}%', fontSize: 11 }, splitLine: { lineStyle: { type: 'dashed' } } },
    tooltip: { trigger: 'axis', formatter: p => `${p[0].name}<br/>收益率: ${fmtNum(p[0].value, 2)}%` },
    series: [{ type: 'bar', data: returns.map(r => ({ value: r.pct, itemStyle: { color: r.pct >= 0 ? '#22c55e' : '#ef4444' } })), barMaxWidth: 40 }],
  });
}

function renderHoldingsPie(holdings) {
  const el = document.getElementById('vip-holdings-pie');
  if (!el) return;
  if (!holdings.length) { el.innerHTML = '<p class="st-empty-hint">暂无持仓，导入月结单后生成</p>'; return; }
  const c = vipChart('vip-holdings-pie');
  if (!c) { el.innerHTML = '<p class="st-empty-hint">图表库未加载，请刷新</p>'; return; }
  const data = holdings.map(h => ({ name: h.ticker, value: Math.round((h.market_value || 0) * 100) / 100 }));
  c.setOption({
    tooltip: { trigger: 'item', formatter: p => `${p.name}<br/>市值 $${fmtNum(p.value)}（${fmtNum(p.percent, 1)}%）` },
    legend: { type: 'scroll', bottom: 0, textStyle: { fontSize: 11 } },
    series: [{ type: 'pie', radius: ['40%', '68%'], center: ['50%', '44%'], avoidLabelOverlap: true,
      itemStyle: { borderColor: '#fff', borderWidth: 2 }, label: { show: false }, data }],
  });
}

function renderDerivList(items, accounts = null) {
  const box = document.getElementById('vip-deriv-list');
  if (!box) return;
  if (accounts) {
    if (!accounts.length) { box.innerHTML = '<p class="st-empty-hint">暂无子账户摘要</p>'; return; }
    box.innerHTML = '<table class="st-table"><thead><tr><th>账户</th><th>机构</th><th>类型</th><th>总价值</th><th>现金</th><th>持仓数</th><th>占比</th></tr></thead><tbody>' +
      accounts.map(a => `<tr><td>${esc(a.display_name || a.account_ref || '兼容账户')}</td><td>${esc(a.institution_name || '—')}</td><td>${esc(a.account_kind === 'bank' ? '银行' : '券商')}</td><td>$${fmtNum(a.total_equity)}</td><td>$${fmtNum(a.cash_balance)}</td><td>${fmtNum(a.n_holdings, 0)}</td><td>${fmtNum(a.weight_pct || 0, 1)}%</td></tr>`).join('') +
      '</tbody></table>';
    return;
  }
  if (!items.length) { box.innerHTML = '<p class="st-empty-hint">暂无已建模衍生品文件</p>'; return; }
  box.innerHTML = '<table class="st-table"><thead><tr><th>产品族</th><th>标的</th><th>币种</th><th>操作</th></tr></thead><tbody>' +
    items.map(d => `<tr><td>${esc(d.product_family || '—')}</td><td>${esc(d.underlying_symbol || '—')}</td><td>${esc(d.currency || '—')}</td>` +
      `<td>${d.id ? `<button class="btn btn-sm" data-reextract="${esc(d.id)}">重新抽取条款</button>` : '—'}</td></tr>`).join('') +
    '</tbody></table>';
  box.querySelectorAll('button[data-reextract]').forEach(btn =>
    btn.addEventListener('click', () => reextractDeriv(btn.getAttribute('data-reextract'))));
}

// 重传原始结算单回填/刷新条款（旧数据缺 trade_date 时用）
function reextractDeriv(did) {
  if (!did) return;
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.pdf,application/pdf';
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const pwd = /\.pdf$/i.test(file.name) ? (prompt(`「${file.name}」如有密码请输入（无密码留空）:`) || '') : '';
    const fd = new FormData();
    fd.append('file', file);
    try {
      const r = await fetch(vipApiUrl(`/derivatives/${encodeURIComponent(did)}/reextract?pdf_password=${encodeURIComponent(pwd)}`),
        { method: 'POST', body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { setStatus('vip-account-status', d.detail || '重抽失败', false); return; }
      setStatus('vip-account-status', `已重抽条款：${d.term?.underlying || ''}` + (d.trade_date ? `（起始日 ${d.trade_date}）` : '（未解析到起始日）'), true);
      loadDashboard();
    } catch (e) {
      setStatus('vip-account-status', String(e), false);
    }
  });
  input.click();
}

// ── 导入中心 ──────────────────────────────────────────────────────────────
async function loadImportCenter() {
  clearScopeHint('import');
  const body = document.getElementById('vip-imports-body');
  const empty = document.getElementById('vip-imports-empty');
  const chosenRef = selectedImportAccountRef();
  const accountMap = new Map((vipState.accounts || []).map(a => [a.account_ref || '', a]));
  if (body) {
    try {
      const scope = chosenRef ? 'account' : 'all';
      const params = new URLSearchParams({ scope, limit: '100' });
      if (chosenRef) params.set('account_ref', chosenRef);
      const res = await vipGet(`/imports?${params.toString()}`);
      const imports = (res.imports || []).slice();
      imports.sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
      if (!imports.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; }
      else {
        if (empty) empty.style.display = 'none';
        body.innerHTML = imports.slice(0, 100).map(x => {
          const km = x.key_metrics || {};
          const bits = Object.entries(km).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${esc(v)}`).join(' · ');
          const detail = [esc(x.summary || ''), x.reason ? '⚠ ' + esc(x.reason) : '', bits].filter(Boolean).join('<br/>');
          const acc = accountMap.get(x.account_ref || '');
          const accountLabel = acc ? (acc.display_name || acc.account_ref || '未命名账户') : (x.account_ref || '自动归户');
          return `<tr><td style="font-weight:600">${esc(x.file_name)}</td><td>${esc(accountLabel)}</td><td>${esc((x.created_at || '').replace('T', ' ').slice(0, 16))}</td>` +
            `<td>${esc(x.file_type)}/${esc(x.detected_kind || '—')}</td><td>${importStatusBadge(x.status)}</td><td>${detail}</td></tr>`;
        }).join('');
      }
    } catch (_) { if (empty) empty.style.display = ''; }
  }
  const mbox = document.getElementById('vip-missing');
  if (mbox) {
    try {
      const { missing } = await vipGet(`/account/missing?account_ref=${encodeURIComponent(chosenRef)}`);
      if (!missing?.length) { mbox.innerHTML = '<p class="st-empty-hint">✓ 数据齐全，暂无缺口</p>'; }
      else mbox.innerHTML = missing.map(m =>
        `<div class="vip-missing-item ${esc(m.severity)}"><div style="font-weight:600">${esc(m.label)}</div>` +
        `<div style="color:var(--muted);margin-top:2px">${esc(m.hint)}</div></div>`).join('');
    } catch (_) {}
  }
}

function importStatusBadge(s) {
  const map = {
    imported: ['已导入', '#22c55e'],
    duplicate: ['重复', '#f59e0b'],
    rejected: ['已拒绝', '#ef4444'],
    unparseable: ['无法解读', '#ef4444'],
    needs_account_confirmation: ['待确认账户', '#f59e0b'],
  };
  const [txt, color] = map[s] || [s, '#64748b'];
  return `<span style="color:${color};font-weight:600">${esc(txt)}</span>`;
}

function invalidateVipCache({ accountRef = '', includeImport = true, includeOverview = true } = {}) {
  // 面板共用一份 DOM，导入/改动后强制当前视图重渲：清掉受影响页签的已渲染标记
  void accountRef;
  if (includeOverview) ['dashboard', 'positions', 'history', 'reports', 'chat'].forEach(t => delete vipState.shownKey[t]);
  if (includeImport) delete vipState.shownKey['import'];
}

async function uploadAny(file, forcedAccountRef = null, cachedPassword = null) {
  const results = document.getElementById('vip-import-results');
  if (!file) return;
  const row = document.createElement('div');
  row.className = 'vip-import-row';
  row.textContent = `上传解析中：${file.name}…`;
  results?.prepend(row);
  let pwd = cachedPassword;
  if (pwd == null) pwd = /\.pdf$/i.test(file.name) ? (prompt(`「${file.name}」如有密码请输入（无密码留空）:`) || '') : '';
  try {
    const fd = new FormData();
    fd.append('file', file);
    const pickedRef = forcedAccountRef == null ? (selectedImportAccountRef() || currentPrimaryAccountRef() || '') : forcedAccountRef;
    const ref = encodeURIComponent(pickedRef || '');
    const r = await fetch(`/api/vip/import?market=${encodeURIComponent(market())}&account_ref=${ref}&pdf_password=${encodeURIComponent(pwd)}`,
      { method: 'POST', body: fd });
    const text = await r.text();
    let d = {};
    try {
      d = text ? JSON.parse(text) : {};
    } catch (_) {
      d = {};
    }
    if (!r.ok) {
      const detail = d.detail || text || '上传失败';
      row.className = 'vip-import-row bad';
      row.textContent = `✗ ${file.name}：${detail}`;
      return;
    }
    if (d.status === 'needs_account_confirmation') {
      const candidates = Array.isArray(d.account_candidates) ? d.account_candidates : [];
      // 后端给不出候选（券商/账号都没匹配上）时，回退到「让用户从全部真实账户里选」，
      // 否则用户只会看到一条报错行、无从选择——这正是"弹窗没出现"的根因。
      const choices = (candidates.length ? candidates : (vipState.accounts || []))
        .map(a => ({
          value: a.account_ref,
          label: a.display_name || a.account_ref,
          sub: a.institution_name || '',
        }))
        .filter(c => c.value);
      const chosenRef = choices.length
        ? await showChoice(
            candidates.length
              ? `「${file.name}」匹配到多个账户，请选择归属账户：`
              : `「${file.name}」未能自动识别账户，请选择归属账户：`,
            choices,
            { title: '确认导入账户' })
        : null;
      if (chosenRef) {
        row.textContent = `账户已确认，重新导入：${file.name}…`;
        await uploadAny(file, chosenRef, pwd);
        row.remove();
        return;
      }
    }
    const cls = d.status === 'imported' ? 'ok' : d.status === 'duplicate' ? 'dup' : 'bad';
    row.className = 'vip-import-row ' + cls;
    const km = d.key_metrics || {};
    const bits = Object.entries(km).filter(([, v]) => v != null && v !== '').map(([k, v]) => `${k}=${v}`).join(' · ');
    const autoSwitched = d.status === 'imported'
      && d.resolved_account_ref
      && ['monthly_statement', 'position_report'].includes(d.detected_kind || '');
    const statusLine = autoSwitched
      ? `已导入并切换到账户：${d.resolved_account_ref}`
      : d.detected_kind === 'trade_confirm'
        ? '已导入交易流水；不会新增当前持仓或价值曲线。'
        : ['accumulator', 'decumulator', 'mli'].includes(d.detected_kind || '')
          ? '已导入衍生品条款；不会新增当前持仓或价值曲线。'
          : '';
    row.innerHTML = `${importStatusBadge(d.status)} <b>${esc(file.name)}</b>` +
      (d.summary ? ` — ${esc(d.summary)}` : '') + (d.reason ? `<br/><span style="color:#ef4444">⚠ ${esc(d.reason)}</span>` : '') +
      (d.resolved_account_ref ? `<br/><span style="color:var(--muted)">账户：${esc(d.resolved_account_ref)}</span>` : '') +
      (statusLine ? `<br/><span style="color:var(--muted)">${esc(statusLine)}</span>` : '') +
      (bits ? `<br/><span style="color:var(--muted)">${esc(bits)}</span>` : '');
    if (d.status === 'imported') {
      invalidateVipCache({ accountRef: d.resolved_account_ref || pickedRef || '' });
    }
    if (d.status === 'imported' && d.resolved_account_ref) {
      await loadVipAccounts(d.resolved_account_ref);
      loadImportCenter();
    }
    if (autoSwitched) {
      setPrimaryTab(primaryTabKeyForAccount(d.resolved_account_ref), { statusMsg: `已导入并切换到账户：${d.resolved_account_ref}` });
    } else {
      await loadVipAccounts(currentPrimaryAccountRef() || selectedAccountRef());
      loadImportCenter();
    }
  } catch (e) {
    row.className = 'vip-import-row bad';
    row.textContent = `✗ ${file.name}：${e.message}`;
  }
}

function bindImportCenter() {
  const zone = document.getElementById('vip-dropzone');
  const input = document.getElementById('vip-import-file');
  if (!zone || !input) return;
  zone.addEventListener('click', () => input.click());
  input.addEventListener('change', () => { for (const f of input.files) uploadAny(f); input.value = ''; });
  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add('dragover'); }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.remove('dragover'); }));
  zone.addEventListener('drop', e => { for (const f of e.dataTransfer.files) uploadAny(f); });
}

async function loadPositions() {
  if (requireConcreteAccount('positions')) return;
  clearScopeHint('positions');
  const body = document.getElementById('vip-positions-body');
  const empty = document.getElementById('vip-positions-empty');
  if (!body) return;
  try {
    const { positions } = await vipGet(`/account/positions?account_ref=${encodeURIComponent(selectedAccountRef())}`);
    if (!positions?.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    body.innerHTML = positions.map(p => {
      const pnl = p.unrealized_pnl || 0;
      const cls = pnl > 0 ? 'st-pnl-pos' : pnl < 0 ? 'st-pnl-neg' : 'st-pnl-zero';
      // 双击整行 → 复用观察池统一企业详情抽屉（全局 data-company-ticker 委托，见 watchlist.js）
      const tk = esc(p.ticker);
      return `<tr data-company-ticker="${tk}" data-company-name="${tk}" data-company-market="${esc(market())}" style="cursor:pointer" title="双击查看企业详情">` +
        `<td style="font-weight:600">${tk}</td><td>${fmtNum(p.shares, 0)}</td>` +
        `<td>$${fmtNum(p.avg_cost)}</td><td>$${fmtNum(p.current_price)}</td><td>$${fmtNum(p.market_value)}</td>` +
        `<td class="${cls}">${pnl >= 0 ? '+' : '-'}$${fmtNum(Math.abs(pnl))}</td><td>${fmtNum(p.weight_pct, 1)}%</td></tr>`;
    }).join('');
  } catch (_) { if (empty) empty.style.display = ''; }
}

async function loadTransactions() {
  if (requireConcreteAccount('history')) return;
  clearScopeHint('history');
  const body = document.getElementById('vip-transactions-body');
  const empty = document.getElementById('vip-transactions-empty');
  if (!body) return;
  const ref = selectedAccountRef();
  const params = new URLSearchParams();
  if (ref) params.set('account_ref', ref);
  const t = document.getElementById('vip-history-ticker')?.value.trim();
  const ty = document.getElementById('vip-history-type')?.value;
  const sd = document.getElementById('vip-history-start')?.value;
  const ed = document.getElementById('vip-history-end')?.value;
  if (t) params.set('ticker', t);
  if (ty) params.set('txn_type', ty);
  if (sd) params.set('start_date', sd);
  if (ed) params.set('end_date', ed);
  params.set('limit', '200');
  try {
    const { transactions } = await vipGet(`/account/transactions?${params.toString()}`);
    if (!transactions?.length) { body.innerHTML = ''; if (empty) empty.style.display = ''; return; }
    if (empty) empty.style.display = 'none';
    body.innerHTML = transactions.map(x => {
      const net = x.net_amount || 0;
      const cls = net > 0 ? 'st-pnl-pos' : net < 0 ? 'st-pnl-neg' : 'st-pnl-zero';
      return `<tr><td>${esc(x.trade_date)}</td><td>${esc(x.symbol || x.company || '—')}</td>` +
        `<td>${esc(x.txn_type)}</td><td>${esc(x.currency || '')}</td><td>$${fmtNum(x.gross_amount)}</td>` +
        `<td class="${cls}">${net >= 0 ? '+' : '-'}$${fmtNum(Math.abs(net))}</td><td>${esc(x.description || '')}</td></tr>`;
    }).join('');
  } catch (_) { if (empty) empty.style.display = ''; }
}

async function loadMandate() {
  if (requireConcreteAccount('mandate')) return;
  clearScopeHint('mandate');
  const ref = selectedAccountRef();
  const q = id => document.getElementById(id);
  setStatus('vip-mandate-status', '', true);
  try {
    const { mandate } = await vipGet(`/account/mandate?account_ref=${encodeURIComponent(ref)}`);
    const m = mandate || {};
    if (q('vip-mn-risk')) q('vip-mn-risk').value = m.risk_appetite || 'balanced';
    if (q('vip-mn-return')) q('vip-mn-return').value = m.annual_return_target_pct ?? 0;
    if (q('vip-mn-drawdown')) q('vip-mn-drawdown').value = m.max_drawdown_pct ?? 25;
    if (q('vip-mn-horizon')) q('vip-mn-horizon').value = m.horizon || 'swing';
    if (q('vip-mn-markets')) q('vip-mn-markets').value = m.focus_markets || '';
    if (q('vip-mn-sectors')) q('vip-mn-sectors').value = m.focus_sectors || '';
    if (q('vip-mn-principles')) q('vip-mn-principles').value = m.principles || '';
    if (q('vip-mn-exclusions')) q('vip-mn-exclusions').value = m.exclusions || '';
    const btn = q('vip-mandate-save');
    if (btn) btn.onclick = () => saveMandate(ref);
  } catch (e) {
    setStatus('vip-mandate-status', `✗ 加载失败：${e.message}`, false);
  }
}

async function saveMandate(ref) {
  const q = id => document.getElementById(id);
  const payload = {
    risk_appetite: q('vip-mn-risk')?.value || 'balanced',
    annual_return_target_pct: Number(q('vip-mn-return')?.value || 0),
    max_drawdown_pct: Number(q('vip-mn-drawdown')?.value || 25),
    horizon: q('vip-mn-horizon')?.value || 'swing',
    focus_markets: q('vip-mn-markets')?.value || '',
    focus_sectors: q('vip-mn-sectors')?.value || '',
    principles: q('vip-mn-principles')?.value || '',
    exclusions: q('vip-mn-exclusions')?.value || '',
  };
  setStatus('vip-mandate-status', '保存中…', true);
  try {
    const r = await fetch(vipApiUrl(`/account/mandate?account_ref=${encodeURIComponent(ref)}`), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(explainVipHttpError(r.status, data.detail || ''));
    }
    const { mandate } = await r.json();
    // 回填规范化结果（后端做了 clamp/枚举校验，如回撤 99→60）
    if (q('vip-mn-drawdown') && mandate) q('vip-mn-drawdown').value = mandate.max_drawdown_pct;
    if (q('vip-mn-return') && mandate) q('vip-mn-return').value = mandate.annual_return_target_pct;
    setStatus('vip-mandate-status', '✓ 已保存，后续报告与咨询将据此给出建议', true);
  } catch (e) {
    setStatus('vip-mandate-status', `✗ 保存失败：${e.message}`, false);
  }
}

// B: 现金/仓位预算对照条（指示性）——顾问加仓 + 荐新建仓的已量化仓位加总 vs 可投资现金。
// 跨两个 pass，内容相同，故同时写进 advisory/recommend 两个容器；旁路失败不影响主体。
async function refreshBudgetBar(ref) {
  const targets = ['vip-advisory-budget', 'vip-recommend-budget']
    .map(id => document.getElementById(id)).filter(Boolean);
  if (!targets.length || !ref) return;
  let html = '';
  try {
    const { budget: b } = await vipGet(`/account/budget-reconciliation?account_ref=${encodeURIComponent(ref)}`);
    if (b && (b.has_advisory || b.has_recommend)) {
      const warn = !b.fits;
      const color = warn ? '#b45309' : '#15803d', bg = warn ? '#fffbeb' : '#f0fdf4', bd = warn ? '#fde68a' : '#bbf7d0';
      const fitTxt = warn ? `已量化新建仓超可投资现金 ${fmtNum(b.overcommit_pct, 1)}%` : '可投资现金可覆盖已量化新建仓';
      html = `<div style="margin:8px 0;padding:8px 12px;border:1px solid ${bd};background:${bg};border-radius:8px;font-size:12.5px;color:${color}">` +
        `<div><b>${warn ? '⚠' : '✓'} 预算对照（指示性）</b>：可投资现金 $${fmtNum(b.available_cash)} · 已量化新建仓需求 $${fmtNum(b.requested_new_buy)} · ${esc(fitTxt)}` +
        (b.unquantified_adds ? ` · 另有 ${b.unquantified_adds} 项加/建仓未量化` : '') +
        (b.partial ? ` · 仅含已生成 pass、结果偏乐观` : '') + `</div>` +
        `<div style="margin-top:3px;color:var(--muted);font-size:11.5px">${esc(b.note)}</div></div>`;
    }
  } catch (_) { /* 预算条失败不影响建议主体 */ }
  targets.forEach(t => { t.innerHTML = html; });
}

async function loadAdvisory() {
  if (requireConcreteAccount('advisory')) return;
  clearScopeHint('advisory');
  const ref = selectedAccountRef();
  const body = document.getElementById('vip-advisory-body');
  const btn = document.getElementById('vip-advisory-generate');
  if (btn) btn.onclick = () => runAdvisory(ref);
  setStatus('vip-advisory-status', '', true);
  refreshBudgetBar(ref);  // 独立异步，与两 pass 建议并存
  if (body) body.innerHTML = '<p class="st-empty-hint">加载中…</p>';
  try {
    const { history } = await vipGet(`/account/advisory/history?account_ref=${encodeURIComponent(ref)}`);
    renderAdvisoryHistory(history || []);
    if (!history?.length) { if (body) body.innerHTML = '<p class="st-empty-hint">暂无顾问决策建议。点上方「生成建议」，按最新持仓 + 纲领 + 宏观出一份（仅建议，不下单）。</p>'; return; }
    renderAdvisory(history[0].result);
  } catch (e) {
    if (body) body.innerHTML = `<p class="st-empty-hint">加载失败：${esc(e.message || e)}</p>`;
  }
}

function renderAdvisoryHistory(history, activeIdx = 0) {
  const box = document.getElementById('vip-advisory-history');
  if (!box) return;
  if (!history.length) { box.innerHTML = ''; return; }
  const vTxt = { approve: '通过', reject: '否决', split: '分歧' };
  box.innerHTML = '<div class="vip-adv-hist-label">历史（点选回看）：</div>' +
    '<div class="vip-adv-hist-wrap">' +
    history.map((h, i) =>
      `<button class="vip-adv-hist${i === activeIdx ? ' active' : ''}" data-i="${i}" type="button">` +
      `${esc(fmtBJ(h.created_at))} · ${esc(vTxt[h.verdict] || '—')} · ${h.n_holdings}仓</button>`).join('') +
    '</div>';
  box.querySelectorAll('.vip-adv-hist').forEach(el =>
    el.addEventListener('click', () => {
      box.querySelectorAll('.vip-adv-hist').forEach(b => b.classList.remove('active'));
      el.classList.add('active');
      renderAdvisory(history[+el.dataset.i].result);
    }));
}

async function runAdvisory(ref) {
  setStatus('vip-advisory-status', '生成中…（拆草案 + 投委会 4 席评审，约 20-40 秒）', true);
  try {
    const r = await fetch(vipApiUrl(`/account/advisory?account_ref=${encodeURIComponent(ref)}`), { method: 'POST' });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(explainVipHttpError(r.status, data.detail || ''));
    }
    const { result } = await r.json();
    renderAdvisory(result);
    setStatus('vip-advisory-status', '✓ 已生成', true);
    refreshBudgetBar(ref);  // 新建议可能改变加仓集 → 刷新预算对照
    try {  // 刷新历史列表（失败不影响已渲染的新建议）
      const { history } = await vipGet(`/account/advisory/history?account_ref=${encodeURIComponent(ref)}`);
      renderAdvisoryHistory(history || []);
    } catch (_) { /* 历史刷新失败无碍 */ }
  } catch (e) {
    setStatus('vip-advisory-status', `✗ 生成失败：${e.message}`, false);
  }
}

function renderAdvisory(result) {
  const body = document.getElementById('vip-advisory-body');
  if (!body || !result) return;
  const actionCls = { '加仓': 'buy', '减仓': 'sell', '持有': 'hold' };
  const c = result.committee || {};
  const vKey = ['approve', 'reject', 'split'].includes(c.verdict) ? c.verdict : 'split';
  const verdictTxt = { approve: '通过', reject: '否决', split: '分歧' }[c.verdict] || '—';
  const voteTxt = { approve: '通过', approve_with_modification: '有条件通过', reject: '否决', abstain: '弃权', split: '分歧' };
  const rows = (result.holdings || []).map(h => {
    const cls = actionCls[h.action] || 'hold';
    return `<tr><td class="vip-adv-cell-key" style="font-weight:600">${esc(h.ticker || '—')}</td>` +
      `<td class="vip-adv-cell-key"><span class="vip-action-pill ${cls}">${esc(h.action || '—')}</span></td>` +
      `<td>${esc(h.reason || '')}</td><td style="color:var(--muted)">${esc(h.risk || '')}</td>` +
      `<td class="vip-adv-cell-deriv">${esc(h.derivative_note || '')}</td></tr>`;
  }).join('');
  const members = (c.members || []).map(m => {
    const vote = m.vote || '';
    const vcls = vote.startsWith('approve') ? 'yes' : (vote === 'reject' ? 'no' : 'abstain');
    return `<div class="vip-member-card">` +
      `<div class="vip-member-head"><span class="vip-member-name">${esc(m.label || m.role)}</span>` +
      `<span class="vip-vote-pill ${vcls}">${esc(voteTxt[vote] || vote)}</span></div>` +
      (m.model ? `<div class="vip-member-model">${esc(m.model)}</div>` : '') +
      ((m.key_concerns || []).length ? `<ul>${m.key_concerns.map(k => `<li>${esc(k)}</li>`).join('')}</ul>` : '') +
      (m.assessment ? `<div class="vip-member-assess">${esc(m.assessment)}</div>` : '') + `</div>`;
  }).join('');
  body.innerHTML =
    `<div class="vip-adv-meta">生成于 ${esc(fmtBJ(result.generated_at))} · ${esc(result.provider || '')}/${esc(result.model || '')}</div>` +
    `<div class="vip-adv-verdict ${vKey}">` +
      `<span class="vip-adv-verdict-badge ${vKey}">投委会：${esc(verdictTxt)}</span>` +
      `<span class="vip-adv-verdict-count">赞成 ${c.approve || 0} · 否决 ${c.reject || 0}</span>` +
      (c.caution ? `<span class="vip-adv-verdict-caution">⚠ 建议审慎</span>` : '') +
    `</div>` +
    (c.risk_veto ? `<div class="vip-adv-foot warn">⛔ 风控委员否决：已抑制升级为「通过」，请审慎核对</div>` : '') +
    (c.diversity_warning ? `<div class="vip-adv-foot warn">⚠ ${esc(c.diversity_warning)}</div>` : '') +
    (c.weighted_note ? `<div class="vip-adv-foot muted">${esc(c.weighted_note)}</div>` : '') +
    (result.reconciled ? `<div class="vip-adv-foot warn">↔ 已按投委会结论对账草案动作（详见逐条建议中的括注）</div>` : '') +
    (result.portfolio_diagnosis ? `<div class="vip-adv-callout"><h4>组合诊断</h4><p>${esc(result.portfolio_diagnosis)}</p></div>` : '') +
    (result.cross_market_coverage ? `<div class="vip-adv-callout coverage"><h4>跨市场覆盖</h4><p>${esc(result.cross_market_coverage)}</p></div>` : '') +
    `<div class="vip-adv-section-title">逐仓建议</div>` +
    `<div class="st-table-wrap"><table class="st-table vip-adv-table"><thead><tr><th>标的</th><th>建议</th><th>理由</th><th>风险</th><th>衍生品提示</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<div class="vip-adv-section-title">投委会 4 席评审</div>` +
    `<div class="vip-committee-grid">${members}</div>` +
    ((result.unverified || []).length ? `<div class="vip-adv-foot warn">⚠ 未在账户数据中核到的数字：${result.unverified.map(esc).join('、')}</div>` : '') +
    (result.disclaimer ? `<div class="vip-adv-foot muted">${esc(result.disclaimer)}</div>` : '');
}

async function loadRecommend() {
  if (requireConcreteAccount('recommend')) return;
  clearScopeHint('recommend');
  const ref = selectedAccountRef();
  const body = document.getElementById('vip-recommend-body');
  const btn = document.getElementById('vip-recommend-generate');
  if (btn) btn.onclick = () => runRecommend(ref);
  setStatus('vip-recommend-status', '', true);
  refreshBudgetBar(ref);  // 独立异步，与两 pass 建议并存
  if (body) body.innerHTML = '<p class="st-empty-hint">加载中…</p>';
  try {
    const { history } = await vipGet(`/account/recommend/history?account_ref=${encodeURIComponent(ref)}`);
    renderRecommendHistory(history || []);
    if (!history?.length) { if (body) body.innerHTML = '<p class="st-empty-hint">暂无荐新建议。点上方「生成建议」，从观察池挑尚未持有的标的出一份（仅建议，不下单）。</p>'; return; }
    renderRecommend(history[0].result);
  } catch (e) {
    if (body) body.innerHTML = `<p class="st-empty-hint">加载失败：${esc(e.message || e)}</p>`;
  }
}

function renderRecommendHistory(history, activeIdx = 0) {
  const box = document.getElementById('vip-recommend-history');
  if (!box) return;
  if (!history.length) { box.innerHTML = ''; return; }
  const vTxt = { approve: '通过', reject: '否决', split: '分歧' };
  box.innerHTML = '<div class="vip-adv-hist-label">历史（点选回看）：</div>' +
    '<div class="vip-adv-hist-wrap">' +
    history.map((h, i) =>
      `<button class="vip-adv-hist${i === activeIdx ? ' active' : ''}" data-i="${i}" type="button">` +
      `${esc(fmtBJ(h.created_at))} · ${esc(vTxt[h.verdict] || '—')} · ${h.n_candidates}只</button>`).join('') +
    '</div>';
  box.querySelectorAll('.vip-adv-hist').forEach(el =>
    el.addEventListener('click', () => {
      box.querySelectorAll('.vip-adv-hist').forEach(b => b.classList.remove('active'));
      el.classList.add('active');
      renderRecommend(history[+el.dataset.i].result);
    }));
}

async function runRecommend(ref) {
  setStatus('vip-recommend-status', '生成中…（观察池候选 + 投委会 4 席评审，约 20-40 秒）', true);
  try {
    const r = await fetch(vipApiUrl(`/account/recommend?account_ref=${encodeURIComponent(ref)}`), { method: 'POST' });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      throw new Error(explainVipHttpError(r.status, data.detail || ''));
    }
    const { result } = await r.json();
    renderRecommend(result);
    setStatus('vip-recommend-status', '✓ 已生成', true);
    refreshBudgetBar(ref);  // 新荐新可能改变建仓集 → 刷新预算对照
    try {  // 刷新历史列表（失败不影响已渲染的新建议）
      const { history } = await vipGet(`/account/recommend/history?account_ref=${encodeURIComponent(ref)}`);
      renderRecommendHistory(history || []);
    } catch (_) { /* 历史刷新失败无碍 */ }
  } catch (e) {
    setStatus('vip-recommend-status', `✗ 生成失败：${e.message}`, false);
  }
}

function renderRecommend(result) {
  const body = document.getElementById('vip-recommend-body');
  if (!body || !result) return;
  const actionCls = { '建仓': 'buy', '规避': 'sell', '关注': 'hold' };
  const c = result.committee || {};
  const vKey = ['approve', 'reject', 'split'].includes(c.verdict) ? c.verdict : 'split';
  const verdictTxt = { approve: '通过', reject: '否决', split: '分歧' }[c.verdict] || '—';
  const voteTxt = { approve: '通过', approve_with_modification: '有条件通过', reject: '否决', abstain: '弃权', split: '分歧' };
  const st = result.pool_stats || {};
  const rows = (result.candidates || []).map(h => {
    const cls = actionCls[h.action] || 'hold';
    return `<tr><td class="vip-adv-cell-key" style="font-weight:600">${esc(h.ticker || '—')}</td>` +
      `<td class="vip-adv-cell-key"><span class="vip-action-pill ${cls}">${esc(h.action || '—')}</span></td>` +
      `<td>${esc(h.reason || '')}</td><td style="color:var(--muted)">${esc(h.risk || '')}</td>` +
      `<td>${esc(h.fit || '')}</td><td class="vip-adv-cell-key">${esc(h.suggested_weight || '')}</td></tr>`;
  }).join('');
  const members = (c.members || []).map(m => {
    const vote = m.vote || '';
    const vcls = vote.startsWith('approve') ? 'yes' : (vote === 'reject' ? 'no' : 'abstain');
    return `<div class="vip-member-card">` +
      `<div class="vip-member-head"><span class="vip-member-name">${esc(m.label || m.role)}</span>` +
      `<span class="vip-vote-pill ${vcls}">${esc(voteTxt[vote] || vote)}</span></div>` +
      (m.model ? `<div class="vip-member-model">${esc(m.model)}</div>` : '') +
      ((m.key_concerns || []).length ? `<ul>${m.key_concerns.map(k => `<li>${esc(k)}</li>`).join('')}</ul>` : '') +
      (m.assessment ? `<div class="vip-member-assess">${esc(m.assessment)}</div>` : '') + `</div>`;
  }).join('');
  const poolLine = st.n_total != null
    ? `候选池 ${st.n_total} 只 · 取综合分前 ${st.n_shown}${st.capped ? '（已截断）' : ''} · 已持剔除 ${(st.dropped_held || []).length} · 纲领排除 ${(st.dropped_excluded || []).length}`
    : '';
  body.innerHTML =
    `<div class="vip-adv-meta">生成于 ${esc(fmtBJ(result.generated_at))} · ${esc(result.provider || '')}/${esc(result.model || '')}</div>` +
    (poolLine ? `<div class="vip-adv-meta">${esc(poolLine)}</div>` : '') +
    `<div class="vip-adv-verdict ${vKey}">` +
      `<span class="vip-adv-verdict-badge ${vKey}">投委会：${esc(verdictTxt)}</span>` +
      `<span class="vip-adv-verdict-count">赞成 ${c.approve || 0} · 否决 ${c.reject || 0}</span>` +
      (c.caution ? `<span class="vip-adv-verdict-caution">⚠ 建议审慎</span>` : '') +
    `</div>` +
    (c.risk_veto ? `<div class="vip-adv-foot warn">⛔ 风控委员否决：已抑制升级为「通过」，请审慎核对</div>` : '') +
    (c.diversity_warning ? `<div class="vip-adv-foot warn">⚠ ${esc(c.diversity_warning)}</div>` : '') +
    (c.weighted_note ? `<div class="vip-adv-foot muted">${esc(c.weighted_note)}</div>` : '') +
    (result.reconciled ? `<div class="vip-adv-foot warn">↔ 已按投委会结论对账草案动作（详见逐条建议中的括注）</div>` : '') +
    (result.portfolio_note ? `<div class="vip-adv-callout"><h4>组合再平衡</h4><p>${esc(result.portfolio_note)}</p></div>` : '') +
    `<div class="vip-adv-section-title">荐新候选</div>` +
    `<div class="st-table-wrap"><table class="st-table vip-adv-table"><thead><tr><th>标的</th><th>建议</th><th>理由</th><th>风险</th><th>契合</th><th>建议仓位</th></tr></thead><tbody>${rows}</tbody></table></div>` +
    `<div class="vip-adv-section-title">投委会 4 席评审</div>` +
    `<div class="vip-committee-grid">${members}</div>` +
    ((result.unverified || []).length ? `<div class="vip-adv-foot warn">⚠ 未在账户数据中核到的数字：${result.unverified.map(esc).join('、')}</div>` : '') +
    (result.disclaimer ? `<div class="vip-adv-foot muted">${esc(result.disclaimer)}</div>` : '');
}

async function loadReports() {
  if (requireConcreteAccount('reports')) return;
  clearScopeHint('reports');
  const box = document.getElementById('vip-report-list');
  if (!box) return;
  try {
    const { reports } = await vipGet(`/reports?account_ref=${encodeURIComponent(selectedAccountRef())}`);
    if (!reports?.length) { box.innerHTML = '<p class="st-empty-hint">暂无报告</p>'; return; }
    box.innerHTML = reports.map(r =>
      `<div class="vip-report-item" data-report-id="${esc(r.id)}" style="padding:8px;border-bottom:1px solid var(--border);cursor:pointer">` +
      `<div style="font-weight:600">${esc(r.period || r.kind)}</div>` +
      `<div style="font-size:12px;color:var(--muted)">${esc(r.created_at || '')}</div></div>`).join('');
    box.querySelectorAll('.vip-report-item').forEach(el => el.addEventListener('click', () => openReport(el.dataset.reportId)));
  } catch (_) { box.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}
async function openReport(id) {
  const viewer = document.getElementById('vip-report-viewer');
  if (!viewer) return;
  viewer.innerHTML = '加载中…';
  try {
    const data = await vipGet(`/reports/${id}`);
    const md = data.report_md || '';
    viewer.innerHTML = window.marked ? window.marked.parse(md) : `<pre style="white-space:pre-wrap">${esc(md)}</pre>`;
  } catch (_) { viewer.innerHTML = '<p class="st-empty-hint">加载失败</p>'; }
}

function bindReportGen() {
  document.getElementById('vip-report-btn')?.addEventListener('click', async () => {
    if (requireConcreteAccount('reports')) return;
    clearScopeHint('reports');
    const btn = document.getElementById('vip-report-btn');
    btn.disabled = true;
    setStatus('vip-report-status', '生成中…（含 AI 分析约需数十秒）', true);
    try {
      const withAi = document.getElementById('vip-with-ai').checked;
      const ref = selectedAccountRef();
      const r = await fetch(vipApiUrl(`/reports/generate?with_ai=${withAi}&account_ref=${encodeURIComponent(ref)}`), { method: 'POST' });
      const data = await r.json();
      if (!r.ok) { setStatus('vip-report-status', '✗ ' + (data.detail || '生成失败'), false); return; }
      const box = document.getElementById('vip-report');
      const md = data.report_md || '';
      box.innerHTML = window.marked ? window.marked.parse(md) : `<pre style="white-space:pre-wrap">${esc(md)}</pre>`;
      const nUnv = (data.unverified || []).length;
      setStatus('vip-report-status', nUnv ? `✓ 已生成（${nUnv} 处数字未核到，已标注）` : '✓ 已生成', true);
      invalidateVipCache({ accountRef: ref, includeImport: false });
      ensureVipLoaded();
    } catch (e) {
      setStatus('vip-report-status', '✗ 生成失败: ' + e.message, false);
    } finally {
      btn.disabled = false;
    }
  });
}

function appendChat(role, text) {
  const log = document.getElementById('vip-chat-log');
  if (!log) return;
  const who = role === 'user' ? '你' : '顾问';
  const div = document.createElement('div');
  div.style.marginBottom = '10px';
  div.innerHTML = `<strong>${who}：</strong>` + (window.marked && role === 'assistant'
    ? window.marked.parse(text || '')
    : `<pre style="white-space:pre-wrap;display:inline;margin:0">${esc(text)}</pre>`);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}
async function sendChat() {
  if (requireConcreteAccount('chat')) return;
  clearScopeHint('chat');
  const input = document.getElementById('vip-chat-input');
  const q = (input?.value || '').trim();
  if (!q) return;
  const btn = document.getElementById('vip-chat-send');
  btn.disabled = true;
  setStatus('vip-chat-status', '顾问思考中…', true);
  appendChat('user', q);
  input.value = '';
  let aiBox = '';
  try {
    const resp = await fetch('/api/vip/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: vipState.sessionId, question: q, market: market(), account_ref: selectedAccountRef() }),
    });
    if (!resp.ok) { const j = await resp.json().catch(() => ({})); throw new Error(j.detail || `HTTP ${resp.status}`); }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '', curEvent = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop() || '';
      for (const raw of lines) {
        const line = raw.trimEnd();
        if (line.startsWith('event:')) { curEvent = line.slice(6).trim(); continue; }
        if (!line.startsWith('data:')) continue;
        const data = JSON.parse(line.slice(5).trim());
        if (curEvent === 'error') throw new Error(data.message || '咨询失败');
        if (curEvent === 'session' && data.session_id) vipState.sessionId = data.session_id;
        else if (curEvent === 'chunk' && data.text) aiBox += data.text;
        else if (curEvent === 'done' && data.session_id) vipState.sessionId = data.session_id;
        curEvent = '';
      }
    }
    appendChat('assistant', aiBox);
    setStatus('vip-chat-status', '✓ 已回复', true);
  } catch (e) {
    setStatus('vip-chat-status', '✗ ' + e.message, false);
  } finally {
    btn.disabled = false;
  }
}

async function gateVipButton() {
  const btn = document.getElementById('btn-vip');
  if (!btn) return;
  try {
    const r = await fetch('/api/auth/me');
    if (!r.ok) return;
    const u = await r.json();
    btn.style.display = u.vip ? '' : 'none';
  } catch (_) {}
}

export function initVip() {
  if (vipState.inited) return;
  const view = document.getElementById('view-vip');
  if (!view) return;
  vipState.inited = true;
  gateVipButton();
  window.addEventListener('resize', () => Object.values(vipState.charts).forEach(c => c?.resize?.()));

  bindImportCenter();
  bindReportGen();
  document.getElementById('vip-history-search')?.addEventListener('click', loadTransactions);
  document.getElementById('vip-chat-send')?.addEventListener('click', sendChat);
  document.getElementById('vip-chat-new')?.addEventListener('click', () => {
    vipState.sessionId = '';
    const log = document.getElementById('vip-chat-log');
    if (log) log.innerHTML = '';
    setStatus('vip-chat-status', '已新建会话', true);
  });
  const openDrawer = () => openAccountsDrawer();
  document.getElementById('vip-account-manage')?.addEventListener('click', openDrawer);
  document.getElementById('vip-overview-manage')?.addEventListener('click', openDrawer);
  document.getElementById('vip-import-manage')?.addEventListener('click', openDrawer);
  document.getElementById('vip-toolbar-import')?.addEventListener('click', () => setPrimaryTab(VIP_PRIMARY_IMPORT));
  document.getElementById('vip-accounts-close')?.addEventListener('click', closeAccountsDrawer);
  document.getElementById('vip-accounts-drawer')?.addEventListener('click', e => {
    if (e.target.id === 'vip-accounts-drawer') closeAccountsDrawer();
  });
  document.getElementById('vip-af-save')?.addEventListener('click', saveAccountForm);
  document.getElementById('vip-af-reset')?.addEventListener('click', resetAccountForm);

  document.getElementById('vip-account-log-btn')?.addEventListener('click', openAccountLogDrawer);
  document.getElementById('vip-log-close')?.addEventListener('click', closeAccountLogDrawer);
  document.getElementById('vip-log-drawer')?.addEventListener('click', e => {
    if (e.target.id === 'vip-log-drawer') closeAccountLogDrawer();
  });
  document.getElementById('vip-log-refresh')?.addEventListener('click', loadAccountLog);
  document.getElementById('vip-log-filter')?.addEventListener('change', loadAccountLog);

  document.getElementById('vip-market')?.addEventListener('change', async () => {
    setStatus('vip-account-status', '加载账户中…', true);
    await loadVipAccounts(currentPrimaryAccountRef() || VIP_ALL_ACCOUNTS, { reloadActive: true });
  });
  document.getElementById('vip-account-ref')?.addEventListener('change', () => {
    const ref = selectedAccountRef();
    if (ref) setPrimaryTab(primaryTabKeyForAccount(ref), { statusMsg: `已切换到账户：${activeAccountLabel()}` });
    else setPrimaryTab(VIP_PRIMARY_OVERVIEW, { statusMsg: '已切换到个人资产总览' });
  });
  document.getElementById('vip-import-account')?.addEventListener('change', () => {
    invalidateVipCache({ accountRef: selectedImportAccountRef(), includeOverview: false });
    ensureVipLoaded();
  });
  loadVipAccounts(VIP_ALL_ACCOUNTS).then(() => {
    renderPrimaryTabs();
    renderAccountSubtabs();
    updateAccountHeading();
    showActivePane();
    ensureVipLoaded();
  });
}
