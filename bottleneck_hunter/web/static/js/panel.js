/**
 * panel.js — Left-side configuration panel logic.
 * Collects form values, handles sector/CV toggles, triggers screening.
 */

import { startScreening } from './pipeline.js';
import { showView } from './app.js';

const DEFAULT_MODELS = {
  openai: 'gpt-4o',
  anthropic: 'claude-sonnet-4-6',
  deepseek: 'deepseek-chat',
  google: 'gemini-2.5-flash',
  qwen: 'qwen-plus',
  glm: 'glm-4-plus',
  ollama: 'qwen2.5',
  openrouter: 'deepseek/deepseek-chat',
  siliconflow: 'deepseek-ai/DeepSeek-V3',
};

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
  const validationModels = parseValidationModels(document.getElementById('cv-models').value);

  return {
    sector, end_product: endProduct, max_depth: maxDepth, top_n: topN,
    language, market, max_market_cap_yi: maxCap, max_suppliers: maxSuppliers,
    provider, model, enable_cross_validation: enableCV,
    validation_models: enableCV ? validationModels : [],
  };
}

/* ── Parse "provider:model, provider:model" textarea ── */
function parseValidationModels(text) {
  if (!text || !text.trim()) return [];
  return text.split(',').map(s => s.trim()).filter(Boolean).map(pair => {
    const [provider, ...rest] = pair.split(':');
    return { provider: provider.trim(), model: rest.join(':').trim() };
  }).filter(m => m.provider && m.model);
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
  // Sector select: toggle custom input
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

  // Cross-validation toggle
  const cvToggle = document.getElementById('cv-toggle');
  const cvGroup = document.getElementById('cv-models-group');
  cvToggle.addEventListener('change', () => {
    cvGroup.style.display = cvToggle.checked ? '' : 'none';
  });

  // Provider → model default
  const providerSelect = document.getElementById('llm-provider');
  const modelInput = document.getElementById('llm-model');
  providerSelect.addEventListener('change', () => {
    const def = DEFAULT_MODELS[providerSelect.value];
    if (def) modelInput.value = def;
  });

  // Start button
  document.getElementById('btn-start').addEventListener('click', handleStart);

  // Rerun button (in dashboard actions)
  const rerunBtn = document.getElementById('btn-rerun');
  if (rerunBtn) rerunBtn.addEventListener('click', handleStart);

  // Auto-select a configured provider on load
  autoSelectProvider();
}

async function autoSelectProvider() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const data = await res.json();
    syncProviderFromSettings(data.providers || []);
  } catch { /* silent */ }
}

export function syncProviderFromSettings(providerList) {
  const configured = providerList.filter(p => p.configured && !p.is_url);
  if (configured.length === 0) return;

  const providerSelect = document.getElementById('llm-provider');
  const modelInput = document.getElementById('llm-model');

  const currentConfigured = configured.find(p => p.id === providerSelect.value);
  if (currentConfigured) return;

  const firstId = configured[0].id;
  if (providerSelect.querySelector(`option[value="${firstId}"]`)) {
    providerSelect.value = firstId;
    const def = DEFAULT_MODELS[firstId];
    if (def) modelInput.value = def;
  }
}
