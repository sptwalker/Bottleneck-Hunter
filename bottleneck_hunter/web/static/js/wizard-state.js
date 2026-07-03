/**
 * wizard-state.js — Wizard 共享状态与工具函数
 */

/* ── Wizard 状态 ───────────────────────────── */
export const state = {
  currentPhase: 0,
  currentPage: null,
  analysisId: null,
  seqNo: null,
  running: false,
  config: {},
  phase1: null,
  phase2: null,
  phase3: null,
  phase4: null,
  p1TriState: 'start',
  p2TriState: 'start',
  p3TriState: 'start',
  manualPicks: [],
  p1Error: false,
  p2Error: false,
  p4Error: false,
  p2NeedsUpdate: false,
  p3NeedsUpdate: false,
  p4NeedsUpdate: false,
  aiReports: {},
  autoMode: false,
};
window.wizardState = state;

/* ── 评分颜色 ─────────────────────────────── */
export const SCORE_COLORS = {
  10: '#FFD700', 9: '#166534', 8: '#16a34a', 7: '#4ade80',
  6: '#f97316', 5: '#f59e0b', 4: '#eab308', 3: '#b45309',
  2: '#9ca3af', 1: '#991b1b',
};

export function getScoreColor(score) {
  const s = Math.round(Math.max(1, Math.min(10, score)));
  return SCORE_COLORS[s] || '#9ca3af';
}

export function scoreNeedsDarkText(score) {
  return [10, 7, 4].includes(Math.round(Math.max(1, Math.min(10, score))));
}

/* ── 日志面板 ─────────────────────────────── */
export function logMsg(text, level = 'info') {
  const body = document.getElementById('wiz-log-body');
  const panel = document.getElementById('wiz-log-panel');
  if (!body || !panel) return;
  if (panel.style.display !== 'none') {
    panel.classList.remove('collapsed');
    const toggleBtn = document.getElementById('wiz-log-toggle');
    if (toggleBtn) toggleBtn.textContent = '▼';
  }
  const ts = new Date().toLocaleTimeString('zh-CN', { hour12: false });
  const cls = level === 'error' ? 'log-error' : level === 'done' ? 'log-done' : level === 'warn' ? 'log-warn' : '';
  const line = document.createElement('div');
  line.className = `wiz-log-line ${cls}`;
  line.innerHTML = `<span class="log-ts">[${ts}]</span> ${text}`;
  body.appendChild(line);
  body.scrollTop = body.scrollHeight;
  const logBtn = document.getElementById('sidebar-log-btn');
  if (logBtn && panel.style.display === 'none') logBtn.classList.add('has-unread');
}

export function clearLog() {
  const body = document.getElementById('wiz-log-body');
  if (body) body.innerHTML = '';
  const panel = document.getElementById('wiz-log-panel');
  if (panel) panel.style.display = 'none';
}

/* ── 解析主分析模型 ──────────────────────── */
export function getMainModel() {
  const sel = document.getElementById('wiz-main-model') || document.getElementById('wiz-p1-model');
  const val = sel?.value;
  // 空值 = "跟随顶栏配置" → 发空 provider,后端走 get_llm_for_position(role) 用顶栏角色配置
  if (!val) return { provider: '', model: '' };
  const [provider, model] = val.split('::');
  return { provider, model };
}

/* ── Markdown 格式化 ─────────────────────── */
export function formatMarkdown(text) {
  if (!text) return '';
  if (typeof marked !== 'undefined' && marked.parse) {
    try {
      return marked.parse(text, { breaks: true, gfm: true });
    } catch (e) {
      console.warn('marked.parse failed, fallback', e);
    }
  }
  return text
    .replace(/^### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    .replace(/^# (.+)$/gm, '<h2>$1</h2>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`)
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>')
    .replace(/^/, '<p>').replace(/$/, '</p>');
}
