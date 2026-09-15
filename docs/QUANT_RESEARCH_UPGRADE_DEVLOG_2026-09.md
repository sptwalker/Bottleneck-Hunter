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
