"""P0-1 实施记录。"""

# 2026-09-14

## P0-1：统一数据契约与时间语义

- 状态：🔄 实施中；初版通过门禁，冻结模型及非有限数校验补强待重新验证。
- 新增 `watchlist/data_contracts.py`：统一描述所属期、生效、可见、采集时间，并强制时区与 UTC 归一化。
- 新增来源、单位、币种、质量、降级说明和修订版本字段。
- 契约使用 Pydantic v2，禁止未知字段，模型冻结，禁止 NaN/Infinity。
- 新增 `tests/test_data_contracts.py`，覆盖 UTC 转换、无时区拒绝、所属期边界和未知字段拒绝。

### 门禁结果

- 专项测试：`3 passed`。
- 范围 Ruff：通过。
- 全量测试：`1546 passed, 4 skipped`，退出码 0。
- 既有全仓 Ruff 基线问题仍限定在无关 `.agents/` 等目录，本项未修改。

### 已知边界

## P0-2：研究快照与来源观测持久化

- 状态：✅ 边界修复已恢复，专项门禁通过；冻结基线 恢复后专项 `18 passed`、全量 `1561 passed, 4 skipped`。P0-3 全量 `1732 passed, 4 skipped` 复核后再次确认无回归。
- 新增 `watchlist/research_contracts.py`：不可变研究快照与来源观测契约。
- 新增 `watchlist/store_research_snapshot.py`：快照原子写入、typed 观测读取、用户/市场隔离和 fail-closed 读取。
- `store_schema.py` 新增研究快照、来源观测表及 UPDATE/DELETE 不可变触发器。
- 保留既有 `_AIModelsMixin`，避免破坏 WatchlistStore 原有能力。

### 门禁结果

- 初版专项测试：`7 passed`（含 P0-1）；覆盖不足，不能证明完整不可变性。
- 初版全量测试：`1547 passed, 4 skipped`，退出码 0；运行时间早于新增 SQLite 专项测试，不作为最终版本验收。
- 边界补强后曾误用旧 worktree 覆盖三个文件，专项测试暴露 `9 failed, 5 passed`；随后原代理在共享工作区恢复，未再次复制旧文件。
- 恢复后专项测试：`18 passed in 7.54s`（代理在共享工作区执行）。
- 恢复后研究契约、快照 Store、两个专项测试文件 Ruff：通过；`store_schema.py` 全文件仍有代理报告的 5 个既有 E501，未修改无关行。
- 恢复后的全量 `python -m pytest -q`：`1561 passed, 4 skipped in 468.59s`，退出码 0。此结果对应恢复后的共享工作区版本。

### 已知边界

- 旧 `market_snapshots` 表仍保持原有写入语义，未为历史旧数据伪造 PIT 字段。
- 快照 ID 当前由数据库全局主键约束；读取同时按用户和市场过滤。
- P0-4 将继续负责 PIT 可见性、未来数据泄漏、缺失和降级审计。

## P0-3：决策链快照绑定

- 状态：✅ 强制绑定与旧记录兼容闭环完成，全量门禁通过。
- 新增 `watchlist/snapshot_binding.py`：`bind_snapshot` 校验边界，`strict=True` 默认强制非空 `snapshot_id`+`strategy_version` 且校验当前用户/市场下快照存在、`strategy_version` 匹配；`strict=False` 仅供显式 legacy 入口写双 NULL。
- 新增 `watchlist/stage_snapshot.py`：`save_stage_snapshot(store, stage, inputs)` 在各阶段实际输入读取结束后封存快照，返回稳定 `strategy_version="decision-p0-3-v1"`。
- `store_schema.py` 为 `macro_strategies`、`strategic_plans`、`tactical_plans`、`execution_plans`、`committee_reviews`、`committee_consensus`、`trade_feedback`、复盘落点增加 nullable `snapshot_id`/`strategy_version` 及索引；历史行保持 NULL，不回写、不伪造快照。
- `store_decision.py`、`store_committee.py`、`store_simtrading.py` 的 create/reject/feedback/review 路径改为 keyword-only 绑定并 strict 默认拒绝缺绑定的新生产写入；读取优先返回本行绑定，旧记录沿父链解析，断链返回 `legacy_unbound` 而非异常。
- `decision_engine.py`、`committee.py` 在 L1→L4 各阶段与投委会评审前封存实际输入快照；同阶段多 ticker 共享快照，L3 重跑生成新快照，被风控/投委会拒绝的执行计划仍保留绑定。
- `macro_consultation.py` 修复：新生成内容不再写入过期/未绑定的原始快照。

### 门禁结果

- 受影响四模块合并复跑：`36 passed`（rejection/discipline/committee_quorum_freshness/provenance）。
- 新增 `tests/test_rejection_snapshot_validation.py` Ruff：`All checks passed!`；四个主改测试文件未引入新诊断，其余为未触碰行的既有 E501。
- 全量 `python -m pytest -q`：`1732 passed, 4 skipped in 601.80s`，退出码 0。

### 已知边界

- `visible_at` 缺失不从 `collected_at` 推断；严格 PIT 泄漏门禁留待 P0-4。
- 旧 `market_snapshots` 覆盖式兼容语义未改。
- LLM prompt/provider/cost 审计属 P2-2，未在本阶段实现。

## P0-4：PIT 可见性门禁、泄漏检测与缺失/降级审计

- 状态：✅ 门禁接入生产读取路径，专项与全量门禁通过。
- 新增 `watchlist/pit_gate.py`：三条边界一处收口。
  - **可见性**：`assert_observations_visible` / `assert_snapshot_visible` 拒绝决策时点尚不可见的观测（`future_data_leak`）；决策时点早于快照 `created_at` 时拒绝（`snapshot_not_yet_created`，用未来才存在的快照做过去的决策即泄漏）；决策时点必须带时区（`naive_time`）。
  - **泄漏**：采集早于所属期结束（`collection_before_period_end`）、采集早于可见（`collected_before_visible`）、同 ticker+指标修订号倒流或可见时间非单调（`revision_regression` / `revision_time_regression` / `revision_visible_conflict`）。修订单调性逐条与"已接受的最新修订"比较，不取全集合极值，避免后出现的低修订号被掩盖。
  - **缺失/降级**：`audit_snapshot` 产出 `AuditReport`（`missing` / `degraded` / `ok` / `usable`）。缺失值必须带已知来源标记（`not_available` / `not_disclosed` / `provider_gap`），否则拒绝（`missing_data`），绝不用默认值静默填充；降级质量（`degraded/stale/estimated/fallback/partial` 及未知标签）只记录不阻断但必须让调用方看见；空阶段捕获记为降级（历史上下文缺口），结构损坏 payload（非 JSON / 非对象）记为缺失并拦截。
  - `gate_snapshot` 为统一入口：先过可见性与泄漏，再做缺失/降级审计。
- 修改 `watchlist/store_research_snapshot.py`：新增 `get_visible_research_observations(snapshot_id, *, decision_at=None)`，在返回观测前先跑 `assert_snapshot_visible`，使生产读取路径物理上无法泄漏未来数据；快照不存在返回空元组，不伪造。
- 迁移旁路 `migrate(reason)` 上下文管理器：仅限离线迁移，空/纯空白原因拒绝（`bypass_without_reason`），作用域受限、异常时自动关闭，无生产常开开关；默认 `migration_bypass_active()` 为 False。

### 本阶段修复的四个真实缺陷

- `snapshot_not_yet_created` 判据方向写反：原 `if created_at < cutoff` 恰好相反——较晚的决策时点是正常的（用已存在的快照做决策），较早才是拿未来快照做过去决策。改为 `if cutoff < created_at`。
- 修订单调性用 `max()` 取极值掩盖倒流：改为逐条与"上一条已接受修订"比较；并把该检查移入 `assert_observations_visible`，此前仅 `assert_snapshot_visible` 可达，观测级测试打不到。
- 空捕获与损坏 payload 判定颠倒：空 payload 曾错误阻断、损坏 payload 曾错误放行；现损坏 payload 以 `missing_data` 拦截，空 payload 记降级。
- 脏数据注入测试：最终用 `StageInputCapture.model_construct(captured_at=BASE, …)`（datetime 而非 str）+ `snapshot.model_copy(update={"captures": (...)})` 模拟绕过 Pydantic 校验的历史脏行。

### 门禁结果

- 专项测试 `python -m pytest tests/test_pit_gate.py -q`：`32 passed in 3.83s`。
- P0-4 范围 Ruff `ruff check pit_gate.py store_research_snapshot.py tests/test_pit_gate.py`：`All checks passed!`。
- 全量 `python -m pytest -q`：`1764 passed, 4 skipped in 676.16s`，退出码 0。

### 已知边界

- 门禁只在研究快照读取路径（`get_visible_research_observations`）强制接入；`market_snapshots` 旧覆盖式语义未改。
- `visible_at` 仍不从 `collected_at` 推断；缺失可见时间的数据由契约层与降级审计处理，不猜测历史可见性。
- 事件驱动回测对门禁的调用留待 P0-5 接入。

## P0-5：事件驱动回测核心

- 状态：✅ 与模拟盘复盘明确分层，专项/回归/全量门禁通过。
- 新增 `watchlist/event_backtest.py`：给定历史 bar 与决策日订单，按交易日历逐日推进的确定性回测引擎。
  - **结构性 PIT（无前视）**：第 T 日信息产生的订单只能在 `decision_date < day` 的首个可交易日成交（下一交易日规则），成交价取该成交日 bar，绝不回看更晚价格。`test_no_lookahead_price_is_from_fill_day`、`test_order_on_last_day_never_fills` 锁死此边界。
  - **交易日历**：由所有 ticker 的 bar 日期并集排序生成；每日按 `退市清仓 → 订单成交 → 盯市` 三步推进。
  - **停牌**：`halted` 当日不可成交，订单顺延至下一可交易日（`still_pending`），持仓按最后有效价盯市。
  - **退市**：`delisted` 当日优先强制清仓（以当日 close，不加滑点，`trade_type="delisting_liquidation"`），并将该 ticker 标记为已退市；此后针对它的订单以 `ticker_delisted` 拒绝。
  - **现金约束**：买入前校验 `amount + commission <= cash`，不足以 `insufficient_cash` 拒绝，现金绝不为负；卖出无持仓以 `no_position` 拒绝，超持仓按当前持仓封顶。
  - **成本**：`COST_CONFIG` 按市场配置佣金 + 卖方印花税（校准旋钮，非硬编码事实）；`_commission` 卖方叠加 `stamp_sell_bps`，入账按 4 位小数。滑点复用现有 `slippage.calc_slippage`。
  - **可复现**：无随机、迭代有序、订单按 `(decision_date, ticker, side)` 稳定排序；相同输入必得逐字段一致结果（`test_deterministic_reproducibility`）。
  - **指标接入**：收盘计算净值曲线，末日调用现有 `performance.compute_metrics`，与既有绩效口径一致。
- 分层保护：不改动 `backtest.py` 的"模拟盘复盘"（回放系统自身已发生的 `sim_trades`）；本模块是"信号 → 订单 → 成交"的独立事件引擎，回滚只需移除新模块，现有模拟盘回放入口不受影响。

### 门禁结果

- 专项测试 `python -m pytest tests/test_event_backtest.py -q`：`11 passed`。
- 受影响回归（watchlist 回测/绩效/滑点相关）：`42 passed`。
- P0-5 范围 Ruff `ruff check event_backtest.py tests/test_event_backtest.py`：`All checks passed!`。
- 全量 `python -m pytest -q`：`1775 passed, 4 skipped in 401.97s`，退出码 0。

### 已知边界

- 引擎按"当日参考价（close）+ 滑点"成交，不建模盘中撮合、部分成交与订单状态机——这些属于 P2-1 多市场交易规则范围。
- 尚未在引擎内接入 P0-4 PIT 门禁校验 bar/订单来源可见性；当前依赖调用方传入的 bar 已是可见数据，快照级门禁接入留待后续风险/执行子项。
- `COST_CONFIG` 为近似费率旋钮，接实盘费率（分档佣金、最低佣金、过户费等）时在此校准。

## P0-6：walk-forward / 样本外 / 消融 / 统计区间评估

- 状态：✅ 严格样本外评估层落地，专项/范围 Ruff/全量门禁通过。
- 新增 `watchlist/evaluation.py`：在事件驱动回测（P0-5）之上做严格样本外评估，不引入 scipy，全部用 numpy 固定种子生成器，确定可复现。
  - **`walk_forward_splits`**：按时间滚动切分 train/test 窗口，日期先去重排序，`step` 默认 = `test_size`（测试窗不重叠）。任一窗口若 `train[-1] >= test[0]` 立即抛错——从结构上杜绝训练窗触及测试期或其后信息（前视泄漏）。数据不足返回空列表，`train_size/test_size/step` 非正抛 `ValueError`。
  - **`bootstrap_ci`**：对任意统计量做有放回重采样置信区间；`np.random.default_rng(seed)` 固定种子 → 相同输入必得相同区间。单样本退化为点估计（low=point=high），空样本或 `confidence` 越界抛 `ValueError`。
  - **`ablation`**：基线 vs 变体的统计量差 + 差值 bootstrap 区间。两序列等长走配对差重采样（同一逐窗样本外收益），否则走各自独立重采样差。区间不跨 0（`CI.excludes_zero`）才判显著，并给出 `positive/negative/inconclusive` 方向；不显著即 inconclusive，不夸大结论。供 P2-3 证明或否定 LLM/多 Glob 的真实增量复用。
  - **`summarize_oos`**：把逐期样本外收益聚合成标准指标（均值/年化/波动率/夏普，无风险=0 简化口径）并附均值 bootstrap 区间。

### 本阶段修正的两处测试问题（非代码缺陷）

- `test_a_stock_stamp_duty_on_sell`（P0-5 遗留）：断言容差过紧（`rel=1e-6`）与 `_commission` 有意的 4 位小数入账冲突；改为 `abs=1e-4`。佣金按 4 位小数入账是刻意口径，非 bug。
- `test_ablation_inconclusive_when_noise_dominates`：原用 seed=7 的两组独立 40 样本 N(0,1)，有限样本本就可能偶然相差约 2σ，`ablation` 正确判显著——是测试数据缺陷而非代码错误。改为 `test_ablation_inconclusive_when_no_net_effect`，用对称零和扰动（变体 = 基线 ±0.02 交替）使净差恒为 0，保证 inconclusive，稳定验证"无净效应即不显著"。

### 门禁结果

- 专项测试 `python -m pytest tests/test_evaluation.py -q`：`17 passed in 0.59s`。
- P0-6 范围 Ruff `ruff check evaluation.py tests/test_evaluation.py`：`All checks passed!`（`zip(bi, vi, strict=True)` 满足 B905）。
- 全量 `python -m pytest -q`：`1792 passed, 4 skipped in 556.79s`，退出码 0。

### 已知边界

- 本层是纯统计评估函数，尚未接线到具体信号/决策数据；与实际回测收益的对接留待 P1/P2 各评估子项按需调用。
- 夏普用无风险=0 的简化口径；年化用 `(1+mean)**periods_per_year - 1` 的几何近似，接实盘时按需替换为对数收益或含无风险利率口径。
- `ablation` 的独立重采样分支适用于不等长样本；配对分支要求等长且逐元素对齐（同一批样本外窗口），调用方需保证语义匹配。

## P1-1：概率化信号与校准

- 状态：✅ 概率校准层落地，与共识加权明确分层，专项/范围 Ruff/全量门禁通过。
- 新增 `watchlist/signal_calibration.py`：把原始信号分数映射为校准概率，并度量校准质量。不引入 scipy，纯 numpy 确定可复现。
  - **Brier / LogLoss**：概率预测整体误差。`brier_score = mean((p-y)^2)`（完美 0、最差 1）；`log_loss = -mean(y·log p + (1-y)·log(1-p))`，`p` 先钳到 `[eps, 1-eps]` 防 `log(0)` 溢出。两者入口都强校验：概率必须落在 `[0,1]`、结果必须为 0/1、形状一致、非空，否则 `ValueError`——从源头拒绝非法概率。
  - **可靠性曲线 + ECE**：`reliability_curve` 把 `[0,1]` 等宽分桶，返回每桶预测均值（置信度）与实际正例率（准确率），空桶记 `nan`+`count=0`；末桶取闭区间纳入 `p==1.0`。`expected_calibration_error` 按各桶样本占比加权 `|实际频率 - 预测均值|` 求和，量化过/欠自信。
  - **`IsotonicCalibrator`（等序回归 PAV）**：用 Pool Adjacent Violators 把"原始分数 → 经验正例率"拟合成单调非降映射；`fit` 先按唯一分数聚合正例率与样本数再做加权等序回归，`predict` 用分段线性插值、越界钳到端点，输出恒在 `[0,1]`。无随机、相同输入必得相同映射。
  - **校准/测试隔离**：`calibrate_and_evaluate` 只在校准集 `fit`、在独立测试集 `apply` 并度量 Brier/LogLoss/ECE/可靠性曲线，从结构上避免用测试集信息调参。
- 分层保护：与 `model_calibrator.py` 明确分层——后者是 AI 模型共识"准确率 → 权重"的加权（consensus calibration_weight），本模块是"原始分数 → 校准概率"的概率校准，两者互不依赖、互不 import。回滚只需移除新模块，既有分数读取与共识加权入口不受影响（保留原分数读取兼容）。

### 门禁结果

- 专项测试 `python -m pytest tests/test_signal_calibration.py -q`：`22 passed in 0.60s`。
- 受影响回归（评估/事件回测/打分模型 sibling 模块）`test_evaluation.py test_event_backtest.py test_models.py`：`48 passed`（本模块未改动既有代码，此为预防性回归）。
- P1-1 范围 Ruff `ruff check signal_calibration.py tests/test_signal_calibration.py`：`All checks passed!`。
- 全量 `python -m pytest -q`：`1814 passed, 4 skipped in 369.34s`，退出码 0（= P0-6 基线 1792 + 22 项 P1-1 专项）。

### 已知边界

- 本层是纯概率校准与度量函数，尚未接线到具体信号来源；现有 `chain/models.py` 打分为 0-10 分制、`model_calibrator` 记录为二元 `is_correct`，把它们喂入校准器需调用方先归一/配对，留待 P1/P2 各评估子项按需接线。
- 等序回归是保序（单调）校准，只纠正单调错配的过/欠自信，不重排分数序；若原始分数与结果非单调相关，需先修分数本身而非依赖校准。
- ECE 用等宽分桶（非等频/自适应桶）；桶数 `n_bins` 为口径旋钮，样本极少时分桶估计噪声大，调用方按样本量选桶。

## P1-2：评委分组、相关性与有效独立性

- 状态：✅ 有效独立性度量层落地，与 `committee._fallback_consensus` 明确分层，专项/范围 Ruff/全量门禁通过。
- 背景缺陷：投委会加权表决 `committee._fallback_consensus` 按「历史权重 × 票数」线性相加（`w_approve += w`）。当多位评委高度相关（同源模型、历史投票同步、甚至重复评委）时，线性相加把同一份信号重复计数，高估共识强度——正是本子项验收「权重不以人数简单相加」所指。
- 新增 `watchlist/judge_independence.py`：由历史投票向量度量评委相关性并做有效独立性权重校正。不引入 scipy，纯 numpy 确定可复现。
  - **投票数值映射**：与 `committee._VALID_VOTES` 对齐——赞成族（`approve`/`approve_with_modification`）→ +1，`reject` → -1，`abstain`/未知 → 0。
  - **`vote_similarity_matrix`**：两两 Pearson 相关钳到 `[0,1]`（对称、对角 1）。**负相关=真分歧，钳到 0 视作独立，不做反向增益**（`ponytail` 标注的刻意口径）。零方差（恒定投票）Pearson 未定义时退化为逐元素一致率：恒定且相同→1，否则按一致比例。
  - **`effective_number_of_judges`**：`N_eff = N² / Σ相似度`。k 个完全相同评委 → N_eff=1，全独立 → N_eff=N，量化「名义人数 vs 有效独立人数」的落差。
  - **`independence_weights`**：每位评委权重除以其冗余簇规模（相似度行和）：`eff_i = base_i / Σⱼ sim[i,j]`。k 个相同评委合计有效权重 = 单个评委权重，而非 k 倍——从结构上杜绝相关评委线性叠加。支持自定义 `base_weights`（对齐 `committee._member_weights` 的历史校准权重），非负校验。
  - **`weighted_approval`**：给定票与权重的加权赞成/反对质量与赞成率（与 committee 口径一致：赞成族 vs 反对，弃权不计入分母），便于对比朴素计票 vs 有效独立计票。
  - **`analyze_independence`**：一站式 `IndependenceReport`（roles/n_members/n_effective/base_weights/effective_weights/redundancy）。
- 分层保护：本模块只做度量与权重校正，**`_fallback_consensus` 的既有等权/加权表决保持不变**，也未接线进 `committee.py`（回退旧聚合器只需不调用本模块）。与 `signal_calibration.py`（P1-1，分数→概率校准）、`model_calibrator.py`（共识准确率→权重）互不依赖、互不 import，各司其职。

### 门禁结果

- 专项测试 `python -m pytest tests/test_judge_independence.py -q`：`23 passed in 0.59s`。
- 受影响回归（投委会绑定/持久化/法定人数 + P1-1/P0-6 sibling 评估模块）`test_committee_parent_binding.py test_committee_persistence_gate.py test_committee_quorum_freshness.py test_evaluation.py test_signal_calibration.py`：`49 passed`（本模块未改动既有代码，此为预防性回归）。
- P1-2 范围 Ruff `ruff check judge_independence.py tests/test_judge_independence.py`：`All checks passed!`（SIM108 用早返回而非三元/块内双赋值消解，保留零方差与钳制两条注释）。
- 全量 `python -m pytest -q`：`1837 passed, 4 skipped in 707.02s`，退出码 0（= P1-1 基线 1814 + 23 项 P1-2 专项）。

### 已知边界

- 本层是纯度量与权重校正函数，**尚未接线到 `committee.py` 生产表决**；把有效独立权重接入 `_build_consensus`/`_fallback_consensus` 需调用方先积累各评委历史投票向量（跨决策周期的投票序列），留待后续投委会评估子项（P2-3 增量消融）按需接线。
- 相关性用 Pearson 钳 `[0,1]`：只折叠正相关（同向冗余），负相关（真分歧）保留为独立，不做反向增益——这是刻意的保守口径，避免把「对着干」误当作额外独立信息。
- N_eff 与冗余校正基于历史投票序列长度一致的假设；序列过短时相关性估计噪声大，调用方需保证足够的历史样本（与 P0-6 walk-forward 的样本量口径一致）。

## P1-3：特征定义、依赖图与重复计权检测

- 状态：✅ 特征溯源与重复计权诊断层落地，纯 stdlib，专项/范围 Ruff/全量门禁通过。
- 背景缺陷：评分系统 `chain/supplier_eval.py` 的 `AlphaScorer.compute` 线性加权 5 维（市值/分析师/成交量/涨幅/机构持仓）+ 若干加分项；`FinalScorer.compute` 用 `quality^0.55 × alpha^0.45` 几何加权并在注释里断言「两者完全正交，无维度重叠」。但该正交性只是注释断言、无从校验；若两个特征其实源自同一原始信号（如 `volume_ratio` 与 `consecutive_volume_days` 都来自成交量）却各自计权，就把同一份信息重复计数——与 P1-2 评委相关性同构，只是换到「特征」这条轴。
- 新增 `watchlist/feature_graph.py`：把特征来源与依赖显式化，并把「正交」断言变成可校验的诊断。纯 stdlib（`graphlib`/`dataclasses`/`collections`），不引入 numpy/scipy，确定可复现。
  - **`FeatureSpec`**：每个特征声明 `name`、`sources`（原始来源键集合，叶子输入）、`depends_on`（上游特征名，构成图的边）、`weight`（加权汇总权重，0 = 中间特征不直接计权）。`feature()` 便捷构造把任意可迭代来源/依赖归一为 `frozenset`。
  - **`validate_graph`**：一次性校验三类问题——**环检测**（`graphlib.TopologicalSorter.prepare` 抛 `CycleError` 即回报环链，含自环）、**缺失依赖**（`depends_on` 指向未定义特征；`sources` 是外部叶子不算缺失）、**重复特征**（来源集合完全相同的不同名特征，即同一信号换名重复定义）。无环时同时产出拓扑序。同名特征直接 `ValueError` 拒绝而非静默覆盖。
  - **`resolve_sources`**：沿依赖图回溯任一特征的全部根来源（自身 `sources` ∪ 所有传递依赖的 `sources`），使验收标准「每个特征可追溯来源与依赖」可执行。已访问集合去重，天然防环/钻石依赖死循环。
  - **`detect_duplicate_weighting`**：找出各自带非零权重且共享根来源的特征对（`WeightOverlap`），精确定位线性加权时被重复计权的信号。`weight=0` 的中间特征不参与，避免误报。
  - **`analyze_features`**：一站式 `FeatureAudit`（图校验 + 重复计权检测）。
- 分层保护：**纯诊断层**，不改任何评分口径、不接线进 `supplier_eval`；`AlphaScorer`/`FinalScorer` 的既有权重与计算完全不变。回退方式=不调用本模块（「禁用检测只限开发诊断」）。

### 门禁结果

- 专项测试 `python -m pytest tests/test_feature_graph.py -q`：`15 passed in 0.61s`。
- 受影响回归（评分 sibling `alpha_scorer`/`final_scorer`/`models` + 两个新叶子模块 `feature_graph`/`judge_independence`）：`80 passed`（本模块未改动既有代码，此为预防性回归）。
- P1-3 范围 Ruff `ruff check feature_graph.py tests/test_feature_graph.py`：`All checks passed!`。
- 全量 `python -m pytest -q`：`1852 passed, 4 skipped in 557.61s`，退出码 0（= P1-2 基线 1837 + 15 项 P1-3 专项）。

### 已知边界

- 本层是纯诊断函数，**尚未接线到 `supplier_eval` 的实际特征清单**；要真正审计现网评分，需调用方把 `AlphaScorer` 的 5 维 + 加分项按其真实来源建成 `FeatureSpec` 图再喂入本模块，留待 P1-4 风险预算组合或后续评估子项按需接线。
- 重复特征判定按「来源集合完全相同」；部分重叠（如一个特征来源 ⊂ 另一个）不归为重复特征，但会被 `detect_duplicate_weighting` 的共享根来源检测捕获——两者互补。
- `graphlib` 每次只报一条代表性环；多环并存时修一条重跑暴露下一条（`ponytail` 标注），诊断场景够用，未做全环枚举。
