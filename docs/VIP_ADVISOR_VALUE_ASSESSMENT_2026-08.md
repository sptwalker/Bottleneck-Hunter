# VIP 私人账户顾问系统 —— 价值呈现·风险收益·宏观前瞻专项评估与升级路线（2026-08-02）

> 视角：以资深私人理财分析师 / 家族办公室季度评审标准审视，落到系统**已实现的真实函数与代码行**。
> 状态：**评估完成，路线待批**。这是一次「能力盘点 → 顾问评估 → 首席综合」的深读产出（11 智能体全链读码）。
> 姊妹文档：
> - `VIP_ADVISOR_ROADMAP_2026-07.md` —— 接真账户 + 决策闭环（A/B/C/D，Phase A 档案层是本评估的地基）
> - `VIP_ADVISOR_AUDIT_2026-07.md` —— 结单解析/口径/币种/时间的代码审计修复（TIER-1/2）
> - `VIP_ADVISOR_TECH_SPEC.md` / `VIP_ADVISOR_PLAN.md` / `VIP_ADVISOR_HANDOFF.md`
> 配套记忆：`project_vip_advisor_roadmap.md` / `project_vip_daily_projection.md` / `project_vip_advisory_pass.md`

---

## 0. 定性与硬约束（本评估全程遵守，不可动摇）

- **周期性投资参谋（Decision-Support），非实时投顾**：数据来自券商 PDF 结算单的周期性人工导入（每周或更勤），非券商 API。→ 一切分析/建议/复盘显式标注「数据截至 X 日」，**止于建议层**（减/持/加 + 理由 + 风险 + 衍生品敞口），不生成执行单、不下单、不接券商 API。
- **单用户（管理员专用）**：多用户隔离地基（Key/market/account_ref）保留不动，新功能一律单用户设计，不为多租户付抽象成本。
- **衍生品严格单列**：真值层「账户真实价值」= 结算单事实（股票 + 现金），**衍生品不并入** `total_equity`，单列 `derivative_exposure`（敞口 + 模型估值 + 敲出/累计状态）。
- **成本/盈亏诚实留空**：结算单有则解析（Citi/Nomura 已验证），无则 None 不猜；覆盖率透明回传（`cost_coverage`）。

---

## 1. 一句话总判断

> **系统已是一个「可信的后视镜」，但还不是「私行级的仪表盘 + 前挡风玻璃」。**

信任地基（数字双核验、免责唯一真源、审计留痕、诚实留空）与真值权益口径打得**扎实且带自检**——能诚实告诉你截至结算日持了什么、成本多少、浮盈多少。但站在私行季度评审标准，三块结构性偏弱：

1. **收益侧**——任何标准收益率/归因全空或失真；
2. **风险侧**——beta 恒为 0、衍生品不进风险计量、无压力测试；
3. **产业前瞻接账户**——系统最强的瓶颈引擎对自己账户零作用。

**关键洞察：多数缺口是「算法未接 / 数据未富化」（wiring gaps），而非「数据不可得」（data-unavailable）。** 成本、交易流水、基准收盘、join 原语大多已在库。这让它成为一个**投产比极高的中位盘**，而非需推倒重来的欠债盘。

---

## 2. 现有功能模块全面评估（逐层，锚定真实入口）

| 模块 | 核心入口（真实函数 · file:line） | 评级 | 一句话 |
|---|---|---|---|
| **价值呈现层** | `build_account_dossier` `portfolio.py:743` / `value_series:1012` / `_forward_filled_series:905` / `_rebase_benchmark:984` / `_projection_point:953` / `_overview_totals:549` | 🟡 ~50% | 真值口径 + 曲线三件套 + 成本诚实覆盖扎实；**收益侧结构性空白** |
| **风险与衍生品层** | `compute_portfolio_risk`（`watchlist/risk_metrics.py`，HHI/VaR95/CVaR95/beta/correlation）/ `derivatives.py`（bs_price/bs_greeks/implied_vol/payoff_accumulator）/ `projection.py`（project_derivative_accrual） | 🟡 ~45% | HHI/VaR/CVaR 可用且已喂投委会；**beta 恒 0、衍生品不进风险、BS/Greeks 就绪零调用** |
| **顾问决策层** | `generate_account_advisory` `advisory.py:502` + `_consensus` + `reconcile_draft` + `summarize_cash_budget:425` + 投委会 4 席 | 🟢 ~70% | 逐仓建议主链路 + 确定性共识 + 对账扎实；**纲领硬约束无代码执行器** |
| **宏观产业引擎** | `format_macro_for_prompt` `advisory.py:100` / `get_by_ticker` `store_watchlist.py:126`（join 原语，dossier 未用）/ `chain/bottleneck.py` / `bottleneck_node`（唯一桥梁） | 🔴 ~30% | 两套引擎都成熟但**与账户几近断连**；差异化优势变现最差 |
| **荐新（G3）** | `generate_account_recommendations`（`recommend.py`）+ 投委会评审 | 🟢 ~80% | 观察池荐新 + 评审已通；缺可交易市场白名单 |
| **交互问答（G4）** | `stream_vip_chat`（SSE，事实源喂持仓 + 衍生品） | 🟢 ~85% | 单发式，对话中不实时再查行情 |
| **复盘闭环（G5）** | `review_pending_advice`（调度器）+ `run_attribution`（导入流） | 🔴 ~70% | 打点结算 + 确定性归因已接线；**LLM 经验卡 C-4b 显式关闭**、无账户级卡 |
| **顾问叙事·信任·体验** | `number_guard` / `compliance` / 前端 `vip.js` | 🟡 ~65% | 信任机制强、可视化扎实；**「数据截至」未贯通、叙事像数据单** |

**总体：信任与真值地基优秀，决策链路成型，但呈现的「深度」（收益/风险/前瞻）与叙事的「私行感」还差一层。**

---

## 3. 账户价值呈现分析 —— 如何更清晰全面

### 3.1 已扎实落地（地基，值得肯定）

- **真值权益口径**：头条 `total_equity` = 股票 sim 权益 + 现金，衍生品严格单列 `derivative_exposure` 不并入；空 `account_ref` 硬守卫根除决策中心幻影盘。
- **价值曲线三件套**：`value_series` 期末拼点 + `_forward_filled_series` 多账户前向填充（治快照日期不齐的假跳变）+ `_rebase_benchmark` 基准 on-or-before 对齐（无收盘不画假线，带 4 条 `__main__` 自检）+ `_projection_point`（严格晚于真值才叠加）。
- **成本/未实现盈亏**：结单直接解析，有成本才算否则 None，透明回传 `cost_coverage {covered, total}`。
- **多币种归一 USD** + **Top5 集中度三处一致** + **价源覆盖体检**。

### 3.2 结构性缺口（私行标准下最痛的 7 处）

| 缺口 | 私行标准 | 对用户的影响 |
|---|---|---|
| **无任何标准收益率**（TWR/MWR/IRR/年化）；`returns` 只是相邻期末点简单 pct、分母含现金且未剔注资 | 季报首页必列 period TWR + since-inception + 年化，区分 TWR（评策略）与 MWR（评真实体验） | **无法回答「这季度/今年真实赚了几个点」**——最核心的数字完全缺失 |
| **净值曲线口径不含现金**，与头条（含现金）分裂 | NAV 曲线单一口径（含现金），全报告可交叉比读 | 同屏「头条 $X 但曲线终点 ≠ X」引读者误解 |
| **无真外部现金流建模**：`net_inflow` 靠 `net_amount` 符号猜、混入买卖交割额；枚举无 deposit/withdrawal | external cashflow 与 investment return 严格分离 | **收益率的分母根本性失真**——收益侧一切指标的地基缺口 |
| **无绩效归因**：attribution 只检测「动作」不量化「结果」 | Brinson 归因（配置 vs 选股）或至少 top 贡献/拖累清单 | 看到组合涨跌却不知哪只票贡献/拖累几个点 |
| **已实现盈亏永远 None**：即便有 buy/sell 也未做 FIFO 配对 | realized + unrealized 分列（税务 + 绩效刚需） | 只见浮盈不见落袋；已解析成本的账户本可算却永远留空 |
| **无币种/资产类别敞口视图、无 FX 损益归因**：`nominal_ccy` 只落审计不进呈现 | 按币种/地域/资产类别多维敞口 + FX attribution | **多币种真账户（港币/日元/美元混持）看不到汇率敞口**——正是本系统主力场景 |
| **无股息率/收入率视图**：只给累计标量 | portfolio income yield =（股息 + 利息）/ 市值，年化 | 偏收益型/结构化产品账户看不到「每年生息几个点」 |

### 3.3 增强方向（按投产比排序）

**速赢（全 S 级，一周内可清）：**
1. **绩效摘要 KPI 卡** `perf_summary`：since-inception 收益% / 年化（近似）/ 累计 income yield / vs 基准超额 / 期末近似回撤——**全从 `value_series` + `_overview_totals` 现成数据装配**，一个纯函数塞进 dossier。私行季报首页那排数字，一步补齐。
2. **币种 + 资产类别敞口饼**：`positions.currency` 与 `instruments.instrument_type` 分桶 Σ`market_value_base`，复用 `renderHoldingsPie`。数据恒可算、零新增。
3. **头条/饼图双轨口径打标** `is_derivative`：一个布尔 + 前端分色，消除「总权益不含 X 但饼图含 X」的同屏误读。

**战略（收益侧地基，必须先补分子分母）：**
- **净值曲线口径统一为含现金总权益**（复用 `_import_total_series` 的权威净值口径推广到股票账户）。
- **真外部现金流建模**：ingest 解析结单转入/转出行，落 `deposit/withdrawal` 与真买卖交割区分——**TWR/MWR 成立的前提**，否则算出来都是错分母的伪精确。
- **已实现盈亏 FIFO 引擎**：把「永远 None」改成「无完整流水才 None」（需先核实逐笔股数是否落库）。
- **标的贡献归因**（相邻两期 × 权重，用 transactions 剔除买卖污染）。

---

## 4. 投资项目风险收益评估 —— 如何更专业

### 4.1 已实现（可用且部分已喂投委会）

- **HHI 集中度 / max 单股权重**（纯权重，**恒可用**）；**VaR95 / CVaR95 历史模拟法 / 相关性对 ρ>0.7**（仅覆盖有 `market_snapshots` 的标的）；这些**真实喂进投委会 4 席**（非伪造，过 `number_guard`）。
- **衍生品**：条款抽取（Citi/Nomura/招银 FCN/累购/累沽/MLI 三版式）+ payoff 引擎 + 逐日 accrual 推算 + 结单校准闭环。
- **BS 定价 / Greeks / IV 纯函数就绪**（交易台口径，含股息 q，带自检）。

### 4.2 结构性缺口（8 处，风险侧最痛）

| 缺口 | 影响 |
|---|---|
| **portfolio_beta 在 VIP 路径恒为 0**（`_portfolio_risk_summary` 未传 `benchmark_returns`） | 投委会与报告引用的 β 永远是**伪数 0.0**；SPY/指数日收盘**其实已在库**——「可算但未接」 |
| **无最大回撤计算**，`mandate.max_drawdown_pct` 无任何代码比对 | 用户设的「回撤上限 25%」形同虚设——纯 prompt + 软否决，LLM 可违反而无函数挡下 |
| **无组合级情景/压力测试**（市场 -20% / 利率 / 波动率冲击） | 极端市况下组合损失多少不可见；**杠杆衍生品的尾部风险完全盲区** |
| **无风险调整收益**（Sharpe/Sortino/Calmar）与组合波动率 σ | 无法判断收益是靠承担过度风险还是真 alpha |
| **BS/Greeks/IV 就绪未接线，无组合净 Greeks 聚合** | 累购/FCN/MLI 的方向性与波动率敞口不可见，用户不知隐含杠杆 |
| **衍生品完全不进组合 VaR / 风险计量**，无 notional 名义敞口视图 | **组合真实风险被系统性低估**——「总权益不含衍生品」下这是最大敞口盲区 |
| **无 KO/KI 实时状态面板**（距敲出/敲入 %、KI 是否触发、剩余名义额度） | 用户不知累购是否临近敲出（利润封顶）或 FCN 是否已敲入（本金风险激活） |
| **VaR/CVaR/相关性仅美股子集，无覆盖率标注** | 港股数字码/欧洲 ISIN 被静默丢弃却权重仍进 HHI，误导用户以为已覆盖全组合 |

### 4.3 增强方向

**速赢：**
1. **接通 `benchmark_returns` 让 beta 生效**——复用 `get_snapshots` + `default_benchmark_ticker`，**约 10 行**，unbreak 投委会已消费的伪数，同一份基准线首末差顺产「vs 基准超额收益」标量。
2. **组合波动率 σ 单列 + VaR/CVaR 覆盖率标注**——`compute_portfolio_risk` 已算出 `portfolio_returns` 却在算完 VaR 后**丢弃**，顺手加 σ，约 8 行。
3. **衍生品 notional 名义敞口 / 市值 + 杠杆比率**——`terms.max_nominal_shares` 与 `loan_balance` 均已在库，纯乘除。
4. **mandate 数值校验器** `check_mandate_compliance`：集中度/排除清单/聚焦板块硬拦（恒可算），回撤给稀疏近似 + 标注——让 risk_officer 收到**结构化硬约束破坏信号**而非纯文本自由推理。

**战略：**
- **组合级情景/压力测试引擎** `stress_test`：市场 -20% / vol / 利率冲击组合级 P&L，股票线性 delta、衍生品 payoff 重放 + BS 重定价——**让就绪未接线的 BS/Greeks 首次落地生产**，杠杆衍生品尾部风险首次可见。
- **KO/KI 实时状态面板**（抽 `project_derivative_accrual` 已有的 KO 扫描为只读查询）。
- **风险调整收益 + 稀疏 MDD**（受 `value_series` 稀疏点硬约束，必须标注「基于 N 期结单·非逐日」）。

---

## 5. 宏观形势 + 产业趋势 → 未来价值投资方向（差异化核心）

这是本系统**相对通用理财软件最大的差异化优势，也是当前变现最差的一环**。

### 5.1 现状：两套成熟引擎，与账户几近断连

- **产业链/瓶颈引擎（chain/）**：三步法拆解 → 五维瓶颈评分（scarcity/irreplaceability/供需缺口/定价权/技术壁垒）+ CR3/HHI 反校——成熟，但产物只进 CLI/圆桌报告，**不落账户可查结构**。
- **L1 宏观引擎**：regime / 风险偏好 / 建议现金 / `sector_rotation`（强/弱/中性三桶）/ 风险因子——结构化产出。
- **三根细弦相连**：宏观仅**文本注入**（`format_macro_for_prompt` 只读 L1）、瓶颈仅**候选侧**（`bottleneck_node` 只对新荐、不触持仓）、现有持仓**零产业链映射**。

**结果：账户分析仍停在「看后视镜」——ticker + 成本 + 浮盈；产业前瞻未落到账户决策。**

### 5.2 关键缺口

- **持仓 → 产业链位置映射不存在**：`build_account_dossier` 的 holdings 无 entry_id/sector/bottleneck_node，即便某持仓恰在观察池带瓶颈标签，advisory 也**不做这次 join**。
- **regime → 板块倾斜无确定性对账**：L1 `sector_rotation` 与账户实际板块权重仅靠 LLM 在两段文本间自由推理，无「你 40% 压在走弱板块」这类结构化旗标。
- **账户级瓶颈暴露 + 主题缺口分析缺失**：无法回答「该向哪个被忽视的瓶颈环节倾斜、从哪个拥挤主题腾挪」。
- **持仓催化剂恒空 bug**（与映射缺口同源）：持仓传 `entry_id=''` → 催化剂段恒为「暂无」，只有荐新候选点亮。

### 5.3 增强方向（连接投产比极高，地基件全现成）

**速赢（地基第一步）：**
- **持仓 → `get_by_ticker` join**：dossier holdings 逐仓反查观察池补 `entry_id/sector/bottleneck_node` + `join_coverage`（仿 cost_coverage 诚实降级），**顺手修持仓催化剂恒空 bug**。join 原语 `store_watchlist.py:126` 现成、字段全在库——**所有产业前瞻能力的地基第一步**。
- **板块暴露 vs L1 轮动对账旗标** `reconcile_sector_rotation`：一个带自检的纯函数，把「超配走弱板块」从 LLM 文本推理变**结构化信号喂投委会**，立即让宏观研判在账户上可复现落地。

**战略（差异化变现）：**
- **账户级瓶颈主题暴露 + 机会集缺口图**：聚合持仓 `bottleneck_node × 权重` vs 观察池候选机会集，diff 出 **under-owned 高价值瓶颈主题 / over-owned 拥挤仓**——把差异化的瓶颈引擎**首次接到用户自己的账户**，实现「看后视镜 → 产业前瞻」的核心跃迁。
- **前瞻再平衡方向注入 recommend/advisory**：缺口给候选确定性 re-rank，**向被忽视的优质瓶颈供应商倾斜、拥挤/走弱主题打「建议腾挪」旗标**（止于建议层不执行）。

> 天花板诚实标注：`bottleneck_node` 是自由文本非规范化 taxonomy，主题缺口图只能做粗粒度字符串匹配的**定性图**；量化瓶颈强度暴露须等「持久化五维评分」（Phase 5），且**价值未验证前 YAGNI 不做**。

---

## 6. 顾问叙事·信任·体验 —— 从「数据单」到「私行评审」

**信任机制强**（number_guard 双核验、未核到显式回传、免责唯一真源、审计留痕、顾问可信度透明），但有几处体验断点：

- **「数据截至 X 日」未贯通**：只有 dashboard 有；advisory/recommend/report 三个建议面板只带「生成于」（出具时间），**用户看到「今天生成的意见」却无从判断底层持仓可能来自 40 天前的结单**——硬约束局部被违反。
- **叙事像「数据单 + 3 段 AI 短评」**：缺本期综述、自上期变化、开篇目标重述、收尾行动清单。
- **无统一「本轮账户行动清单」**：advisory（减/持/加）与 recommend（荐新）分居两 tab，用户须自行心算合并。
- **反向信任瑕疵**：最大回撤 KPI 用稀疏期末点算出后**直接当硬风险指标展示**，无「非逐日近似」标注——把粗近似伪装成精确。
- **纲领设了却不在建议里对照回显**；**good-path 无绿色核验回执**（只在异常时打 ⚠，数据全核且新鲜时无正向信号）。

**增强**：数据截至标签贯通三面板（P0/S）、投委会主席综述行（确定性拼装、不加第 2 次 LLM）、统一行动清单合并视图（复用 `summarize_cash_budget` 的现金配平）、报告叙事升级（喂 `attribution.detect_position_events` 确定性事件）、纲领合规结构化对账面板。

---

## 7. 分阶段升级路线图（后续开发规划）

> 排序原则：**先诚实合规 → 先接线（投产比最高）→ 先地基后指标 → 差异化战略压后但价值最高**。
> 依赖：本路线的地基是 `VIP_ADVISOR_ROADMAP_2026-07.md` 的 Phase A 档案层（`build_account_dossier` 唯一事实源）。

### Phase 0 · 速赢诚实批（全 S，约 1 周，零数据地基依赖）

**一步补齐硬约束合规、消除 beta=0 伪数、点亮季报首页数字与差异化 join 地基。**

| # | 项 | 层 | 规模 | 依赖 |
|---|---|---|---|---|
| 0-1 | 「数据截至 X 日」标签贯通 advisory/recommend/report 三面板 | 呈现/信任 | S | 无 |
| 0-2 | 接通 `benchmark_returns` 让 portfolio_beta 生效（修伪数 0.0）+ 顺产 vs 基准超额 | 风险 | S（~10 行） | `get_snapshots`/`default_benchmark_ticker` |
| 0-3 | 绩效摘要 KPI 卡 `perf_summary`（since-inception/年化/income yield/超额/近似回撤） | 呈现 | S | `value_series`/`_overview_totals` |
| 0-4 | 币种 + 资产类别敞口饼 | 呈现 | S | `positions.currency`/`instrument_type` |
| 0-5 | 持仓 → `get_by_ticker` join（补 entry_id/sector/bottleneck_node + join_coverage，**顺手修催化剂恒空 bug**） | 宏观地基 | S | `store_watchlist.py:126` |
| 0-6 | 组合波动率 σ 单列 + VaR/CVaR 覆盖率标注 | 风险 | S（~8 行） | `compute_portfolio_risk` 已算 `portfolio_returns` |
| 0-7 | 衍生品 notional 名义敞口 / 市值 + 杠杆比率 | 风险 | S | `terms.max_nominal_shares`/`loan_balance` |
| 0-8 | 头条/饼图双轨口径打标 `is_derivative`（前端分色） | 呈现/信任 | S | 无 |
| 0-9 | 投委会主席综述行（确定性拼装，不加第 2 次 LLM） | 叙事 | S | `summarize_cash_budget` |
| 0-10 | good-path 绿色核验回执（数据全核且新鲜时的正向信号） | 信任 | S | `number_guard` |

### Phase 1 · 校验器与体验闭环（M，依赖 Phase 0）

**把纲领从「背景板」变结构化对账，闭合「导入 → 建议 → 执行」体验最后一段。**
- mandate 数值校验器 `check_mandate_compliance` + 合规对账面板（集中度/排除/聚焦板块硬拦；回撤稀疏近似 + 标注）
- 统一「本轮账户行动清单」合并视图（advisory 减/持/加 + recommend 荐新，复用现金配平）
- 板块暴露 vs L1 轮动对账旗标 `reconcile_sector_rotation`（带自检纯函数，结构化信号喂投委会）
- KO/KI 状态面板（抽 `project_derivative_accrual` 的 KO 扫描为只读查询）
- 已实现盈亏 FIFO 引擎（`永远 None` → `无完整流水才 None`；需先核实逐笔股数落库）

### Phase 2 · 收益侧地基（M/L，数据层，收益率一切正确性的前提）

**补齐分子（含现金 NAV）与分母（外部现金流分离），否则上层收益率全是错分母的伪精确。**
- 净值曲线口径统一为含现金总权益（复用 `_import_total_series` 权威净值口径推广到股票账户）
- 真外部现金流建模（ingest 补 deposit/withdrawal 解析，与真买卖交割区分）

### Phase 3 · 收益率、风险调整与归因叙事（L，建在 Phase 2 上）

**回答「真实赚了几个点、靠 alpha 还是加杠杆、哪只票在拖累」。**
- Modified Dietz + MWR/IRR（显式标注「基于 N 期结单·非逐日」）
- Sharpe/Sortino/Calmar + 稀疏 MDD（指示性趋势，非日频精确）
- 标的贡献归因（相邻两期 × 权重，transactions 剔除买卖污染）
- 报告叙事升级（喂 `attribution.detect_position_events` 确定性事件）

### Phase 4 · 产业前瞻接账户 + 组合级尾部风险（L，差异化战略）

**兑现相对通用理财软件的最大差异化优势；杠杆衍生品尾部风险从盲区变可见。**
- 账户级瓶颈主题暴露 + 机会集缺口图（持仓 `bottleneck_node × 权重` vs 观察池候选，diff under/over-owned）
- 前瞻再平衡方向注入 recommend/advisory（候选确定性 re-rank + 拥挤/走弱主题「建议腾挪」旗标，止于建议层）
- 组合级压力测试 `stress_test`（市场/vol/利率冲击，股票线性 delta + 衍生品 payoff 重放 + BS 重定价——**BS/Greeks 首次落地生产**）
- 组合净 Greeks 聚合

### Phase 5 · 量化瓶颈地基（L，YAGNI 守门·可选）

- 仅当 Phase 4 定性主题图证明价值后，再持久化 chain 五维评分。
- 刻意**不建** regime → 板块目标权重执行器（越界且 YAGNI，止于建议层用 L1 轮动桶对账已够）。

---

## 8. 诚实边界（不可动摇的红线）

1. **数据是周期性稀疏期末点，非逐日 NAV**：TWR/MWR 只能是 Modified Dietz 近似，Sharpe/MDD 只能是指示性趋势——必须显式标注「基于 N 期结单·非逐日」，**绝不伪装日频精确**。
2. **收益率分母目前根本性失真**：Phase 2 补齐前，**不应对外呈现任何声称精确的 TWR/MWR**。
3. **衍生品严格单列不并入头条**：notional/Greeks/压测是「呈现敞口」，绝不把衍生品市值并回头条权益。
4. **已实现盈亏诚实留空**：只在流水完整覆盖时才算，缺则维持 None 并标「未配平」，**绝不 FIFO 臆造**。
5. **止于建议层**：再平衡为方向性建议非精确目标权重、不下单、不做实时投顾。
6. **覆盖口径必须披露**：所有风险指标声明「基于 N/总 M 只可定价标的」。
7. **单用户定性不变**：沿用现有 `for_user/for_market` 隔离，不新造第二套配置。

---

## 9. 北极星

成为管理员本人专属的、诚实到可托付的「周期性投资参谋」——每次导入结单后，一屏之内既能**回望**（真实赚了几个点、哪只票贡献/拖累、钱压在哪个瓶颈环节、汇率贡献几何），又能**前瞻**（对照纲领逐条打勾、把宏观 house view 和瓶颈机会集落成方向性建议、极端市况看清杠杆尾部风险），全程标注「数据截至 X 日」、诚实降级、止于建议——**用私行级的严谨，服务一个人的账本。**

**建议起手**：Phase 0 速赢批，尤其 **0-2（接通 beta，10 行修伪数）** 与 **0-5（持仓 join，差异化地基第一步、顺手修催化剂 bug）**——投产比最高、风险最低。
