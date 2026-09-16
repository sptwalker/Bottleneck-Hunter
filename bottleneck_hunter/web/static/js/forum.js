/**
 * 互动留言系统前端（消费 /api/forum）—— 两个入口：
 *  1) 决策中心「🗣️ 猎手自由讨论区」抽屉：看/发帖、回帖、关评、删帖，SSE 实时刷新。
 *  2) 系统配置中心「AI角色属性配置」页签：AI 自主发言开关/配额/手动触发 + 8 角色人设编辑与禁言。
 *
 * 板主 = 当前登录用户；全端点同源自动带 cookie、后端按 for_user(sub) 严格隔离（跨板不可见）。
 * 所有内容一律 esc() 转义后渲染（帖/回帖来自用户与 AI，防注入）。SSE 用原生 EventSource（同源带 cookie）。
 */
import { showConfirm } from './utils/confirm.js';
import { fmtBJ, toast } from './wizard-state.js';

const API = '/api/forum';

let _es = null;            // EventSource（论坛实时流）
let _identities = null;    // GET /identities 缓存（含 label/banned/overridden），供帖作者名映射复用

/* ── fetch 小封装 ─────────────────────────────────────── */
const $ = (id) => document.getElementById(id);
function esc(s) { const d = document.createElement('div'); d.textContent = s ?? ''; return d.innerHTML; }

async function jfetch(path, opts = {}) {
  const resp = await fetch(API + path, opts);
  if (resp.status === 401) throw new Error('请先登录');
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    try { msg = (await resp.json()).detail || msg; } catch { /* 非 JSON 错误体 */ }
    throw new Error(msg);
  }
  try { return await resp.json(); } catch { return null; }
}
const jbody = (method) => (path, body) => jfetch(path, {
  method, headers: { 'Content-Type': 'application/json' },
  body: body == null ? undefined : JSON.stringify(body),
});
const jpost = jbody('POST');
const jput = jbody('PUT');
const jdel = (path) => jfetch(path, { method: 'DELETE' });

async function ensureIdentities(force = false) {
  if (_identities && !force) return _identities;
  _identities = (await jfetch('/identities')).identities || [];
  return _identities;
}
function roleName(rk) {
  const id = _identities?.find((x) => x.role_key === rk);
  return id ? id.display_name : (rk || 'AI');
}
function authorBadge(t) {
  return t === 'ai'
    ? '<span class="forum-badge forum-badge-ai">AI</span>'
    : '<span class="forum-badge forum-badge-me">我</span>';
}
function authorName(a) { return a.author_type === 'ai' ? roleName(a.author_role_key) : '我'; }

/* ── 讨论区抽屉 ───────────────────────────────────────── */
async function openForum() {
  const d = $('forum-drawer');
  if (!d) return;
  d.style.display = '';
  document.body.style.overflow = 'hidden';  // 锁背景滚动：抽屉内 .drawer-panel 自身 overflow-y 独立滚动，否则内容不满屏时滚轮冒泡到背景页
  try { await ensureIdentities(); } catch { /* 名字映射失败不挡帖子渲染 */ }
  loadPosts();
  startStream();
}
function closeForum() {
  const d = $('forum-drawer');
  if (d) d.style.display = 'none';
  document.body.style.overflow = '';  // 还原背景滚动
  stopStream();
}

async function loadPosts() {
  const box = $('forum-list');
  if (!box) return;
  try {
    const { posts } = await jfetch('/posts?limit=50');
    renderPosts(posts || []);
  } catch (e) {
    box.innerHTML = `<div class="forum-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderPosts(posts) {
  const box = $('forum-list');
  if (!box) return;
  if (!posts.length) {
    box.innerHTML = '<div class="forum-empty">还没有帖子。发一条试试，'
      + '或到「系统配置中心 → AI角色属性配置」开启 AI 发言。</div>';
    return;
  }
  box.innerHTML = posts.map((p) => `
    <div class="forum-post" data-id="${p.id}">
      <div class="forum-post-head">
        ${authorBadge(p.author_type)}
        <b>${esc(authorName(p))}</b>
        ${p.ticker ? `<span class="forum-ticker">${esc(p.ticker)}</span>` : ''}
        ${p.comments_closed ? '<span class="forum-badge forum-badge-muted">已关评</span>' : ''}
        <span class="forum-time">${esc(fmtBJ(p.created_at))}</span>
      </div>
      ${p.title ? `<div class="forum-post-title">${esc(p.title)}</div>` : ''}
      <div class="forum-post-body">${esc(p.body)}</div>
      <div class="forum-post-actions">
        <button class="forum-link" data-act="replies">💬 回帖${p.reply_count ? ` ${p.reply_count}` : ''}</button>
        <button class="forum-link" data-act="toggleclose">${p.comments_closed ? '开评' : '关评'}</button>
        <button class="forum-link forum-danger" data-act="del">删帖</button>
      </div>
      <div class="forum-replies" data-for="${p.id}" hidden></div>
    </div>`).join('');
}

async function submitPost() {
  const bodyEl = $('forum-new-body');
  const body = bodyEl.value.trim();
  if (!body) { toast('说点什么再发', 'warning'); return; }
  const btn = $('forum-post-btn');
  btn.disabled = true;
  try {
    await jpost('/posts', {
      body, title: $('forum-new-title').value.trim(), ticker: $('forum-new-ticker').value.trim(),
    });
    bodyEl.value = ''; $('forum-new-title').value = ''; $('forum-new-ticker').value = '';
    toast('已发布');
    loadPosts();
  } catch (e) {
    toast(e.message || '发布失败', 'error');
  } finally {
    btn.disabled = false;
  }
}

async function refreshReplies(postEl) {
  const box = postEl.querySelector('.forum-replies');
  const { post, replies } = await jfetch(`/posts/${postEl.dataset.id}`);
  renderReplies(box, post, replies || []);
}

async function toggleReplies(postEl) {
  const box = postEl.querySelector('.forum-replies');
  if (!box.hidden) { box.hidden = true; box.innerHTML = ''; return; }
  box.hidden = false;
  box.innerHTML = '<div class="forum-empty">加载中…</div>';
  try { await refreshReplies(postEl); } catch (e) {
    box.innerHTML = `<div class="forum-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderReplies(box, post, replies) {
  const items = replies.map((r) => `
    <div class="forum-reply" data-rid="${r.id}">
      ${authorBadge(r.author_type)}
      <b>${esc(authorName(r))}</b>
      <span class="forum-reply-body">${esc(r.body)}</span>
      <span class="forum-time">${esc(fmtBJ(r.created_at))}</span>
      <button class="forum-link forum-danger" data-act="delreply">删</button>
    </div>`).join('') || '<div class="forum-empty">还没有回帖</div>';
  const composer = post.comments_closed
    ? '<div class="forum-empty">该帖已关闭评论</div>'
    : '<div class="forum-reply-compose">'
      + '<input type="text" class="forum-input forum-reply-input" placeholder="回一句…" maxlength="500">'
      + '<button class="btn btn-sm btn-primary" data-act="sendreply">回复</button></div>';
  box.innerHTML = items + composer;
}

async function onListClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const postEl = btn.closest('.forum-post');
  const id = postEl.dataset.id;
  const act = btn.dataset.act;
  try {
    if (act === 'replies') { await toggleReplies(postEl); return; }
    if (act === 'del') {
      if (!await showConfirm('确定删除这条帖子？（软删除，保留可审计）', { title: '删帖', danger: true })) return;
      await jdel(`/posts/${id}`); toast('已删除'); loadPosts(); return;
    }
    if (act === 'toggleclose') {
      const closing = btn.textContent.trim() === '关评';
      await jpost(`/posts/${id}/${closing ? 'close' : 'open'}`); toast(closing ? '已关评' : '已开评'); loadPosts(); return;
    }
    if (act === 'sendreply') {
      const input = btn.closest('.forum-replies').querySelector('.forum-reply-input');
      const body = input.value.trim();
      if (!body) return;
      await jpost(`/posts/${id}/replies`, { body }); input.value = ''; await refreshReplies(postEl); return;
    }
    if (act === 'delreply') {
      if (!await showConfirm('删除这条回帖？', { title: '删回帖', danger: true })) return;
      await jdel(`/replies/${btn.closest('.forum-reply').dataset.rid}`); await refreshReplies(postEl); return;
    }
  } catch (err) {
    toast(err.message || '操作失败', 'error');
  }
}

/* ── SSE 实时刷新 ─────────────────────────────────────── */
function startStream() {
  if (_es) return;
  try {
    _es = new EventSource(`${API}/stream`);
    // ponytail: 事件到就整表重拉，单人板低频足够；量大再做增量渲染。展开中的回帖会收起，可接受。
    _es.addEventListener('forum', () => { if ($('forum-drawer')?.style.display !== 'none') loadPosts(); });
    _es.onerror = () => { /* EventSource 自带重连，无需处理 */ };
  } catch { /* 不支持 EventSource：退化为手动刷新，不报错 */ }
}
function stopStream() { if (_es) { _es.close(); _es = null; } }

/* ── 系统配置中心：AI角色属性配置页签 ───────────────────── */
async function loadConfig() {
  await loadSettings();
  await renderIdentityCards();
}

async function loadSettings() {
  try {
    const s = await jfetch('/settings');
    $('forum-ai-enabled').checked = !!s.ai_enabled;
    $('forum-daily-cap').value = s.daily_cap ?? 20;
  } catch (e) { toast(e.message || '设置加载失败', 'error'); }
}

async function saveSettings() {
  try {
    await jput('/settings', {
      ai_enabled: $('forum-ai-enabled').checked,
      daily_cap: Number($('forum-daily-cap').value) || 20,
    });
    setCfgStatus('已保存');
  } catch (e) { toast(e.message || '保存失败', 'error'); }
}

async function runAI() {
  const btn = $('forum-ai-run');
  btn.disabled = true;
  setCfgStatus('AI 发言中…');
  try {
    const r = await jpost('/ai/run');
    const total = (r.posts || 0) + (r.replies || 0);
    if (total) {
      const msg = `本轮 AI 发帖 ${r.posts || 0} 条、回帖 ${r.replies || 0} 条`;
      setCfgStatus(msg); toast(msg);
    } else {
      setCfgStatus(r.reason || '本轮没有新发言');
    }
  } catch (e) { toast(e.message || '触发失败', 'error'); setCfgStatus(''); } finally { btn.disabled = false; }
}
function setCfgStatus(t) { const el = $('forum-settings-status'); if (el) el.textContent = t; }

const _ID_FIELDS = [
  ['display_name', '昵称'], ['gender', '性别'], ['age', '年龄'],
  ['persona_identity', '身份'], ['personality', '性格'], ['bio', '简介'],
];
const _WIDE = new Set(['persona_identity', 'personality', 'bio']);

async function renderIdentityCards() {
  const root = $('forum-roles-root');
  if (!root) return;
  try {
    const ids = await ensureIdentities(true);
    root.innerHTML = ids.map(cardHtml).join('');
  } catch (e) {
    root.innerHTML = `<div class="forum-empty">加载失败：${esc(e.message)}</div>`;
  }
}

function cardHtml(id) {
  const fields = _ID_FIELDS.map(([k, label]) => {
    const v = esc(id[k] ?? '');
    return _WIDE.has(k)
      ? `<label class="forum-id-field forum-id-field-wide">${label}<textarea data-k="${k}" rows="2">${v}</textarea></label>`
      : `<label class="forum-id-field">${label}<input type="text" data-k="${k}" value="${v}"></label>`;
  }).join('');
  return `<div class="forum-id-card${id.banned ? ' is-banned' : ''}" data-rk="${id.role_key}">
    <div class="forum-id-head">
      <b>${esc(id.display_name)}</b>
      <span class="forum-id-role">${esc(id.label)}</span>
      ${id.overridden ? '<span class="forum-badge forum-badge-muted">已自定义</span>' : ''}
      ${id.banned ? '<span class="forum-badge forum-badge-ban">已禁言</span>' : ''}
    </div>
    <div class="forum-id-grid">${fields}</div>
    <div class="forum-id-actions">
      <button class="btn btn-sm btn-primary" data-act="saveid">保存</button>
      <button class="btn btn-sm" data-act="toggleban">${id.banned ? '解禁' : '禁言'}</button>
    </div>
  </div>`;
}

async function onRolesClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const card = btn.closest('.forum-id-card');
  const rk = card.dataset.rk;
  try {
    if (btn.dataset.act === 'saveid') {
      const patch = {};
      card.querySelectorAll('[data-k]').forEach((el) => { patch[el.dataset.k] = el.value.trim(); });
      await jput(`/identities/${rk}`, patch); toast('已保存'); renderIdentityCards(); return;
    }
    if (btn.dataset.act === 'toggleban') {
      const banning = btn.textContent.trim() === '禁言';
      if (banning && !await showConfirm('禁言后该角色不再自主发言，确定？', { title: '禁言角色' })) return;
      await jpost(`/identities/${rk}/${banning ? 'ban' : 'unban'}`); toast(banning ? '已禁言' : '已解禁'); renderIdentityCards();
    }
  } catch (err) {
    toast(err.message || '操作失败', 'error');
  }
}

/* ── 初始化 ───────────────────────────────────────────── */
export function initForum() {
  // 决策中心：讨论区抽屉
  $('forum-open-btn')?.addEventListener('click', openForum);
  $('forum-close')?.addEventListener('click', closeForum);
  $('forum-drawer')?.addEventListener('click', (e) => { if (e.target.id === 'forum-drawer') closeForum(); });
  $('forum-post-btn')?.addEventListener('click', submitPost);
  const list = $('forum-list');
  if (list) {
    list.addEventListener('click', onListClick);
    list.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.classList.contains('forum-reply-input')) {
        e.preventDefault();
        e.target.closest('.forum-replies').querySelector('[data-act="sendreply"]')?.click();
      }
    });
  }

  // 系统配置中心：AI角色属性配置页签（点开即拉，2 个 GET，自愈无陈旧态）
  document.querySelector('.aic-main-tab[data-tab="roles"]')?.addEventListener('click', loadConfig);
  $('forum-settings-save')?.addEventListener('click', saveSettings);
  $('forum-ai-run')?.addEventListener('click', runAI);
  $('forum-roles-root')?.addEventListener('click', onRolesClick);
}
