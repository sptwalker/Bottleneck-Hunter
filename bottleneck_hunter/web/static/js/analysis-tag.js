/**
 * analysis-tag.js — 多段式彩色分析记录标签生成器
 *
 * buildAnalysisTag(data, opts) → HTML 字符串
 */

/* ── 品牌色相映射 (OKLCH hue) ────────────────── */
const PROVIDER_HUE = {
  deepseek: 220,
  openai: 155,
  anthropic: 30,
  qwen: 280,
  google: 200,
  glm: 210,
  kimi: 170,
  minimax: 340,
  ollama: 100,
  openrouter: -1,   // 灰色，无色相
};

/* ── Provider 显示名 ─────────────────────────── */
const PROVIDER_LABEL = {
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  anthropic: 'Claude',
  qwen: 'Qwen',
  google: 'Gemini',
  glm: 'GLM',
  kimi: 'Kimi',
  minimax: 'MiniMax',
  ollama: 'Ollama',
  openrouter: 'OpenRouter',
};

/* ── 市场配置 ────────────────────────────────── */
const MARKET_CFG = {
  a_stock:  { label: 'A股', flag: '🇨🇳', hue: 25 },
  us_stock: { label: '美股', flag: '🇺🇸', hue: 250 },
};

/* ── 色彩工具 ────────────────────────────────── */
function _sectorHue(name) {
  if (!name) return 200;
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) & 0xffff;
  return h % 360;
}

function _oklchBg(hue) {
  if (hue < 0) return 'oklch(0.94 0.005 250)';
  return `oklch(0.92 0.06 ${hue})`;
}
function _oklchText(hue) {
  if (hue < 0) return 'oklch(0.45 0.01 250)';
  return `oklch(0.40 0.12 ${hue})`;
}
function _oklchBgLight(hue) {
  if (hue < 0) return 'oklch(0.96 0.003 250)';
  return `oklch(0.95 0.04 ${hue})`;
}

/* ── 安全转义 ────────────────────────────────── */
function _esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ── 阶段圆点 ────────────────────────────────── */
function _phaseDots(completed) {
  const n = completed || 0;
  return [0, 1, 2, 3, 4].map(i =>
    `<span class="at-dot${i < n ? ' done' : ''}" title="${['瓶颈','筛选','评分','验证','会议'][i]}"></span>`
  ).join('');
}

/* ── 日期格式化（精确到日） ───────────────────── */
function _fmtDate(ts) {
  if (!ts) return '';
  const s = String(ts);
  const m = s.match(/(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[1]}-${m[2]}-${m[3]}`;
  return s.slice(0, 10);
}

/* ── Tooltip 文本 ─────────────────────────────── */
function _buildTooltip(data) {
  const parts = [];
  if (data.seq_no) parts.push(`编号: #${data.seq_no}`);
  const mkt = MARKET_CFG[data.market];
  if (mkt) parts.push(`市场: ${mkt.label}`);
  if (data.sector) parts.push(`产业: ${data.sector}`);
  if (data.end_product) parts.push(`终端: ${data.end_product}`);
  if (data.provider) {
    const modelStr = data.model ? `${data.provider}/${data.model}` : data.provider;
    parts.push(`模型: ${modelStr}`);
  }
  const cp = data.completed_phases || 0;
  parts.push(`进度: ${cp}/5 阶段`);
  const rc = data.run_count || data.analysis_count;
  if (rc) parts.push(`累计分析: ${String(rc).padStart(3, '0')}`);
  if (data.bottleneck_count) parts.push(`瓶颈: ${data.bottleneck_count}`);
  if (data.supplier_count) parts.push(`供应商: ${data.supplier_count}`);
  if (data.created_at) {
    const ts = String(data.created_at).replace('T', ' ').slice(0, 16);
    parts.push(`创建: ${ts}`);
  }
  return parts.join('\n');
}

/* ── 主构建函数 ───────────────────────────────── */
/**
 * 生成分析记录标签 HTML。
 * @param {Object} data   — { seq_no, market, sector, end_product, provider, model, completed_phases, created_at, run_count, ... }
 * @param {Object} opts   — { compact: false, sidebar: false }
 * @returns {string} HTML
 */
export function buildAnalysisTag(data, { compact = false, sidebar = false } = {}) {
  if (!data) return '';

  // ── 侧边栏双行布局 ──
  if (sidebar) {
    return _buildSidebar(data);
  }

  const seq = data.seq_no ? `#${data.seq_no}` : '';
  const mkt = MARKET_CFG[data.market] || MARKET_CFG.us_stock;
  const sectorH = _sectorHue(data.sector);
  const provKey = (data.provider || '').toLowerCase();
  const provH = PROVIDER_HUE[provKey] ?? -1;
  const provLabel = PROVIDER_LABEL[provKey] || data.provider || '';
  const tooltip = _buildTooltip(data);
  const cls = compact ? 'analysis-tag compact' : 'analysis-tag';

  const segments = [];

  // 1. 序号
  if (seq) {
    segments.push(`<span class="at-seg at-seq">${_esc(seq)}</span>`);
  }

  // 2. 市场
  segments.push(
    `<span class="at-seg at-market" style="background:${_oklchBg(mkt.hue)};color:${_oklchText(mkt.hue)}">`
    + `<span>${mkt.flag}</span>${_esc(mkt.label)}</span>`
  );

  // 3. 产业
  if (data.sector) {
    segments.push(
      `<span class="at-seg at-sector" style="background:${_oklchBg(sectorH)};color:${_oklchText(sectorH)}">`
      + `${_esc(data.sector)}</span>`
    );
  }

  // 4. 终端产品
  if (data.end_product) {
    segments.push(
      `<span class="at-seg at-product" style="background:${_oklchBgLight(sectorH)};color:${_oklchText(sectorH)}">`
      + `${_esc(data.end_product)}</span>`
    );
  }

  // 5. 模型
  if (provLabel) {
    segments.push(
      `<span class="at-seg at-model" style="background:${_oklchBg(provH)};color:${_oklchText(provH)}">`
      + `${_esc(provLabel)}</span>`
    );
  }

  // 6. 阶段圆点
  segments.push(`<span class="at-seg at-phases">${_phaseDots(data.completed_phases)}</span>`);

  // 7. 累计分析次数
  const runCount = data.run_count || data.analysis_count || 1;
  segments.push(`<span class="at-seg at-runs" title="累计分析次数">${String(runCount).padStart(3, '0')}</span>`);

  // 8. 日期
  const dateStr = _fmtDate(data.created_at || data.updated_at);
  if (dateStr) {
    segments.push(`<span class="at-seg at-date">${_esc(dateStr)}</span>`);
  }

  return `<span class="${cls}" data-tooltip="${_esc(tooltip)}">${segments.join('')}</span>`;
}

/* ── 侧边栏双行标签（内部） ──────────────────── */
function _buildSidebar(data) {
  const seq = data.seq_no ? `#${data.seq_no}` : '';
  const mkt = MARKET_CFG[data.market] || MARKET_CFG.us_stock;
  const sectorH = _sectorHue(data.sector);
  const runCount = data.run_count || data.analysis_count || 1;
  const tooltip = _buildTooltip(data);
  const runStr = String(runCount).padStart(3, '0');

  return `<span class="at-sidebar" data-tooltip="${_esc(tooltip)}">` +
    `<span class="at-sb-seq">${_esc(seq)}</span>` +
    `<span class="at-sb-body">` +
      `<span class="at-sb-row">` +
        `<span class="at-sb-market" style="color:${_oklchText(mkt.hue)}">${mkt.flag} ${_esc(mkt.label)}</span>` +
        `<span class="at-sb-dots">${_phaseDots(data.completed_phases)}</span>` +
      `</span>` +
      `<span class="at-sb-row">` +
        `<span class="at-sb-sector" style="color:${_oklchText(sectorH)}">${_esc(data.sector || '')}</span>` +
        `<span class="at-sb-runs">${runStr}</span>` +
      `</span>` +
    `</span>` +
  `</span>`;
}
