# BottleneckHunter 全系统专业性与合理性评审报告

> **评审视角**：资深金融分析师 + 投资系统架构师
> **评审日期**：2026-07-02
> **系统规模**：后端 Python ~34,766 行（100+ 模块），前端 JS/CSS/HTML，LLM 提示词 40 个，SQLite 4 库 50+ 表
> **评审方法**：11 个专项审计智能体并行精读真实源码 → 40 项 Critical/High 发现逐条对抗性验证 → 数据库实况交叉核对 → 审查主管补盲
> **证据基础**：91 条原始发现，验证后 40 条存活（1 条被驳回，50 条 Medium/Low），全部对照 2026-07-02 生产数据库实况

---

## 一、一句话结论

> **这是一套架构设计一流、但"声称已完成"与"实际在运行"之间存在系统性鸿沟的投资决策系统。** 决策链的前半段（产业链拆解 → 观察池 → L1-L4 分层判断 → 投委会评审）真实在跑；后半段（校验把关 → 模拟成交 → 归因复盘 → 学习回灌）大面积空转。更严重的是：多个关键数据维度是**占位/虚构数据冒充真实信号**，而前端与文档呈现出一个"看似风控在生效、系统在学习"的完整闭环——这构成对使用者的**认知误导**。

**核心量化证据**：文档 `DECISION_LOOP_IMPROVEMENT.md` 宣称 P0-P3 于 2026-06-30 "✅ 全部完成"，但其承诺产出的 **12 张核心表当前 0 行**：
`layer_performance`、`scenario_valuations`、`trade_feedback`、`auto_reviews`、`experience_cards`、`backtest_runs`、`tuning_log`、`user_preferences`、`thesis_evidence_log`、`earnings_reports`、`sim_fund_ops`、`ab_snapshots`。

---

## 二、综合评分

| 评估维度 | 评分 | 等级 | 相比 2026-06-27 旧评审 |
|---------|------|------|----------------------|
| 架构设计 | 8.5/10 | A | 持平（依然一流） |
| 产业链瓶颈分析 | 6.5/10 | C+ | 略升（可投性过滤已写但接错位置） |
| 数据管道与真实性 | **4.0/10** | D+ | **下降**（发现更多虚构占位） |
| 四层决策引擎 | 6.0/10 | C | 升（校验前置、硬止损、动态约束已落地） |
| 投委会 gating | 4.5/10 | D+ | 框架升级但"否决"从未真正触发 |
| 风险控制 | 4.0/10 | D | 升（组合 Beta/VaR 已实现）但无账户级熔断 |
| 回测能力 | 2.5/10 | F+ | 框架有了，`backtest_runs` 仍 0 行 |
| 反馈/自进化闭环 | **2.5/10** | F+ | **代码齐全但从未产出数据** |
| 合规与安全 | **3.0/10** | D | 新增审查维度：荐股合规/提示注入/越权 |
| 前端呈现真实性 | 5.0/10 | C- | 展示了后端并未算出的能力 |
| **综合** | **4.6/10** | **C-** | **架构在进步，"最后一公里"仍未打通** |

---

## 三、最严重问题：系统性 Over-Promise（元级发现）

这是本次评审最重要的发现，独立于任何单个模块。**它不是某一处 bug，而是"文档声称 / 前端呈现 / 数据库实况"三者系统性脱节的叠加效应**：

```
用户 / 使用者的认知              系统的实际状态
─────────────────────────      ──────────────────────────
"止损位在被实时监控"     ←→     scenario_valuations 0 行（止损锚点从未落库）
"风控闸门在拦截违规"     ←→     投委会 rejected 分支从未触发，quality_gate 只标记不拦截
"系统在从交易中学习"     ←→     auto_reviews / experience_cards / layer_performance 全 0 行
"内幕交易信号可参考"     ←→     176 条 insider_trades 中 137 条（78%）是 shares=0 的空壳
"策略已生成完成"         ←→     无 LLM 时返回 mock 却标记 status='completed'
"绩效胜率 XX%"           ←→     sim_trades 仅 3 行且全是买入、无一笔卖出
```

**为什么这比单个 bug 更危险**：一个投资者若信任这些呈现，会以为自己的持仓有止损保护、有风控审查、系统在自我校准——**而这些能力实际都不存在**。在真金白银的场景下，这种"虚假的安全感"比"明确的功能缺失"危害更大。

**根因有三类**，下文分类展开：
1. **占位/虚构数据未标记**，与真实数据混流（第四节）
2. **代码写了但从未触发**（wired-but-never-fires），链路存在断点（第五节）
3. **修复已存在但未接入主流程**（fix-not-cascaded），如分行业权重、动态约束（第六节）

---

## 四、类别一：空置 / 虚构信息（用户最关注，逐条已验证）

### 🔴 C-1｜Form 4 内幕交易 78% 是空壳占位，与真实数据无差别入库
**验证：CONFIRMED** ｜ `sec_pipeline.py:311-325`

`_make_stub_trade()` 在 Form 4 XML 解析失败时返回 `shares=0, price=None, total_value=None, insider_name=filing.title(常为"FORM 4"), transaction_type="unknown"`。数据库实况：**176 条 `insider_trades` 中 137 条（78%）满足 shares=0 且 price 为空**，`insider_name` 大量为字面量 "FORM 4"。这些空壳与真实解析记录同表存储、无 `is_stub` 标记。
**影响**：内幕交易本是最有价值的基本面信号之一。系统喂给决策层与前端的 78% 是噪声，决策层无法区分"数据缺失"与"真实无交易"，可能把空壳当"高管无动作"解读。
**建议**：① XML 解析失败时**不落库**或落入独立 `insider_trades_unparsed` 表；② 已入库的 137 条空壳批量清理；③ 修复 Form 4 XML 解析（SEC 提供结构化 XML，非不可解析）。

### 🔴 C-2｜A 股公告管道产出的是纯占位空壳
**验证：CONFIRMED** ｜ `notice_pipeline.py:115-131`

A 股公告抓取后生成的记录同样是占位结构，无真实公告正文/类型/影响判定。系统因此无法识别 A 股高管增减持、重大合同、股权激励等真实事件，却在"情报"面板呈现为有数据。
**建议**：接入东财/交易所公告真实解析，或明确标注"A 股公告暂不可用"，不以空壳填充。

### 🔴 C-3｜无 LLM 时策略引擎返回 mock 数据却标记 `completed`
**验证：CONFIRMED** ｜ `strategy_engine.py:394-405`

LLM 未配置时调用 `store.complete_strategy(intelligence_summary="模拟数据（未配置 LLM）", signal="neutral", confidence=5)`，记录状态为 `completed`。`strategy_records` 现有 70 行，无法区分其中多少是 mock。下游 `get_latest_strategy()` 取用时**不过滤 mock**，假策略直接流入 L3/决策链。
**建议**：无 LLM 时调 `fail_strategy()` 而非 `complete_strategy()`；或加 `is_mock` 字段并在所有取用查询与前端过滤/标注。

### 🟠 H-4｜UZI 深度分析 mock 结果无任何可见标记
**验证：PARTIALLY_CONFIRMED** ｜ `uzi_runner.py:122-123,175-176,288-289`

无 LLM 时 `_mock_deep_analysis / _mock_investor_panel / _mock_trap_result` 直接返回假分析并经 `complete_uzi_analysis()` 入库（`uzi_analyses` 8 行），表结构无 `is_mock` 列，前端历史列表也不标注——用户无法分辨"65 位大佬评审"是真跑的还是占位的。
**建议**：加 `is_mock` 标记贯穿存储与前端展示。

### 🟠 H-5｜情景估值 bear/base/bull 价格与概率纯 LLM 拍脑袋，无方法论
**验证：CONFIRMED** ｜ `decision_engine.py:652-684`、`store_research.py:454-502`

L2 输出的三场景估值（如 bear=$80/20%、base=$105/60%、bull=$130/20%）由 LLM 仅凭当前价 + 技术指标 + 情绪信号生成，**无财报、无 PE Comps、无分析师预期、无 DCF 支撑**；概率默认值（20/60/20）极少被改动。这些价格随后被设计为 L3 的止损止盈锚点。
**影响**：止损位建立在 LLM 幻觉数字上；且无法事后验证 L2 估值是否有效（无历史留痕对照）。
**建议**：情景估值必须绑定 `financial_data.py` 已采集的真实财务与可比公司，prompt 提供估值方法论；概率需给出推导依据。

### 🟠 H-6｜LLM 幻觉价格可直通模拟成交
**验证：CONFIRMED（审查主管补盲）** ｜ `trade_executor.py:41-48`

`exec_basis = snap.get("close") ... or planned_price` —— 无市场快照时**直接用 LLM 生成的目标价/预估价作为成交价**写入 `sim_trades`，无与现价的偏离度校验（如 ±30% 边界）。
**影响**：一旦快照缺失（停牌/节假日/抓取失败），账户会以幻觉价"成交"，污染全部绩效统计。
**建议**：无真实快照时**拒绝成交并标记 `needs_price`**，绝不用 LLM 价格成交；加偏离度硬校验。

### 🟠 H-7｜`layer_performance` 永远为空 → 绩效驱动的层权重始终回退默认
**验证：CONFIRMED** ｜ `trade_reviewer.py:341-347`、`decision_engine.py:1055-1062`

`_update_composite_scores()` 调 `_layer_weight_factors(store)` 读取分层绩效动态调权，但 `get_layer_performance_summary()` 数据源 `layer_performance` **0 行**。写入点 `record_layer_performance()` 仅在"有卖出交易 + 复盘 + attribution 非空"时触发，而 sim_trades 无一笔卖出 → 永不触发。P3.3"绩效驱动层权重自调整"实为死代码。
**建议**：见下节"闭环断点"根治方案。

### 🔴 C-8｜`preference_learner` 是死代码，用户偏好学习从未启动
**验证：CONFIRMED** ｜ `preference_learner.py:19-163`

`learn_preferences()` 完整实现了 7 维偏好学习并调 `save_preference()`，但**全项目无任何调用方**（scheduler 无此任务，decision_engine 不调用），`user_preferences` **0 行**。L4 每次都用 `{user_preferences}` 空字典，退化为 one-size-fits-all。
**建议**：在 `scheduler.job_auto_review` 后附加 `learn_preferences(store)`，门槛设为拒绝/成交样本 ≥3-5 笔。

### 其他已验证的"空置"结构（Medium）
- `scenario_valuations` 0 行——写入被 try/except 静默吞掉（见 H-14 根因链）
- `tuning_engine` 生成的调优建议无人读取，`tuning_log` 0 行
- `model_calibrator` 写 `model_accuracy`（93 行）但**无人读取其结果**
- `experience_cards` 0 行——经验卡片贝叶斯置信度更新链路未产出
- `macro_consultation`（新增）快照采集完整，但**数据流向 UI 即终点，不回写决策**
- 5 个观察池标的（AAPL/TSLA/ALAB/CBRS/ORCL）`composite_score=0.0`，前端照常展示

---

## 五、类别二：业务漏洞（逻辑断点与风控空转）

### 🔴 C-9｜投委会 gating："rejected"分支从未触发，否决权名存实亡
**验证：CONFIRMED** ｜ `committee.py:752-761`

仅当 `verdict_raw == 'rejected'` 才调 `reject_execution()`。数据库实况：`committee_consensus` 84 条**全部是 `approved`(1) 或 `approved_with_modifications`(79)，零条 rejected**；`trade_feedback` 0 行。即便 4 位委员全票反对，`_fallback_consensus()` 的共识规则也几乎不产出 `rejected`，计划照样进入待确认队列。
**影响**：投委会是核心安全机制，实际是"4 次独立投票拼接"的咨询，无拦截力。
**建议**：① 修正共识规则，`reject_ratio >= 0.75` 或委员一票否决 → rejected；② 强制所有非 approved 结论 writeback `trade_feedback`；③ 前端展示否决理由与用户恢复入口。

### 🔴 C-10｜约束校验前置于投委会，导致投委会无法否决/推翻系统约束
**验证：CONFIRMED** ｜ `decision_engine.py:1250-1303` vs `committee.py:586-892`

执行顺序是：L4 生成 → **硬约束校验（违规即拒 + 回滚）** → 通过者建 `pending` → 投委会评审 pending 计划。这意味着**违反系统约束的计划在投委会看到之前就已被拦截**，投委会无机会推翻硬约束。反之，投委会即使一致同意某标的加仓到 30%（超 25% 上限），也无法实现——权限倒挂。
**建议**：约束校验后移到投委会之后（approved 前/confirmed 前）；`approved_with_modifications` 支持带 `constraint_override` 标签的越权（如 3 票同意 + 用户二次确认才可突破系统硬约束）。

### 🟠 H-11｜无账户级止损/熔断机制
**验证：PARTIALLY_CONFIRMED** ｜ `constraint_validator.py`、`risk_metrics.py`

**已有个股级硬止损**（`decision_engine.py:1353-1407` `_hard_stop_loss_sweep`，跌破 L3 stop_loss 自动生成清仓计划，非 LLM 决策——这是一个亮点，旧审计未识别）。但**无账户级**"单日最大亏损"或"连续亏损 N 笔"熔断，`risk_metrics` 算 max_drawdown 但仅用于报告不用于防护。
**影响**：黑天鹅行情（如 -10% 跌停）下系统继续交易直到手动干预。
**建议**：在约束校验层加账户级日亏损/回撤熔断闸门。

### 🟠 H-12｜可投性过滤接在错误的流程阶段
**验证：CONFIRMED** ｜ `supplier_eval.py:551-564`、`investability_filter.py:28-132`

可投性过滤（市值≥15 亿、毛利≥20%、上市≥365 天）**已实现并接入**——但是在**供应商评估之后**作为"排序阶段"运行，而非在**瓶颈识别阶段作为门控**。因此 `BottleneckReport` 仍无"该瓶颈环节的供应商是否值得投资"的判断；一个高瓶颈度环节即使全部候选供应商市值都 <15 亿，用户仍看到"硅化物瓶颈 9/10"却找不到可投标的。旧审计的 C-1（瓶颈≠标的）**部分修复但未闭合**。
**建议**：`BottleneckReport` 增 `investable_supplier_count` 与 `investability_summary` 字段，在报告返回前对每个瓶颈环节做可投性统计。

### 🟠 H-13｜催化剂判定结果未转化为买卖信号进入 L3
**验证：PARTIALLY_CONFIRMED** ｜ `decision_engine.py:819-833`

`catalyst_monitor.judge_catalyst_outcome` 判定 realized/failed（`catalyst_tracking` 69 行有数据），但 P1.1 承诺的"outcome → sell/add 信号 → L3 输入"未真正接通，催化剂落空/兑现不产生下一轮买卖动作。
**建议**：补 `catalyst_signals` 通道，L3 `run_tactical_plans` 读取。

### 🟠 H-14｜市场代码不一致（`us` vs `us_stock`）破坏跨表关联
**验证：PARTIALLY_CONFIRMED** ｜ `price_pipeline.py:323-324` 等

`watchlist` 表 14 行中 12 行 `market='us_stock'`、**2 行 `market='us'`（正是 AAPL/TSLA）**，而 `market_snapshots`/`committee_reviews` 全是 `us_stock`。这直接导致两个连锁 bug：
- **这 2 个标的 `composite_score=0.0`**（关联不上快照数据）
- **`scenario_valuations` 写入静默失败的根因之一**：`decision_engine.py:655` 用 `e.get("market") == market` 构建 entry_map，市场码不一致 → 匹配失败 → `if not entry_id: continue` 跳过 → 整段被 try/except 只记 warning。
**建议**：统一市场枚举（建议全用 `us_stock`/`cn_stock`/`hk_stock`），迁移历史数据；`create_scenario_valuation` 失败应告警而非静默吞。

### 🟠 H-15｜quality_gate 只标记不拦截
**验证：PARTIALLY_CONFIRMED** ｜ `quality_gate.py:88-224`

`run_quality_checks()` 产出 green/yellow/red 事件仅作为 SSE 推给前端，L1-L4 调用后**从不检查返回的 `quality_check_block` 事件**，即便 red（数据 5 天未更新）也不阻断决策。
**建议**：pre_l3/pre_l4 门控点 red 时强制阻断或要求 revalidate。

---

## 六、类别三：不完善逻辑（闭环断点）

**核心断点——反馈闭环因"无卖出交易"而全链瘫痪**（多条发现汇聚于此）：

```
sim_trades 3 行全是 buy/entry，无一笔 sell
        ↓ (无卖出 → 无已实现盈亏)
auto_reviews 0 行（复盘只在卖出触发）
        ↓
layer_performance 0 行（复盘产出四因归因）
        ↓
experience_cards 0 行 + tuning_log 0 行（经验/调优依赖复盘）
        ↓
L2/L3/L4 prompt 回灌历史表现 = 空 → 层权重回退默认
```

根因链：`execute_trade` **只在用户手动点"确认执行"时触发**（`decision_api.py:489`），投委会评审后不自动执行；且 `trade_executor.py:73-77` 用 `asyncio.get_event_loop().create_task()` 在同步上下文触发复盘，某些场景静默失败。加之从未有卖出场景被端到端走通。

**其他不完善逻辑**：
- **L2 情景估值未喂给 L3/L4**（`decision_engine.py:839-926`）：L3 止损止盈全由 LLM 每天重新拍脑袋，与 L2 bear/bull 价无引用关系（H-5 的下游）
- **`chain_versions` 201 行历史拆解未复用**：每次拆解仍全量调 LLM，无缓存命中，成本与不可重复性双输
- **`realized_pnl` 无持久化来源**（`performance.py:116-136`，CONFIRMED）：`sim_trades` 表无 `realized_pnl` 列，胜率/盈亏比计算全落空
- **`backtest.py` 框架完整但从未被调用**：无前端/scheduler 触发点，`backtest_runs` 0 行
- **Regime 判断纯 LLM 无量化规则**，`regime_confidence` 未做 1-10 边界校验即用于插值 alloc_bounds
- **宏观数据降级无新鲜度约束**（`macro_data.py:133-141`）：采集失败静默回退缓存，可能拿 7 天前 VIX 生成策略，L1 与前端无感

---

## 七、类别四：专业性缺陷

### 🟠 H-16｜瓶颈评分纯 LLM 主观，无外部数据锚定、不可重复
**验证：PARTIALLY_CONFIRMED** ｜ `bottleneck.py:463-577`

五维评分与 CR3/HHI 均由 LLM "估算"（`cr3_estimate`），非查询真实数据。**已有缓解机制**（提示词刻度锚定、多模型加权中位数、z-score 标准化、分歧度检测≥2.0）显著优于旧审计描述的"完全随意"，但根本问题仍在：无外部真值校验、无版本控制/缓存 → 同一输入不同时刻结果不同，参数敏感性分析无法进行。
**建议**：scarcity/pricing_power 引入真实 CR3/HHI 数据（akshare/wind），LLM 评分作补充；`BottleneckReport` 加 `evidence_sources`。

### 🟠 H-17｜维度权重冗余，且分行业权重修复未接入主流程
**验证：PARTIALLY_CONFIRMED** ｜ `bottleneck.py:28-67`

`scarcity` 与 `irreplaceability` 各占 0.25（合计 50%），两者高度相关（同测"下游被动性"）。**分行业优化权重 `INDUSTRY_WEIGHTS` 已存在**（半导体/医药/新能源/消费），但仅 `reverse.py` 正确传 `industry=sector`，**主路径 `graph.py`/`legacy.py`/`phases.py` 创建 analyzer 时均未传 industry**，落回冗余的 `DEFAULT_WEIGHTS`。典型的"fix 已写但未级联"。
**建议**：所有 `BottleneckAnalyzer(...)` 创建点统一传 `industry=chain.sector`。

### 🟠 H-18｜多 LLM 交叉验证在部分路径退化为"伪多样性"
**验证：PARTIALLY_CONFIRMED** ｜ `cross_validation.py:164-168`、`committee.py:80-106`

交叉验证/投委会理论上用不同 provider，但存在两个退化：① 某路径仅取 1 个 `provider:model`，4 个视角其实是同一模型换 4 个 prompt；② 投委会头 3 个 provider 配置失败时静默回退到全 kimi/glm，无告警，独立性丧失变成 1 模型算 4 次。
**建议**：强制校验委员 provider 去重，不足时告警而非静默同模型化。

### 🟠 H-19｜交叉验证结论对最终排序影响极小
**验证：PARTIALLY_CONFIRMED** ｜ `graph.py:235-246`

`consensus_score` 计算后基本不参与 Borda 计分（final_ranking 主要来自圆桌 summary），交叉验证沦为"装饰品"——花了 API 成本却不 gate 排序。仅 `fatal_risk` 标志有微弱前置过滤。
**建议**：将 consensus_score 纳入排序权重，或明确其为诊断性并降低调用成本。

**其他专业性问题（Medium）**：盲测暴露公司身份（名称+行业+市场，只藏了财务）；圆桌"3 轮"实为"2 轮+总结"且后发质疑约束力弱；Kelly 上界 25% 偏保守；VaR/CVaR 无样本不足警告（需 20+ 日）；仓位管理无流动性校验；A 股 RSI/MACD 与美股数据源不一致存比较偏差。

---

## 八、类别五：合理性与合规（审查主管补盲，此前从未覆盖）

| # | 风险 | 严重度 | 要点 |
|---|------|--------|------|
| G-1 | **荐股合规风险** | 🔴 High | 全项目仅 `chain/report.py` 一处免责声明。决策中心、投委会、L3/L4 输出**具体到入场价/止损/股数的可执行买卖指令**、导出的 HTML/PDF 报告及前端 UI 均无投资建议免责。境内无证券投资咨询牌照提供荐股触及《证券法》，美股触及 SEC 投顾监管。 |
| G-2 | **前视偏差（look-ahead bias）** | 🔴 High | 无 point-in-time 架构。财务数据取"当前最新值"（含追溯重述），`market_snapshots`/`news` 无 as-of 版本。一旦基于这些表做回测/自进化，结果被未来数据污染，虚高失真。 |
| G-3 | **提示注入 / 数据投毒** | 🟠 Med | 新闻/公告/SEC 原文未经任何过滤直接拼入决策 prompt。pump-and-dump 通稿或"忽略之前指令"文本可直接篡改买卖判断。仅 web 层有 XSS 转义，prompt 层无 sanitize。 |
| G-4 | **多用户数据隔离高危** | 🔴 High | `store.py:86-120` 用**正则/字符串解析动态在 SQL 插入 `WHERE user_id`**。子查询、UNION、CTE、无别名聚合 JOIN 等形态一旦未被正确改写即造成**跨用户数据泄露**。 |
| G-5 | **仿真→实盘无硬隔离** | 🟠 Med | `execution_plans` 已是可执行订单格式，距接真实 broker 仅一步。当前 quality_gate 不拦截 + 约束前置 + 投委会不否决，在模拟盘是数据问题，接实盘即真金白银损失。需显式 sim/live 模式标志与 live 双确认闸门。 |
| G-6 | **LLM 成本无硬上限** | 🟠 Med | `budget.py` 仅单次 token 估算，无按日/按用户累计入库、无超额熔断。多市场 cron × 4 委员多 provider × 圆桌 × L1-L4 每日全量 + watchlist 扩容后二次方增长，失控重试可烧钱。 |
| G-7 | **公司行动盲区** | 🟠 Med | 无拆股/分红复权（拆股使 `avg_cost`/`unrealized_pnl` 全失真）、无停牌/退市处理、scheduler 仅排除周末不含春节/感恩节，节假日拿过期数据照跑决策。 |
| G-8 | **决策审计追踪缺失** | 🟠 Med | L1-L4 决策未绑定 prompt 版本/模型参数/输入快照哈希，坏决策无法归因（数据错？prompt 改坏？模型漂移？），也无法做 prompt 回归。投资建议留痕本身是合规要求。 |
| G-9 | **数据源许可风险** | 🟢 Low | yfinance 违反 Yahoo ToS（禁商用/再分发），SEC EDGAR 要求声明式 User-Agent 限 10 req/s。产品化/SaaS 化后有被封禁 + 法律双重暴露。 |
| G-10 | **SQLite 运维盲区** | 🟢 Low | 仅 `CREATE TABLE IF NOT EXISTS` 无迁移框架，列语义变更靠手工 ALTER；`watchlist.db` 承载全部交易历史却无备份/WAL 审查，单文件损坏即全丢。 |

---

## 九、改进路线图（按投资回报排序）

### Phase 0：诚信止血（3-5 天）— 消除认知误导，最高优先级
> 原则：**宁可显示"功能不可用"，绝不用占位/虚构数据冒充真实。**

| # | 任务 | 对应发现 |
|---|------|---------|
| 0.1 | 清理 137 条 insider 空壳 + 解析失败不落库；A 股公告空壳同理 | C-1, C-2 |
| 0.2 | mock 数据全链标记：`strategy_engine`/`uzi_runner` 加 `is_mock`，无 LLM 时改 `fail_*` 而非 `complete_*`，前端红标 | C-3, H-4 |
| 0.3 | LLM 幻觉价格禁止成交：无真实快照 → `needs_price` 不成交 + 偏离度硬校验 | H-6 |
| 0.4 | 统一市场代码（`us`→`us_stock`）+ 迁移历史数据 → 修复 0.0 评分 + scenario 写入 | H-14 |
| 0.5 | 全部买卖指令/导出报告/决策中心 UI 加投资建议免责声明 | G-1 |
| 0.6 | `scenario_valuations`/`layer_performance` 写入失败改为**告警**，不静默吞 | H-5, H-7, H-14 |

### Phase 1：打通闭环（1-2 周）— 让"声称完成"变"真在运行"
| # | 任务 | 对应发现 |
|---|------|---------|
| 1.1 | 端到端走通一笔"买入→卖出→复盘→经验卡片→层绩效"，验证 6 张空表落数据 | 第六节断点 |
| 1.2 | `sim_trades` 加 `realized_pnl` 列并在卖出时持久化 | H(perf) |
| 1.3 | 投委会 gating 修正：rejected 分支可触发 + writeback trade_feedback + 前端展示 | C-9 |
| 1.4 | 约束校验后移到投委会之后，支持 override 标签 | C-10 |
| 1.5 | `preference_learner` 接入 scheduler | C-8 |
| 1.6 | 催化剂 outcome → 买卖信号 → L3 | H-13 |
| 1.7 | L2 情景估值喂给 L3/L4 作止损止盈锚点 | H-5 下游 |

### Phase 2：专业性补强（2-4 周）
| # | 任务 | 对应发现 |
|---|------|---------|
| 2.1 | 瓶颈评分接真实 CR3/HHI 数据 + evidence_sources + 拆解缓存复用 chain_versions | H-16 |
| 2.2 | 分行业权重 `INDUSTRY_WEIGHTS` 级联到所有 analyzer 创建点 | H-17 |
| 2.3 | 交叉验证/投委会 provider 去重强制校验 + 失败告警 | H-18 |
| 2.4 | 可投性过滤前移为瓶颈门控，BottleneckReport 加 investable_supplier_count | H-12 |
| 2.5 | 账户级止损/回撤熔断闸门 | H-11 |
| 2.6 | quality_gate red 硬阻断 pre_l3/l4 | H-15 |
| 2.7 | 情景估值绑定真实财务 + 方法论 | H-5 |

### Phase 3：安全与合规加固（并行）
| # | 任务 | 对应发现 |
|---|------|---------|
| 3.1 | 多用户隔离改用参数化查询层/ORM，废弃字符串 SQL 拼接 | G-4 |
| 3.2 | prompt 层对第三方文本做注入过滤 + 来源可信度加权 | G-3 |
| 3.3 | LLM 成本按日/用户累计入库 + 硬熔断 | G-6 |
| 3.4 | point-in-time 数据架构（as-of 快照）为回测/自进化铺路 | G-2 |
| 3.5 | sim/live 模式标志 + live 双确认独立闸门 | G-5 |
| 3.6 | 公司行动复权、停牌处理、交易日历（含节假日） | G-7 |
| 3.7 | 决策留痕：绑定 prompt 版本/模型参数/输入快照哈希 | G-8 |

---

## 十、结论

**保持的核心优势**（勿动）：
1. 产业链瓶颈选股方法论——差异化 Alpha 来源，且瓶颈评分已有多模型交叉 + z-score + 分歧检测的工程化缓解
2. 四层递进决策架构 + 个股级硬止损（非 LLM 决策的风控亮点）
3. 组合级风控（Beta/VaR/相关性）已从"纸面"落到"代码"
4. 多用户系统、SSE 实时、双市场覆盖的工程成熟度

**最紧迫的三件事**：
1. **Phase 0 诚信止血**——这是信任的地基。当前系统对使用者呈现的能力显著超出实际，占位/虚构数据与真实数据混流，必须先止血再谈其他。
2. **打通闭环产出真实数据**——12 张空表不是"待优化"，是"声称已完成却从未运行"。走通一笔完整卖出即可让半数空表活起来。
3. **合规免责 + 越权隔离**——一个输出可执行买卖指令的系统，免责声明与数据隔离是发布的前提，不是可选项。

> **量化分析师一句话评价**："架构是教科书级的，个股硬止损与组合风控的落地令人鼓励。但系统当前最大的风险不是'功能缺失'，而是'把没做到的呈现成做到了'——占位数据冒充信号、空转闭环标称完成。补齐诚信、打通闭环、加上合规护栏，它才配得上它的架构。"

---

*本报告由 11 个专项审计智能体并行精读真实源码、40 项发现逐条对抗性验证、并对照 2026-07-02 生产数据库实况编写。附完整发现清单 `.audit_findings.json`（40 存活 + 50 Medium/Low + 12 盲区）。*
