/**
 * auto-update.js — 自动更新设置面板（AI 配置视图内）
 * 用户：总开关 + 分类开关 + 陈旧阈值 + 每类"立即刷新"。管理员：全局时间表编辑。
 */

const API = '/api/settings';
const CATS = ['watchlist_data', 'daily_decision', 'weekly_strategy', 'auto_review', 'catalyst', 'full_refresh'];

function _fmtNext(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString('zh-CN', { hour12: false }).slice(5, 16); }
  catch { return '—'; }
}

let _state = null;

async function loadAutoUpdate() {
  try {
    const resp = await fetch(`${API}/auto-update`);
    if (!resp.ok) return;
    _state = await resp.json();
    renderAutoUpdate();
  } catch (e) { console.warn('加载自动更新配置失败', e); }
}

function renderAutoUpdate() {
  if (!_state) return;
  const cfg = _state.config || {};
  const labels = _state.category_labels || {};
  const jobs = _state.jobs || [];

  const master = document.getElementById('au-master');
  if (master) master.checked = cfg.master_enabled === '1';

  const thr = document.getElementById('au-stale-threshold');
  if (thr) thr.value = cfg.stale_threshold_hours || '24';

  // 各分类开关 + 下次运行时间 + 立即刷新
  const jobsByCat = {};
  jobs.forEach(j => { (jobsByCat[j.category] = jobsByCat[j.category] || []).push(j); });
  const box = document.getElementById('au-categories');
  if (box) {
    box.innerHTML = CATS.map(cat => {
      const on = cfg[cat] === '1';
      const cjobs = jobsByCat[cat] || [];
      const next = cjobs.map(j => j.next_run_at).filter(Boolean).sort()[0];
      return `<div class="au-cat-row">
        <label class="au-toggle">
          <input type="checkbox" class="au-cat" data-cat="${cat}" ${on ? 'checked' : ''}>
          <span>${labels[cat] || cat}</span>
        </label>
        <span class="au-next">下次：${_fmtNext(next)}</span>
        <button class="btn btn-xs au-run" data-cat="${cat}">立即刷新</button>
      </div>`;
    }).join('');
  }

  // 管理员区
  const adminSec = document.getElementById('au-admin-section');
  if (adminSec) adminSec.style.display = _state.is_admin ? '' : 'none';
  if (_state.is_admin) { renderScheduleEditor(); loadEgressStatus(); }
  bindAutoUpdate();
}

async function loadEgressStatus() {
  const el = document.getElementById('au-egress-status');
  if (!el) return;
  try {
    const resp = await fetch('/api/egress/status');
    if (!resp.ok) { el.style.display = 'none'; return; }
    el.style.display = '';
    const s = await resp.json();
    if (s.connected) {
      const sites = (s.reachable || []).join('、') || '白名单站点';
      el.className = 'au-egress-status au-egress-on';
      el.innerHTML = `🛰 借道中 · 桌面已连接（${s.count} 个），可达：${sites}`;
    } else {
      el.className = 'au-egress-status au-egress-off';
      el.innerHTML = '⚪ 未借道 · 在桌面运行 <code>bottleneck-hunter relay --server &lt;本服务器地址&gt;</code> 后自动生效';
    }
  } catch { el.style.display = 'none'; }
}

function renderScheduleEditor() {
  const ge = document.getElementById('au-global-enabled');
  if (ge) ge.checked = _state.global_enabled !== false;
  const sch = _state.global_schedule || {};
  const labels = _state.job_labels || {};
  const grid = document.getElementById('au-schedule-grid');
  if (!grid) return;
  grid.innerHTML = Object.entries(sch).map(([jobId, t]) => {
    const L = labels[jobId] || {};
    const name = L.label || jobId;
    const meta = [L.tz, L.freq].filter(Boolean).join(' · ');
    const nameCell = `<span class="au-sch-name" title="${jobId}">
        <span class="au-sch-label">${name}</span>
        <span class="au-sch-desc">${L.desc || ''}${meta ? '（' + meta + '）' : ''}</span>
      </span>`;
    if ('interval_hours' in t) {
      return `<div class="au-sch-row">${nameCell}
        <span class="au-sch-fields">每 <input type="number" class="au-sch" data-job="${jobId}" data-field="interval_hours" value="${t.interval_hours}" min="1" max="168" style="width:56px"> 小时</span></div>`;
    }
    const dow = 'day_of_week' in t
      ? `<span title="周几运行：mon-fri/sat/sun">周 <input type="text" class="au-sch" data-job="${jobId}" data-field="day_of_week" value="${t.day_of_week}" style="width:56px"></span> `
      : '';
    return `<div class="au-sch-row">${nameCell}
      <span class="au-sch-fields">${dow}<span title="触发时:分">时刻 <input type="number" class="au-sch" data-job="${jobId}" data-field="hour" value="${t.hour ?? 0}" min="0" max="23" style="width:48px">:<input type="number" class="au-sch" data-job="${jobId}" data-field="minute" value="${t.minute ?? 0}" min="0" max="59" style="width:48px"></span></span></div>`;
  }).join('');
}

let _bound = false;
function bindAutoUpdate() {
  if (_bound) return; _bound = true;

  document.getElementById('au-master')?.addEventListener('change', e =>
    saveUser({ master_enabled: e.target.checked }));
  document.getElementById('au-stale-threshold')?.addEventListener('change', e =>
    saveUser({ stale_threshold_hours: parseInt(e.target.value) || 24 }));

  document.getElementById('au-categories')?.addEventListener('change', e => {
    const cb = e.target.closest('.au-cat');
    if (cb) saveUser({ [cb.dataset.cat]: cb.checked });
  });
  document.getElementById('au-categories')?.addEventListener('click', async e => {
    const btn = e.target.closest('.au-run');
    if (!btn) return;
    btn.disabled = true; btn.textContent = '已触发';
    try { await fetch(`${API}/auto-update/run/${btn.dataset.cat}`, { method: 'POST' }); }
    catch {}
    setTimeout(() => { btn.disabled = false; btn.textContent = '立即刷新'; }, 3000);
  });

  document.getElementById('au-save-schedule')?.addEventListener('click', saveSchedule);
}

async function saveUser(patch) {
  const st = document.getElementById('au-save-status');
  if (st) st.textContent = '保存中...';
  try {
    const resp = await fetch(`${API}/auto-update`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
    });
    if (st) st.textContent = resp.ok ? '✓ 已保存' : '✗ 保存失败';
    if (resp.ok) { const d = await resp.json(); _state.config = d.config; }
  } catch { if (st) st.textContent = '✗ 保存失败'; }
  setTimeout(() => { if (st) st.textContent = ''; }, 2000);
}

async function saveSchedule() {
  const st = document.getElementById('au-schedule-status');
  if (st) st.textContent = '保存中...';
  const schedule = {};
  document.querySelectorAll('#au-schedule-grid .au-sch').forEach(inp => {
    const job = inp.dataset.job, field = inp.dataset.field;
    schedule[job] = schedule[job] || {};
    schedule[job][field] = (field === 'day_of_week') ? inp.value.trim() : parseInt(inp.value);
  });
  const body = {
    global_enabled: document.getElementById('au-global-enabled')?.checked,
    schedule,
  };
  try {
    const resp = await fetch(`${API}/schedule`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (st) st.textContent = resp.ok ? '✓ 已保存并重排' : '✗ 保存失败';
  } catch { if (st) st.textContent = '✗ 保存失败'; }
  setTimeout(() => { if (st) st.textContent = ''; }, 2500);
}

export function initAutoUpdate() {
  // AI 配置视图激活时加载（与其它 section 同页，直接加载即可）
  loadAutoUpdate();
  // 切到 AI 配置时刷新一次（拿最新 next_run）
  document.querySelector('.nav-btn[data-view="aiconfig"]')
    ?.addEventListener('click', () => setTimeout(loadAutoUpdate, 100));
}
