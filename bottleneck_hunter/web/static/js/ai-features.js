/**
 * ai-features.js — AI 圆桌会议、图表解读、横向对比报告
 */

import { state, logMsg, getMainModel, formatMarkdown } from './wizard-state.js';
import { readSSEStream } from './sse.js';
import { openReport, buildRoundtableReport } from './report-export.js';

/* ── AI 投研圆桌会议 ──────────────────────── */

export const MEETING_ROLES = [
  { id: 'growth', name: '成长型投资者', letter: '成', color: '#10a37f' },
  { id: 'value',  name: '价值型投资者', letter: '价', color: '#d97706' },
  { id: 'risk',   name: '风险分析师',   letter: '风', color: '#dc2626' },
  { id: 'chain',  name: '产业链专家',   letter: '链', color: '#6366f1' },
];

export function buildMeetingSetup() {
  const setup = document.getElementById('meeting-setup');
  const grid = document.getElementById('meeting-role-grid');
  if (!setup || !grid) return;

  const modelCheckboxes = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  const models = Array.from(modelCheckboxes).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model, label: `${provider}/${model}` };
  });

  if (models.length === 0) {
    setup.style.display = 'none';
    return;
  }

  grid.innerHTML = '';
  const optionsHtml = models.map((m, i) => `<option value="${m.provider}::${m.model}">${m.label}</option>`).join('');

  MEETING_ROLES.forEach((role, idx) => {
    const row = document.createElement('div');
    row.className = 'meeting-role-row';
    const defaultIdx = idx % models.length;
    const defaultVal = `${models[defaultIdx].provider}::${models[defaultIdx].model}`;
    row.innerHTML = `
      <div class="meeting-role-avatar" style="background:${role.color}">${role.letter}</div>
      <div class="meeting-role-name">${role.name}</div>
      <select class="meeting-role-select" data-role="${role.id}">
        ${optionsHtml}
      </select>
      <span class="meeting-role-status" id="preflight-${role.id}"></span>
    `;
    const select = row.querySelector('select');
    if (select) select.value = defaultVal;
    grid.appendChild(row);
  });

  setup.style.display = 'block';
  const preflightBtn = document.getElementById('btn-preflight');
  if (preflightBtn) preflightBtn.disabled = false;
}

function getSelectedRoleAssignments() {
  const assignments = {};
  MEETING_ROLES.forEach(role => {
    const select = document.querySelector(`select[data-role="${role.id}"]`);
    if (select && select.value) {
      const [provider, model] = select.value.split('::');
      assignments[role.id] = { provider, model };
    }
  });
  return assignments;
}

export async function runPreflight() {
  const btn = document.getElementById('btn-preflight');
  const statusEl = document.getElementById('preflight-status');
  if (btn) { btn.disabled = true; btn.textContent = '测试中...'; }
  if (statusEl) statusEl.textContent = '';

  const assignments = getSelectedRoleAssignments();
  const seen = new Set();
  const models = [];
  Object.values(assignments).forEach(m => {
    const key = `${m.provider}::${m.model}`;
    if (!seen.has(key)) {
      seen.add(key);
      models.push(m);
    }
  });

  MEETING_ROLES.forEach(role => {
    const el = document.getElementById(`preflight-${role.id}`);
    if (el) { el.textContent = '⏳'; el.className = 'meeting-role-status'; }
  });

  try {
    const resp = await fetch('/api/meeting/preflight', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ models }),
    });
    const data = await resp.json();
    const resultMap = {};
    for (const r of (data.results || [])) {
      resultMap[`${r.provider}::${r.model}`] = r.success;
    }

    let allOk = true;
    MEETING_ROLES.forEach(role => {
      const m = assignments[role.id];
      const key = m ? `${m.provider}::${m.model}` : '';
      const ok = resultMap[key] === true;
      if (!ok) allOk = false;
      const el = document.getElementById(`preflight-${role.id}`);
      if (el) {
        el.textContent = ok ? '✅' : '❌';
        el.className = `meeting-role-status ${ok ? 'preflight-ok' : 'preflight-fail'}`;
      }
    });

    if (allOk) {
      if (statusEl) statusEl.textContent = '全部通过';
      const meetBtn = document.getElementById('btn-start-meeting');
      if (meetBtn) meetBtn.disabled = false;
    } else {
      if (statusEl) statusEl.textContent = '部分模型不可用';
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = `测试失败: ${err.message}`;
  }
  if (btn) { btn.disabled = false; btn.textContent = '测试连通性'; }
}

export function enableMeetingButton() {
  buildMeetingSetup();
  const btn = document.getElementById('btn-start-meeting');
  if (btn) {
    btn.textContent = '启动会议';
  }
}

export async function startMeeting() {
  if (!state.analysisId) return;
  const btn = document.getElementById('btn-start-meeting');
  const statusEl = document.getElementById('meeting-status');
  const transcript = document.getElementById('meeting-transcript');
  const messages = document.getElementById('meeting-messages');
  const resultDiv = document.getElementById('meeting-result');

  if (btn) { btn.disabled = true; btn.textContent = '会议进行中...'; }
  if (statusEl) statusEl.textContent = '进行中';
  if (transcript) transcript.style.display = 'block';
  if (messages) messages.innerHTML = '';
  if (resultDiv) { resultDiv.style.display = 'none'; resultDiv.innerHTML = ''; }

  document.getElementById('ai-meeting-card')?.classList.add('meeting-active');

  const modelCheckboxes = document.querySelectorAll('#wiz-cv-models input[type="checkbox"]:checked');
  const validationModels = Array.from(modelCheckboxes).map(cb => {
    const [provider, model] = cb.value.split('::');
    return { provider, model };
  });

  const roleAssignments = getSelectedRoleAssignments();

  const body = {
    analysis_id: state.analysisId,
    validation_models: validationModels,
    role_assignments: Object.keys(roleAssignments).length > 0 ? roleAssignments : null,
    language: 'zh',
  };

  logMsg('圆桌会议启动', 'info');

  try {
    await readSSEStream('/api/phase4/meeting', body, {
      label: 'meeting-sse',
      logFn: logMsg,
      getAnalysisId: () => state.analysisId,
      onEvent: (data) => handleMeetingEvent(data),
      onError: (err) => {
        logMsg(`圆桌会议连接失败: ${err.message}`, 'error');
        if (statusEl) statusEl.textContent = '失败';
      },
    });
  } catch (err) {
    logMsg(`圆桌会议连接失败: ${err.message}`, 'error');
    if (statusEl) statusEl.textContent = '失败';
  }

  if (btn) { btn.textContent = '重新开会'; btn.disabled = false; }
  document.getElementById('ai-meeting-card')?.classList.remove('meeting-active');
}

export function handleMeetingEvent(data) {
  if (data.meeting_error || data.message && !data.role && !data.participants) {
    if (data.meeting_error) {
      const msg = data.message || data.meeting_error || '未知错误';
      logMsg(`[会议错误] ${msg}`, 'error');
      const statusEl = document.getElementById('meeting-status');
      if (statusEl) statusEl.textContent = '失败';
      return;
    }
    if (data.message && !data.role) {
      logMsg(`[会议] ${data.message}`, 'info');
      const statusEl = document.getElementById('meeting-status');
      if (statusEl) statusEl.textContent = data.message;
      return;
    }
  }

  if (data.participants !== undefined && data.company_count !== undefined) {
    logMsg(`会议开始 — ${data.company_count} 家企业, ${data.participants.length} 位参会者`);
    state.meetingParticipants = data.participants;
    return;
  }

  if (data.round_num !== undefined && data.round_name !== undefined && !data.content) {
    renderMeetingRoundDivider(data.round_num, data.round_name);
    logMsg(`第 ${data.round_num} 轮: ${data.round_name}`);
    return;
  }

  if (data.content !== undefined && data.role !== undefined) {
    renderMeetingBubble(data);
    return;
  }

  if (data.ranking) {
    logMsg(`Borda 排名出炉 — 第一: ${data.ranking[0]?.name || '?'}`, 'done');
    return;
  }

  if (data._sseEvent === 'meeting_saved' && data.completed_phases) {
    state.config.completed_phases = data.completed_phases;
    return;
  }

  if (data.result) {
    state.meetingResult = data.result;
    renderMeetingResult(data.result);
    showMeetingExport();
    const statusEl = document.getElementById('meeting-status');
    if (statusEl) statusEl.textContent = '已完成';
    logMsg('圆桌会议完成', 'done');
    return;
  }
}

function showMeetingExport() {
  const b = document.getElementById('btn-export-meeting');
  if (b) b.style.display = '';
}

export function exportMeeting() {
  const m = state.meetingResult;
  if (!m || !(m.final_ranking && m.final_ranking.length)) { alert('暂无圆桌会议结果可导出'); return; }
  const tk = (m.final_ranking || []).map(r => r.ticker).filter(Boolean).join('-');
  const date = new Date().toISOString().slice(0, 10);
  const fname = `圆桌会议纪要${tk ? '_' + tk : ''}_${date}.html`;
  openReport('AI 投研圆桌会议纪要', fname, buildRoundtableReport(m));
}

function renderMeetingRoundDivider(roundNum, roundName) {
  const container = document.getElementById('meeting-messages');
  if (!container) return;
  const div = document.createElement('div');
  div.className = 'meeting-round-divider';
  div.innerHTML = `<span>第 ${roundNum} 轮: ${roundName}</span>`;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function renderMeetingBubble(msg) {
  const container = document.getElementById('meeting-messages');
  if (!container) return;

  const bubble = document.createElement('div');
  bubble.className = 'meeting-bubble';

  const avatarClass = `av-${msg.role}`;
  const avatarLetter = msg.avatar_letter || msg.participant_name?.charAt(0) || '?';
  const color = msg.color || '#64748b';

  bubble.innerHTML = `
    <div class="meeting-avatar ${avatarClass}" style="background:${color}">${avatarLetter}</div>
    <div class="meeting-bubble-body">
      <div class="meeting-name">${msg.participant_name}${msg.model_name ? ` <span class="meeting-model">${msg.model_name}</span>` : ''}</div>
      <div class="meeting-msg md-body">${formatMarkdown(msg.content)}</div>
    </div>
  `;
  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

function renderMeetingResult(result) {
  const div = document.getElementById('meeting-result');
  if (!div) return;
  div.style.display = 'block';

  const roleMap = {};
  MEETING_ROLES.forEach(r => { roleMap[r.id] = r; });
  function roleIcon(roleId, size = 18) {
    const r = roleMap[roleId];
    if (!r) return '';
    return `<span class="vote-icon" style="background:${r.color}" title="${r.name}">${r.letter}</span>`;
  }

  let html = '<h4>最终排名</h4>';
  html += '<table class="meeting-ranking-table"><thead><tr><th>排名</th><th>企业</th><th>Borda</th><th>支持</th><th>反对</th><th>理由</th></tr></thead><tbody>';
  for (const r of (result.final_ranking || [])) {
    const supIcons = (r.supporters || []).map(id => roleIcon(id)).join('');
    const oppIcons = (r.opposers || []).map(id => roleIcon(id)).join('');
    html += `<tr>
      <td><strong>${r.rank}</strong></td>
      <td>${r.name} (${r.ticker})</td>
      <td>${r.borda_points}</td>
      <td class="vote-cell">${supIcons || '<span class="vote-none">—</span>'}</td>
      <td class="vote-cell">${oppIcons || '<span class="vote-none">—</span>'}</td>
      <td>${r.reasoning || ''}</td>
    </tr>`;
  }
  html += '</tbody></table>';

  if (result.investment_thesis) {
    html += `<div class="meeting-thesis md-body"><strong>投资主线:</strong> ${formatMarkdown(result.investment_thesis)}</div>`;
  }

  if (result.key_agreements?.length) {
    html += '<div class="meeting-section"><strong>共识:</strong><ul>' +
      result.key_agreements.map(a => `<li>${a}</li>`).join('') + '</ul></div>';
  }
  if (result.key_disagreements?.length) {
    html += '<div class="meeting-section"><strong>分歧:</strong><ul>' +
      result.key_disagreements.map(d => `<li>${d}</li>`).join('') + '</ul></div>';
  }
  if (result.risk_warnings?.length) {
    html += '<div class="meeting-section meeting-risk"><strong>风险警示:</strong><ul>' +
      result.risk_warnings.map(w => `<li>${w}</li>`).join('') + '</ul></div>';
  }

  div.innerHTML = html;
}

export function restoreMeeting(meetingData) {
  if (!meetingData) return;
  state.meetingResult = meetingData;
  const transcript = document.getElementById('meeting-transcript');
  const messages = document.getElementById('meeting-messages');
  if (transcript) transcript.style.display = 'block';
  if (messages) messages.innerHTML = '';

  let lastRound = -1;
  for (const msg of (meetingData.transcript || [])) {
    if (msg.round_num !== lastRound) {
      const roundNames = { 0: '开场', 1: '独立提名', 2: '辩论与质疑', 3: '会议总结' };
      renderMeetingRoundDivider(msg.round_num, roundNames[msg.round_num] || `第${msg.round_num}轮`);
      lastRound = msg.round_num;
    }
    const participant = (meetingData.participants || []).find(p => p.role === msg.role);
    renderMeetingBubble({
      ...msg,
      avatar_letter: participant?.avatar_letter || msg.participant_name?.charAt(0) || '?',
      color: participant?.color || '#64748b',
    });
  }

  if (meetingData.final_ranking?.length) {
    renderMeetingResult(meetingData);
  }

  enableMeetingButton();
  const statusEl = document.getElementById('meeting-status');
  if (statusEl) statusEl.textContent = '已完成';
  const btn = document.getElementById('btn-start-meeting');
  if (btn) btn.textContent = '重新开会';
  showMeetingExport();
}

/* ── AI 评点权重指纹 ──────────────────────── */
function _aiScoringConfig() {
  return state.phase3?.scoring_config || { quality_weight: 0.5, alpha_weight: 0.5 };
}

function _aiConfigMatch(cached) {
  if (!cached?.scoring_config) return false;
  const cur = _aiScoringConfig();
  return cached.scoring_config.quality_weight === cur.quality_weight
      && cached.scoring_config.alpha_weight === cur.alpha_weight;
}

/* ── AI 解读 — 内嵌展开面板 ──────────────────── */

function _updateExpandMeta(panel, data) {
  const modelEl = panel.querySelector('.ai-expand-model');
  const timeEl = panel.querySelector('.ai-expand-time');
  if (modelEl && data.model) modelEl.textContent = data.model;
  if (timeEl && data.generated_at) {
    const ts = data.generated_at.replace('T', ' ').slice(0, 19);
    timeEl.textContent = ts;
  }
}

export function updateTriggerBtn(chartType, hasCache) {
  const btn = document.querySelector(`.ai-interp-trigger[data-chart-type="${chartType}"]`);
  if (btn) {
    btn.classList.toggle('has-cache', !!hasCache);
    btn.textContent = hasCache ? '查看解读' : 'AI 解读';
  }
}

let _aiInterpBusy = false;
export function toggleAiInterp(chartType) {
  if (_aiInterpBusy) return;
  _aiInterpBusy = true;
  setTimeout(() => _aiInterpBusy = false, 300);

  const panel = document.getElementById(`ai-expand-${chartType}`);
  if (!panel || !state.analysisId) return;

  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }

  panel.style.display = '';
  const body = panel.querySelector('.ai-expand-body');
  let cached = state.aiReports[chartType];
  // 防串台：只复用属于当前分析的缓存，跨分析残留的缓存视作未命中、重新生成
  if (cached && cached.analysis_id && cached.analysis_id !== state.analysisId) cached = null;

  if (cached?.text) {
    if (_aiConfigMatch(cached)) {
      body.innerHTML = '<div class="md-body">' + formatMarkdown(cached.text) + '</div>';
      _updateExpandMeta(panel, cached);
      return;
    }
    body.innerHTML = `<div class="ai-stale-notice">
      <p>⚠ 权重已调整，以下为旧评点，可能与当前图表不一致</p>
    </div><div class="md-body">` + formatMarkdown(cached.text) + '</div>';
    _updateExpandMeta(panel, cached);
    const regenBtn = panel.querySelector('.ai-regen-btn');
    if (regenBtn) regenBtn.style.display = '';
    return;
  }

  _fetchAiInterp(chartType, false);
}

async function _fetchAiInterp(chartType, force) {
  const panel = document.getElementById(`ai-expand-${chartType}`);
  if (!panel) return;
  panel.style.display = '';
  const body = panel.querySelector('.ai-expand-body');
  const regenBtn = panel.querySelector('.ai-regen-btn');
  if (regenBtn) regenBtn.disabled = true;
  body.innerHTML = '<div class="ai-expand-loading">正在生成 AI 解读...</div>';

  try {
    const { provider, model } = getMainModel();
    const resp = await fetch('/api/ai-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analysis_id: state.analysisId,
        provider, model,
        report_type: 'chart_interp',
        chart_type: chartType,
        force,
      }),
    });
    if (!resp.ok) {
      let msg = `AI 解读请求失败 (${resp.status})`;
      try { const j = await resp.json(); if (j.detail) msg = j.detail; } catch {}
      body.innerHTML = `<p style="color:var(--danger)">${msg}</p>`;
      return;
    }
    body.innerHTML = '';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let accumulated = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.slice(5).trim());
          if (d.kind === 'model_fallback') { window.notifyFallback?.(d.message); continue; }
          if (d.text) {
            accumulated += d.text;
            body.innerHTML = '<div class="md-body">' + formatMarkdown(accumulated) + '</div>';
          }
          if (d.full_text) {
            body.innerHTML = '<div class="md-body">' + formatMarkdown(d.full_text) + '</div>';
            const reportData = {
              text: d.full_text,
              scoring_config: _aiScoringConfig(),
              model: d.model || model,
              provider: d.provider || provider,
              generated_at: d.generated_at || '',
              analysis_id: state.analysisId,
            };
            state.aiReports[chartType] = reportData;
            _updateExpandMeta(panel, reportData);
            updateTriggerBtn(chartType, true);
          }
          if (d.message) body.innerHTML = `<p style="color:var(--danger)">${d.message}</p>`;
        } catch {}
      }
    }
  } catch (e) {
    body.innerHTML = `<p style="color:var(--danger)">AI 解读失败: ${e.message}</p>`;
  } finally {
    if (regenBtn) { regenBtn.disabled = false; regenBtn.style.display = ''; }
  }
}

export { _fetchAiInterp as fetchAiInterp };

export async function generateAiReport() {
  const body = document.getElementById('wiz-report-body');
  const btn = document.getElementById('wiz-gen-report');
  if (!body || !state.analysisId) return;

  let cached = state.aiReports['comparison'];
  // 防串台：跨分析残留的缓存不复用
  if (cached && cached.analysis_id && cached.analysis_id !== state.analysisId) cached = null;
  const isStale = cached?.text && !_aiConfigMatch(cached);
  const isFresh = cached?.text && _aiConfigMatch(cached);

  if (isFresh && btn?.textContent !== '重新生成') {
    body.innerHTML = '<div class="md-body">' + formatMarkdown(cached.text) + '</div>';
    if (btn) btn.textContent = '重新生成';
    return;
  }

  if (isStale && btn?.textContent !== '重新生成') {
    body.innerHTML = `<div class="ai-stale-notice"><p>⚠ 权重已调整，以下为旧报告</p>
      <button class="btn btn-sm ai-regen-btn" id="ai-regen-comparison">重新生成</button>
    </div><div class="md-body">` + formatMarkdown(cached.text) + '</div>';
    document.getElementById('ai-regen-comparison')?.addEventListener('click', () => {
      _fetchAiReport(body, btn, true);
    });
    if (btn) btn.textContent = '重新生成';
    return;
  }

  _fetchAiReport(body, btn, !!isFresh);
}

async function _fetchAiReport(bodyEl, btn, force) {
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
  bodyEl.innerHTML = '<p style="color:var(--muted);text-align:center;padding:20px">正在生成横向对比报告...</p>';

  try {
    const { provider, model } = getMainModel();
    const resp = await fetch('/api/ai-report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        analysis_id: state.analysisId,
        provider, model,
        report_type: 'comparison',
        force,
      }),
    });
    if (!resp.ok) {
      bodyEl.innerHTML = `<p style="color:var(--danger)">AI 报告请求失败 (${resp.status})</p>`;
      return;
    }
    bodyEl.innerHTML = '';
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let accumulated = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        try {
          const d = JSON.parse(line.slice(5).trim());
          if (d.kind === 'model_fallback') { window.notifyFallback?.(d.message); continue; }
          if (d.text) {
            accumulated += d.text;
            bodyEl.innerHTML = '<div class="md-body">' + formatMarkdown(accumulated) + '</div>';
          }
          if (d.full_text) {
            bodyEl.innerHTML = '<div class="md-body">' + formatMarkdown(d.full_text) + '</div>';
            state.aiReports['comparison'] = {
              text: d.full_text,
              scoring_config: _aiScoringConfig(),
              model: d.model || model,
              provider: d.provider || provider,
              generated_at: d.generated_at || '',
              analysis_id: state.analysisId,
            };
          }
          if (d.message) bodyEl.innerHTML = `<p style="color:var(--danger)">${d.message}</p>`;
        } catch {}
      }
    }
  } catch (e) {
    bodyEl.innerHTML = `<p style="color:var(--danger)">报告生成失败: ${e.message}</p>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '重新生成'; }
  }
}
