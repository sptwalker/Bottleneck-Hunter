# 决策链路闭环提升路线图

> **版本**：v1.0 — 决策→执行→反馈完整闭环改造
> **日期**：2026-06-30
> **背景**：以金融分析师 + 操盘手视角全面审视 `TRADING_DECISION_SYSTEM.md` 设计 vs 实际实现后，识别出三类结构性问题，制定分阶段（P0-P3）提升方案。

---

## 一、问题诊断（审视结论）

决策闭环 7 环节体检：

```
感知数据 → 分层判断 → 校验把关 → 用户确认 → 模拟成交 → 归因复盘 → 学习回灌
 (✅好)    (✅好)    (❌漏)     (✅好)    (⚠️脆)    (⚠️半)    (❌断)
```

### 三个结构性问题

**问题 1：校验滞后**
- L4 生成执行计划时**不做约束校验**（`run_execution_plans` 直接 `create_execution_plan` 入库）
- 约束校验只在用户点"确认执行"后才在 `trade_executor.execute_trade` 跑
- 后果：不合规指令出现在下单界面、状态机污染（confirmed 卡死）、用户认知负担转嫁
- 深层：约束校验用 **worst-case 最坏滑点**（`constraint_validator.py:67`）→ "压线被拒"（20% 仓位因滑点变 20.1% 被拒）。概念混用：校验应该用预期值，最坏值只用于风险预算。

**问题 2：安全机制空转**
- 投委会用 4 个不同 LLM 评审，但结论是**纯咨询性（advisory），不 gate 任何东西**
- `run_committee_review` 只写 `committee_consensus` 表，从不调 `reject_execution`，从不应用 `consensus_modifications`
- 4 个委员一致否决，计划照样 `pending` 出现在待确认列表
- L1 算出 `risk_appetite`，但 L4 约束是**写死常量**（`DEFAULT_CONSTRAINTS`），宏观判断与风控参数断连

**问题 3：反馈断头**

| 反馈机制 | 计算了吗 | 用上了吗 |
|---|---|---|
| 经验卡片（贝叶斯置信度更新）→ L4 | ✅ | ✅ 真闭环 |
| 用户拒绝模式 → L2/L4 prompt | ✅ | ✅ 软提示 |
| 催化剂兑现/落空判定 | ✅ | ❌ 没生成买卖信号 |
| 复盘四因归因（L1/L2/L3/L4） | ✅ | ❌ 锁在 `auto_reviews` 表没人读 |
| 情景估值（bear/base/bull） | ✅ L2 算了 | ❌ L3/L4 没当止损止盈锚点 |
| 观察池淘汰 | ❌ | ❌ 设计了，代码没有 |

- 复盘**只在卖出且 |收益|≥10% 时触发**，对踏空 / 错误持有 / 拒绝后反转等**机会成本**无感
- L4 倾向**每天都生成操作**，"今天不动"几乎不是合法输出 → 过度交易风险

---

## 二、可复用的现有基础设施

实施前需知，以下框架**已存在**，改造应复用而非重建：

| 模块 | 现状 | 复用点 |
|------|------|--------|
| `quality_gate.py` | green/yellow/red 数据新鲜度 + pre_l2/l3/l4 门控点 | P0 校验回路 + P2 staleness 守卫 |
| `constraint_validator.py` | `validate_execution_plan` + `validate_batch` | P0 前置校验 |
| `committee.py` | 4 委员并行评审 + 圆桌 + 共识 | P0 升级为 gating |
| `catalyst_monitor.py` | `judge_catalyst_outcome` 已判定 realized/failed | P1 接信号生成 |
| `trade_reviewer.py` | 四因归因已算 | P1 接分层绩效 |
| `trade_feedback` 表 + `get_rejection_patterns` | 拒绝回灌已通 | P0 投委会拒绝复用此通道 |

---

## 三、分阶段路线图

### P0 — 校验前置 + 自修正回路 + 投委会 gating

**状态：✅ 已完成（2026-06-30）**

**目标**：不合规计划永不进入待确认队列；投委会真正把关。

#### P0.1 L4 生成后前置约束校验

`run_execution_plans`（`decision_engine.py:619`）中，`create_execution_plan` 入库**之前**插入校验：

```
exec_plans = result.get("execution_plans", [])
for ep in exec_plans:
    validation = validate_execution_plan(ep, account, positions)  # 用预期值
    if validation.valid:
        → create_execution_plan
    else:
        → 进入自修正回路（P0.2）
```

#### P0.2 LLM 自修正回路（auto-repair）

违规计划带【违规详情 + 账户状态】重新调用 L4 LLM 再生成，最多 2 轮：

```
for attempt in range(2):
    repaired = await _repair_execution_plan(llm, ep, violations, account)
    if validate_execution_plan(repaired, account, positions).valid:
        ep = repaired; break
else:
    → 自动降级（P0.3）
```

需新增：`_repair_execution_plan()` + prompt 模板 `decision_execution_repair.md`（输入原计划 + 违规列表 + 约束上限，要求 LLM 在约束内重新给方案）。

#### P0.3 无法修复时自动降级

- **可缩量**（如超持仓上限）：按约束反推最大合规股数，缩量入库，标注 `auto_adjusted=true` + 原因
- **不可缩量**（如现金不足、单股已满仓）：标记 `status='needs_review'` + 原因，**不进入 pending 队列**，前端单独区域展示"被系统拦截的计划"

需新增 store 方法：`create_execution_plan` 增加 `status` 参数支持 `needs_review`；前端新增"已拦截"折叠区。

#### P0.4 约束校验改用预期值

`constraint_validator.py:64-72`：拆分两套口径
- **校验口径**（valid 判定）：用预期价（不加最坏滑点）
- **风险预算口径**（stress test 警告）：保留最坏滑点，作为 warning 而非 violation

#### P0.5 投委会升级为 gating

`run_committee_review`（`committee.py:239`）每个 plan 评审后，按 `final_verdict` 动作：

| verdict | 动作 |
|---------|------|
| `rejected` | 自动 `reject_execution(plan_id, "投委会否决: " + summary)` → 进 `trade_feedback` |
| `approved_with_modifications` | 应用 `consensus_modifications` 到 `execution_plans.result_json`（缩量/调价/改方式） |
| `approved` | 保持 pending，附投委会通过标记 |

需新增 store 方法：`apply_committee_modifications(plan_id, modifications)`。
保留用户 override：被投委会拒绝的计划在"已拦截"区可手动恢复。

#### P0.6 动态约束（L1 risk_appetite → 约束参数）

`constraint_validator.py`：`validate_execution_plan` 增加 `risk_appetite` 入参，按宏观风险偏好调整阈值：

| risk_appetite | 单股上限 | 现金下限 |
|---------------|---------|---------|
| aggressive（进攻） | 30% | 10% |
| balanced（平衡） | 25% | 15% |
| defensive（防守） | 18% | 25% |

调用方从 `store.get_latest_macro_strategy()` 取 `risk_appetite` 传入。

**P0 改动文件**：`decision_engine.py`、`committee.py`、`constraint_validator.py`、`store.py`、`chain/prompts/decision_execution_repair.md`（新建）、前端 `decision.js` + `index.html`（"已拦截"区）

**P0 验证**：刷新决策 → 所有进入 pending 的计划必然合规 → 点击确认全部成功；超限计划进"已拦截"区；投委会否决的计划自动消失出 pending。

---

### P1 — 接通三个断头反馈

**状态：✅ 已完成（2026-06-30）**

**目标**：让系统真正形成可进化的闭环。

#### P1.1 催化剂 outcome → 买卖信号

`catalyst_monitor.py` 的 `judge_expired_catalysts` 判定后：
- `outcome == "failed"` → 生成 sell 信号（写入待办或直接进 L3 输入）
- `outcome == "realized"` → 生成 add 信号

需新增：信号生成函数 + 一张轻量 `catalyst_signals` 表（或复用现有 signal 机制）。L3 `run_tactical_plans` 读取这些信号作为额外输入。

#### P1.2 复盘四因归因 → 分层绩效表

新建 `layer_performance` 表，`trade_reviewer` 复盘后写入四层归因：

```sql
CREATE TABLE layer_performance (
    id TEXT PRIMARY KEY, trade_id TEXT,
    layer TEXT,              -- L1/L2/L3/L4
    attribution_score REAL,  -- 该层对本次盈亏的贡献(-1~1)
    note TEXT, created_at TEXT, user_id TEXT, market TEXT
);
```

聚合统计：L1 regime 判断准确率 / L2 选股胜率 / L3 择时质量 / L4 执行滑点。
回灌：各层 prompt 注入"本层历史表现"（如 L3 prompt 加入"你近期择时准确率 62%，倾向过早建仓"）。

#### P1.3 情景估值 → 止损止盈锚点

L2 `stock_selection` 已含 `scenario_valuation`（bear/base/bull 价格 + 概率）。
L3 `run_tactical_plans` / L4 直接读取：bear 价 → 止损位，bull 价 → 止盈位，替代 LLM 每天重新拍脑袋。

**P1 改动文件**：`catalyst_monitor.py`、`trade_reviewer.py`、`decision_engine.py`、`store.py`（新表 + CRUD）、L3/L4 prompt 模板

**P1 验证**：催化剂落空后下次决策出现对应 sell 信号；分层绩效表有数据且注入 prompt；L3 止损位 = L2 bear 价。

---

### P2 — 组合级风控 + 动态约束 + 状态机健壮性

**状态：✅ 已完成（2026-06-30）**

**目标**：从单股风控升级到组合风控，根治状态机脆弱性。

#### P2.1 组合级风险约束

`constraint_validator.py` 新增组合层检查（现有只有单股 cap）：
- **行业集中度**：已有 `max_sector_pct` 但需确保跨多笔累计计算
- **组合 Beta 上限**：进攻期 ≤1.3、防守期 ≤0.9
- **相关性集中**：避免买入与现有持仓高相关的标的（同板块多只）

需数据：个股 beta（yfinance info 已采集，见 `company_profiles.raw.beta`）、板块归类。

#### P2.2 执行状态机加固

清理 `pending→confirmed→executed/failed` 全链路：
- `confirm_execution` + `execute_trade` 用单一事务包裹，失败自动回滚到 pending（而非卡 confirmed）
- 新增 `execution_status_log` 审计每次状态变迁
- （P0 已临时修复 reject 接受 confirmed 态，此处做根治）

#### P2.3 上游 staleness 守卫

复用 `quality_gate.py` 新鲜度框架：
- L1 > 7 天 / L2 > 7 天未更新 → pre_l3 门控亮红 → 强制先 revalidate 或警告
- `scope="l3l4"` 重复调用时检查 L2 年龄，过期则提示用 `full`

**P2 改动文件**：`constraint_validator.py`、`store.py`、`quality_gate.py`、`decision_engine.py`

---

### P3 — 复盘"没做的决定" + 自进化

**状态：✅ 已完成（2026-06-30）**

**目标**：捕捉机会成本，抑制过度交易，绩效驱动自调整。

#### P3.1 扩展复盘范围

`trade_reviewer` 增加三类复盘（不限于 |收益|≥10% 的卖出）：
- **踏空**：拒绝/未买的标的后续大涨 → 记录机会成本
- **错误持有**：浮亏超阈值仍持有 → 检讨止损纪律
- **拒绝后反转**：用户拒绝的计划后续验证正确/错误 → 校准建议质量

需新增定时任务扫描这些场景。

#### P3.2 "今天不交易"作为一等输出

L4 prompt 明确："若无高置信操作，输出空操作序列是合理的。过度交易损害收益。"
统计交易频率，异常高频时告警。

#### P3.3 绩效驱动的层权重自调整

基于 P1.2 分层绩效：长期看哪层准，动态提升其在综合评分/置信度中的权重。
（最高阶，依赖 P1.2 积累足够样本后启动。）

**P3 改动文件**：`trade_reviewer.py`、`scheduler.py`、L4 prompt、`decision_engine.py`

---

## 四、依赖关系与建议顺序

```
P0（校验闭环）──┬──→ P1（接通反馈）──→ P3（自进化）
                └──→ P2（组合风控）──────┘
```

- **P0 必做且独立**，收益最大（直接修复用户痛点 + 堵两大漏气点）
- P1 依赖 P0 的 gating 通道（投委会拒绝复用 trade_feedback）
- P2 可与 P1 并行
- P3 依赖 P1.2 积累的分层绩效样本

---

## 五、验证策略（端到端）

每阶段完成后用真实服务器验证（启动命令见 `project_server_start_command` 记忆）：

1. `bottleneck-hunter serve --port 8899`（后台）
2. 生成 JWT cookie（`jwt_utils.create_token`）调 `/api/decision/*` 端点
3. P0：刷新决策 → 检查 pending 全合规、investment committee 拒绝项自动出队
4. P1：构造催化剂落空 → 验证 sell 信号生成；查 `layer_performance` 表
5. P2：构造高相关持仓 → 验证组合约束拦截
6. P3：构造踏空场景 → 验证机会成本复盘生成
