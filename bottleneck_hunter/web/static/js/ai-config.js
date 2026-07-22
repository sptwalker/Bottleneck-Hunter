/**
 * ai-config.js — 统一 AI 配置页面
 * 区块: Provider 管理 / 综合测试 / 模型分配矩阵 / 调度看板
 */

const API = '/api/ai-config';

const GROUP_LABELS = {
  decision: '决策层级',
  committee: '投资委员会',
  pipeline: '产业链管线',
  watchlist: '看板模块',
  bottleneck: '瓶颈交叉评分',
};

// 系统模块层级（流程顺序）—— 供分配页签 / 侧栏树 统一排序
const MODULE_TREE = [
  { module: '产业链分析', groups: ['pipeline', 'bottleneck'] },
  { module: '观察池',     groups: ['watchlist'] },
  { module: '决策中心',   groups: ['decision', 'committee'] },
];

const DIMENSION_LABELS = {
  connectivity: '连通性',
  json_output: 'JSON',
  chinese_analysis: '中文分析',
  speed: '速度',
  scoring_variance: '评分力',
  instruction_follow: '指令遵循',
  chain_decompose: '拆解力',
};

function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function scoreClass(score) {
  if (score >= 7) return 'aic-score-high';
  if (score >= 4) return 'aic-score-mid';
  return 'aic-score-low';
}

/* ── State ────────────────────────────────────────────── */

let _roles = [];
let _providers = [];
let _customProviders = [];
let _testResults = [];
let _activeModule = '产业链分析';
let _providerHealth = {};   // provider_id -> {ok_rate, cooldown_s}（来自 /model-usage，健康点用）
let _rolesLoaded = false;    // /roles 是否至少成功返回过一次（configured 判定就绪门闩，防竞态误弹引导卡）

// 免费档 provider（与后端 health._PROVIDER_TIER 对应，前端仅用于引导徽标/推荐排序）。
// ponytail: 小白名单，非精确 tier；新增自定义 provider 默认按"未知档"处理。
const FREE_PROVIDERS = new Set(['deepseek', 'qwen', 'glm', 'kimi', 'siliconflow']);
const RECOMMEND_ORDER = ['deepseek', 'qwen', 'glm', 'kimi'];  // 引导优先推荐的国内免费模型

/* ── Init ─────────────────────────────────────────────── */

export function initAIConfig() {
  const container = document.getElementById('view-aiconfig');
  if (!container) return;

  loadRoles();
  loadTestResults();
  loadCustomProviders();
  loadAutoUpdateSummary();
  loadPaidSources();
  loadProviderHealth();   // 融入模型卡片的成功率/熔断状态（增强，失败静默）
  loadSchedPolicy();      // 模型使用策略 + 一键自动配置（在 API KEY 管理页）

  // Provider actions
  container.querySelector('#aic-test-conn')?.addEventListener('click', testConnectivity);

  // Custom endpoint actions
  container.querySelector('#aic-add-custom')?.addEventListener('click', () => showCustomForm());
  container.querySelector('#aic-custom-cancel')?.addEventListener('click', hideCustomForm);
  container.querySelector('#aic-custom-save')?.addEventListener('click', saveCustomProvider);
  container.querySelector('#aic-custom-test')?.addEventListener('click', testFormConfig);

  // Provider 编辑/删除：事件委托（避免内联 onclick 因名称含引号等被截断）
  container.querySelector('#aic-provider-grid')?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-aic-act]');
    if (!btn) return;
    const id = btn.dataset.pid;
    const isCustom = btn.dataset.custom === '1';
    if (btn.dataset.aicAct === 'edit') editProvider(id, isCustom);
    else if (btn.dataset.aicAct === 'delete') deleteProvider(id, btn.dataset.name || id, isCustom);
    else if (btn.dataset.aicAct === 'primary') setProviderPrimary(id, btn.dataset.name || id);
    else if (btn.dataset.aicAct === 'disable') toggleProviderActive(id, btn.dataset.name || id, btn.dataset.active === '1');
  });

  // Auto-generate ID from name
  container.querySelector('#aic-custom-name')?.addEventListener('input', (e) => {
    const idInput = container.querySelector('#aic-custom-id');
    const editId = container.querySelector('#aic-custom-edit-id')?.value;
    if (idInput && !editId) {
      idInput.value = e.target.value
        .toLowerCase().replace(/[^a-z0-9一-鿿]/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '').slice(0, 32);
    }
  });

  // Test actions（用箭头包一层：addEventListener 会把 click 事件当首参传入，直接绑定会误触发增量）
  container.querySelector('#aic-start-test')?.addEventListener('click', () => startComprehensiveTest(false));
  container.querySelector('#aic-incr-test')?.addEventListener('click', () => startComprehensiveTest(true));

  // Matrix actions
  container.querySelector('#aic-save-matrix')?.addEventListener('click', saveMatrixConfig);
  // 切换 provider 下拉 → 联动把「模型名」填成该 provider 的默认模型（选“未配置”则清空）。
  // 委托绑在稳定容器上：renderMatrixForModule 只替换其 innerHTML、不换容器，故只需绑一次。
  container.querySelector('#aic-matrix-list')?.addEventListener('change', (e) => {
    const sel = e.target.closest?.('.aic-slot-provider');
    if (!sel) return;
    const modelInp = sel.closest('.aic-slot')?.querySelector('.aic-provider-input');
    if (!modelInp) return;
    const p = _providers.find(x => x.id === sel.value);
    modelInp.value = sel.value ? (p?.default_model || '') : '';
  });

  // Module tabs（分配矩阵按系统模块）
  container.querySelectorAll('.aic-module-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.aic-module-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      _activeModule = tab.dataset.module;
      renderMatrixForModule(_activeModule);
    });
  });

  // 顶层页签：API KEY 管理 / AI 模型分配 / 自动更新管理
  container.querySelectorAll('.aic-main-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      container.querySelectorAll('.aic-main-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const pane = tab.dataset.tab;
      container.querySelectorAll('.aic-tab-pane').forEach(p => {
        p.classList.toggle('active', p.dataset.pane === pane);
      });
      if (pane === 'scheduler') loadScheduler();
    });
  });
}

/* ── Data Loading ────────────────────────────────────── */

async function loadRoles() {
  try {
    const resp = await fetch(`${API}/roles`);
    if (!resp.ok) {
      console.warn('loadRoles failed:', resp.status);
      renderSidebarError(resp.status === 401 ? '请先登录' : `加载失败 (${resp.status})`);
      return;
    }
    const data = await resp.json();
    _roles = data.roles || [];
    _providers = data.available_providers || [];
    _rolesLoaded = true;   // configured 数据已就绪，引导态可安全渲染
    renderProviders();
    renderConfiguredModelSummary();
    renderMatrixForModule(_activeModule);
  } catch (e) {
    console.error('Failed to load roles:', e);
    renderSidebarError('网络错误');
  }
}

function renderSidebarError(msg) {
  const body = document.getElementById('aic-sidebar-body');
  if (body) body.innerHTML = `<div class="aic-sidebar-empty">${escHtml(msg)}<br><button class="btn btn-xs" style="margin-top:8px" onclick="window._aicRetryLoad()">重试</button></div>`;
}

window._aicRetryLoad = () => { loadRoles(); loadCustomProviders(); loadTestResults(); };

async function loadTestResults() {
  try {
    const resp = await fetch(`${API}/test/results`);
    if (!resp.ok) return;
    const data = await resp.json();
    _testResults = data.results || [];
    renderTestResults();
  } catch (e) {
    console.error('Failed to load test results:', e);
  }
}

/* ── Section 1: Provider Management ──────────────────── */

function renderProviders() {
  const grid = document.getElementById('aic-provider-grid');
  if (!grid) return;

  const isAdmin = window.appState?.user?.role === 'admin';
  // 「+ 添加 Provider」（新增共享定义）仅管理员可见。在此设置而非 init，
  // 以避免 initAIConfig 早于 auth 加载导致 role 未就绪时误隐藏。
  const addBtn = document.getElementById('aic-add-custom');
  if (addBtn) addBtn.style.display = isAdmin ? '' : 'none';

  try {
    // 「当前用户是否已配 Key」来自 _providers（按用户，含 configured/key_hint），
    // 与 _customProviders（定义 + is_active/is_primary）按 id 合并。
    const cfgMap = {};
    _providers.forEach(p => { cfgMap[p.id] = p; });
    const configuredCount = _customProviders.filter(cp => cfgMap[cp.provider_id]?.configured).length;

    // 排序：未配 + 免费 + 推荐的靠前（引导用户先配免费模型），已配的其次
    const list = [..._customProviders].sort((a, b) => {
      const ac = cfgMap[a.provider_id]?.configured ? 1 : 0;
      const bc = cfgMap[b.provider_id]?.configured ? 1 : 0;
      if (ac !== bc) return ac - bc;                       // 未配的靠前
      const ar = RECOMMEND_ORDER.indexOf(a.provider_id);
      const br = RECOMMEND_ORDER.indexOf(b.provider_id);
      if (ar !== br) return (ar < 0 ? 99 : ar) - (br < 0 ? 99 : br);  // 推荐的靠前
      return String(a.display_name || a.provider_id).toLowerCase()
        .localeCompare(String(b.display_name || b.provider_id).toLowerCase());
    });

    // 首次引导 / 已就绪状态条：仅在 /roles 就绪(configured 可信) 且有 provider 卡片时渲染，
    // 避免竞态/空目录/加载失败时对已配用户误弹"你还没配 Key"引导卡。
    if (_rolesLoaded && list.length) {
      renderOnboardState(configuredCount);
    } else {
      const onboard = document.getElementById('aic-onboard');
      const banner = document.getElementById('aic-ready-banner');
      if (onboard) onboard.innerHTML = '';
      if (banner) banner.innerHTML = '';
    }

    if (!list.length) {
      grid.innerHTML = isAdmin
        ? '<div class="aic-provider-empty">暂无 Provider，点击上方「+ 添加 Provider」配置。</div>'
        : '<div class="aic-provider-empty">暂无可用模型，请联系管理员开通后再配置密钥。</div>';
      return;
    }

    grid.innerHTML = list.map(cp => {
      const id = cp.provider_id;
      const displayName = cp.display_name || id;
      const model = cp.default_model || '';
      const isDisabled = cp.is_active === 0;
      const isPrimary = cp.is_primary === 1;
      const cfg = cfgMap[id] || {};
      const configured = !!cfg.configured;
      const keyHint = cfg.key_hint || '';
      const health = _providerHealth[id];

      // 健康点：未配→灰；禁用→灰；熔断→红；成功率<70%→黄；否则绿
      let dotClass = 'aic-status-unknown', stateText = '未配置';
      if (isDisabled) { dotClass = 'aic-status-unknown'; stateText = '已禁用'; }
      else if (configured) {
        stateText = keyHint ? `已连接 · ${escHtml(keyHint)}` : '已连接';
        if (health && health.cooldown_s > 0) { dotClass = 'aic-status-fail'; stateText += ' · 暂时不可用'; }
        else if (health && health.calls >= 5 && health.ok_rate < 70) { dotClass = 'aic-status-warn'; stateText += ` · 成功率${health.ok_rate}%`; }
        else { dotClass = 'aic-status-ok'; if (health && health.calls >= 5) stateText += ` · 成功率${health.ok_rate}%`; }
      }

      const freeBadge = FREE_PROVIDERS.has(id) ? '<span class="aic-tier-badge free">免费</span>' : '';
      const primaryBadge = isPrimary ? '<span class="aic-provider-primary-badge" title="全局主要模型（默认+兜底优先）">主要</span>' : '';
      const editLabel = isAdmin ? '编辑' : (configured ? '换密钥' : '填入密钥');
      const editCls = (!configured && !isAdmin) ? 'btn-primary' : '';
      const delBtn = isAdmin
        ? `<button class="btn btn-xs btn-danger" data-aic-act="delete" data-pid="${escHtml(id)}" data-name="${escHtml(displayName)}" data-custom="1">删除</button>`
        : '';
      const primaryBtn = (isAdmin && !isPrimary && !isDisabled)
        ? `<button class="btn btn-xs" data-aic-act="primary" data-pid="${escHtml(id)}" data-name="${escHtml(displayName)}" title="设为全局主要模型（默认+兜底优先）">设为主要</button>`
        : '';
      const disableBtn = isAdmin
        ? `<button class="btn btn-xs aic-btn-disable" data-aic-act="disable" data-pid="${escHtml(id)}" data-name="${escHtml(displayName)}" data-active="${isDisabled ? '0' : '1'}">${isDisabled ? '启用' : '禁用'}</button>`
        : '';
      return `
      <div class="aic-provider-item${isDisabled ? ' is-disabled' : ''}${configured ? ' is-configured' : ''}" data-pid="${escHtml(id)}">
        <div class="aic-provider-row-top">
          <span class="aic-provider-status ${dotClass}"></span>
          <div class="aic-provider-info">
            <span class="aic-provider-name">${escHtml(displayName)}${freeBadge}${primaryBadge}</span>
            <span class="aic-provider-state">${stateText}${model ? ` · ${escHtml(model)}` : ''}</span>
          </div>
        </div>
        <div class="aic-provider-actions-inline">
          <button class="btn btn-xs ${editCls}" data-aic-act="edit" data-pid="${escHtml(id)}" data-custom="1">${editLabel}</button>
          ${primaryBtn}${disableBtn}${delBtn}
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    console.error('renderProviders error:', e);
  }
}

// 首次引导（未配任何 Key）/ 已就绪状态条
function renderOnboardState(configuredCount) {
  const onboard = document.getElementById('aic-onboard');
  const banner = document.getElementById('aic-ready-banner');
  if (!onboard || !banner) return;
  if (configuredCount === 0) {
    onboard.innerHTML = `
      <div class="aic-onboard-card">
        <div class="aic-onboard-title">让 AI 开始为你工作</div>
        <div class="aic-onboard-desc">在下面任选一个模型填入密钥即可 —— 系统会自动挑选、自动容错、越用越准，无需手动配置。</div>
        <div class="aic-onboard-rec">💡 国内推荐免费模型（无需翻墙）：<b>DeepSeek</b> / <b>通义千问</b> / <b>智谱GLM</b> / <b>Kimi</b>，填 1 个即可开始，2–3 个容错更稳。</div>
      </div>`;
    banner.innerHTML = '';
  } else {
    onboard.innerHTML = '';
    banner.innerHTML = `<div class="aic-ready-banner">✓ 已就绪 · 已连接 ${configuredCount} 个模型，系统将自动为各环节挑选并容错，无需手动分配。</div>`;
  }
}

// 拉一次模型健康数据（成功率/熔断），融进 provider 卡片。失败静默（健康只是增强）。
async function loadProviderHealth() {
  try {
    const res = await fetch('/api/decision/model-usage?days=14');
    if (!res.ok) return;
    const data = await res.json();
    const map = {};
    (data.stats || []).forEach(r => {
      // 同 provider 多模型：取调用最多的那条代表健康
      if (!map[r.provider] || r.calls > map[r.provider].calls) {
        map[r.provider] = { ok_rate: r.ok_rate, calls: r.calls, cooldown_s: r.cooldown_s || 0 };
      }
    });
    _providerHealth = map;
    renderProviders();
  } catch { /* 忽略 */ }
}

/* ── Custom Endpoint Management ─────────────────────── */

const CUSTOM_API = '/api/custom-providers';

async function loadCustomProviders() {
  try {
    const resp = await fetch(CUSTOM_API);
    if (!resp.ok) return;
    const data = await resp.json();
    _customProviders = data.providers || [];
    renderProviders();
    renderConfiguredModelSummary();
  } catch (e) {
    console.error('Failed to load custom providers:', e);
  }
}

function renderCustomProviders() {
  renderProviders();
  renderConfiguredModelSummary();
}

// Expose to onclick handlers
window._aicTestCustom = testCustomProvider;
window._aicEditCustom = editCustomProvider;
window._aicDeleteCustom = deleteCustomProvider;
window._aicEditProvider = editProvider;
window._aicDeleteProvider = deleteProvider;

let _autoUpdateCfg = null;

async function loadAutoUpdateSummary() {
  try {
    const resp = await fetch('/api/settings/auto-update');
    if (!resp.ok) return;
    _autoUpdateCfg = await resp.json();
    renderConfiguredModelSummary();
  } catch { /* 忽略 */ }
}

function _autoUpdateSummaryHtml() {
  if (!_autoUpdateCfg) return '';
  const cfg = _autoUpdateCfg.config || {};
  const labels = _autoUpdateCfg.category_labels || {};
  const master = cfg.master_enabled === '1';
  const cats = Object.keys(labels);
  const onCats = cats.filter(c => cfg[c] === '1');
  let html = `<div class="aic-tree-module aic-tree-au">`;
  html += `<div class="aic-tree-module-title">自动更新设置</div>`;
  html += `<div class="aic-tree-role"><span class="aic-tree-role-name">总开关</span>`;
  html += `<span class="aic-tree-model ${master ? '' : 'aic-tree-model-default'}">${master ? '已启用' : '已关闭'}</span></div>`;
  if (cats.length) {
    html += `<div class="aic-tree-role"><span class="aic-tree-role-name">分类</span>`;
    html += `<span class="aic-tree-model">${onCats.length}/${cats.length} 开启</span></div>`;
    for (const c of cats) {
      html += `<div class="aic-tree-role aic-tree-role-sub"><span class="aic-tree-role-name">${escHtml(labels[c] || c)}</span>`;
      html += `<span class="aic-tree-model ${cfg[c] === '1' ? '' : 'aic-tree-model-default'}">${cfg[c] === '1' ? '开' : '关'}</span></div>`;
    }
  }
  html += `<div class="aic-tree-role"><span class="aic-tree-role-name">陈旧阈值</span>`;
  html += `<span class="aic-tree-model">${escHtml(String(cfg.stale_threshold_hours || '24'))}h</span></div>`;
  html += `</div>`;
  return html;
}

function renderConfiguredModelSummary() {
  const body = document.getElementById('aic-sidebar-body');
  const countEl = document.getElementById('aic-sidebar-count');
  if (!body) return;

  const rolesByGroup = {};
  for (const r of _roles) (rolesByGroup[r.group] = rolesByGroup[r.group] || []).push(r);

  let assigned = 0;
  const models = new Set();
  let html = '<div class="aic-tree">';
  for (const mod of MODULE_TREE) {
    const groups = mod.groups.filter(g => (rolesByGroup[g] || []).length);
    if (!groups.length) continue;
    html += `<div class="aic-tree-module"><div class="aic-tree-module-title">${escHtml(mod.module)}</div>`;
    for (const g of groups) {
      for (const role of rolesByGroup[g]) {
        const slots = role.slots || [];
        if (slots.length) {
          assigned++;
          const ms = slots.map(s => `${s.provider}:${s.model}`);
          ms.forEach(m => models.add(m));
          html += `<div class="aic-tree-role"><span class="aic-tree-role-name">${escHtml(role.label)}</span>`;
          html += ms.map(m => `<span class="aic-tree-model">${escHtml(m)}</span>`).join('');
          html += `</div>`;
        } else {
          html += `<div class="aic-tree-role"><span class="aic-tree-role-name">${escHtml(role.label)}</span>`;
          html += `<span class="aic-tree-model aic-tree-model-default">默认</span></div>`;
        }
      }
    }
    html += `</div>`;
  }
  html += '</div>';
  html += _autoUpdateSummaryHtml();

  body.innerHTML = html || '<div class="aic-sidebar-empty">暂无配置</div>';
  if (countEl) countEl.textContent = `${assigned} 已分配 · ${models.size} 模型`;
}

function showCustomForm(editData) {
  const form = document.getElementById('aic-custom-form');
  if (!form) return;

  const nameEl = document.getElementById('aic-custom-name');
  const show = (id, on) => { const el = document.getElementById(id); if (el) el.style.display = on ? '' : 'none'; };
  const isAdmin = window.appState?.user?.role === 'admin';

  if (editData && typeof editData === 'object' && editData.provider_id) {
    // 编辑现有 provider：管理员可改共享定义；普通用户只配自己的 API Key（隐藏定义字段）
    show('aic-field-name', isAdmin); show('aic-field-id', isAdmin);
    show('aic-field-url', isAdmin); show('aic-field-model', isAdmin);
    document.getElementById('aic-custom-edit-id').value = editData.provider_id;
    nameEl.value = editData.display_name || '';
    nameEl.disabled = false;
    document.getElementById('aic-custom-id').value = editData.provider_id || '';
    document.getElementById('aic-custom-id').disabled = true;
    document.getElementById('aic-custom-url').value = editData.base_url || '';
    document.getElementById('aic-custom-url').placeholder = '留空 = 官方默认端点（openai/anthropic/google）';
    document.getElementById('aic-custom-key').value = '';
    document.getElementById('aic-custom-key').placeholder = editData.api_key_hint ? '已配置（留空保持不变）' : '可选';
    document.getElementById('aic-custom-model').value = editData.default_model || '';
  } else {
    // 新增 provider
    show('aic-field-name', true); show('aic-field-id', true);
    show('aic-field-url', true); show('aic-field-model', true);
    document.getElementById('aic-custom-edit-id').value = '';
    nameEl.value = '';
    nameEl.disabled = false;
    document.getElementById('aic-custom-id').value = '';
    document.getElementById('aic-custom-id').disabled = false;
    document.getElementById('aic-custom-url').value = '';
    document.getElementById('aic-custom-url').placeholder = '如：http://localhost:11434/v1（OpenAI 兼容留空走默认）';
    document.getElementById('aic-custom-key').value = '';
    document.getElementById('aic-custom-key').placeholder = '可选（如 Ollama 无需填写）';
    document.getElementById('aic-custom-model').value = '';
  }

  form.style.display = 'block';
  document.getElementById('aic-custom-model').focus();
}

function hideCustomForm() {
  const form = document.getElementById('aic-custom-form');
  if (form) {
    form.style.display = 'none';
  }
  document.getElementById('aic-custom-id').disabled = false;
  const nameEl = document.getElementById('aic-custom-name');
  if (nameEl) nameEl.disabled = false;
}

async function saveCustomProvider() {
  const editId = document.getElementById('aic-custom-edit-id')?.value;
  const display_name = document.getElementById('aic-custom-name')?.value.trim();
  const provider_id = document.getElementById('aic-custom-id')?.value.trim();
  const base_url = document.getElementById('aic-custom-url')?.value.trim();
  const api_key = document.getElementById('aic-custom-key')?.value.trim();
  const default_model = document.getElementById('aic-custom-model')?.value.trim();

  if (!display_name) { alert('请输入显示名称'); return; }
  if (!provider_id) { alert('请输入标识符 ID'); return; }
  if (!default_model) { alert('请输入默认模型'); return; }
  // base_url 可留空：openai/anthropic/google 走各自 SDK 官方端点；其余 OpenAI 兼容必须填
  if (!base_url && !['openai', 'anthropic', 'google'].includes(provider_id)) {
    alert('请输入 API 地址（仅 openai/anthropic/google 可留空走官方端点）');
    return;
  }

  const body = { provider_id, display_name, base_url: base_url || '', default_model, api_key: api_key || '' };

  try {
    let resp;
    if (editId) {
      resp = await fetch(`${CUSTOM_API}/${editId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    } else {
      resp = await fetch(CUSTOM_API, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }

    if (resp.ok) {
      hideCustomForm();
      await loadCustomProviders();
      await loadRoles();
      setStatus('aic-provider-status', editId ? 'Provider 已更新' : 'Provider 已添加', 'ok');
      // 新增/改动接口后：显示闪烁的「新接口增量测试」，点击只补测未测过的接口
      const incrBtn = document.getElementById('aic-incr-test');
      if (incrBtn) { incrBtn.style.display = ''; incrBtn.classList.add('aic-incr-flash'); }
    } else {
      const err = await resp.json().catch(() => ({}));
      alert(err.detail || '保存失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function testFormConfig() {
  const btn = document.getElementById('aic-custom-test');
  const provider = document.getElementById('aic-custom-id')?.value.trim();
  const model = document.getElementById('aic-custom-model')?.value.trim() || '';
  const base_url = document.getElementById('aic-custom-url')?.value.trim() || '';
  const api_key = document.getElementById('aic-custom-key')?.value.trim() || '';
  if (!provider) { alert('请先填写 Provider'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  try {
    const resp = await fetch(`${API}/test/one`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model, base_url, api_key }),
    });
    let data = {};
    try { data = await resp.json(); } catch { /* 非 JSON */ }
    if (!resp.ok) {
      const detail = (data && (data.detail || data.error)) || '';
      alert(`测试请求失败 (HTTP ${resp.status})${detail ? '：' + (typeof detail === 'string' ? detail : JSON.stringify(detail)) : '；请确认已登录并刷新页面'}`);
      return;
    }
    if (data.ok) alert(`连接成功！\n模型: ${data.model || model}`);
    else alert(`连接失败：${data.error || '未返回错误信息'}`);
  } catch (e) {
    alert('测试失败: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试连通性'; }
  }
}

async function testCustomProvider(id, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  try {
    const resp = await fetch(`${CUSTOM_API}/${id}/test`, { method: 'POST' });
    const data = await resp.json();
    const dot = document.querySelector(`.aic-provider-item[data-pid="${id}"] .aic-provider-status`);
    if (data.ok) {
      alert(`连接成功！\n模型: ${data.model || id}`);
      if (dot) dot.className = 'aic-provider-status aic-status-ok';
    } else {
      alert(`连接失败: ${data.error || '未知错误'}`);
      if (dot) dot.className = 'aic-provider-status aic-status-fail';
    }
  } catch (e) {
    alert('测试失败: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试'; }
  }
}

function editCustomProvider(id) {
  const p = _customProviders.find(cp => cp.provider_id === id);
  if (!p) return;
  showCustomForm(p);
}

// 统一真源后，所有 Provider 皆为 custom_providers 行，编辑/删除单轨（isCustom 参数保留兼容调用点）
function editProvider(id) {
  editCustomProvider(id);
}

async function deleteProvider(id, name) {
  await deleteCustomProvider(id, name);
}

async function deleteCustomProvider(id, name) {
  if (!confirm(`确定删除「${name}」？\n将清除其 API Key 与模型/端点/名称配置，使用该 Provider 的角色分配将失效。`)) return;

  try {
    const resp = await fetch(`${CUSTOM_API}/${id}`, { method: 'DELETE' });
    if (resp.ok) {
      await loadCustomProviders();
      await loadRoles();
      setStatus('aic-provider-status', 'Provider 已删除', 'ok');
    } else {
      alert('删除失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function setProviderPrimary(id, name) {
  try {
    const resp = await fetch(`${CUSTOM_API}/${id}/primary`, { method: 'POST' });
    if (resp.ok) {
      await loadCustomProviders();
      await loadRoles();
      window.invalidateFollowModel?.();  // 失效卡片预解析缓存，主模型改动即时反映
      setStatus('aic-provider-status', `已将「${name}」设为主要模型（默认+兜底优先）`, 'ok');
    } else {
      alert(resp.status === 403 ? '仅管理员可设置主要模型' : '设置主要失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function toggleProviderActive(id, name, currentlyActive) {
  const disabling = currentlyActive;
  if (disabling && !confirm(`确定禁用「${name}」？\n所有使用它的角色分配将自动替换为可用模型（优先主要模型）。`)) return;
  try {
    const resp = await fetch(`${CUSTOM_API}/${id}/toggle-active`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: !currentlyActive }),
    });
    if (resp.ok) {
      const data = await resp.json();
      await loadCustomProviders();
      await loadRoles();
      if (disabling) {
        setStatus('aic-provider-status', `已禁用「${name}」，${data.replaced || 0} 处角色配置已替换为可用模型`, 'ok');
      } else {
        setStatus('aic-provider-status', `已启用「${name}」`, 'ok');
      }
    } else {
      alert(resp.status === 403 ? '仅管理员可启用/禁用 Provider' : '操作失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function testConnectivity() {
  const btn = document.getElementById('aic-test-conn');
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  setStatus('aic-provider-status', '正在测试...', 'info');

  try {
    const resp = await fetch(`${API}/test/connectivity`, { method: 'POST' });
    if (!resp.ok) throw new Error('test failed');
    const data = await resp.json();
    const results = data.results || [];

    results.forEach(r => {
      const item = document.querySelector(`.aic-provider-item[data-pid="${r.provider}"]`);
      if (!item) return;
      const dot = item.querySelector('.aic-provider-status');
      if (dot) {
        dot.className = `aic-provider-status ${r.score > 0 ? 'aic-status-ok' : 'aic-status-fail'}`;
      }
    });

    const ok = results.filter(r => r.score > 0).length;
    setStatus('aic-provider-status', `测试完成: ${ok}/${results.length} 可用`, ok > 0 ? 'ok' : 'fail');
  } catch (e) {
    setStatus('aic-provider-status', '测试失败', 'fail');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '测试连通性'; }
  }
}

/* ── Section 2: Comprehensive Testing ──────────────────── */

async function startComprehensiveTest(incremental = false) {
  const btn = document.getElementById('aic-start-test');
  const incrBtn = document.getElementById('aic-incr-test');
  if (btn) btn.disabled = true;
  if (incrBtn) incrBtn.disabled = true;
  const activeBtn = incremental ? incrBtn : btn;
  if (activeBtn) activeBtn.textContent = incremental ? '增量测试中…' : '测试中...';

  const progressEl = document.querySelector('.aic-test-progress');
  const fillEl = document.getElementById('aic-test-progress-fill');
  const textEl = document.getElementById('aic-test-progress-text');
  if (progressEl) progressEl.classList.add('active');
  if (textEl) textEl.textContent = '正在连接...';
  if (fillEl) fillEl.style.width = '0%';

  _testResults = [];
  _testTotal = 0;
  _testDoneCount = 0;
  renderTestResults();

  try {
    const resp = await fetch(`${API}/test/comprehensive${incremental ? '?incremental=true' : ''}`, { method: 'POST' });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      const errMsg = errData.detail || `HTTP ${resp.status}`;
      if (textEl) textEl.textContent = `测试失败: ${errMsg}`;
      return;
    }

    if (!resp.body) {
      if (textEl) textEl.textContent = '浏览器不支持流式读取';
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      for (const line of lines) {
        const trimmed = line.replace(/\r$/, '');
        if (trimmed.startsWith('data:')) {
          try {
            const payload = JSON.parse(trimmed.slice(5).trim());
            handleTestEvent(payload, { fillEl, textEl });
          } catch { /* skip unparseable */ }
        }
      }
    }

    if (textEl) textEl.textContent = `测试完成: ${_testDoneCount} 个模型`;
  } catch (e) {
    console.error('Comprehensive test error:', e);
    if (textEl) textEl.textContent = `测试出错: ${e.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '开始综合测试'; }
    if (incrBtn) {
      incrBtn.disabled = false; incrBtn.textContent = '✨ 新接口增量测试';
      if (incremental) { incrBtn.style.display = 'none'; incrBtn.classList.remove('aic-incr-flash'); }  // 补测完即隐藏
    }
    setTimeout(() => { if (progressEl) progressEl.classList.remove('active'); }, 2000);
    await loadTestResults();
  }
}

let _testTotal = 0;
let _testDoneCount = 0;
const _testInProgress = {};

function handleTestEvent(payload, { fillEl, textEl }) {
  if (payload.total !== undefined) {
    _testTotal = payload.total;
    _testDoneCount = 0;
    return;
  }
  if (payload.provider && payload.composite_score !== undefined) {
    _testDoneCount++;
    const pct = _testTotal > 0 ? Math.round((_testDoneCount / _testTotal) * 100) : 0;
    if (fillEl) fillEl.style.width = pct + '%';
    if (textEl) textEl.textContent = `${_testDoneCount}/${_testTotal} 模型已完成`;

    _testResults.push({
      provider: payload.provider,
      model: payload.model,
      scores: payload.scores || {},
      composite_score: payload.composite_score,
    });
    renderTestResults();
    return;
  }
  if (payload.dimension && payload.provider) {
    const key = `${payload.provider}:${payload.model}`;
    if (!_testInProgress[key]) _testInProgress[key] = {};
    _testInProgress[key][payload.dimension] = payload.score;
    if (textEl) textEl.textContent = `测试 ${payload.provider} - ${DIMENSION_LABELS[payload.dimension] || payload.dimension}...`;
  }
}

function renderTestResults() {
  const tbody = document.getElementById('aic-test-tbody');
  if (!tbody) return;

  if (_testResults.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="aic-test-empty">暂无测试结果，点击"开始综合测试"</td></tr>';
    return;
  }

  const sorted = [..._testResults].sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0));
  tbody.innerHTML = sorted.map(r => {
    const dims = ['connectivity', 'json_output', 'chinese_analysis', 'speed', 'scoring_variance', 'instruction_follow', 'chain_decompose'];
    const cells = dims.map(d => {
      const s = r.scores?.[d] ?? '-';
      if (s === '-') return '<td class="aic-score-cell">-</td>';
      const cls = scoreClass(s);
      // 0 分带原因时：加 tooltip + 角标，区分「欠费/超时/连不通」vs「真拆不动」，免得逐个人肉重测
      const detail = r.scores?.[`${d}_detail`] || {};
      const reason = detail.fail_reason || detail.error || '';
      if (Number(s) === 0 && reason) {
        return `<td class="aic-score-cell ${cls}" title="${escHtml(reason)}">0.0<sup style="color:var(--danger,#dc2626)">!</sup></td>`;
      }
      return `<td class="aic-score-cell ${cls}">${Number(s).toFixed(1)}</td>`;
    }).join('');

    const cs = r.composite_score ?? 0;
    return `<tr>
      <td><strong>${escHtml(r.provider)}</strong><br><small style="color:var(--muted)">${escHtml(r.model)}</small></td>
      ${cells}
      <td class="aic-score-cell aic-composite-cell ${scoreClass(cs)}">${cs.toFixed(1)}</td>
    </tr>`;
  }).join('');
}

/* ── Section 3: Model Assignment Matrix ──────────────── */

function renderMatrixForModule(moduleName) {
  const listEl = document.getElementById('aic-matrix-list');
  if (!listEl) return;
  const mod = MODULE_TREE.find(m => m.module === moduleName) || MODULE_TREE[0];

  const providerOptions = _providers
    .filter(p => p.configured)
    .map(p => `<option value="${escHtml(p.id)}">${escHtml(p.name)}${p.default_model ? ` (${p.default_model})` : ''}</option>`)
    .join('');

  const roleItemHtml = (role) => {
    const slotsHtml = [];
    const numSlots = role.multi_model ? role.max_slots : 1;
    for (let i = 0; i < numSlots; i++) {
      const existing = (role.slots || []).find(s => s.slot_index === i);
      const slotLabel = role.multi_model ? ((role.slot_labels && role.slot_labels[i]) || `模型 ${i + 1}`) : '';
      const selectedModel = existing?.model || '';
      slotsHtml.push(`
        <div class="aic-slot" data-role="${escHtml(role.key)}" data-slot="${i}">
          ${slotLabel ? `<span class="aic-slot-label">${slotLabel}</span>` : ''}
          <select class="aic-slot-provider" data-role="${escHtml(role.key)}" data-slot="${i}">
            <option value="">未配置(使用默认)</option>
            ${providerOptions}
          </select>
          <input type="text" class="aic-provider-input" style="min-width:140px" placeholder="模型名"
                 value="${escHtml(selectedModel)}"
                 data-role="${escHtml(role.key)}" data-slot="${i}">
        </div>
      `);
    }
    const multiBadge = role.multi_model ? `<span class="aic-role-multi-badge">多模型 (最多${role.max_slots})</span>` : '';
    return `
      <div class="aic-role-item">
        <span class="aic-role-label">${escHtml(role.label)} ${multiBadge}</span>
        <div class="aic-role-slots">${slotsHtml.join('')}</div>
      </div>`;
  };

  let html = '';
  for (const g of mod.groups) {
    const groupRoles = _roles.filter(r => r.group === g);
    if (!groupRoles.length) continue;
    html += `<div class="aic-group-section"><div class="aic-group-heading">${escHtml(GROUP_LABELS[g] || g)}</div>`;
    html += groupRoles.map(roleItemHtml).join('');
    html += `</div>`;
  }
  listEl.innerHTML = html || '<div style="padding:16px;color:var(--muted);text-align:center">该模块暂无角色</div>';

  // 回填已选 provider
  for (const g of mod.groups) {
    _roles.filter(r => r.group === g).forEach(role => {
      (role.slots || []).forEach(slot => {
        const sel = listEl.querySelector(`select.aic-slot-provider[data-role="${role.key}"][data-slot="${slot.slot_index}"]`);
        if (sel) sel.value = slot.provider;
      });
    });
  }
}

async function saveMatrixConfig() {
  const listEl = document.getElementById('aic-matrix-list');
  if (!listEl) return;

  const configs = [];
  listEl.querySelectorAll('.aic-slot').forEach(slotEl => {
    const roleKey = slotEl.dataset.role;
    const slotIdx = parseInt(slotEl.dataset.slot, 10);
    const provSel = slotEl.querySelector('.aic-slot-provider');
    const modelInp = slotEl.querySelector('.aic-provider-input');
    const provider = provSel?.value?.trim();
    const model = modelInp?.value?.trim();

    if (provider && model) {
      configs.push({ role_key: roleKey, slot_index: slotIdx, provider, model });
    }
  });

  if (configs.length === 0) {
    setStatus('aic-matrix-status', '没有需要保存的配置', 'warn');
    return;
  }

  try {
    const resp = await fetch(`${API}/roles`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ configs }),
    });
    if (resp.ok) {
      const data = await resp.json();
      setStatus('aic-matrix-status', `已保存 ${data.saved} 条配置`, 'ok');
      await loadRoles();
    } else {
      setStatus('aic-matrix-status', '保存失败', 'fail');
    }
  } catch (e) {
    setStatus('aic-matrix-status', '网络错误', 'fail');
  }
}

/* ── Utilities ────────────────────────────────────────── */

/* ── Section: 调度看板（智能调度 Phase 2）──────────────── */
const DC_SCHED_API = '/api/decision';
const esc = escHtml;  // 本模块统一转义函数别名（renderSchedUsage 等用）
let _schedBound = false;

async function loadScheduler() {
  if (!document.getElementById('sched-usage-root')) return;
  if (!_schedBound) {
    _schedBound = true;
    document.getElementById('sched-refresh')?.addEventListener('click', () => { loadSchedUsage(); loadSchedAssignments(); });
    document.getElementById('sched-days')?.addEventListener('change', loadSchedUsage);
  }
  loadSchedUsage();
  loadSchedAssignments();
}

// 模型使用策略 + 一键自动配置（现位于「API KEY 管理」页，随配置中心初始化加载）
let _policyBound = false;
async function loadSchedPolicy() {
  const preferSel = document.getElementById('sched-prefer-tier');
  const optSel = document.getElementById('sched-optimize-for');
  if (!preferSel) return;
  if (!_policyBound) {
    _policyBound = true;
    document.getElementById('sched-policy-save')?.addEventListener('click', saveSchedPolicy);
    document.getElementById('aic-autotest-btn')?.addEventListener('click', autoTestAndConfigure);
  }
  try {
    const res = await fetch(`${DC_SCHED_API}/routing-policy`);
    if (res.ok) {
      const g = (await res.json()).global || {};
      preferSel.value = g.prefer_tier || 'auto';
      optSel.value = g.optimize_for || 'balanced';
    }
  } catch { /* 忽略 */ }
}

// 一键：跑综合测试 → 结果落 model_capability_test，调度器据此自动为各角色选型（无需手动分配/推荐）
async function autoTestAndConfigure() {
  const btn = document.getElementById('aic-autotest-btn');
  const st = document.getElementById('aic-autotest-status');
  if (btn) { btn.disabled = true; btn.textContent = '测试中…'; }
  if (st) { st.className = 'aic-autotest-status'; st.textContent = '正在测试各模型能力，请稍候（视模型数量约 1-2 分钟）…'; }
  try {
    await startComprehensiveTest();  // 复用综合测试；内部已捕获异常，不抛出
    if (st) {
      if (_testDoneCount > 0) {
        st.className = 'aic-autotest-status ok';
        st.innerHTML = `✅ 测试完成（${_testDoneCount} 个模型），系统已按能力自动为所有角色智能选型，可直接开始使用。`;
      } else {
        st.className = 'aic-autotest-status fail';
        st.textContent = '未测到可用模型，请先在上方「API KEY 管理」配好至少一个可用的 API Key。';
      }
    }
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🚀 模型自动测试并配置'; }
  }
}

async function loadSchedAssignments() {
  const root = document.getElementById('sched-assign-root');
  if (!root) return;
  try {
    const res = await fetch(`${DC_SCHED_API}/model-assignments`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    root.innerHTML = renderSchedAssignments((await res.json()).assignments || []);
  } catch (e) {
    root.innerHTML = `<div class="aic-provider-empty">加载失败：${esc(e.message)}</div>`;
  }
}

const _GROUP_LABEL = {
  decision: '决策层级', committee: '投资委员会', pipeline: '产业链管线',
  watchlist: '看板模块', bottleneck: '瓶颈交叉评分',
};

function renderSchedAssignments(rows) {
  if (!rows.length) return '<div class="aic-provider-empty">暂无角色</div>';
  const byGroup = {};
  rows.forEach(r => { (byGroup[r.group] = byGroup[r.group] || []).push(r); });
  let html = '';
  for (const [g, list] of Object.entries(byGroup)) {
    html += `<div class="sched-assign-group"><div class="sched-assign-group-title">${esc(_GROUP_LABEL[g] || g)}</div>`;
    html += '<table class="sched-assign-table"><tbody>';
    for (const r of list) {
      const badge = r.source === 'manual'
        ? '<span class="sched-src manual">手填</span>'
        : '<span class="sched-src auto">自动</span>';
      const picks = r.picks.length
        ? r.picks.map(p => `<code>${esc(p.provider)}/${esc(p.model)}</code>`).join(r.multi ? ' + ' : '')
        : '<span class="sched-none">无可用（未配 Key）</span>';
      html += `<tr>
        <td class="sched-assign-role">${esc(r.label)}${r.multi ? ' <small>(多槽)</small>' : ''}</td>
        <td>${badge}</td>
        <td>${picks}</td>
      </tr>`;
    }
    html += '</tbody></table></div>';
  }
  return html;
}

async function saveSchedPolicy() {
  const prefer_tier = document.getElementById('sched-prefer-tier').value;
  const optimize_for = document.getElementById('sched-optimize-for').value;
  try {
    const res = await fetch(`${DC_SCHED_API}/routing-policy`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prefer_tier, optimize_for, role_key: '' }),
    });
    setStatus('sched-policy-status', res.ok ? '策略已保存' : '保存失败', res.ok ? 'ok' : 'fail');
  } catch {
    setStatus('sched-policy-status', '网络错误', 'fail');
  }
}

async function loadSchedUsage() {
  const root = document.getElementById('sched-usage-root');
  if (!root) return;
  const days = document.getElementById('sched-days')?.value || 14;
  setStatus('sched-usage-status', '加载中…', 'info');
  try {
    const res = await fetch(`${DC_SCHED_API}/model-usage?days=${days}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    root.innerHTML = renderSchedUsage(await res.json());
    setStatus('sched-usage-status', '', 'info');
  } catch (e) {
    root.innerHTML = `<div class="aic-provider-empty">加载失败：${esc(e.message)}</div>`;
    setStatus('sched-usage-status', '失败', 'fail');
  }
}

function renderSchedUsage(data) {
  const stats = data.stats || [];
  if (!stats.length) {
    return '<div class="aic-provider-empty">暂无调用数据。系统运行、发生模型调用后，这里会逐步积累各模型的成功率/延迟/切换情况。</div>';
  }
  // 告警条：当前熔断中的 + 成功率偏低(<70%，且样本≥5)的模型
  const cooling = (data.cooling || []).map(c => c.provider);
  const weak = stats.filter(r => r.calls >= 5 && r.ok_rate < 70).map(r => `${r.provider}/${r.model}(${r.ok_rate}%)`);
  let banner = '';
  if (cooling.length || weak.length) {
    const parts = [];
    if (cooling.length) parts.push(`熔断中：${cooling.map(esc).join('、')}`);
    if (weak.length) parts.push(`成功率偏低：${weak.map(esc).join('、')}`);
    banner = `<div class="sched-alert">⚠️ ${parts.join('　|　')}</div>`;
  }
  const series = data.series || [];
  const byProv = {};
  series.forEach(r => { (byProv[r.provider] = byProv[r.provider] || []).push(r); });

  let html = banner + '<table class="sched-usage-table"><thead><tr>' +
    '<th>模型</th><th>档位</th><th>调用</th><th>成功率</th><th>均延迟</th><th>近期成功率曲线</th><th>状态</th>' +
    '</tr></thead><tbody>';
  for (const r of stats) {
    const okColor = r.ok_rate >= 90 ? 'ok' : r.ok_rate >= 70 ? 'warn' : 'fail';
    const tierBadge = r.tier === 'free' ? '<span class="sched-tier free">免费</span>'
      : r.tier === 'paid' ? '<span class="sched-tier paid">付费</span>' : '';
    const cool = r.cooldown_s > 0
      ? `<span class="sched-cooling">熔断中 ${r.cooldown_s}s</span>`
      : '<span class="sched-ok">正常</span>';
    const spark = sparkline((byProv[r.provider] || []).map(x => x.ok_rate));
    html += `<tr>
      <td><strong>${esc(r.provider)}</strong><br><small style="color:var(--muted)">${esc(r.model)}</small></td>
      <td>${tierBadge}</td>
      <td>${r.calls}</td>
      <td class="sched-rate ${okColor}">${r.ok_rate}%</td>
      <td>${r.avg_latency_ms || 0}ms</td>
      <td>${spark}</td>
      <td>${cool}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  return html;
}

// 极简内联 SVG 折线（成功率 0-100 曲线），无外部依赖（前端库被墙，一律本地/内联）
function sparkline(vals) {
  if (!vals || vals.length < 2) return '<span style="color:var(--muted)">—</span>';
  const w = 90, h = 22, n = vals.length;
  const pts = vals.map((v, i) => {
    const x = (i / (n - 1)) * (w - 2) + 1;
    const y = h - 1 - (Math.max(0, Math.min(100, v)) / 100) * (h - 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const last = vals[vals.length - 1];
  const color = last >= 90 ? 'oklch(0.72 0.19 142)' : last >= 70 ? 'oklch(0.75 0.15 85)' : 'oklch(0.63 0.24 25)';
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/></svg>`;
}

function setStatus(id, msg, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'ok' ? 'oklch(0.72 0.19 142)'
    : type === 'fail' ? 'oklch(0.63 0.24 25)'
    : type === 'warn' ? 'oklch(0.75 0.15 85)'
    : 'var(--muted)';

  if (type === 'ok' || type === 'warn') {
    setTimeout(() => { if (el.textContent === msg) el.textContent = ''; }, 3000);
  }
}

/* ── 付费数据源 ─────────────────────────────────────── */

const DS_API = '/api/data-sources';
let _paidSources = [];

async function loadPaidSources() {
  const grid = document.getElementById('paid-source-grid');
  if (!grid) return;
  try {
    const resp = await fetch(`${DS_API}/catalog`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _paidSources = data.sources || [];
    renderPaidSources();
  } catch (e) {
    grid.innerHTML = `<p class="aic-hint">加载失败：${escHtml(e.message)}</p>`;
  }
}

// 各外部源在 DataHub 里实际服务的能力（与 providers.py 能力矩阵对齐），让用户知道"配这个源补哪块"
const DS_SERVES = {
  fmp: '财报/一致预期 · 深财务 · 新闻', finnhub: '财报 · 新闻', tushare: 'A股财报',
  alphavantage: '深财务 · 财报 · 新闻', tiingo: '新闻 · 深财务',
  polygon: '期权（需 Polygon 付费 Options 订阅，否则回退 yfinance）', custom: '自定义',
  fred: '宏观经济数据（利率 · 通胀 · 失业率 · 美债）',
};

function renderPaidSources() {
  const grid = document.getElementById('paid-source-grid');
  if (!grid) return;
  grid.innerHTML = _paidSources.map(s => {
    const dot = s.configured ? (s.verified ? 'aic-status-ok' : 'aic-status-unknown') : 'aic-status-unknown';
    const site = s.site ? `<a href="${escHtml(s.site)}" target="_blank" rel="noopener" class="aic-hint" style="margin-left:6px">官网↗</a>` : '';
    const vtag = s.verified
      ? '<span style="color:oklch(0.62 0.17 145)">已验证</span>'
      : '<span style="color:var(--muted)">未验证</span>';
    const hint = s.key_hint ? `已存凭证：${escHtml(s.key_hint)}（${vtag}）` : (s.testable ? '未配置' : '仅存凭证');
    const serves = DS_SERVES[s.id] ? `<span class="aic-hint" style="display:block;margin-top:2px">服务能力：${DS_SERVES[s.id]}</span>` : '';
    const customUrl = s.id === 'custom'
      ? `<input class="aic-provider-input" data-ds-url="${escHtml(s.id)}" placeholder="探测 URL（用 {KEY} 占位）" value="${escHtml(s.base_url_saved || '')}" style="margin-bottom:6px">`
      : '';
    const testBtn = s.testable
      ? `<button class="btn btn-xs" data-ds-act="test" data-ds-id="${escHtml(s.id)}">测试</button>`
      : `<button class="btn btn-xs" disabled title="无公开自助 API">不可测</button>`;
    const delBtn = s.configured
      ? `<button class="btn btn-xs btn-danger" data-ds-act="del" data-ds-id="${escHtml(s.id)}">删除</button>` : '';
    return `<div class="aic-provider-item" data-ds-card="${escHtml(s.id)}">
      <div class="aic-provider-row-top">
        <span class="aic-provider-status ${dot}"></span>
        <div class="aic-provider-info">
          <span class="aic-provider-name">${escHtml(s.name)}${site}</span>
          <span class="aic-provider-model">${escHtml(s.note || '')}</span>
          ${serves}
        </div>
      </div>
      <div class="aic-provider-row-bottom" style="flex-direction:column;align-items:stretch;gap:6px">
        ${customUrl}
        <input class="aic-provider-input" type="password" data-ds-key="${escHtml(s.id)}" placeholder="API Key" autocomplete="off">
        <div style="display:flex;gap:6px;align-items:center">
          <button class="btn btn-xs btn-primary" data-ds-act="save" data-ds-id="${escHtml(s.id)}">保存</button>
          ${testBtn}${delBtn}
          <span class="aic-provider-model" data-ds-status="${escHtml(s.id)}">${hint}</span>
        </div>
      </div>
    </div>`;
  }).join('');
}

function _dsStatus(id, msg, type) {
  const el = document.querySelector(`[data-ds-status="${id}"]`);
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'ok' ? 'oklch(0.72 0.19 142)'
    : type === 'fail' ? 'oklch(0.63 0.24 25)' : 'var(--muted)';
}

function _dsCardVals(id) {
  const grid = document.getElementById('paid-source-grid');
  const key = grid.querySelector(`[data-ds-key="${id}"]`)?.value?.trim() || '';
  const url = grid.querySelector(`[data-ds-url="${id}"]`)?.value?.trim() || '';
  return { key, url };
}

async function _dsSave(id, btn) {
  const { key, url } = _dsCardVals(id);
  if (!key) { _dsStatus(id, '请先填写 API Key', 'fail'); return; }
  btn.disabled = true;
  try {
    const resp = await fetch(`${DS_API}/${encodeURIComponent(id)}/key`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ api_key: key, base_url: url }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.detail || '保存失败');
    _dsStatus(id, `已保存：${data.key_hint || ''}`, 'ok');
    await loadPaidSources();
  } catch (e) {
    _dsStatus(id, e.message, 'fail');
  } finally { btn.disabled = false; }
}

async function _dsTest(id, btn) {
  const { key, url } = _dsCardVals(id);
  btn.disabled = true; const old = btn.textContent; btn.textContent = '测试中...';
  _dsStatus(id, '测试中...', 'info');
  try {
    const resp = await fetch(`${DS_API}/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_id: id, api_key: key, base_url: url }),
    });
    const data = await resp.json();
    const dot = document.querySelector(`[data-ds-card="${id}"] .aic-provider-status`);
    if (data.ok) {
      _dsStatus(id, '✓ 已验证 · ' + (data.msg || '连通成功'), 'ok');
      if (dot) dot.className = 'aic-provider-status aic-status-ok';
    } else {
      _dsStatus(id, '✕ ' + (data.msg || '连通失败'), 'fail');
      if (dot) dot.className = 'aic-provider-status aic-status-fail';
    }
  } catch (e) {
    _dsStatus(id, '✕ ' + e.message, 'fail');
  } finally { btn.disabled = false; btn.textContent = old; }
}

async function _dsDelete(id, btn) {
  if (!confirm(`删除该数据源已保存的 API Key？`)) return;
  btn.disabled = true;
  try {
    await fetch(`${DS_API}/${encodeURIComponent(id)}/key`, { method: 'DELETE' });
    await loadPaidSources();
  } catch (e) {
    _dsStatus(id, e.message, 'fail');
  } finally { btn.disabled = false; }
}

// 事件委托：保存/测试/删除
document.addEventListener('click', (e) => {
  const btn = e.target.closest('#paid-source-grid button[data-ds-act]');
  if (!btn) return;
  const id = btn.dataset.dsId;
  const act = btn.dataset.dsAct;
  if (act === 'save') _dsSave(id, btn);
  else if (act === 'test') _dsTest(id, btn);
  else if (act === 'del') _dsDelete(id, btn);
});
