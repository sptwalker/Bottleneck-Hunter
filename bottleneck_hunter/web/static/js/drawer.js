/**
 * drawer.js — 企业详情抽屉面板
 */

import { state } from './wizard-state.js';

/* ── 抽屉 ─────────────────────────────────── */
export function openDrawer(company) {
  const drawer = document.getElementById('wiz-drawer');
  const title = document.getElementById('drawer-title');
  const body = document.getElementById('drawer-body');
  if (!drawer) return;

  const name = company.supplier?.name || company.name || '未知';
  const ticker = company.supplier?.ticker || company.ticker || '';
  const layer = company.supplier?.layer_label || company.layer_label || '';

  title.innerHTML = `${name} ${layer ? `<span class="layer-badge ${layer}">${layer}</span>` : ''} <span class="col-ticker">${ticker}</span>`;

  body.innerHTML = buildDrawerContent(company);
  drawer.style.display = 'flex';

  const klineDom = document.getElementById('drawer-kline');
  if (klineDom && ticker) {
    klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">加载K线数据…</p>';
    const market = state.config?.market || 'us_stock';
    fetch(`/api/stock/${encodeURIComponent(ticker)}/kline?market=${market}`)
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(data => {
        if (!data?.length) { klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">暂无K线数据</p>'; return; }
        renderKlineChart(klineDom, data, name);
      })
      .catch(() => { klineDom.innerHTML = '<p style="color:var(--muted);text-align:center;padding:40px 0">K线数据加载失败</p>'; });
  }
}

export function closeDrawer() {
  const drawer = document.getElementById('wiz-drawer');
  if (drawer) drawer.style.display = 'none';
  const overlay = document.getElementById('kline-fullscreen');
  if (overlay) overlay.remove();
}

function renderKlineChart(dom, data, title) {
  dom.innerHTML = '';
  const chart = echarts.init(dom);
  const dates = data.map(d => d.date);
  const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
  const vols = data.map(d => d.volume);
  const colors = data.map(d => d.close >= d.open ? '#26a69a' : '#ef5350');

  const option = {
    animation: false,
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
    grid: [
      { left: 60, right: 20, top: 20, height: '55%' },
      { left: 60, right: 20, top: '78%', height: '16%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { fontSize: 10, color: '#888' } },
    ],
    yAxis: [
      { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: 'rgba(255,255,255,.06)' } }, axisLabel: { fontSize: 10, color: '#888' } },
      { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: 60, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 14, borderColor: 'transparent', fillerColor: 'rgba(0,160,233,.18)' },
    ],
    series: [
      { type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
      { type: 'bar', data: vols.map((v, i) => ({ value: v, itemStyle: { color: colors[i] + '66' } })),
        xAxisIndex: 1, yAxisIndex: 1 },
    ],
  };
  chart.setOption(option);

  dom.addEventListener('click', () => openKlineFullscreen(data, title));
}

function openKlineFullscreen(data, title) {
  let overlay = document.getElementById('kline-fullscreen');
  if (overlay) overlay.remove();

  overlay = document.createElement('div');
  overlay.id = 'kline-fullscreen';
  overlay.className = 'kline-overlay';
  overlay.innerHTML = `
    <div class="kline-overlay-header">
      <span>${title || ''} 近一年K线</span>
      <button class="kline-overlay-close">&times;</button>
    </div>
    <div id="kline-full-chart" style="flex:1;width:100%"></div>
  `;
  document.body.appendChild(overlay);

  const closeBtn = overlay.querySelector('.kline-overlay-close');
  closeBtn.addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });
  document.addEventListener('keydown', function esc(e) { if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', esc); } });

  requestAnimationFrame(() => {
    const dom = document.getElementById('kline-full-chart');
    if (!dom) return;
    const chart = echarts.init(dom);
    const dates = data.map(d => d.date);
    const ohlc = data.map(d => [d.open, d.close, d.low, d.high]);
    const vols = data.map(d => d.volume);
    const colors = data.map(d => d.close >= d.open ? '#26a69a' : '#ef5350');

    chart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      grid: [
        { left: 80, right: 40, top: 30, height: '58%' },
        { left: 80, right: 40, top: '82%', height: '12%' },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false } },
        { type: 'category', data: dates, gridIndex: 1, axisLabel: { fontSize: 11, color: '#ccc' } },
      ],
      yAxis: [
        { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: 'rgba(255,255,255,.08)' } }, axisLabel: { color: '#ccc' } },
        { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 30, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 8, height: 18, borderColor: 'transparent', fillerColor: 'rgba(0,160,233,.25)' },
      ],
      series: [
        { type: 'candlestick', data: ohlc, xAxisIndex: 0, yAxisIndex: 0,
          itemStyle: { color: '#26a69a', color0: '#ef5350', borderColor: '#26a69a', borderColor0: '#ef5350' } },
        { type: 'bar', data: vols.map((v, i) => ({ value: v, itemStyle: { color: colors[i] + '66' } })),
          xAxisIndex: 1, yAxisIndex: 1 },
      ],
    });
    window.addEventListener('resize', () => chart.resize());
  });
}

function buildDrawerContent(c) {
  const q = c.quality_score || c.overall_score || 0;
  const a = c.alpha_val || c.alpha?.alpha_score || 0;
  const f = c.final_score || 0;
  const snap = c.financial_snapshot || {};
  const alpha = c.alpha || {};
  const capYi = snap.market_cap_yi ?? c.supplier?.market_cap;

  const dimTips = {
    market_position: `市场地位 ${(c.market_position||0).toFixed(1)} 分\nAI 综合评估市场份额、品牌影响力、行业排名`,
    customer_validation: `客户验证 ${(c.customer_validation||0).toFixed(1)} 分\nAI 评估客户质量、长期订单合同、复购率`,
    capacity: `产能状况 ${(c.capacity_status||c.capacity||0).toFixed(1)} 分\nAI 评估产能利用率、扩产计划、交付周期`,
    financial_health: `财务健康 ${(c.financial_health||0).toFixed(1)} 分\n${snap.roe_pct != null ? 'ROE: ' + snap.roe_pct.toFixed(1) + '%' : ''}${snap.debt_ratio_pct != null ? ' 负债率: ' + snap.debt_ratio_pct.toFixed(1) + '%' : ''}${snap.gross_margin_pct != null ? ' 毛利率: ' + snap.gross_margin_pct.toFixed(1) + '%' : ''}`,
    valuation: `估值水平 ${(c.valuation||0).toFixed(1)} 分\n${snap.consensus_pe != null ? '预期PE: ' + snap.consensus_pe.toFixed(1) + 'x' : ''}${capYi != null ? ' 市值: ' + capYi + '亿' : ''}`,
  };

  const alphaDims = [
    { key: 'dim_cap', label: '市值规模', tip: `市值规模得分 ${alpha.dim_cap ?? '-'}\n${capYi != null ? '市值: ' + capYi + '亿' : '市值未知'}\n市值越小，预期差越大` },
    { key: 'dim_analyst', label: '分析师覆盖', tip: `分析师覆盖得分 ${alpha.dim_analyst ?? '-'}\n覆盖机构数: ${snap.analyst_report_count ?? '未知'}\n覆盖越少，信息差越大` },
    { key: 'dim_volume', label: '成交量动量', tip: `成交量动量得分 ${alpha.dim_volume ?? '-'}\n量比(10日/60日): ${snap.volume_ratio?.toFixed(2) ?? '未知'}\n连续放量 ${snap.consecutive_volume_days || 0} 天` },
    { key: 'dim_price', label: '近期涨幅', tip: `近期涨幅得分 ${alpha.dim_price ?? '-'}\n近3月: ${snap.price_change_3m_pct?.toFixed(1) ?? '?'}%  近1月: ${snap.price_change_1m_pct?.toFixed(1) ?? '?'}%` },
    { key: 'dim_institution', label: '机构持仓', tip: `机构持仓得分 ${alpha.dim_institution ?? '-'}\n机构持仓: ${snap.institution_holding_pct?.toFixed(1) ?? '未知'}%` },
  ];

  const fRows = [];
  if (snap.revenue_yi != null) fRows.push(['营收', `${snap.revenue_yi.toFixed(2)} 亿`]);
  if (snap.revenue_yoy_pct != null) fRows.push(['营收同比', `${snap.revenue_yoy_pct > 0 ? '+' : ''}${snap.revenue_yoy_pct.toFixed(1)}%`]);
  if (snap.net_profit_yi != null) fRows.push(['净利润', `${snap.net_profit_yi.toFixed(2)} 亿`]);
  if (snap.net_profit_yoy_pct != null) fRows.push(['净利同比', `${snap.net_profit_yoy_pct > 0 ? '+' : ''}${snap.net_profit_yoy_pct.toFixed(1)}%`]);
  if (snap.gross_margin_pct != null) fRows.push(['毛利率', `${snap.gross_margin_pct.toFixed(1)}%`]);
  if (snap.roe_pct != null) fRows.push(['ROE', `${snap.roe_pct.toFixed(1)}%`]);
  if (snap.debt_ratio_pct != null) fRows.push(['负债率', `${snap.debt_ratio_pct.toFixed(1)}%`]);
  if (snap.consensus_pe != null) fRows.push(['预期PE', `${snap.consensus_pe.toFixed(1)}x`]);
  if (snap.analyst_report_count != null) fRows.push(['机构覆盖', `${snap.analyst_report_count} 家`]);

  return `
    <div class="drawer-score-grid">
      <div class="drawer-score-box"><div class="val val-accent">${f.toFixed(2)}</div><div class="lbl">最终评分</div></div>
      <div class="drawer-score-box"><div class="val val-yellow">${q.toFixed(1)}</div><div class="lbl">质量分</div></div>
      <div class="drawer-score-box"><div class="val val-green">${a.toFixed(1)}</div><div class="lbl">预期差</div></div>
    </div>
    <div class="drawer-section"><h4>企业简介</h4><p style="font-size:13px;line-height:1.7">${c.supplier?.description || c.description || '暂无企业简介'}</p></div>
    ${c.supplier?.products?.length ? `
    <div class="drawer-section"><h4>核心产品</h4><div class="p2-product-tags">${(c.supplier.products || []).map(p => `<span class="p2-product-tag">${p}</span>`).join('')}</div></div>
    ` : ''}
    <div class="drawer-section"><h4>五维评分 <small style="color:var(--muted);font-weight:400">（悬停查看详情）</small></h4><div class="dim-bar-list">
      ${['market_position', 'customer_validation', 'capacity', 'financial_health', 'valuation'].map(k => {
        const labels = { market_position: '市场地位', customer_validation: '客户验证', capacity: '产能状况', financial_health: '财务健康', valuation: '估值水平' };
        const val = k === 'capacity' ? (c.capacity_status ?? c.capacity ?? c.scores?.capacity ?? 0) : (c[k] ?? c.scores?.[k] ?? 0);
        const tip = dimTips[k] || '';
        return `<div class="dim-bar-row" title="${tip}"><span class="dim-bar-label">${labels[k]}</span><div class="dim-bar-track"><div class="dim-bar-fill" style="width:${val * 10}%"></div></div><span class="dim-bar-val">${val.toFixed(1)}</span></div>`;
      }).join('')}
    </div></div>
    ${alpha.alpha_score != null ? `
    <div class="drawer-section"><h4>预期差分析 <small style="color:var(--muted);font-weight:400">（悬停查看详情）</small></h4><div class="dim-bar-list">
      ${alphaDims.map(d => {
        const val = alpha[d.key] ?? 0;
        return `<div class="dim-bar-row" title="${d.tip}"><span class="dim-bar-label">${d.label}</span><div class="dim-bar-track"><div class="dim-bar-fill dim-bar-fill--alpha" style="width:${val * 11.1}%"></div></div><span class="dim-bar-val">${val.toFixed(1)}</span></div>`;
      }).join('')}
    </div></div>` : ''}
    ${fRows.length ? `
    <div class="drawer-section"><h4>财务与市场</h4><div class="fin-grid">
      ${fRows.map(([l, v]) => {
        const isUp = v.includes('+');
        const isDown = v.startsWith('-');
        return `<div class="fin-cell"><span class="fin-label">${l}</span><span class="fin-val${isUp ? ' val-up' : isDown ? ' val-down' : ''}">${v}</span></div>`;
      }).join('')}
    </div></div>` : ''}
    <div class="drawer-section"><h4>股价走势 <small style="color:var(--muted);font-weight:400">（点击放大）</small></h4>
      <div id="drawer-kline" style="height:280px;width:100%;cursor:pointer"></div>
    </div>
    ${c.strengths?.length || c.weaknesses?.length ? `
    <div class="drawer-section"><h4>优势与风险</h4><div class="drawer-strengths">
      <div><h5 style="color:var(--success)">优势</h5>${(c.strengths || []).map(s => `<div class="s-item s-good">✅ ${s}</div>`).join('')}</div>
      <div><h5 style="color:var(--danger)">风险</h5>${(c.weaknesses || c.risks || []).map(r => `<div class="s-item s-bad">⚠️ ${r}</div>`).join('')}</div>
    </div></div>` : ''}
  `;
}
