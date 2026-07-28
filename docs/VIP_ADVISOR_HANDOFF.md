# VIP 私人财务顾问 — 交接词 / 当前开发日志

> 用途：切到新会话时，直接把本文件贴给 Claude，按「三、Phase 5b 开工包」继续，无需重新梳理上下文。
> 最后更新：2026-07-27

## 状态锚点（务必先 `git status -sb && git log --oneline -6` 复核）

- **主线**：`main = cd37a37`（Phase 5a 已提交，工作区干净）
- **未推送**：本地 `main` 领先 `origin/main` **5 个提交**（`74a76a5`→`cd37a37`，含 Phase 1–4 + 5a），**尚未 push**。部署/交接前需 `git push origin main`（仅在用户要求时）。
- **VIP 基线**：`pytest tests/ -k vip -q` → **88 passed / 2 skipped**
- **离线全量基线**：`pytest tests/ -m "not slow" --disable-socket --allow-hosts=127.0.0.1,::1 -q` → **1169 passed**
- **纯逻辑自检**（`PYTHONIOENCODING=utf-8`）：`python -m bottleneck_hunter.vip.portfolio` / `.advisory` / `.recommend` → 各 `... self-check OK`
- 模式：**Ponytail full**（复用优先、最短有效 diff）· **管理员单用户自用** · 全部中文 · **提交/推送仅在用户明确要求时**

---

## 一、一句话现状

VIP 顾问四阶段路线（A 档案层 / B 接决策 / C 荐新 / D 复盘，见 `docs/VIP_ADVISOR_ROADMAP_2026-07.md`）与 **Phase 1–4**（宏观入链 / 投委会喂真实数据 / 合议加固 / 草案对账）已完成并提交；**Phase 5a**（净值基准对照 A + 现金预算提示 B + 复盘打点 C-1）已完成提交（`cd37a37`）。**当前正在开发 Phase 5b — 复盘闭环主体（C-2 / C-3 / C-4，代码尚未动笔，关键复用件已探查完毕）**。

C-1 打点自 Phase 5a 上线起已在 `model_accuracy` 表按 `role_context='vip_advisor'` 积累 pending 预测——**数据时钟已启动**。C-3 复盘管道现在就能开发（逻辑用构造数据自检），真实校准信号随数据自然流入（建议 ≥3 已评样本 / ~30 天后再看信号）。

---

## 二、给新会话的交接词（可直接复制）

> 我们继续 Bottleneck-Hunter 的 **VIP 私人财务 AI 顾问线**，现在开发 **Phase 5b：复盘闭环主体**。
>
> 请先读：`docs/VIP_ADVISOR_HANDOFF.md`（本文件，含开工包）、`docs/VIP_ADVISOR_ROADMAP_2026-07.md`（四阶段路线）。
>
> 背景：Phase 1–5a 已完成并提交在本地 `main`（领先 origin 5 个提交、未 push）。Phase 5a 已给 VIP 建议打了复盘点（`record_prediction`，`role_context='vip_advisor'`，`prediction_type` = `vip_advice`/`vip_recommend`）。5b 要把这些 pending 预测「再评即结」成准确率信号，并接归因复盘。
>
> 请按本文件「三、Phase 5b 开工包」的 C-2 → C-3 → C-4 顺序做，先解决 C-3 头号决策点（见下），每步留 `__main__` assert 自检，最后跑「四、验证命令」三件套。**不要执行真实交易**——VIP 是 advice-only 周期性参谋，「不下单」是设计不是缺陷。**未经我确认不提交、不推送、不删任何财务 PII。**

---

## 三、Phase 5b 开工包 ★（已核事实，照着写）

> 行号供快速定位，可能随改动漂移，以实际文件为准。全部走 `.for_user(sub).for_market(market)` 隔离。

### C-2 · `list_projections` 加区间参数（~4 行，先做，最简）

- 文件：`bottleneck_hunter/watchlist/store_vip_projection.py:70`
- 现签名：`list_projections(*, account_ref="", as_of_date="", kind="", status="")`，函数体用 `clauses/params` 拼 WHERE。
- 改：加 `since_date: str = ""`, `until_date: str = ""`，在 clauses 追加 `as_of_date >= ?` / `as_of_date <= ?`。纯加性、向后兼容。
- 3 个调用点（均关键字调用，不受影响）：本文件 `latest_projection_map`、`vip/projection.py`、`web/vip_api.py`。
- 自检：`__main__` 造 3 条不同 `as_of_date` 记录，断言区间过滤命中数正确。

### C-3 · 复盘 job `job_vip_advice_review`（主体）

**骨架**：照抄 `scheduler.py:626 job_auto_review` —— `for uid, store, budget in _iter_users("vip_project")` → `mstore = store.for_market(market)` → 取 pending VIP 预测 → 逐条判定 → `record_outcome` → `log_account_event` / logger。

**方向判定纯函数**（建议新建 `bottleneck_hunter/vip/advice_review.py`，或并入 `advisory.py`）：
- `_judge(action: str, chg_pct: float, band: float = 3.0) -> bool`（对/错）
  - 加仓 / 建仓 ⇔ 涨（`chg_pct > band` 为对）
  - 减仓 / 规避 ⇔ 跌（`chg_pct < -band` 为对）
  - 持有 / 关注 ⇔ 横盘（`abs(chg_pct) <= band` 为对）
- band 是校准旋钮（ponytail：留可调，物理世界需要 tuning），默认 ±3%。

**「再评即结」结算**（已核 `store_ai_models.py:37 record_outcome`）：
- 签名 `record_outcome(ticker, prediction_type, outcome_value, outcome_date="", score_delta=0.0) -> rowcount`
- **二值编码**：对 → `score_delta=0.0`，错 → `score_delta=5.0`。因内部 `is_correct = 1 if abs(score_delta) < 2.0 else 0`（对→correct=1、错→correct=0）。
- **匹配键 = `(ticker, prediction_type, is_correct=-1, user_id)`**——不含 market、不含 role_context、不含日期。

**⚠ C-3 头号决策点（先定这个再写）**：`record_outcome` 会把同 `(ticker, prediction_type)` 下**所有** `is_correct=-1` 的行一次性结成同一个 outcome。但同一标的可能在不同结算周期各有一条 pending（C-1 每次生成都打点）。Phase 5a 打点注释已预告：「5b 按 `prediction_date` 区间处理」。两条路线择一：
  1. **给 `record_outcome` 加可选 `prediction_date`/日期区间约束**（在 WHERE 追加 `AND prediction_date = ?`），逐条结算——改动小、语义准，**推荐**。注意该方法 `trade_executor.py` 有 1 个现存调用，新增参数须带默认值保持兼容。
  2. 新增 `list_pending_predictions(prediction_types=[...])` 读法（`model_accuracy WHERE is_correct=-1 AND prediction_type IN (...) AND user_id=?`）先取明细、按 id 逐条结——需要一个按 id 的结算方法。
  - 读 pending 明细：现有 `get_model_accuracy(provider, model, role_context, ...)` 需 provider+model、不便扫全量；`get_model_accuracy_stats` 只聚合。大概率需按方案 1 直接 SQL 取 `prediction_type IN ('vip_advice','vip_recommend') AND is_correct=-1` 的明细行。

**取价做判定**：`get_snapshots(ticker, days)`（`store_market_data.py:70`，读共享 `market_snapshots`）取 `prediction_date` 与复盘日收盘，算 `chg_pct`。无价则跳过（不硬结）。

**隔离已核**：VIP 用 `prediction_type` = `vip_advice`/`vip_recommend`（Phase 5a 常量 `VIP_PT_ADVICE`/`VIP_PT_RECOMMEND` 在 `vip/advisory.py`），sim 用 `vote`——`prediction_type` 即隔离键，`record_outcome` 不会误结 sim 的行。role_context 层面 VIP=`vip_advisor`、sim=`committee_{role}` 再加一层隔离。

**注册 4 处**（缺一处则任务不出现在前端/不被调度）：
1. `scheduler.py:1042 _JOB_SPECS`：加 `("us_vip_advice_review", job_vip_advice_review, {"market":"us_stock"}, _TZ_CN, "weekly", "...")` + `cn_` 对称行。格式 `(job_id, func, kwargs, tz, freq, desc)`；freq ∈ `daily/weekly/everyday/interval/monthly`。**建议 weekly**（价格需时间才有区分度，非 daily）——决策点。
2. `schedule_config.py:21 GLOBAL_SCHEDULE_DEFAULTS`：加 `us_vip_advice_review`/`cn_vip_advice_review` 触发时刻（北京时区，与既有周末任务错峰，避免锁竞争）。
3. `scheduler.py:1093 list_job_categories`：归类。建议归 `"vip_project"`（跟随 VIP 每用户开关，与每日推算同组）——决策点。
4. `scheduler.py:1118 list_job_labels`：中文 `{label, desc, tz, freq}`。

**自检**：`_judge` 各分支（涨/跌/横盘 × 加/减/持）+ 二值编码 `score_delta→is_correct` 断言；隔离守卫 `assert VIP_PT_ADVICE != "vote"`。

### C-4 · 结算单导入触发归因复盘

- 挂载点：`vip/portfolio.py` 紧邻 `calibrate_projections`（结算单导入触发处）。
- 复用 `watchlist/trade_reviewer.py` 思路与经验卡结构，**数据源换 VIP 流水**（不碰 sim）。
- **C-5 前置**：仅当 C-4 真写经验卡，才做 `store_schema.py` 迁移尾幂等 `ALTER TABLE experience_cards ADD account_ref`。不写经验卡则跳过 C-5（YAGNI）。

### 时机与范围边界

- **采纳信号**：只跨结算单推断股数变动、标「推断·非确认」，**绝不喂校准**；回喂 `_consensus` 需显式 blend（触发＝≥20–30 已评 outcome 且准确率背离），不靠共桶自动混。
- **不做**：`advice_audit_trail` CHECK 重建加 'review'（复用 `event_type="calibration"`）；不新建「建议复盘表」（先零表，靠 `model_accuracy`+`model_ratings`+log；要复盘历史 UI 时再加薄表）。

---

## 四、验证命令（每步做完 + 收尾都跑）

```bash
# 纯逻辑自检（Windows PowerShell：先 $env:PYTHONIOENCODING="utf-8"）
PYTHONIOENCODING=utf-8 python -m bottleneck_hunter.vip.portfolio    # portfolio self-check OK
PYTHONIOENCODING=utf-8 python -m bottleneck_hunter.vip.advisory     # advisory self-check OK
PYTHONIOENCODING=utf-8 python -m bottleneck_hunter.vip.recommend    # recommend self-check OK
# （C-2/C-3 新增自检模块同理 python -m ...）

# VIP 回归（须保持 88 passed / 2 skipped，新增测试后数字应上升）
pytest tests/ -k vip -q

# 离线全量基线（须保持 1169 passed；socket 警告是预期，非失败）
pytest tests/ -m "not slow" --disable-socket --allow-hosts=127.0.0.1,::1 -q

# 端到端：只能用 serve 启动（绝不用 python -m web.app），浏览器硬刷新
bottleneck-hunter serve
```

E2E 关注点：生成顾问/荐新后 `model_accuracy` 出现 `role_context='vip_advisor'` 的 pending 行；跑复盘 job 后这些行 `is_correct` 从 -1 变 0/1；**sim 的 `committee_*`/`vote` 行与 `sim_*` 交易表零影响**。

---

## 五、真实样本路径（含密码，保留）

| 类别 | 路径 | 备注 |
|---|---|---|
| 花旗月结单 | `C:\Users\walker\Documents\walker\银行文件\花旗月结单\` | 7 期（2025-12~2026-06）对账全 `$0.00` |
| 花旗导出文件 | `...\银行文件\花旗导出文件\` | 流水/已实现盈亏/余额轨迹底料 |
| 花旗日常文件 | `...\银行文件\花旗日常文件\` | Citi MLI / Booster 条款 |
| 野村结单 | `...\银行文件\野村结单\` | **密码 `22704339`**；NAV 锚点 |
| 野村日常文件 | `...\银行文件\野村日常文件\` | **密码 `22704339`**；OAC/ODC Accumulator/Decumulator |

---

## 六、关键文件与行号索引

**Phase 5 涉及**：
- `vip/portfolio.py` — `_rebase_benchmark`(816) / `value_series`(844) 净值+基准；C-4 挂载点在 `calibrate_projections` 邻近
- `vip/advisory.py` — 隔离常量 `VIP_ROLE_CONTEXT`/`VIP_PT_ADVICE`/`VIP_PT_RECOMMEND`；`summarize_cash_budget`/`_parse_weight`；C-1 打点段；`get_latest_advisory`
- `vip/recommend.py` — C-1 打点段；`get_latest_recommendations`
- `watchlist/store_vip_projection.py:70` — C-2 `list_projections`
- `watchlist/store_ai_models.py` — `record_prediction`(12) / `record_outcome`(37) / `get_model_accuracy`(61) / `get_model_accuracy_stats`(82) / `get_calibration_weight`(169)
- `watchlist/store_market_data.py:70` — `get_snapshots`（取价做判定）
- `watchlist/scheduler.py` — `job_auto_review`(626 骨架样板) / `_JOB_SPECS`(1042) / `list_job_categories`(1093) / `list_job_labels`(1118)
- `watchlist/schedule_config.py:21` — `GLOBAL_SCHEDULE_DEFAULTS`
- `watchlist/macro_data.py` — `default_benchmark_ticker`
- `web/vip_api.py` — `/account/budget-reconciliation`(B-2) 等 VIP 路由
- `web/static/js/vip.js` + `index.html`（改 js 版本号 `app.js?v=`）

**文档**：`docs/VIP_ADVISOR_ROADMAP_2026-07.md`（路线）·`VIP_ADVISOR_PLAN.md`·`VIP_ADVISOR_TECH_SPEC.md`·本文件

---

## 七、约束红线（违反即 bug，摘自 CLAUDE.md + memory）

- **服务器只用 `bottleneck-hunter serve`**，`python -m web.app` 不起服务。
- **Key 严格按用户隔离**：无全局 Key，缺 Key 即 `MissingUserKeyError`；Store 用 `.for_user(sub).for_market(market)`。
- **时区**：UTC 存 / 北京展示（`fmtBJ`）/ 调度 `Asia-Shanghai`；勿引入美东或非北京时区。
- **前端库本地 vendor**：国内 CDN（jsdelivr/unpkg/cdnjs）不可达，一律 `web/static/vendor/`（npmmirror 下载）。
- **空 `account_ref` 硬守卫**：VIP 端点空 ref 直接报错——否则经 `build_account_dossier→get_sim_account('')` 越界读/惰性建决策中心模拟盘（见 memory `dc_sim_account_decoupled`）。
- **C-1/复盘隔离唯一干净维度是 `role_context`**：VIP=`vip_advisor`、sim=`committee_{role}`；user_id/market 与 sim 共享，勿靠它们隔离。
- **VIP 是 advice-only 周期性参谋**：从不执行交易，「不下单」是设计，勿当缺陷修。绝不写 `sim_*` 表。
- commit 需含 `📢` 行首独立白话行才进 `UPDATE_HISTORY.json`（hook 自动 amend）。
- AI 配置以顶栏「AI 配置中心」为唯一入口，勿加影子写。
- **未经用户确认不提交、不推送、不删任何财务 PII 文档/记录。**

---

## 八、最近关键提交（本地 main，未 push）

- `cd37a37` feat(vip): Phase 5a — 净值基准对照 + 现金预算提示 + 复盘打点
- `fb6076c` docs+test(vip): 顾问路线图文档 + 离线测试卫生
- `f6e5b38` chore: 决策中心约束提示与模拟交易/前端杂项完善
- `5c25289` feat(vip): 顾问决策与荐新 pass + 投委会信息链路加固(Phase 1-4)
- `74a76a5` feat(vip): 多券商子账户体系与月结单/交易文件接入
- `a840d63` docs(vip): 新增 VIP 顾问开发交接日志（本文件初版）
