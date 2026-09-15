# BottleneckHunter 量化研发提升方案（2026-09）

## 目标
建立可复现、可校准、可审计的 Research / Signal / Portfolio / Execution / Evaluation 五引擎体系，降低前视偏差、重复计权和风险低估，证明 LLM 与多 Agent 的真实增量价值。

## 原则
- UTC 存储、Asia/Shanghai 展示与调度；严格用户和市场隔离。
- 先数据可见性和可复现性，再评分、组合和执行；禁止用默认值掩盖缺失。
- LLM 负责研究组织、事件抽取和解释；确定性风险模型和组合优化器负责约束与执行。
- 每个阶段必须有自动化测试、回归测试、数据质量报告和审计日志。

## 实施与验收矩阵

状态含义：⬜ 未开始；🔄 实施中；✅ 已完成并通过门禁；⏸️ 阻塞。

| 编号 | 阶段/子项 | 状态 | 依赖 | 实施文件（候选） | 测试策略 | 验收标准 | 回滚方式 |
|---|---|---|---|---|---|---|---|
| P0-0 | 冻结策略、数据源、版本与基线 | ✅ | 无 | 本文档、开发日志 | pytest 全量、ruff 范围基线 | 基线可复现并记录真实输出 | 仅回滚文档提交 |
| P0-1 | 统一数据契约与时间语义 | ✅ | P0-0 | `watchlist/` 数据模型、契约模块 | 模型边界、单位/时区/缺失值测试 | 字段含来源、所属期、生效/可见/采集时间 | 删除新增契约模块/迁移 |
| P0-2 | 研究快照与来源观测持久化 | ✅ | P0-1 | `store_schema.py`、研究 Store | SQLite 迁移、隔离、幂等回归 | 快照不可变、来源可追溯、用户/市场隔离 | 保留旧表，停用新表读取 |
| P0-3 | decision/signal/portfolio/execution/review 绑定快照 | ✅ | P0-2 | `store_*`、决策/执行模型 | 写读链路、旧数据兼容、字段完整性 | 新记录强制 `snapshot_id` 与 `strategy_version` | 读取兼容旧记录，回退旧写入 |
| P0-4 | PIT 可见性、泄漏检测、缺失/降级审计 | ✅ | P0-1/P0-2 | `watchlist/pit_gate.py`、`store_research_snapshot.py` 门禁读取 | 人工构造未来数据、修订、缺失、降级场景 | 不可见观测被拒绝；缺失不静默填充 | 关闭门禁仅限离线迁移，不得生产绕过 |
| P0-5 | 事件驱动回测核心 | ✅ | P0-1/P0-4 | `watchlist/event_backtest.py`（复用 slippage/performance） | 事件顺序、停牌、退市、现金、成本、滑点 | 与模拟成交回放明确分层；结果可复现 | 保留现有模拟盘回放入口 |
| P0-6 | walk-forward、样本外、消融与统计指标 | ✅ | P0-5 | `watchlist/evaluation.py`（numpy 固定种子，不依赖 scipy） | 合成数据、边界样本、置信区间回归 | 输出标准指标及区间，禁止未来数据 | 仅回滚评估入口，不删历史结果 |
| P1-1 | 概率化信号与校准 | ✅ | P0-6 | `watchlist/signal_calibration.py`（等序回归 PAV，纯 numpy） | Brier、Log loss、ECE、曲线测试 | 校准集与测试集隔离，概率范围合法 | 保留原分数读取兼容 |
| P1-2 | 评委分组、相关性与有效独立性 | ✅ | P0-6 | `watchlist/judge_independence.py`（相似度/N_eff/冗余校正，纯 numpy） | 重复评委、相关信号、权重稳定性 | 权重不以人数简单相加 | 回退旧聚合器 |
| P1-3 | 特征定义、依赖图、重复计权检测 | ✅ | P1-2 | `watchlist/feature_graph.py`（graphlib 环检测/来源回溯，纯 stdlib） | 环检测、重复特征、缺失依赖 | 每个特征可追溯来源与依赖 | 禁用检测只限开发诊断 |
| P1-4 | 风险预算组合与约束 | ✅ | P0-6/P1-3 | `watchlist/portfolio_budget.py`（单票/行业/因子/链条/流动性/CVaR/现金七维，纯 stdlib 诊断层） | 单票/行业/因子/链条/流动性/CVaR/现金 | 超限明确拒绝或降级，不静默放行 | 保留现有风险摘要入口 |
| P2-1 | 多市场交易规则、冲击与订单状态机 | ✅ | P0-5/P1-4 | `watchlist/execution_rules.py`（市场规则表 + 订单状态机 + 部分成交/撤单，纯 stdlib 叶子层） | A/H/美股、价差、部分成交、撤单 | 订单状态单调且成交可审计 | 回退现有成交约束 |
| P2-2 | LLM 输入输出与成本审计 | ✅ | P0-2/P0-3 | `watchlist/llm_audit.py`（成本模型 + 单次调用不可变审计记录 + 汇总，纯 stdlib 叶子层） | prompt、快照、版本、耗时、成本、覆写 | 每次调用可关联用户、策略和快照 | 停止新审计写入，保留调用 |
| P2-3 | LLM、多 Agent、投委会增量消融 | ✅ | P0-6/P1-2/P2-2 | `watchlist/incremental_ablation.py`（组件阶梯增量消融 + N_eff 独立性守卫，复用 evaluation/judge_independence，纯计算叶子层） | 严格样本外、置信区间、重复实验 | 证明增量或明确无增量，不以 persona 数量代替独立性 | 不改变线上默认决策 |
| P2-4 | 质量门禁、审计报表、回滚与部署验收 | ✅ | 全部 | `watchlist/release_gate.py`（发布门禁+迁移演练+回滚预案+验收报表，纯计算叶子层，不接线部署） | 快/全量测试、ruff、迁移演练、验收清单 | 门禁失败阻止发布，回滚步骤经演练 | 使用上一稳定镜像/提交 |

## 每个子项的统一门禁
1. 明确改动范围和不纳入范围，保护既有工作区修改。
2. 实现最小可用变更，优先复用现有 Store、模型、风险与执行能力。
3. 增加单元测试、集成测试和必要的回归测试。
4. 先跑相关快测试，再跑全量 `python -m pytest -q`；失败必须修复并重跑。
5. 对本方案新增/修改范围执行 `ruff check`；`.agents/` 等既有无关目录单独记录，不擅自修改。
6. 记录测试输出、数据质量、已知限制和回滚点。
7. 只有满足验收标准才将状态改为 ✅。

## 真实基线（2026-09-14）
- 全量测试：`python -m pytest -q` → **1543 passed, 4 skipped**，退出码 0，耗时 375.74 秒。
- 全仓 Ruff：退出码 1；主要由既有未跟踪 `.agents/` 技能副本中的无效 noqa、未排序导入、无占位符 f-string 和超长行导致。本方案不修改或提交该目录。
- 测试隔离：已抽查测试普遍显式使用 `tmp_path`/临时 `db_path`；仍需完成全量 fixture 审计，禁止依赖 `WATCHLIST_DB` 静默写生产库。

## 当前状态
P0-0 至 P0-6 与 P1-1、P1-2、P1-3、P1-4、P2-1、P2-2、P2-3、P2-4 已全部完成并通过全量门禁（P2-4 全量 `2031 passed, 4 skipped`，退出码 0）；全部子项已完成，转入最终代码审查、验收与推送阶段。现有风险指标、决策闭环、真实快照成交、数据源降级和多用户/市场隔离可复用；概率化信号校准已落地（`signal_calibration.py`：Brier/LogLoss/ECE/可靠性曲线 + 等序回归校准，校准/测试隔离），评委有效独立性度量已落地（`judge_independence.py`：投票相似度 + N_eff + 冗余校正权重，相关/重复评委不再线性叠加），特征依赖图与重复计权检测已落地（`feature_graph.py`：环检测/缺失依赖/重复特征/来源回溯，纯 stdlib 诊断层），组合层风险预算已落地（`portfolio_budget.py`：单票/行业/因子/链条/流动性/CVaR/现金七维预算，超限拒绝或降级+建议缩仓，纯 stdlib 诊断层，与逐笔校验/描述性摘要互补），多市场交易规则与订单状态机已落地（`execution_rules.py`：各市场 tick/lot/涨跌停/T+1 规则表 + 单调订单状态机 + 部分成交 + 撤单 + 可审计成交记录，纯 stdlib 叶子层，复用 slippage/COST_CONFIG 概念但不接线进生产成交链），LLM 输入输出与成本审计已落地（`llm_audit.py`：确定性 token→USD 定价表 + 单次调用不可变审计记录 LlmCallAudit 把 用户/策略/快照 + prompt 哈希/model/版本 + 耗时/token/成本 + 人工覆写 收敛为一条可查询条目 + 汇总报表 + 密钥遮蔽，纯 stdlib 叶子层，与按日预算账/健康遥测/provenance 互补但不接线进生产 LLM 调用链），LLM/多智能体/投委会增量消融已落地（`incremental_ablation.py`：组件阶梯逐级增量消融，相邻臂复用 `evaluation.ablation` 算严格样本外 delta + bootstrap 区间 + 跨 0 显著性，多种子重复取「显著且跨种子稳定」才算证明，跨种子一致不显著且区间窄贴 0 判「明确无增量」，否则 inconclusive；独立性守卫按 N_eff/人数 判 persona 冗余膨胀，杜绝以评委人数冒充独立信号，复用 `judge_independence` 的 N_eff；纯计算叶子层，不接线进生产决策链，回退=不调用）；发布质量门禁、迁移演练、回滚与部署验收已落地（`release_gate.py`：`evaluate_release` 汇总门禁项、任一阻断项失败即裁决 block 绝不放行，`migration_drill` 在内存 SQLite 上把迁移 SQL 真跑两遍验应用与幂等、坏迁移与非幂等均被抓出且不碰生产库，`build_rollback_plan`/`validate_rollback_plan` 按 `deploy.sh` 机制生成并演练「使用上一稳定镜像/提交」的回滚预案（含健康检查与容器内证真，落实「容器烘焙源码、git reset/restart 不上线代码须重建」教训），`acceptance_report`/`render_checklist` 复用 `llm_audit.summarize` 拼装结构化验收报表与人读验收清单；纯 stdlib 叶子层，不接线进生产部署链，回退=不调用）。全部子项完成，待最终代码审查 + 验收工具全面复核后按授权推送主线。已有工作区修改和未跟踪文件不属于本方案，实施时不得覆盖、删除或擅自提交。

## 复用现有能力
复用 `StandardQuote`、`safe_float`、`WatchlistStore.for_user().for_market()`、现有 provenance、`compute_portfolio_risk`、真实行情成交约束、`phase_cache`、现有 pytest/ruff 配置和 `deploy.sh`，不重复建设同类基础设施。

## 最终交付
完成所有子项后，必须运行全量测试、范围 Ruff、代码审查和验收工具，生成开发日志与验收报告；检查工作区后仅提交本方案相关文件，并按授权推送主线。提交信息需包含行首独立 `📢` 白话行，并以规定的 Co-Authored-By 行结尾。
