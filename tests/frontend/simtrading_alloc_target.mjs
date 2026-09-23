/**
 * simtrading_alloc_target.mjs — 目标 vs 实际对照条（P2-2）自检
 *
 * 在 Node 下 stub window/document/fetch，import simtrading.js 的 __test__ 句柄，
 * 验证对照条的核心不变量：
 *   A 无 L2 目标（取不到 / 无 target_allocation）→ 整块隐藏，不误导
 *   B 目标存在 → 渲染权益+现金两行，实际值按 持仓/总额 计算
 *   C 超带（>5pct）判超配/低配且着色；带内判"在带内"
 *
 * 运行：node tests/frontend/simtrading_alloc_target.mjs
 */

let failures = 0;
function assert(cond, msg) {
  if (cond) { console.log(`  ✓ ${msg}`); }
  else { console.error(`  ✗ ${msg}`); failures++; }
}

globalThis.window = { addEventListener() {} };

function makeEl() {
  const el = { _html: '', style: {}, className: '', title: '', disabled: false, value: '',
    appendChild() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, closest() { return null; }, scrollHeight: 0, scrollTop: 0 };
  Object.defineProperty(el, 'innerHTML', { get() { return this._html; }, set(v) { this._html = v; }, enumerable: true });
  Object.defineProperty(el, 'textContent', {
    get() { return this._html; },
    set(v) { this._html = String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    enumerable: true });
  return el;
}

const els = { 'st-alloc-target': makeEl() };
globalThis.document = {
  getElementById: (id) => els[id] || null,
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => makeEl(),
  addEventListener() {},
};

let _fetchImpl = async () => ({ ok: true, json: async () => ({}) });
globalThis.fetch = (...a) => _fetchImpl(...a);

const { __test__ } = await import('../../bottleneck_hunter/web/static/js/simtrading.js');
const { allocRow, renderAllocTarget, stState } = __test__;

const el = els['st-alloc-target'];
const planResp = ta => async () => ({ ok: true, json: async () => ({ plan: { result_json: { target_allocation: ta, overall_stance: '积极' } } }) });

console.log('A 无 L2 目标 → 隐藏');
_fetchImpl = async () => ({ ok: false, status: 500, statusText: 'err' });
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], 90000);
assert(el.style.display === 'none', 'API 不可达时隐藏（不显示假对照）');

_fetchImpl = async () => ({ ok: true, json: async () => ({ plan: null }) });
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], 90000);
assert(el.style.display === 'none', '无 plan（尚未生成 L2）时隐藏');

_fetchImpl = planResp({});
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], 90000);
assert(el.style.display === 'none', '有 plan 但无 target_allocation（旧格式）时隐藏');

console.log('B 目标存在 → 权益/现金两行，实际按 持仓/总额 算');
_fetchImpl = planResp({ equity_pct: 60, cash_pct: 40 });
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], 90000);
assert(el.style.display === '', '有目标时显示');
assert(el.innerHTML.includes('权益') && el.innerHTML.includes('现金'), '含权益与现金两行');
assert(el.innerHTML.includes('实际 10.0% / 目标 60.0%'), '实际权益 = 10000/(10000+90000) = 10.0%');
assert(el.innerHTML.includes('实际 90.0% / 目标 40.0%'), '实际现金 = 90.0%');
assert(el.innerHTML.includes('低配 50.0pct'), '权益低配 50pct 被点名');
assert(el.innerHTML.includes('超配 50.0pct'), '现金超配 50pct 被点名');

console.log('C 带内（≤5pct）判"在带内"，不误报');
_fetchImpl = planResp({ equity_pct: 60, cash_pct: 40 });
await renderAllocTarget([{ ticker: 'NVDA', market_value: 57000 }], 43000);
assert(el.innerHTML.includes('在带内'), '偏离 3pct → 在带内');
assert(!el.innerHTML.includes('低配') && !el.innerHTML.includes('超配'), '带内不出超配/低配标签');

console.log('D allocRow 边界：极端权重被钳进 0-100，不出负宽条');
const row = allocRow('权益', 120, -20);
assert(row.includes('width:100%'), '实际 >100 钳到 100%');
assert(row.includes('left:0%'), '目标 <0 钳到 0%');

console.log('E 超带无论超配/低配都标红（低配不能显示成"好"的绿色）');
assert(allocRow('权益', 10, 60).includes('st-pnl-neg'), '权益低配 → 红');
assert(allocRow('现金', 90, 40).includes('st-pnl-neg'), '现金超配 → 红');
assert(allocRow('权益', 58, 60).includes('st-pnl-zero'), '带内 → 中性');

console.log('F 现金取不到 / 期间切了市场 → 不渲染假对照');
_fetchImpl = planResp({ equity_pct: 60, cash_pct: 40 });
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], null);
assert(el.style.display === 'none', '现金未知（null）时隐藏，不把持仓当 100% 权益');

el.style.display = 'sentinel';
stState.market = 'a_stock';
await renderAllocTarget([{ ticker: 'NVDA', market_value: 10000 }], 90000, 'us_stock');
assert(el.style.display === 'sentinel', '发起时 us_stock、返回时已切 a_stock → 丢弃，不覆盖');
let seenUrl = '';
_fetchImpl = async (url) => { seenUrl = url; return { ok: true, json: async () => ({ plan: null }) }; };
await renderAllocTarget([], 1, 'a_stock');
assert(seenUrl.includes('market=a_stock'), '目标按发起时的市场拉取');

console.log(failures === 0 ? '\n全部通过' : `\n${failures} 项失败`);
process.exit(failures === 0 ? 0 : 1);
