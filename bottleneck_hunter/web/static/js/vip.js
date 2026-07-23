/**
 * vip.js — VIP 私人财务顾问面板：上传月结单 PDF → 解析入库 → 生成投资分析报告。
 * 自包含模块，仅用 fetch + DOM。VIP 按钮可见性由 /api/auth/me 的 vip 标记 / admin 决定。
 */
(function () {
  'use strict';

  const fileEl = document.getElementById('vip-file');
  const marketEl = document.getElementById('vip-market');
  const uploadBtn = document.getElementById('vip-upload-btn');
  const reportBtn = document.getElementById('vip-report-btn');
  const btnVip = document.getElementById('btn-vip');
  if (!uploadBtn || !reportBtn) return;

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

  if (marketEl) marketEl.addEventListener('change', loadDocs);
  // 进入 VIP 视图时刷新文档列表
  const nav = document.getElementById('btn-vip');
  if (nav) nav.addEventListener('click', () => setTimeout(loadDocs, 50));
})();
