# 决策中心双市场上下文隔离与切换同步 — 实施计划

> 日期：2026-08-24 ｜ 范围：`bottleneck_hunter/web/static/js/decision.js`（纯前端编排）
> 目标：美股 / A股 切换时，决策层 L1-L4 与「AI分析师互动咨询」的历史上下文完全独立、跟随市场即时同步切换，杜绝跨市场信息混淆与串台。

## 1. 背景与诊断结论

用户反馈三症状：
1. 切换市场后各**决策层信息停留在原市场**。
2. **AI分析师页面不自动同步**切换，历史对话与自动生成的**日期分割线仍按原（美股）市场**。
3. 期望：两个市场的历史咨询信息**完全分开保存为两套独立上下文**，随市场切换同步切换。

**关键诊断（已代码级核实）：后端早已按市场隔离，两套独立上下文其实已存在。**
- AI分析师历史存于 `meeting_records` 表，带 `market` 列；`_load_session(store, market)` 按市场过滤读取、保存亦带 `market`（`macro_consultation.py:70-72`、`:631`）。美股 / A股 = 两行独立 session，互不覆盖。
- 决策层 L1-L4 全部走 `store.for_market(market)`，所有 API 端点均接收 `?market=`。

**故三症状同源，100% 是前端切换编排缺陷**，无需改后端 / 数据模型。

## 2. 根因（三条，同源）

切换处理器 `decision.js:1101-1106` 现状：
```js
dcState.market = e.target.value;
dcState.overview = null;
closeConsultDrawer();   // 仅 display:none
loadOverview();
```

| # | 症状 | 根因 |
|---|------|------|
| R1 | 决策层停留原市场 / 偶发错乱 | `loadOverview` 开头 `if(dcState.loading) return` 会**丢弃**切换时的重载；且响应无版本保护，慢的旧市场响应可能覆盖新市场（竞态） |
| R2 | 分析师不同步、分割线仍旧市场 | 切换仅隐藏抽屉，`dcConsult` 的市场标签 / 分割线基准 `lastMsgTs` / 快照基准 `lastSnapTs` / 气泡 `bubbles` / 日志 DOM / 研报状态**全部残留旧市场**；抽屉不随切换重载 |
| R3 | 跨市场串台 | `consultStream` 用 fetch+reader **无中断能力**（`decision.js:1583`），切换时在途 SSE 继续写共享 `dcConsult.bubbles` → 旧市场分析师回答写进新市场视图 |

## 3. 设计原则

- **后端是两套上下文的权威存储**，前端不再另存两份缓存（YAGNI）。切换 = ①作废在途请求 → ②彻底拆除分析师视图 → ③按新市场从后端重载。
- **单一编排入口** `switchMarket()`，取代内联 change 处理，切换语义集中一处、可测试。
- **版本纪元（epoch）守卫**消灭异步竞态：所有受市场影响的加载在 `await` 归来后校验 epoch，过期即丢弃。
- 全部改动集中在 `decision.js`，`index.html` 不动，后端不动。

## 4. 实施步骤（每步后跑全量 pytest + JS 自检）

> **执行状态（2026-08-24）**：Step 1-6 全部完成 ✅ + 代码审查一轮修复完成 ✅；
> 全量 pytest 1493 passed/4 skipped（零回归）；JS 自检 25 项全绿（含快速连切历史守卫 Test E）。
> 代码审查见 §8。

### Step 1 — 状态与版本纪元基座
- `dcState` 增 `marketEpoch: 0`。
- `dcConsult` 增 `open: false`、`abort: null`（AbortController 句柄）。
- 加 `export const __test__ = { dcState, dcConsult, ... }`，暴露编排函数供 Node 自检（生产不引用，零副作用）。

### Step 2 — `loadOverview` 纪元守卫
- 进入时 `const epoch = dcState.marketEpoch;` 捕获。
- 删除硬 `if(dcState.loading) return`（改为不阻断切换重载）。
- `await` 归来后 `if (epoch !== dcState.marketEpoch) return;` 丢弃过期响应，再 `renderAll`。
- `finally` 中仅当 epoch 未变才复位 `loading`。

### Step 3 — `consultStream` 可中断 + `abortConsultStream()`
- `consultStream` 内 `new AbortController()`，挂到 `dcConsult.abort`，fetch 传 `signal`。
- 新 `abortConsultStream()`：`abort()` 在途流、置 `streaming=false`、清 `abort` 句柄；`AbortError` 静默不报错。

### Step 4 — `resetConsultContext()` 上下文拆除
- 清 `#dc-consult-log`、`#dc-consult-snapshot` DOM。
- `bubbles={}`、`lastMsgTs=0`、`lastSnapTs=''`。
- 复位市场标签 `#dc-consult-market`、研报状态栏 `#dc-consult-report-status`。

### Step 5 — `switchMarket()` 编排器 + 换绑事件
- 新 `switchMarket(newMarket)`：改 `dcState.market` → `marketEpoch++` → `overview=null` → `abortConsultStream()` → `resetConsultContext()` → `loadOverview()`；若 `dcConsult.open` 则 `openConsultDrawer()` 跟随重载，否则 `closeConsultDrawer()`。
- `openConsultDrawer`/`closeConsultDrawer` 维护 `dcConsult.open`。
- change 监听改为调 `switchMarket(e.target.value)`。

### Step 6 — 自检脚本落地
- `tests/frontend/decision_market_switch.mjs`：Node 下 stub `global.window/document/fetch`，import `decision.js` 的 `__test__`，断言：
  - **A**：`loadOverview` 期间 `marketEpoch` 自增后，旧响应被丢弃（不 render）。
  - **B**：`resetConsultContext()` 后 log/snapshot 清空、`lastMsgTs=0`、`lastSnapTs=''`。
  - **C**：`abortConsultStream()` 令 `streaming=false` 且中断信号触发。
  - **D**：`switchMarket()` 使 epoch 自增、`dcConsult.market` 跟随、在途流被 abort。

## 5. 验收

1. **全量 pytest**（1497 项）每步后运行，证明零后端回归。
2. **JS 自检** `node tests/frontend/decision_market_switch.mjs` 全绿。
3. **代码审查工具**：对最终 diff 跑 code-review，修复确认项。
4. **手动端到端**（口头验收清单）：美股开分析师问一句 → 切 A股 → 分析师即时重载为 A股快照/历史、无美股分割线；L1-L4 即时变 A股；切换中途发消息不串台。

## 6. 刻意不做（YAGNI，留天花板标注）

- 子加载器（blocked/resting/risk/meetings/ratings/stats/style）在切换后已以正确市场重新发起；仅「极快连续切换」下某子加载器迟到响应可能残留。上升路径：给这几个也套同一 epoch 守卫。当前按 YAGNI 不做，在代码留 `ponytail:` 标注。
- 不引入前端市场级缓存层（后端已是权威两套上下文）。

## 7. 风险与回滚

- 改动集中单文件、无 schema / API 变更，回滚 = revert 单 commit。
- 主要风险为竞态守卫遗漏 → 由 epoch 自检 A/D 覆盖。

## 8. 代码审查一轮（2026-08-24）

对最终 diff 跑 code-review，发现并修复以下真问题：

- **A2（中危回归，自引入）**：Step 2 直接删掉 `loadOverview` 开头的 `if(dcState.loading) return`，破坏了「跑批互斥锁」——`runDaily/runFullRefresh/scanCatalysts` 靠 `dcState.loading` 防重复跑批，而切换市场时 `switchMarket` 会 `await loadOverview()`，其 `finally` 会把 `loading` 提前解锁 → 跑批途中切市场可触发并发重复跑批（浪费成本 / 重复写库）。
  **修复**：给 `loadOverview` 独立 `overviewLoading` 标志，全程不碰 `loading`；`loading` 仅由三个跑批函数读写。新增自检 **A2** 断言 loadOverview 完成后 `loading` 仍为 true。
- **测试恒真项（两处）**：Test E 的 `renderedInto` 只在 innerHTML 含 'HISTORY' 时触发，而生产走 appendChild → 删守卫仍绿（零覆盖）；Test A 第二断言 `loading===false||loading===true` 恒真。
  **修复**：Test E 改为跟踪 appendChild + lastSnapTs 并加对照组；Test A 改断言 `overviewLoading===true`。均经变异测试确认（删守卫 → 对应断言失败）。
- **D（次要）**：`#dc-consult-focus` 聚焦个股下拉切换后残留旧市场票。**修复**：`resetConsultContext` 复位下拉为占位；新增 Test B 断言并经变异测试确认真实覆盖。

审查其余项（B/C/E/F）经复核为无问题。全部修复后再跑全量 pytest：1493 passed / 4 skipped，零回归。
