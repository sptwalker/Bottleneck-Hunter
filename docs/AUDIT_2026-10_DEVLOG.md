# 2026-10 体检 + P0-2/P0-3 开发日志

依据：`docs/QUANTDINGER_RESEARCH_2026-10.html` 研究后的对照体检。原则是「最小根因修复」，每项都配有回归测试（`tests/test_audit_2026_10.py`）。

## 一、体检计划（8 个维度）

| # | 维度 | 检查点 |
|---|------|--------|
| A | 风控执行链 | 硬止损 / 减仓是否会被投委会、自动执行或去重挡住 |
| B | 学习闭环标签 | 投票结算的方向、弃权、跨市场串结 |
| C | 复盘归因 | 基准 ticker、买卖配对、基准数据缺口 |
| D | VIP 建议复盘 | 判定对称性、队列饥饿 |
| E | 数据源 | 配额耗尽后的行为、财报 PIT（公告日） |
| F | 统计评估 | 自助法独立同分布假设、多重比较、成本敏感性 |
| G | LLM 可追溯 | 理由能否回查到输入数据 |
| H | 并发 / 门禁 | 执行单竞态、质量门、调参留出集 |

## 二、发现与处置

| # | 问题 | 严重度 | 处置 |
|---|------|--------|------|
| 1 | 投委会否决会挡掉硬止损和减仓卖单 | 高 | 已修：`_is_risk_reducing` 实现非对称否决，减险计划不受否决，保留待确认；质询改票后重新 gating 时同样处理 |
| 2 | 投票结算不分方向，reject 票在赢单上被记为「对」，abstain 也被结算 | 高 | 已修：按方向标注，approve 族盈利记对，reject 族亏损记对，abstain 作废（`is_correct=-2`，不计入准确率和校准）；结算按 prediction_value 和 market 过滤 |
| 3 | 复盘基准写死为 000300.SH / SPY，且 ^GSPC、^HSI 无快照 | 中 | 已修：基准改用 `default_benchmark_ticker(market)`；价格更新时，基准缺数据会用代理 ETF（SPY / 2800.HK）回填 |
| 4 | 卖单与卖出时刻之后的买单配对 | 中 | 已修：trade_reviewer 和 preference_learner 只与 `created_at <= sell` 的买单配对 |
| 5 | VIP 建议长期无价格，永远 pending 堵住队列；「关注」涨了也算对 | 中 | 已修：未知动作立即作废，无价格超过 60 天作废；「关注」改为对称判定 `abs(chg) <= band` |
| 6 | 已有挂单的标的，硬止损巡检会再生成一张重复卖单；auto_execute 会丢弃硬止损单 | 中 | 已修：巡检跳过已有 pending 或挂单 sell 的票；auto_execute 无条件保留硬止损单 |
| 7 | Tushare 配额耗尽后同日继续狂刷 | 中 | 已修：配额类报错会闩锁到北京次日 0 点 |
| 8 | Tushare 快报 report_date 取期末日而非公告日，存在前视风险 | 中 | 已修：优先使用 ann_date，fiscal_quarter 由 end_date 推导 |
| 9 | 质量门黄灯不降低置信度 | 低 | 未做，详见第四节 |
| 10 | bootstrap 采用独立同分布重采样，低估收益序列自相关下的区间宽度 | 中 | 已修：新增 `block_bootstrap_ci`（移动块） |
| 11 | 从多个配置中挑最优时缺多重比较校正 | 高 | P0-2 已做，见第三节 |
| 12 | LLM 理由无法回查到输入数据 | 高 | P0-3 已做，见第三节 |

## 三、改进项

### P0-2：PBO / DSR / 成本压测（`watchlist/evaluation.py`、`event_backtest.py`）
- `pbo_cscv(perf T×N, n_splits=16)`：CSCV 过拟合概率，λ=ln(ω/(1−ω))，PBO=P(λ≤0)。
- `deflated_sharpe(returns, trial_sharpes)`：SR₀ 取试验间方差，N 为全部试过的配置数，σ̂ 含偏度和峰度校正。
- `overfit_flags`：PBO>0.5 或 DSR<0.9 时标「疑似过拟合」，只做标记，不自动采纳。
- `incremental_ablation` 结果新增 `selection` 字段，包含最优臂的 DSR，窗口数 ≥8 时附 PBO。
- `run_event_backtest(cost_mult=)` 和 `cost_stress()` 分别在成本 ×1 / 1.5 / 2 / 3 下各回放一次。

### P0-3：证据 ID（`watchlist/evidence.py`）
- `evidence_id = sha256(ticker|metric|as_of|value)[:12]`，同一事实恒得同一 ID。
- 证据来源：L3 观察池行情（close / change_pct / rsi_14 / sma_50 / volume）；L4 在场标的行情和持仓；投委会估值和行情。prompt 末尾附「证据索引」，并硬性要求输出 `evidence_ids`。
- 校验：Pydantic `Citation` 只保留本次索引中真实存在的 ID，编造或串票的 ID 写入 `evidence_invalid_ids`，无有效引用时标 `evidence_label="无据"`。
- 后果：L3 / L4 只标记，不拦截。投委会**赞成票**无据时按弃权计，不计入通过票，摘要会注明张数。反对票不设门槛，与减险不受阻的原则一致。证据索引为空时不判，结构上不误伤。
- 投委会阶段快照中保存 `evidence_index` 原文，复盘时可回查。

## 四、刻意未做 / 已知上限
- Gangtise 配额闩锁：共 46 个响应解析点，且生产环境无凭据，等接入后再统一做。
- 质量门黄灯降低置信度：低优先级，pre_l4 红灯已能拦截。
- 执行单竞态没有唯一约束：现有 pending 去重已覆盖常见路径，需要时再加 DB 唯一索引。
- 调参没有留出集：已有 overfit_flags 标记，且不自动采纳，风险可控。
- signal_calibration / pit_gate 尚未接入主链路，单独立项。
- 投票结算以交易盈亏为近似（ponytail 注释已标注），升级路径是固定持有期对基准超额结算。

## 五、验证
- `tests/test_audit_2026_10.py`：11 项。
- 定向用例：auto_execute / committee / decision / snapshot 共 70 项；advice / accuracy / vote 共 139 项；evaluation / ablation / backtest 共 48 项。
- 全量串行 pytest：2602 passed / 5 skipped；唯一失败 test_price_update_filters_by_market 为代理 ETF 回填引起的预期行为变化，已更新断言（7 passed）。

---

# 追加：A股仓位长期偏低（2026-10-20）

## 现场
A股决策账户总资产 469.7 万，持仓 12.6 万（2.69%），L2 目标权益 36%（约 169 万）；近 40 天已实现 -30.65 万。
美股同口径持仓 20.8%。根因是「扩张通道窄 + 买入被系统性否决 + 名单抖动」三者叠加。

## 逐项改动与验收

| 项 | 改动 | 验收 |
|---|---|---|
| P0-1 | `_plan_gap_fills` 纳入 `tactical_holdings`，按 `BH_GAP_TACTICAL_FACTOR`（默认 0.5）打折；水位仍写 L2 原始目标，避免收敛点被压低 | `test_gap_driver.py` 新增 2 项：tactical 进入通道、已达目标不产单 |
| P0-2 | 新增 `_attach_stop_loss`：买入/加仓落库前挂止损，来源 L3 exit_plan → L2 Bear 场景 → 默认幅度（A股 10%、美股 8%）；高于现价则退回默认。硬止损巡检优先读执行计划上的止损 | 新增 4 项：L3 优先、默认幅度/卖单跳过、拒绝高于现价的止损、无 L3 计划时巡检仍触发 |
| P1-1 | `committee_value.md` 增加「估值口径」：成长池按历史分位与同业中位数相对判断，绝对 PE 单独不构成否决；否决须同时满足分位 ≥90% 且同业显著更低 | `test_committee_value_growth_pool.py` 2 项 |
| P1-2 | 新增 `_stabilize_selection`：新进 L2 名单的标的须昨日亦在名单才纳入（存量不受影响、断档日不启用），清仓因此天然延后一天 | `test_l2_selection_stability.py` 5 项 |
| P2-1 | `apply_committee_modifications` 遇「原股数 >0、投委会缩到 0」直接置 `rejected`（`[投委会取消]`），不再留下 0 股单空转 | `test_review_fixes.py` 新增 2 项 |
| P2-2 | `create_tactical_plan` 先取消同 ticker 同日 active 计划；`get_tactical_plans_by_date` 默认只返回 active（`include_inactive=True` 保留留痕）；巡检用 `ORDER BY plan_date DESC LIMIT 1` 时不再取到空 exit_plan | `test_gap_driver.py` 新增 1 项 |

## 兼容性处理
- `get_tactical_plans_by_date` 新增可选参数，默认行为收紧为 active；阶段快照测试改用 `include_inactive=True` 校验历史留痕。
- 止血：LLM 把 `stop_loss` 写成裸数字/字符串时按价格解析，不因类型异常中断整个 L4 批次。
- 硬止损巡检在止损只来自执行计划（无 L3 计划）时不再因 `plan=None` 崩溃。

## 验证
- 相关模块：180 passed。
- 全量串行 pytest：2619 passed / 5 skipped。
