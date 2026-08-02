# VIP 私人投顾后续开发全面规划（2026-08）

> 本规划由「现状调研 → 逐特性设计 → 对抗式反审（YAGNI/复用/边界）」三阶段多智能体工作流产出，
> 所有结论均经真实源码核实并引 `file:line`。反审阶段的整改已折进下方每个特性的**最终方案**，
> 而非原始设计——凡设计与反审冲突处，一律以反审（更省、更诚实、更守边界）为准。
>
> 覆盖四大用户点名特性 + 额外候选模块 + 跨切面复用底座 + 不可逾越的守门红线。
> 相关既有文档：`docs/VIP_ADVISOR_ROADMAP_2026-07.md`、`docs/VIP_ADVISOR_VALUE_ASSESSMENT_2026-08.md`、
> `docs/VIP_ADVISOR_TECH_SPEC.md`。相关记忆：`project_vip_phase5_yagni_deferred`、
> `project_vip_value_assessment_2026-08`、`project_multiuser_isolation`、`project_frontend_cdn_blocked`。

---

## 0. 贯穿全规划的守门红线（任何特性不得违背）

| 红线 | 含义 | 落点 |
|---|---|---|
| **止于建议** | 不下单、不碰真实资金、不做实时自动投顾；对外文本过 `compliance.with_disclaimer` | `vip/compliance.py:18` |
| **VIP 只写推算层** | 只写 推算层/衍生品条款/账户日志/结单物化，**绝不改 `sim_*` 真值** | `store_vip_projection.py:2-6`；`_resolve_ref` 防空 ref 误喂决策中心模拟盘 `vip_api.py:81-88` |
| **严格用户隔离** | 绝无全局 Key；读写必 `.for_user(sub).for_market(market)` + `_filtered` | `store.py:70-191` |
| **诚实降级** | 缺数据留空转人工不臆造；非美元绝不 ÷1 冒充美元 | `number_guard._USD_CCY`、`ingest._cmbi_to_usd` 缺锚返 None |
| **时区约定** | UTC 存储（`_now_iso`）/ 北京展示（`fmtBJ`）/ 调度 Asia-Shanghai | `store_base.py:14-20`、`wizard-state.js:106` |
| **前端本地 vendor** | 国内 CDN 被墙，新增前端零外链 | `project_frontend_cdn_blocked` |
| **金融 PII** | 只读不入库不入日志；不擅自 commit/push/删除含 PII 数据 | `vip_api.py:4` |
| **YAGNI 守门（已决策）** | Phase 5 量化瓶颈地基「证明后再建」；regime→板块目标权重执行器「**永不建**（越界）」 | `project_vip_phase5_yagni_deferred` |

---

## 1. 跨切面复用底座（四大特性都吃它，先立此约，勿重造轮子）

调研已确认本仓把「隔离落库/调度补跑/操作广播/鉴权锁屏/北京时间/红负绿正」全部沉淀为成件。新特性一律复用：

| 能力 | 复用件 | 位置 |
|---|---|---|
| 用户+市场隔离 store 克隆 | `for_user` / `for_market` | `store.py:70-93` |
| 读时自动追加过滤（含 UNION/子查询安全护栏） | `_filtered` / `_user_filter` / `_market_filter` | `store.py:96-191` |
| 写时隔离列拼接 | `_user_insert_cols/vals/params` | `store.py:137-184` |
| 加锁写事务 + WAL | `_write_conn` / `_connect` | `store.py:194-216` |
| 幂等迁移（新列 `ALTER`、新表 `CREATE IF NOT EXISTS`） | `MIGRATIONS` 列表 | `store_schema.py:693` |
| VIP 落库范式样板 | `upsert_projection` / `log_account_event` | `store_vip_projection.py:19-73,157` |
| 声明式定时任务注册 + 双市场 + 补跑 | `_JOB_SPECS` / `_iter_users` / `job_vip_project` | `scheduler.py:1066,30,243` |
| 操作日志落库 + SSE 红点广播 | `record_operation` / `_UserBroadcaster` | `web/oplog.py:60-70,17-48` |
| 三档鉴权 + VIP 锁屏门禁 | `get_current_user`/`require_admin`/`require_vip` + `require_vip_unlocked` | `auth/dependencies.py:8-44`、`vip_api.py:38-43` |
| 隔离 store 取用 + 子账户解析 | `_wl` / `_resolve_ref` | `vip_api.py:75-88` |
| 前端页签骨架 + 渲染助手 | `VIP_ACCOUNT_SUBTABS`/`switchTab`/`esc`/`fmtNum`/`ccySym`/`vipGet`/`setStatus` | `vip.js:17,27-108` |
| 北京时间格式化 | `fmtBJ` | `wizard-state.js:106` |
| 红负绿正着色 | `st-pnl-pos/neg/zero` | `simtrading.css:584`、`vip.js:1124,806` |
| LLM 数字幻觉护栏 | `number_guard.verify_numbers` / `annotate_unverified` | `number_guard.py:106-172` |
| 市场/代码归一器 | `normalize_market`/`normalize_ticker` | `store_base.py:31-101` |
| 数据现查总入口（含 failover/熔断/按用户 key） | `get_hub().fetch(cap,ticker,market,user_id)` | `data_provider/hub.py:133` |

**底座已知缺口（据实，非本规划新造）**：无「按功能模块红点/未读计数」组件（只有 oplog 操作流广播）；无独立汇率快照表/每日 FX 任务；scheduler 无「按日期区间历史回填」通用件；前端 `.val-*` 类语义冲突（A股红涨），**新 VIP 组件勿复用 `.val-*`，必用原生 `st-pnl-*`**。

---

## 2. 特性一：币种敞口 + 汇率损益归因

**反审结论：`sound`（复用主张全部经代码核实成立，未越死线）。**

### 2.1 现状与本质
- 多币种头寸**解析时就已归一到美元**：`EquityHolding.market_value_usd` 即统一基币，`normalize_statement` 写进 `positions.market_value_base`（`portfolio.py:153-158`）。
- **币种敞口后端已现成**：`_exposure_breakdown`（`portfolio.py:1037`）按 `currency` 分桶 Σ`market_value_base`，已挂进 `dossier.exposure_breakdown.by_currency`（`portfolio.py:1280`）——但**前端 `vip.js` 零渲染**。
- 期末点位 FX（`mv_usd/mv_nominal`）与原币市值（`market_value_nominal`）**解析时就在手**（`ingest.py:37-38`），但 `_upsert_position`（`portfolio.py:200-228`）**未持久化**，`positions.fx_rate` 恒 1.0（`projection.py:106-107` 已记此坑）。
- **本质是「算法未接 + 字段未落」，非数据不可得。** 唯一真数据缺口是「逐日/任意日 FX 时序」（`data_provider` 无 FX 适配）——本特性不依赖它，划 P2 deferred。

### 2.2 复用（不另起归因引擎）
| 复用件 | 怎么用 | 位置 |
|---|---|---|
| `_exposure_breakdown` | SELECT 加 `market_value_nominal`/`fx_rate` 两列，聚合出 `by_currency_detail=[{ccy,usd,nominal,implied_fx}]`，与现有 `by_currency` 并存 | `portfolio.py:1037-1071,1280` |
| `contribution_attribution`（纯函数） | 加姊妹纯函数 `fx_attribution`：本币收益 `r_local=npx1/npx0-1`、汇率收益 `r_fx=fx1/fx0-1`、乘性 `total=(1+r_local)(1+r_fx)-1`，**残差进 total 腿不塞错腿** | `metrics.py:142-171` |
| `_contribution` 接线 | 照抄相邻两期胜出快照取数，新增 `_fx_contribution` 挂 `dossier.fx_attribution` | `portfolio.py:518-540` |
| `EquityHolding` 双口径 + normalize 写入 site | `_upsert_position` 加两 kwarg 透传：`market_value_nominal=h.market_value_nominal`、`fx_rate=(mv_usd/mv_nominal if mv_nominal else 1.0)` | `ingest.py:32-47`；`portfolio.py:153-158` |
| `number_guard` 非美元诚实闸门 | 原币金额/隐含 FX 照 `compute_realized_pnl_fifo` 分列范式；缺锚留 None | `number_guard.py:29,32` |
| `vip.js` echarts 饼 + 页签骨架 | 币种敞口饼复用 `renderHoldingsPie`；归因表套 `st-pnl-*` | `vip.js:17,27-108` |

### 2.3 反审整改（已折进方案）
1. **删掉一次性 `backfill_fx.py` 脚本**：`ingest_and_store` 对重复文件已自动重解析并刷新 `parsed_json`（`ingest.py:1665-1672`），**存量回填=让用户重导结单**，不写新脚本（省一个 newWork）。
2. **先评估复用死列 `positions.market_value`**：该列此刻被 `_upsert_position` 写成 `market_value_base` 的死重复（同值）。落地前先 grep 全仓 `market_value` 读点——**若确认无人当 USD 读**，改写它落原币值即零新增 schema 列；**存量污染风险高则按原设计加 `market_value_nominal` 新列**。默认先查读点再决定，别默认加列。
3. **前端 `implied_fx` 直读落库 `fx_rate`**，不在 `_exposure_breakdown` 二次算 `mv_usd/mv_nominal`（避免舍入抖动与落库值不一致）。
4. **P2 归因表 UI 可推迟**：P0+P1（字段落地 + 币种敞口饼）已交付主价值（把被 mv_usd 掩盖的原币敞口显出来）；P2 先只落 `fx_attribution` 纯函数 + `dossier` 挂载（后端零风险），前端归因表视反馈再上。

### 2.4 分期
| 期 | 交付 | Gate |
|---|---|---|
| **P0 字段落地 + 存量回填** | `_upsert_position` 回填 `fx_rate`/原币市值（复用死列或加列）；存量重导 | `SELECT fx_rate,market_value_nominal`：港币持仓 `fx_rate≈0.128`、日元≈0.0067，且 `nominal*fx_rate≈market_value_base`(±1%)；美元=1.0；旧数据 `fx_rate` 不再恒 1.0 |
| **P1 币种敞口呈现** | `_exposure_breakdown` 扩 `by_currency_detail` + 前端币种敞口饼 | 多币种账户饼图正确显示 HKD/JPY/USD 三桶，tooltip 含原币金额与隐含汇率；纯美元账户只显 USD 桶不报错 |
| **P2 FX 归因** | `fx_attribution` 纯函数 + `_fx_contribution` 接线（+ 前端归因表可延后） | 构造港币标的本币+5%、港币贬2% → 归因表 `r_local≈+5%`/`r_fx≈-2%`/`total≈+2.9%`(乘性)；纯美元 `r_fx=0`；缺 nominal 行 FX 腿留空计入 coverage note |

**自检**：`metrics.py __main__` 加 assert（照 `metrics.py:174` 范式）——美元标的 `r_fx==0`；港币标的 `nominal_px 78→81.9`(+5%)、`fx 0.128→0.1255`(-2%) → `total≈+2.9%`，`w0×total` 与 contribution 一致（±1e-6）；缺 nominal 标的跳过并计未覆盖。

### 2.5 诚实边界与 YAGNI
- 逐日 FX 时序是真数据缺口 → 本期只做「期末 vs 期末」点位归因，逐日划 P2 deferred（与 `projection.py:107`、`TECH_SPEC:770` 一致）。
- 有权威结单汇率（cmbi Account Summary `ingest.py:1335-1375`）优先用，无则用隐含值并标注口径。
- **不建** FX 快照表 / 每日 FX 定时任务 / 不接 yfinance FX 对 / 不放开非美元逐日 MtM / 不新增 HTTP 端点 / 本期不做地域敞口。

### 2.6 待定问题
- FX 锚优先级：权威结单汇率 vs 逐持仓隐含值不一致时以哪个落 `positions.fx_rate`（默认权威优先、隐含回落）。
- 二腿分解用乘性（`(1+rl)(1+rf)-1`，交叉项并入 total）还是对数可加（完美可加但需解释）——倾向乘性。

---

## 3. 特性二：对话内实时行情立查

**反审结论：`sound`（路线 A 确定性预取选型正确，无死线风险）。**

### 3.1 现状与本质
- VIP 对话喂给 LLM 的是**入库快照非现查**：`stream_vip_chat`（`chat.py:107`）单模型单趟流式，文件首行明写「不做多轮 tool-loop」；facts 来自 `build_account_dossier`，价=`market_snapshots` 最新收盘（EOD 非盘中）。
- 全仓 grep 证实 **`vip/` 目录零处调用** `fetch_realtime/get_hub/CAP_QUOTE`——对话层完全无现查能力。
- 取数底座**已完整且生产在跑**：`get_hub().fetch` 含 failover+熔断（阈值5/冷却60s）+按用户 key+记账，A股/美股双市场 `StandardQuote`（price/change_pct/pe/pb/market_cap/timestamp）就绪。
- **缺口是「编排层未接」，非数据不可得。**

### 3.2 方案（路线 A：确定性预取，零管线改造）
在 `chat.py:131-135` facts 生成后、`prompt.format` 前，插一段现查：抽 symbol → `get_hub().fetch(CAP_QUOTE)` → 实时值**同时并进 `facts_text` 与 `guard_corpus`** → `_iter_tokens` 单趟原样回答。

| 复用件 | 怎么用 | 位置 |
|---|---|---|
| `get_hub().fetch(CAP_QUOTE/CAP_FINANCIALS,…,user_id)` | 唯一现查入口，透传 user_id/归一 ticker；报价走免费 yfinance/akshare 无 key 主力 | `hub.py:133` |
| `stream_vip_chat` + `_build_facts` + guard 合并点 | facts 后插现查文本，追加进 `facts_text`+`guard_corpus`，单趟不引 tool-loop | `chat.py:107,131-135` |
| `number_guard.foreign_account_values` | 现查文本**按 currency 分列**注入 guard_corpus，HKD 现价按外币池核验不进美元断言 | `number_guard.py:32,106` |
| `_US_TICKER_RE` + `collect_priceable_symbols` 形态过滤 | 判定「哪些标的可现查」（US 字母码/A股6位可查，港股数字/ISIN 跳过），**勿另写正则** | `projection.py:20,23-44` |
| `normalize_ticker`/`normalize_market` | 现查前统一归一，杜绝 hub.fetch 跨源匹配失败 | `store_base.py:31-101` |

**新代码仅一个薄文件** `vip/live_quote.py`（~60 行）：`fetch_live_quotes`（归一→形态过滤→`asyncio.gather` 逐票 fetch+整体超时+容错，失败票列 skipped 不塞0）+ `_pick_live_symbols`（意图/目标抽取）。

### 3.3 反审整改（已折进方案）
1. **P1/P2 合并为单期**：`guard_corpus` 合入**不是可选项**——P1 一旦注入现价文本却不同步并进 guard_corpus，`number_guard.annotate_unverified`（`chat.py:155`）当场把现价误标「未核到」。基本面补查是同函数几行分支，无 gate 隔离必要。
2. **裸代码抽取限白名单**：`question` 抽到的裸代码只与「持仓∩形态可查」**取交集不做并集扩展**——防用户随口提任意代码触发无谓外部查询打爆额度（成本死线的一部分，非待定项）。
3. **限流上限做成命名常量校准旋钮**（照 `advice_review.py:18 _BAND` 范式），别把 `≤8票` 写死在函数体。
4. **SSE 现查角标本期直接不做**（设计自标「可选/非必须」）——回答带「数据源+北京时间」文本已满足「答案标源」目标，气泡角标纯装饰，YAGNI 砍。
5. **CAP_FINANCIALS 辅查若工作量吃紧可整体后置**：免费 CAP_QUOTE 已覆盖「现在多少钱/涨跌%」主场景（~80% 问法）；PE/PB 靠 keyed 源缺 key 即空，增量有限。

### 3.4 分期（合并后）
| 期 | 交付 | Gate |
|---|---|---|
| **P1 报价现查主干（含 guard 闭环，基本面辅查同期或后置）** | `live_quote.py`（适配+目标抽取+限流常量）+ `chat.py` 接线；现查数值全并入 `guard_corpus`；US/A股 equity 生效，港股/ISIN 诚实跳过 | 真实账户问「我持仓的 XX 现在多少钱」，回答现价与 `hub.fetch` 直查一致且带数据源+北京时间；港股答「无实时映射，用最近收盘」不臆造；引用的现价/PE 不被误标「未核到」；`__main__` 自检通过 |

**自检**：`live_quote.py __main__`——注入 fake hub（AAPL→`StandardQuote(price=250)`、0700.HK→形态过滤不进 fetch、缺映射票→None）；assert 港股/ISIN 不进 fetch、返回含 AAPL 且 `price==250`、失败票落 skipped 而非塞0、HKD 现价不进美元白名单文本。一条 assert 链，无框架。

### 3.5 诚实边界与 YAGNI
- 现查**只在用户问答时触发的只读查询**，绝不演变为定时盯盘/下单信号；不写 `sim_*`/positions。
- 港股数字码/欧洲 ISIN 无稳定 yfinance 映射 → 跳过留 carry-forward 并明示；缺 key 基本面空并标源。
- 实时性是「近实时」（源本身分钟级/EOD），不宣称 tick 级；单次问答现查上限 + 并发 + 整体超时防打爆额度。
- **不做** 真 tool-call 多轮循环（路线 B，成本/延迟显著上升）/ 单票「刷新此仓」按钮+新端点 / 落库现查快照 / 逐日 FX 现查重估。

---

## 4. 特性三：VIP 投资经验复盘闭环

**反审结论：`missing-reuse`（后台闭环已在跑，设计误把可直读的单表数据拖进 M 级 join；已按整改大幅降本）。**

### 4.1 现状与本质
VIP **已有一条物理隔离于 sim 的「建议→再评即结→校准→回看」闭环，且已在跑**：
- **打点（C-1）**：`advisory`/`recommend` 出建议即 `record_prediction` 写 `model_accuracy` 的 VIP 桶（`role_context="vip_advisor"`, `prediction_type∈{vip_advice,vip_recommend}`, value=动作），与 sim 的 `committee_*/vote` 物理隔离（`advisory.py:978-991`、`recommend.py:280-293`）。
- **对账**：`review_pending_advice`（`advice_review.py:59-88`）读共享行情算涨跌幅、±3% band 判方向、逐条 `record_outcome` 写 `is_correct`+`outcome_value='chg=+5%'`，周任务 `job_vip_advice_review`（`scheduler.py:690`）每周跑。
- **落库可回溯**：整份建议进 `vip_advisory`/`vip_recommendations`（`store_schema.py:1288-1312`），`list_advisory`/`/history` 端点现成（`vip_api.py:755-791`）。
- **校准回看（F1）**：`ModelCalibrator.recalibrate` 按 `role_context` 聚合出 `vip_advisor` 权重，`advisor_calibration` 读回注入建议。

**真正缺的只是「面向用户的呈现面」+「账户级粒度」——数据已具备。**

### 4.2 复用（核心整改：直读单表，不 join 拼装）
反审关键发现：`{date,ticker,action,chg,correct}` **五列已全在 `model_accuracy` 单表**（`prediction_value=action`、`outcome_value='chg=+5%'`、`is_correct`、`prediction_date`）。原设计的 M 级 `vip_advisory×model_accuracy` join 是把已有数据重拼一遍。

| 复用件 | 怎么用 | 位置 |
|---|---|---|
| `list_pending_predictions` getter | **加 settled 孪生**（翻 `is_correct != -1`）即得已结明细，五列直接 map 成 ledger——本期唯一必写的 store 层新码 | `store_ai_models.py:64-88` |
| `get_model_accuracy_stats` 汇总 | 命中率 KPI 直接调它取 `vip_advisor` 桶 `total/correct/pending`，**勿在 ledger 里重算** | `store_ai_models.py:112-130` |
| `list_advisory` result_json | reason 列若要：best-effort 从当日含该 ticker 最近一份 `holdings[].reason` 回捞，取不到留空，**不作为 stitch 主键** | `advisory.py:1049-1072` |
| `decision_api /model-accuracy` 呈现范式 | 端点结构仿写 | `decision_api.py:1123-1141` |
| VIP 页签骨架 + `st-pnl-*` + `fmtBJ` + `_wl`/`_resolve_ref` | `VIP_ACCOUNT_SUBTABS` 加 `{key:'review',label:'复盘对错'}`；隔离/着色/时间全复用 | `vip.js:17`、`simtrading.css:584`、`vip_api.py:75-88` |

### 4.3 反审整改（已折进方案）
1. **`build_review_ledger` 从 M 降为 S**：五列直读 settled 孪生 + 命中率读 stats getter，reason 降为 best-effort 单表回捞。整个 Phase1 = 1 store 孪生 getter(S) + 1 端点(S) + 1 subtab(M)，无独立「对账」逻辑。
2. **reason 列 Phase1 可整列先砍**：action+chg+对错徽标+命中率 KPI 已构成可信后视镜；reason 富化（含 result_json 解析歧义：同日多次生成/advice+recommend 并存是多对多）证明面板被用后再补。
3. **⚠ Phase2 `account_ref` 触碰共享件（唯一贴近死线处）**：`record_outcome`/`record_prediction` **还被 `committee.py:765` 走**（`role_context=committee_*/vote`）。新增 `account_ref` **必须 keyword-optional 默认 `''`**，使 committee/vote 桶结算 `WHERE account_ref=''` 字节不变——否则逼近「vip_advisor 桶绝不撞 committee_*/vote、勿污染 sim 校准」隔离死线。Phase2 gate 必须加一条「committee/vote 结算 rowcount 与改前一致」的 assert。

### 4.4 分期
| 期 | 交付 | Gate |
|---|---|---|
| **Phase1 呈现闭环（本次主体）** | 「复盘对错」subtab：逐条 建议动作/实际 chg/对错徽标 + 顾问命中率 KPI（reason 列可先砍） | 选定账户看到已结建议对错且命中率与 `get_model_accuracy_stats` 一致；stitch 自检 assert 通过；缺价条目呈现「待结」不报错；换用户看不到（隔离） |
| **Phase2 账户级台账（deferred，边缘触发才做）** | `model_accuracy.account_ref` 落地（keyword-optional 默认 `''`），命中率可按账户拆 | 两账户同标同日不同动作 → 各自独立结算（`record_outcome` rowcount 各 1、`is_correct` 各自判定）；**committee/vote 结算 rowcount 与改前一致**（隔离守卫 assert） |
| **Phase3 经验卡沉淀（deferred，遵 YAGNI 守门）** | 接 `attribution.py:104-109` C-4b → `store_research` 经验卡 | 累计样本 ≥ `_CARD_THRESHOLD(12)` **且 Phase1 呈现面被证明使用**后才接；否则不建 |

**自检**：`advice_review.py __main__` 加 `build_review_ledger` stitch 断言——喂 fake advisory{AAPL,加仓,D} + settled pred{AAPL,vip_advice,D,is_correct=1,'chg=+5%'} → ledger 恰一行 correct=True/chg=+5/action='加仓'；无对应 pred 的 ticker → 该行 correct=None 标「待结」不抛。

### 4.5 诚实边界与 YAGNI
- 全程只读呈现，绝不写 `sim_*`；Phase2 `account_ref` 写在复盘桶非真值，符合只写推算层。
- 启发式天花板照旧：±3% band 三值方向 + 二值对错，无幅度评分/持有期/基准相对（跑输大盘 beta 算不出）；`_BAND` 是校准旋钮，本期不动。
- 无行情不硬结、保持 pending；港股数字码/ISIN 无稳定映射 → carry-forward 留空标「无法结算」。
- **不补落库**（`vip_advisory`/history 已现成）/ **不建经验卡桥**（YAGNI-gated，需 sim_trades 真值 VIP 无、样本未到阈值）/ **不改 `_judge` 启发式** / **不做逐日走势曲线** / regime→板块目标权重执行器**永不建**。

---

## 5. 特性四：预设产业链用户共享模板库

**反审结论：`missing-reuse`（双存储自相矛盾 + market 维度未定却已固化进 schema；已按整改收口）。**

### 5.1 现状与本质
- 模板现状两套互不相干：`chain/data/*.json`（ev/gpu/robot）是**死文件**（grep 全仓无加载代码，且 `ev_chain.json` 内容实为**商业航天**误名）；`cli.py:32-38` `PRESET_CHAINS` 硬编码字典（label→sector/product 字符串，选中只是再跑一遍 LLM 全量拆解）。
- 真正被持久化的是拆解结果：`ChainStore` 写独立库 `chains.db` 的 `chain_versions`（`chain_store.py:22-91`），**无 user_id/owner/公开标志**——事实上已是隐式全局共享，但无归属、未作为模板暴露。
- 唯一受控跨用户读范式 `__shared__` 桶（`store_market_data.py:13`）**抹掉归属**，与「用户生成+署名+可发布」语义不同。
- **全部数据可得，缺的是元数据表 + 端点 + 前端 + 受控可见性，纯工程落地。**

### 5.2 方案（元数据表 + 快照冗余 + Python 层受控可见性）
新表 `chain_templates`（落 `watchlist.db` 继承隔离助手）：
```
id INTEGER PK / user_id TEXT(owner) / market TEXT('all') / template_name TEXT /
description TEXT / sector TEXT / end_product TEXT / max_depth INT /
source_chain_id INT(仅溯源审计,永不查) / chain_json TEXT(权威快照) /
is_public INT DEFAULT 0 / created_at TEXT(UTC) ；UNIQUE(user_id, template_name)
```

| 复用件 | 怎么用 | 位置 |
|---|---|---|
| Web 向导载入已存链通路（`reused=True`） | `stream_phase1` 直接吃 `chain_templates.chain_json`→`ChainGraph(**cj)`→走已跑通复用路，跳新鲜度/模型门 | `phases.py:155-164` |
| `for_user`/`_user_filter`/`_user_insert_*` | 模板表落 watchlist.db 自动继承隔离；写走 `_user_insert_*(owner_id)`，读「我的」走 `_user_filter` | `store.py:70-191` |
| `_shared_filter` swap→restore 手法 | 公开段单独用助手（把 user_id 临时置公开态），**Python 层 UNION 合并**「我的 + 公开」，避开 `store.py:115` 的 OR/UNION 护栏 | `store_market_data.py:31-39` |
| `ChainGraph` Pydantic | 选用时 `ChainGraph(**chain_json)` 做结构校验，脏 JSON 抛错不进向导 | `models.py:75-106` |
| 向导页 `phases.js` 渲染助手 | 模板属 chain 侧，UI 挂向导页，**勿从 `vip.js` 跨页搬** | `phases.js:654` 附近 |

### 5.3 反审整改（已折进方案）
1. **删掉双存储矛盾**：`chains.db` 是会被清的死缓存 → **快照 `chain_json` 权威**，`source_chain_id` 降为纯溯源审计列（可空、永不查），**删除「跨库取链适配」整个 newWork**（`get_chain_by_id` 在此特性根本不需调用）。
2. **砍掉 market 强隔离**：产业链天然跨市场（GPU 链两市都有），`market` 存 `'all'`（或存但 list 不过滤），`UNIQUE` 改 `(user_id, template_name)`——按 market 切分与「共享」目标冲突且平白翻倍存储。
3. **⚠ OR 可见性查询是私有泄露唯一实质风险点**：`_user_filter` 护栏（`store.py:119`）会拦 OR/子查询。**不在 store 里手写裸 OR SQL 绕护栏**，而用 `_shared_filter` 式 swap→restore + Python 层 UNION。**selfCheck 必须补一条 SQL 拼接层不错插的 assert**（验证过滤参数位置正确，照 `store.py:131` count_before 口径）。
4. **官方三件套 seed 推迟/砍到 P3 之后**：三件 JSON 是「无人加载的死文件」、`ev_chain` 还得先改名——连是否有人用都没证明（同 `project_vip_phase5_yagni` 纪律：生产证明价值再建）。P1/P2 先跑通用户自建+发布闭环，官方 seed 按需补。
5. **砍掉独立「模板管理 mini 面板」**：删除/切公开各一个按钮，挂在模板下拉每项右侧即可。

### 5.4 分期
| 期 | 交付 | Gate |
|---|---|---|
| **P1 后端骨架（私有另存+选用闭环）** | `chain_templates` 表 + `ChainTemplateStore`(save/list_my/get/delete) + POST/GET/DELETE 端点 + `Phase1Request.template_id` 直取链（先只做私有 `is_public=0`） | 新建模板→GET 见到→POST /phase1 带 template_id→SSE `step_done reused=True` 且链体一致；换用户 GET 看不到（隔离） |
| **P2 受控公开可见性** | `is_public` + PATCH /public + `list_visible_templates`（Python 层 UNION，非裸 OR SQL） | 用户A发布公开→用户B 能看到且能选用，但 DELETE/PATCH 返 403；A 私有 B 仍不可见；**SQL 拼接层 assert 过滤参数位置正确** |
| **P3 前端接入** | 向导模板下拉 + 另存为弹层 + 删/切公开按钮 | 从 UI 选模板起步跑完 Phase1、另存、切公开全链路可操作，无外链 |

**自检**：`tests/test_chain_templates.py`——同一 store 造两个 `for_user` 克隆 A/B：(1) A save 后 A 见 B 不见；(2) A set_public 后 B `list_visible` 见到但 `delete` 拒绝（仅 owner）；(3) `get_template` 取回 `chain_json` 能 `ChainGraph(**cj)` 还原；**(4) `list_visible_templates` 传含 ORDER BY 的查询，assert user_id 过滤参数位置正确（OR 分支未静默错插）**。纯 store 层 assert，无框架。

### 5.5 诚实边界与 YAGNI
- 模板只是选股向导输入起点，天然不越「止于建议」死线。
- 跨用户可见性严格二值：owner OR `is_public=1`；私有绝不泄露，安全失败优先。
- 公开模板抹不掉署名（不塞 `__shared__`，owner_id 恒真实用户）。
- **不做** 评分/收藏/热度/评论 / 版本树 diff / 审核举报下架 / 模板文件导出导入 / 不动 `PRESET_CHAINS` 与 `get_fresh_chain` 缓存 / 不迁表进 `chains.db` 自建隔离管道。

---

## 6. 额外候选后续模块（用户「包括不限于」——据已核实缺口据实列，不画大饼）

> 说明：本轮工作流中「路线图与守门」探针 agent 未产出实质内容（返回占位符），故以下候选取自四个真实
> 研究 agent 的 `decidedConstraints` 与既有文档、记忆，**均据实标注状态**，未证明价值者明确守门不建。

| 候选模块 | 价值 | 状态 / 边界 |
|---|---|---|
| **地域/资产类别多维敞口** | 与币种敞口同源（`_exposure_breakdown` 已按 `instrument_type` 分桶），补地域维度即多维敞口视图 | 🟢 已规划（`VALUE_ASSESSMENT:78,186`）；本轮特性一先做币种+FX，地域另算，避免一次吞太多 |
| **逐日 FX 时序 / 非美元逐日 MtM 重估** | 两次结单之间的逐日汇率归因与盯市 | 🟡 已决 P2 deferred（`TECH_SPEC:770` M1 明确不做，`projection.py:107` 已标）；yfinance FX 通道现成（`macro_data.py:157 CNY=X`）但未接，**证明期末归因价值后再接** |
| **复盘结算 oplog 红点提示** | 「本周结算 N 条建议」推送用户复核 | 🟡 可复用 `record_operation`（`oplog.py:60`），属特性三增量，视复盘面板被用后决定 |
| **复盘 `_judge` 启发式升级** | 加幅度评分/持有期口径/基准相对（跑输大盘） | 🟡 `_BAND` 已注为校准旋钮（`advice_review.py:18`），**证明呈现面被用后再谈**，本期不动 |
| **PII 脱敏/掩码公共件** | 集中的金额/账号掩码函数，替代各端点自律 | 🟡 底座缺口（算法未接）；`vip_api.py:4` 声明 PII 只后端处理但无集中掩码，规模化后补 |
| **按功能模块红点/未读计数组件** | 真正的 per-模块未读徽标（现只有 oplog 操作流广播） | 🟡 底座缺口，多特性都会用；需自建计数字段，有多处消费者后再抽 |
| **量化瓶颈地基（Phase 5，持久化 chain 五维评分）** | 把定性主题图升级为量化强度暴露 | 🟠 **YAGNI 守门待证明**：`project_vip_phase5_yagni_deferred` 明确「定性主题图在生产被证明影响决策后再建」，`chain/models.py:BottleneckScore` 五维模型已在，缺的是账户级持久化+join |
| **regime→板块目标权重自动执行器** | 按宏观 regime 自动调板块目标权重并执行 | 🔴 **明确永不建（越界）**：`project_vip_phase5_yagni_deferred` + L1 轮动桶对账已够；触碰「止于建议」死线 |
| **券商 API 直连实时下单 / 全自动投顾** | 接券商实时下单 | 🔴 **明确不建**：超出「止于建议、不碰真实资金」系统死线，刻意的产品边界 |

---

## 7. 落地顺序建议（据「价值/成本比」与依赖）

1. **特性一 P0+P1**（币种敞口）：后端已现成，成本最低（补 1 列/2 参 + 一个饼），直接补上 `VALUE_ASSESSMENT` 点名的「多币种真账户」主力缺口。
2. **特性三 Phase1**（复盘呈现）：后台闭环已在跑，按反审降本后近乎「1 getter + 1 端点 + 1 subtab」，把沉睡数据变可信后视镜，性价比极高。
3. **特性二 P1**（实时立查）：取数底座全现成，一个薄 `live_quote.py` + 一处接线，显著提升对话可信度。
4. **特性一 P2**（FX 归因）+ **特性四 P1-P3**（模板库）：前者算法增量，后者是四特性中唯一新表+新端点+新前端全套，工作量最大，放最后。
5. 额外候选按各自守门条件触发，**均待相应前置价值被证明**。

**全部工作待管理员明确批准才 commit**（金融 PII + 未验收上线双重约束，遵既有纪律）。
