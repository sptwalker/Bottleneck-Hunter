# BottleneckHunter 系统回归评审 + 完善计划（2026-07-27）

> 方法：三视角**证据驱动**评审（资深投资顾问 / 资深研发工程师 / 产品经理），三名评审员并行实地考察真实代码、提示词、测试与前端，结论均附文件行号与真实数字，不采信文档声称。
> 基线：main = `cd37a37` 之后本地含 Phase 5b（C-2/C-3/C-4，未提交）；53047 行 Python / 149 模块 / 101 测试文件 · 1137 test 函数 / 53 张 DB 表 / 37 提示词。
> 定位复核：AI 产业链瓶颈选股（Serenity 三步法）+ 决策中心（L1-L4 + 投委会 + 模拟交易 + 复盘）+ VIP advice-only 周期性参谋。

---

## 〇、总评

早期审计（`SYSTEM_AUDIT_REPORT.md`，5.2/10、7 Critical）的绝大多数 Critical 已**实质解决**（看代码非看声称）：闭环、圆桌、约束硬验证、组合风控、Form4 诚信化、催化剂判定均真跑通。系统已从"架构一流、实现半成品"跨到**"决策闭环与风控真跑通、诚实边界克制到位"的可辅助阶段**。

距"完全生产级 / 敢单独信任"就差三类事：
1. **别自欺** —— 伪回测正名、死代码仓位引擎接活；
2. **别悄悄亏** —— 资金/风控路径测试补齐、一处 latent bug 修掉；
3. **别误导** —— 最醒目数字的诚实标注/币种口径修正、反馈层统一。

---

## 一、资深投资顾问视角：方法论与实用性

**判断：敢用作产业链研究、组合风险体检、投委会式反面拷问，敢把 VIP 参谋当诚实的周期性第二意见；但绝不敢单凭它下单，尤其不信它的回测数字与仓位建议。**

### 扎实、可信任（有据）
- **瓶颈评分非"LLM 拍脑袋"**（`chain/bottleneck.py`）：5 维 + 分行业权重；A 股用东财成分股市值算**真实 CR3/HHI 覆盖 LLM 估算**（`_analyze_node` L494-514）；`_check_hhi_consistency`（L647-710）强制校准留痕；`normalize_scores` z-score 消批次偏差并检测"分数雷同=抄示例值"（L149-179）；多模型加权中位数 + σ≥2 标"分歧"。
- **投委会真拷问**：`committee_risk.md` 硬否决（单股>20%/无止损）、`committee_contrarian.md` 打拥挤交易/伪分散、`cv_financial_auditor.md` `fatal_risk` 一票否决；`committee.py` 2 轮可改票 + `_run_discussion` 分歧圆桌。
- **组合风控真接入**：`risk_metrics.py:compute_portfolio_risk` 真算 VaR(历史模拟95分位)/CVaR/Beta/HHI/相关性，注入 L2/L4/投委会；L4 `validate_execution_plan`+`validate_portfolio_beta`+`validate_against_regime` 硬拦截；`check_account_circuit_breaker` 回撤≥20% 只许减仓（`decision_engine.py` L1390-1421）。
- **VIP 诚实度经得起真钱推敲**：真实权益=股票+现金（不含衍生品估值）；`number_guard` 给 AI 凭空数字打 ⚠；无价源标的明示"结转价非最新行情"；衍生品敲出"1 天保守近似不假装精确"；`_rebase_benchmark` 承认曲线是月结单拼点、非按日净值。

### 华而不实 —— 必须摘掉的招牌
| # | 问题 | 证据 | 风险 |
|---|---|---|---|
| INV-1 | **伪回测**：只回放自身 `sim_trades`，不能证明方法论历史 alpha | `watchlist/backtest.py` + `performance.py` | 最危险误信点 |
| INV-2 | **仓位算法是死代码**：凯利/波动率缩放/风险平价零 import，L4 股数靠 LLM 直觉 | `watchlist/position_sizing.py` 无被引用 | 资金相关 |
| INV-3 | **可投性过滤流动性空转**：`avg_daily_volume=None` 永久 stub，4 门槛只市值+毛利率生效 | `supplier_eval.py` `InvestabilityFilter` L102 | 选股质量 |
| INV-4 | **VIP 加仓无仓位量化**："加仓 NVDA"不说加多少/现金够不够 | `vip/advisory.py` `summarize_cash_budget` 自述 | 建议可执行性 |

### 次要缺口
- 5 维仅集中度类在 A 股有事实锚，美股侧及"不可替代/技术壁垒"仍主观；稀缺与不可替代权重各 0.25 但 ρ≈0.8（双重计数未修）；prompt 内仍带示例数值（锚定风险）。
- provider 独立性软肋：4 委员降级到同一 provider 仅告警不阻断（`committee.py` L721-727）。
- VIP 归因只有确定性 diff（`attribution.py`），LLM 因果复盘（C-4b）默认关未接；净值曲线月结单拼点粒度粗；现金口径不含融资 buying_power。

---

## 二、资深研发工程师视角：健壮性 / 质量 / 测试 / 可观测

**判断：达到"实用化"，接近但尚未完全"生产级"。工程纪律优秀，缺口集中在资金路径测试空白 + 一处笔误 bug + 无自动告警。**

### 纪律面优秀（真实数据）
- **0 裸 `except` / 0 `except:pass` / 0 注释死代码 / 仅 1 条真 TODO**（`fact_check.py:344`）；异常全部带类型 + 日志。
- **日志统一**：791 处 `logger.`（352 处 error/warning/exception）；生产代码无遗漏调试 `print`。
- **迁移幂等**（`store.py:219-237` 全 `IF NOT EXISTS` + ALTER 逐条 try 忽略 duplicate）；多用户/市场隔离在新代码一致。
- **测试规模健康**：101 文件 / 1137 test；`pytest --co -m "not slow"` 1180 收集 1.1s 无 import 错误；VIP 子集 94 全绿。
- **可观测已生产化**：`OBSERVABILITY.md` Grafana+Loki+Alloy 自托管栈（stdout→Loki 14 天→Grafana 登录墙）。

### 值得修
| # | 问题 | 证据 | 严重度 |
|---|---|---|---|
| ENG-1 | **latent bug**：兜底分支 `provider`/`model` 未定义 → `NameError` 而非降级 | `web/streaming/legacy.py:117`（应为 `config.provider, config.model`） | 中（主流式路径） |
| ENG-2 | **资金/风控路径测试全 0**：risk_metrics / position_sizing / vip.projection / slippage / backtest / model_calibrator / preference_learner / quality_gate | `grep 模块名 tests/` = 0 | 中偏高 |
| ENG-3 | **无自动告警**：采集+检索就绪，"5 分钟 ERROR>10"仍需人工看 Grafana | `OBSERVABILITY.md` L90-93 | 中 |
| ENG-4 | **ruff 信噪比差**：1117 项中 265 项 B008 是 FastAPI `Depends()` 误报，淹没真问题；F401×70 / F841×9 应清；I001×104 可自动修 | `ruff check --statistics` | 低（治噪声） |
| ENG-5 | God 模块 8 个（`decision_engine.py` 1976 / `web/api.py` 1477 …） | `wc -l` | 低（顺手拆，不专项） |

> F821 另 2 项经核实为误报/良性：`cli.py:327`（`from __future__ import annotations` 下注解不求值）、`committee.py:147`/`uzi_runner.py:334` B023（lambda 同迭代内立即 await，无延迟绑定后果，可加 noqa）。

---

## 三、产品经理视角：用户体验

**判断：已达"顺手好用"的实用化水平（面向单管理员）。核心旅程无断点，数据诚实度是罕见亮点。主要拖累是反馈层不统一 + 几处最醒目数字的口径漏洞。**

### 亮点（真实落地）
- **诚实标注到第一线**：价值曲线实值(实线)vs 推算(琥珀虚线+菱形，`vip.js:697-719`)、staleness 三级徽章（`vip.js:654-660`）、空状态解释口径（`vip.js:680/732`）、硬约束标红"不得推荐"（`index.html:1788/1816`）、"仅供参考不下单"反复声明。
- 导航 `role="tab"` 无障碍齐全；长操作加载+时间预期（`vip.js:1142`"约20-40秒"）；危险操作统一 `showConfirm(danger)`（`utils/confirm.js`）；CSS OKLCH 专业色彩体系 + AA 对比度注释（`tokens.css:18`）；echarts 本地 vendor。

### 值得改
| # | 问题 | 证据 | 影响 |
|---|---|---|---|
| UX-1 | **反馈层三套 toast 并存 + 60 处原生 `alert()`** 破坏视觉统一 | `ai-config.js`×20 / `decision.js`×9 / `admin.js`·`phases.js`×7 … | 高 |
| UX-2 | **头部"总权益"KPI 推算时无"推算·非确认"标记**（staleness 只挂下方图表）；金额硬编码 `$`，A 股账户显示错误"$X"与"币种=CNY"自相矛盾 | `vip.js:581/601/605` | 中高（可信度） |
| UX-3 | **静默失败**：数据主表加载失败与"无数据"界面相同，无重试引导；交易表 `txn_type` 渲染原始英文 | `vip.js:1021`（~11 处空 catch）/ `vip.js:1018` | 中 |
| UX-4 | L1-L4 / verdict / 催化剂 / 瓶颈评分 无就地 tooltip，靠"新手必读"外部文档 | `index.html` | 低 |
| UX-5 | 无全局暗色模式；宽表页面窄屏只能横滚（桌面自用可接受） | `css/` 仅局部 dark | 低 |

---

## 四、完善计划

> 排序原则：**投入产出比 × 风险**。P0=先做（低成本/高风险或阻断）；P1=应做（诚实性/资金/体验一致性）；P2=优化（技术债/健壮性）。
> 红线：VIP advice-only 不写 `sim_*`；隔离 `.for_user().for_market()`；提交/推送需用户明确点头。

### P0 — 先做（低成本高价值）

- **P0-1 修 `legacy.py:117` NameError**（ENG-1）
  - 改：`bottleneck_llms = [(deep_llm, provider, model)]` → `(deep_llm, config.provider, config.model)`。
  - 验收：构造"角色矩阵为空"场景，兜底分支不抛 NameError、优雅降级到 config 模型。
- **P0-2 资金/风控路径边界测试**（ENG-2）
  - 目标模块：`risk_metrics`、`position_sizing`、`vip/projection`（先这 3 个"错了会亏钱"的）。
  - 覆盖：负仓位/除零/缺价跳过/隔离越权/空组合；每模块一个 `test_*.py`，assert 为主、无框架化。
  - 验收：新增测试全绿；VIP 子集仍 ≥94；离线全量不回退。

### P1 — 应做（诚实性 + 资金 + 体验一致性）

- **P1-1 摘掉"回测"招牌**（INV-1，诚实性最高优先）
  - 二选一：① 前端/文档把 `backtest`/`performance` 结果改称**"模拟盘复盘绩效"**并显式标注"非策略历史验证"；② 若要真回测，另立历史信号重放 + 参数敏感性（大工程，单独排期）。
  - 建议先做 ①（改名 + 标注，低成本除误信），②列 P2 排期。
  - 验收：界面/报告不再出现暗示"策略历史 alpha"的措辞；口径一句话说清。
- **P1-2 仓位引擎接活**（INV-2 + INV-4）
  - `position_sizing.PositionSizer` 接进 L4 执行股数计算（至少波动率缩放/上限钳制），或先在 VIP `summarize_cash_budget` 给"加仓"量化建议股数/金额 + 现金校验。
  - 验收：L4 计划股数有量化依据（非纯 LLM）；VIP 加仓建议带"建议 N 股 / 约 $X / 现金覆盖率 Y%"。
- **P1-3 统一前端反馈层**（UX-1，前端投产比最高）
  - 新增 `web/static/js/utils/toast.js`，合并 `dashboard._showToast`/`watchlist.showToast`/`wizard-state.toast`；60 处 `alert()` 全量替换（错误用 danger 变体）。改 `index.html` 版本号。
  - 验收：全站无原生 alert；成功/错误提示样式统一。
- **P1-4 诚实标注延伸 KPI + 币种修正**（UX-2）
  - 总权益卡加"结算截至X日"角标（复用 value-series，推算尾段琥珀提示）。
  - **实施时校正**：核对后端发现 KPI/图表口径为 `market_value_base`=统一美元（`portfolio.py:130/376`、报告"统一美元口径"），`$` 本就正确、不应改成 ¥；唯一真币种错配在交易表——`gross_amount/net_amount` 是原币（`ingest.py:632`，旁边已有币种列却前缀 `$`）→ 改为按行 `currency` 取符号。
  - 验收：交易表金额符号与币种列一致；推算态总权益一眼可辨。
- **P1-5 消灭静默失败**（UX-3）
  - 数据主表 catch 区分"加载失败·重试"与空状态 + toast；交易表 `txn_type` 复用 `vip.js:436` 映射范式中文化。
  - 验收：断网/500 时主表给明确失败反馈；交易类型显示中文。

### P2 — 优化（技术债 / 健壮性）

- **P2-1 接一条 ERROR 阈值自动告警**（ENG-3）：Grafana/Loki 已就绪，配"5 分钟 ERROR>N"→ 通知渠道，闭合无人值守发现能力。
- **P2-2 ruff 治噪声**（ENG-4）：`ruff check --fix`（自动修 I001 等 324 项）+ 路由文件 `per-file-ignores` B008 + 清 F401×70 / F841×9。目标把 1117 降到"每条都值得看"。
- **P2-3 可投性流动性 stub 填实**（INV-3）：`avg_daily_volume` 接真实成交量数据源，让流动性门槛真生效。
- **P2-4 瓶颈评分因子去重 + 去锚定**（次要缺口）：稀缺/不可替代权重合并或降相关；prompt 示例数值改为区间描述去锚。
- **P2-5 VIP LLM 因果复盘（C-4b）**：归因事件攒够 `_CARD_THRESHOLD=12` 后接 LLM 经验卡生成（挂载点已留 `attribution.py` `ponytail:` 注释）——依赖真实数据积累，勿提前。
- **P2-6 真回测框架**（P1-1 的 ②）：历史信号重放 + out-of-sample + 参数敏感性，单独大排期。
- **P2-7 God 模块顺手拆**（ENG-5）：按新增功能拆 `decision_engine.py`/`web/api.py`，不专项重构。

### 进度追踪

| 编号 | 事项 | 视角 | 优先级 | 预估 | 状态 |
|---|---|---|---|---|---|
| P0-1 | legacy.py NameError | 工程 | P0 | 5 min | ✅ 已修（含 phases.py 复核无误） |
| P0-2 | 资金路径边界测试(3 模块) | 工程 | P0 | 半天 | ✅ 16 测试全绿 |
| P1-1 | "回测"改名+标注 | 投资 | P1 | 小 | ✅ 选①：模拟交易「账户盈亏」页顶加"模拟盘复盘绩效·非策略历史验证"横幅；backtest.py docstring 补口径诚实说明（前端无 backtest UI，仅此绩效面板与 API） |
| P1-2 | 仓位引擎接活 | 投资 | P1 | 中 | ✅ 在 VIP `summarize_cash_budget` 接入 `PositionSizer.volatility_scaled`（新 `_size_one_add`：60 日快照算年化波动→目标波动 15%缩放→20%上限钳制）；每笔加仓返回 suggested_shares/amount/target_weight_pct/vol_annual_pct + 逐笔现金覆盖 cash_covered + 顶层 cash_coverage_pct；vip_api 传 wl_store；前端 refreshBudgetBar 渲染"建议 N 股·约 \$X·目标权重%·现金覆盖率%"，无快照回退 unquantified。PositionSizer 从此有真实调用方（INV-2+INV-4） |
| P1-3 | 统一 toast/替换 alert | 产品 | P1 | 半天 | ✅ 新建 utils/toast.js，3 套合一 + 60 处 alert 全替换，按文案自动判级 |
| P1-4 | KPI 诚实标注+币种 | 产品 | P1 | 小 | ✅ 总权益挂"结算截至X日"角标（推算尾段琥珀提示）；**校正**：KPI/图表用 market_value_base=统一美元，$ 本就正确、勿改；真正币种错配仅在交易表(gross_amount=原币)→已按行 currency 取符号 |
| P1-5 | 消灭静默失败+txn 中文 | 产品 | P1 | 小 | ✅ 总览/持仓/交易三主表 catch 区分"加载失败·重试"+toast；txn_type 复用 VIP_TXN_TYPE_LABEL 中文化 |
| P2-1 | ERROR 自动告警 | 工程 | P2 | 小 | ✅ 随 observability 栈自动装载 deploy/grafana/provisioning/alerting/error-rate.yaml：近5分钟 bottleneck-hunter 容器 ERROR 日志>10 → 每分钟评估触发 → webhook 投递（ALERT_WEBHOOK_URL，飞书/钉钉/企业微信/Slack/自建通用）；未设 env 落 example.invalid 占位(告警仍触发可见、仅投递失败、非安全项软默认不 fail-closed)；noDataState=OK(无 ERROR=健康)。compose 注入 env，OBSERVABILITY.md 五节改写为"已接"含改阈值/改邮件指引；YAML 结构自检通过 |
| P2-2 | ruff 治噪声 | 工程 | P2 | 小 | ✅ 1351→617；B008 265 误报经 per-file-ignores 消除（FastAPI Depends/Query/Body 官方惯例）+ 487 项 autofix + 生产代码 F401/F841/F821/B023 清零；残留 617 为纯风格项（E501 行长/E402/E701/E702/SIM）+ 测试文件 F841，均不掩盖真 bug，接受为噪声底 |
| P2-3 | 流动性 stub 填实 | 投资 | P2 | 中 | ✅ FinancialSnapshot 新增 avg_daily_amount_wan（本币万元）；financial_data.py 新 `_avg_daily_amount_wan`（≤60日均值/1e4，有效<20日返 None 不误杀），A股取「成交额」列(元)、美股取 Volume×Close(美元)；InvestabilityFilter 规则3 市场感知阈值（A股¥5000万/日、美股$5M/日）真生效；含 `__main__` 自检（缺失跳过/低于淘汰/达标通过，A股美股各一遍）已过 |
| P2-4 | 瓶颈因子去重去锚 | 投资 | P2 | 中 | ✅ 去重：DEFAULT_WEIGHTS 中高相关(ρ≈0.8)的 scarcity+irreplaceability 合计 0.50→0.40，释放 0.10 分给正交的 supply_demand_gap(0.20→0.25)、tech_barrier(0.15→0.20)，和为 1.0；含决策依据注释。去锚：复核 bottleneck.md 已是**刻度锚定**（每维 0-2/3-4/…/9-10 段均有客观标准，如 CR3/HHI/认证周期/专利数），无随意示例数值可去；supplier_eval prompt 的示例 JSON 已带"数值仅为格式示例"免锚声明——故"去锚"本项 N/A。test_bottleneck 两处硬编码权重期望已同步更新，24 测试全绿 |
| P2-5 | VIP C-4b LLM 复盘 | 投资 | P2 | 中(待数据) | ⬜ |
| P2-6 | 真回测框架 | 投资 | P2 | 大 | ⬜ |
| P2-7 | God 模块拆分 | 工程 | P2 | 大(顺手) | ⬜ |

---

## 五、一句话结论

这是一套**决策闭环与风控真跑通、工程卫生与数据诚实度均在水准之上**的可辅助系统。P0（一行修 + 资金测试）先堵回归风险，P1（回测正名 + 仓位接活 + 前端反馈/口径）除掉"自欺与误导"，即可从"敢辅助"稳步走向"敢信任"。**真回测与港股支持是另一量级工程，诚实地留在 P2 单独排期。**
