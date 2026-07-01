/**
 * report-export.js — 会议记录 / 决策总结 的 HTML / PDF 导出
 *
 * 生成独立、可打印的报告页面：新窗口打开，页内含「下载 HTML」+「打印 / 存为 PDF」。
 * 无后端依赖，数据来自前端已加载的会议记录 / 决策概览。
 */

/* ── 小工具 ─────────────────────────────────────── */
const ROLE_LABELS = {
  risk_officer: '风险控制官', growth_investor: '成长投资人',
  value_investor: '价值投资人', contrarian: '逆向投资人', consensus_builder: '共识构建者',
};
const MARKET_LABELS = { us_stock: '美股', a_stock: 'A股', hk_stock: '港股' };

function roleLabel(r) { return ROLE_LABELS[r] || r || ''; }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function fmtDate(ts) { return String(ts || '').replace('T', ' ').slice(0, 16); }
function parseJSON(v) { if (typeof v === 'string') { try { return JSON.parse(v); } catch { return {}; } } return v || {}; }
function asArr(v) { if (Array.isArray(v)) return v; if (v == null || v === '') return []; return [v]; }
function num(v, d = 2) { const n = Number(v); return Number.isFinite(n) ? n.toLocaleString('zh-CN', { maximumFractionDigits: d }) : '--'; }

function voteLabel(v) {
  const s = String(v || '').toLowerCase();
  if (!s || s === '--') return v || '--';
  if (s.includes('modification') || s.includes('conditional')) return '有条件赞成';
  if (s.includes('approve') || s.includes('赞成')) return '赞成';
  if (s.includes('reject') || s.includes('反对')) return '反对';
  if (s.includes('abstain') || s.includes('弃权')) return '弃权';
  return v || '--';
}
function verdictLabel(v) {
  const s = String(v || '').toLowerCase();
  if (!s || s === '--') return v || '--';
  if (s.includes('modification') || s.includes('conditional') || s.includes('有条件')) return '有条件通过';
  if (s.includes('approve') || s.includes('pass') || s.includes('通过')) return '通过';
  if (s.includes('reject') || s.includes('fail') || s.includes('否决')) return '否决';
  if (s.includes('discussion') || s.includes('needs')) return '需再讨论';
  if (s.includes('abstain')) return '弃权';
  return v || '--';
}
function voteClass(v) {
  const s = String(v || '').toLowerCase();
  if (s.includes('modification') || s.includes('conditional') || s.includes('有条件') || s.includes('discussion') || s.includes('needs')) return 'v-cond';
  if (s.includes('approve') || s.includes('pass') || s.includes('通过') || s.includes('赞成')) return 'v-pass';
  if (s.includes('reject') || s.includes('fail') || s.includes('否决') || s.includes('反对')) return 'v-rej';
  return 'v-cond';
}
function voteBadge(v, labelFn) {
  return `<span class="v-badge ${voteClass(v)}">${esc((labelFn || voteLabel)(v))}</span>`;
}
function actionLabel(a) {
  const m = { buy: '买入', add: '加仓', sell: '卖出', reduce: '减仓', hold: '持有',
              trim: '减持', accumulate: '建仓', wait_for_pullback: '等待回调' };
  return m[String(a || '').toLowerCase()] || a || '--';
}

/* ── 通用区块 ───────────────────────────────────── */
function sec(title, inner) { return inner ? `<section class="rpt-sec"><h2>${esc(title)}</h2>${inner}</section>` : ''; }
function kv(pairs) {
  const rows = pairs.filter(([, v]) => v != null && v !== '' && v !== '--')
    .map(([k, v]) => `<div class="kv"><span class="kv-k">${esc(k)}</span><span class="kv-v">${v}</span></div>`).join('');
  return rows ? `<div class="kv-grid">${rows}</div>` : '';
}
function tbl(headers, rows) {
  if (!rows.length) return '';
  const head = headers.map(h => `<th>${esc(h)}</th>`).join('');
  const body = rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join('')}</tr>`).join('');
  return `<table class="rpt-tbl"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function bullets(items, fmt) {
  const li = asArr(items).map(x => `<li>${(fmt ? fmt(x) : esc(typeof x === 'string' ? x : JSON.stringify(x)))}</li>`).join('');
  return li ? `<ul class="rpt-ul">${li}</ul>` : '';
}
function para(t) { return t ? `<p class="rpt-p">${esc(t)}</p>` : ''; }

/* ── 报告外壳（含打印/下载工具条）───────────────── */
const REPORT_CSS = `
*{box-sizing:border-box}
body{margin:0;background:#f3f4f6;color:#1f2937;font:14px/1.7 -apple-system,"Segoe UI",Roboto,"Microsoft YaHei",sans-serif}
.rpt-toolbar{position:sticky;top:0;z-index:10;background:#111827;color:#fff;padding:10px 16px;display:flex;gap:10px;align-items:center}
.rpt-toolbar button{background:#2563eb;color:#fff;border:0;border-radius:6px;padding:6px 14px;font-size:13px;cursor:pointer}
.rpt-toolbar button:hover{background:#1d4ed8}
.rpt-hint{font-size:12px;color:#9ca3af}
.rpt{max-width:900px;margin:20px auto;background:#fff;padding:36px 44px;box-shadow:0 1px 4px rgba(0,0,0,.1)}
.rpt-head{border-bottom:2px solid #111827;padding-bottom:14px;margin-bottom:8px}
.rpt-head h1{margin:0 0 4px;font-size:24px}
.rpt-head .sub{color:#6b7280;font-size:13px}
.rpt-sec{margin:22px 0;page-break-inside:avoid}
.rpt-sec h2{font-size:16px;margin:0 0 10px;padding:6px 10px;background:#f3f4f6;border-left:4px solid #2563eb;border-radius:0 4px 4px 0}
.rpt-sec h3{font-size:14px;margin:14px 0 6px;color:#374151}
.kv-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:6px 18px}
.kv{display:flex;justify-content:space-between;gap:10px;border-bottom:1px dotted #e5e7eb;padding:3px 0}
.kv-k{color:#6b7280}.kv-v{font-weight:600;text-align:right}
.rpt-tbl{width:100%;border-collapse:collapse;margin:8px 0;font-size:13px}
.rpt-tbl th,.rpt-tbl td{border:1px solid #e5e7eb;padding:6px 9px;text-align:left;vertical-align:top}
.rpt-tbl th{background:#f9fafb;font-weight:600}
.rpt-ul{margin:6px 0;padding-left:20px}.rpt-ul li{margin:2px 0}
.rpt-p{margin:6px 0;white-space:pre-wrap}
.v-badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;color:#fff}
.v-pass{background:#16a34a}.v-rej{background:#dc2626}.v-cond{background:#d97706}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;font-size:12px;background:#eef2ff;color:#3730a3;margin-right:4px}
.turn{border:1px solid #e5e7eb;border-radius:6px;padding:10px 12px;margin:8px 0;page-break-inside:avoid}
.turn-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px}
.turn-name{font-weight:600}
.muted{color:#6b7280;font-size:12px}
.chal{background:#eff6ff;border-left:3px solid #3b82f6;border-radius:4px;padding:6px 10px;margin:6px 0;font-size:13px}
.arrow{color:#6b7280;margin:0 4px}
@media print{
  body{background:#fff}
  .no-print{display:none!important}
  .rpt{max-width:none;margin:0;padding:0 6mm;box-shadow:none}
  @page{margin:14mm}
}`;

export function openReport(title, filename, bodyHtml) {
  const shell = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">`
    + `<title>${esc(title)}</title><style>${REPORT_CSS}</style></head><body>`
    + `<div class="rpt-toolbar no-print">`
    + `<button onclick="window.print()">🖨 打印 / 存为 PDF</button>`
    + `<button id="__dl">⬇ 下载 HTML</button>`
    + `<span class="rpt-hint">PDF：打印时目标选择「另存为 PDF」</span></div>`
    + `<main class="rpt">${bodyHtml}</main>`
    + `<script>document.getElementById('__dl').addEventListener('click',function(){`
    + `var h='<!DOCTYPE html>\\n'+document.documentElement.outerHTML;`
    + `var b=new Blob([h],{type:'text/html;charset=utf-8'});`
    + `var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download=${JSON.stringify(filename)};`
    + `document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},100);});`
    + `<\/script></body></html>`;
  const w = window.open('', '_blank');
  if (!w) { alert('弹窗被拦截，请允许本站弹窗后重试'); return; }
  w.document.open(); w.document.write(shell); w.document.close();
}

/* ── 会议记录报告（圆桌 / 投委会）──────────────── */
export function buildMeetingReport(meeting) {
  const m = meeting || {};
  const isCommittee = m.meeting_type === 'committee';
  const typeLabel = isCommittee ? '投委会审议纪要' : '圆桌会议纪要';
  const tickers = asArr(m.tickers_discussed);
  const participants = asArr(m.participants);
  const result = parseJSON(m.result_json);
  const transcript = asArr(m.transcript_json);

  let html = `<div class="rpt-head"><h1>${esc(m.title || typeLabel)}</h1>`
    + `<div class="sub">${esc(typeLabel)} · ${esc(fmtDate(m.created_at))}`
    + (tickers.length ? ` · 标的：${tickers.map(esc).join('、')}` : '')
    + (m.duration_seconds ? ` · 耗时 ${Math.round(m.duration_seconds / 60)} 分钟` : '') + '</div></div>';

  // 参会者
  if (participants.length) {
    html += sec('参会成员', tbl(['角色', '模型'],
      participants.map(p => [esc(roleLabel(p.role) || p.name || '--'), esc(p.model || '')])));
  }

  // 结论
  if (isCommittee) {
    const voteDetail = result.vote_detail || {};
    const weights = result.member_weights || {};
    const voteRows = Object.entries(voteDetail).map(([role, info]) => {
      const v = typeof info === 'object' ? (info.vote || '--') : String(info);
      const conf = typeof info === 'object' ? (info.confidence ?? '--') : '--';
      const w = weights[role] != null ? Number(weights[role]).toFixed(2) + 'x' : '--';
      return [esc(roleLabel(role)), voteBadge(v), conf === '--' ? '--' : conf + '/10', w];
    });
    let inner = kv([
      ['最终结论', voteBadge(m.final_verdict || result.final_verdict, verdictLabel)],
      ['通过率', result.approval_rate != null ? Math.round(result.approval_rate) + '%' : null],
    ]);
    if (voteRows.length) inner += '<h3>委员投票</h3>' + tbl(['委员', '投票', '信心', '历史权重'], voteRows);
    const mods = asArr(result.consensus_modifications);
    if (mods.length) {
      inner += '<h3>共识修改</h3>' + tbl(['标的', '字段', '原值', '修改为', '理由'],
        mods.map(x => [esc(x.ticker || ''), esc(x.field || ''), esc(String(x.original ?? '')),
          esc(String(x.modified ?? '')), esc(x.reason || '')]));
    }
    if (asArr(result.key_risks_flagged).length) inner += '<h3>风险提示</h3>' + bullets(result.key_risks_flagged);
    const minority = asArr(result.minority_opinions);
    if (minority.length) {
      inner += '<h3>少数派意见</h3>' + bullets(minority, x => typeof x === 'object'
        ? `<strong>${esc(roleLabel(x.member))}：</strong>${esc(x.opinion || '')}${x.recommendation ? `（${esc(x.recommendation)}）` : ''}`
        : esc(String(x)));
    }
    inner += para(result.summary || m.investment_thesis);
    html += sec('审议结论', inner);
  } else {
    let ranking = m.final_ranking || result.final_ranking || [];
    ranking = asArr(ranking);
    if (ranking.length) {
      html += sec('最终排名', tbl(['#', '标的', '加权分', '理由'],
        ranking.map((r, i) => [r.rank || i + 1, `<strong>${esc(r.ticker || '')}</strong>`,
          esc(String(r.weighted_score ?? r.score ?? r.borda_points ?? '--')),
          esc((r.reason || r.reasoning || '').slice(0, 200))])));
    }
    html += sec('会议结论', kv([['结论', esc(m.final_verdict || '--')]]) + para(result.summary));
  }

  // 共识与分歧
  const agrees = asArr(m.key_agreements);
  const disagrees = asArr(m.key_disagreements);
  if (agrees.length || disagrees.length) {
    let inner = '';
    if (agrees.length) inner += '<h3>共识</h3>' + bullets(agrees, x => esc(typeof x === 'string' ? x : (x.opinion || x.point || JSON.stringify(x))));
    if (disagrees.length) inner += '<h3>分歧</h3>' + bullets(disagrees, x => typeof x === 'object'
      ? `<strong>${esc(roleLabel(x.member))}：</strong>${esc(x.opinion || x.point || '')}${x.recommendation ? `<br><span class="muted">💡 ${esc(x.recommendation)}</span>` : ''}`
      : esc(String(x)));
    html += sec('共识与分歧', inner);
  }

  // 风险警示
  if (asArr(m.risk_warnings).length) {
    html += sec('风险警示', bullets(m.risk_warnings, x => esc(typeof x === 'string' ? x : (x.warning || x.risk || x.description || JSON.stringify(x)))));
  }

  // 讨论过程（transcript）
  html += buildTranscriptSection(transcript);

  // 回溯结论
  if (m.outcome_recorded && m.outcome_summary) html += sec('回溯结论', para(m.outcome_summary));

  return html;
}

function buildTranscriptSection(transcript) {
  if (!transcript.length) return '';
  const isMember = t => !String(t.role || '').startsWith('_');
  const members = transcript.filter(t => t.round === 1 && isMember(t) && t.type !== 'challenge');
  const revisions = transcript.filter(t => t.round === 2 && isMember(t) && t.type !== 'challenge');
  const challenges = transcript.filter(t => t.type === 'challenge');
  const discussion = transcript.find(t => t.role === '_discussion');
  const chByRole = {};
  challenges.forEach(c => { (chByRole[c.role] = chByRole[c.role] || []).push(c); });

  const turn = (m, showChal) => {
    const concerns = asArr(m.key_concerns);
    const sugg = asArr(m.suggestions);
    const w = (m.weight != null && Math.abs(Number(m.weight) - 1) > 0.001) ? `<span class="tag">权重 ${Number(m.weight).toFixed(2)}x</span>` : '';
    let h = `<div class="turn"><div class="turn-head"><span class="turn-name">${esc(m.name || roleLabel(m.role))}</span>`
      + voteBadge(m.vote) + `<span class="muted">信心 ${m.confidence != null ? m.confidence : '--'}/10</span>${w}`
      + `<span class="muted">${esc(m.model || '')}</span></div>`;
    if (m.content) h += para(m.content);
    if (concerns.length) h += `<div class="muted"><b>关注点：</b>${concerns.map(c => esc(typeof c === 'string' ? c : JSON.stringify(c))).join('；')}</div>`;
    if (sugg.length) h += `<div class="muted"><b>建议：</b>${sugg.map(s => esc(typeof s === 'object' ? `${s.field || ''} ${s.original ?? ''}→${s.suggested ?? ''}` : String(s))).join('；')}</div>`;
    if (showChal) (chByRole[m.role] || []).forEach(c => {
      h += `<div class="chal"><b>🙋 用户质询：</b>${esc(c.user_message)}<br><b>${esc(m.name || roleLabel(m.role))}回应：</b>${esc(c.response)} `
        + (c.vote_changed ? `${voteBadge(c.old_vote)}<span class="arrow">→</span>${voteBadge(c.new_vote)}` : '<span class="muted">（维持原票）</span>') + '</div>';
    });
    return h + '</div>';
  };

  let inner = members.map(m => turn(m, true)).join('');
  if (revisions.length) inner += '<h3>🔁 辩论后改票</h3>' + revisions.map(m => turn(m, false)).join('');
  if (discussion && (discussion.content || discussion.key_disagreement)) {
    inner += '<h3>🗣 圆桌讨论</h3><div class="turn">' + para(discussion.content)
      + (discussion.key_agreement ? `<div class="muted"><b>共识：</b>${esc(discussion.key_agreement)}</div>` : '')
      + (discussion.key_disagreement ? `<div class="muted"><b>分歧：</b>${esc(discussion.key_disagreement)}</div>` : '') + '</div>';
  }
  return sec('讨论过程', inner);
}

/* ── 决策中心总结报告（L1-L4 + 投委会）─────────── */
export function buildDecisionReport(data, market) {
  const d = data || {};
  const mkt = MARKET_LABELS[market] || market || '';
  const now = new Date().toLocaleString('zh-CN', { hour12: false });

  let html = `<div class="rpt-head"><h1>决策中心总结报告</h1>`
    + `<div class="sub">市场：${esc(mkt)} · 生成时间：${esc(now)}</div></div>`;

  // 账户快照
  const acc = d.account || {};
  const positions = asArr(d.positions);
  if (Object.keys(acc).length || positions.length) {
    html += sec('账户快照', kv([
      ['总权益', acc.total_equity != null ? num(acc.total_equity, 0) : null],
      ['可用现金', acc.cash_balance != null ? num(acc.cash_balance, 0) : null],
      ['持仓数', positions.length || null],
    ]));
  }

  // L1 宏观
  const macro = d.macro_strategy;
  if (macro) {
    const rj = parseJSON(macro.result_json);
    html += sec('L1 · 宏观策略', kv([
      ['市场风险', rj.risk_level || rj.market_risk],
      ['趋势判断', asArr(rj.trend_assessment || rj.trend).join('、')],
      ['建议仓位', rj.position_suggestion || rj.recommended_position],
      ['更新于', fmtDate(macro.created_at)],
    ]) + para(rj.market_summary) + (asArr(rj.key_risks).length ? '<h3>关键风险</h3>' + bullets(rj.key_risks) : ''));
  }

  // L2 组合
  const plan = d.strategic_plan;
  if (plan) {
    const rj = parseJSON(plan.result_json);
    const ss = parseJSON(plan.stock_selection || rj.stock_selection);
    const core = asArr(ss.core_holdings), tac = asArr(ss.tactical_holdings);
    const alloc = rj.target_allocation || {};
    const allocLabels = { equity_pct: '权益', cash_pct: '现金', hedge_pct: '对冲', bond_pct: '债券' };
    let inner = kv([
      ['整体立场', rj.overall_stance || rj.stance],
      ...Object.entries(alloc).filter(([, v]) => typeof v === 'number').map(([k, v]) => [allocLabels[k] || k, v + '%']),
      ['更新于', fmtDate(plan.created_at)],
    ]);
    const holdRows = core.concat(tac).filter(h => h.ticker).map(h => [
      `<strong>${esc(h.ticker)}</strong>`, core.includes(h) ? '<span class="tag">核心</span>' : '<span class="tag">战术</span>',
      (h.target_weight_pct ?? '--') + '%', esc(h.reason || h.thesis || '')]);
    if (holdRows.length) inner += '<h3>选股配置</h3>' + tbl(['标的', '类型', '目标仓位', '理由'], holdRows);
    else inner += para(rj.strategy_text || rj.summary);
    html += sec('L2 · 组合策略', inner);
  }

  // L3 战术
  const tacticals = asArr(d.tactical_plans);
  if (tacticals.length) {
    const rows = tacticals.map(p => {
      const rj = parseJSON(p.result_json);
      const ep = rj.entry_plan || {}, xp = rj.exit_plan || {}, ra = rj.risk_assessment || {};
      const entry = ep.ideal_price ?? rj.target_price ?? rj.entry_price ?? '--';
      const stop = xp.stop_loss?.price ?? '--';
      const tp = asArr(xp.target_prices)[0]?.price ?? '--';
      const conf = ra.confidence ?? rj.confidence ?? '--';
      return [`<strong>${esc(p.ticker || rj.ticker)}</strong>`, actionLabel(rj.action || p.action),
        entry === '--' ? '--' : num(entry), stop === '--' ? '--' : num(stop),
        tp === '--' ? '--' : num(tp), conf === '--' ? '--' : conf + '/10'];
    });
    html += sec('L3 · 战术计划', tbl(['标的', '操作', '入场', '止损', '止盈', '信心'], rows));
  }

  // L4 待执行
  const pending = asArr(d.pending_executions);
  if (pending.length) {
    const rows = pending.map(ex => {
      const rj = parseJSON(ex.result_json);
      const flags = [rj.committee_modified && '投委会调整', rj.auto_repaired && '自修正', rj.auto_adjusted && '已缩量']
        .filter(Boolean).map(f => `<span class="tag">${esc(f)}</span>`).join('');
      return [`<strong>${esc(ex.ticker)}</strong>${flags ? ' ' + flags : ''}`, actionLabel(ex.action || rj.action),
        (ex.shares || rj.shares || '--'), num(ex.target_price || rj.target_price), esc(rj.reasoning || '')];
    });
    html += sec('L4 · 待执行操作', tbl(['标的', '操作', '股数', '价格', '理由'], rows));
  }

  // 投委会结论
  const meta = d.committee_meta;
  const committee = asArr(d.committee);
  if (meta || committee.length) {
    let inner = meta ? kv([
      ['标的', meta.ticker],
      ['结论', voteBadge(meta.verdict, verdictLabel)],
      ['通过率', meta.approval_rate != null ? Math.round(meta.approval_rate) + '%' : null],
      ['时间', fmtDate(meta.created_at)],
    ]) : '';
    if (committee.length) {
      inner += tbl(['委员', '投票', '信心', '要点'], committee.map(r => {
        const rj = parseJSON(r.result_json);
        return [esc(r.member_name || roleLabel(r.member_role) || '--'), voteBadge(rj.vote),
          rj.confidence != null ? rj.confidence + '/10' : '--', esc((rj.overall_assessment || '').slice(0, 160))];
      }));
    }
    if (meta && meta.summary) inner += para(meta.summary);
    html += sec('投委会审议结论', inner);
  }

  // 催化剂
  const cats = asArr(d.upcoming_catalysts);
  if (cats.length) {
    html += sec('近期催化剂', tbl(['日期', '标的', '类型', '事件'],
      cats.slice(0, 20).map(c => [esc((c.expected_date || c.date || '').slice(0, 10)), esc(c.ticker || ''),
        esc(c.event_type || c.catalyst_type || ''), esc(c.description || '')])));
  }

  // 持仓明细
  if (positions.length) {
    html += sec('当前持仓', tbl(['标的', '股数', '成本', '现价', '市值', '浮盈亏'],
      positions.map(p => [`<strong>${esc(p.ticker)}</strong>`, (p.shares ?? '--'),
        num(p.avg_cost ?? p.cost_basis), num(p.current_price ?? p.last_price),
        num(p.market_value), num(p.unrealized_pnl ?? p.pnl)])));
  }

  return html;
}
