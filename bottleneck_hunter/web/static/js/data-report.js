/**
 * data-report.js — 外部数据获取报告页（系统配置中心「数据报告」页签）。
 * 纯 fetch 拉取 /api/data-report/overview 渲染健康/用量/覆盖矩阵。仿 auto-update.js。
 */

const DR_API = '/api/data-report';

function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

const HEALTH_DOT = { success: '🟢', error: '🔴', idle: '⚪', running: '🟡', unknown: '⚪' };
const CAP_LABEL = {
  quote: '实时报价', daily: '日K行情', earnings: '财报/一致预期', news: '新闻',
  sec: 'SEC文件', institutional: '机构/评级', options: '期权', notice: 'A股公告', smartmoney: '聪明钱',
};
const MKT_LABEL = { us_stock: '美股', a_stock: 'A股', hk_stock: '港股' };

async function loadDataReport() {
  const root = document.getElementById('data-report-root');
  if (!root) return;
  try {
    const resp = await fetch(`${DR_API}/overview`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    renderDataReport(await resp.json());
  } catch (e) {
    root.innerHTML = `<p class="aic-hint">加载失败：${esc(e.message)}</p>`;
  }
}

function renderDataReport(d) {
  const root = document.getElementById('data-report-root');
  if (!root) return;
  root.innerHTML = [
    _sourcesSection(d.sources || []),
    _usageSection(d.usage_today || [], d.usage_7d || []),
    _pipelinesSection(d.pipelines || []),
    _managerSection(d.manager || [], d.hub || []),
    _coverageSection(d.coverage || []),
  ].join('');
}

function _sourcesSection(sources) {
  const rows = sources.map(s => {
    const dot = HEALTH_DOT[s.health] || '⚪';
    const cfg = s.configured ? '已配置' : '<span style="color:var(--muted)">未配置</span>';
    const test = s.testable ? '' : '<span class="aic-hint">（无自助API）</span>';
    const err = s.last_error ? `<span style="color:var(--down,#dc2626)">${esc(s.last_error)}</span>` : '';
    return `<tr><td>${dot} ${esc(s.name)} ${test}</td><td>${cfg}</td><td>${esc(s.health)}</td><td>${err}</td><td class="aic-hint">${esc((s.last_run_at || '').slice(0, 16).replace('T', ' '))}</td></tr>`;
  }).join('');
  return `<h4 style="margin:4px 0 8px">数据源健康</h4>
    <table class="aic-test-table"><thead><tr><th>数据源</th><th>配置</th><th>状态</th><th>错误</th><th>上次巡检</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="5" class="aic-hint">无数据源</td></tr>'}</tbody></table>`;
}

function _usageSection(today, week) {
  const idx = {}; today.forEach(r => { idx[r.source] = r; });
  const merged = week.map(w => ({ ...w, today: (idx[w.source] || {}).calls || 0 }));
  const rows = merged.map(r => {
    const rate = r.ok_rate != null ? r.ok_rate : (r.calls ? Math.round(100 * r.ok / r.calls) : 0);
    const color = rate >= 90 ? 'var(--up,#16a34a)' : rate >= 60 ? '#d97706' : 'var(--down,#dc2626)';
    return `<tr><td>${esc(r.source)}</td><td>${r.today}</td><td>${r.calls}</td>
      <td style="color:${color}">${rate}%</td><td>${r.fail || 0}</td>
      <td>${Math.round(r.avg_latency_ms || 0)}ms</td><td>${r.rows || 0}</td></tr>`;
  }).join('');
  return `<h4 style="margin:16px 0 8px">用量统计（近7日）</h4>
    <table class="aic-test-table"><thead><tr><th>源</th><th>今日调用</th><th>7日调用</th><th>成功率</th><th>失败</th><th>均延迟</th><th>行数</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="7" class="aic-hint">暂无调用记录（触发一次采集后显示）</td></tr>'}</tbody></table>`;
}

function _pipelinesSection(pipelines) {
  if (!pipelines.length) return '';
  const rows = pipelines.map(p => {
    const dot = HEALTH_DOT[p.last_status] || '⚪';
    return `<tr><td>${dot} ${esc(p.pipeline_name)}</td><td>${esc(p.last_status || '')}</td>
      <td>${p.stocks_processed || 0}/${p.stocks_total || 0}</td>
      <td class="aic-hint">${esc((p.last_run_at || '').slice(0, 16).replace('T', ' '))}</td>
      <td style="color:var(--down,#dc2626)">${esc(p.last_error || '')}</td></tr>`;
  }).join('');
  return `<h4 style="margin:16px 0 8px">采集管线状态</h4>
    <table class="aic-test-table"><thead><tr><th>管线</th><th>状态</th><th>处理</th><th>上次运行</th><th>错误</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function _managerSection(manager, hub) {
  const all = [
    ...manager.map(m => ({ ...m, kind: '行情源' })),
    ...hub.map(h => ({ ...h, kind: 'DataHub' })),
  ];
  if (!all.length) return '';
  const rows = all.map(m => {
    const cb = m.circuit_open ? '🔴 熔断' : '🟢 正常';
    const mkts = (m.markets || []).map(x => MKT_LABEL[x] || x).join('/');
    return `<tr><td>${esc(m.name)}</td><td>${esc(m.kind)}</td><td>${esc(mkts)}</td>
      <td>${cb}</td><td>${m.total_calls || 0}</td><td>${m.total_failures || 0}</td></tr>`;
  }).join('');
  return `<h4 style="margin:16px 0 8px">取数器运行时（熔断/调用）</h4>
    <table class="aic-test-table"><thead><tr><th>源</th><th>类型</th><th>市场</th><th>熔断</th><th>调用</th><th>失败</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function _coverageSection(coverage) {
  if (!coverage.length) return '';
  const mkts = ['us_stock', 'a_stock', 'hk_stock'];
  const head = mkts.map(m => `<th>${MKT_LABEL[m]}</th>`).join('');
  const rows = coverage.map(c => {
    const cells = mkts.map(m => `<td>${c.markets[m] ? '✅' : '—'}</td>`).join('');
    return `<tr><td>${esc(CAP_LABEL[c.capability] || c.capability)}</td>${cells}</tr>`;
  }).join('');
  return `<h4 style="margin:16px 0 8px">能力 × 市场 覆盖矩阵</h4>
    <table class="aic-test-table"><thead><tr><th>数据能力</th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
}

function _drStatus(msg, type) {
  const el = document.getElementById('dr-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = type === 'ok' ? 'oklch(0.72 0.19 142)' : type === 'fail' ? 'oklch(0.63 0.24 25)' : 'var(--muted)';
}

export function initDataReport() {
  // 切到「数据报告」页签时懒加载
  document.querySelectorAll('#view-aiconfig .aic-main-tab[data-tab="datareport"]').forEach(tab => {
    tab.addEventListener('click', () => loadDataReport());
  });
  document.getElementById('dr-refresh')?.addEventListener('click', loadDataReport);
  document.getElementById('dr-probe')?.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; _drStatus('巡检中...', 'info');
    try {
      const resp = await fetch(`${DR_API}/probe`, { method: 'POST' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '巡检失败');
      const ok = (data.results || []).filter(r => r.ok).length;
      _drStatus(`巡检完成：${ok}/${(data.results || []).length} 连通`, 'ok');
      await loadDataReport();
    } catch (err) {
      _drStatus(err.message, 'fail');
    } finally { btn.disabled = false; }
  });
}
