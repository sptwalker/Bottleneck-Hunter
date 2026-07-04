/**
 * panel.js — Left-side configuration panel logic.
 * Collects form values, handles sector/CV toggles, triggers screening.
 */

import { startScreening } from './pipeline.js';
import { showView } from './app.js';

// provider → 默认模型映射：一律来自 /api/ai-config/providers（单一真源），不写死
let DEFAULT_MODELS = {};

async function _mergeCustomProviders() {
  try {
    const res = await fetch('/api/ai-config/providers');
    if (!res.ok) return;
    const data = await res.json();
    DEFAULT_MODELS = {};
    for (const p of (data.providers || [])) {
      DEFAULT_MODELS[p.id] = p.default_model || '';
    }
  } catch { /* silent */ }
}

const MAX_CV_MODELS = 5;

let _configuredProviders = [];

/* ── Collect all form values into a ScreenRequest JSON object ── */
function collectConfig() {
  const sectorSelect = document.getElementById('sector-select');
  const isCustom = sectorSelect.value === 'custom';
  const sector = isCustom
    ? document.getElementById('custom-sector').value.trim()
    : sectorSelect.value;

  const endProduct = document.getElementById('end-product').value.trim();
  const maxDepth = parseInt(document.querySelector('input[name="max_depth"]:checked').value, 10);
  const topN = parseInt(document.getElementById('top-n').value, 10);
  const language = document.getElementById('language').value;
  const market = document.querySelector('input[name="market"]:checked').value;
  const maxCap = parseFloat(document.getElementById('max-cap').value) || 200;
  const maxSuppliers = parseInt(document.getElementById('max-suppliers').value, 10) || 20;
  const provider = document.getElementById('llm-provider').value;
  const model = document.getElementById('llm-model').value.trim();
  const enableCV = document.getElementById('cv-toggle').checked;
  const validationModels = collectCvModels();

  return {
    sector, end_product: endProduct, max_depth: maxDepth, top_n: topN,
    language, market, max_market_cap_yi: maxCap, max_suppliers: maxSuppliers,
    provider, model, enable_cross_validation: enableCV,
    validation_models: enableCV ? validationModels : [],
  };
}

/* ── 从已勾选的 checkbox 中收集验证模型列表 ── */
function collectCvModels() {
  const checks = document.querySelectorAll('#cv-model-list .cv-check:checked');
  const result = [];
  checks.forEach(cb => {
    const provider = cb.dataset.provider;
    const model = cb.dataset.model;
    if (provider && model) {
      result.push({ provider, model });
    }
  });
  return result;
}

/* ── 渲染 CV checkbox 列表 ── */
function renderCvCheckboxList() {
  const list = document.getElementById('cv-model-list');
  if (_configuredProviders.length === 0) {
    list.innerHTML = '<p class="cv-empty-hint">请先在 API 设置中配置至少一个 LLM</p>';
    return;
  }

  const items = _configuredProviders.map((p, i) => {
    const model = DEFAULT_MODELS[p.id] || '';
    const checked = i < MAX_CV_MODELS ? 'checked' : '';
    return `
      <div class="cv-check-row">
        <label class="cv-check-label">
          <input type="checkbox" class="cv-check" data-provider="${p.id}" data-model="${model}" ${checked}>
          <span class="cv-check-name">${_escapeHtml(p.name)}</span>
          <span class="cv-check-model">${_escapeHtml(model)}</span>
        </label>
      </div>`;
  }).join('');

  list.innerHTML = items + `<p class="cv-check-hint">最多选择 ${MAX_CV_MODELS} 个模型</p>`;

  list.querySelectorAll('.cv-check').forEach(cb => {
    cb.addEventListener('change', _enforceCvLimit);
  });

  _enforceCvLimit();
}

/* ── 限制最多勾选 MAX_CV_MODELS 个 ── */
function _enforceCvLimit() {
  const all = document.querySelectorAll('#cv-model-list .cv-check');
  const checkedCount = document.querySelectorAll('#cv-model-list .cv-check:checked').length;

  all.forEach(cb => {
    if (!cb.checked) {
      cb.disabled = checkedCount >= MAX_CV_MODELS;
    }
  });

  const hint = document.querySelector('#cv-model-list .cv-check-hint');
  if (hint) {
    hint.textContent = `已选 ${checkedCount}/${MAX_CV_MODELS} 个模型`;
    hint.classList.toggle('cv-hint-full', checkedCount >= MAX_CV_MODELS);
  }
}

/* ── 初始化 CV 面板 ── */
function initCvPanel() {
  const cvToggle = document.getElementById('cv-toggle');
  const cvGroup = document.getElementById('cv-models-group');

  cvToggle.addEventListener('change', () => {
    cvGroup.style.display = cvToggle.checked ? '' : 'none';
    if (cvToggle.checked) {
      renderCvCheckboxList();
    }
  });

  if (cvToggle.checked) {
    ensureProvidersLoaded().then(() => {
      renderCvCheckboxList();
    });
  }
}

/* ── Validate required fields ── */
function validate(config) {
  if (!config.sector) return '请输入产业链方向';
  if (!config.end_product) return '请输入终端产品';
  return null;
}

/* ── Disable / enable panel during analysis ── */
export function setPanelDisabled(disabled) {
  const panel = document.getElementById('panel');
  if (disabled) {
    panel.classList.add('disabled');
  } else {
    panel.classList.remove('disabled');
  }
  document.getElementById('btn-start').disabled = disabled;
  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) rerunBtn.disabled = disabled;
}

/* ── Start analysis flow ── */
async function handleStart() {
  await ensureProvidersLoaded();
  const cvToggle = document.getElementById('cv-toggle');
  if (cvToggle && cvToggle.checked) {
    const list = document.getElementById('cv-model-list');
    if (!list || list.querySelectorAll('.cv-check').length === 0) {
      renderCvCheckboxList();
    }
  }

  const config = collectConfig();
  const err = validate(config);
  if (err) {
    alert(err);
    return;
  }

  showView('screen');
  startScreening(config);
}

/* ── Init all panel event listeners ── */
export function initPanel() {
  const sectorSelect = document.getElementById('sector-select');
  const customGroup = document.getElementById('custom-sector-group');
  const endProductInput = document.getElementById('end-product');

  sectorSelect.addEventListener('change', () => {
    const isCustom = sectorSelect.value === 'custom';
    customGroup.style.display = isCustom ? '' : 'none';
    if (isCustom) {
      endProductInput.value = '';
    } else {
      const opt = sectorSelect.selectedOptions[0];
      endProductInput.value = opt?.dataset.product || '';
    }
  });

  initCvPanel();

  const providerSelect = document.getElementById('llm-provider');
  const modelInput = document.getElementById('llm-model');
  providerSelect.addEventListener('change', () => {
    const def = DEFAULT_MODELS[providerSelect.value];
    if (def) modelInput.value = def;
  });

  document.getElementById('btn-start').addEventListener('click', handleStart);

  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) rerunBtn.addEventListener('click', handleStart);

  autoSelectProvider();
}

async function autoSelectProvider() {
  await ensureProvidersLoaded();
}

async function ensureProvidersLoaded() {
  if (_configuredProviders.length > 0) return;
  try {
    const [settingsRes] = await Promise.all([
      fetch('/api/settings'),
      _mergeCustomProviders(),
    ]);
    if (!settingsRes.ok) return;
    const data = await settingsRes.json();
    const providers = data.providers || [];
    syncProviderFromSettings(providers);
  } catch { /* silent */ }
}

export function syncProviderFromSettings(providerList, testedIds) {
  if (testedIds) {
    _configuredProviders = providerList.filter(p => p.configured && !p.is_url && testedIds.has(p.id));
  } else {
    _configuredProviders = providerList.filter(p => p.configured && !p.is_url);
  }

  if (_configuredProviders.length === 0) return;

  const providerSelect = document.getElementById('llm-provider');
  const modelInput = document.getElementById('llm-model');

  if (providerSelect && modelInput) {
    const currentConfigured = _configuredProviders.find(p => p.id === providerSelect.value);
    if (!currentConfigured) {
      const firstId = _configuredProviders[0].id;
      if (providerSelect.querySelector(`option[value="${firstId}"]`)) {
        providerSelect.value = firstId;
        const def = DEFAULT_MODELS[firstId];
        if (def) modelInput.value = def;
      }
    }
  }

  const cvToggle = document.getElementById('cv-toggle');
  if (cvToggle && cvToggle.checked) {
    renderCvCheckboxList();
  }
}

function _escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ── 从历史记录还原侧边栏参数，锁定 market 和 depth ── */
export function restorePanelFromHistory(config) {
  const sectorSelect = document.getElementById('sector-select');
  const customGroup = document.getElementById('custom-sector-group');
  const endProductInput = document.getElementById('end-product');

  // sector
  const matched = sectorSelect.querySelector(`option[value="${config.sector}"]`);
  if (matched) {
    sectorSelect.value = config.sector;
    customGroup.style.display = 'none';
  } else {
    sectorSelect.value = 'custom';
    customGroup.style.display = '';
    document.getElementById('custom-sector').value = config.sector || '';
  }
  sectorSelect.disabled = true;
  const customInput = document.getElementById('custom-sector');
  if (customInput) customInput.disabled = true;

  // end_product
  endProductInput.value = config.end_product || '';
  endProductInput.disabled = true;

  // max_depth — lock
  document.querySelectorAll('input[name="max_depth"]').forEach(r => {
    r.checked = String(r.value) === String(config.max_depth || 4);
    r.disabled = true;
  });

  // market — lock
  document.querySelectorAll('input[name="market"]').forEach(r => {
    r.checked = r.value === (config.market || 'us_stock');
    r.disabled = true;
  });

  // top_n
  const topN = document.getElementById('top-n');
  if (topN && config.top_n) topN.value = String(config.top_n);

  // language
  const lang = document.getElementById('language');
  if (lang && config.language) lang.value = config.language;

  // provider + model
  const providerSelect = document.getElementById('llm-provider');
  const modelInput = document.getElementById('llm-model');
  if (config.provider && providerSelect.querySelector(`option[value="${config.provider}"]`)) {
    providerSelect.value = config.provider;
  }
  if (config.model) modelInput.value = config.model;

  // max_cap, max_suppliers — keep editable with current DOM defaults

  // 隐藏"开始分析"按钮（历史模式不需要）
  const startBtn = document.getElementById('btn-start');
  if (startBtn) startBtn.style.display = 'none';
}

/* ── 解除历史模式锁定，恢复侧边栏为可编辑状态 ── */
export function unlockPanel() {
  document.getElementById('sector-select').disabled = false;
  const customInput = document.getElementById('custom-sector');
  if (customInput) customInput.disabled = false;
  document.getElementById('end-product').disabled = false;
  document.querySelectorAll('input[name="max_depth"]').forEach(r => r.disabled = false);
  document.querySelectorAll('input[name="market"]').forEach(r => r.disabled = false);
  const startBtn = document.getElementById('btn-start');
  if (startBtn) startBtn.style.display = '';
}

export { collectCvModels, DEFAULT_MODELS, ensureProvidersLoaded };
