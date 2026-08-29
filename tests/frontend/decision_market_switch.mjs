/**
 * decision_market_switch.mjs — 决策中心市场切换隔离自检
 *
 * 在 Node 下 stub window/document/fetch，import decision.js 的 __test__ 句柄，
 * 验证四个核心不变量（对应计划 Step 6 A/B/C/D）：
 *   A 纪元守卫：loadOverview 期间市场切走 → 旧响应被丢弃（overview 不被污染）
 *   A+ 正常路径：未切走 → 响应正常落地
 *   B resetConsultContext：拆除后 log/snapshot 清空、分割线基准归零
 *   C abortConsultStream：中断在途流 + 复位 streaming
 *   D switchMarket：纪元自增 + 市场跟随 + 在途流被 abort
 *
 * 运行：node tests/frontend/decision_market_switch.mjs
 */

let failures = 0;
function assert(cond, msg) {
  if (cond) { console.log(`  ✓ ${msg}`); }
  else { console.error(`  ✗ ${msg}`); failures++; }
}

// ── 全局 stub（import decision.js 前必须就位：wizard-state.js 顶层写 window.*）──
globalThis.window = {};

// 可配置的 DOM stub：默认 getElementById 返回可写假元素；测试可临时替换。
function makeEl() {
  return { innerHTML: '', textContent: '', style: {}, className: '', title: '',
           disabled: false, value: '', appendChild() {}, querySelector() { return null; },
           querySelectorAll() { return []; }, addEventListener() {}, closest() { return null; },
           scrollHeight: 0, scrollTop: 0 };
}
let _getById = () => null;   // 默认返回 null → 各 render 函数 if(!el) return 早退，不触碰 DOM
globalThis.document = {
  getElementById: (id) => _getById(id),
  querySelectorAll: () => [],
  querySelector: () => null,
  createElement: () => makeEl(),
  addEventListener() {},
};

// 可配置 fetch stub。
let _fetchImpl = async () => ({ ok: true, json: async () => ({}) });
globalThis.fetch = (...a) => _fetchImpl(...a);

const { __test__ } = await import('../../bottleneck_hunter/web/static/js/decision.js');
const S = __test__;

// ── 测试 A：纪元守卫丢弃过期响应 ─────────────────────────────
async function testEpochGuardDiscards() {
  console.log('A. loadOverview 纪元守卫：切换后旧响应被丢弃');
  S.dcState.market = 'us_stock';
  S.dcState.marketEpoch = 0;
  S.dcState.overview = null;
  S.dcState.overviewLoading = false;

  let release;
  const gate = new Promise(r => { release = r; });
  _fetchImpl = async () => { await gate; return { ok: true, json: async () => ({ tag: 'US_DATA' }) }; };

  const p = S.loadOverview();          // 发起加载（await 卡在 gate）
  S.dcState.marketEpoch += 1;          // 模拟市场切走
  release();                           // 旧市场响应此刻才回来
  await p;

  assert(S.dcState.overview === null, '过期响应被丢弃，overview 未被旧市场数据污染');
  // 过期分支不复位 overviewLoading（留给接管的新加载管理）：此处无新加载，故应仍为 true
  assert(S.dcState.overviewLoading === true, '过期响应未误复位 overviewLoading（纪元不匹配跳过复位）');
}

// ── 测试 A2：loadOverview 不碰跑批互斥锁 dcState.loading ──────
async function testLoadOverviewDoesNotTouchBatchMutex() {
  console.log('A2. loadOverview 与跑批互斥锁 dcState.loading 解耦（防并发重复跑批）');
  _getById = () => null;
  S.dcState.market = 'us_stock';
  S.dcState.marketEpoch = 0;
  S.dcState.loading = true;            // 模拟跑批进行中（runDaily 持锁）
  S.dcState.overviewLoading = false;
  _fetchImpl = async () => ({ ok: true, json: async () => ({ tag: 'OV' }) });

  await S.loadOverview();              // 切市场触发的 /overview 归来

  assert(S.dcState.loading === true, 'loadOverview 完成后跑批互斥锁 loading 仍为 true（未被提前解锁）');
  assert(S.dcState.overviewLoading === false, 'overviewLoading 正常复位');
  S.dcState.loading = false;           // 清理
}

// ── 测试 A+：正常路径响应落地 ────────────────────────────────
async function testEpochGuardPasses() {
  console.log('A+. loadOverview 正常路径：未切换则响应落地');
  S.dcState.market = 'us_stock';
  S.dcState.marketEpoch = 5;
  S.dcState.overview = null;
  S.dcState.loading = false;
  _getById = () => null;               // render 全部早退，仅验证 overview 落地
  _fetchImpl = async () => ({ ok: true, json: async () => ({ tag: 'FRESH' }) });

  await S.loadOverview();
  assert(S.dcState.overview && S.dcState.overview.tag === 'FRESH', '未切换时响应正常写入 overview');
  assert(S.dcState.loading === false, '正常路径 loading 复位');
}

// ── 测试 B：resetConsultContext 拆除上下文 ───────────────────
async function testResetConsultContext() {
  console.log('B. resetConsultContext：拆除分析师上下文');
  const log = makeEl(); log.innerHTML = '<div>旧美股对话</div>';
  const snap = makeEl(); snap.innerHTML = '<div>旧快照</div>';
  const mk = makeEl(); mk.textContent = '· 美股';
  const rpt = makeEl(); rpt.textContent = '已上传研报';
  const focus = makeEl(); focus.innerHTML = '<option value="AAPL">AAPL（美股旧票）</option>';
  _getById = (id) => ({
    'dc-consult-log': log, 'dc-consult-snapshot': snap,
    'dc-consult-market': mk, 'dc-consult-report-status': rpt,
    'dc-consult-focus': focus,
  }[id] || null);

  S.dcConsult.bubbles = { 'macro_market-0': {} };
  S.dcConsult.lastMsgTs = 123456;
  S.dcConsult.lastSnapTs = '2026-08-01';

  S.resetConsultContext();

  assert(log.innerHTML === '', '对话日志 DOM 清空');
  assert(snap.innerHTML === '', '快照 DOM 清空');
  assert(mk.textContent === '', '市场标签清空');
  assert(rpt.textContent === '', '研报状态清空');
  assert(Object.keys(S.dcConsult.bubbles).length === 0, '气泡索引清空');
  assert(S.dcConsult.lastMsgTs === 0, '分割线基准 lastMsgTs 归零');
  assert(S.dcConsult.lastSnapTs === '', '快照基准 lastSnapTs 归零');
  assert(!focus.innerHTML.includes('AAPL') && focus.innerHTML.includes('不聚焦'), '聚焦个股下拉复位为占位（清除旧市场票）');
}

// ── 测试 C：abortConsultStream 中断在途流 ────────────────────
async function testAbortConsultStream() {
  console.log('C. abortConsultStream：中断在途流 + 复位 streaming');
  _getById = () => null;               // setConsultSending 各元素 null → 安全早退
  let aborted = false;
  S.dcConsult.abort = { abort() { aborted = true; } };
  S.dcConsult.streaming = true;

  S.abortConsultStream();

  assert(aborted === true, '在途流 AbortController.abort() 被调用');
  assert(S.dcConsult.abort === null, 'abort 句柄已清空');
  assert(S.dcConsult.streaming === false, 'streaming 复位为 false');
}

// ── 测试 D：switchMarket 编排 ────────────────────────────────
async function testSwitchMarket() {
  console.log('D. switchMarket：纪元自增 + 市场跟随 + 在途流被 abort');
  _getById = () => null;               // 抽屉/各元素 null → 相关操作安全早退
  _fetchImpl = async () => ({ ok: true, json: async () => ({ tag: 'CN_DATA' }) });

  S.dcState.market = 'us_stock';
  S.dcState.marketEpoch = 10;
  S.dcState.overview = { tag: 'OLD_US' };
  S.dcConsult.open = false;
  let aborted = false;
  S.dcConsult.abort = { abort() { aborted = true; } };
  S.dcConsult.streaming = true;

  await S.switchMarket('a_stock');

  assert(S.dcState.market === 'a_stock', '市场切换为 a_stock');
  assert(S.dcState.marketEpoch === 11, '纪元自增（10→11）');
  assert(aborted === true, '切换时在途宏观咨询流被 abort');
  assert(S.dcConsult.streaming === false, '切换后 streaming 复位');
  assert(S.dcState.overview && S.dcState.overview.tag === 'CN_DATA', '决策层已按新市场重载');
}

// ── 测试 E：openConsultDrawer 历史渲染纪元守卫 ───────────────
async function testConsultHistoryEpochGuard() {
  console.log('E. openConsultDrawer：切走后旧市场历史不渲染');
  // 真实覆盖：生产渲染走 renderConsultLog→appendChild，且渲染快照后会写 dcConsult.lastSnapTs。
  // 守卫生效时应在 fetch 后整体 bail → appendChild 不被调用、lastSnapTs 保持空。
  let appendCount = 0;
  const trackedLog = { ...makeEl(), appendChild() { appendCount++; } };
  const drawer = makeEl();
  const snap = makeEl();
  _getById = (id) => ({
    'dc-consult-drawer': drawer, 'dc-consult-log': trackedLog,
    'dc-consult-snapshot': snap, 'dc-consult-market': makeEl(),
    'dc-consult-report-status': makeEl(),
  }[id] || null);

  // 含 snapshot 的历史：renderConsultLog 会 appendChild，openConsultDrawer 会写 lastSnapTs
  const histResp = { ok: true, json: async () => ({
    session: { transcript_json: [
      { type: 'snapshot', ts: '2026-08-01T00:00:00', indices: {}, sentiment: {}, macro: {}, sectors: {}, news: [] },
      { type: 'analyst', round: 0, ts: '2026-08-01T00:01:00', content: 'HISTORY-US' },
    ] },
    stale: true,   // stale=true 避免走 _todayHasOpening 早退，确保守卫是唯一拦截点
  }) };

  // 切走场景：fetch 归来前市场切走 → 守卫应阻止渲染
  S.dcState.market = 'us_stock';
  S.dcState.marketEpoch = 20;
  S.dcConsult.open = false;
  S.dcConsult.lastSnapTs = '';
  appendCount = 0;
  let release;
  const gate = new Promise(r => { release = r; });
  _fetchImpl = async () => { await gate; return histResp; };

  const p = S.openConsultDrawer();
  S.dcState.marketEpoch += 1;   // 打开途中市场切走
  release();
  await p;

  assert(appendCount === 0, '市场切走后旧市场历史未 appendChild 进日志');
  assert(S.dcConsult.lastSnapTs === '', '市场切走后未写入旧市场快照基准 lastSnapTs');

  // 控制组：不切走时同样的数据必须真实渲染（证明上面的断言非因数据/桩失效而恒真）
  S.dcState.marketEpoch = 30;
  S.dcConsult.open = false;
  S.dcConsult.lastSnapTs = '';
  appendCount = 0;
  _fetchImpl = async () => histResp;   // 立即返回，不切走

  await S.openConsultDrawer();

  assert(appendCount > 0, '控制组：未切走时历史真实 appendChild 渲染（证明守卫断言有效非恒真）');
  assert(S.dcConsult.lastSnapTs === '2026-08-01T00:00:00', '控制组：未切走时快照基准 lastSnapTs 被写入');
}

// ── 测试 F：分割线按市场着色 ───────────────────────────────
async function testDividerMarketColor() {
  console.log('F. 时效分割线按市场着色（美股 dc-divider-us / A股 dc-divider-cn）');
  const snap = { ts: '2026-08-01T00:00:00', indices: {}, sentiment: {}, macro: {}, strategy: {} };

  S.dcState.market = 'us_stock';
  const usDiv = S._consultDividerEl(snap);
  assert(usDiv.className.includes('dc-consult-divider'), '美股：保留基础分割线类');
  assert(usDiv.className.includes('dc-divider-us'), '美股：带 dc-divider-us（蓝底红边）类');
  assert(!usDiv.className.includes('dc-divider-cn'), '美股：不带 A股类');

  S.dcState.market = 'a_stock';
  const cnDiv = S._consultDividerEl(snap);
  assert(cnDiv.className.includes('dc-divider-cn'), 'A股：带 dc-divider-cn（红底黄边）类');
  assert(!cnDiv.className.includes('dc-divider-us'), 'A股：不带美股类');
}

console.log('=== 决策中心市场切换隔离自检 ===');
await testEpochGuardDiscards();
await testLoadOverviewDoesNotTouchBatchMutex();
await testEpochGuardPasses();
await testResetConsultContext();
await testAbortConsultStream();
await testSwitchMarket();
await testConsultHistoryEpochGuard();
await testDividerMarketColor();

console.log('');
if (failures === 0) { console.log('✅ 全部通过'); process.exit(0); }
else { console.error(`❌ ${failures} 项失败`); process.exit(1); }
