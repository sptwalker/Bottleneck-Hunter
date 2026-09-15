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
