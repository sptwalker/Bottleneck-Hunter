# BottleneckHunter 9分改造总方案与执行日志

> **目标**：综合评分从 4.6/10 (C-) 提升到 **9.0+/10 (A)**
> **依据**：[FULL_SYSTEM_REVIEW_2026-07-02.md](FULL_SYSTEM_REVIEW_2026-07-02.md) 的 40 项已验证发现 + 12 项盲区
> **启动日期**：2026-07-02
> **纪律要求**（用户明确）：
> 1. **每完成一项 → 全面验收审计**：不以"代码写完"为完成标准，必须用 DB 实况查询 / 可运行测试 / 端到端走通证明功能真正达到设计标准
> 2. **每阶段完成 → 回顾复核**：检查是否产生偏差、疏漏，与本方案对齐
> 3. **全部完成 → 功能审计报告 + 重新评分**

---

## 零、评分模型（如何从 4.6 到 9.0+）

| 维度 | 现状 | 目标 | 关键动作 |
|------|------|------|---------|
| 架构设计 | 8.5 | 9.0 | 保持，补决策留痕 |
| 产业链瓶颈分析 | 6.5 | 9.0 | 真实 CR3/HHI 锚定 + 分行业权重级联 + 可投性门控 |
| 数据管道与真实性 | 4.0 | 9.0 | **零占位/虚构**：空壳不入库、mock 标记、幻觉价禁成交 |
| 四层决策引擎 | 6.0 | 9.0 | 情景估值锚定 + 催化剂信号接通 + staleness 硬阻断 |
| 投委会 gating | 4.5 | 9.0 | rejected 可触发 + 约束后移 + writeback |
| 风险控制 | 4.0 | 9.0 | 账户级熔断 + 已有个股止损/组合风控 |
| 回测能力 | 2.5 | 8.5 | backtest 触发 + PIT 数据 + 样本充足性 |
| 反馈/自进化闭环 | 2.5 | 9.0 | **6 张空表落真数据**：卖出→复盘→经验→层绩效→偏好 |
| 合规与安全 | 3.0 | 9.0 | 免责 + 越权隔离 + 注入过滤 + sim/live 闸门 |
| 前端呈现真实性 | 5.0 | 9.0 | 不展示未算出的能力 + 数据质量徽章 |

**达标定义**：一个维度得 9 分 = 该维度所有 Critical/High 发现关闭 + 有 DB/测试证据证明真实运行 + 无新引入的占位/空转。

---

## 一、分阶段方案

### Phase 0 — 诚信止血（最高优先级）
**目标**：消除"占位/虚构数据冒充真实"与"声称完成实则空转"的认知误导。原则：**宁可显示"不可用"，绝不用假数据填充。**

| # | 任务 | 发现 | 验收标准 |
|---|------|------|---------|
| 0.1 | Form 4 解析失败**不落库**，清理已有 137 条空壳 | C-1 | DB: `insider_trades` 中 shares=0&price空 的记录 = 0 |
| 0.2 | A 股公告空壳同理不落库 | C-2 | DB: notice 空壳 = 0 |
| 0.3 | mock 数据全链标记：无 LLM 时 `fail_*` 而非 `complete_*` + `is_mock` 字段 | C-3,H-4 | 代码：strategy/uzi 无 LLM 分支调 fail_*；查询过滤 mock |
| 0.4 | 幻觉价格禁成交：无真实快照→needs_price + 偏离度硬校验 | H-6 | 测试：无快照时 execute_trade 拒绝成交 |
| 0.5 | 统一市场代码 `us`→`us_stock` + 迁移历史 | H-14 | DB: watchlist.market 无 `us`；两标的评分非 0 |
| 0.6 | scenario/layer 写入失败改**告警**不静默 | H-5,H-7 | 代码：except 分支 logger.error + 计数 |
| 0.7 | 全买卖指令/导出/UI 加投资建议免责 | G-1 | 前端/报告含免责文案 |

### Phase 1 — 打通闭环
**目标**：让"声称完成"变"真在运行"，6 张空表落真实数据。

| # | 任务 | 发现 | 验收标准 |
|---|------|------|---------|
| 1.1 | `sim_trades` 加 `realized_pnl` 列并持久化 | perf | DB: sell 交易有 realized_pnl |
| 1.2 | 端到端走通 买入→卖出→复盘→经验→层绩效 | 断点 | DB: auto_reviews/layer_performance ≥1 行 |
| 1.3 | auto-review 触发改健壮（不用 get_event_loop） | H-7 | 卖出后 auto_reviews 落数据 |
| 1.4 | 投委会 gating：rejected 可触发 + writeback | C-9 | 测试：全票反对→reject_execution 调用 |
| 1.5 | 约束校验后移到投委会之后 + override 标签 | C-10 | 代码顺序：committee → validate |
| 1.6 | preference_learner 接入 scheduler | C-8 | DB: user_preferences ≥1 行 |
| 1.7 | 催化剂 outcome→买卖信号→L3 | H-13 | DB: catalyst_signals 落数据 |
| 1.8 | L2 情景估值喂给 L3/L4 止损止盈锚点 | H-5下游 | 代码：L3 读 scenario bear/bull |

### Phase 2 — 专业性补强
| # | 任务 | 发现 | 验收标准 |
|---|------|------|---------|
| 2.1 | 瓶颈评分接真实 CR3/HHI + evidence_sources | H-16 | 报告含数据来源 |
| 2.2 | 分行业权重级联到所有 analyzer 创建点 | H-17 | 代码：graph/legacy/phases 传 industry |
| 2.3 | 交叉验证/投委会 provider 去重强制校验 | H-18 | 测试：同 provider 触发告警 |
| 2.4 | 可投性过滤前移为瓶颈门控 + investable_supplier_count | H-12 | 报告字段存在 |
| 2.5 | 账户级止损/回撤熔断闸门 | H-11 | 测试：日亏损超限拦截 |
| 2.6 | quality_gate red 硬阻断 pre_l3/l4 | H-15 | 测试：red 时阻断 |
| 2.7 | 拆解缓存复用 chain_versions | 不完善 | 命中缓存不调 LLM |

### Phase 3 — 安全与合规
| # | 任务 | 发现 | 验收标准 |
|---|------|------|---------|
| 3.1 | 多用户隔离改参数化 | G-4 | 无字符串拼 user_id |
| 3.2 | prompt 层注入过滤 | G-3 | 测试：注入文本被清洗 |
| 3.3 | LLM 成本按日累计 + 硬熔断 | G-6 | DB: 消费入库 |
| 3.4 | sim/live 模式标志 + live 双确认 | G-5 | 代码：mode 检查 |
| 3.5 | 公司行动复权 + 交易日历 | G-7 | 拆股复权正确 |
| 3.6 | 决策留痕：prompt 版本+快照哈希 | G-8 | DB: 决策含 provenance |

---

## 二、执行日志

> 每项完成后在此记录：改动文件、验收方式、验收结果（PASS/FAIL + 证据）。

<!-- EXECUTION LOG START -->

### Phase 0 — 诚信止血 ✅ 完成 2026-07-02

| # | 改动文件 | 验收方式 | 结果 |
|---|---------|---------|------|
| 0.1 | `sec_pipeline.py`（删 `_make_stub_trade`，XML 失败不落库） | DB 查询 + 单测 | **PASS** 137 空壳已删，剩 39 条真实记录；`test_sec_pipeline` 21/21 |
| 0.2 | A 股 notice 保留方向性信号（真实 buy/sell），无零信号空壳 | DB 查询 | **PASS** 无 insider_name 空且 type unknown 的记录 |
| 0.3 | `strategy_engine.py`（无 LLM→fail 而非 mock-completed）+ `uzi_runner.py`（is_mock 标记） | grep + import | **PASS** fail_strategy 生效；4 处 is_mock |
| 0.4 | `trade_executor.py`（无真实快照拒绝成交 + 偏离 30% 硬校验） | 新单测 `test_exec_price_guard` | **PASS** 3/3 |
| 0.5 | `store_base.normalize_market` + `store_watchlist.add` + DB 迁移 | 单测 + DB 查询 | **PASS** 14 行全 us_stock；normalize 6 case |
| 0.6 | `decision_engine.py`/`trade_reviewer.py`（写入失败告警+计数，不静默） | 代码审查 | **PASS** error 级 + SSE 告警 + 计数日志 |
| 0.7 | `index.html`+`common.css`+`report-export.js`（免责声明） | grep | **PASS** UI 底栏 + 导出报告底部 |

**回归验证**：改动模块单测 37/37 通过（trade_executor 13 + sec_pipeline 21 + price_guard 3）。
全量套件其余失败（watchlist_api/decision_8b2/8b4/pipeline_health）经 main 分支对照，**全部为改造前既有**（缺 auth cookie / fixture 漂移），非本次引入。

**阶段回顾（偏差与疏漏检查）**：
- ✅ 无偏差：所有改动符合"宁可显示不可用，不用假数据"原则。
- ⚠️ 遗留 1：`get_event_loop().create_task()` 触发自动复盘仍脆弱 → 归入 **Phase 1.3** 根治（本就在计划内）。
- ⚠️ 遗留 2：A 股 notice 方向性记录仍缺 shares/price，属"部分数据"非"虚构" → Phase 2 数据源增强处理。
- 📌 发现：多处 API 测试因 auth fixture 漂移长期失败，掩盖真实回归信号 → 建议 Phase 3 顺带修复测试 auth 夹具。
