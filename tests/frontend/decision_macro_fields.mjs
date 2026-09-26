/**
 * decision_macro_fields.mjs — L1 宏观面板取数口径自检
 *
 * 病根：`renderMacro` 读的是 `trend_assessment` / `key_risks` / `position_suggestion` /
 * `risk_level` —— L1 契约（chain/prompts/decision_macro.md）里**一个都没有**，
 * 真实契约字段是 regime / regime_confidence / risk_appetite / key_signals / risk_factors /
 * recommended_cash_pct / sector_rotation。更坏的是末尾 `.filter(([,v]) => v)` 把取空的字段
 * 静默滤掉：面板不报错、不空白，只剩「市场总结」一行——**看着像正常渲染，其实什么都没显示**。
 *
 * 本自检把「面板必须显示契约里的哪些内容」钉死：用真实形态的 fixture（不是理想态）驱动
 * renderMacro，断言渲染结果里出现每个关键信息。若有人再把字段名改回幻影键，filter 会把
 * 对应行滤掉 → 断言立即失败。
 *
 * 运行：node tests/frontend/decision_macro_fields.mjs
 */

let failures = 0;
function assert(cond, msg) {
  if (cond) { console.log(`  ✓ ${msg}`); }
  else { console.error(`  ✗ ${msg}`); failures++; }
}

// ── 全局 stub（import decision.js 前必须就位）──
globalThis.window = {};

function makeEl() {
  const el = { _html: '', style: {}, className: '', title: '',
    disabled: false, value: '', appendChild() {}, querySelector() { return null; },
    querySelectorAll() { return []; }, addEventListener() {}, closest() { return null; },
    scrollHeight: 0, scrollTop: 0 };
  Object.defineProperty(el, 'innerHTML', { get() { return this._html; }, set(v) { this._html = v; }, enumerable: true });
  Object.defineProperty(el, 'textContent', {
    get() { return this._html; },
    set(v) { this._html = String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); },
    enumerable: true });
  return el;
}

const _els = { 'dc-macro-body': makeEl(), 'dc-macro-risk': makeEl() };
globalThis.document = {
  getElementById: (id) => _els[id] || null,
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => makeEl(),
  addEventListener() {},
};

const { __test__ } = await import('../../bottleneck_hunter/web/static/js/decision.js');

// ── 真实形态的 L1 fixture（照 decision_macro.md 契约逐字段构造）──
const MACRO = {
  created_at: '2026-09-20T02:00:00+00:00',
  updated_at: '2026-09-26T02:00:00+00:00',
  status: 'valid',
  result_json: {
    regime: 'bull',
    regime_confidence: 7,
    risk_appetite: 'aggressive',
    recommended_cash_pct: 20,
    market_summary: '流动性宽松叠加AI资本开支持续，市场处于上行趋势。',
    key_signals: [
      { name: 'VIX', value: '14.2', interpretation: 'bullish' },
      { name: '10年期美债', value: '3.85%', interpretation: 'neutral' },
    ],
    risk_factors: ['估值处于历史高位', '地缘冲突升级'],
    sector_rotation: { strengthening: ['科技', '工业'], weakening: ['公用事业'], neutral: [] },
    strategy_text: '完整策略阐述……',
    valid_until_trigger: 'VIX 突破 30',
  },
};

function renderWith(rj) {
  _els['dc-macro-body'].innerHTML = '';
  _els['dc-macro-risk'].textContent = '';
  __test__.renderMacro({ ...MACRO, result_json: rj });
  return { html: _els['dc-macro-body'].innerHTML, badge: _els['dc-macro-risk'].textContent };
}

// ── 主用例：契约字段必须逐个出现在面板里 ──
function testContractFieldsAreRendered() {
  console.log('1. L1 契约字段逐个落到面板（幻影键回归即失败）');
  const { html, badge } = renderWith(MACRO.result_json);

  assert(html.includes('流动性宽松'), '市场总结（market_summary）显示');
  assert(html.includes('牛市') && html.includes('7/10'), '市场状态显示 regime + 置信度（regime/regime_confidence）');
  assert(html.includes('进取'), '风险偏好显示中文档位（risk_appetite）');
  assert(html.includes('VIX') && html.includes('偏多'), '关键信号显示 name：value（解读）');
  assert(html.includes('偏空') === false || html.includes('中性'), '关键信号解读做了中文映射');
  assert(html.includes('估值处于历史高位'), '风险因素逐条显示（risk_factors）');
  assert(html.includes('80%') && html.includes('20%'), '建议权益仓位由 recommended_cash_pct 反推（100-20）');
  assert(html.includes('科技'), '板块轮动显示走强板块（sector_rotation）');
  assert(badge === '进取', `徽章显示风险偏好而非恒 '--'（实际 "${badge}"）`);
}

// ── 回归守卫：曾经的幻影键即使被填上也不该再被读 ──
function testPhantomKeysAreNotTheSource() {
  console.log('2. 幻影键不再是取数来源（沿用旧字段名的数据也显示不出东西）');
  const { html } = renderWith({
    // 只有幻影键、没有真契约字段 → 面板除市场总结外应为空
    market_summary: '只有总结',
    trend_assessment: '幻影趋势',
    key_risks: ['幻影风险'],
    position_suggestion: '幻影仓位',
    risk_level: '高',
  });

  assert(html.includes('只有总结'), '市场总结仍显示（唯一本就正确的字段）');
  assert(!html.includes('幻影趋势'), 'trend_assessment 不再被读取');
  assert(!html.includes('幻影风险'), 'key_risks 不再被读取');
  assert(!html.includes('幻影仓位'), 'position_suggestion 不再被读取');
}

// ── 容错：历史/异常形态不得让面板崩掉 ──
function testDegradedShapes() {
  console.log('3. 异常形态容错（历史数据里 key_signals 出现过单字符串 / 数字）');
  const { html } = renderWith({
    market_summary: '总结',
    key_signals: 'VIX 偏低',
    risk_factors: '单一风险字符串',
    recommended_cash_pct: 0,
    regime: 'sideways',
  });

  assert(html.includes('VIX 偏低'), 'key_signals 为字符串时不崩且照显');
  assert(html.includes('单一风险字符串'), 'risk_factors 为字符串时不崩且照显');
  assert(html.includes('100%'), 'recommended_cash_pct=0 → 权益 100%（0 不是"缺失"，不该被丢）');
  assert(html.includes('震荡'), 'regime 有值但没有置信度时只显示 regime 名');
}

// ── 空策略：显示引导语而不是空白 ──
function testNullMacroShowsHint() {
  console.log('4. 无宏观策略时显示引导语');
  _els['dc-macro-body'].innerHTML = '';
  __test__.renderMacro(null);
  assert(_els['dc-macro-body'].innerHTML.includes('尚未生成宏观策略'), '空 macro → 引导语');
}

testContractFieldsAreRendered();
testPhantomKeysAreNotTheSource();
testDegradedShapes();
testNullMacroShowsHint();

console.log('');
if (failures === 0) { console.log('✅ 全部通过'); process.exit(0); }
else { console.error(`❌ ${failures} 项失败`); process.exit(1); }
