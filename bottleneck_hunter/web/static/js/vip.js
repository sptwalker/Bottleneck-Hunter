/**
 * vip.js — VIP 私人财务顾问面板：上传月结单 PDF → 解析入库 → 生成投资分析报告。
 * 自包含模块，仅用 fetch + DOM。VIP 按钮可见性由 /api/auth/me 的 vip 标记 / admin 决定。
 */
(function () {
  'use strict';

  const fileEl = document.getElementById('vip-file');
  const marketEl = document.getElementById('vip-market');
  const uploadBtn = document.getElementById('vip-upload-btn');
  const derivFileEl = document.getElementById('vip-deriv-file');
  const derivUploadBtn = document.getElementById('vip-deriv-upload-btn');
  const reportBtn = document.getElementById('vip-report-btn');
  const chatInput = document.getElementById('vip-chat-input');
  const chatSend = document.getElementById('vip-chat-send');
  const chatNew = document.getElementById('vip-chat-new');
  const chatLog = document.getElementById('vip-chat-log');
  const btnVip = document.getElementById('btn-vip');
  let currentSessionId = '';
  if (!uploadBtn || !reportBtn || !derivUploadBtn || !chatSend) return;

  function setStatus(id, msg, ok) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || '';
    el.className = 'account-status' + (msg ? (ok ? ' success' : ' error') : '');
  }

  // VIP 按钮可见性：/api/auth/me 返回的 vip 标记（admin 或 settings.vip）
  async function gateVipButton() {
    if (!btnVip) return;
    try {
      const r = await fetch('/api/auth/me');
      if (!r.ok) return;
      const u = await r.json();
      btnVip.style.display = u.vip ? '' : 'none';
    } catch (_) { /* 静默 */ }
  }
  gateVipButton();

  async function loadDocs() {
    try {
      const r = await fetch(`/api/vip/statements?market=${encodeURIComponent(marketEl.value)}`);
      if (!r.ok) return;
      const data = await r.json();
      const docs = data.documents || [];
      const box = document.getElementById('vip-docs');
      if (!box) return;
      if (!docs.length) { box.innerHTML = '<p class="empty-text">暂无已导入月结单</p>'; return; }
      let html = '<table class="data-table"><thead><tr><th>期末</th><th>券商</th><th>状态</th><th>对账</th></tr></thead><tbody>';
      for (const d of docs) {
        let flags = '';
        try { flags = (JSON.parse(d.recon_flags_json || '{}').equities_recon) || ''; } catch (_) {}
        html += `<tr><td>${d.period_end || '—'}</td><td>${d.broker || '—'}</td><td>${d.status}</td><td>${flags}</td></tr>`;
      }
      box.innerHTML = html + '</tbody></table>';
    } catch (_) {}
  }

  async function loadDerivatives() {
    try {
      const r = await fetch(`/api/vip/derivatives?market=${encodeURIComponent(marketEl.value)}`);
      if (!r.ok) return;
      const data = await r.json();
      const items = data.items || [];
      const box = document.getElementById('vip-deriv-list');
      if (!box) return;
      if (!items.length) { box.innerHTML = '<p class="empty-text">暂无已建模衍生品文件</p>'; return; }
      let html = '<table class="data-table"><thead><tr><th>产品族</th><th>标的</th><th>币种</th><th>来源文件</th></tr></thead><tbody>';
      for (const d of items) {
        html += `<tr><td>${d.product_family || '—'}</td><td>${d.underlying_symbol || '—'}</td><td>${d.currency || '—'}</td><td>${d.source_file || '—'}</td></tr>`;
      }
      box.innerHTML = html + '</tbody></table>';
    } catch (_) {}
  }

  uploadBtn.addEventListener('click', async () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) { setStatus('vip-upload-status', '请先选择 PDF 文件', false); return; }
    uploadBtn.disabled = true;
    setStatus('vip-upload-status', '上传解析中…', true);
    try {
      const fd = new FormData();
      fd.append('file', f);
      const url = `/api/vip/statements/upload?market=${encodeURIComponent(marketEl.value)}`;
      const r = await fetch(url, { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) { setStatus('vip-upload-status', '✗ ' + (data.detail || '解析失败'), false); return; }
      if (data.duplicate) {
        setStatus('vip-upload-status', '该月结单已导入过（去重）', true);
      } else if (data.status === 'parsed_ok') {
        setStatus('vip-upload-status',
          `✓ 解析成功：${data.n_positions || 0} 只持仓，总权益 $${(data.total_equity || 0).toLocaleString()}`, true);
      } else {
        setStatus('vip-upload-status', `⚠ 状态：${data.status}（对账 ${data.recon?.status || '—'}），待复核`, false);
      }
      await loadDocs();
    } catch (e) {
      setStatus('vip-upload-status', '✗ 上传失败: ' + e.message, false);
    } finally {
      uploadBtn.disabled = false;
    }
  });

  derivUploadBtn.addEventListener('click', async () => {
    const f = derivFileEl.files && derivFileEl.files[0];
    if (!f) { setStatus('vip-deriv-status', '请先选择 PDF 文件', false); return; }
    derivUploadBtn.disabled = true;
    setStatus('vip-deriv-status', '上传建模中…', true);
    try {
      const pwd = prompt('如文件有密码，请输入（无密码可留空）:') || '';
      const fd = new FormData();
      fd.append('file', f);
      const url = `/api/vip/derivatives/upload?market=${encodeURIComponent(marketEl.value)}&pdf_password=${encodeURIComponent(pwd)}`;
      const r = await fetch(url, { method: 'POST', body: fd });
      const data = await r.json();
      if (!r.ok) { setStatus('vip-deriv-status', '✗ ' + (data.detail || '建模失败'), false); return; }
      setStatus('vip-deriv-status', `✓ 已建模：${data.term.family} / ${data.term.underlying}`, true);
      await loadDerivatives();
    } catch (e) {
      setStatus('vip-deriv-status', '✗ 上传失败: ' + e.message, false);
    } finally {
      derivUploadBtn.disabled = false;
    }
  });

  reportBtn.addEventListener('click', async () => {
    reportBtn.disabled = true;
    setStatus('vip-report-status', '生成中…（含 AI 分析约需数十秒）', true);
    try {
      const withAi = document.getElementById('vip-with-ai').checked;
      const url = `/api/vip/reports/generate?market=${encodeURIComponent(marketEl.value)}&with_ai=${withAi}`;
      const r = await fetch(url, { method: 'POST' });
      const data = await r.json();
      if (!r.ok) { setStatus('vip-report-status', '✗ ' + (data.detail || '生成失败'), false); return; }
      const box = document.getElementById('vip-report');
      const md = data.report_md || '';
      box.innerHTML = (window.marked ? window.marked.parse(md) : `<pre>${md.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`);
      const nUnv = (data.unverified || []).length;
      setStatus('vip-report-status', nUnv ? `✓ 已生成（${nUnv} 处数字未核到，已标注）` : '✓ 已生成', true);
    } catch (e) {
      setStatus('vip-report-status', '✗ 生成失败: ' + e.message, false);
    } finally {
      reportBtn.disabled = false;
    }
  });

  function appendChat(role, text) {
    if (!chatLog) return;
    const who = role === 'user' ? '你' : '顾问';
    const div = document.createElement('div');
    div.style.marginBottom = '10px';
    div.innerHTML = `<strong>${who}：</strong>` + (window.marked && role === 'assistant'
      ? window.marked.parse(text || '')
      : `<pre style="white-space:pre-wrap;display:inline;margin:0">${String(text || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}</pre>`);
    chatLog.appendChild(div);
    chatLog.scrollTop = chatLog.scrollHeight;
  }

  async function sendChat() {
    const q = (chatInput.value || '').trim();
    if (!q) return;
    chatSend.disabled = true;
    setStatus('vip-chat-status', '顾问思考中…', true);
    appendChat('user', q);
    chatInput.value = '';
    let aiBox = '';
    try {
      const resp = await fetch('/api/vip/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: currentSessionId, question: q, market: marketEl.value }),
      });
      if (!resp.ok) {
        const j = await resp.json().catch(() => ({}));
        throw new Error(j.detail || `HTTP ${resp.status}`);
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data:')) continue;
          const data = JSON.parse(line.slice(5).trim());
          if (data.session_id) currentSessionId = data.session_id;
          if (data.text) { aiBox += data.text; }
        }
      }
      appendChat('assistant', aiBox);
      setStatus('vip-chat-status', '✓ 已回复', true);
    } catch (e) {
      setStatus('vip-chat-status', '✗ ' + e.message, false);
    } finally {
      chatSend.disabled = false;
    }
  }

  chatSend.addEventListener('click', sendChat);
  if (chatNew) chatNew.addEventListener('click', () => { currentSessionId = ''; if (chatLog) chatLog.innerHTML = ''; setStatus('vip-chat-status', '已新建会话', true); });


  if (marketEl) marketEl.addEventListener('change', () => { loadDocs(); loadDerivatives(); });
  // 进入 VIP 视图时刷新文档列表 / 衍生品列表
  const nav = document.getElementById('btn-vip');
  if (nav) nav.addEventListener('click', () => setTimeout(() => { loadDocs(); loadDerivatives(); }, 50));
})();
