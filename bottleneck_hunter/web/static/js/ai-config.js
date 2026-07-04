/**
 * ai-config.js — 统一 AI 配置页面
 * 4 个区块: Provider 管理 / 综合测试 / 模型分配矩阵 / 自动推荐
 */

const API = '/api/ai-config';

const PROVIDER_KEY_NAMES = {
  openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  deepseek: 'DEEPSEEK_API_KEY',
  google: 'GOOGLE_API_KEY',
  qwen: 'DASHSCOPE_API_KEY',
  glm: 'GLM_API_KEY',
  minimax: 'MINIMAX_API_KEY',
  openrouter: 'OPENROUTER_API_KEY',
  siliconflow: 'SILICONFLOW_API_KEY',
  agnes: 'AGNES_API_KEY',
  kimi: 'MOONSHOT_API_KEY',
};

const GROUP_LABELS = {
  decision: '决策层级',
  committee: '投资委员会',
  pipeline: '产业链管线',
  watchlist: '看板模块',
  bottleneck: '瓶颈交叉评分',
};

// 系统模块层级（流程顺序）—— 供分配页签(#4)/自动推荐(#5)/侧栏树(#6)统一排序
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
let _recommendations = [];
let _activeModule = '产业链分析';

/* ── Init ─────────────────────────────────────────────── */

export function initAIConfig() {
  const container = document.getElementById('view-aiconfig');
  if (!container) return;

  loadRoles();
  loadTestResults();
  loadRecommendations();
  loadCustomProviders();
  loadAutoUpdateSummary();

  // Provider actions
  container.querySelector('#aic-test-conn')?.addEventListener('click', testConnectivity);

  // Custom endpoint actions
  container.querySelector('#aic-add-custom')?.addEventListener('click', showCustomForm);
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

  // Test actions
  container.querySelector('#aic-start-test')?.addEventListener('click', startComprehensiveTest);

  // Matrix actions
  container.querySelector('#aic-save-matrix')?.addEventListener('click', saveMatrixConfig);

  // Recommend actions
  container.querySelector('#aic-gen-recommend')?.addEventListener('click', () => generateRecommendations(true));
  container.querySelector('#aic-apply-recommend')?.addEventListener('click', applyRecommendations);

  // Recommend modal
  document.getElementById('aic-recommend-modal-close')?.addEventListener('click', hideRecommendModal);
  document.getElementById('aic-recommend-modal-cancel')?.addEventListener('click', hideRecommendModal);
  document.getElementById('aic-recommend-modal-apply')?.addEventListener('click', async () => {
    await applyRecommendations();
    hideRecommendModal();
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

window._aicRetryLoad = () => { loadRoles(); loadCustomProviders(); loadTestResults(); loadRecommendations(); };

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

async function loadRecommendations() {
  try {
    const resp = await fetch(`${API}/recommendations`);
    if (!resp.ok) return;
    const data = await resp.json();
    _recommendations = data.recommendations || [];
    renderRecommendations();
  } catch (e) {
    console.error('Failed to load recommendations:', e);
  }
}

/* ── Section 1: Provider Management ──────────────────── */

function renderProviders() {
  const grid = document.getElementById('aic-provider-grid');
  if (!grid) return;

  try {
    const apiIds = new Set(_providers.map(p => p.id));
    const merged = [
      ..._providers,
      ..._customProviders
        .filter(cp => !apiIds.has(cp.provider_id))
        .map(cp => ({
          id: cp.provider_id, name: cp.display_name || cp.provider_id,
          configured: true, default_model: cp.default_model || '', is_builtin: false,
        })),
    ];
    const customIds = new Set(_customProviders.map(cp => cp.provider_id));
    const customNames = new Map(_customProviders.map(cp => [cp.provider_id, cp.display_name || cp.provider_id]));
    // 已配置在前（字母序），未配置的自动排到最后（字母序）
    merged.sort((a, b) => {
      const ac = a.configured !== false, bc = b.configured !== false;
      if (ac !== bc) return ac ? -1 : 1;
      return String(a.name || a.id).toLowerCase().localeCompare(String(b.name || b.id).toLowerCase());
    });

    grid.innerHTML = merged.map(p => {
      // 内置/自定义判定优先用后端 is_builtin（不依赖 _customProviders 加载时序）；兜底用 customIds
      const isCustom = (p.is_builtin === false) || (p.is_builtin === undefined && customIds.has(p.id));
      const displayName = isCustom ? (customNames.get(p.id) || p.name) : p.name;
      const configured = p.configured !== false;

      return `
      <div class="aic-provider-item${configured ? '' : ' aic-provider-unconfigured'}" data-pid="${escHtml(p.id)}">
        <div class="aic-provider-row-top">
          <span class="aic-provider-status ${configured ? 'aic-status-ok' : 'aic-status-unknown'}"></span>
          <div class="aic-provider-info">
            <span class="aic-provider-name">${escHtml(displayName)}</span>
            ${configured
              ? (p.default_model ? `<span class="aic-provider-model">${escHtml(p.default_model)}</span>` : '')
              : '<span class="aic-provider-unconfig-label">未配置</span>'}
          </div>
          <div class="aic-provider-actions-inline">
            <button class="btn btn-xs" data-aic-act="edit" data-pid="${escHtml(p.id)}" data-custom="${isCustom ? 1 : 0}">${configured ? '编辑' : '配置'}</button>
            ${configured ? `<button class="btn btn-xs btn-danger" data-aic-act="delete" data-pid="${escHtml(p.id)}" data-name="${escHtml(displayName)}" data-custom="${isCustom ? 1 : 0}">删除</button>` : ''}
          </div>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    console.error('renderProviders error:', e);
  }
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

function showCustomForm(editData, builtinMode) {
  const form = document.getElementById('aic-custom-form');
  if (!form) return;

  const nameEl = document.getElementById('aic-custom-name');
  const show = (id, on) => { const el = document.getElementById(id); if (el) el.style.display = on ? '' : 'none'; };

  if (builtinMode) {
    // 内置 provider：名称只读，模型 / base_url / Key 均可编辑；id 字段隐藏
    show('aic-field-name', true); show('aic-field-id', false);
    show('aic-field-url', true); show('aic-field-model', true);
    document.getElementById('aic-custom-edit-id').value = builtinMode.id;
    nameEl.value = builtinMode.name || builtinMode.id;
    nameEl.disabled = false;
    document.getElementById('aic-custom-url').value = builtinMode.base_url || '';
    document.getElementById('aic-custom-url').placeholder = '留空 = 官方默认端点';
    document.getElementById('aic-custom-model').value = builtinMode.default_model || '';
    document.getElementById('aic-custom-key').value = '';
    document.getElementById('aic-custom-key').placeholder = '留空 = 保持现有 Key';
    form.dataset.builtinMode = builtinMode.id;
    form.dataset.builtinEnv = builtinMode.envKey;
  } else if (editData && typeof editData === 'object' && editData.provider_id) {
    show('aic-field-name', true); show('aic-field-id', true);
    show('aic-field-url', true); show('aic-field-model', true);
    document.getElementById('aic-custom-edit-id').value = editData.provider_id;
    nameEl.value = editData.display_name || '';
    nameEl.disabled = false;
    document.getElementById('aic-custom-id').value = editData.provider_id || '';
    document.getElementById('aic-custom-id').disabled = true;
    document.getElementById('aic-custom-url').value = editData.base_url || '';
    document.getElementById('aic-custom-url').placeholder = '如：http://localhost:11434/v1';
    document.getElementById('aic-custom-key').value = '';
    document.getElementById('aic-custom-key').placeholder = editData.api_key_hint ? '已配置（留空保持不变）' : '可选';
    document.getElementById('aic-custom-model').value = editData.default_model || '';
    delete form.dataset.builtinMode;
    delete form.dataset.builtinEnv;
  } else {
    show('aic-field-name', true); show('aic-field-id', true);
    show('aic-field-url', true); show('aic-field-model', true);
    document.getElementById('aic-custom-edit-id').value = '';
    nameEl.value = '';
    nameEl.disabled = false;
    document.getElementById('aic-custom-id').value = '';
    document.getElementById('aic-custom-id').disabled = false;
    document.getElementById('aic-custom-url').value = '';
    document.getElementById('aic-custom-url').placeholder = '如：http://localhost:11434/v1';
    document.getElementById('aic-custom-key').value = '';
    document.getElementById('aic-custom-key').placeholder = '可选（如 Ollama 无需填写）';
    document.getElementById('aic-custom-model').value = '';
    delete form.dataset.builtinMode;
    delete form.dataset.builtinEnv;
  }

  form.style.display = 'block';
  document.getElementById('aic-custom-model').focus();
}

function hideCustomForm() {
  const form = document.getElementById('aic-custom-form');
  if (form) {
    form.style.display = 'none';
    delete form.dataset.builtinMode;
    delete form.dataset.builtinEnv;
  }
  document.getElementById('aic-custom-id').disabled = false;
  const nameEl = document.getElementById('aic-custom-name');
  if (nameEl) nameEl.disabled = false;
}

async function saveCustomProvider() {
  const form = document.getElementById('aic-custom-form');
  const builtinId = form?.dataset.builtinMode;

  if (builtinId) {
    const api_key = document.getElementById('aic-custom-key')?.value.trim();
    const default_model = document.getElementById('aic-custom-model')?.value.trim() || '';
    const base_url = document.getElementById('aic-custom-url')?.value.trim() || '';
    const display_name = document.getElementById('aic-custom-name')?.value.trim() || '';
    const envKey = form.dataset.builtinEnv;

    try {
      // 1) 保存默认模型 + base_url + 显示名 覆盖（单一真源）
      const cfgResp = await fetch(`${API}/providers/${builtinId}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_model, base_url, display_name }),
      });
      if (!cfgResp.ok) { alert('保存模型配置失败'); return; }
      // 2) 仅当填了新 Key 才更新（留空=保持现有）
      if (api_key && envKey) {
        await fetch(`${API}/providers/keys`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ settings: { [envKey]: api_key } }),
        });
      }
      hideCustomForm();
      await loadRoles();
      setStatus('aic-provider-status', '已更新', 'ok');
    } catch (e) {
      alert('网络错误: ' + e.message);
    }
    return;
  }

  const editId = document.getElementById('aic-custom-edit-id')?.value;
  const display_name = document.getElementById('aic-custom-name')?.value.trim();
  const provider_id = document.getElementById('aic-custom-id')?.value.trim();
  const base_url = document.getElementById('aic-custom-url')?.value.trim();
  const api_key = document.getElementById('aic-custom-key')?.value.trim();
  const default_model = document.getElementById('aic-custom-model')?.value.trim();

  if (!display_name) { alert('请输入显示名称'); return; }
  if (!provider_id) { alert('请输入标识符 ID'); return; }
  if (!base_url) { alert('请输入 API 地址'); return; }
  if (!default_model) { alert('请输入默认模型'); return; }

  const body = { provider_id, display_name, base_url, default_model, api_key: api_key || '' };

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
      setStatus('aic-provider-status', editId ? '端点已更新' : '端点已添加', 'ok');
    } else {
      const err = await resp.json().catch(() => ({}));
      alert(err.detail || '保存失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function testFormConfig() {
  const form = document.getElementById('aic-custom-form');
  const builtinId = form?.dataset.builtinMode;
  const btn = document.getElementById('aic-custom-test');
  const provider = builtinId || document.getElementById('aic-custom-id')?.value.trim();
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

function editProvider(id, isCustom) {
  if (isCustom) {
    editCustomProvider(id);
  } else {
    const envKey = PROVIDER_KEY_NAMES[id];
    if (!envKey) { alert('未知 Provider'); return; }
    const p = _providers.find(x => x.id === id) || {};
    showCustomForm(null, { id, envKey, name: p.name || id, default_model: p.default_model || '', base_url: p.base_url || '' });
  }
}

async function deleteProvider(id, name, isCustom) {
  // 防御：只要不是已知内置 provider（无 env key 映射），一律按自定义端点删除，避免 deleteBuiltinProvider 静默 no-op
  if (isCustom || !PROVIDER_KEY_NAMES[id]) {
    await deleteCustomProvider(id, name);
  } else {
    await deleteBuiltinProvider(id, name);
  }
}

async function deleteCustomProvider(id, name) {
  if (!confirm(`确定删除自定义端点「${name}」？\n删除后使用该端点的模型配置将失效。`)) return;

  try {
    const resp = await fetch(`${CUSTOM_API}/${id}`, { method: 'DELETE' });
    if (resp.ok) {
      await loadCustomProviders();
      await loadRoles();
      setStatus('aic-provider-status', '端点已删除', 'ok');
    } else {
      alert('删除失败');
    }
  } catch (e) {
    alert('网络错误: ' + e.message);
  }
}

async function deleteBuiltinProvider(id, name) {
  if (!confirm(`确定删除「${name}」？\n将清除其 API Key 与模型/端点/名称配置，回到「未配置」状态（内置 Provider 可随时重新配置）。`)) return;

  const envKey = PROVIDER_KEY_NAMES[id];
  if (!envKey) return;

  try {
    // 清 Key + 清 provider_configs 覆盖 → 完全回到未配置，与自定义端点删除后一致（不再出现在已配置列表）
    await fetch(`${API}/providers/keys`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings: { [envKey]: '' } }),
    });
    await fetch(`${API}/providers/${id}/config`, { method: 'DELETE' });
    await loadRoles();
    setStatus('aic-provider-status', `${name} 已删除`, 'ok');
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

async function startComprehensiveTest() {
  const btn = document.getElementById('aic-start-test');
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }

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
    const resp = await fetch(`${API}/test/comprehensive`, { method: 'POST' });
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
    setTimeout(() => { if (progressEl) progressEl.classList.remove('active'); }, 2000);
    await loadTestResults();
    if (_testDoneCount > 0) {
      await generateRecommendations(true);
    }
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
    tbody.innerHTML = '<tr><td colspan="8" class="aic-test-empty">暂无测试结果，点击"开始综合测试"</td></tr>';
    return;
  }

  const sorted = [..._testResults].sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0));
  tbody.innerHTML = sorted.map(r => {
    const dims = ['connectivity', 'json_output', 'chinese_analysis', 'speed', 'scoring_variance', 'instruction_follow'];
    const cells = dims.map(d => {
      const s = r.scores?.[d] ?? '-';
      if (s === '-') return '<td class="aic-score-cell">-</td>';
      const cls = scoreClass(s);
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

/* ── Section 4: Auto-recommendation ──────────────────── */

async function generateRecommendations(showModal = false) {
  const btn = document.getElementById('aic-gen-recommend');
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }

  try {
    const resp = await fetch(`${API}/recommend`, { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      setStatus('aic-recommend-status', err.detail || '生成失败', 'fail');
      return;
    }
    const data = await resp.json();
    _recommendations = data.recommendations || [];
    renderRecommendations();
    setStatus('aic-recommend-status', `已生成 ${_recommendations.length} 条推荐`, 'ok');
    if (showModal && _recommendations.length > 0) {
      showRecommendModal();
    }
  } catch (e) {
    setStatus('aic-recommend-status', '网络错误', 'fail');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '生成推荐'; }
  }
}

async function applyRecommendations() {
  const btn = document.getElementById('aic-apply-recommend');
  if (btn) { btn.disabled = true; btn.textContent = '应用中...'; }

  try {
    const resp = await fetch(`${API}/recommend/apply`, { method: 'POST' });
    if (!resp.ok) {
      setStatus('aic-recommend-status', '应用失败', 'fail');
      return;
    }
    const data = await resp.json();
    setStatus('aic-recommend-status', `已应用 ${data.applied} 条推荐`, 'ok');
    await loadRoles();
  } catch (e) {
    setStatus('aic-recommend-status', '网络错误', 'fail');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '一键应用'; }
  }
}

function renderRecommendations() {
  const grid = document.getElementById('aic-recommend-grid');
  if (!grid) return;

  if (_recommendations.length === 0) {
    grid.innerHTML = '<div class="aic-rec-empty">暂无推荐结果，请先运行综合测试后点击"生成推荐"</div>';
    return;
  }

  const groupOf = {};
  for (const r of _roles) groupOf[r.key] = r.group;

  const cardHtml = (rec) => {
    const role = _roles.find(r => r.key === rec.role_key);
    const currentSlot = (role?.slots || []).find(s => s.slot_index === rec.slot_index);
    const currentText = currentSlot ? `${currentSlot.provider}:${currentSlot.model}` : '未配置';
    const recText = `${rec.provider}:${rec.model}`;
    const isSame = currentText === recText;
    return `
      <div class="aic-rec-card">
        <div class="aic-rec-card-header">
          <span class="aic-rec-role">${escHtml(rec.role_label || rec.role_key)}${rec.slot_index > 0 ? ` #${rec.slot_index + 1}` : ''}</span>
          <span class="aic-rec-score">${(rec.composite_score || 0).toFixed(1)}</span>
        </div>
        <div class="aic-rec-model">${escHtml(recText)}</div>
        <div class="aic-rec-vs">
          ${isSame ? '与当前配置一致' : `当前: ${escHtml(currentText)} <span class="aic-vs-arrow">&rarr;</span> ${escHtml(recText)}`}
        </div>
      </div>`;
  };

  let html = '';
  const known = new Set();
  for (const mod of MODULE_TREE) {
    mod.groups.forEach(g => known.add(g));
    const recs = _recommendations
      .filter(rec => mod.groups.includes(groupOf[rec.role_key]))
      .sort((a, b) => (mod.groups.indexOf(groupOf[a.role_key]) - mod.groups.indexOf(groupOf[b.role_key])));
    if (!recs.length) continue;
    html += `<div class="aic-rec-module"><div class="aic-rec-module-title">${escHtml(mod.module)}</div>`;
    html += `<div class="aic-rec-cards">${recs.map(cardHtml).join('')}</div></div>`;
  }
  const rest = _recommendations.filter(rec => !known.has(groupOf[rec.role_key]));
  if (rest.length) {
    html += `<div class="aic-rec-module"><div class="aic-rec-module-title">其它</div>`;
    html += `<div class="aic-rec-cards">${rest.map(cardHtml).join('')}</div></div>`;
  }
  grid.innerHTML = html;
}

/* ── Utilities ────────────────────────────────────────── */

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

/* ── Recommend Modal ──────────────────────────────────── */

function showRecommendModal() {
  const modal = document.getElementById('aic-recommend-modal');
  const body = document.getElementById('aic-recommend-modal-body');
  if (!modal || !body) return;

  const groups = {};
  for (const rec of _recommendations) {
    const role = _roles.find(r => r.key === rec.role_key);
    const g = role?.group || 'other';
    if (!groups[g]) groups[g] = [];
    const currentSlot = (role?.slots || []).find(s => s.slot_index === rec.slot_index);
    groups[g].push({ ...rec, role, currentSlot });
  }

  const groupOrder = ['decision', 'committee', 'pipeline', 'watchlist', 'bottleneck'];
  let html = '<div class="aic-rm-summary">';
  html += `<p>综合测试完成，共 ${_testResults.length} 个模型参与测试，已为 ${_recommendations.length} 个角色生成推荐配置。</p>`;
  html += '</div>';

  for (const gKey of groupOrder) {
    const items = groups[gKey];
    if (!items || items.length === 0) continue;
    const gLabel = GROUP_LABELS[gKey] || gKey;

    html += `<div class="aic-rm-group">`;
    html += `<div class="aic-rm-group-title">${escHtml(gLabel)}</div>`;
    html += `<table class="aic-rm-table"><tbody>`;

    for (const item of items) {
      const label = item.role?.label || item.role_key;
      const recModel = `${item.provider}:${item.model}`;
      const curModel = item.currentSlot
        ? `${item.currentSlot.provider}:${item.currentSlot.model}`
        : '';
      const isChanged = curModel && curModel !== recModel;
      const isNew = !curModel;
      const cs = item.composite_score ?? 0;

      let statusHtml;
      if (isNew) {
        statusHtml = '<span class="aic-rm-badge aic-rm-new">新配置</span>';
      } else if (isChanged) {
        statusHtml = '<span class="aic-rm-badge aic-rm-changed">变更</span>';
      } else {
        statusHtml = '<span class="aic-rm-badge aic-rm-same">不变</span>';
      }

      html += `<tr>
        <td class="aic-rm-role">${escHtml(label)}${item.slot_index > 0 ? ` #${item.slot_index + 1}` : ''}</td>
        <td class="aic-rm-model">
          <span class="aic-rm-model-name">${escHtml(recModel)}</span>
          ${isChanged ? `<span class="aic-rm-old">← ${escHtml(curModel)}</span>` : ''}
        </td>
        <td class="aic-rm-score ${scoreClass(cs)}">${cs.toFixed(1)}</td>
        <td>${statusHtml}</td>
      </tr>`;
    }

    html += `</tbody></table></div>`;
  }

  body.innerHTML = html;
  modal.style.display = 'flex';
}

function hideRecommendModal() {
  const modal = document.getElementById('aic-recommend-modal');
  if (modal) modal.style.display = 'none';
}
