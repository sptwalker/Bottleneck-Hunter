# 投委会下一阶段开发计划（Phase 2）

> **范围**：在已完成的投委会（4 成员独立评审 + 第 2 轮辩论改票 + 圆桌讨论 + gating + transcript）基础上，新增两个方向。
> **状态**：实施中（2026-07-01）

## 方向一：绩效加权共识（历史准确率 → 权重）

**现状**：`_fallback_consensus` / `_build_consensus` 用等权计数（`approve_count/total`）得出 verdict，未消费已采集的委员历史准确率。

**改造**：
- 委员投票后已 `record_prediction(role_context="committee_{role}")` → `model_accuracy` → 校准产出 `model_ratings.calibration_weight`（`store.get_calibration_weight(provider, model, role_context)`，无数据默认 1.0，向后兼容）。
- `run_committee_review` 生成共识前，按 `(provider, model, committee_{role})` 取每位委员的 `calibration_weight`，构造 `weights={role: w}`。
- `_fallback_consensus(reviews, weights)`：改为**加权表决**——`approve_ratio = Σw(赞成) / Σw(全部)`；阈值映射到 approved / approved_with_modifications / rejected / needs_discussion；`approval_rate = round(approve_ratio*100)`；`vote_detail` 附每人 `weight`。
- `_build_consensus`：prompt 注入 `{member_weights}`（历史权重提示），并以加权 `_fallback_consensus` 兜底。
- 共识 `result_json` 增加 `member_weights`；transcript 每位委员条目增加 `provider`/`model`/`weight` 字段（前端展示"权重 1.2x"）。

## 方向二：用户交互质询（讨论后可质询委员，接受则改票）

**流程**：投委会讨论完成后，用户在会议详情里对**任一委员**发起质询（输入论据）→ 该委员 LLM 带着原评审 + 用户论据重新表态 → 若接受用户意见则改票 → 重新计算（加权）共识 → 按新结论**重新 gating** 执行计划 → 持久化。

**后端**：
- 新 prompt `committee_challenge.md`：委员人设 + 原评审 + 用户质询 → 输出 JSON `{response, accept_user_point, new_vote, new_confidence, revised_assessment}`。
- `committee.py` 新增 `async challenge_member(store, *, meeting_id, role, user_message, market)`：
  1. `get_meeting_record` → 取 `execution_plan_id`/`ticker`/该委员最新评审（transcript 取该 role 最大 round 条目）。
  2. 用 `_build_llm_chain(member)` + `_invoke_with_retry` 调该委员模型。
  3. transcript 追加质询条目（`round=3, type="challenge"`）；若改票，追加 `round=3` 的改票评审条目。
  4. 改票则：重算加权共识 → 写回 `final_verdict`/`result_json`/`transcript_json`（新增 `store.update_meeting_review`）→ **重新 gating**：
     - 新 verdict `rejected` 且计划 pending → `reject_execution`；
     - 新 verdict 非 rejected 且计划已被投委会 `rejected` → `restore_execution`（+ `approved_with_modifications` 时 `apply_committee_modifications`）。
  5. 返回 `{member_response, vote_changed, old_vote, new_vote, consensus, gating_action}`。
- `store.update_meeting_review(record_id, *, transcript_json, result_json, final_verdict)`。

**API**：`POST /api/decision/committee/challenge` body `{meeting_id, role, message}` → JSON（单次 LLM 调用，前端转圈）。

**前端**（decision.js 会议抽屉 `renderCommitteeTranscript`）：
- 每位委员条目加「质询」按钮 → 展开输入框 → 提交 → 显示委员回应 + 改票徽章（旧→新）+ 共识更新提示 → 重载抽屉与概览。
- 委员条目展示历史权重（方向一透明化）。

## 复用 / 改动文件

| 文件 | 改动 |
|------|------|
| `watchlist/committee.py` | 加权共识 + `challenge_member` + transcript 加 provider/model/weight |
| `watchlist/store.py` | `update_meeting_review`（新） |
| `chain/prompts/committee_consensus.md` | 加 `{member_weights}` |
| `chain/prompts/committee_challenge.md` | 新建 |
| `web/decision_api.py` | `POST /committee/challenge` |
| `web/static/js/decision.js` + css | 质询 UI + 权重展示 |

## 验证
- `py_compile` + `node --check`；
- 单元：加权 `_fallback_consensus`（构造不同 weight 验证 verdict 翻转）；`challenge_member` 在 DB 副本上跑改票→重算→re-gate；
- `pytest`（对照基线零新增失败）；硬刷新前端验证质询交互。
