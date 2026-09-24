# 决策中心决策链复核报告（第二轮）· 2026-09-23

> **性质**：只读复核 + 新方案。**复核与方案部分未改动任何代码**；其中 **P0-F / P1-K / P1-L / P2-I** 四项已于 2026-09-24 经授权实施，执行记录见**第六节**（含与原文方案的差异、生产证真、门禁结果，以及**执行时发现并更正的报告自身一处错误判据**）。
> **基准**：`docs/DECISION_CHAIN_REVIEW_2026-09.md`（第一轮，9 月中旬）
> **方法**：全部论断均由本人直接读代码 / 查生产库独立验证。**不采信任何未经自证的转述结论**（含后台审计 agent 的输出）。凡我先前得出、后被自己证据推翻的判断，一律在文中显式更正。
> **生产基线**：复核时 `main@72bc42f`（容器 `bottleneck-hunter` 已 grep 证真扩张侧代码全部上线）；第 0 批落地后为 **`main@846e065`**（2026-09-24 经 `./deploy.sh` 重建部署，容器内 `inspect.getsource` 证真，见第六节 6.3）。

---

## 摘要（结论先行）

| 维度 | 判断 |
|---|---|
| 第一轮方案闭环度 | P0-1 ~ P2-2 **八项均有代码落点**，但 **3 项存在实质偏差**（P0-1 落点、P1-1 落点+擅加冷却、P0-4 的 B 类与留痕） |
| 新发现的高危缺陷 | **2 项**：投委会「不可背书」结论被自动执行静默放行（**有生产成交铁证**）；失败/无人管的 pending 计划无老化、无计数、无留痕 |
| 扩张侧实际有效性 | **未验证**。驱动器靠"LLM 恰好也点同一票"才能落地，属未测的巧合依赖；生产首日观察**零扩张计划落地** |
| 第一轮 §4 要求的端到端验收 | **从未执行**。无脚本、无多轮测试、无收敛证据 |
| 收缩/扩张对称性 | 现金上限侧（`cash_max`）**仍零消费方**；**无超配→减仓驱动器**；**板块漂移零消费者**。第一轮诊断的根因**只补了一半** |
| 新发现问题总数 | **N-1 ~ N-36 共 36 项**（N-1~N-7 本人直查；N-8~N-20 与 N-21~N-32 分别由两路后台审计产出，**均经我逐条 grep/读原文/本地复现/生产查库复核后只保留属实的**，含 3 处我对审计员的更正；**N-33/N-34 为第三轮 L4 执行与账户记账域独立审计所得；N-35/N-36 为第四轮筹备数据修复时解密结单 `parsed_json` 逐期比对所得——这四条后台 agent 均零产出、全部由我自证**） |
| 扩张侧实现的真实有效性 | **机制齐全，主线失效**。N-21 使「已持仓低配核心票补仓」定股恒为 0（前后端与生产均证）；N-23 使缺口**永不可达**；两者叠加＝扩张侧在生产上只对「从未持有过的名字」有效 |
| 组合级风控的真实缺口 | **不是现金下限（已由成交期兜住），而是日额度上限**（`max_daily_turnover_pct` 逐笔比对、从不累计；实测 10 笔各 5% 权益全部放行 → 单日累计 50% 权益 vs 上限 30%）。**此处更正了我自己的先前结论**，见 N-22 |
| 账户记账一致性 | **①②③ 三项已于 2026-09-24 全部闭环**：①头条权益/现金经授权改回 07-31 期真值（改前库备份 `data/watchlist.db.bak-cmbis-equityfix-20260924T020356Z`）→ 留痕 `vip_account_log` id `3857914823a9`；②衍生品当期 MTM 期次错配由 **P1-L** 修复（`2,099,173.74`→`1,047,068.00`，高估消除）；③排序键时区由 **P0-F** 修复（`created_at` 由裸本地时间改为 UTC，与同事件导入时间戳由差 8 小时变为 <5 秒）。**根因（P1-K）**：零股票持仓账户的两道导入护栏同时失效已修——陈旧判据补期次证据源、骤降分支对零持仓改看净值，**净值不再由「上传顺序」决定**。12 条生产形态回归守卫全绿、改前 6 条必红；全量 2223 passed / 5 skipped / exit 0；ruff 零新增。详见**第六节执行记录**。**CMBIS 一个账户、三处期次错配**（一次乱序上传造成的连锁）：①—N-34；②—N-35；③—N-36。教训留档："**零持仓→positions 零行**"已查证为真实链路行为，非探针 artifact。另有 25 票「墓碑+活行」并存（CITI-1 18 / NOMURA 7），**全在 VIP 账户、决策中心自有账户零命中**；经用户定调"**VIP 账户未来不会下单**"，路径不可达，已按 P2-I 只做查询侧一行防御（**P2-I 已落地**，未动 schema） |

| **本报告自身未做的验证** | ① 机会驱动与缺口驱动**叠加**后的实际买入上限**未做数值仿真**（只逐条核对了各自上限）；② `_STALE_UPSTREAM_DAYS` 归属校验缺位的影响面**未量化**（只证明它缺位）；③ L2 顶层 `target_allocation.equity_pct` 无消费者——**仅在零成交账户上复现该形态，尚未回放 09-23 真实用户决策日志**。这三条已在下文逐条标注，**不作为结论使用** |

**一句话**：第一轮把"扩张力"写出来了，但扩张力**没有独立下单权**、投委会的"不可背书"结论**形同虚设**、失败的计划**没人管**。这三条互相独立，且前两条有生产证据。

---

## 一、方法与自我更正

### 1.1 我先前的一个错误口径（必须更正）

我在中途曾得出"`needs_review` → pending **36 条**、executed **1 条**"。**这个口径是错的。**

错因：`committee_consensus` 对**同一 `execution_plan_id` 可有多行**（重复评审、质询改票）。我按**共识行数**计数，把它当成了**计划数**。

按计划维度去重（每个计划取 `created_at` 最新一条共识）后，正确答案是：

```
needs_review → pending  3 条    executed  0 条
needs_discussion → pending 0 / executed 2 / rejected 15 / expired 1
```

**因此第一轮我引用的"37 条"、"36 条"数字全部作废**，正文一律使用去重后的计划口径。

### 1.2 证据强度分级

本文每条结论标注证据来源，请按此判断可信度：

- **【生产实测】** —— 直接查生产库/生产容器得到，可复现。
- **【代码实读】** —— 逐行读过源码，含行号。
- **【测试覆盖】** —— 有自动化测试锚定。
- **【无证据】** —— 我明确查过、确实找不到。

---

## 二、第一轮方案逐项闭环核实

| # | 项 | 判定 | 关键证据 |
|---|---|---|---|
| **P0-1** | 权益下限/现金上限校验对称化 | ⚠️ **部分闭环（落点偏差）** | 做成**独立兄弟函数** `compute_underweight_gap`（`constraint_validator.py:517`）；`validate_against_regime:451` 通读 451-514 **仍只校验 `equity_max` + `max_single_pct`**，无下限分支。**`cash_max` 算出后全仓零消费方**（grep 仅命中定义处 `:534/550/555` 与 `regime_mapper.py` 的 bounds 生成 + 两个测试文件）。第一轮点名的 `tests/test_constraint_validator.py` **不存在**。【代码实读】 |
| **P0-2** | 权益缺口驱动器 | ✅ **闭环** | `_GAP_STEP_FRACTION = 0.3333`（`:1210`）；`step_cap = gap_pct/100*equity*0.3333`（`:1231`）；`budget = min(deployable_cash, step_cap)`（`:1232`）；函数内 `left -= amount` 递减。步长与第一轮"每轮最多补 1/3 缺口"**逐字吻合**。【代码实读+测试覆盖】 |
| **P0-3** | L4 建仓侧门禁对补足低配放行 | ⚠️ **闭环（测试仅到 helper 级）** | 三处豁免都在：`:2355`（已持仓）、`:2366`（冷却，仅缺口驱动豁免）、`:2426-2432`（走 `_gap_fill_shares`）。但地板是**旁路**而非"参数化降低"——`_gap_fill_shares:1957` 刻意绕开 `target_shares_for_buy`。**无任何端到端测试证明驱动计划真能穿过 L4 变成 execution_plan**。 |
| **P0-4** | 机会/信念驱动器 | ⚠️ **部分闭环：A 类完整，B 类未实现** | **B 类越线根本不存在**——`decision_engine.py:1731` 注释明文排除："越线缓冲**只对单票有效**：账户级 `equity_max` 由 `validate_against_regime` 硬拦，这里不碰"。第一轮要求的 feature flag（`BH_ALLOW_*`/`BREACH_ENABLED`/`allow_breach`）在 `watchlist/*.py` **零命中**。护栏①（顶档专属）**空转**；护栏③靠既有 5 日冷却兜；护栏④只做了一半——**`grep mandate_exception committee.py` 返回 0，投委会完全不知道该标记**；且 `mandate_exception` 与 `record_operation`/`oplog` 交叉**零命中 → 越线无留痕**。 |
| **P1-1** | L2 复用缓存偏离逃生阀 | ⚠️ **闭环（落点偏移 + 擅加冷却）** | 实现在 `decision_engine.py:2856`（Step 2），**不在第一轮指定的 `:836` 复用块**。且擅加 `_DECISION_ESCAPE_MIN_AGE_H = 20.0`（`:821`），把"无条件逃逸"改成"20h 后逃逸"。**这是 8 项里唯一显式测掉「接线是死的」这个坑的**（`tests/test_decision_l2_reuse_escape.py:135` 断言 `calls == [("regen", True)]`）。 |
| **P1-2** | 缺口未纠正留痕 | ✅ **闭环** | `decision_engine.py:2966` `record_operation(uid, "缺口未纠正", category="error", result="partial")`。3 条端到端测试，含"质量门绿但上游陈旧也留痕"。**证据最干净的一项。** |
| **P2-1** | `decision_execution.md` 双边表述 | ⚠️ **闭环（零测试）** | 文案在 `:142-143`（"该配没配同样是一种偏差" + "买卖对称"）。但与 `:53` 旧口径"宁可少买，不可超限"**并存**，三句并列在对冲而非取代。**无任何测试断言提示词内容**。 |
| **P2-2** | 前端目标 vs 实际对照条 | ⚠️ **闭环（测试不进 CI）** | `simtrading.js:240-270` + `index.html:1074` + css 齐备；自检 `tests/frontend/simtrading_alloc_target.mjs` A–F 六组断言扎实（含 API 500 隐藏、`cash=null` 隐藏、市场切换丢弃）。但靠 **`node` 手工运行**，pytest 不收集，`.github/workflows/` **不存在** → **无自动化门禁**。 |

### 2.1 第一轮 §四 实施建议第 2 条：端到端验收做了吗？

**没有。** 第一轮原文要求：

> 务必带**步长上限 + 收敛带**，先在模拟盘验证"现金 90%→逐步逼近目标 40%"确实发生、且不刷单。

核查结果：

- `scripts/` 只有 `backup.py`、`clean_role_config.py`、`gen_update_history.py`、`normalize_tickers.py`、`repro_phase2_429.py` —— **无任何收敛验证脚本**。
- 现存测试全是**单轮**：`test_step_cap_bounds_single_round` 只断言"这一轮不超过步长"。**没有任何测试跑 3 轮并断言权益单调逼近、停在带内、且不反复产生碎单**。
- 步长上限与收敛判据**在代码里确实存在**（`:1231`、`:1270` 的 `tw - cur_w > 0.5`、`_gap_fill_shares:1964` 的 `existing_value >= planned_amount → 0`），但**只被单轮单测证明存在，未被多轮证明有效**。

**判定：验收步骤被跳过了。**

### 2.2 生产首日实测：扩张侧有效吗？

我查了生产库今日（扩张侧上线后）的实际运行：

| 观察项 | 实测值 |
|---|---|
| 美股账户实际权益占比 | **12.5%**（现金 87.5%） |
| A股账户实际权益占比 | **9.4%**（现金 90.6%） |
| 今日 L3 产出 `gap_driven` 意图 | 美股 4 条（MSFT/DELL/TSM/NVDA）、A股 3 条 |
| 今日 `execution_plans` 带扩张标记 | **0 条** |
| 今日生成计划总数 | 7 条（美股 3、A股 4） |

**账户正处在报告点名的"现金 90%"最坏情形，扩张侧也确实产出了意图，但落地的执行计划里一条扩张标记都没有。**

原因见第三章 N-3：驱动器**不自己造单**。

---

## 三、新发现的问题

### N-1 【高危 · 功能缺陷】投委会「不可背书」的结论被自动执行静默放行

**这是本轮最严重的问题，且有生产成交铁证。**

**设计意图**（`committee.py:445`，代码自己的注释）：

```python
# (A) 投委会法定人数：真正表决(approve/reject)的委员数低于此值 → 结论不可背书，强制 needs_review 人工复核。
```

`:493-498` 实现该闸：真正表决数不足 `QUORUM_MIN` 时，`verdict = "needs_review"`。

**实现缺口**（`committee.py:1042-1054`）：

```python
verdict_raw = consensus.get("final_verdict", "unknown")
if verdict_raw == "rejected":
    store.reject_execution(plan_id, ...)
elif verdict_raw == "approved_with_modifications":
    store.apply_committee_modifications(plan_id, mods)
# 无 else —— approved / needs_review / needs_discussion / unknown 全部不落动作
```

`_regate_after_challenge`（`:1259-1293`）同构：**只有 `rejected` / 非 `rejected` 两分**。

**于是**：计划留在 `status='pending'` → `get_pending_executions()` 按 status 取件 → `auto_execute_pending` **只过滤 `mandate_exception`**（`auto_execute.py:98-99`）→ **半授权/高授权下直接成交**。

**生产铁证（时序，非计数）**：

| 标的 | 投委会结论 | 结论给出时刻 | 实际成交时刻 | 判定 |
|---|---|---|---|---|
| `002006.SZ` | `needs_discussion` | 2026-08-31 10:33:21 | 2026-09-01 09:46:53 | **结论之后成交** |
| `603005.SS` | `needs_discussion` | 2026-08-03 10:37:23 | 2026-08-04 01:55:13 | **结论之后成交** |

对照：24 条 `approved`/`approved_with_modifications` 的计划全部也在结论之后成交——**说明成交路径对所有结论一视同仁，未区分"可背书"与"不可背书"**。

**放大因素**（全部【代码实读】）：

1. 该用户生产偏好 `us_stock::auto_execute_l4 = 2` 与 `a_stock::auto_execute_l4 = 2`，**两个市场都开在高授权档**（`is_breach_authorized` 为真，连越线计划也不再拦）。
2. `revert_to_pending`（`store_decision.py:605`）把计划退回 pending 且**丢失先前评审结论**。
3. `execution_plans` **无评审结论字段**（DDL `store_schema.py:343-368` 已核）——计划本身承载不了"已评审/已否决"。
4. `needs_review` 在全仓**只有定义处一个引用、零消费方**。
5. 前端 `decision.js:534-614` **无 `needs_review` 徽标**。
6. 统计口径把它并入"待议"（`store_decision.py:72`：`needs_discussion / needs_review = 待议`）——**一个"须人工复核"的结论被混进"还在讨论"，从统计上消失**。

**附带第二扇门（同样 fail-open）**：Step 5 的 try/except 在投委会抛异常时只发 `decision_error`，随后 Step 5.5 **仍用同一批未评审的 pending 执行自动成交**（`decision_engine.py:3013-3021`）；共识保存失败分支（`committee.py:1036-1040`）同样是 `committee_error` + `continue`，计划留 pending 被收走。

**危害**：法定人数闸的全部意义就是"委员大面积故障时不要凭残缺意见下单"。当前实现把它的输出**转成了放行**。委员故障越是成片发生（LLM 限流/欠费时正是如此），被放行的计划越多。

---

### N-2 【高危 · 卫生缺陷】失败 / 无人管的 pending 计划无老化、无计数、无留痕

**生产实测**：全库 `pending` 11 条，**最老 2026-07-05（80 天）**，全部 `snapshot_id=None`（legacy 计划），分属 4 个非活跃用户。与之对应的 `committee_consensus` 行数分别为 1~14 次——**说明这些计划被反复评审了十几次，每一轮都失败，每一轮都原样退回**。

**三条缺失**（全部【代码实读】）：

1. **无失败计数**：`execution_plans` DDL 无 `attempt_count` 类字段。
2. **无老化清理**：`scheduler.py` 全部 23 个 `job_` 中**无一处理 pending 老化**。
3. **失败零留痕**：`confirm_and_execute`（`trade_executor.py:226-245`）的两个失败分支——异常回滚、业务错误回滚——**只有 `logger.warning`，没有 `record_operation`**。

第 3 条在生产库得到直接印证：`operation_log` 最近 47 条决策相关记录**全部是 `success`，无一条失败记录**。而 `execute_trade` 拒绝成交的三种主路径（legacy 未绑定 `:105-108`、约束不通过 `:123-127`、无真实市价快照 `:143-144`）**都只写 logger**。

**结果**：一个反复失败的计划在任何用户可见界面（`operation_log`、推送、前端）上都是**完全隐形的**，只会永久占用 pending。这也解释了为什么那 11 条能躺 80 天。

---

### N-3 【中危 · 因果链澄清】扩张驱动器没有独立下单权，落不落地取决于 LLM 是否恰好选中同一票

**这一条更正是我自己的判断**：我先前说"两股扩张力各自按整份现金算预算，叠加后可超可用现金"。**跨计划现金累计确实不存在**（`grep cash_left|remaining_cash|running_cash|cash_used|total_planned` 零匹配），但**真正的问题不在超支，而在这里**：

**`exec_plans` 完全来自 L4 的 LLM 输出**（`decision_engine.py:2284` `exec_plans = result.get("execution_plans", [])`）。两个驱动器产出的 `driver_plans` **不自己造单**，只对 LLM 已经提出的计划做两件事：

1. 豁免三道建仓侧门禁（`:2355` / `:2366` / `:2426`）；
2. 改写股数（`_gap_fill_shares`）。

**所以逻辑是**：L3 产出缺口意图 → **如果 L4 的 LLM 恰好也对同一票提了 buy/add** → 该计划被豁免并定股 → 落地。**如果 LLM 没提，缺口意图就此蒸发。**

**生产实证**：今日 L3 产出 7 条 `gap_driven` 意图，L4 生成 7 条计划，`execution_plans` 里**零扩张标记**——两批票并不对应（L3 是 MSFT/DELL/TSM/NVDA，L4 落地的部分不同）。**今天没有一条缺口意图走完闭环。**

**进一步**：即便走通，`execution_plans` 也**不保留 `gap_driven` 标记**（因为 LLM 重写了 `result_json`），所以**事后无法审计一笔成交流是来自缺口驱动还是 LLM 自主判断**。

**判定**：第一轮把扩张力做成了"**给 LLM 的单子开绿灯**"，而不是"**系统自己下单**"。这与其设计文档"确定性的缺口→建仓意图生成器"（第一轮原文用词）不符。**未找到任何测试覆盖这个耦合。**

> 附带：因此我先前那条"扩张力叠加超现金"应降级表述为——**理论风险存在（L4 生成期只做逐笔校验、`account` 快照循环前读一次不重读 `:2131`），但有 `trade_executor.py:281` 成交层独立复核兜底，现金不会变负；代价是同轮后到的计划静默失败回滚**。生产**未见实际发生**。这两条与 N-2 叠加才是真问题：静默失败回滚产生的 pending 恰好落进 N-2 的无留痕黑洞。

---

### N-4 【中危 · 可审计性】计划与"生成它的那份 L2"之间没有版本外键

投委会按 `store.get_latest_strategic_plan()` 读最新一张 L2 来评，L4 按 `_l2_target_weights(store.get_latest_strategic_plan())` 取目标权重。但：

- 计划表**没有 L2 版本外键**，`_planned_amount` 是塞在 `result_json` 里的**裸数值**。
- 计划生成时的 L2 与评审时回读的 L2 **可能不是同一版本**（L2 每日可重生成）。
- `execution_plans` 有 `snapshot_id` / `strategy_version`（研究快照绑定，管**数据时效**），但**没有管"决策依据版本"的字段**。

**后果**：事后无法回答"这条计划是按哪一版目标权重算出来的、评审它的是哪一版"。对自进化/复盘回路是硬伤。

---

### N-5 【低危 · 一致性】`run_full_refresh` 缺两条保护

`_reuse_escape_reason` 全仓**只有 `:2856` 一个调用点**（在 `run_daily_decision`），`缺口未纠正` 留痕也只在 `run_daily_decision`。`run_full_refresh`（`:3035-3103`）顺序直调各层，**既无 P1-1 逃逸阀、也无 P1-2 留痕**。

**意味着**：UI 上"一键全量刷新"这条路径上，第一轮诊断的不对称**原样保留**。

---

### N-6 【低危 · 工程】前端自检无门禁，提示词改动无测试

- `tests/frontend/*.mjs`（P2-2 的自检）靠 `node` 手工跑，pytest 不收集，`.github/workflows/` 不存在。**六组断言写得很好，但没人保证它还会被跑。**
- P2-1 的提示词双边表述**零测试**（`grep decision_execution tests/` 只命中列名与算 hash）。

---

### N-7 【低危 · 测试盲区】P0-3 端到端未验证

`tests/test_gap_execution.py` 7 条**全是 helper 级**。`grep -l run_execution_plans tests/` 命中的 `test_decision_8b2.py` 里 3 条用例**无一条带驱动标记**；`test_stage_snapshot.py` grep `gap_driven|opportunity_driven|_planned_amount` **零命中**。

**结论**：没有任何测试证明一张带 `gap_driven` 的 L3 计划真能穿过 L4 变成 `execution_plan`。**这正是 N-3 那个耦合没人发现的原因。**

---

### N-8 ~ N-20：L1-L2-L3 主链补充复核

> 以下由一路后台审计产出，**我已逐条 grep/读原文独立复核，只保留属实的**；其中一处我做了更正（N-14 附注），一处我发现了审计漏掉的代码（N-11 附注）。

**N-8 【中高危 · 与 N-3 叠加】** **L1 会长期"僵尸有效"，而它是缺口的源头。**
- `get_latest_macro_strategy`（`store_decision.py:156-170`）读 `WHERE status='valid' ORDER BY version DESC`，**查不到还有一层 fallback**：取版本最新一条（`:163-168`）。⇒ `needs_major_revision` 状态下依然取得到。
- `create_macro_strategy` 落库**硬写 `status="valid"`**（`store_decision.py:147`）——另两档只在日检时由 `update_macro_status` 补写。
- 日检只在 `status == "needs_major_revision"` 时重生成（`decision_engine.py:783-787`），否则 `update_macro_status(..., status)` 把状态**又盖回 `valid`**（`:791`）。
- 而提示词在**主动压低触发率**：`decision_macro_check.md:47`「注意：大多数日子应该返回 `"strategy_status": "valid"`。只有真正的重大变化才需要 `needs_major_revision`。」
- **金融后果（这是与 N-3 叠加最危险的一条）**：风格已从 bull 切到 bear，但单日没跌 3%、VIX 没破 30 → 连续返回 `valid` → 那份 `regime=bull` 的 L1 永远被取到 → `get_allocation_bounds` 一直给 `equity_min=60` → **缺口驱动器持续要求把权益配到 60%+**。**L1 越迟钝，扩张侧越激进——方向恰好相反。**

**N-9 【中】`minor_tweaks` 白写。** LLM 认真给出的「把 X 从 A 调到 B」写进 `result_json["minor_tweaks"]`（`store_decision.py:194/201`）后**零读者**（grep 全仓仅写入侧 `:794` + 定义 + 一处文档）。微调是否生效纯属"整个 macro_json 被塞进 L2 prompt"的副作用，**无确定性落地路径**。

**N-10 【低·未来 bug 源】两套"是否重建"字段并存。** 提示词同时要求 `strategy_status`（字符串枚举）与 `major_revision_needed`（布尔，`decision_macro_check.md:43`），而**后者全仓零读取**（grep 只命中该 md 一行）。模型按契约填了 `true` 却把 status 填成 `valid` 时，重建**被静默跳过**。

**N-11 【高】置信度不进风险预算，`regime_confidence` 是纯装饰。** `conf_weight = (confidence-1)/9`（`regime_mapper.py:63`）**只喂 `recommended_equity`/`recommended_cash` 两个展示值**（`:69-70`）；返回字典里 `equity_min/equity_max/cash_min/cash_max/max_single_pct/beta_limit` **全是原值**（`:73-78`，已读全文确认）。
- **后果**：`confidence=2` 与 `confidence=10` 给出的**敞口上限完全相同**。系统在最不确定时给的仓位上限与最确定时一致——白算一轮多模型交叉验证。
- **而扩张侧恰恰由 `equity_min` 驱动**。正确的耦合应是"置信度低 → 下界放宽（少补）或不触发"，当前恰好相反。
- **【更正审计员】** 它称"三套 beta 上限并存且语义冲突，L2 的目标在 L4 处根本不合法"。**部分成立**：我查到 `decision_engine.py:2243-2245` 有一处它漏掉的 `min()` —— L4 把 `constraints["max_portfolio_beta"]` 与 `alloc_bounds["beta_limit"]` **取 min 后才校验**。所以 L4 并非"忘了 regime_mapper 的 beta"，而是**主动收紧**。真正的问题降级为：**三套数字（1.5 / 1.3 / 0.9）并存，用户在 UI 上看到的目标组合可能被下游静默否掉**，属可读性/预期管理问题，不是放行漏洞。

**N-12 【高】板块漂移算出来，零消费者。** `sector_drift` 出现在 `decision_engine.py:456/496/502/504/515` —— **全在函数内部**，外部仅一处测试引用（`test_decision_discipline.py:201`）。逃逸判据 `_reuse_escape_reason:838-841` 的候选集**只有权益/现金**，`top = max((("权益", …), ("现金", …)))`。
- **后果**：最典型的"策略已失效"形态——**板块结构被打乱**——完全不触发重生成；`rebalance_suggested=True` 只换回一行文案（`:1189-1196` 是终点，grep `web/static/` 找 `rebalance_needed` **无命中**）。**系统能识别"组合错了"但不会纠正它。**

**N-13 【中】钳制发生在落库之前，"目标"是被代码改写过的值。** `clamp_warnings = _clamp_target_allocation(result, alloc_bounds)`（`decision_engine.py:1008`）**早于** `store.create_strategic_plan(...)`（`:1025`）——已读原文确认。⇒ 库里的 `target_allocation` 是「LLM 意图 ∩ 代码边界」，随后被偏离计算、前端目标条、`_l2_target_weights`、机会驱动越线判据**共同当作"策略"**。
- **后果**：所有"实际 vs 目标"的偏离数字都在与**一个从未被选择过的数字**比较。极端情形（bear/defensive，`equity_max=25`，LLM 真给 55%）下会持续报"偏离目标"，而**真实组合与 LLM 判断是一致的**——方向性错误的警报。

**N-14 【中】类型错误/字段缺失静默放行。** `_clamp_target_allocation:383-392` 的 `max_single_stock_pct`/`max_portfolio_beta` **只有 `if … > cap` 一条分支，无下限**；三处 `isinstance(x, (int,float))` 守卫为假时**不钳、不告警、不写默认值**（`equity_pct` 双向，`:375-381`）。
- **后果**：LLM 若返回 `"equity_pct": "55%"`（字符串）→ 原样落库 → `_compute_deviation_drift:471` 的 `isinstance(target_equity, …)` 同样为假 → `has_target` 只由 `sector_targets` 决定 → `rebalance_suggested` 可能变 `None` → **`:836-837` 见 `None` 直接 `return {}` → 逃逸机制永久沉默**。一次字符串污染就能让整个 N-12/P1-1 机制失效。
- **附注**：`_generate_gap_driven_plans` 的驱动器**无 try 包裹**（`decision_engine.py:1614`），而机会驱动有（`:1620`，我读了原文确认）。两处失败模式不一致。

**N-15 【中高】投委会裁决无法回写 L1，"自进化闭环"缺最后一环。** `update_macro_status` **全仓唯一调用方是 `decision_engine.py:791`**（`run_macro_check` 的 else 分支）。投委会任何裁决都到不了 L1。
- **后果**：同一板块的买入连续被 `committee_risk.md` 的硬信条（「单行业不超过 40%」，`:16-18`）否决，而 L1 仍写着"超配该板块" → **上下游持续相互抵消**，每轮烧一次全链算力，用户看到"一直建议买、一直不执行"的两套说辞。

**N-16 【中】投委会独立性守卫只告警，不改判。** `committee.py:915-935` 检测到 `len(distinct_providers) <= 1` 时只 `logger.warning` + SSE `committee_diversity_warning`，**四票仍按四个独立意见计入 `approve_ratio`**。
- **后果**：一个 provider 大面积超时 → 四委员全落同一后端 → 交叉验证退化为"一个模型投四次"，而**四种 persona 提示词各异恰好掩盖了同源事实**（模型会顺着 persona 说话）。加权表决的权威性建立在独立性假设上，假设被破却无代价。

**N-17 【中】`_needs_discussion` 硬编码 2:2。** `committee.py:528` 写死 `if approve == 2 and reject == 2: return True` ——**4 人委员会假设**。委员数变化时（3:1 这种真实分歧）不触发圆桌，只剩 `max-min(confidence) >= 5` 一条线（`:530-531`）。

**N-18 【低】前端 `needs_review` 可读性两处不一致。** `decision.js:933-941` 的 `verdictLabel` **缺 `needs` 分支**（我读了原文确认：只有 modification/approve/reject/abstain 四支）→ `needs_review` **原样透出英文串**；而 `report-export.js:43` **有** `if (s.includes('discussion') || s.includes('needs')) return '需再讨论'`。同一条结论，记录页显示英文、导出报告显示中文。

**N-19 【低】过期机制形同虚设。** `valid_until_trigger` 只在 `store_decision.py:145` 写入、`store_schema.py:301` 定义，**零读取**；`expires_at` 只有 `store_schema.py:301-306` 定义，**零读取**（grep 全仓其余命中全属 `auth/` 的邀请码，无关）。

**N-20 【中】陈旧上游时"卖得出、买不进"，机制性来源确认。** 时序已核（grep 步骤序）：
- **Step 0.6 硬止损巡检**（`:2823-2826`）**先于** Step 3 L3（`:2891-2893`），且 `_hard_stop_loss_sweep` **零 LLM**，破位即产卖出。
- 而 L3 的陈旧闸（`:1378-1391`）**在 L1/L2 读取之后**（`:1367-1376`）→ 陈旧即 `return`；两个驱动器各自也有一道陈旧闸（`:1290-1295`、`:1823-1828`）→ `return []`。
- **⇒ 数据最陈旧的那天，系统只执行卖出**，把组合推成纯现金。这是第一轮"现金单向棘轮"的**机制性来源**，现在有日志（P1-2）但**行为不变**。
- **反过来说这是正确的保守**（不在陈旧数据上补仓）——问题不在停买，在**它没有恢复后的补跑**：`scheduler.py:1650-1700` 的 `_probe_business_staleness` 会为停滞的 L2 主动补跑，**但不管 L3 停摆这段时间欠下的配**。

### N-21 ~ N-32：扩张侧实现审计（第二轮，逐条自证）

> 方法：一路后台审计给出结论后，**未采信转述**，逐条 grep/读原文/本地复现/生产查库复证。
> 下面每条都附我自己的证据；**审计员报错的地方已更正**，未证实的直接剔除。

**N-21【致命·已本地复现+生产铁证】缺口驱动器的"收敛判据"把增量当存量比，已持仓核心票的补仓计划被静默归零、计划丢弃、驱动器永久卡死。**

`_gap_fill_shares`（`decision_engine.py:1962`）第一行判据：

```python
if existing_value >= planned_amount: return 0        # :1967-1971
```

`planned_amount` 是**本轮要补的增量金额**，`existing_value` 是**现有持仓总市值**——量纲同、含义不同。`:1969-1970` 的注释声称"持仓对应的目标差额正是 `_planned_amount`，故等价"，**这句推论是错的**。

我的复现（权益 10 万、L2 目标 12%、现持 3%、步长 1/3）：

```
第1轮: 现权重 3.00%  计划补 9,000 元 → 定股 60 股（成交 6,000）
第2轮: 现权重 9.00%  计划补 3,000 元 → 定股  0 股（丢弃）
第3~6轮: 完全相同 → 永久停在 9.00%，目标 12.0% 永远到不了
```

注意第1轮**自身就被截断**：计划 9000、只成交 6000（因 `existing(3000) >= ...` 之外，`cap_room`/`_round_lot` 共同作用）。规律是：**补的轮次越多、持仓越大，越必然触发 `existing >= planned`** —— 持得越多越必被归零，与设计意图完全相反。

**生产铁证（09-23 当日计划 × 现有持仓，环境内实算）：**

| 市场 | 票 | 计划额 | 现有持仓 | 判定 |
|---|---|---|---|---|
| us | MSFT | 27,596.72 | 0.00 | 可成交 |
| us | DELL | 27,596.72 | 27,503.12 | 仅剩 94 元（≈1 股） |
| us | TSM | 23,853.70 | 10,866.75 | 可成交 |
| **us** | **NVDA** | **21,859.07** | **26,672.40** | **归零 → 计划丢弃** |

NVDA 在 L2 里目标权重 **9%**、实际仅 **2.66%**——最典型的"该补没补"，恰是唯一被归零丢弃的那条。

**为什么测试全绿**：`tests/test_gap_execution.py:50-53` 把 `_gap_fill_shares(2000, 100, 100000, 5000) == 0` **写成断言**，docstring 还称其为"收敛判据"——把 bug 钉死为期望行为；`tests/test_gap_driver.py` 全文不含 `_gap_fill_shares`（`grep -c` = 0），只测 `_plan_gap_fills`，**两半从未被联合测试**。我把这条链路跑起来才暴露。

**N-22【中高·已本地复现·结论已更正】L4 用同一份未变动的账户/持仓逐笔校验 → 被绕过的是「日额度上限」，不是现金下限。**

> **本节是对我先前结论的更正，留痕说明**：我最初写的是"5 笔各 4000 全部放行 → 执行完现金 0、**15% 现金下限被击穿**"。本地复现（`c:\tmp\probe_n22b.py`）**否证了这个断言**，我据此改写：执行期**会**重读账户并**确实**卡住现金下限，被绕过的是**另一个**约束。原结论作废。

**事实一（先前的断言错在哪）**：`decision_engine.py:2346` 的 `for ep in exec_plans:` 循环内 `account` / `positions` 确实**从不被写入**，`_equity` 只在 `:2402` 读一次——**生成期**的逐笔校验全部基于同一份"买入前"快照。但**成交期并不共用这份快照**：

```python
# trade_executor.py:106 —— 每次成交都重读账户
account = store.get_sim_account()
validation = validate_execution_plan(plan, account, positions)
```

而 `validate_execution_plan` 里 `constraint_validator.py:275-282` 的 `min_cash_pct` 是**硬校验**：

```python
cash_after = cash - trade_amount
cash_pct = cash_after / total_equity * 100
if cash_pct < min_cash:
    result.add_violation(f"买入后现金比例 {cash_pct:.1f}% 低于下限 {min_cash:.0f}%")
```

实测（`c:\tmp\probe_n22b.py` 子案 (i)：权益 1,000,000 / 起始现金 400,000 = 40% / 5 笔各买入 100,000 = 各 10% 权益 / 下限 15%）：**生成期 5/5 全放行，成交期实际只成交 2/5，第 3 笔起因跌破下限被拦，全程最低现金 20%** —— 下限**守住了**。原因很简单：现金是**单调递减**的，逐笔重读即可兜住；生成期共享快照只会让**多出来的单子被拦在执行期**（整笔失败回退，用户在待确认队列看到"点了也成交不了"的单子），不会造成下限击穿。

**事实二（真正被绕过的约束）**：`constraint_validator.py:288-293` 的日额度上限**逐笔比对、从不累计**：

```python
max_turnover = total_equity * c["max_daily_turnover_pct"] / 100
if trade_amount > max_turnover:
    result.add_violation(...)
```

它拿**单笔金额**去比**当日总额度**，且不查当日已成交额。实测（`c:\tmp\probe_n22b.py` 子案 (ii)：权益 1,000,000 / 起始现金 1,000,000 / 10 笔各 50,000 = 各 5% 权益 / `max_daily_turnover_pct=30`）：

```
单笔额度校验：每笔 50,000 < 300,000（= 30% × 权益）→ 笔笔「不超额度」
当日实际累计成交：500,000 = 50% 权益   vs   上限 30%
```

**日额度形同虚设**——只要把一笔大单拆成若干笔不超过 30% 的单子，当日换手可以无上限。这与现金下限不同：换手是**累加量**，逐笔比对在数学上就不可能守住，必须在**成交期累计记账**才成立。

**可达性**：现金下限侧因此是"用户体验问题"（多出无效待确认单），**日额度侧才是真实的组合级风控缺口**。叠加两个驱动器各自对**全额现金**独立算预算（`_plan_gap_fills:1228`、`_plan_opportunity_fills:1762`），单日批量下多笔中等单的场景是常态而非例外。

**N-23【高】缺口驱动器的实质收敛判据是错的（N-21 的正确版本应在 L3 可满足的范围内）。**

L2 实测只承诺 **4 只核心票、合计权重 33%**（NVDA 9 / TSM 8 / DELL 8 / MSFT 8），而 L1 `sideways/balanced` 的 `equity_min = 40`。**哪怕 4 只全部补到目标、其余全空，组合权益也只有 33% < 40%**——缺口驱动器**永远不可能**把 `gap_pct` 补到零。因此 N-21 那条"补到目标即停"的判据在 L3 层就不成立（目标本身够不着），它的唯一实际作用就是**提前把已持仓票归零**。这是 N-21 之外独立的第二重缺陷：**判据不仅写错，而且指向一个不可达的目标**。

**N-24【中高】缺口驱动器豁免了 5 日同向冷却，而它唯一的刹车（N-21/N-23）是坏的 → 越跌越买的价值陷阱回路。**

`:2366` `if ticker not in gap_plans and _is_recent_duplicate(...)` —— 缺口驱动计划**豁免冷却**（机会驱动不豁免，这个不对称是刻意的、正确的）。但 `:2363-2365` 的注释把刹车寄托在"步长上限 + 达标即停"，而"达标即停"正是 N-21/N-23 里失效的那一个。驱动器以**权重**为目标：核心票下跌 → 权重跌破目标 → `tw - cur_w > 0.5`（`:1250`）持续成立 → 继续买。`_plan_gap_fills` 内部无任何价格/趋势/止损条件。**结论：跌得最多的核心票会被反复加仓。**

**N-25【中】"驱动计划"与"执行计划"之间没有确定性桥接，落地靠 L4 的 LLM 自觉。**

`_generate_gap_driven_plans` 写的是 `create_tactical_plan`（`:1336`，进 `tactical_plans` 表），而 L4 主循环遍历的是 `exec_plans = result.get("execution_plans", [])`（`:2282`，**L4 LLM 的输出**）。全文无代码把驱动计划合成执行计划；`gap_plans`/`driver_plans` 只用于**放行判定**（`:2355`/`:2426`）。提示词确实对冲了（`chain/prompts/decision_execution.md:142` 新增"优先按 Layer 2 目标权重对低配核心持仓生成加仓/建仓"），但那是**期望而非保证**。

> 审计员说"P0-3 的豁免实际只对从未持有过的名字有效"——**这条我确认为真但归因要改**：不是豁免机制的问题（三道门都开对了），而是门后面的 N-21 把单子归零了。

**N-26【中】机会驱动器"信念最高者优先补"是假象——排序发生在预算已经分完之后。**

`_plan_opportunity_fills:1805-1806` 的 `budget -= amount; cash -= amount` 在 `for tk_raw, v in (valuations or {}).items()`（`:1774`）循环体内执行，而 `out.sort(key=lambda f: f["score"], reverse=True)`（`:1807`）只是把**已经分完钱**的结果重排输出。预算实际按估值表的行序分配（`get_valuation_map`，`store_committee.py:358-375`，SQL 无 ORDER BY 语义保证）。`tests/test_opportunity_driver.py:98-109` 抓不到：三票档位上限恰好 4000/2000/1000，**总和正好等于 6000 预算**，全部足额——测试构造把 bug 掩盖了。

**N-27【中】文档明文的"B 类越线默认关闭（feature flag）"与"只有顶档信念才有资格越线"两条护栏均未实现。**

`decision_engine.py` 全文只有 5 处 `_os.getenv("BH_...")`（`:47`/`:817`/`:886`/`:1210`/`:1738`），**无任何越线开关**，机会驱动器恒定开启。且 `:2441-2448` 的越线判据只有 `_post_w > _l2_w + 0.05`，**与信念档位无关**——任何达到最低档（score ≥ 0.45，档位目标 4%）的票都能拿到 `mandate_exception`。
**缓解（必须一并说明）**：默认 `LEVEL_OFF` 下自动执行什么都不成交；`LEVEL_SEMI` 也会剔除带 `mandate_exception` 的计划。所以敞口是"多了一条要人工点的待确认单"，**不是静默自动越线**；但回滚粒度只能靠关掉整个自动执行。

**N-28【中】越线标记在硬校验与降级之前计算，与自身注释矛盾。**

`:2439-2448` 的注释写"用**实际定出的股数**判……若 L4 的硬校验/降级把仓位压回了目标之内，就不必再打扰人"；实际顺序是 `_sized` → 打标（`:2441-2448`）→ `_full_validate`（`:2490`）→ 两轮 repair（`:2494-2509`）→ `max_compliant_shares` 降级（`:2511-2519`）。被降级回目标以内的计划**仍带 `mandate_exception`**。方向安全（多要一次人工确认），但"越线集合 == 真越线集合"不成立，人工确认队列被污染。

**N-29【中】`mandate_exception` 不进投委会视野、不进 oplog。**

- `committee.py` 全文 **零处** `mandate_exception`（`grep -c` = 0）：越线计划与其他计划走完全相同的评审流程。
- `record_operation` 全文只有两处调用（`:2918` 质量门阻断、`:2963` 缺口未纠正），**无越线留痕**。
- 痕迹只存在于 `execution_plans.result_json` + 前端徽章（`decision.js:569-571`）。文档要求的"必上会""必留痕"只兑现了下限（所有 pending 都上会，顺带满足）。

**N-30【中高·B 类铁律名不副实】`equity_max` 不是"半突破"而是硬拦，且该类计划无法降级、只能整体被拦。**

`validate_against_regime` 在 `_full_validate`（`:2484`）内，越线即 invalid；而 `max_compliant_shares` 的 limits 列表（`constraint_validator.py:351-363`）只含**单笔上限 / 单股占比 / 现金下限 / 日额度 / 板块集中度**——**不含 `equity_max`**。所以权益超上限的计划**没有降级路径**，只能被整体拦下。第一轮文档设想的"有界战术缓冲带 +10pct"未实现。

**N-31【中】跨市场守卫是空操作。**

`:1914`/`:1936`：`normalize_ticker(tk, market) != normalize_ticker(tk, tp.get("market") or market)` —— 两个实参是**同一个字符串**，恒等式恒真，`continue` 永不触发。`:1904-1906` 的 docstring 声称能排除"A股码 vs 美股同号"，**实际不能**。唯一后果是回落到普通门禁（偏保守），但注释在撒谎。

**N-32【低】同票被两个驱动器同时选中时，一方预算无声蒸发。**

`driver_plans = {**gap_plans, **opp_plans}`（`:2320`）——机会驱动的金额静默覆盖缺口驱动的金额，而两个驱动器各自都是对着**全额现金**独立算的。另：`existing_tickers` 被赋值两次（`:2300`/`:2311`），第一处是死代码；`save_stage_snapshot` 在逐票循环内（`:1334`）一轮 N 张快照，而机会驱动刻意改成每轮一张并写明理由（`:1854-1856`），两侧不一致；日志 `:1346` 打印的步长上限是 `deployable_cash×1/3`，与真实步长 `gap_pct/100×equity×1/3`（`:1231`）在现金 < 缺口时不等。

**N-33【中高·已本地复现·当前不可达】`sim_positions` 同一账户同一标的存在「墓碑行 + 活行」时，`get_sim_position_any` 可能命中墓碑，导致买入写错行 → 组合市值重复计数、减仓卖错行。**（**已于 2026-09-24 由 P2-I (a) 落地防御；VIP 不下单已定调 → (b) 不做。见第六节**）

VIP 导入路径的 `materialize_portfolio`（`vip/portfolio.py:552-556`）**从不删除**旧持仓，只把它们**清零为墓碑**（`shares=0, market_value=0`），再 `create_sim_position` 新建活行。因此同一 `account_id + ticker` 可以有多行并存（`sim_positions` **无 UNIQUE 约束**，`store_schema.py` 仅有 `idx_sim_positions_account` / `idx_sim_positions_market`）。而两处读取函数都是 `fetchone` **且无 `ORDER BY`**：

```python
# store_simtrading.py:468-479 —— 无 ORDER BY，命中哪行由 SQLite 的行序决定
def get_sim_position_any(self, account_id, ticker):
    "...AND shares > 0..."  # 注意：这里刻意【不】过滤 shares
    row = conn.execute(q, p).fetchone()

# store_simtrading.py:456-466 —— 同样 fetchone 无排序（但有 shares > 0）
def get_sim_position(self, account_id, ticker): ... "AND ticker = ? AND shares > 0"
```

`trade_executor._execute_buy`（`:295-306`）正是用 `get_sim_position_any` 找"有没有可复用的行"：

```python
pos = store.get_sim_position_any(account["id"], ticker)   # ← 可能拿到墓碑
if pos: store.update_sim_position(pos["id"], shares=new_shares, ...)
else:   store.create_sim_position(...)
```

**本地复现（`probe_tomb.py`）**：同一账户同一票造墓碑（`shares=0 @200`）与活行（`shares=200 @150`）：

```
get_sim_position_any → id=a9f5c354a5b2 shares=0 avg_cost=200.0   ← 命中墓碑（应复用活行 d717154922b7）
写入墓碑后同票两行均 shares>0：
  a9f5c354a5b2 shares=50  mv=9,000.0
  d717154922b7 shares=200 mv=36,000.0
组合市值合计 = 45,000.0   ← 同一只 'NVDA' 被计入两次（重复计数）
```

**生产实测（`probe_dc_dup.py`）**：危险形态（墓碑+活行并存）共 **25 票**，但**全部落在 VIP 账户**：

| 账户 | ref | 危险票数 |
|---|---|---|
| `290ea37707b1` | NOMURA | 7 |
| `de2eda65d0d6` | CITI-1 | 18 |
| **决策中心自有账户（`account_ref=''`）** | — | **0** |

**可达性限定（这条决定它现在是不是真缺陷）**：`create_execution_plan` **无 `account_ref` 参数**，且

```
grep -rn "execute_trade\|confirm_and_execute" bottleneck_hunter/vip/  →  零命中
```

**VIP 路径从不下单**。`scheduler.py` 的三处 VIP 作业（`:410` / `:1062` / `:1726`）也都带 `if not ref: continue`（**刻意跳过**决策中心账户，方向相反）。因此成交恒落在 `account_ref=''` 上，而该账户**零命中**危险形态——**现状不可达**。它是一颗**潜伏雷**：一旦将来出现"在 VIP 账户上成交"的功能（VIP 顾问的减/持/加建议若接成可执行单），重复计数与卖错行会立刻发生。

**附带卫生问题**：全库 `sim_positions` 148 行，**活跃 39 / 墓碑 109（74%）**。墓碑只增不减，且是上面这个歧义的唯一燃料。

**N-34【中·生产实测 + 已本地复现】零股票持仓账户（纯结构性产品/衍生品）的两道导入护栏**同时**失效 → 最终净值由「上传顺序」而非「结单期次」决定，账户权益长期错版（**已于 2026-09-24 改回 07-31 期真值，见 P1-K 执行记录**）。**

生产实测 `2952e48930e1`（CMBIS / 招银国际）：

```
cash_balance =     8,037.40
total_equity = 1,065,337.14
cash + Σ(market_value of shares>0) = 8,037.40
偏差 = −1,057,299.74        ← 全库唯一对账不符的账户（其余含 CITI-1 / NOMURA 均为 +0.00）
```

该账户的 6 份结单**全部是「0 只持仓，另 N 笔结构性产品/衍生品」**（`vip_imports.summary` 原文），因此 `positions` 表**零行**（实测 `SELECT COUNT(*) FROM positions WHERE account_ref='CMBIS'` → 0）。

> **关于我的复现与真实调用链是否一致（我自己提出的疑点，已查证）**：我的复现脚本里 `positions` 表恒为 0 行，一度疑为探针的提交瑕疵。逐行查证后确认**与真实链路一致**：写 `positions` 的唯一入口是 `_upsert_position`（`vip/portfolio.py:287`），而它**只在 `normalize_statement` 的 `if stmt.holdings and source_doc_id:` 分支内被调用**（`:240`）。CMBIS 结单 0 只持仓 → `stmt.holdings` 为空 → **从不进入该分支**。因此"零持仓账户永不写 `positions`"是真实行为，不是探针 artifact；生产实测 `positions` 表只有 CITI-1(130 行) 与 NOMURA(48 行) 两个账户，CMBIS 一行都没有。，FCN 只在 `vip_derivative_terms`（5 行，`XS3372957897` / `CMBIGP`），`vip_projections` **0 行**（CITI-1 有 779、NOMURA 有 395）。两行 `sim_positions` 均为 `XS3372957897` 且 `shares=0`——即 `materialize` 建的活行，后来被下一次 `materialize` 清零成墓碑。

**根因在 `_overwrite_guard`（`vip/portfolio.py:399-424`）的两道护栏对本类账户全失效**：

1. **陈旧分支**：唯一证据源是 `SELECT 1 FROM positions WHERE as_of_date > ?`——而**零持仓账户永不写入 `positions` 表**，故恒查不到"更晚日期"。
2. **骤降分支**：要求 `existing_n > 0`（`existing_n = len(get_sim_positions(acct_id))`）——**也恒为 0**。

于是任何一份月结单都能长驱直入。而 `materialize` 的 `total_equity = account_total_usd if ... else computed`（`:559`）**优先采信结单权威锚**，紧接着 `:589` 无条件写回账户——**净值因此由"最后上传哪份 pdf"决定**。

**本地复现（`probe_n34.py`，按生产真实上传顺序喂 4 期）**：

```
[1] 导入 2026-07-31 → eq= 1,063,096.50   positions表行数=0  guard=None
[2] 导入 2026-06-30 → eq= 1,057,266.50   positions表行数=0  guard=None
[3] 导入 2026-07-07 → eq= 1,057,902.50   positions表行数=0  guard=None
[4← 上传得最晚，却是最旧的一期] 导入 2026-04-30 → eq= 1,065,337.14  guard=''（放行）
最终 = 1,065,337.14 / cash=8,037.40   ← 与生产 CMBIS 实测逐位一致
```

生产 `vip_imports` 里 `M381691-20260430-Monthly.pdf` 的 `created_at` 正是 **`2026-08-03T02:48:20`（最晚的一份）**，`sim_account.updated_at` 同为 `2026-08-03T02:48:20` —— **最旧的一期覆盖了最新的一期**，铁证闭环。

**影响面**：`build_account_summary`（`vip/portfolio.py:1934`）的 `total = account.get("total_equity")` 直接把它当日志与报告的头条总权益；`value_series`（`:1782-1794`）虽然对纯衍生品账户走 `_import_total_series` 逐期权威值（口径正确、曲线不受此影响），但**头条与曲线会不同口径**。另外这类账户**每份结单都会新建活行再被清零**，正是 N-33 墓碑堆积的主要来源（CMBIS 2 行、全部为墓碑）。

---

**N-35【中高 · 生产实测 · 新发现】衍生品条款按「上传时间」而非「结单期次」选当期 → CMBIS 对外展示的 FCN 持仓明细与 MTM 是**过期期次**（07-07 与 04-30），且包含一笔**已于 2026-05-15 到期**的产品。**（**已于 2026-09-24 由 P1-L 修复：MTM `2,099,173.74`→`1,047,068.00`。见第六节**）

这是我在为 N-34 筹备数据修复时、解密 `parsed_json` 全量比对后**新发现**的问题。它与 N-34 **同源**（都由那次乱序上传触发），但**独立成立**：即使结单按期次顺序上传，只要**同一条款分多期上传**，选当期仍会错。

**机理**：`_current_derivative_rows`（`vip/portfolio.py:874-893`）的注释声称「同一 (family, underlying, lot_key) 只取**最新一期**(MAX created_at)」，SQL 用的是：

```sql
SELECT ... FROM vip_derivative_terms WHERE account_ref = ? AND is_indicative = 0
GROUP BY product_family, underlying_symbol, lot_key ORDER BY _mx DESC
```

`MAX(created_at)` 取的是**数据库插入时刻**，**不是结单的 `period_end`**。两者只在"按期次顺序上传"时才一致。生产 CMBIS 的 NVDA 同券（`XS3372957897:2026-09-04`）三行：

```
2026-08-03T01:14:10   M381691-20260731-Monthly.pdf   mv= 1,047,068.00   ← 真·最新期次
2026-08-03T01:16:41   M381691-20260630-Monthly.pdf   mv= 1,051,838.00
2026-08-03T10:48:02   M381691-20260707-Daily.pdf     mv= 1,041,874.00   ← 被选中（插得最晚）
```

**选中的是 07-07 那份日结单**（`10:48:02` 晚于 `01:14:10`），比 07-31 的月结单**旧了 24 天**。

**生产实测的当期构成（`is_indicative=0`）**：

| 标的 | 被选中的来源 | MV | 期次 | 问题 |
|---|---|---|---|---|
| NVDA | `20260707-Daily.pdf` | 1,041,874.00 | 07-07 | 比 07-31 **旧 24 天** |
| CMBIGP | `20260430-Monthly.pdf` | 781,072.92 | **04-30** | 结单期 04-30，**到期日 2026-05-15 已过** |
| CMBIGP | `20260430-Monthly.pdf` | 276,226.82 | **04-30** | 同上（HKD 份额） |
| | **合计** | **2,099,173.74** | | |

而 **07-31 结单的真实组合市值只有 1,047,068.00**（单笔 NVDA FCN）——该表 `20260731-Monthly.pdf` **只有那一行**，`20260430-Monthly.pdf` 的 CMBIGP 两笔在 07-31 期**并不存在**。

**影响面（用户可见）**：`_derivative_mtm_total`（`:915`）被 `derivative_summary.mtm_total_usd`（`:1488`）、`derivative_exposure`（`:1984`）与 `value_series` 的兜底锚（`:1798`）消费 → 顾问页"衍生品当期 MTM 合计"显示 **2,099,173.74**，比真实值 **1,047,068.00 高估 1,052,105.74（约 2.0×）**；`_derivative_holdings`（`:1023`、`:1591`）还会把两笔**已到期**的 CMBIGP 当成现持仓并入概览持仓构成。
（`value_series` 对 CMBIS 走 `_import_total_series` 的逐期权威值，**曲线本身正确**，不受此影响——这与 N-34 的"头条正确/曲线错误"恰好相反，两处口径各自分裂。）

**改法（P1-L）**：见下文。

**N-36【中 · 工程 · 新发现】`vip_derivative_terms.created_at` 用**北京本地时间裸写**，违反项目 UTC 存储约定；且它正是 N-34 修复所依赖的排序键。**（**已于 2026-09-24 由 P0-F 修复：改走 `store_base._now_iso()`，与同事件导入时间戳由差 8 小时变为 <5 秒；历史行按原方案不迁移。见第六节**）

`derivatives.py:821` 写入 `datetime.now().isoformat()` —— **naive、无时区、取容器本地时间**。生产实测同一导入事件在两张表的时间戳：

```
vip_imports.created_at            = 2026-08-03T02:47:41+00:00   ← UTC，7 处一致
vip_derivative_terms.created_at   = 2026-08-03T01:14:10.607418  ← 裸本地，无后缀（01:14 是 UTC 巧合，
                                                                   10:48 那批即 +8 后的北京时刻）
```

同一批导入里 `02:48:20`（UTC）在衍生品表里是 `10:48:20`——**相差 8 小时**，正是容器 `Asia/Shanghai`。项目内其它 store 一律走 `store_base._now_iso()`（`datetime.now(timezone.utc)`），**全代码库仅此一处裸写**（`grep "datetime.now().isoformat()"` 唯一命中）。

**为什么它不只是"不规范"**：
1. N-34 的修复方案 (a) 与 N-35 的修复方案**都要依赖该表的 `created_at` 做期次/新旧判定**——键本身就脏，判据会跟着脏。
2. 该列是 `_current_derivative_rows` 的排序键（`ORDER BY _mx DESC`），**时区漂移会直接改变"当期条款"的选取**。
3. 库里此列是"裸本地字符串"与"其它表 UTC 带后缀字符串"**混排**；`created_at` 若有跨表比较，8 小时偏移会翻转同日排序。
4. 服务器实际时区是 `Asia/Shanghai`（当前取值正确），一旦容器以 UTC 启动（`TZ` 未固定），**所有历史行的相对顺序会与新增行错乱**。

**改法（P0-F）**：见下文——一行替换。

**附：本轮执行生产数据修复时发现的同域问题（低 · 工程）** —— `vip_account_log.event_type` 的 **CHECK 约束取值集过窄**：

```sql
event_type IN ('projection','calibration','anomaly','settlement')
```

上述四类都是**系统自动事件**，**没有容纳"人工干预"**的位置。本轮经授权的人工修复（把 CMBIS 权益回填到 07-31 期）想留痕时**被该约束直接拒绝**（`sqlite3.IntegrityError`），只能借用语义最近的 `calibration` 落库；这样做的代价是**事后无法从 `event_type` 区分"系统校准"与"人工改数"**——而审计场景恰恰最需要区分这两者。建议：给取值集补一个 `manual`（或 `repair`），并把落库方（运维脚本）与自动方（`log_account_event` 的默认参数）**在类型上分开**。风险极低（`ALTER` CHECK 需重建表或走 `PRAGMA writable_schema`，属小型迁移，可并入 P1-K/P0-F 同批）。

---

### 仍然存在的收缩/扩张不对称（根因只补了一半）

| # | 不对称 | 证据 |
|---|---|---|
| 1 | **现金上限侧零拦截路径** | `cash_max` 全仓零消费方。`min_cash_pct`（`:278-282`/`:354`）是**买侧最低现金**，方向相反。第一轮诊断的另一半（"现金被当底线"）**未对称化**。 |
| 2 | **失败模式不对称** | `_hard_stop_loss_sweep:2615` 由 `:2826` **无条件调用**（零 LLM、照常产卖出）；两个驱动器遇上游陈旧即 `return []`。**数据陈旧时收缩照跑、扩张停摆**——如今只多了一条 P1-2 日志，**行为不变**。 |
| 3 | **无超配→减仓驱动器** | `grep overweight\|超配\|_plan_trim` 在 `decision_engine.py` **零命中**。只有上限校验器；"实际超配于目标"无主动收敛逻辑。对称性只做了"补低配"这一半。 |
| 4 | **`run_full_refresh` 两条保护缺失** | 见 N-5。 |
| 5 | **L2 顶层 `target_allocation.equity_pct` 竟无消费者** | 生产实测：L2 写 `equity_pct:51` 且 `core_holdings` 权重合计 **33%**，而驱动/逃逸/前端对照条**只读 `core_holdings[].target_weight_pct`**。缺口驱动器因此把「组合权益」往一个**自己的 L2 从未设定的 40% 下限**推，同时**永远够不着**（33% < 40%，见 N-23）。派生后果：`strategic_plans.result_json.target_allocation.equity_pct` 当前是**装饰字段**；**前端「目标 vs 实际」对照条的权益目标可能口径不符**——若该 51% 源自 LLM 而 `core_holdings` 权重由代码覆盖（N-13 的钳制链），三者会互相打架。**我用自身账户（零成交）复现了该形态，但尚未回放决策日志确认 09-23 真实用户是否同样命中**，故标为待证实。 |

---

## 四、新开发方案

**原则**：先修"放行"与"隐形"两类问题（N-1、N-2）——它们直接产生错误的成交和不可见的失败；再补"扩张力没有独立执行权"（N-3）——这是第一轮方案的核心承诺没兑现的地方；其余为打磨。

### P0 —— 必须先做（安全性与可观测性）

**P0-A　修复 N-1：非结论性裁决必须落动作，且强制不进自动执行**

- **改法**（最小）：`committee.py` gating 补 `else` 兜底——`needs_review` / `needs_discussion` / `unknown` 一律 `reject_execution(plan_id, f"{BLOCK_MARKER_COMMITTEE} 结论不可背书：{verdict_raw}，须人工复核")`。
- **同时**：`_regate_after_challenge`（`:1259`）补同构兜底。
- **加固**：`auto_execute_pending` 增加一道独立判据——只执行"存在一条 `final_verdict ∈ {approved, approved_with_modifications}` 的共识"的计划。**不能只靠 status**（`revert_to_pending` 会丢结论，N-1 放大因素 2）。
- **落点**：`committee.py`（两处）+ `auto_execute.py`。
- **验证**：`c:\tmp\probe_needs_review_gating.py` 已有现成复现脚本（断言 `not executed`），改成正式测试；加一条断言"共识保存失败时计划不得被自动执行"。
- **风险**：低。只把"不动作"改成"拒绝"，方向是收紧。
- **回归注意**：现网若有用户依赖"待议也能成交"，此改会显性拦截——**这正是期望行为**，但需在更新历史里说明。

**P0-B　修复 N-2：失败留痕 + pending 老化 + 失败计数**

- **改法**：
  1. `confirm_and_execute` 的两个失败分支、`execute_trade` 的三条拒绝路径，补 `record_operation(..., category="error")`（复用质量门阻断留痕的既有模式，`_PUSH_CATEGORIES` 已含 `error`，自动进推送）。
  2. `execution_plans` 加 `attempt_count INTEGER DEFAULT 0` + `last_error TEXT`，每次失败累加。
  3. `scheduler.py` 加一个 `job_expire_stale_pending`：`status='pending' AND created_at < now-N天 AND COALESCE(resting_until,'')=''` → `expire_execution` + 留痕。N 默认 14 天（与挂单上限对齐）。
- **落点**：`trade_executor.py` + `store_schema.py` + `scheduler.py`。
- **验证**：造一条注定失败的计划，跑两轮，断言 `attempt_count==2` 且 `operation_log` 有两条 error；造一条 15 天前的 pending，断言被 expire 且留痕。
- **风险**：低。纯增量。

**P0-F　修复 N-36：`vip_derivative_terms.created_at` 改回 UTC（一行）**

> ✅ **已于 2026-09-24 执行**（`derivatives.py:795,825`）。验证与门禁见**第六节**。

- **现状**：`vip/derivatives.py:821` 写入 `datetime.now().isoformat()`——naive 本地时间、无时区后缀。**全代码库唯一一处裸写**（其余一律 `store_base._now_iso()`），生产实测与 `vip_imports` 同事件相差 8 小时（`02:48:20+00:00` vs `10:48:20`）。
- **改法**：改成 `store_base._now_iso()`（`datetime.now(timezone.utc).isoformat(timespec="seconds")`）——与全库其它表同口径。若该文件不便导入 `store_base`，直接用 `datetime.now(timezone.utc).isoformat()`。**一行。**
- **顺带（可选，一并做）**：该列现存历史值为裸本地串，**不要**写数据迁移去回填——它的相对顺序在 `Asia/Shanghai` 容器里仍是对的（漂移是常数 8h，排序不变）；一旦迁移跑错反而打乱顺序。**只修写入侧，历史行不动。**
- **验证**：新增一条条款，断言 `created_at` 带 `+00:00` 后缀，且与同一导入的 `vip_imports.created_at` 相差 < 5 秒（修复前相差 8 小时，**必红**）。
- **风险**：极低。方向是把写入对齐既有约定，不读旧值、不改 schema。

### P1 —— 兑现第一轮承诺

**P1-A　修复 N-3：让扩张意图拥有一等执行权（不再依赖 LLM 巧合）**

这是第一轮方案**核心承诺**未兑现处。最小改法：

- 在 L4 循环**结束后**，对 `driver_plans` 中**未被 LLM 计划覆盖**的票（即 LLM 没提的），用现成的 `_gap_fill_shares` **追加一条确定性计划**，走与 LLM 计划完全相同的校验/投委会/留痕路径。
- `result_json` 保留 `gap_driven` / `opportunity_driven` 标记（**当前会被 LLM 重写覆盖**），使事后可审计。
- **不新增 LLM 调用**，复用 `_plan_gap_fills` 已算好的金额。
- **落点**：`decision_engine.py` L4 循环尾部（`:2600` 附近）。
- **验证**：先补 N-7 的端到端测试——构造缺口账户 + 一个**不点该票**的假 LLM 输出，断言仍生成带 `gap_driven` 的执行计划。
- **风险**：中。这会真正开始下单，**必须**先完成第一轮 §4 要求的收敛验证（见 P1-B）。

**P1-B　补做第一轮 §4 的端到端收敛验收（此前被跳过）**

- 写一个 `scripts/verify_gap_convergence.py`（或测试）：模拟盘账户现金 90% + L2 目标权益 60%，**跑 3~5 轮**，断言：
  1. 权益占比**单调逼近**目标带内；
  2. 停在带内后**不再产生新计划**（不刷单）；
  3. 每轮总额 ≤ 步长上限。
- **落点**：`scripts/` 或 `tests/`。
- **验证**：脚本自身即验证。
- **风险**：无。

**P1-C　补齐 `run_full_refresh` 的两条保护（N-5）**

- 把 `_reuse_escape_reason` 判据与 `缺口未纠正` 留痕抽成共享 helper，`run_daily_decision` 与 `run_full_refresh` 同时调用。
- **落点**：`decision_engine.py`。
- **风险**：低。

**P1-D　`cash_max` 要么接消费方，要么删**

- 现状是**死字段**——算出来没人读，比不算更糟（让人以为已对称化）。
- **建议**：接进 P1-2 的留痕与偏离报告（"现金超配 N%"），与 `equity_min` 对称。**若暂不接，就删掉**，别留着误导。

### P2 —— 打磨与门禁

**P2-A　前端自检进 pytest**：用一个 `test_frontend_selfcheck.py` 通过 `subprocess` 调 `node`，无 node 则 `skip`。让那六组断言真正受门禁保护。

**P2-B　`needs_review` 可见化**：前端 pending 区加"结论不可背书·须人工复核"徽标；统计口径把它从"待议"里拆出来单列。

**P2-C　B 类越线（第一轮 P0-4 的 B 类）**：**建议继续不做**。当前 A 类尚未在生产证明有效（见 2.2），B 类是第一轮自己都标注"风险：高，默认关闭"的部分。**先把 A 类跑通再谈。**

---

### L1-L2-L3 主链的修复方案（对应 N-8 ~ N-20）

**P1-E　修复 N-8+N-11：让 L1 别"僵尸有效"，让置信度进风险预算**

这两条是**同一条因果链**（僵尸 L1 把 `equity_min=60` 长期挂着 → 置信度又不参与 → 扩张侧照买），必须一起修。

- **N-8 最小改法**：给 `get_allocation_bounds` 加一道**陈旧度闸**——`equity_min` 的触发条件从"只看是否低于下界"改为"低于下界 **且** 该 L1 未超期"。超期阈值复用现成的 `_STALE_UPSTREAM_DAYS=8`；超期时缺口驱动器**不补仓**（与 `_plan_gap_fills:1290-1295` 已有的陈旧闸同构，只是那里查的是 L1/L2 的 age，这里要查的是"regime 判定本身是否可信"）。
- **同时**：修 `decision_macro_check.md:47` 那句压低触发率的引导，改为"若市场风格可能已切换（regime 存疑），应返回 `needs_major_revision`"；并**删掉零读取的 `major_revision_needed` 布尔**（N-10），只留一个契约字段。
- **N-11 最小改法**：`get_allocation_bounds` 里让 `confidence` 影响**下界**（扩张侧的触发线），不只是 `recommended_*`。最省的做法：`equity_min_effective = equity_min - (1 - conf_weight) * (equity_min - equity_min_at_bear)` 之类的**保守插值**——置信度低 → 下界下移 → 少补甚至不补。**上界不动**（不放松风控）。
  - `ponytail:` 这条只改一个函数、不动调用方；具体插值公式建议先拿 `test_decision_gap_*` 那几条用例的数值手算一遍再定。
- **落点**：`regime_mapper.py`（`get_allocation_bounds`）+ `chain/prompts/decision_macro_check.md`。
- **验证**：加测试断言「同一 regime 下 `confidence=2` 的 `equity_min` 低于 `confidence=10` 的」；加一条断言「L1 超 8 天时缺口驱动器不产计划」。
- **风险**：中。会**直接减少**扩张侧下单量——但方向是收紧，且正是第一轮诊断要的。

**P1-F　修复 N-12：把板块漂移接进逃逸判据**

- **改法**：`_reuse_escape_reason:838-841` 的候选集从 `(权益, 现金)` 扩成 `(权益, 现金, 板块最大漂移)`。`_compute_deviation_drift` **已经在算并返回 `sector_drift`**（`:515`），只是没人读。
- **落点**：`decision_engine.py` 一个函数内的三行。
- **验证**：写一条测试——权益/现金都贴目标（drift < 15）但某板块漂移 > 15 → 断言逃逸。
- **风险**：低。**性价比最高的一条**（三行代码接通一个已经算好但被丢掉的信号）。
- **附带**：`rebalance_suggested` 同样零消费者（N-12 后半）。要么让它驱动一次 L3 重算，要么就别留——现在是"能识别、不纠正"。

**P1-G　修复 N-14：`_clamp_target_allocation` 的类型守卫不能静默**

- **改法**：`isinstance` 为假时**不再静默 return**——写 warning 进 `clamp_warnings`（既有机制，`:1009-1012` 会发 SSE），并对可修复的值做默认填充（如 `equity_pct` 缺失 → 用 `recommended_equity`）。
- **理由**：现在一次字符串污染就能让整个逃逸机制**永久沉默**（`rebalance_suggested` → `None` → `:836` 直接 `return {}`），且没有任何痕迹。
- **落点**：`decision_engine.py:364-400`。
- **验证**：喂 `{"equity_pct": "55%"}`，断言有 warning 且逃逸判据仍能工作。
- **风险**：低。

**P1-H　修复 N-8 附带的驱动一致性：`_generate_gap_driven_plans` 补 try 包裹**

- **改法**：`:1614` 照 `:1620` 的样子包 `try/except`（`logger.warning` 即可）。
- **理由**：现在是"驱动器抛异常 → 整个 L3 中断"，而同级的放大器却容错。**同一层两个驱动器失败模式不一致**是明确的疏漏，不是设计。
- **落点**：`decision_engine.py` 一行包装。
- **风险**：低。

**P2-D　修复 N-15：给投委会裁决一条回写 L1 的路（**建议先只做留痕，不做自动改 regime**）**

- **最小做**：把被否决的**板块**聚合计数（当前 `get_rejection_patterns` 是裸 SELECT，N-17 附带），超过阈值时**在 L1 日检的 prompt 里注入一条提示**（"该板块近期被投委会连续否决 N 次"）。
- **不做**：让投委会直接改 `regime`/`sector_allocation_bias`。那等于让下游改写上游的宏观判断，风险远大于收益。
- **理由**：现状是上下游**持续相互抵消**且无人知道；只留痕 + 进 prompt，就能让 L1 至少"看得见"分歧。
- **落点**：`store_committee.py:536-553`（加聚合）+ `decision_engine.py:729+`（日检 prompt）。
- **风险**：中（改 prompt 会影响 LLM 输出，需实测）。

**P2-E　修复 N-16：独立性守卫要有后果**

- **改法**：`distinct_providers <= 1` 时**不是只告警**——按 `1/len(providers)` 之类对权重打折，或直接把该轮结论降级为 `needs_discussion`（这会自动被 **P0-A** 的新兜底拦住）。
- **落点**：`committee.py:915-935`。
- **依赖**：**必须先完成 P0-A**，否则降级成 `needs_discussion` 反而会被自动执行放行——**修完 P0-A 才安全**。
- **风险**：中。

**P2-F　修复 N-17＋N-18：口径与展示**

- `_needs_discussion:528` 的 `approve == 2 and reject == 2` 改为按 `len(reviews)` 动态判"平局"。
- `decision.js:933-941` 补 `needs` 分支（照抄 `report-export.js:43` 那行），让两处显示一致。
- `store_decision.py:72` 把 `needs_review` 从"待议"里拆出来单列。
- **落点**：三处各一行。**风险**：低。

**P2-G　修复 N-9＋N-19：死字段清理**

- `minor_tweaks`：要么接进 L2 prompt 的**显式**段落（而非整包 JSON 副作用），要么删。
- `valid_until_trigger` / `expires_at`：要么接一个真实过期判据，要么删。
- **原则同 P1-D 的 `cash_max`**：**算出来没人读比不算更糟**——它让人以为这个机制存在。

---

### 扩张侧实现层的修复方案（对应 N-21 ~ N-32）

> N-33 / N-34 属**账户记账与导入护栏**域，修复方案见本章末「账户记账与导入护栏的修复方案」。

**P0-C　修复 N-21：定股判据从「本轮计划额」换成「L2 目标持仓额」——本报告最高优先**

这是全部 34 项里**唯一一个正在生产上主动制造错误行为**的缺陷：它把最该补的票（持仓已大、离目标最远）**恰好归零丢弃**。

- **现状**（`decision_engine.py:1962-1971`）：`if existing_value >= planned_amount: return 0`。`planned_amount` 是**本轮按缺口比例摊出来的增量**，不是目标持仓金额。
- **本地复现**（我自跑）：权益 10 万 / L2 目标 12% / 现持 3% → 第 1 轮计划 9,000 → 定股 60 股（成交 6,000）；第 2 轮起计划 3,000 而 `existing_value` 已 ≥ 3,000 → **定股 0 → 丢弃**，权重**永久停在 9.00%**，距目标差 3pct 再也不动。
- **生产铁证**（09-23 日志）：NVDA 计划额 21,859.07、现有持仓 26,672.40 → 归零丢弃；而 NVDA 在 L2 目标 9%、实际仅 **2.66%**——**全组合最该补的恰是唯一被丢弃的**。规律一句话：**持得越多越必被归零**。
- **改法**：`_gap_fill_shares` 增参 `target_value`（= `target_weight_pct / 100 * equity`），判据换成 `existing_value >= target_value`；`cap_room` 仍按 `cap_pct` 算（上限语义不变）。调用点 `:2432` 已在手边有 `_l2_w`，`_target_value = _l2_w / 100 * _equity` 一行即得，**不需要新数据来源**。
- **必须同时改测试**：`tests/test_gap_execution.py:50-53` 现在把 bug **钉成了期望行为**（`test_fill_stops_at_target_when_already_held` 断言 5,000 持仓 / 2,000 计划 → 返回 0）。改后断言应为「已达 **L2 目标** → 0；仅达本轮计划额 → 继续补」。**这条测试不改，修完必红，而最省事的"修法"就是回滚源码**——所以它是这次修复的一部分，不是附带。
- **落点**：`decision_engine.py:1962-1971` + 调用点 `:2432` + `tests/test_gap_execution.py`。
- **验证**：新增端到端用例——连跑 5 轮，断言目标票权重**单调逼近** L2 目标并落入 ±0.5pct 带；另断言 `existing_value > target_value` 时返回 0（"防每天刷单"的**原意**保留）。`tests/test_gap_driver.py` 与 `test_gap_execution.py` 分别只覆盖这个函数的一半（计划生成 / 定股），**两半从未联合测试**，套件全绿正是盲区所在——端到端用例补的就是这个缺口。
- **风险**：中高——它会**真的开始补仓**，且补的正是当前被丢弃的大额票。**必须与 P0-D 同批上线**：P0-C 打开的是"单票补得动"，P0-D 装的是"全组合每日本该有的刹车"；只开不刹，同轮多笔大额计划会一起通过（见 N-22，**该条结论已更正**：被绕过的是日额度上限，不是现金下限）。
- `ponytail:` 不引入新的目标权重来源，直接用 L2 `core_holdings[].target_weight_pct`（运行期唯一权威）。

**P0-D　修复 N-22：给「当日累计换手」装上真正的事前卡口**

> **本节随 N-22 的结论更正而重写**：原方案的目标是"防止现金下限被击穿"，复现证明**下限本就被成交期守住了**，故原目标作废；真实缺口是**日额度上限从不累计**。

- **现状（两件事，别混）**：
  1. **生成期共享快照**：`:2346 for ep in exec_plans:` 循环体内 `account` / `positions` **从不被写入**，`_equity` 只在 `:2402` 读一次 → 逐笔校验基于同一份"买入前"快照。后果**不是**击穿现金下限（成交期 `trade_executor.py:106` 每笔重读账户，`min_cash_pct` 硬拦，实测 5 笔只成交 2 笔、全程最低现金 20%），而是**产出一批执行期注定失败的待确认单**——用户体验问题。
  2. **日额度上限从不累计**（`constraint_validator.py:288-293`）：拿**单笔金额**比**当日总额度**，且不查当日已成交额。实测 10 笔各 5% 权益全部放行 → 单日累计成交 **50% 权益** vs 上限 **30%**（拆得更碎则更高，无上限）。**这才是真实的组合级风控缺口**，且数学上不可能靠逐笔比对守住（换手是累加量）。
- **改法**：
  - **(a) 生成期影子账本（原方案，保留但降级为目标 1）**：循环前取 `_cash_left = account["cash_balance"]`；每笔买入方向通过校验后 `_cash_left -= amount`；把 `max_compliant_shares` 的可用现金入参从原始 `cash` 换成 `_cash_left`。**本地变量、不写库**（落库仍由 `:2555-2567 pending_writes` 一次性做），不引入半执行状态。收益＝减少无效待确认单。
  - **(b) 日额度累计（新增，也是真正的目标）**：`validate_execution_plan` 增加"当日已成交额"入参；生成期从 `sim_trades` 按 **Asia/Shanghai 当日**（`created_at` 是 UTC，须按项目时区约定换算，**勿引入新时区**）汇总 `amount`，判据改为 `已成交 + 本笔 > max_turnover`。成交期沿用同一函数、传当刻真实已成交额 → 生成期与成交期**同一判据**，不会出现"生成期放过、成交期拦下"的错位。
  - **(c) 同一影子账本顺带覆盖换手**：循环内把本批**尚未落库**的计划金额也累加进"已成交"口径，否则同轮 10 条各 50,000（各 5% 权益）仍会全部通过、累计 50% 权益。
- **落点**：`decision_engine.py:2346-2570` 循环体 + `constraint_validator.py:288-293` 与 `:351-363` 入参 + `store_simtrading.py`（新增按日汇总 `sim_trades` 的只读查询）。
- **验证**：
  1. 现金侧：权益 1,000,000 / 现金 400,000 / 下限 15%，摆 5 条各 100,000 的计划 → 断言可用现金随前序计划递减：**第 1、2 条原样通过，第 3 条被缩量到 50,000，第 4、5 条缩量到 0（静默放弃，不再产出注定失败的待确认单），收尾现金恰好停在 15%**（**修复前必红**：现状是 5 条全部落成待确认单、执行期 3 条失败回滚 —— 见 `c:\tmp\probe_n22b.py` 子案 (i) 的 2/5）。
  2. 换手侧：权益 1,000,000 / `max_daily_turnover_pct=30`，摆 10 条各 50,000 的计划 → 断言**累计成交额 ≤ 300,000**（**修复前必红**，实测 500,000，这是本缺陷的判据）；再预置一笔当日已成交 280,000，断言本轮只放行 20,000。
  3. 时区：预置一笔 UTC 跨日（如北京 00:30 = UTC 前一日 16:30）的成交，断言它计入**北京当日**而非 UTC 当日。
- **风险**：中。方向是收紧，不新增下单。**(b) 依赖时区口径正确**，是本条唯一容易做错的地方。
- **前置依赖**：无。可与 P0-C 并行；但若 P0-C 先上线，本条的紧迫性立刻上升（扩张侧真开始下单后，日额度是唯一的批量刹车）。

**P0-E　修复 N-23 ＋ N-30：让「L1 下限」与「L2 实际承诺」可同时满足**

- **N-23 现象**（生产实测）：L2 `target_allocation = {equity_pct: 51, cash_pct: 32, hedge_pct: 17}`，但 `core_holdings` 4 票合计只有 **33%**；L1 `sideways / balanced` conf 7 → `equity_min = 40`。于是缺口恒为 40 − 33 = **7pct 且永不收敛**——缺口驱动器**每天追一个 L2 从未答应过的数字**。这解释了那个反直觉的现场：*缺口驱动器天天跑，组合却纹丝不动*。
- **两种修法，二选一，不要都做**：
  - **(a) 让 L2 自己合得上**：`run_strategic_plan` 产出后加一条**自洽校验**——`sum(core_holdings[].target_weight_pct) + cash_pct + hedge_pct` 与顶层 `equity_pct` 偏差 > 5pct 时，在 L2 结果里写 `allocation_inconsistent` 警告、前端亮黄标。**不自动改数**（改数等于替 LLM 编仓位）。
  - **(b) 让 L1 下限认 L2**：`compute_underweight_gap` 的分母从"L1 `equity_min`"改为 `max(L1 equity_min, L2 合计承诺权重)`——L2 更低时不产生缺口。
- **建议 (a)**：它顺带解决顶层 `equity_pct` **无消费者**的问题（见三章末"不对称"表第 5 行——`equity_pct:51` vs holdings 合计 33%，前端/驱动器/逃逸口全部只读 `core_holdings[].target_weight_pct`）。把"顶层与明细是否自洽"变成一条**可见断言**：谁写错谁被点名，而不是被默默忽略。
- **N-30 附带**：`equity_max` 是硬拦且**无降级路径**（`constraint_validator.py:351-363` 的 limits 列表含 `_effective_single_trade_cap` / `max_single_position_pct` / `cash - min_cash_pct` / `max_daily_turnover_pct` 与板块，**不含 `equity_max`**）→ 该类超限计划没有"改小到能过"的出路。**建议**：保留硬拦（上限硬拦比"降级成能过"更安全），但在 `validate_against_regime` 处写死一行注释说明**这是设计而非遗漏**——否则下一个审计者还会把它当 bug 报一遍。
- **验证**：造 L2 `equity_pct=51` / holdings 合计 33 → 断言 L2 结果含 `allocation_inconsistent`；断言缺口驱动器在该配置下**不产计划**（消除"追不存在的数字"）。
- **风险**：(a) 低，纯增量警告。

**P1-I　修复 N-24 ＋ N-25：给缺口驱动器补刹车，给驱动计划补确定性桥接**

- **N-24（越跌越买）**：缺口驱动**豁免了 5 日冷却**，而它唯一的刹车（`existing_value >= planned_amount`）本身是坏的（N-21）。且该刹车**方向错误**：价格下跌 → 市值下降 → `existing_value` 变小 → **反而更容易通过** → 越跌越买。**注意：修完 P0-C 后这个行为会变得更明显**——以前是被归零误挡住的，现在挡不住了。
  - **必须与 P0-C 同批**做一件事。两个选项：**①（建议，一行）**收回缺口驱动器的冷却豁免，纳入统一 5 日冷却——代价是补仓变慢，但"慢"远好过"越跌越买"；**②** 加价格条件（未跌破 20 日均线 / 未触发硬止损才补），复用 `_hard_stop_loss_sweep` 已有的价格数据，**不引入新数据源**。建议先做 ①，把 ② 留作观察到真实拖累后的第二步。
- **N-25（驱动计划 → 执行计划无确定性桥接）**：`:2320 driver_plans = {**gap_plans, **opp_plans}` 合并后，能不能落成单子**取决于 L4 的 LLM 是否恰好选中同一票**。
  - **改法（建议后者）**：在 `run_execution_plans` 里让 `driver_plans` **直通校验**（不经 LLM 取舍），而非仅在 prompt 里提一句。这**一次修完 N-3 / N-25**：扩张意图从此不再依赖"LLM 巧合"——这正是第一轮方案 P1-A 的承诺，而 N-25 说明它只兑现了一半。
- **落点**：`decision_engine.py:1288-1295`（豁免处）、`:1614`（顺手补 `_generate_gap_driven_plans` 的 try 包裹 = 原 P1-H）、`:2320`、`run_execution_plans`。
- **验证**：断言"缺口 > 0 且本票 5 日内有过计划 → 不产计划"；断言"`driver_plans` 的每一票都出现在 `exec_plans` 或带原因的 `skips` 中"（**不许无声丢单**）。
- **风险**：中。① 会降低补仓频率，属预期内。

**P1-J　修复 N-31：跨市场守卫是空操作**

- **现状**：守卫比较的是同一个字符串，恒真 → 空操作，等于**没有守卫**。
- **改法**：计划表已有 `market` 列 → 判据换成 `plan.get("market") != market`（一行）。**不要**删守卫了事：A 股计划进美股执行是真实可能的路径。
- **验证**：造一条 `market='a_stock'` 的计划，断言在 `us_stock` 的执行流程里被拦并计入 `skips`。
- **风险**：低。

**P2-H　修复 N-26 ~ N-29 与 N-32（机会驱动与卫生）**

- **N-26 机会驱动"信念最高优先"是假象**：排序发生在**预算分完之后** → 预算可能分给低信念票。改法：把排序提到预算分配**之前**。这是本组里唯一会改变资金分配结果的改动。
- **N-27 B 类 feature flag 与"顶档才有资格越线"未实现**：**建议先不做**——它与 N-30（`equity_max` 硬拦无降级）的关系未定调，等 P0-E 结论。先在 prompt / 文档写明"当前只有 A 类越线"。
- **N-28 越线标记在硬校验 / 降级之前计算**（与注释矛盾）：把 `mandate_exception` 的写入移到 `validate_against_regime` **之后**，使标记与"实际越没越线"对齐。
- **N-29 `mandate_exception` 不进投委会、不进 oplog**：`grep -c mandate_exception committee.py` = **0** → 越线计划不带豁免上下文送审，事后也无痕。补两处接线：投委会 prompt 加一个字段、`oplog` 落一条 `result="exception"`。
- **N-32 两驱动器选中同一票时一方预算无声蒸发**：`{**gap_plans, **opp_plans}` 后者**覆盖**前者。改法：合并时同票取**更大**的 `planned_amount` 并记一条 `logger.warning`（把"覆盖"变成"取大 + 留痕"）。
- **死代码**（与 P2-G 一并）：`minor_tweaks` / `valid_until_trigger` / `expires_at` / `major_revision_needed` 一类"算了没人读"的字段，一律**要么接消费方、要么删**。
- **风险**：N-26 中（改变预算分配），其余低。

---

### 账户记账与导入护栏的修复方案（对应 N-33 / N-34）

这两个问题出自本轮对**执行层与账户记账域**的独立复核，两条均已由我**亲自复现**后确认（N-33 本地复现 + 生产查库；N-34 本地复现 + 与生产逐位吻合）。它们与前文的扩张侧缺陷**没有因果关系**，可独立排期。

**P1-K　修复 N-34：导入护栏对「零持仓账户」形同虚设，净值由上传顺序决定**

> ✅ **已于 2026-09-24 执行**（`portfolio.py:399-451`，两条判据并存而非替换）。生产顺序重放末值 `1,065,337.14`→**`1,063,096.50`**。验证与门禁见**第六节**。

- **现象（生产实测，只读）**：CMBIS 账户的 `sim_account.total_equity = 1,065,337.14`，而 `cash_balance = 8,037.40` 且该账户 `sim_positions` 无一行市值 → **`cash + Σ市值` 与之相差 −1,057,299.74**。全库唯一不符（CITI-1 / NOMURA 均 +0.00）。更关键的是：这个数字**是真实的**，但**是 04-30 那期的真值**——解密 04-30 结单的 `parsed_json` 得 `cash 8,037.40 + FCN 组合市值 1,057,299.74 = 1,065,337.14`，逐位吻合。**它不是估算，而是过期期次的真值覆盖了当前生效值。**
  
  > **附：我自己的一个口径更正**：上一稿把 `cash + Σsim_positions 市值` 的差额 **−1,057,299.74** 描述为"对账不符"。**这不准确**——CMBIS 是纯 FCN 账户，衍生品走 `vip_derivative_terms`、**不进 `sim_positions`**（`_derivative_holdings` 的存在正是为此，见 `vip/portfolio.py:896`），该差额**恰好等于** 04-30 的 FCN 组合市值 1,057,299.74，属**预期行为**。真正的不符是**期次错配**（下面 N-35 进一步证明它还错配了 FCN 明细），不是加法不成立。
- **根因（逐行核验 + 本地复现）**：`_overwrite_guard`（`vip/portfolio.py:399-424`）的两道护栏对本类账户**全部失效**——
  - **陈旧分支**：唯一证据源是 `positions.as_of_date`，而这类"0 只持仓"的纯衍生品账户**从不往 `positions` 写行** → 查询恒空 → 判不出"这期比库里那期旧"。
  - **骤降分支**：条件里写死 `existing_n > 0`，而这类账户 `existing_n` 恒为 0 → 永不触发。
- 于是 `materialize_portfolio` 里 `total_equity = account_total_usd if account_total_usd is not None else ...`（`vip/portfolio.py:559`）这条**优先采信结单权威锚**的路径，在无护栏情况下被无条件写回（`:589`）→ **净值 = 最后上传的那一份**。
- **本地复现（按真实上传顺序喂 4 期）**：07-31 → 06-30 → 07-07 → **04-30（上传得最晚、却是最旧一期）**，末步产出 **1,065,337.14**，与生产逐位一致。旁证：生产 `sim_account.updated_at = 2026-08-03T02:48:20`，与最旧那期 `M381691-20260430-Monthly.pdf` 的 `created_at` 完全相同。
- **影响面**：`build_account_summary`（`vip/portfolio.py:1934`）把它当日志/报告的**头条总权益**；而 `value_series`（`:1782-1794`）对纯衍生品账户走 `_import_total_series` 的**逐期权威值** → **曲线口径正确、头条口径错误**，两者长期分裂而无人察觉。附带：这类账户每期新建活行再被清零，正是 N-33 墓碑堆积的主要来源。
- **改法**：
  - **(a) 给陈旧判据补一个对零持仓账户也成立的证据源**：`vip_imports.key_metrics_json.period_end`（`json_extract` 已在 `_import_total_series` 用过，`vip/portfolio.py:957-976`，**不引入新数据来源**）。判据改为「本期待导入 `period_end` < 该账户已落库最新 `period_end` → 拒绝覆盖」。
    - **该证据源可用性已生产实测**：CMBIS 的 6 份 import **全部抽到了 `period_end`**（`2026-07-31` / `06-30` / `05-29` / `07-07` / `05-26` / `04-30`），且其中 4 份**同时抽到了权威净值**（落库键名是 `total_equity`，`importer.py:531`；值分别为 `1063096.5` / `1057266.5` / `1057902.5` / `1065337.14`，另 2 份是 trade_confirm 无该字段）  
    > **此处更正我自己**：上一稿写"锚映射没抽到 NUMERIC 口径、只抽到日期"，**经解密 `parsed_json` 证伪**。结单原文的锚键名 `total_value_usd` **六份全部抽到**（`importer.py:487` 对 cmbi 取它），只是落库时改名 `total_equity`。故 P1-K 的判据不仅"存在且可靠"，**连权威净值本身都是齐的**——修复后重放能直接得到 07-31 真值，无需重导。。故该字段**存在且可靠**，判据可直接建立其上。
    - **在真实上传顺序下的推演**：`07-31` 先落库成为生效值 → 其后 `06-30` / `05-29` / `07-07` / `05-26` / `04-30` **全部早于 07-31 → 全部被拦**，不再覆盖。这正是期望行为：历史期进 `vip_imports` 作历史锚（它们本来就在），**不覆盖当前生效的账户总权益**——与 `value_series` 逐期读 `vip_imports` 的曲线口径**归位一致**。反向顺序（先传旧期再传新期）不受影响，因为新期 `period_end` 更大、不触发拒绝。
  - **(b) 骤降分支的 `existing_n > 0` 前置条件**放宽为「(有持仓 且 行数骤降) 或 (无持仓 且 权威净值骤降)」——把"没有持仓"从"护栏不适用"改成"护栏改看净值"。
- **落点**：`vip/portfolio.py:399-424`（`_overwrite_guard`）+ `:957-987`（复用 `_latest_import_period`）。
- **验证**：
  1. 本地重放上述 4 期上传顺序，断言末值 = **07-31 的 1,063,096.50**（**修复前必红**，当前得到 1,065,337.14）。
  2. 断言"旧单晚传"被拒且**留下可读原因**（如 `stale_snapshot:2026-04-30`），不是静默跳过。
  3. 回归：CITI-1 / NOMURA 两个原本 +0.00 的账户结果不变（防止护栏拧得过紧，反把合法的新一期挡住）。
- **风险**：中。方向是收紧，且**只影响导入路径**，不碰下单。唯一代价是"合法地补传一份更早的历史结单"会被拒——故拒绝时**必须留痕**而非静默，运维可据此判断是否人工放行。
- **生产数据修复（已于 2026-09-24 经用户授权执行完毕，留痕见下）**：CMBIS 的 `sim_account` 行**三个字段都停在 04-30 期**，均已改回 07-31 期真值。真值由解密 07-31 结单 `parsed_json` 逐位取得：

  | 字段 | 现值（04-30 期） | 应改为（07-31 期真值） | 来源 |
  |---|---|---|---|
  | `total_equity` | 1,065,337.14 | **1,063,096.50** | `account_summary.total_value_usd` |
  | `current_capital` | 1,065,337.14 | **1,063,096.50** | `materialize` 与 `total_equity` 同写（`:590`） |
  | `cash_balance` | 8,037.40 | **16,028.50** | `total_cash_usd` |

  校验：`16,028.50 + 1,047,068.00 = 1,063,096.50` ✓（现金 + 组合市值 = 权威总额）

  **实际执行记录（2026-09-24，两步均带断言，任一步断言失败即中止）：**

  | 步骤 | 动作 | 结果 |
  |---|---|---|
  | 0 | 库文件级备份（写库前） | `data/watchlist.db.bak-cmbis-equityfix-20260924T020356Z`（90,402,816 bytes） |
  | 1 | 前置断言：`id='2952e48930e1' AND account_ref='CMBIS'` 命中 1 行，三字段现值逐位一致 | 通过 |
  | 2 | 该行全字段 JSON 备份 | `data/sim_account_CMBIS_backup_20260924T020356Z.json` |
  | 3 | 事务内 `UPDATE … WHERE id=? AND account_ref='CMBIS'`，断言 `rowcount == 1` | 影响 1 行，`updated_at` → `2026-09-24T02:03:56+00:00` |
  | 4 | 复核三字段 + 断言 `account_ref=''` 自有账户仍为 49 行 | 通过（49 行未变） |
  | 5 | 审计留痕 `vip_account_log`（`event_type='calibration'`，`severity='warn'`） | id `3857914823a9`，`2026-09-24T02:10:46+00:00` |

  > 第 5 步首次尝试用 `event_type='maintenance'` 被 **CHECK 约束**拒绝（该列只允许 `projection`/`calibration`/`anomaly`/`settlement`）——数据修复本身已提交生效，仅留痕缺失，随即以 `calibration` 重写成功。**该 CHECK 约束的取值集过窄**（容纳不下"人工修复"这类事件）已记入 N-36 的补注，见下。

  **注意**：本修复**只改这三个数**，不解决 N-35 的衍生品期次错配（那要改代码，P1-L）——改完头条对了，衍生品栏仍会高估约 2.0×，**这属于预期，不是修复失败**。

**P1-L　修复 N-35：衍生品条款按「结单期次」而非「上传时刻」选当期（与 N-34 同一个修复窗口）**

> ✅ **已于 2026-09-24 执行**（`portfolio.py:900-964`，方案 (b)）。当期 MTM `2,099,173.74`→**`1,047,068.00`**。⚠️ **本节"到期判据"原文有误、已就地更正**——基准应为**账户最新结单期次**而非"今天"，详见下方批注与**第六节 6.1**。

- **现象（生产实测，只读）**：CMBIS 对外展示的 FCN `mtm_total_usd = 2,099,173.74`，而 **07-31 结单的真实组合市值只有 1,047,068.00**，**高估 1,052,105.74（约 2.0×）**；且其中两笔 CMBIGP（`S20250515S916USD` / `S20250515S917HKD`）来自 **04-30** 结单，**到期日 2026-05-15 早已过去**，却仍被 `_derivative_holdings` 当作现持仓并入概览构成。
- **根因（逐行核验）**：`_current_derivative_rows`（`vip/portfolio.py:874-893`）用 `MAX(created_at)` 选"当期条款"——取的是**插入时刻**。注释写的是"最新一期"，但 SQL 里没有任何东西对应"期次"。生效前提是"上传顺序 = 期次顺序"，而 N-34 已证这个前提在生产上**被打破**（最旧一期最后上传）。
  - **注意它比 N-34 更宽**：即使上传顺序完全正确，只要**同一条款分多期上传**，`MAX(created_at)` 仍会选到最近**上传**的那期而非最近**结单**的那期。生产 NVDA 三行即如此（选中的是 07-07 日结单，而 07-31 月结单更晚）。
  - **前置依赖**：本改法依赖 `created_at` 的可信排序，故 **P0-F（N-36）必须先落地或同批落地**——否则新写入的行仍与旧行口径不一。
- **改法（二选一，建议 (b)）**：
  - **(a) 最小改法**：`ORDER BY` 的判据从"插入时刻"换成"结单期次"。该表**没有 `period_end` 列**，须新增一列 `period_end`（写入侧在 `derivatives.py` 落库时带上，来源即 `stmt.period_end`），选择改为 `GROUP BY ... 取 MAX(period_end), 同期内取 MAX(created_at)`。**动 schema（加列）。**
  - **(b) 借道已有期次锚（不改 schema，推荐）**：`vip_imports` 已有 `period_end` 且已按 `source_file_hash` 与条款行对应（`derivatives.py:800` 的幂等键就含 `source_file_hash`）。用 `key_metrics_json.period_end`（`json_extract` 已在 `_import_total_series` 用过，**不引入新数据来源**）join 出每行的结单期次，再按期次选当期。若 join 不到该期（历史导入无此键），**回落现状 `MAX(created_at)`** 并如实标注——绝不因 join 缺失而丢行。
  - 同时：**已被后一期结单取代的头寸（条款 `maturity` 早于账户最新结单期次）不应作为"当期条款"并入持仓构成**。建议在 `_current_derivative_rows` 出口按此过滤（或在 `_derivative_holdings` 内），并**留痕说明剔除了几笔**，不静默。
    > **此处更正我自己（2026-09-24，执行时发现）**：本段原写"到期日已过（`lot_key` 尾部的 `:YYYY-MM-DD` **< 今天**）"，**该判据是错的且自相矛盾**——以今天（09-24）为准会把 07-31 期**仍在册**的 NVDA（到期 **09-04**）一并剔掉，把该账户 MTM 清成 **0**；而本节验证 1 又要求选出 NVDA 的 1,047,068.00。两者不可能同时成立。**正确基准是"账户最新结单期次"而非"今天"**：条款是"哪一期结单里报的"，其失效与否应相对结单口径判断（CMBIGP 到期 05-15 < 最新期次 07-31 → 已被 05-29 结单列示卖出，剔除；NVDA 到期 09-04 > 07-31 → 07-31 期仍在册，保留）。已按此执行，见第六节。
- **落点**：`vip/portfolio.py:874-893`（`_current_derivative_rows`）、`vip/derivatives.py:815-823`（写入侧，配合 (a) 或 P0-F）；`_derivative_holdings`（`:896-913`）的到期过滤。
- **验证**：
  1. 本地重放生产 CMBIS 的 5 行条款（3 行 NVDA 跨 3 期 + 2 行 04-30 的 CMBIGP），断言选出的当期 MTM = **1,047,068.00**（**修复前必红**，当前得 2,099,173.74）。
  2. 断言两笔已到期的 CMBIGP **不出现在** `_derivative_holdings` 的构成里，且留痕报告剔除 2 笔。
  3. 逆序上传（先传 07-07 再传 07-31）断言结果与正序一致——这正是 N-34 的核心错误模式。
  4. 回归：CITI-1 / NOMURA 的衍生品构成不变。
- **风险**：中。只影响衍生品展示与 MTM 汇总口径（`derivative_summary` / `derivative_exposure` / `value_series` 兜底锚），**不碰下单、不碰 `sim_account` 总权益**。最大风险是 (b) 的 join 在历史导入上落空——故明确要求"落空即回落现状 + 留痕"。

**P2-I　修复 N-33：墓碑行让「同一票两行都算市值」**

> ✅ **已于 2026-09-24 执行 (a)**（`store_simtrading.py:461,481`）。前置条件已由用户定调解决——**VIP 账户未来不会下单** → 按本节原定"若否则只做 (a)"，**(b) 不做**（未动 schema）。见**第六节**。

- **现象**：`materialize_portfolio`（`vip/portfolio.py:552-556`）把旧持仓**清零为墓碑**（`shares=0`）而**不删除**；而 `get_sim_position_any`（`store_simtrading.py:468-479`，**不过滤 `shares`**）与 `get_sim_position`（`:456-466`）都是 `fetchone` 且**无 `ORDER BY`** → 命中哪一行**由 SQLite 的行序决定**。`_execute_buy`（`trade_executor.py:295-306`）正是用前者找"可复用行"。
- **本地复现**：同票放活行（200 股 @150）与墓碑（0 股 @200），`get_sim_position_any` 命中**墓碑**；买入 200 股后**两行都 `shares > 0`** → 组合市值被**重复计数 45,000**。
- **可达性（必须连读）**：**当前不可达**。`create_execution_plan` 无 `account_ref` 参数；`grep -rn "execute_trade|confirm_and_execute" bottleneck_hunter/vip/` **零命中**；`scheduler.py` 三处 VIP 作业都带 `if not ref: continue` → **VIP 路径从不下单**。生产实测：危险形态 25 票（NOMURA 7 / CITI-1 18），而**决策中心自有账户（`account_ref=''`）零命中**。故它是**潜伏雷**，不是正在发生的错误。
- **卫生现状**：全库 `sim_positions` 148 行，活跃 39 / 墓碑 109（**74% 是墓碑**）。
- **改法（二选一，建议先做 (a)）**：
  - **(a) 让查询不再掷骰子**：`get_sim_position_any` / `get_sim_position` 加 `ORDER BY shares > 0 DESC, updated_at DESC`——活行优先，墓碑让位。**不改变任何现有正确行为**，只把"偶然正确"变成"必然正确"。
  - **(b) 从库里根除歧义**：`sim_positions` 加 `UNIQUE(account_id, ticker)`（现只有 `idx_sim_positions_account` / `idx_sim_positions_market`），并把 `materialize_portfolio` 的清零改成删除或 UPSERT。**风险显著高于 (a)**：需先清历史墓碑，且动导入路径。
- **前置条件**：**先定调"VIP 账户将来是否会被下单"**。若否，只做 (a) 这一行防御；若是（例如 P1-A 落地后 VIP 侧接入执行），则必须做 (b)，且 (b) 要排在接入**之前**。
- **验证**：同票放墓碑 + 活行，断言 `get_sim_position_any` **必命中活行**；买入后断言该票 `shares > 0` 的行**有且仅有一行**、组合市值无重复计数。
- **风险**：(a) 极低；(b) 中（动 schema + 导入路径）。

---

## 五、实施顺序建议

**第 0 批 —— 止血（互不依赖，可并行，全部风险低）**

- **P0-A + P0-B**：放行与隐形问题。P0-A 有生产成交证据支持（投委会"不可背书"结论被自动执行放行）。
- **P1-K + P1-L + P0-F**：账户记账域的三件套（N-34 净值期次错配 / N-35 衍生品期次错配 / N-36 排序键时区）。**已有生产实证**（CMBIS 头条权益按 04-30 计、衍生品 MTM 高估约 2.0×），且是后续一切账户对账的地基——排在第 0 批。**三者同批**：P0-F 修排序键时区，是 P1-K/P1-L 的判据前提；P1-L 与 P1-K 在同一个函数窗口内，分两次改等于把同一段逻辑摸两遍。✅ **本批已于 2026-09-24 执行完毕并验证**（详见**第六节**）；同批的 **P2-I** 因前置条件解除一并落地。
- **P0-D**：给"当日累计换手"装上事前卡口（**不只是**生成期影子账本）。与 P0-A/B 无交集，可以先做。
- **P1-C + P1-D**：一致性与死字段（`cash_max` 接消费方或删）。
- **P1-J**：跨市场守卫（一行）。

**第 1 批 —— 扩张侧主缺陷（P0-C / P0-D / P1-I 三件必须同批上线）**

这是本次评估的核心结论：**N-21 是唯一一个正在生产上主动制造错误行为的缺陷**（把最该补的票恰好归零丢弃）。但它**不能单独修**——

- 修了 N-21 而**没修 N-22** → 单票补得动了，同轮多笔计划的**当日累计换手**却无人拦（逐笔比对在数学上守不住累加量），组合级风控形同虚设；
- 修了 N-21 而**没做 P1-I 的 ①** → 唯一的坏刹车消失后，"越跌越买"从被误挡变成彻底放开。

三者的因果是**串联**的：P0-C 开门 → P0-D 装门后的限流 → P1-I 装价格/频率侧的护栏。**少任何一件都比不修更危险**。因此：

1. **P1-B**（收敛验收）—— 先有判据，才能证明 P0-C 真的让组合收敛。
2. **P0-C + P0-D + P1-I** 同批。
3. **P0-E**（L2 自洽校验）—— 与上批无依赖，可并行；它决定"缺口目标到底该是多少"，最好在 P0-C 之前定调。
4. **P1-A**（扩张力独立执行权）—— 第一轮承诺的核心；P1-I 的 N-25 改法做掉之后，这一步的收益会大幅下降（LLM 巧合问题已解）。

**第 2 批 —— 打磨与门禁**

- **P2-A / P2-B** 打磨；**P2-I**（墓碑行歧义，N-33）——前置条件**已于 2026-09-24 满足**（用户定调"**VIP 账户未来不会下单**" → 不可达）→ **只做 (a) 一行防御，(b) 不做**。✅ 已落地，见第六节。
- **P2-C / P2-H 里的 N-27 不做**（B 类越线与 feature flag：A 类尚未在生产证明有效）。
- **P1-E ~ P1-H、P2-D ~ P2-G**（对应 N-8 ~ N-20）按第四章原顺序。

**全程保持买卖对称**：每加一个扩张侧机制，先问"它对应的收缩侧是否已存在"。当前收缩侧仍过剩（N-1 修完后更明显），缺口仍在"超配→减仓"与"现金上限侧"两处——**P0-C 让扩张侧真正开始下单之后，这两处的缺口会从"纸面不对称"变成"真实风险敞口"**，建议排在第 1 批之后立刻评估。

---

## 六、第 0 批账户记账三件套 + P2-I 执行记录（2026-09-24）

**授权来源**：用户明确指示「**1 VIP 账户未来不会下单 / 2 批准所有修复**」。第 1 条解除了 P2-I 的前置条件（**只做 (a) 一行防御，不做 (b) 动 schema**）；第 2 条覆盖本报告的全部修复方案。本节只记录**已落地**的四项（P0-F / P1-K / P1-L / P2-I）；**其余方案（P0-A~P0-E、P1-A~P1-J、P2-A~P2-H）本次未动**，仍以第四节原文为准。

**已实现**：3 个源文件改动 + 1 个新测试文件（12 条），全部为**生产形态夹具**（期次与 `file_hash` 只读取自生产库，非编造）。

| 项 | 文件 | 改动 | 与报告原方案的差异 |
|---|---|---|---|
| **P0-F** | `vip/derivatives.py:795,825` | `datetime.now().isoformat()` → 惰性导入的 `store_base._now_iso()` | **一致**。报告建议的"若不便导入则直接用 `datetime.now(timezone.utc)`"未走——该文件的既有 import 本就可用，选同口径写法。**历史行未迁移**（报告明确要求只修写入侧）。 |
| **P1-K** | `vip/portfolio.py:399-451` | `_overwrite_guard` 两道护栏各补一个对**零持仓账户**也成立的证据源 | **等效但更省**。报告 (a) 说"陈旧判据**改为**期次比较"；实现取 **(a)+(1b) 双判据并存**——期次判据走 1a，原有 `positions.as_of_date` 判据原样保留为 1b。**已证二者对正常账户结论完全一致**，故不放松也不新增拦截面；好处是期次证据缺失时护栏不退化。(b) 按报告放宽为零持仓走净值骤降。 |
| **P1-L** | `vip/portfolio.py:900-964` | 借道 `vip_imports.key_metrics_json.period_end`（走 `source_file_hash` 关联，方案 (b)）选当期；到期头寸按**账户最新结单期次**剔除 | **较报告原文收紧了一处判据（见下"执行时更正"）**；未按报告的"落点"把 `_latest_import_period` 挪进 `watchlist/`（该函数的既有位置可用，挪动＝无收益的跨模块改动）。 |
| **P2-I** | `watchlist/store_simtrading.py:461,481` | `get_sim_position` 加 `ORDER BY updated_at DESC` + `AND shares > 0`；`get_sim_position_any` 加 `ORDER BY shares > 0 DESC, updated_at DESC` | **报告 (a) 原文 + 一处顺带**。(a) 只提 `ORDER BY`；实现同时给 `get_sim_position` 补了 `AND shares > 0`——它同样是无 `ORDER BY` 的 `fetchone`，同属 N-33 的"掷骰子"形态。未做 (b)，因 VIP 不下单。 |

### 6.1 执行时发现报告自身的一处错误判据（已就地更正）

P1-L 原文要求"到期日已过（`< 今天`）的条款不并入构成"，**该判据自相矛盾**——今天为 09-24，而 07-31 期**仍在册**的 NVDA 到期 **09-04** 已过，滤掉它会把该账户 MTM 清成 **0**；而同节验证 1 又要求选出 NVDA 的 `1,047,068.00`。**正确基准是"账户最新结单期次"而非"今天"**：条款失效与否应相对结单口径判断（CMBIGP 到期 `2026-05-15` < 最新期次 `2026-07-31`，且 05-29 结单已列示卖出 → 剔除；NVDA 到期 `2026-09-04` > `07-31`，07-31 期仍在册 → 保留）。判据已按此实现，原文段落已就地加更正批注。**同节验证项 2 的措辞（"两笔已到期 CMBIGP 不出现"）与此判据一致，验证项 1/3 亦全绿——原文三项目标在更正后的口径下可同时满足**（更正前不可能）。

### 6.2 生产证真（逐条复现，非阅读推断）

| 项 | 修复前（生产实测） | 修复后（本地按真实上传顺序重放） |
|---|---|---|
| P1-K 末值 | `1,065,337.14`（= **04-30** 期真值覆盖了当前生效值） | **`1,063,096.50`**（= 07-31 期真值）✓ |
| P1-K 留痕 | 静默覆盖 | `guard_skipped = "stale_snapshot:2026-04-30"`，**非静默**；历史期仍进 `vip_imports` 供曲线 ✓ |
| P1-L 当期 MTM | `2,099,173.74`（276,226.82 + 781,072.92 + 1,041,874.00 三笔错配叠加） | **`1,047,068.00`**（仅 07-31 期 NVDA，lot `XS3372957897:2026-09-04`）✓ |
| P1-L 构成 | 含两笔已到期 CMBIGP | 仅 `NVDA·结构性`；剔除数走 debug 留痕（**不静默**）✓ |
| P0-F 时区 | 裸本地时间，与同事件 `vip_imports.created_at` 相差 **8 小时** | `+00:00`，与同一导入相差 **< 5 秒** ✓ |
| P2-I 复用行 | 墓碑**先**插入时命中墓碑 → 买入后两行皆 `shares>0`（重复计 45,000） | 必命中活行（`shares=200`, `id='p1'`）✓ |

### 6.3 上线后生产证真（2026-09-24，容器内实跑，只读）

**部署**：`main@846e065` 经 `./deploy.sh` 重建镜像（容器 bake 源码，`git reset + restart` 不上线代码）；健康检查 `/healthz` **200**，容器 **healthy**。

**容器内源码证真**（`inspect.getsource` 直读容器内实际加载的模块，非读宿主机文件）：

| 项 | 断言 | 结果 |
|---|---|---|
| P0-F | `derivatives` 走 `_now_iso()` 且不再有 `datetime.now().isoformat()` | **True** |
| P1-K | `_overwrite_guard` 含期次直接判据 | **True** |
| P1-L | `_current_derivative_rows` 含 `period_by_hash` + `latest_period` | **True** |
| P2-I | `store_simtrading` 含 `ORDER BY shares > 0 DESC, updated_at DESC` | **True** |

**真实业务路径结果**（`_current_derivative_rows('CMBIS')`）：当期条款 **1 行**（NVDA `XS3372957897:2026-09-04` @ 期次 `2026-07-31`，MV 1,047,068.00）→ **当期 MTM = `1,047,068.00`**（修复前生产值 `2,099,173.74`）；构成 = `['NVDA·结构性']`；最新结单期次 = `2026-07-31`。

**回归证真——旧口径 vs 新口径逐账户逐行比对**（这是本批最关键的防"拧得过紧"证据）：

| 账户 | 旧行数/旧 MTM | 新行数/新 MTM | 被剔除 | 同键但值变 | 构成变化 |
|---|---|---|---|---|---|
| **CITI-1** | 9 / 5,118,953.15 | **9 / 5,118,953.15（完全不变）** | 无 | 无 | 无 |
| **NOMURA** | 12 / 1,752,503.09 | **12 / 1,752,503.09（完全不变）** | 无 | 无 | 无 |
| CMBIS | 3 / 2,099,173.74 | **1 / 1,047,068.00** | 2 笔已到期 CMBIGP | NVDA 1,041,874 → **1,047,068**（07-07 期 → 07-31 期） | 3 票 → 1 票 |

即：**两个正常账户零变化**（连 MTM 都逐位相同），错配账户被精确修到 07-31 期真值。NOMURA 的两笔大额 XS（`XS3422497100` 991,100 + `XS3164880992` 976,300 = **1,967,400**）**完整保留**——这正是 P1-L 刻意不"关联不到期次即剔除"的原因：那会凭空删掉该账户的近 2M 在册敞口。

### 6.4 门禁结果

| 门禁 | 命令 | 结果 |
|---|---|---|
| 新增回归守卫 | `pytest tests/test_decision_chain_round2_fixes.py` | **12 passed** |
| **守卫有效性**（关键） | 同上，`git stash` 掉 3 个源文件后 | **6 failed / 6 passed** —— 红的正是**瞄准缺陷**的 6 条，证明它们是真护栏而非恒真断言 |
| 全量套件 | `pytest -q` | **2223 passed / 5 skipped / 1 warning，605.56s，exit 0** |
| 关联套件 | `test_decision_chain_round2_fixes` + `test_vip_portfolio` + `test_vip_nomura_nav` + `test_trade_executor` | **76 passed** |
| 静态检查 | `ruff check`（**只读**，不加 `--fix`） | 3 个改动文件**零新增**；新测试文件 All checks passed! **规则码直方图改动前后逐项相同**（30 E501 / 4 B023 / 2 SIM108 / 2 I001 / 1 SIM105 / 1 SIM102 / 1 B905） |

### 6.5 过程中被测试抓到的两个真实缺陷（记入以防复发）

1. **`_current_derivative_rows` 的 SQL 别名错位**：初版把列写成 `created_at AS _mx` 却在 Python 侧读 `r["created_at"]` → **`KeyError`，会让生产每次渲染/报告生成都崩**。被新测试**首次运行**即抓住（非人工 review）。已去掉别名。
2. **`existing_n` 的语义陷阱**：`get_sim_positions` 默认已滤 `shares > 0`，故 `existing_n` 是**活仓**数（墓碑不计）。这正是零持仓纯衍生品账户 `existing_n` 恒为 0 的原因——即 N-34 骤降分支失效的根因。已在函数注释与 P1-K 实现处写明，防后人误判。

### 6.6 未做的事（明确不做，非遗漏）

- **P2-I (b)**（`UNIQUE(account_id, ticker)` + 清墓碑）：因用户定调 **VIP 账户永不下单**，该路径不可达，按报告"若否则只做 (a)"执行。
- **`created_at` 历史值迁移**：报告明确"**只修写入侧，历史行不动**"——裸本地串的相对顺序在 `Asia/Shanghai` 容器内仍正确，迁移跑错反会打乱顺序。
- **P1-L 的严格期次归属**（把关联不到期次的条款也剔除）：会删掉 NOMURA 约 **1.97M** 的**在册** FCN 敞口。报告 (b) 本身要求"**落空即回落现状 + 留痕**"，故保守保留——**这是刻意的**，不是没做。
- **`vip_account_log.event_type` CHECK 约束扩容**（N-36 补注建议的 `manual`/`repair`）：属 schema 迁移，不在本次四项授权范围内，**仍待批**。

---

## 附录 A：生产证真数据（2026-09-23，只读）

**代码上线**：`main@72bc42f`，容器 `bottleneck-hunter` 内 grep 计数——`compute_underweight_gap`=1、`mandate_exception`=4、驱动器函数=4、`缺口未纠正`=3、`_reuse_escape_reason`=2、`simtrading` alloc-target=5。**扩张侧代码确认全部上线。**

**账户现实**：

| 市场 | 现金 | 总权益 | 持仓市值 | 实际权益% | 实际现金% |
|---|---|---|---|---|---|
| us_stock | 876,147 | 1,001,457 | 125,311 | **12.5%** | **87.5%** |
| a_stock | 4,567,849 | 5,042,484 | 474,636 | **9.4%** | **90.6%** |

**计划维度结论分布（去重后，活跃用户）**：

```
approved                     → confirmed 7 / executed 15 / expired 5 / rejected 22
approved_with_modifications  → confirmed 9 / executed  7 / expired 2 / rejected 36
needs_discussion             → executed 2 / rejected 15 / expired 1      ← 2 条为放行成交
rejected                     → rejected 65
```

**僵尸计划**：全库 pending **11 条**，最老 2026-07-05，全部 `snapshot_id=None`，分属 4 个非活跃用户；活跃用户 **pending = 0**（`get_pending_executions()` 正确按 user+market 过滤）。

**挂单（正常行为）**：us_stock 9 条、a_stock 7 条 `resting`，等价格达限价成交。

---

## 附录 B：本报告使用但作废的口径

| 作废说法 | 错因 | 正确口径 |
|---|---|---|
| "needs_review → pending 36 条、executed 1 条" | 按 `committee_consensus` **行数**计数，误当**计划数** | **pending 3 条 / executed 0 条**（计划维度去重） |
| "37 条 needs_review 共识未拦截" | 同上 | 共识行数 ≠ 计划数；正确说法是"**3 条计划从未被 gating 驱动**" |
| "扩张力叠加可超可用现金（已发生）" | 代码阅读正确但**生产未见实际发生** | 理论风险 + `trade_executor.py:281` 兜底；真实代价是**静默失败回滚**（并入 N-2） |
| **"5×4000 全部放行 → 现金 0、15% 现金下限被击穿"** | 生成期共享快照是真，但**成交期并不共用该快照**——`trade_executor.py:106` 每笔重读账户，`constraint_validator.py:275-282` 硬卡下限，实测 5 笔只成交 2 笔、全程最低现金 20% | **下限守住了**；真正被绕过的是 **`max_daily_turnover_pct`**（`:288-293` 逐笔比对、从不累计；实测 10 笔各 5% 权益全部放行 → 单日累计 50% 权益 vs 上限 30%）。见 N-22 与 P0-D |
| **"CMBIS 的 `cash + Σsim_positions 市值` 差额 −1,057,299.74 = 对账不符"** | 该差额**恰好等于** 04-30 的 FCN 组合市值；CMBIS 是纯 FCN 账户，衍生品走 `vip_derivative_terms`、**不进 `sim_positions`**，故差额属**预期行为** | 真正的不符是**期次错配**（头条/现金停在 04-30，衍生品明细停在 07-07+04-30），见 N-34 / N-35 |
| **"`total_value_usd` / `net_asset_value_usd` 均为 null，锚映射没抽到 NUMERIC 口径、只抽到日期"** | 解密 6 份 `parsed_json` 证伪：结单原文锚键 `total_value_usd` **六份全部抽到**，只是落库改名 `total_equity` | 4 份月/日结单**权威净值齐备**（1063096.5 / 1057266.5 / 1057902.5 / 1065337.14），另 2 份 trade_confirm 无该字段 |
| **"1,065,337.14 不是任何一期结单的真实值"** | 解密证伪：`8037.40 + 1057299.74 = 1065337.14` 逐位吻合 04-30 结单 | 它**是真实值，但是过期期次的真值**——覆盖了当前生效值。正确说法是"期次错配"而非"估算/无据" |
| **"N-34 复现里 positions 表恒 0 行 = 我的探针提交瑕疵"** | 我自己提出的疑点，**查证后不成立** | 写 `positions` 的唯一入口 `_upsert_position` 只在 `normalize_statement` 的 `if stmt.holdings ...` 分支内调用（`vip/portfolio.py:240`）；CMBIS 0 持仓 → `stmt.holdings` 空 → **从不进入**。生产 `positions` 表只有 CITI-1(130)/NOMURA(48)，CMBIS 零行——**与探针一致** |

---

> **本文的复核与方案部分为只读产物，未改动任何代码。** 其中 **P0-F / P1-K / P1-L / P2-I 四项**已于 2026-09-24 经用户明确授权（「批准所有修复」+「VIP 账户未来不会下单」）**实施并验证**，执行记录见**第六节**。其余方案（P0-A~P0-E、P1-A~P1-J、P2-A~P2-H）**仍待用户逐项确认**再动手——「批准所有修复」是否覆盖它们的**范围**已在回报中提请确认。
