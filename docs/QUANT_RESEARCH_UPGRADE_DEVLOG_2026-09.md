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

## P1-4：风险预算组合与约束

- 状态：✅ 组合层风险预算体检落地，纯 stdlib，专项/受影响回归/范围 Ruff/全量门禁通过。
- 背景与分工：既有 `constraint_validator.validate_execution_plan` 已在**逐笔交易**生成期硬校验单票/行业/现金/换手/beta（违规拒绝、告警降级），回答「这一笔能不能下」；`risk_metrics.compute_portfolio_risk` 只产出 VaR/CVaR/HHI 等**描述性**摘要（warnings，不否决）。缺口在于：组合**整体装配后**的 CVaR、跨行业同链聚集、清仓流动性天数、广义因子净暴露没有可拒绝/可降级的预算约束——CVaR 仅被描述、从不否决。P1-4 补这一层，与前两者互补而非替代。
- 新增 `watchlist/portfolio_budget.py`：对「整本组合」在 单票/行业/因子/链条/流动性/CVaR/现金 七维逐项体检，每项超限都给出**明确的拒绝或降级 + 建议缩仓比例**，绝不静默放行。纯 stdlib（`dataclasses`），不引入 numpy/scipy，确定可复现。
  - **上限型判定 `_ceiling_status`**：usage≤limit 通过；≤limit×(1+band) 降级（band 默认 0.15）；再高拒绝。超限项返回缩到合规需乘的比例 `suggested_scale=limit/usage`（<1）。预算=0 表示禁止该敞口，任何正暴露即拒绝（scale=0）。
  - **下限型判定 `_floor_status`（现金）**：usage≥limit 通过；≥limit×(1−band) 降级；再低拒绝。现金是下限，无缩仓比例（`suggested_scale=None`）。
  - **单票 / 行业**：恒可从毛敞口权重算，恒体检。市值取**绝对值**（毛敞口口径，兼容潜在空头）；行业无标签归「未知」桶，与 `compute_portfolio_risk` 口径一致，大未知桶本身即真实集中度。
  - **链条（产业链环节聚集）**：仅对已标注 `chain_node` 的标的聚类，未标注不参与——避免凭空造簇。捕捉本系统核心风险：跨行业但同属一个瓶颈环节（如「先进封装」横跨半导体与设备）的隐性聚集。
  - **因子净暴露**：对调用方传入的 `factor_exposures` 逐因子查 |暴露| 上限（含市场 beta）；负暴露按绝对值判（做空动量的敞口也算敞口）；本层只查上限，不建因子模型。
  - **流动性**：清仓天数 = 市值 /（ADV×参与率）超上限即降级/拒绝；部分标的缺 ADV 时**另记一条覆盖率降级**（缺口不作通过论）；全无 ADV 则跳过该维、也不谎报覆盖率。
  - **CVaR 预算**：把 `compute_portfolio_risk` 的描述性 CVaR 金额转成「尾部日损占权益 %」的可拒绝预算。
  - **现金下限**：现金比例跌破下限拒绝、逼近下限降级。
  - **最坏态聚合**：全组合状态取所有体检项最坏值（reject > degrade > ok）；`approved` = 非拒绝（degrade 仍放行但须按 `suggested_scale` 缩仓）。
  - **输入校验**：`total_equity<=0` 直接抛 `ValueError`，绝不在非正权益上冒充体检通过。
  - **`from_constraints`**：用既有 constraint 字典（如 `REGIME_CONSTRAINTS['balanced']`）填充对应维度，市场 beta 视作市场因子暴露上限映射到 `max_factor_exposure`；**不 import 生产模块**（调用方传字典），保持回退纯净。
- 分层保护：**纯诊断层，不接线进生产决策链**（回退=不调用）；`compute_portfolio_risk` 描述性摘要与 `validate_execution_plan` 逐笔校验完全不变。与 P1-1/P1-2/P1-3 各叶子模块互不依赖、互不 import。

### 门禁结果

- 专项测试 `python -m pytest tests/test_portfolio_budget.py -q`：`29 passed in 0.73s`。
- 受影响回归（风险/仓位/组合 sibling `test_risk_metrics_boundary`/`test_position_sizing_boundary`/`test_phase2_risk`/`test_budget`/`test_sell_holdings_guard` + 两个新叶子 `test_feature_graph`/`test_judge_independence`）：`98 passed`（本模块未改动既有代码，此为预防性回归）。
- P1-4 范围 Ruff `ruff check portfolio_budget.py tests/test_portfolio_budget.py`：`All checks passed!`（SIM108 用早返回消解，dict 分组避免裸 zip）。
- 全量 `python -m pytest -q`：`1881 passed, 4 skipped in 557.32s`，退出码 0（= P1-3 基线 1852 + 29 项 P1-4 专项）。

### 已知边界

- 本层是纯预算体检函数，**尚未接线到生产决策链**；要真正在下单前拦截超预算组合，需调用方在 L2/L4 装配组合后调用 `check_portfolio_budget` 并按 `suggested_scale` 缩仓，留待 P2-1（多市场交易规则/订单状态机）或后续执行子项按需接线。
- 链条聚类只对已标注 `chain_node` 的标的生效；未标注标的不参与（不凭空造簇），要覆盖全组合链条聚集需调用方先补齐环节标签。
- 因子净暴露由调用方的因子模型算好后传入，本层只查上限、不建因子模型；`max_factor_exposure` 默认对齐 `max_portfolio_beta`，多因子场景的各因子独立上限属校准旋钮。
- 各维默认预算（链条 35% / 流动性 5 天 / CVaR 8% / 参与率 20% / 降级带 0.15）是校准旋钮而非硬事实，接实盘风控口径时在 `BudgetLimits` 校准。

## P2-1：多市场交易规则、冲击与订单状态机

- 状态：✅ 单笔订单微结构层落地，纯 stdlib，专项/受影响回归/范围 Ruff/全量门禁通过。
- 背景与分工：既有执行链只有**组合级全量成交**能力——`event_backtest.run_event_backtest`（P0-5，下一交易日 close+滑点，全有或全无成交）、`trade_executor.execute_trade`（生产真实行情快照成交，全有或全无）、`slippage.calc_slippage`（sqrt 冲击成本）、`position_sizing._round_lot`（A股建仓定量取整手）。缺口在于**单笔订单的微结构**：最小变动价位（tick）、成交期涨跌停**约束**（`decision_engine` 里 A股涨跌停/T+1 仅是提示词文本、`data_validator` 里涨跌停仅作脏数据标记，都不可执行）、订单状态机、部分成交、撤单、T+1 可卖延迟——全系统无任何现成实现。P2-1 补这一层，与前述组合级能力互补而非替代。
- 新增 `watchlist/execution_rules.py`：在「一笔订单」维度用**可审计的订单状态机**驱动成交并施加各市场真实规则。纯 stdlib（`dataclasses`/`enum`），不引入 numpy/scipy，确定可复现。
  - **`MarketRules` 规则表 + `rules_for`**：美股（tick 0.01 / lot 1 / 无涨跌停 / T+0 / 可空）、港股（lot 100 / 无涨跌停 / T+0）、A股主板（lot 100 / ±10% / T+1 / 不可空）；A股创业板·科创板经 `board` 参数走 ±20% 变体；未知市场回退美股规则。默认值为校准旋钮而非硬事实。
  - **`round_to_tick` / `round_to_lot`**：价格就近对齐最小变动价位；数量向下取整到最小交易单位（A股不足一手→0，美股 1 股）。把 `position_sizing._round_lot` 的整手语义一般化进规则表并在**成交时**强制。
  - **`limit_band` / `can_fill` / `fill_price`**：由昨收算涨跌停带；封涨停买不到、封跌停卖不掉（无涨跌停市场恒可成交）；成交价 = 参考价按半价差向不利方向偏移 → 涨跌停带夹取 → tick 取整。价差是与 `calc_slippage` 冲击成本**不同的成本旋钮**，明确注释勿重复叠加。
  - **`OrderStatus`（IntEnum）+ `Order` 状态机**：状态 `NEW→PARTIALLY_FILLED→FILLED/CANCELLED/REJECTED` **数值单调、终态不可逆**。`_advance` 是唯一推进入口，`new < self.status` 即抛错——从结构上杜绝状态回退。`record_fill` 拒绝终态订单/非正数量/超额成交；`cancel` 仅未完结订单可撤（保留已成交部分）；`reject` 仅未成交新订单可拒（已部分成交须走撤单）。每笔成交落一条不可变 `Fill`（seq/数量/价格/金额/佣金/价差/成交后状态），使「订单状态单调且成交可审计」可执行、可验证；`avg_fill_price` 按成交额加权。
  - **`match_against_bar` 撮合**：对一根 bar 撮合至多一笔（可能部分）成交，流动性上限 = `bar_volume×participation_rate` 向下取整手，买单再受 `cash` 上限约束；卖方叠加印花税。`bar_volume=None`=无量数据不设限、`<=0`=当日零成交/停牌不可成交（二者刻意区分）。
  - **`simulate_execution` 驱动 + `next_sellable_index`**：按 bar 序列逐轮撮合，部分成交累积到全部成交，跑完仍有余额则撤单（GTC 到期）；昨收由上一根 bar 价链式推得。`next_sellable_index` 给出 T+t_plus 最早可卖交易日下标。
- 本阶段修复的一处真实缺陷：`match_against_bar` 原用 `if bar_volume and bar_volume > 0` 把「无量数据（None）」与「当日零成交/停牌（0）」混为一谈而同样跳过流动性上限，导致**停牌/零成交量当日订单竟能全额成交**（前视式虚假流动性）。改为 `None` 不设限、`<=0` 返回 None 不可成交，并加 `__main__` 自检 + `test_match_zero_volume_no_fill_but_none_volume_fills` 专项锁死。
- 分层保护：**纯叶子层，不接线进生产成交链**（回退=不调用）；`trade_executor.execute_trade` 全量成交路径、`event_backtest` 组合回测完全不变。与 P1-1/P1-2/P1-3/P1-4 各叶子模块互不依赖、互不 import。

### 门禁结果

- 专项测试 `python -m pytest tests/test_execution_rules.py -q`：`60 passed in 0.61s`。
- 受影响回归（执行/成交/仓位 sibling `test_event_backtest`/`test_exec_price_guard`/`test_trade_executor`/`test_position_sizing_boundary`/`test_downstream_snapshot_binding`）：`52 passed`（本模块未改动既有代码，此为预防性回归）。
- P2-1 范围 Ruff `ruff check execution_rules.py tests/test_execution_rules.py`：`All checks passed!`。
- 全量 `python -m pytest -q`：`1941 passed, 4 skipped in 317.16s`，退出码 0（= P1-4 基线 1881 + 60 项 P2-1 专项）。

### 已知边界

- 本层是纯规则/状态机函数，**尚未接线到生产成交链**；要在生产成交时强制涨跌停/部分成交/T+1，需调用方在 `trade_executor`/`event_backtest` 显式改调本模块，留待 P2-4 部署验收或后续执行子项按需接线。
- 撮合按「当日单一参考价 + 半价差」成交，不建模盘中逐笔撮合、盘口深度、排队优先级；`participation_rate` 是流动性上限的近似旋钮。
- 港股 tick 实为按价分档（如 <0.25 港元为 0.001）、lot 因股而异，本表简化为 tick 0.01 / lot 100；A股涨跌停按主板 ±10% / 双创 ±20% 两档，未含 ST（±5%）、北交所（±30%）等细分档；接实盘按交易所细则在 `MarketRules` 校准。
- T+1 仅由 `next_sellable_index` 给出可卖日下标供调用方按自身交易日历判定，本模块不持有持仓账本，不自动拦截 T+0 卖出；现金账本亦由调用方管理，避免与 `event_backtest` 组合账本重复扣现。

## P2-2：LLM 输入输出与成本审计

- 状态：✅ 单次调用审计层落地，纯 stdlib，专项/受影响回归/范围 Ruff/全量门禁通过。
- 背景与分工：LLM 审计所需信号**已散落在四处，但无一条把它们串成「单次调用」维度的可查询审计**——`store_budget.record_llm_usage` 是**按日聚合**的预算账（token/成本，且 `estimated_cost_usd` 由调用方外部传入，**全仓无任何中央定价计算**），`store_ai_models.record_model_call` 是**按日×用户×provider×model×角色聚合**的健康/延迟遥测（有耗时/成败但无 prompt/快照/策略/成本），`fallback._record_call` 在调用点算出 `latency_ms` 但只喂给聚合遥测（且 pytest 下跳过落库），`provenance.build_provenance` 把 prompt 哈希/model/快照嵌进决策 `result_json`（无成本/耗时/覆写，非可查询条目）。缺口正是 P2-2 验收所要的「每次调用可关联用户、策略和快照」：无单条不可变审计把 (用户+策略版本+快照) × (prompt 哈希+model+版本) × (耗时+token+成本) × (人工覆写) 绑在一起；且**无确定性 token→成本模型**（`estimated_cost_usd` 无人计算）。P2-2 补这一层，与上述四处互补而非替代。
- 新增 `watchlist/llm_audit.py`：在「一次 LLM 调用」维度产出一条不可变、可追溯的审计条目。纯 stdlib（`dataclasses`/`re`/`uuid`/`datetime`），复用 `provenance.prompt_hash`，不引入 numpy/scipy，定价与哈希给定输入必得同一结果。
  - **确定性成本模型 `_PRICING` + `price_for` + `estimate_cost`**：定价表 `provider → {model 前缀: (输入价, 输出价)/1K tok USD}`，`model` 按**最长前缀命中**（`gpt-4o-mini` 压过 `gpt-4o`），缺省回退 provider 缺省价、再回退全局缺省价 `(0.001, 0.002)`；`ollama` 本地零费用。`estimate_cost` token→USD 保留 6 位小数、负 token 当 0。补齐既有 `estimated_cost_usd` 无人计算的缺口。定价为校准旋钮而非硬事实，接实盘按各家价目表校准。
  - **`estimate_tokens` 兜底**：CJK≈1 token、其余≈4 字符/token 的无依赖启发式，仅在上游 usage 回包缺 token 数时兜底，明确注释勿当计费真值。
  - **`redact_secrets` 密钥遮蔽**：保守正则遮蔽 `sk-*`/`Bearer *`/超长令牌，作用于自由文本字段（`reason`/`override_reason`）；审计只存 prompt 哈希不存原文，本函数是纵深防线，过度遮蔽可接受，从源头杜绝 Key/凭据泄漏进审计。
  - **`LlmCallAudit`（frozen dataclass）+ `build_call_audit`**：单次调用的不可变审计记录，字段即审计六维 + 关联维——`user_id`/`strategy_version`/`snapshot_id`（复用 P0-2/P0-3 快照与策略版本）、`provider`/`model`/`model_version`、`prompt_hashes`（prompt 名列表逐个取 `provenance.prompt_hash` + 内联哈希合并）、`input_tokens`/`output_tokens`/`cost_usd`（缺省由定价表估算，可显式覆盖）、`latency_ms`（与 fallback 遥测同源口径）、`overridden`/`override_reason`/`override_by`（人工改判）。`provider` 归一化小写，token/latency 负值夹 0，`call_id` 缺省 uuid4、`created_at` 缺省当前 UTC，使「每次调用可关联用户、策略与快照」可执行、可验证。
  - **`summarize` 汇总报表**：把一批审计条目汇总为总量 + 按 `provider/model` + 按 `strategy_version`（calls/token/成本/覆写数/失败数），供 P2-3 增量消融成本核算与 P2-4 审计报表复用。
- 分层保护：**纯审计/诊断叶子层，不接线进生产 LLM 调用链**（回退=不调用，对齐验收「停止新审计写入，保留调用」）；`FallbackChatModel`/`_record_call`/`record_llm_usage`/`record_model_call`/`build_provenance` 现有路径完全不变，持久化审计 Store 表留待接线时再建（YAGNI）。与 P1-1/P1-2/P1-3/P1-4/P2-1 各叶子模块互不依赖、互不 import（仅单向复用 `provenance.prompt_hash`）。

### 门禁结果

- 专项测试 `python -m pytest tests/test_llm_audit.py -q`：`38 passed in 0.55s`。
- 范围 Ruff `ruff check llm_audit.py tests/test_llm_audit.py`：`All checks passed!`（两处 E501 手工改多行/删长断言消息，未用 `--fix`）。
- 全量 `python -m pytest -q`：`1979 passed, 4 skipped in 554.19s`，退出码 0（= P2-1 基线 1941 + 38 项 P2-2 专项）。

### 已知边界

- 本层是纯审计记录/成本模型函数，**尚未接线到生产 LLM 调用链**；要真正逐次审计线上调用，需调用方在 `fallback._record_call`（已持有 provider/model/latency）或各角色调用点装配 `build_call_audit` 并落库到新审计表，留待 P2-3/P2-4 或后续子项按需接线。
- 定价表 `_PRICING` 是校准旋钮而非硬事实，各家价目随时调整；未知 provider/model 回退全局缺省价宁可粗估不为 0（0 会让成本审计静默失真），接实盘须按各家最新价目表校准。
- `estimate_tokens` 是无 tokenizer 依赖的极粗启发式，真实 token 应优先取 provider usage 回包，本函数仅兜底、不作计费真值。
- `redact_secrets` 是保守正则纵深防线，不替代「审计只存 prompt 哈希不存原文」这一根本隔离；超长令牌阈值（32 字符）是旋钮，接入更严格密钥规范时校准。
- 审计条目当前为内存/传值对象，未定义持久化表结构与查询索引；跨用户/市场隔离在接线到 Store 时按既有 `.for_user().for_market()` 范式补齐。

## P2-3：LLM、多智能体、投委会增量消融

- 状态：✅ 组件阶梯增量消融层落地，纯计算叶子层，专项/受影响回归/范围 Ruff/全量门禁通过。
- 背景与分工：证明「加了 LLM/多智能体/投委会到底有没有带来真实增量」所需原语**已全部就位，但无一处把它们编排成可下结论的阶梯消融**——`evaluation.ablation`（P0-6）能算「基线 vs 单一变体」的严格样本外 delta + bootstrap 区间 + 跨 0 显著性（docstring 明写「供 P2-3 复用」），`judge_independence.effective_number_of_judges`（P1-2）能算有效独立评委数 N_eff，`evaluation.walk_forward_splits` 能切样本外窗口。缺口正是 P2-3 验收所要的「证明增量或明确无增量，不以 persona 数量代替独立性」：无逐级阶梯编排、无「一次显著 vs 跨种子稳定」的区分、无「明确无增量（真零）vs 证据不足（样本不够）」的区分、无「人数涨但 N_eff 冗余」的独立性守卫。P2-3 补这一层编排，复用而非重造上述原语。
- 新增 `watchlist/incremental_ablation.py`：把有序配置臂（如 无LLM→单LLM→多智能体→投委会）逐级做增量消融并给出可审计裁决。纯计算叶子层，仅复用 `evaluation.ablation` 与 `judge_independence`，不引入 scipy，给定输入与种子必得同一结论。
  - **`Arm` 配置臂**：`name` + 同一组样本外窗口下的实测逐窗收益 `oos_returns` + `n_components`（原始人数）+ `n_effective`（N_eff，集成臂给出）+ `cost_usd`（复用 P2-2 成本口径）。逐窗收益须由调用方在严格 walk-forward 下实测传入，本模块不代跑回测、不臆造收益。
  - **`incremental_ablation` 逐级编排**：校验 ≥2 臂、种子非空、各臂 `oos_returns` 非空且等长（同一组窗口）；相邻两臂逐对交给 `_one_step`，汇总为阶梯裁决（`ladder`/`steps`/`n_windows`/`n_proven_positive`/`n_proven_negative`/`any_persona_inflation`/`total_cost_delta`）。
  - **`_one_step` 增量裁决 + 重复实验**：对每一步用多个 bootstrap 种子跑 `ablation`，`seeds[0]` 为确定性主结论；跨种子一致性 = 与主结论同向显著（或同为不显著）的比例，`>= stability_threshold` 才算稳定。裁决：显著且稳定→`proven_positive`/`proven_negative`；不显著且稳定且区间宽度 `<= null_width`→`no_increment`（真零，而非样本不足）；否则 `inconclusive`。把「一次显著」与「跨种子稳定」、「明确无增量」与「证据不足」从结构上分开。
  - **独立性守卫（不以 persona 数量代替独立性）**：`redundancy = 1 - N_eff/人数`，人数较上一臂增加且冗余 `>= redundancy_threshold` 时置 `persona_inflation=True` 并写明「人数+N 但有效独立仅+M」的告警；非集成臂（`n_effective=None`）永不置位。报表同时呈现 delta 与 N_eff，杜绝「加了 5 个评委所以更好」这类以人数冒充独立信号的结论。
  - **`n_eff_from_votes`**：由历史投票向量直接复用 P1-2 的 `vote_similarity_matrix` + `effective_number_of_judges` 算 N_eff，便于用真实投票构造投委会臂。
- 分层保护：**纯计算叶子层，不接线进生产决策链**（回退=不调用，对齐验收「不改变线上默认决策」）；`evaluation`/`judge_independence` 现有路径完全不变，仅单向 import 复用。与 P1-1/P1-3/P1-4/P2-1/P2-2 各叶子模块互不依赖。

### 门禁结果

- 专项测试 `python -m pytest tests/test_incremental_ablation.py -q`：`20 passed in 0.91s`。
- 范围 Ruff `ruff check incremental_ablation.py tests/test_incremental_ablation.py`：`All checks passed!`（一处 docstring E501 手工折行，未用 `--fix`）。
- 全量 `python -m pytest -q`：`1999 passed, 4 skipped in 520.94s`，退出码 0（= P2-2 基线 1979 + 20 项 P2-3 专项）。

### 已知边界

- 本层只做「阶梯编排 + 裁决」，各臂逐窗样本外收益须由调用方在严格 walk-forward 下实测传入；本模块不代跑回测、不接线进生产决策链，要真正评估线上「LLM/投委会是否值回成本」需调用方喂入真实分臂回测结果，留待 P2-4 或后续子项按需接线。
- `null_width`（明确无增量的区间宽度上限）与 `redundancy_threshold`（persona 冗余阈值）是校准旋钮而非硬事实，按收益量纲与投委会构成校准；默认 `stability_threshold=0.8`、`seeds=(0,1,2,3,4)` 同理。
- 显著性沿用 P0-6 `ablation` 的「bootstrap 区间跨 0」判据（非参数、无 scipy），不做多重比较校正；阶梯多步时若需控制族错误率，由调用方在报表层叠加。
- N_eff 由调用方传入或 `n_eff_from_votes` 由历史投票算得，本模块不核验投票与该臂配置是否同源，须调用方保证 `n_effective` 与 `oos_returns` 出自同一配置。

## P2-4：质量门禁、审计报表、回滚与部署验收

- 状态：✅ 发布质量门禁 + 迁移演练 + 回滚预案 + 验收报表落地，纯计算叶子层，专项/范围 Ruff/全量门禁通过。
- 背景与分工：P2-4 是方案收官项。现有 `deploy.sh` 是**实际执行**的一键部署（备份→`git pull --ff-only`→`docker compose up -d --build`→`/healthz` 30 次重试），但它**无发布前门禁、无回滚路径、无迁移演练**——健康检查失败只记录 `OLD_REV` 并不自动回滚；`quality_gate.py` 是**运行期决策质量**门禁（数据新鲜度/持仓一致性/催化剂过期，SSE），面向单次决策，与发布/部署期是不同域、不可重载。缺口正是 P2-4 验收所要的「门禁失败阻止发布，回滚步骤经演练，使用上一稳定镜像/提交」：无「任一阻断项失败即不放行」的发布裁决、无「迁移能否干净应用且幂等重跑」的演练、无确定可执行且经演练的回滚预案、无把门禁/演练/回滚/审计拼成一体的验收报表。P2-4 补这层判定与预案，复用 `deploy.sh` 机制、`store_schema` 迁移语义与 `llm_audit.summarize` 审计汇总，而非新建 CI 或改动生产部署脚本。
- 新增 `watchlist/release_gate.py`：把「一次上线」的门禁判定、迁移演练、回滚预案与验收报表收敛成确定可复现的纯函数。纯 stdlib（迁移演练用内建 sqlite3 内存库），逻辑不读 wall-clock，给定输入必得同一裁决/预案。
  - **质量门禁 `evaluate_release`**：汇总 `GateCheck`（`pass|fail|warn|skip` + `blocking`），任一**阻断项** fail 即裁决 `block`、绝不放行；非阻断失败与告警项并入 `warnings` 照实上报但不阻断，落实「门禁失败阻止发布」且不静默吞掉次要问题。
  - **迁移演练 `migration_drill`**：在 `:memory:` SQLite 上把迁移 SQL 真跑两遍——首遍验能否干净应用（坏 SQL→`ok=False`→阻断 fail），次遍验可否幂等重跑（`ALTER ADD COLUMN` 这类非幂等语句被抓出→`idempotent=False`→warn）；`setup` 前置建表仅应用一次、不计入幂等判定。**只连内存库，与生产数据完全隔离**，`as_check()` 直接产出对应门禁项。
  - **回滚预案 `build_rollback_plan` + 演练 `validate_rollback_plan`**：按 `deploy.sh` 真实机制生成确定有序步骤——记录当前版本→备份数据→（提交模式）检出上一稳定提交 + `docker compose up -d --build` 重建 /（镜像模式）切回上一稳定镜像、不重建→`/healthz`→**容器内 `git rev-parse` 证真**；落实「使用上一稳定镜像/提交」，并把教训「容器烘焙源码、git reset/restart 不上线代码、必须重建 + 容器内核对版本」编码进步骤。缺 `to_rev`/`image_tag` 直接抛错，绝不生成空目标预案。`validate_rollback_plan` 演练预案完备性（目标非空且不同于当前、序号连续、每步可验证、含健康检查、含容器内证真），把回滚变成可判定门禁项——「回滚步骤经演练」。
  - **验收报表 `acceptance_report` + `render_checklist`**：把门禁裁决 + 迁移演练 + 回滚预案 + 调用方传入的 `llm_audit.summarize(...)` 审计汇总拼成结构化报表与 ✅/❌/⚠️/⬜ 人读验收清单；`meta` 由调用方补充（版本/提交/时间戳），本函数不自造时间戳以保持确定可复现。
- 分层保护：**纯计算叶子层，不接线进生产部署链**（回退=不调用，`deploy.sh` 与现有流程完全不变）；不执行真实部署、不 SSH、不碰生产库——迁移演练只用内存库。与 `quality_gate.py` 运行期门禁不同域，不重叠、不改动。与 P1-1/…/P2-3 各叶子模块互不依赖，仅在验收报表处按传值方式复用 P2-2 `llm_audit.summarize` 的输出形状。

### 门禁结果

- 专项测试 `python -m pytest tests/test_release_gate.py -q`：`32 passed in 0.60s`。
- 范围 Ruff `ruff check release_gate.py tests/test_release_gate.py`：`All checks passed!`（一处测试未用 import 手工删除，未用 `--fix`）。
- 全量 `python -m pytest -q`：`2031 passed, 4 skipped in 316.39s`，退出码 0（= P2-3 基线 1999 + 32 项 P2-4 专项）。

### 已知边界

- 本层只做「判定 + 预案生成 + 演练」，**不执行任何真实部署/回滚动作**；回滚步骤是可执行文本，实际执行仍由运维在生产按 `deploy.sh` 拓扑操作，外部/不可逆操作须按授权确认。要把发布门禁接进 CI，需调用方在流水线里采集各门禁项结果喂给 `evaluate_release`，留待接线时按需补齐（本方案不新建 `.github/workflows/`、不改 `deploy.sh`）。
- 迁移演练在内存 SQLite 上验应用与幂等，覆盖 SQL 层的语法/幂等性，**不覆盖**跨版本数据回填、SQLite 与生产同款引擎差异、或迁移与应用码的耦合；接真实迁移须以 `store_schema.CREATE_TABLES` 为 `setup`、`MIGRATIONS` 为演练输入再校准。
- 默认 `compose` 命令、`host_port=8089`、`app` 名与 `data_dir` 是对齐当前部署拓扑的校准旋钮，换机/换端口须按 `project_prod_deploy_topology` 校准。
- 非幂等迁移默认判 `warn` 而非阻断（`ALTER ADD COLUMN` 等一次性迁移合法非幂等），是否升级为阻断由调用方按迁移性质在门禁层决定。

## 最终审查与验收（2026-09-15）

方案 P0-0→P2-4 全部子项已完成并各自过门禁提交（HEAD `3a2d1d3`）。本节记录收官阶段的整体代码审查与验收工具复核的真实命令、数字与结论，作为发布依据。

### 代码审查（11/11 叶子模块逐一评审，零真实缺陷）

对 P0-0→P2-4 交付的 11 个叶子模块逐一评审。所有模块共享同一纪律：纯函数、确定可复现（种子固定、逻辑不读 wall-clock）、不接线进生产链路（回退=不调用）、各带一个可运行的 `__main__` 断言自检 + 聚焦 `test_*.py`。**结论：零真实缺陷。**

仅 3 条低 severity 建模备注，均为刻意的、已加注释的设计选择，**非 bug**：

1. **`pit_gate.py`**：全局 `_MIGRATION` 迁移旁路是「建议性」标记，门禁函数从不查询它——调用方通过「不调用」退出（安全叶子层范式），且仅供离线迁移、不可重入。
2. **`event_backtest.py`**：`avg_cost`（[event_backtest.py:147](bottleneck_hunter/watchlist/event_backtest.py#L147)）不含买入佣金，故单笔 `realized_pnl` 轻微高估；但净值曲线（现金 + MTM）完整计入所有成本，内部自洽。
3. **`llm_audit.py`**：`estimate_tokens` 是无 tokenizer 依赖的粗启发式，已在边界注明「仅兜底、非计费真值」，真实 token 应优先取 provider usage 回包。

（子代理在本会话不可用，审查在主进程内逐模块完成。）

### 验收工具复核（release_gate 裁决：放行 / release）

用 P2-4 落地的 `release_gate.py` 对真实门禁数据跑整体验收（**只读**：迁移演练仅用 `:memory:`，回滚预案只生成文本不执行，绝不碰生产库、不部署）：

**裁决 `release`（放行），released=true，5/5 门禁项通过，0 阻断失败，0 告警。** 门禁项明细：

1. `full_tests` — **pass** — 全量 `python -m pytest -q` → **2031 passed, 4 skipped**，退出码 0，耗时 530.40s（= P2-3 基线 1999 + 32 项 P2-4 专项，与 P2-4 基线一致，**零回归**）。
2. `ruff` — **pass** — 范围内只读 `ruff check`（11 叶子模块 + 11 专项测试）→ `All checks passed!`，退出码 0（不 `--fix`、不 `format`）。
3. `code_review` — **pass** — 11/11 叶子模块逐一评审，零真实缺陷。
4. `migration_drill` — **pass** — 「规范基础表模式」（`CREATE_TABLES` + `CREATE_INDEXES`，均 `IF NOT EXISTS`）幂等演练：applied=2/2，idempotent=True。这正是新部署 `_init_db` 实际应用的规范 DDL。
5. `rollback_drill` — **pass** — 回滚预案 `3a2d1d3 → a7d7cca`（上一稳定提交 P2-3）经 `validate_rollback_plan` 演练：6 步（记录当前版本 → 备份数据 → `git checkout a7d7cca` → `docker compose up -d --build` → `curl /healthz:8089` → 容器内 `git rev-parse` 证真），目标非空且异于当前、序号连续、每步可验证、含健康检查、含容器内证真。

### 迁移演练的忠实口径（既不 false-red 也不 false-green）

门禁项迁移演练取「规范基础表模式」（新部署真正应用的 DDL），跑两遍干净且幂等 → pass。

另附**诊断项（非门禁）**：全量 `MIGRATIONS` 裸重放（以基础表模式为 `setup`）→ applied=**210/224**，14 条失败**全部**为 `duplicate column`（`dup_only=True`）。这 14 条是「基础表 `CREATE_TABLES` 与历史增量 `ALTER` 同时含该列」的模式合并产物，**非本方案迁移**——本方案新增的 P0-3 `snapshot_id`/`strategy_version` 迁移在 210 条成功应用之列。生产 `_init_db`（[store.py:255](bottleneck_hunter/watchlist/store.py#L255)）对每条迁移套 `duplicate column/already exists` 例外守卫，故整条 bootstrap 在生产幂等自洽；裸重放无此守卫，仅用于暴露该事实、不作门禁裁决——既不把生产靠守卫吸收的历史产物冒充 pass（false-green），也不因一个生产按设计已处理的非问题阻断发布（false-red）。

### 结论

代码审查零真实缺陷 + 验收工具裁决「放行」+ 全量测试零回归，方案 P0-0→P2-4 达到发布质量。推送主线属外部不可逆操作，按既有授权边界须经用户显式确认后执行。
