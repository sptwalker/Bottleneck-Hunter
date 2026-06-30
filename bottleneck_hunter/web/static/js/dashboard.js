/**
 * dashboard.js — Render data into the 4 dashboard cards + picks card.
 */

import { renderDAG, renderBottleneckBars, renderRadar, renderCompareRadar, renderMiniRadar, renderDetailRadar, renderAiScoreBar } from './charts.js';
import { collectCvModels, DEFAULT_MODELS, ensureProvidersLoaded } from './panel.js';

/* ── Helpers ──────────────────────────────────────────── */
function removeSkeleton(id) {
  const el = document.getElementById(id);
  if (el) { el.classList.remove('skeleton'); el.style.minHeight = ''; }
}

function scoreClass(val) {
  if (val >= 8) return 'score-high';
  if (val >= 6) return 'score-mid';
  return 'score-low';
}

function consensusBadge(score) {
  if (score == null) return '';
  if (score >= 7.5) return `<span class="badge badge-pass">${score.toFixed(1)} 推荐</span>`;
  if (score >= 5) return `<span class="badge badge-concern">${score.toFixed(1)} 存疑</span>`;
  return `<span class="badge badge-fail">${score.toFixed(1)} 不推荐</span>`;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/* ── 交叉验证刷新按钮可见性管理 ───────────────────────── */
function _updateCvRefreshBtn() {
  const btn = document.getElementById('btn-refresh-cv');
  if (!btn) return;
  const scorecards = window.appState.results?.supplier_eval;
  btn.style.display = (scorecards && scorecards.length > 0) ? '' : 'none';
}

/* ── Store bottleneck data for radar on click ─────────── */
let bottleneckReports = [];
let bottleneckViewMode = 'bar'; // 'bar' | 'radar'
let selectedBottleneckIdx = 0;

/* ── 瓶颈排名卡片切换按钮管理 ─────────────────────────── */
function ensureToggleBtn() {
  const card = document.getElementById('card-bottleneck');
  if (!card) return;
  let btn = card.querySelector('.bottleneck-toggle');
  if (!btn) {
    btn = document.createElement('button');
    btn.className = 'btn btn-sm bottleneck-toggle';
    btn.textContent = '雷达图';
    const title = card.querySelector('.dash-card-title');
    if (title) {
      title.style.display = 'flex';
      title.style.alignItems = 'center';
      title.style.justifyContent = 'space-between';
      title.appendChild(btn);
    }
    btn.addEventListener('click', () => {
      if (!bottleneckReports.length) return;
      if (bottleneckViewMode === 'bar') {
        bottleneckViewMode = 'radar';
        btn.textContent = '柱状图';
        renderRadar(bottleneckReports[selectedBottleneckIdx] || bottleneckReports[0]);
      } else {
        bottleneckViewMode = 'bar';
        btn.textContent = '雷达图';
        renderBottleneckBars(bottleneckReports);
      }
    });
  }
  return btn;
}

/* ── renderChain: decompose step_done ────────────────── */
export function renderChain(chainData) {
  removeSkeleton('chart-chain-force');
  renderDAG(chainData);
}

/* ── renderBottlenecks: bottleneck step_done ──────────── */
export function renderBottlenecks(reports) {
  removeSkeleton('chart-bottleneck');
  bottleneckReports = reports;
  bottleneckViewMode = 'bar';
  selectedBottleneckIdx = 0;

  renderBottleneckBars(reports);

  // 初始化切换按钮
  const btn = ensureToggleBtn();
  if (btn) btn.textContent = '雷达图';

  // 注册柱状图点击回调：联动切换到雷达图
  window._onBottleneckBarClick = (idx, report) => {
    selectedBottleneckIdx = idx;
    bottleneckViewMode = 'radar';
    const toggleBtn = document.querySelector('.bottleneck-toggle');
    if (toggleBtn) toggleBtn.textContent = '柱状图';
    renderRadar(report);
  };

  // 注册雷达图点击回退回调：同步按钮状态
  window._onRadarBackClick = () => {
    bottleneckViewMode = 'bar';
    const toggleBtn = document.querySelector('.bottleneck-toggle');
    if (toggleBtn) toggleBtn.textContent = '雷达图';
  };

  // 构建瓶颈得分映射，供 DAG 着色使用
  const bnScoreMap = {};
  if (reports && reports.length > 0) {
    reports.forEach(r => {
      const name = r.node_name || r.name;
      if (name) bnScoreMap[name] = r.overall_score ?? 0;
    });
  }
  window.appState.bottleneckScoreMap = bnScoreMap;

  // 将瓶颈标记回写到产业链图谱数据，刷新图谱高亮
  const chainData = window.appState.results.decompose;
  if (chainData && chainData.nodes && reports && reports.length > 0) {
    const bnNames = new Set(reports.map(r => r.node_name || r.name));
    chainData.nodes.forEach(n => {
      n.is_bottleneck = bnNames.has(n.name);
    });
    renderDAG(chainData);
  }
  _updateRefreshSuppliersBtn();
  _renderBnRetryBar();
}

/* ── 瓶颈失败节点重试横幅 ────────────────────────────── */
function _renderBnRetryBar() {
  const bar = document.getElementById('bn-retry-bar');
  if (!bar) return;
  const failed = window.appState.failedBottleneckNodes || [];
  if (failed.length === 0) { bar.style.display = 'none'; return; }

  const names = failed.map(n => n.name).join('、');
  bar.innerHTML = `
    <span class="retry-label">⚠ ${failed.length} 个节点分析失败</span>
    <span class="retry-nodes">${escapeHtml(names)}</span>
    <select id="bn-retry-provider" class="form-select">
      <option value="">选择备选引擎</option>
    </select>
    <select id="bn-retry-model" class="form-select">
      <option value="">选择模型</option>
    </select>
    <button class="btn btn-sm btn-primary" id="btn-bn-retry" disabled>补充分析</button>`;
  bar.style.display = 'flex';

  _populateRetryProviders();

  const retryBtn = document.getElementById('btn-bn-retry');
  if (retryBtn) retryBtn.addEventListener('click', _retryFailedBottlenecks);
}

async function _populateRetryProviders() {
  const sel = document.getElementById('bn-retry-provider');
  if (!sel) return;
  try {
    const resp = await fetch('/api/settings');
    if (!resp.ok) return;
    const data = await resp.json();
    const providers = (data.providers || []).filter(p => p.configured);
    providers.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id; opt.textContent = p.name;
      sel.appendChild(opt);
    });
  } catch {}
  sel.addEventListener('change', () => {
    _updateRetryModelList(sel.value);
  });
}

const _RETRY_MODEL_MAP = {
  openai: ['gpt-4o', 'gpt-4o-mini', 'gpt-5.5'],
  anthropic: ['claude-sonnet-4-6', 'claude-haiku-4-5-20251001'],
  deepseek: ['deepseek-chat', 'deepseek-reasoner'],
  google: ['gemini-2.5-flash', 'gemini-2.5-pro'],
  qwen: ['qwen-plus', 'qwen-max', 'qwen-turbo'],
  glm: ['glm-4-plus', 'glm-4-flash'],
  minimax: ['MiniMax-Text-01'],
  openrouter: ['deepseek/deepseek-chat', 'google/gemini-2.5-flash'],
  siliconflow: ['deepseek-ai/DeepSeek-V3'],
  agnes: ['agnes-2.0-flash'],
  kimi: ['moonshot-v1-8k'],
};

function _updateRetryModelList(provider) {
  const modelSel = document.getElementById('bn-retry-model');
  const retryBtn = document.getElementById('btn-bn-retry');
  if (!modelSel) return;
  modelSel.innerHTML = '<option value="">选择模型</option>';
  const models = _RETRY_MODEL_MAP[provider] || [];
  models.forEach(m => {
    const opt = document.createElement('option');
    opt.value = m; opt.textContent = m;
    modelSel.appendChild(opt);
  });
  if (models.length > 0) modelSel.value = models[0];
  if (retryBtn) retryBtn.disabled = !provider || models.length === 0;
  modelSel.addEventListener('change', () => {
    if (retryBtn) retryBtn.disabled = !modelSel.value;
  });
}

let _bnRetryAbort = null;

async function _retryFailedBottlenecks() {
  const failed = window.appState.failedBottleneckNodes || [];
  const chain = window.appState.results?.decompose;
  if (!failed.length || !chain) return;

  const provider = document.getElementById('bn-retry-provider')?.value;
  const model = document.getElementById('bn-retry-model')?.value;
  if (!provider || !model) { alert('请选择备选引擎和模型'); return; }

  const retryBtn = document.getElementById('btn-bn-retry');
  const bar = document.getElementById('bn-retry-bar');
  if (retryBtn) { retryBtn.disabled = true; retryBtn.textContent = '分析中...'; }

  if (_bnRetryAbort) _bnRetryAbort.abort();
  _bnRetryAbort = new AbortController();

  try {
    const response = await fetch('/api/retry-bottleneck', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        chain, failed_nodes: failed, provider, model,
        language: document.getElementById('language')?.value || 'zh',
      }),
      signal: _bnRetryAbort.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let newReports = null;
    let stillFailed = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        if (!part.trim()) continue;
        let eventType = '', dataLines = [];
        for (const line of part.split('\n')) {
          if (line.startsWith('event:')) eventType = line.slice(6).trim();
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          const data = JSON.parse(dataLines.join('\n'));
          if (data.message && bar) {
            const label = bar.querySelector('.retry-label');
            if (label) label.textContent = data.message;
          }
          if (eventType === 'error' && data.message) {
            _showToast('补充分析失败: ' + data.message, 4000);
          }
          if (eventType === 'step_done' && data.result) {
            newReports = data.result;
            stillFailed = data.failed_nodes || [];
          }
        } catch {}
      }
    }

    if (newReports && newReports.length > 0) {
      const existing = window.appState.results.bottleneck || [];
      const merged = [...existing, ...newReports];
      merged.sort((a, b) => (b.overall_score ?? 0) - (a.overall_score ?? 0));
      merged.forEach((r, i) => r.rank = i + 1);
      window.appState.results.bottleneck = merged;
      window.appState.failedBottleneckNodes = stillFailed || [];
      renderBottlenecks(merged);
      if (!stillFailed || stillFailed.length === 0) {
        _showToast('补充分析完成，瓶颈数据已补全');
      } else {
        _showToast(`补充完成，仍有 ${stillFailed.length} 个节点失败`, 3000);
      }
    } else if (stillFailed && stillFailed.length > 0) {
      window.appState.failedBottleneckNodes = stillFailed;
      _renderBnRetryBar();
      _showToast('补充分析未成功，请尝试其他引擎', 3000);
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      _showToast('补充分析失败: ' + err.message, 4000);
    }
  } finally {
    if (retryBtn) { retryBtn.disabled = false; retryBtn.textContent = '补充分析'; }
    _bnRetryAbort = null;
  }
}
/* ── 层级筛选 ─────────────────────────────────────────── */
function _resolveLayer(sc) {
  if (sc.layer != null && sc.layer > 0) return sc.layer;
  // 回退：从产业链节点数据查找（覆盖全部节点）
  const bnNode = sc.bottleneck_node || '';
  const names = bnNode.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  if (!names.length) return 0;
  // 优先从 chain nodes 查（最全）
  const chainNodes = window.appState.results?.decompose?.nodes || [];
  for (const cn of chainNodes) {
    if (names.includes(cn.name) && cn.layer != null) return cn.layer;
  }
  // 再从瓶颈报告查
  const reports = window.appState.results?.bottleneck || [];
  for (const r of reports) {
    if (names.includes(r.node_name || r.name) && r.layer != null) return r.layer;
  }
  return 0;
}

function _populateLayerFilter(selectId, scorecards) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  const layers = new Set();
  scorecards.forEach(sc => {
    const l = _resolveLayer(sc);
    if (l > 0) layers.add(l);
  });
  const sorted = [...layers].sort((a, b) => a - b);
  sel.innerHTML = '<option value="all">全部层级</option>';
  sorted.forEach(l => {
    sel.innerHTML += `<option value="${l}">第 ${l} 层</option>`;
  });
  sel.style.display = sorted.length > 0 ? '' : 'none';
  sel.value = 'all';
}

function _filterScoresByLayer(scorecards, layerVal) {
  if (layerVal === 'all') return scorecards;
  const target = parseInt(layerVal, 10);
  return scorecards.filter(sc => _resolveLayer(sc) === target);
}

function _layerBadge(sc) {
  const l = _resolveLayer(sc);
  if (!l) return '';
  return ` <span class="layer-badge">L${l}</span>`;
}

function _smartMoneyIndicator(sc) {
  const sm = sc.smart_money;
  if (!sm) return '';
  const cls = sm.signal_direction === 'bullish' ? 'sm-bull' : sm.signal_direction === 'bearish' ? 'sm-bear' : 'sm-neutral';
  const label = sm.signal_direction === 'bullish' ? '多' : sm.signal_direction === 'bearish' ? '空' : '平';
  return ` <span class="sm-indicator ${cls}" title="聪明钱: ${sm.smart_money_score.toFixed(1)}">${label}</span>`;
}

/* ── renderSuppliers ──────────────────────────────────── */
export function renderSuppliers(scorecards) {
  removeSkeleton('table-suppliers');
  const container = document.getElementById('table-suppliers');
  if (!scorecards || scorecards.length === 0) {
    container.innerHTML = '<p class="empty-msg">未找到符合条件的供应商</p>';
    return;
  }

  window.appState.allScorecards = scorecards;
  window.appState.selectedSuppliers = [];

  _populateLayerFilter('supplier-layer-filter', scorecards);

  const filterSel = document.getElementById('supplier-layer-filter');
  if (filterSel) {
    filterSel.onchange = () => {
      const filtered = _filterScoresByLayer(scorecards, filterSel.value);
      _renderSupplierTable(filtered);
    };
  }

  _renderSupplierTable(scorecards);

  _updateCvRefreshBtn();
  _updateRefreshSuppliersBtn();
}

function _renderSupplierTable(scorecards) {
  const container = document.getElementById('table-suppliers');
  if (!container) return;

  window.appState.selectedSuppliers = [];

  const sorted = [...scorecards].sort((a, b) => (b.overall_score ?? 0) - (a.overall_score ?? 0));
  const top10 = sorted.slice(0, 10);
  window.appState._currentSupplierList = top10;

  const rows = top10.map((sc, i) => {
    const s = sc.supplier || {};
    const scores = sc.dimension_scores || {};
    const position = scores.position ?? sc.market_position ?? '-';
    const customer = scores.customer ?? sc.customer_validation ?? '-';
    const capacity = scores.capacity ?? sc.capacity_status ?? '-';
    const financial = scores.financial ?? sc.financial_health ?? '-';
    const valuation = scores.valuation ?? sc.valuation ?? '-';
    const overall = sc.overall_score ?? '-';
    const alpha = sc.alpha?.alpha_score ?? '-';

    const description = s.description || '';
    const keyProducts = (s.key_products || []).map(p => `<span class="product-tag">${escapeHtml(p)}</span>`).join('');
    const sector = s.sector || '';

    const displayName = s.name || sc.company_name || '';
    const nameCn = s.name_cn || '';
    const nameLabel = nameCn && nameCn !== displayName ? `${escapeHtml(displayName)} (${escapeHtml(nameCn)})` : escapeHtml(displayName);

    return `
      <tr class="supplier-row" data-idx="${i}">
        <td class="col-check"><input type="checkbox" class="supplier-check" data-idx="${i}" /></td>
        <td>${i + 1}</td>
        <td>
          <div class="company-name-cell">
            <span class="company-name">${nameLabel}${_layerBadge(sc)}${_smartMoneyIndicator(sc)}</span>
            <span class="company-ticker-sub">${escapeHtml(s.ticker || sc.ticker || '')}</span>
          </div>
        </td>
        <td>${escapeHtml(sc.bottleneck_node || '')}</td>
        <td class="${scoreClass(position)}">${position}</td>
        <td class="${scoreClass(customer)}">${customer}</td>
        <td class="${scoreClass(capacity)}">${capacity}</td>
        <td class="${scoreClass(financial)}">${financial}</td>
        <td class="${scoreClass(valuation)}">${valuation}</td>
        <td class="overall ${scoreClass(overall)}"><strong>${overall}</strong></td>
        <td class="${alpha !== '-' ? scoreClass(alpha) : ''}">${alpha !== '-' ? alpha.toFixed(1) : '-'}</td>
      </tr>
      <tr class="supplier-detail" id="detail-${i}" style="display:none">
        <td colspan="11">
          <div class="detail-grid">
            ${description ? `<div class="detail-col detail-col-wide"><h4>企业简介</h4><p>${escapeHtml(description)}</p></div>` : ''}
            ${keyProducts ? `<div class="detail-col"><h4>核心产品</h4><div class="product-tags">${keyProducts}</div></div>` : ''}
            ${sector ? `<div class="detail-col"><h4>所属行业</h4><p>${escapeHtml(sector)}</p></div>` : ''}
          </div>
        </td>
      </tr>`;
  }).join('');

  container.innerHTML = `
    <table class="data-table">
      <thead>
        <tr>
          <th class="col-check"></th>
          <th>#</th><th>公司</th><th>瓶颈环节</th>
          <th>地位</th><th>客户</th><th>产能</th><th>财务</th><th>估值</th><th>综合</th><th>Alpha</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div class="compare-action-bar" id="compare-action-bar" style="display:none">
      <span class="compare-count" id="compare-count">已选 0 家</span>
      <button class="btn btn-primary btn-sm" id="btn-compare" disabled>对比分析</button>
      <button class="btn btn-sm" id="btn-clear-selection">清除选择</button>
    </div>`;

  function _syncSelection() {
    const selected = [];
    container.querySelectorAll('.supplier-check:checked').forEach(cb => {
      selected.push(parseInt(cb.dataset.idx, 10));
    });
    window.appState.selectedSuppliers = selected;

    const bar = document.getElementById('compare-action-bar');
    const countEl = document.getElementById('compare-count');
    const btnCompare = document.getElementById('btn-compare');
    if (bar) bar.style.display = selected.length > 0 ? 'flex' : 'none';
    if (countEl) countEl.textContent = `已选 ${selected.length} 家`;
    if (btnCompare) {
      btnCompare.disabled = selected.length < 2;
      btnCompare.title = selected.length < 2 ? '请至少选择 2 家' : '';
    }

    container.querySelectorAll('.supplier-row').forEach(row => {
      const idx = parseInt(row.dataset.idx, 10);
      row.classList.toggle('selected', selected.includes(idx));
    });

    const panel = document.getElementById('card-compare');
    if (panel && panel.style.display !== 'none' && selected.length >= 2) {
      _showComparePanel(top10, selected);
    }
  }

  container.querySelectorAll('.supplier-check').forEach(cb => {
    cb.addEventListener('change', (e) => {
      e.stopPropagation();
      const checked = container.querySelectorAll('.supplier-check:checked');
      if (checked.length > 4) {
        cb.checked = false;
        return;
      }
      _syncSelection();
    });
    cb.addEventListener('click', (e) => e.stopPropagation());
  });

  container.querySelectorAll('.supplier-row').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.classList.contains('supplier-check')) return;
      const idx = row.dataset.idx;
      const detail = document.getElementById(`detail-${idx}`);
      if (detail) {
        detail.style.display = detail.style.display === 'none' ? '' : 'none';
      }
    });
  });

  const btnCompare = document.getElementById('btn-compare');
  if (btnCompare) {
    btnCompare.addEventListener('click', () => {
      const sel = window.appState.selectedSuppliers || [];
      if (sel.length < 2) return;
      _showComparePanel(top10, sel);
    });
  }

  const btnClear = document.getElementById('btn-clear-selection');
  if (btnClear) {
    btnClear.addEventListener('click', () => {
      container.querySelectorAll('.supplier-check').forEach(cb => { cb.checked = false; });
      _syncSelection();
      const panel = document.getElementById('card-compare');
      if (panel) panel.style.display = 'none';
    });
  }
}

/* ── renderValidation: 存储交叉验证数据（无独立卡片）──── */
export function renderValidation(validations) {
  _updateCvRefreshBtn();
  _updateCvSaveBtn();
}

/* ── renderShortlist: 所有入围企业（overall_score >= 5）──── */
export function renderShortlist(scorecards) {
  const card = document.getElementById('card-shortlist');
  const body = document.getElementById('shortlist-body');
  removeSkeleton('shortlist-body');
  if (!card || !body) return;

  window.appState.allShortlistCards = scorecards || [];

  _populateLayerFilter('shortlist-layer-filter', scorecards || []);

  const filterSel = document.getElementById('shortlist-layer-filter');
  if (filterSel) {
    filterSel.onchange = () => {
      const filtered = _filterScoresByLayer(scorecards || [], filterSel.value);
      _renderShortlistGrid(filtered);
    };
  }

  _renderShortlistGrid(scorecards || []);
}

function _renderShortlistGrid(scorecards) {
  const card = document.getElementById('card-shortlist');
  const body = document.getElementById('shortlist-body');
  if (!card || !body) return;

  const qualified = scorecards.filter(sc => (sc.overall_score ?? 0) >= 5);
  if (qualified.length === 0) {
    body.innerHTML = '<p class="empty-msg">无评分 ≥ 5 的入围企业</p>';
    card.style.display = '';
    return;
  }

  const panels = qualified.map((sc, i) => {
    const s = sc.supplier || {};
    const name = s.name || sc.company_name || '';
    const nameCn = s.name_cn || '';
    const panelName = nameCn && nameCn !== name ? `${name} (${nameCn})` : name;
    const ticker = s.ticker || sc.ticker || '';
    const score = sc.overall_score ?? '-';

    return `
      <div class="shortlist-panel" data-sc-idx="${i}">
        <div class="sp-header">
          <span class="sp-name">${escapeHtml(panelName)}${_layerBadge(sc)}</span>
          <span class="sp-score ${scoreClass(score)}">${score}</span>
        </div>
        <div class="sp-ticker">${escapeHtml(ticker)}</div>
        <div class="sp-radar" id="sp-radar-${i}"></div>
      </div>`;
  }).join('');

  body.innerHTML = `<div class="shortlist-grid">${panels}</div>`;
  card.style.display = '';

  requestAnimationFrame(() => {
    qualified.forEach((sc, i) => {
      const radarDom = document.getElementById(`sp-radar-${i}`);
      if (radarDom) renderMiniRadar(radarDom, sc);
    });
  });

  body.querySelectorAll('.shortlist-panel').forEach(panel => {
    panel.addEventListener('click', () => {
      const idx = parseInt(panel.dataset.scIdx, 10);
      const sc = qualified[idx];
      if (sc) _openDetailDrawer(sc);
    });
  });
}

/* ── renderPicks: 最终推荐（交叉验证后 Top 5）─────────── */
function _dimLabel(key) {
  const labels = { position: '地位', customer: '客户', capacity: '产能', financial: '财务', valuation: '估值' };
  return labels[key] || key;
}

function _fmtNum(val, suffix) {
  if (val == null || val === '' || isNaN(val)) return '-';
  return `${Number(val).toFixed(1)}${suffix || ''}`;
}

function _fmtMarketCap(s) {
  const cap = s.market_cap;
  if (cap == null || cap === '') return '-';
  const market = s.market || '';
  if (market === 'a_stock') return _fmtNum(cap, '亿');
  return `$${_fmtNum(cap, 'B')}`;
}

function _fmtPE(s) {
  const pe = s.pe_ratio;
  if (pe == null || pe === '') return '-';
  return _fmtNum(pe, 'x');
}

/* ── Detail Drawer: 企业详情抽屉 ──────────────────────── */
function _openDetailDrawer(sc) {
  const overlay = document.getElementById('drawer-overlay');
  const drawer = document.getElementById('detail-drawer');
  if (!overlay || !drawer) return;

  const s = sc.supplier || {};
  const cv = (window.appState.results?.cross_validate || []).find(v => v.ticker === (s.ticker || sc.ticker));

  // Header
  const drawerName = s.name || sc.company_name || '';
  const drawerNameCn = s.name_cn || '';
  const drawerDisplayName = drawerNameCn && drawerNameCn !== drawerName ? `${drawerName} (${drawerNameCn})` : drawerName;
  document.getElementById('drawer-company-name').textContent = drawerDisplayName;
  document.getElementById('drawer-ticker').textContent = s.ticker || sc.ticker || '';
  document.getElementById('drawer-industry').textContent = s.sector || '';
  document.getElementById('drawer-bottleneck-tag').textContent =
    (sc.bottleneck_node || '') + (sc.layer > 0 ? ` L${sc.layer}` : '');

  // Body content
  const body = document.getElementById('drawer-body');
  let html = '';

  // 1. 企业简介
  if (s.description) {
    html += `<div class="drawer-section"><h4>企业简介</h4><p>${escapeHtml(s.description)}</p></div>`;
  }

  // 2. 核心产品
  if (s.key_products && s.key_products.length > 0) {
    const tags = s.key_products.map(p => `<span class="product-tag">${escapeHtml(p)}</span>`).join('');
    html += `<div class="drawer-section"><h4>核心产品</h4><div class="product-tags">${tags}</div></div>`;
  }

  // 3. 评分雷达图 + AI 推荐指数（横向并排）
  const aiBarHeight = (cv && cv.validations && cv.validations.length > 0)
    ? Math.max(cv.validations.length * 32 + 40, 120) : 0;
  const chartRowHeight = Math.max(280, aiBarHeight);

  html += `<div class="drawer-charts-row">`;
  html += `<div class="drawer-charts-col"><h4>五维评分</h4><div id="drawer-radar" style="width:100%;height:${chartRowHeight}px"></div></div>`;
  if (cv && cv.validations && cv.validations.length > 0) {
    html += `<div class="drawer-charts-col"><h4>AI 推荐指数</h4><div id="drawer-ai-bar" style="width:100%;height:${chartRowHeight}px"></div></div>`;
  }
  html += `</div>`;

  // 5. 优势与风险
  const strengths = sc.strengths || [];
  const weaknesses = sc.weaknesses || [];
  if (strengths.length > 0 || weaknesses.length > 0) {
    html += `<div class="drawer-section drawer-sw">`;
    if (strengths.length > 0) {
      html += `<div class="drawer-sw-col"><h4>优势</h4><ul>${strengths.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul></div>`;
    }
    if (weaknesses.length > 0) {
      html += `<div class="drawer-sw-col drawer-sw-risk"><h4>风险</h4><ul>${weaknesses.map(t => `<li>${escapeHtml(t)}</li>`).join('')}</ul></div>`;
    }
    html += `</div>`;
  }

  // 6. 真实财务数据
  const snap = sc.financial_snapshot;
  if (snap && snap.data_source) {
    const fRows = [];
    if (snap.revenue_yi != null) fRows.push(['营收', `${snap.revenue_yi.toFixed(2)} 亿`]);
    if (snap.revenue_yoy_pct != null) fRows.push(['营收同比', `${snap.revenue_yoy_pct.toFixed(1)}%`]);
    if (snap.net_profit_yi != null) fRows.push(['净利润', `${snap.net_profit_yi.toFixed(2)} 亿`]);
    if (snap.net_profit_yoy_pct != null) fRows.push(['净利同比', `${snap.net_profit_yoy_pct.toFixed(1)}%`]);
    if (snap.gross_margin_pct != null) fRows.push(['毛利率', `${snap.gross_margin_pct.toFixed(1)}%`]);
    if (snap.roe_pct != null) fRows.push(['ROE', `${snap.roe_pct.toFixed(1)}%`]);
    if (snap.debt_ratio_pct != null) fRows.push(['负债率', `${snap.debt_ratio_pct.toFixed(1)}%`]);
    if (snap.cashflow_per_share != null) fRows.push(['每股现金流', `${snap.cashflow_per_share}`]);
    if (snap.analyst_report_count != null) fRows.push(['研报覆盖', `${snap.analyst_report_count} 篇`]);
    if (snap.analyst_rating) fRows.push(['机构评级', snap.analyst_rating]);
    if (snap.consensus_eps != null) fRows.push(['预期EPS', `${snap.consensus_eps}`]);
    if (snap.consensus_pe != null) fRows.push(['预期PE', `${snap.consensus_pe.toFixed(1)}x`]);

    if (fRows.length > 0) {
      const cells = fRows.map(([label, val]) =>
        `<div class="fin-cell"><span class="fin-label">${escapeHtml(label)}</span><span class="fin-val">${escapeHtml(val)}</span></div>`
      ).join('');
      html += `<div class="drawer-section">
        <h4>财务快照 <span class="data-source-tag">${escapeHtml(snap.data_source)}</span></h4>
        <div class="fin-grid">${cells}</div>
        ${snap.report_date ? `<div class="report-date">报告期: ${escapeHtml(snap.report_date)}</div>` : ''}
      </div>`;
    } else {
      html += `<div class="drawer-section"><h4>财务快照</h4><p class="drawer-no-data">已获取数据源（${escapeHtml(snap.data_source)}），但无有效字段</p></div>`;
    }
  } else {
    html += `<div class="drawer-section"><h4>财务快照</h4><p class="drawer-no-data">暂无真实财务数据（Ticker 可能无法被市场API识别）</p></div>`;
  }

  // 6b. 财务趋势
  const trend = snap?.trend;
  if (trend && trend.trend_summary) {
    html += `<div class="drawer-section">
      <h4>财务趋势 <span class="data-source-tag">近${trend.quarters?.length || 0}季</span></h4>
      <div class="trend-summary">${escapeHtml(trend.trend_summary)}</div>
      <div class="fin-grid">`;
    if (trend.revenue_acceleration != null)
      html += `<div class="fin-cell"><span class="fin-label">营收加速度</span><span class="fin-val ${trend.revenue_acceleration > 0 ? 'val-up' : 'val-down'}">${trend.revenue_acceleration > 0 ? '+' : ''}${trend.revenue_acceleration.toFixed(1)}pp</span></div>`;
    if (trend.profit_acceleration != null)
      html += `<div class="fin-cell"><span class="fin-label">利润加速度</span><span class="fin-val ${trend.profit_acceleration > 0 ? 'val-up' : 'val-down'}">${trend.profit_acceleration > 0 ? '+' : ''}${trend.profit_acceleration.toFixed(1)}pp</span></div>`;
    if (trend.gross_margin_trend != null)
      html += `<div class="fin-cell"><span class="fin-label">毛利率趋势</span><span class="fin-val ${trend.gross_margin_trend > 0 ? 'val-up' : 'val-down'}">${trend.gross_margin_trend > 0 ? '+' : ''}${trend.gross_margin_trend.toFixed(1)}pp</span></div>`;
    if (trend.consecutive_growth_quarters > 0)
      html += `<div class="fin-cell"><span class="fin-label">连续正增长</span><span class="fin-val">${trend.consecutive_growth_quarters}季</span></div>`;
    html += `</div></div>`;
  }

  // 6c. 竞争护城河
  const moat = sc.moat;
  if (moat && moat.overall_moat > 0) {
    html += `<div class="drawer-section">
      <h4>竞争护城河 <span class="score-badge ${moat.overall_moat >= 7 ? 'high' : moat.overall_moat >= 4 ? 'mid' : 'low'}">${moat.overall_moat.toFixed(1)}</span></h4>
      <div class="fin-grid">
        <div class="fin-cell"><span class="fin-label">专利壁垒</span><span class="fin-val">${moat.patent_moat}/10</span></div>
        <div class="fin-cell"><span class="fin-label">转换成本</span><span class="fin-val">${moat.switching_cost}/10</span></div>
        <div class="fin-cell"><span class="fin-label">产能优势</span><span class="fin-val">${moat.capacity_lead_time}/10</span></div>
        <div class="fin-cell"><span class="fin-label">成本优势</span><span class="fin-val">${moat.cost_advantage}/10</span></div>
      </div>
      ${moat.moat_reasoning ? `<div class="alpha-reasoning">${escapeHtml(moat.moat_reasoning)}</div>` : ''}
    </div>`;
  }

  // 6d. 聪明钱信号
  const sm = sc.smart_money;
  if (sm) {
    const smColor = sm.signal_direction === 'bullish' ? 'val-up' : sm.signal_direction === 'bearish' ? 'val-down' : '';
    const smLabel = sm.signal_direction === 'bullish' ? '看多' : sm.signal_direction === 'bearish' ? '看空' : '中性';
    html += `<div class="drawer-section">
      <h4>聪明钱信号 <span class="score-badge ${sm.smart_money_score >= 6.5 ? 'high' : sm.smart_money_score <= 3.5 ? 'low' : 'mid'}">${sm.smart_money_score.toFixed(1)}</span></h4>
      <div class="fin-grid">
        <div class="fin-cell"><span class="fin-label">信号方向</span><span class="fin-val ${smColor}"><strong>${smLabel}</strong></span></div>
        <div class="fin-cell"><span class="fin-label">综合评分</span><span class="fin-val">${sm.smart_money_score.toFixed(1)}/10</span></div>
      </div>`;
    if (sm.details && sm.details.length > 0) {
      html += `<ul class="sm-details">${sm.details.map(d => `<li>${escapeHtml(d)}</li>`).join('')}</ul>`;
    }
    html += `</div>`;
  }

  // 6e. 催化剂时间线
  const catalyst = sc.catalyst;
  if (catalyst && catalyst.events && catalyst.events.length > 0) {
    const evtTypeLabel = { policy: '政策', capacity: '产能', technology: '技术', order: '订单', earnings: '财报' };
    const evtTypeClass = { policy: 'cat-policy', capacity: 'cat-capacity', technology: 'cat-tech', order: 'cat-order', earnings: 'cat-earnings' };
    const urgencyColor = catalyst.urgency_score >= 7 ? 'high' : catalyst.urgency_score >= 4 ? 'mid' : 'low';

    const eventsHtml = catalyst.events.map(ev => {
      const typeLabel = evtTypeLabel[ev.event_type] || ev.event_type;
      const typeCls = evtTypeClass[ev.event_type] || 'cat-default';
      const confPct = Math.min(ev.confidence * 10, 100);
      const impactPct = Math.min(ev.impact_score * 10, 100);
      return `<div class="catalyst-event">
        <div class="cat-event-header">
          <span class="cat-type-badge ${typeCls}">${escapeHtml(typeLabel)}</span>
          <span class="cat-event-date">${escapeHtml(ev.expected_date || '')}</span>
        </div>
        <div class="cat-event-desc">${escapeHtml(ev.description)}</div>
        <div class="cat-event-bars">
          <div class="cat-bar-row"><span class="cat-bar-label">置信度</span><div class="cat-bar-track"><div class="cat-bar-fill cat-confidence" style="width:${confPct}%"></div></div><span class="cat-bar-val">${ev.confidence.toFixed(0)}</span></div>
          <div class="cat-bar-row"><span class="cat-bar-label">影响力</span><div class="cat-bar-track"><div class="cat-bar-fill cat-impact" style="width:${impactPct}%"></div></div><span class="cat-bar-val">${ev.impact_score.toFixed(0)}</span></div>
        </div>
      </div>`;
    }).join('');

    html += `<div class="drawer-section">
      <h4>催化剂时间线 <span class="score-badge ${urgencyColor}">${catalyst.urgency_score.toFixed(1)}</span></h4>
      ${catalyst.investment_window ? `<div class="cat-window">投资窗口: <strong>${escapeHtml(catalyst.investment_window)}</strong></div>` : ''}
      ${catalyst.summary ? `<div class="trend-summary">${escapeHtml(catalyst.summary)}</div>` : ''}
      <div class="catalyst-events">${eventsHtml}</div>
    </div>`;
  }

  // 7. 预期差分析
  const alphaObj = sc.alpha;
  if (alphaObj) {
    html += `<div class="drawer-section">
      <h4>预期差分析</h4>
      <table class="drawer-fin-table">
        <tr><td>市场关注度</td><td>${alphaObj.market_attention.toFixed(1)}/10</td></tr>
        <tr><td>信息差</td><td>${alphaObj.information_gap.toFixed(1)}/10</td></tr>`;
    if (alphaObj.trend_bonus != null && alphaObj.trend_bonus !== 0) {
      html += `<tr><td>趋势加分</td><td class="${alphaObj.trend_bonus > 0 ? 'val-up' : 'val-down'}">${alphaObj.trend_bonus > 0 ? '+' : ''}${alphaObj.trend_bonus.toFixed(1)}</td></tr>`;
    }
    if (alphaObj.smart_money_bonus != null && alphaObj.smart_money_bonus !== 0) {
      html += `<tr><td>聪明钱加分</td><td class="${alphaObj.smart_money_bonus > 0 ? 'val-up' : 'val-down'}">${alphaObj.smart_money_bonus > 0 ? '+' : ''}${alphaObj.smart_money_bonus.toFixed(1)}</td></tr>`;
    }
    if (alphaObj.catalyst_bonus != null && alphaObj.catalyst_bonus > 0) {
      html += `<tr><td>催化剂加分</td><td class="val-up">+${alphaObj.catalyst_bonus.toFixed(1)}</td></tr>`;
    }
    html += `<tr><td>Alpha 评分</td><td class="${scoreClass(alphaObj.alpha_score)}"><strong>${alphaObj.alpha_score.toFixed(1)}</strong>/10</td></tr>
      </table>
      ${alphaObj.reasoning ? `<div class="alpha-reasoning">${escapeHtml(alphaObj.reasoning)}</div>` : ''}
    </div>`;
  } else {
    html += `<div class="drawer-section"><h4>预期差分析</h4><p class="drawer-no-data">暂无预期差数据</p></div>`;
  }

  body.innerHTML = html;

  // Show
  overlay.style.display = 'block';
  drawer.style.display = 'flex';
  requestAnimationFrame(() => {
    drawer.classList.add('drawer-open');
  });

  // Render ECharts after DOM visible
  requestAnimationFrame(() => {
    const radarDom = document.getElementById('drawer-radar');
    if (radarDom) renderDetailRadar(radarDom, sc);
    const barDom = document.getElementById('drawer-ai-bar');
    if (barDom && cv) renderAiScoreBar(barDom, cv.validations);
  });
}

function _closeDetailDrawer() {
  const overlay = document.getElementById('drawer-overlay');
  const drawer = document.getElementById('detail-drawer');
  if (drawer) drawer.classList.remove('drawer-open');
  setTimeout(() => {
    if (overlay) overlay.style.display = 'none';
    if (drawer) drawer.style.display = 'none';
  }, 300);
}

export function initDetailDrawer() {
  const overlay = document.getElementById('drawer-overlay');
  const btnClose = document.getElementById('btn-close-drawer');
  if (overlay) overlay.addEventListener('click', _closeDetailDrawer);
  if (btnClose) btnClose.addEventListener('click', _closeDetailDrawer);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') _closeDetailDrawer();
  });
}

export function renderPicks(topPicks, scorecards, validations) {
  const card = document.getElementById('card-picks');
  const body = document.getElementById('picks-body');
  removeSkeleton('picks-body');
  if (!topPicks || topPicks.length === 0) {
    body.innerHTML = '<p class="empty-msg">暂无推荐标的</p>';
    card.style.display = '';
    return;
  }

  const scMap = {};
  (scorecards || []).forEach(sc => {
    const ticker = sc.supplier?.ticker || sc.ticker || '';
    if (ticker) scMap[ticker] = sc;
  });
  const cvMap = {};
  (validations || []).forEach(v => {
    if (v.ticker) cvMap[v.ticker] = v;
  });

  const top5 = topPicks.slice(0, 5);

  const banners = top5.map((ticker, idx) => {
    const sc = scMap[ticker] || {};
    const cv = cvMap[ticker];
    const s = sc.supplier || {};
    const name = s.name || sc.company_name || ticker;
    const score = sc.overall_score ?? '-';
    const node = sc.bottleneck_node || '';
    const badge = cv ? consensusBadge(cv.consensus_score ?? cv.avg_score) : '';
    const avgScore = cv && cv.avg_score != null ? cv.avg_score.toFixed(1) : '-';
    const marketCap = _fmtMarketCap(s);
    const pe = _fmtPE(s);

    const dims = sc.dimension_scores || {};
    const DIM_KEYS = [
      ['position',  'market_position'],
      ['customer',  'customer_validation'],
      ['capacity',  'capacity_status'],
      ['financial', 'financial_health'],
      ['valuation', 'valuation'],
    ];
    const dimBars = DIM_KEYS.map(([key, fallback]) => {
      const val = dims[key] ?? sc[fallback] ?? 0;
      const pct = Math.min(val * 10, 100);
      return `
        <div class="fp-dim-row">
          <span class="fp-dim-label">${_dimLabel(key)}</span>
          <div class="fp-dim-bar-track"><div class="fp-dim-bar-fill ${scoreClass(val)}" style="width:${pct}%"></div></div>
          <span class="fp-dim-val ${scoreClass(val)}">${_fmtNum(val, '')}</span>
        </div>`;
    }).join('');

    const strengths = (sc.strengths || []).slice(0, 2).map(t => `<li>${escapeHtml(t)}</li>`).join('');
    const weaknesses = (sc.weaknesses || []).slice(0, 2).map(t => `<li>${escapeHtml(t)}</li>`).join('');
    const reasoning = cv ? escapeHtml(cv.consensus_reasoning || '') : '';

    // AI 评分柱状图 + 简短分析意见
    let aiScoreBars = '';
    if (cv && cv.validations && cv.validations.length > 0) {
      const bars = cv.validations.map(v => {
        const s = v.score ?? 5;
        const pct = Math.min(s * 10, 100);
        const modelShort = (v.model_name || '').split('/').pop();
        const barColor = s >= 7.5 ? '#4caf50' : (s >= 5 ? '#ffc107' : '#f44336');
        const reasoning = v.reasoning || '';
        const isFallback = reasoning.includes('失败') && s === 5;
        const concerns = (v.concerns || []).filter(c => c && c !== '模型未能完成验证');
        let opinionHtml = '';
        if (reasoning && !isFallback) {
          opinionHtml += `<div class="ai-opinion">${escapeHtml(reasoning)}</div>`;
        }
        if (concerns.length > 0) {
          opinionHtml += `<div class="ai-concerns">${escapeHtml(concerns.join('；'))}</div>`;
        }
        return `
          <div class="ai-score-entry">
            <div class="ai-score-row">
              <span class="ai-model-name">${escapeHtml(modelShort)}</span>
              <div class="ai-score-bar-track"><div class="ai-score-bar-fill" style="width:${pct}%;background:${barColor}"></div></div>
              <span class="ai-score-val">${s.toFixed(0)}</span>
            </div>
            ${opinionHtml}
          </div>`;
      }).join('');
      aiScoreBars = `<div class="fp-ai-scores"><h5>AI 推荐指数</h5>${bars}</div>`;
    }

    return `
      <div class="final-pick-card" data-rank="${idx + 1}">
        <div class="fp-rank">#${idx + 1}</div>
        <div class="fp-header">
          <div class="fp-title-group">
            <span class="fp-name">${escapeHtml(name)}${_layerBadge(sc)}</span>
            <span class="fp-ticker">${escapeHtml(ticker)}</span>
            ${node ? `<span class="fp-node-tag">${escapeHtml(node)}</span>` : ''}
          </div>
          <div class="fp-badge-group">
            ${badge}
          </div>
        </div>
        <div class="fp-body">
          <div class="fp-metrics">
            <div class="fp-metric fp-metric-score">
              <span class="fp-metric-label">综合评分</span>
              <span class="fp-metric-value ${scoreClass(score)}">${score}</span>
            </div>
            <div class="fp-metric">
              <span class="fp-metric-label">市值</span>
              <span class="fp-metric-value">${marketCap}</span>
            </div>
            <div class="fp-metric">
              <span class="fp-metric-label">PE</span>
              <span class="fp-metric-value">${pe}</span>
            </div>
            <div class="fp-metric">
              <span class="fp-metric-label">AI 均分</span>
              <span class="fp-metric-value">${avgScore}</span>
            </div>
          </div>
          <div class="fp-dims">
            ${dimBars}
          </div>
          <div class="fp-sw">
            ${strengths ? `<div class="fp-sw-col fp-strengths"><h5>优势</h5><ul>${strengths}</ul></div>` : ''}
            ${weaknesses ? `<div class="fp-sw-col fp-weaknesses"><h5>风险</h5><ul>${weaknesses}</ul></div>` : ''}
          </div>
          ${aiScoreBars}
        </div>
        ${reasoning ? `<div class="fp-consensus"><span class="fp-consensus-label">AI 共识：</span>${reasoning}</div>` : ''}
      </div>`;
  }).join('');

  body.innerHTML = banners;
  card.style.display = '';
}

/* ── showError: display error in the relevant card ────── */
export function showError(step, message) {
  const stepToCard = {
    decompose: 'card-chain',
    bottleneck: 'card-bottleneck',
    supplier_search: 'card-suppliers',
    supplier_eval: 'card-suppliers',
    cross_validate: 'card-picks',
  };
  const cardId = stepToCard[step];
  if (!cardId) return;

  const card = document.getElementById(cardId);
  if (!card) return;

  const body = card.querySelector('.dash-card-body');
  if (body) {
    const container = body.firstElementChild;
    if (container) container.classList.remove('skeleton');
    body.innerHTML = `<div class="error-banner">错误: ${escapeHtml(message)}</div>`;
  }
}

/* ── 交叉验证刷新 ─────────────────────────────────────── */
let _cvAbortController = null;

function _getCvModelsConfig() {
  const models = collectCvModels();
  if (models.length > 0) return models;
  return [];
}

export async function refreshCrossValidation() {
  const scorecards = window.appState.results?.supplier_eval;
  if (!scorecards || scorecards.length === 0) {
    alert('没有可验证的供应商数据，请先运行分析');
    return;
  }

  const btn = document.getElementById('btn-refresh-cv');
  const body = document.getElementById('picks-body');
  const card = document.getElementById('card-picks');
  if (!body) return;

  if (btn) {
    btn.disabled = true;
    btn.querySelector('span').textContent = '验证中...';
  }
  if (card) card.style.display = '';
  body.innerHTML = '<p class="loading-text">正在准备验证引擎...</p>';

  await ensureProvidersLoaded();

  const models = _getCvModelsConfig();
  if (models.length === 0) {
    body.innerHTML = '<div class="error-banner">没有可用的验证引擎。请在右上角设置中配置至少一个额外的 API Key，或在左侧面板勾选交叉验证并添加模型。</div>';
    if (btn) {
      btn.disabled = false;
      btn.querySelector('span').textContent = '刷新验证';
    }
    return;
  }

  const modelDesc = models.map(m => `${m.provider}/${m.model}`).join(', ');
  body.innerHTML = `<p class="loading-text">正在使用 ${escapeHtml(modelDesc)} 进行交叉验证...</p>`;

  if (_cvAbortController) _cvAbortController.abort();
  _cvAbortController = new AbortController();

  try {
    const language = document.getElementById('language')?.value || 'zh';
    const response = await fetch('/api/cross-validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        scorecards: scorecards,
        validation_models: models,
        language,
      }),
      signal: _cvAbortController.signal,
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let gotResult = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');

      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        if (!part.trim()) continue;
        let eventType = '';
        let dataLines = [];
        for (const line of part.split('\n')) {
          if (line.startsWith('event:')) eventType = line.slice(6).trim();
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          const data = JSON.parse(dataLines.join('\n'));
          if (data.step === 'cross_validate' && data.message && !gotResult) {
            body.innerHTML = `<p class="loading-text">${escapeHtml(data.message)}</p>`;
          }
          if (eventType === 'error' && data.message) {
            body.innerHTML = `<div class="error-banner">交叉验证失败: ${escapeHtml(data.message)}</div>`;
            gotResult = true;
          }
          if (data.result && Array.isArray(data.result)) {
            gotResult = true;
            window.appState.results.cross_validate = data.result;
            _reRenderPicks();
          }
        } catch { /* skip malformed */ }
      }
    }

    if (buffer.trim()) {
      for (const line of buffer.split('\n')) {
        if (!line.startsWith('data:')) continue;
        try {
          const data = JSON.parse(line.slice(5).trim());
          if (data.result && Array.isArray(data.result) && !gotResult) {
            gotResult = true;
            window.appState.results.cross_validate = data.result;
            _reRenderPicks();
          }
        } catch {}
      }
    }

    if (!gotResult) {
      body.innerHTML = '<div class="error-banner">未收到验证结果，请检查网络连接或重试。</div>';
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      body.innerHTML = `<div class="error-banner">交叉验证失败: ${escapeHtml(err.message)}</div>`;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.querySelector('span').textContent = '刷新验证';
    }
    _cvAbortController = null;
  }
}

function _reRenderPicks() {
  const cv = window.appState.results?.cross_validate || [];
  const scorecards = window.appState.results?.supplier_eval || [];
  let topPicks = [];
  for (const v of cv) {
    if ((v.consensus_score ?? 0) >= 5) {
      topPicks.push(v.ticker);
    }
  }
  if (topPicks.length === 0) {
    for (const sc of scorecards.slice(0, 5)) {
      const s = sc.supplier || {};
      if ((sc.overall_score ?? 0) >= 5) topPicks.push(s.ticker || sc.ticker || '');
    }
  }
  renderPicks(topPicks, scorecards, cv);
  _updateCvSaveBtn();
}

export function initCvRefresh() {
  const btn = document.getElementById('btn-refresh-cv');
  if (btn) btn.addEventListener('click', () => refreshCrossValidation());
}

/* ── 保存交叉验证结论 ────────────────────────────────── */
function _updateCvSaveBtn() {
  const btn = document.getElementById('btn-save-cv');
  if (!btn) return;
  const cv = window.appState.results?.cross_validate;
  const hasId = !!(window.appState.analysisId);
  btn.style.display = (cv && cv.length > 0 && hasId) ? '' : 'none';
}

async function saveCvResults() {
  const analysisId = window.appState.analysisId;
  const cv = window.appState.results?.cross_validate;
  if (!analysisId || !cv || cv.length === 0) return;

  const btn = document.getElementById('btn-save-cv');
  if (btn) {
    btn.disabled = true;
    btn.querySelector('span').textContent = '保存中...';
  }

  try {
    const res = await fetch(`/api/history/${analysisId}/cross-validation`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cross_validations: cv }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (btn) btn.querySelector('span').textContent = '已保存';
    setTimeout(() => {
      if (btn) btn.querySelector('span').textContent = '保存结论';
    }, 2000);
  } catch (err) {
    alert(`保存失败: ${err.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

export function initCvSave() {
  const btn = document.getElementById('btn-save-cv');
  if (btn) btn.addEventListener('click', () => saveCvResults());
}

/* ── 重新进行企业分析（原刷新候选） ──────────────────────── */
let _suppAbortController = null;

function _updateRefreshSuppliersBtn() {
  const btn = document.getElementById('btn-refresh-suppliers');
  if (!btn) return;
  const bottlenecks = window.appState.results?.bottleneck;
  btn.style.display = (bottlenecks && bottlenecks.length > 0) ? '' : 'none';
}

function _getProviderName(pid) {
  const map = {
    openai: 'OpenAI', anthropic: 'Anthropic', deepseek: 'DeepSeek',
    google: 'Google', qwen: 'Qwen (通义)', glm: 'GLM (智谱)',
    openrouter: 'OpenRouter', siliconflow: 'SiliconFlow',
    agnes: 'Agnes AI', kimi: 'Kimi (月之暗面)', minimax: 'MiniMax (海螺)',
  };
  return map[pid] || pid;
}

function _getFinancialApiName(market) {
  return market === 'a_stock' ? 'AKShare (A股)' : 'yfinance (美股)';
}

function _showReanalyzeModal() {
  const overlay = document.getElementById('reanalyze-overlay');
  const body = document.getElementById('reanalyze-body');
  const footer = document.getElementById('reanalyze-footer');
  if (!overlay) return;

  const cfg = window.appState.config || {};
  const provider = document.getElementById('llm-provider')?.value || cfg.provider || 'openai';
  const model = document.getElementById('llm-model')?.value || cfg.model || '';
  const market = cfg.market || document.querySelector('input[name="market"]:checked')?.value || 'us_stock';

  const cvModels = collectCvModels();

  let listHtml = '';
  listHtml += `<li class="ra-resource-item" data-key="main">
    <span class="ra-status-icon pending">&#9675;</span>
    <span class="ra-resource-label">
      <span class="ra-resource-type">主分析模型</span>
      <span class="ra-resource-detail">${escapeHtml(_getProviderName(provider))} / ${escapeHtml(model)}</span>
    </span>
  </li>`;

  cvModels.forEach((cv, i) => {
    listHtml += `<li class="ra-resource-item" data-key="cv${i}">
      <span class="ra-status-icon pending">&#9675;</span>
      <span class="ra-resource-label">
        <span class="ra-resource-type">交叉验证#${i + 1}</span>
        <span class="ra-resource-detail">${escapeHtml(_getProviderName(cv.provider))} / ${escapeHtml(cv.model)}</span>
      </span>
    </li>`;
  });

  listHtml += `<li class="ra-resource-item" data-key="financial">
    <span class="ra-status-icon pending">&#9675;</span>
    <span class="ra-resource-label">
      <span class="ra-resource-type">财务数据接口</span>
      <span class="ra-resource-detail">${escapeHtml(_getFinancialApiName(market))}</span>
    </span>
  </li>`;

  body.innerHTML = `<p class="reanalyze-hint">以下为本次分析将使用的模型和接口：</p>
    <ul class="ra-resource-list" id="ra-resource-list">${listHtml}</ul>`;

  const summary = document.getElementById('ra-summary');
  if (summary) { summary.textContent = ''; summary.className = 'ra-summary'; }

  const testBtn = document.getElementById('reanalyze-test');
  const confirmBtn = document.getElementById('reanalyze-confirm');
  if (testBtn) { testBtn.textContent = '开始测试'; testBtn.disabled = false; }
  if (confirmBtn) { confirmBtn.disabled = true; }

  _raValidationParams = { provider, model, cvModels, market };
  overlay.style.display = 'flex';
}

let _raValidationParams = null;
let _raValidationAbort = null;

async function _startValidation() {
  const list = document.getElementById('ra-resource-list');
  const testBtn = document.getElementById('reanalyze-test');
  const confirmBtn = document.getElementById('reanalyze-confirm');
  const summary = document.getElementById('ra-summary');
  if (!list || !_raValidationParams) return;

  if (summary) { summary.textContent = ''; summary.className = 'ra-summary'; }
  if (confirmBtn) confirmBtn.disabled = true;
  if (testBtn) { testBtn.textContent = '停止测试'; testBtn.disabled = false; }

  const items = list.querySelectorAll('.ra-resource-item');
  items.forEach(item => {
    const icon = item.querySelector('.ra-status-icon');
    if (icon) { icon.className = 'ra-status-icon spinning'; icon.innerHTML = '&#9675;'; }
    const err = item.nextElementSibling;
    if (err && err.classList.contains('ra-error-msg')) err.remove();
  });

  _raValidationAbort = new AbortController();
  const { provider, model, cvModels, market } = _raValidationParams;

  try {
    const resp = await fetch('/api/validate-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        main_provider: provider,
        main_model: model,
        cv_models: cvModels,
        market,
      }),
      signal: _raValidationAbort.signal,
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const results = data.results || [];

    let passCount = 0;
    const total = results.length;

    results.forEach((r, idx) => {
      const item = items[idx];
      if (!item) return;
      const icon = item.querySelector('.ra-status-icon');
      if (r.success) {
        passCount++;
        icon.className = 'ra-status-icon ok';
        icon.innerHTML = '&#10003;';
      } else {
        icon.className = 'ra-status-icon fail';
        icon.innerHTML = '&#10007;';
        const errDiv = document.createElement('div');
        errDiv.className = 'ra-error-msg';
        errDiv.textContent = r.error || '连接失败';
        item.after(errDiv);
      }
    });

    if (summary) {
      if (passCount === total) {
        summary.className = 'ra-summary all-pass';
        summary.textContent = 'AI接口测试全部通过，可以开始分析。';
      } else {
        summary.className = 'ra-summary partial';
        summary.textContent = `AI接口测试通过率 ${passCount}/${total}，分析可能存在数据缺失，是否开始分析？`;
      }
    }

    if (testBtn) { testBtn.textContent = '重新测试'; testBtn.disabled = false; }
    if (confirmBtn) confirmBtn.disabled = false;
  } catch (err) {
    if (err.name === 'AbortError') {
      items.forEach(item => {
        const icon = item.querySelector('.ra-status-icon');
        if (icon && icon.classList.contains('spinning')) { icon.className = 'ra-status-icon pending'; icon.innerHTML = '&#9675;'; }
      });
      if (summary) { summary.textContent = '测试已停止'; summary.className = 'ra-summary'; }
      if (testBtn) { testBtn.textContent = '开始测试'; testBtn.disabled = false; }
      return;
    }
    if (summary) { summary.textContent = `验证请求失败: ${err.message}`; summary.className = 'ra-summary partial'; }
    if (testBtn) { testBtn.textContent = '重新测试'; testBtn.disabled = false; }
    if (confirmBtn) confirmBtn.disabled = false;
  } finally {
    _raValidationAbort = null;
  }
}

function _stopValidation() {
  if (_raValidationAbort) _raValidationAbort.abort();
}

function _onTestBtnClick() {
  const testBtn = document.getElementById('reanalyze-test');
  if (!testBtn) return;
  if (_raValidationAbort) {
    _stopValidation();
  } else {
    _startValidation();
  }
}

function _closeReanalyzeModal() {
  _stopValidation();
  const overlay = document.getElementById('reanalyze-overlay');
  if (overlay) overlay.style.display = 'none';
}

export async function refreshSuppliers() {
  const bottlenecks = window.appState.results?.bottleneck;
  if (!bottlenecks || bottlenecks.length === 0) {
    alert('没有瓶颈数据，请先运行分析');
    return;
  }

  await ensureProvidersLoaded();
  _showReanalyzeModal();
}

function _showToast(msg, duration = 2500) {
  let el = document.getElementById('bh-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'bh-toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), duration);
}

function _setSkeleton(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '';
  el.classList.add('skeleton');
  el.style.minHeight = '200px';
}

function _clearSkeleton(id) {
  const el = document.getElementById(id);
  if (el) { el.classList.remove('skeleton'); el.style.minHeight = ''; }
}

async function _doRefreshSuppliers() {
  _closeReanalyzeModal();

  const bottlenecks = window.appState.results?.bottleneck;
  if (!bottlenecks || bottlenecks.length === 0) return;

  const btn = document.getElementById('btn-refresh-suppliers');
  const container = document.getElementById('table-suppliers');
  if (!container) return;

  if (btn) {
    btn.disabled = true;
    btn.querySelector('span').textContent = '分析中...';
  }

  _setSkeleton('table-suppliers');

  const picksCard = document.getElementById('card-picks');
  const shortlistCard = document.getElementById('card-shortlist');
  if (picksCard) picksCard.style.display = '';
  if (shortlistCard) shortlistCard.style.display = '';
  _setSkeleton('picks-body');
  _setSkeleton('shortlist-body');

  const comparePanel = document.getElementById('card-compare');
  if (comparePanel) comparePanel.style.display = 'none';

  const cfg = window.appState.config || {};
  const market = cfg.market || document.querySelector('input[name="market"]:checked')?.value || 'us_stock';
  const maxCap = parseFloat(document.getElementById('max-cap')?.value) || cfg.max_market_cap_yi || 200;
  const maxSuppliers = parseInt(document.getElementById('max-suppliers')?.value, 10) || cfg.max_suppliers || 20;
  const language = document.getElementById('language')?.value || cfg.language || 'zh';
  const provider = document.getElementById('llm-provider')?.value || cfg.provider || 'openai';
  const model = document.getElementById('llm-model')?.value || cfg.model || 'gpt-5.5';

  if (_suppAbortController) _suppAbortController.abort();
  _suppAbortController = new AbortController();

  let newScorecards = null;

  try {
    const response = await fetch('/api/refresh-suppliers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        bottleneck_reports: bottlenecks,
        market,
        max_market_cap_yi: maxCap,
        max_suppliers: maxSuppliers,
        language,
        provider,
        model,
      }),
      signal: _suppAbortController.signal,
    });

    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let gotResult = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, '\n');

      const parts = buffer.split('\n\n');
      buffer = parts.pop();

      for (const part of parts) {
        if (!part.trim()) continue;
        let eventType = '';
        let dataLines = [];
        for (const line of part.split('\n')) {
          if (line.startsWith('event:')) eventType = line.slice(6).trim();
          if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          const data = JSON.parse(dataLines.join('\n'));
          if (data.message && !gotResult) {
            container.innerHTML = `<p class="loading-text">${escapeHtml(data.message)}</p>`;
          }
          if (eventType === 'error' && data.message) {
            container.innerHTML = `<div class="error-banner">分析失败: ${escapeHtml(data.message)}</div>`;
            gotResult = true;
          }
          if (eventType === 'step_done' && data.step === 'supplier_eval' && data.result) {
            window.appState.results.supplier_eval = data.result;
          }
          if (data.scorecards && Array.isArray(data.scorecards)) {
            gotResult = true;
            newScorecards = data.scorecards;
            window.appState.results.supplier_eval = data.scorecards;
            renderSuppliers(data.scorecards);
            renderShortlist(data.scorecards);
          }
        } catch { /* skip malformed */ }
      }
    }

    if (buffer.trim()) {
      for (const line of buffer.split('\n')) {
        if (!line.startsWith('data:')) continue;
        try {
          const data = JSON.parse(line.slice(5).trim());
          if (data.scorecards && Array.isArray(data.scorecards) && !gotResult) {
            gotResult = true;
            newScorecards = data.scorecards;
            window.appState.results.supplier_eval = data.scorecards;
            renderSuppliers(data.scorecards);
            renderShortlist(data.scorecards);
          }
        } catch {}
      }
    }

    if (!gotResult) {
      container.innerHTML = '<div class="error-banner">未收到供应商数据，请检查网络连接或重试。</div>';
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      container.innerHTML = `<div class="error-banner">分析失败: ${escapeHtml(err.message)}</div>`;
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.querySelector('span').textContent = '重新进行企业分析';
    }
    _suppAbortController = null;
  }

  if (newScorecards) {
    const cvModels = collectCvModels();
    if (cvModels.length > 0) {
      await refreshCrossValidation();
    } else {
      _reRenderPicks();
    }

    const analysisId = window.appState.analysisId;
    if (analysisId) {
      try {
        const payload = {
          supplier_scorecards: window.appState.results.supplier_eval || newScorecards,
        };
        const cv = window.appState.results?.cross_validate;
        if (cv && cv.length > 0) payload.cross_validations = cv;

        const res = await fetch(`/api/history/${analysisId}/suppliers`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        _showToast('数据已更新存储成功');
      } catch (err) {
        _showToast('数据存储失败: ' + err.message, 4000);
      }
    }
  }
}

export function initRefreshSuppliers() {
  const btn = document.getElementById('btn-refresh-suppliers');
  if (btn) btn.addEventListener('click', () => refreshSuppliers());

  document.getElementById('reanalyze-close')?.addEventListener('click', _closeReanalyzeModal);
  document.getElementById('reanalyze-test')?.addEventListener('click', _onTestBtnClick);
  document.getElementById('reanalyze-confirm')?.addEventListener('click', _doRefreshSuppliers);
  document.getElementById('reanalyze-overlay')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) _closeReanalyzeModal();
  });
}

/* ── 供应商对比面板 ──────────────────────────────────── */

function _showComparePanel(scorecards, selectedIndices) {
  const panel = document.getElementById('card-compare');
  if (!panel) return;

  const selected = selectedIndices.map(i => scorecards[i]).filter(Boolean);
  if (selected.length < 2) return;

  panel.style.display = '';
  renderCompareRadar(selected);
  _renderCompareTable(selected);

  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function _fmtVal(v, suffix) {
  if (v == null || v === '' || isNaN(v)) return '-';
  return typeof v === 'number' ? v.toFixed(1) + (suffix || '') : String(v);
}

function _renderCompareTable(scorecards) {
  const container = document.getElementById('compare-table');
  if (!container) return;

  const PALETTE = ['#5470c6', '#91cc75', '#fac858', '#ee6666'];

  const cols = scorecards.map((sc, i) => {
    const s = sc.supplier || {};
    return {
      name: s.name || sc.company_name || `公司${i + 1}`,
      ticker: s.ticker || sc.ticker || '',
      color: PALETTE[i % PALETTE.length],
      sc,
      s,
    };
  });

  const dimKeys = ['position', 'customer', 'capacity', 'financial', 'valuation'];
  const dimLabels = { position: '市场地位', customer: '客户验证', capacity: '产能状况', financial: '财务健康', valuation: '估值水平' };

  const rows = [];

  // 基本信息
  rows.push({ label: '公司名称', values: cols.map(c => escapeHtml(c.name)), type: 'text' });
  rows.push({ label: '代码', values: cols.map(c => escapeHtml(c.ticker)), type: 'text' });
  rows.push({ label: '瓶颈环节', values: cols.map(c => escapeHtml(c.sc.bottleneck_node || '')), type: 'text' });
  rows.push({ label: '市值(亿)', values: cols.map(c => _fmtVal(c.s.market_cap)), type: 'num' });
  rows.push({ label: 'PE', values: cols.map(c => _fmtVal(c.s.pe_ratio, 'x')), type: 'num' });

  // 5 维评分
  rows.push({ label: '', values: cols.map(() => ''), type: 'separator', section: '评分维度' });
  dimKeys.forEach(k => {
    const scores = cols.map(c => (c.sc.dimension_scores || {})[k] ?? c.sc[k === 'position' ? 'market_position' : k === 'customer' ? 'customer_validation' : k === 'capacity' ? 'capacity_status' : k === 'financial' ? 'financial_health' : 'valuation']);
    rows.push({ label: dimLabels[k], values: scores.map(v => _fmtVal(v)), rawValues: scores, type: 'score' });
  });
  rows.push({ label: '综合评分', values: cols.map(c => _fmtVal(c.sc.overall_score)), rawValues: cols.map(c => c.sc.overall_score), type: 'score', bold: true });
  rows.push({ label: 'Alpha', values: cols.map(c => _fmtVal(c.sc.alpha?.alpha_score)), rawValues: cols.map(c => c.sc.alpha?.alpha_score), type: 'score' });

  // 财务数据
  const hasFinancial = cols.some(c => c.sc.financial_snapshot?.data_source);
  if (hasFinancial) {
    rows.push({ label: '', values: cols.map(() => ''), type: 'separator', section: '财务数据' });
    const fKeys = [
      { key: 'revenue_yi', label: '营收(亿)', suffix: '' },
      { key: 'revenue_yoy_pct', label: '营收增速', suffix: '%' },
      { key: 'net_profit_yi', label: '净利润(亿)', suffix: '' },
      { key: 'gross_margin_pct', label: '毛利率', suffix: '%' },
      { key: 'roe_pct', label: 'ROE', suffix: '%' },
      { key: 'debt_ratio_pct', label: '负债率', suffix: '%' },
    ];
    fKeys.forEach(({ key, label, suffix }) => {
      const vals = cols.map(c => c.sc.financial_snapshot?.[key]);
      rows.push({ label, values: vals.map(v => _fmtVal(v, suffix)), rawValues: vals, type: 'score' });
    });
  }

  // 交叉验证
  const cvData = window.appState.results?.cross_validation;
  if (cvData && cvData.length > 0) {
    rows.push({ label: '', values: cols.map(() => ''), type: 'separator', section: '交叉验证' });
    cols.forEach((c, ci) => {
      const cv = cvData.find(v => v.ticker === c.ticker);
      if (ci === 0) {
        rows.push({ label: '共识结论', values: cols.map(cc => {
          const ccv = cvData.find(v => v.ticker === cc.ticker);
          if (!ccv) return '-';
          const s = ccv.consensus_score ?? ccv.avg_score ?? 0;
          return s >= 7.5 ? '推荐' : s >= 5 ? '存疑' : '不推荐';
        }), type: 'text' });
        rows.push({ label: 'AI 均分', values: cols.map(cc => {
          const ccv = cvData.find(v => v.ticker === cc.ticker);
          return ccv ? (ccv.avg_score ?? ccv.consensus_score ?? 0).toFixed(1) : '-';
        }), rawValues: cols.map(cc => {
          const ccv = cvData.find(v => v.ticker === cc.ticker);
          return ccv ? (ccv.avg_score ?? ccv.consensus_score ?? 0) / 10 : undefined;
        }), type: 'score' });
      }
    });
  }

  // 渲染
  const headerCells = cols.map(c => `<th style="border-bottom:3px solid ${c.color}">${escapeHtml(c.name)}</th>`).join('');

  const bodyRows = rows.map(r => {
    if (r.type === 'separator') {
      return `<tr class="compare-section"><td colspan="${cols.length + 1}"><strong>${r.section}</strong></td></tr>`;
    }

    // 最优最差高亮
    let bestIdx = -1, worstIdx = -1;
    if (r.rawValues && r.type === 'score') {
      const nums = r.rawValues.map(v => (v != null && !isNaN(v)) ? v : null);
      const valid = nums.filter(v => v !== null);
      if (valid.length >= 2) {
        const isLowerBetter = r.label === '负债率' || r.label === 'PE';
        const best = isLowerBetter ? Math.min(...valid) : Math.max(...valid);
        const worst = isLowerBetter ? Math.max(...valid) : Math.min(...valid);
        if (best !== worst) {
          bestIdx = nums.indexOf(best);
          worstIdx = nums.indexOf(worst);
        }
      }
    }

    const cells = r.values.map((v, vi) => {
      let cls = '';
      if (vi === bestIdx) cls = 'val-best';
      else if (vi === worstIdx) cls = 'val-worst';
      const boldWrap = r.bold ? `<strong>${v}</strong>` : v;
      return `<td class="${cls}">${boldWrap}</td>`;
    }).join('');

    return `<tr><td class="compare-label">${r.label}</td>${cells}</tr>`;
  }).join('');

  container.innerHTML = `
    <table class="compare-data-table">
      <thead><tr><th class="compare-label">指标</th>${headerCells}</tr></thead>
      <tbody>${bodyRows}</tbody>
    </table>`;
}

export function initComparePanel() {
  const btnClose = document.getElementById('btn-close-compare');
  if (btnClose) {
    btnClose.addEventListener('click', () => {
      const panel = document.getElementById('card-compare');
      if (panel) panel.style.display = 'none';
    });
  }
}
