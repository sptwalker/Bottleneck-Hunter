# P0–P1 借鉴功能 · 详细开发计划（对照验收基准）

> 依据：`docs/VIBE_TRADING_ANALYSIS_2026-08.md` §5 + 2026-08 六路真实代码调研。
> 原则：Ponytail/YAGNI —— 每项取「填补已知缺口 / 最小正确 diff / 复用现成」的切片；不照搬 VT 基础设施重量。
> 纪律：每小阶段留一个可运行自检 + 跑全量 `pytest`；每大阶段做代码审查 + 对照本计划验收；全部完成前**不 commit**。
> 说明：调研已**纠正**若干原假设（见每项「调研校准」），计划以校准后的事实为准。

---

## 大阶段一 · P0-① 数据完整性纪律

**调研校准**：① 行情源实为 efinance/akshare/pytdx/baostock(A股)+yfinance/akshare_us/finnhub(美股)，tushare/tencent 不产 OHLC；② 唯一「所有源 DataFrame 必经」的单点是 `manager.py:90-97` 的 `return df`，当前零清洗；③ 量能单位：efinance/akshare/pytdx=**手**(100股)，baostock=**股**，US 源=**股**；降级链 efinance→akshare→pytdx→baostock 切到 baostock 时 A股量能突然 ×100（真实 bug，从未处理）；④ 已有 `data_validator.validate_snapshot`(high<low/非正价/负量/NaN) 但只覆盖 price_pipeline 一条路径，不护 DataFrame 消费者。

**改动（最小正确）**：
- 新增 `bottleneck_hunter/data_provider/cleaning.py`：`clean_ohlc(df, source, market) -> pd.DataFrame`：
  - 丢弃 `high<low`、任一 OHLC≤0、`close` 为 NaN 的行；
  - **量能单位归一**：A股市场下，`source in _SHARES_UNIT_SOURCES({'baostock'})` 则 `volume //= 100`，使全部 A股源统一为「手」（canonical=手，贴合 3/4 源+既有落库+境内惯例，改动面最小）；US 不动（全为股）。`ponytail:` 注释标明 canonical 与「新增 股-源加进集合」的升级路径。
- `manager.py`：`fetch_daily` 在 `return df` 前调 `df = clean_ohlc(df, state.fetcher.name, market)`（单点覆盖所有行情源）。
- 新增离线自检脚本/测试：跨源一致性（同 ticker 两源收盘价差 ≤1% 断言的可运行 demo，用构造数据，不打网络）。

**小阶段**：1) cleaning.py + 单测（脏 bar 丢弃 / baostock 手股归一 / US 不动）；2) 接进 manager.py + 回归 fetcher 相关测试；3) 跨源 1% 一致性 self-check。

**验收标准**：baostock A股 df 量能被 ÷100 且与 akshare 同量级；high<low 行被丢；US 源量能不变；`manager.fetch_daily` 仍返回原列名 DataFrame；全量测试绿。

**不做**：不下沉整个 data_provider 清洗框架；不改 `market_snapshots` 表结构；不给 daily 加 source 列（provenance 归 P0-②，且行情级 source 落库属另一切片，YAGNI）。

---

## 大阶段二 · P0-② 决策证据溯源

**调研校准**：① 决策四表(macro/strategic/tactical/execution) **无** provider/model/prompt/snapshot 列，全文塞 `result_json`；② `committee_reviews` 已有 model_provider/model_name；③ VIP `advice_audit_trail`(auth.db) 已含 model+content_hash(sha256)+disclaimer_version+source_data_ref，**唯缺 prompt 哈希**；④ prompt 由 `chain/prompts/*.md` 明文加载，全库无版本/哈希；⑤ 决策各层已在内存拿到 `(provider, model)` 但只喂 budget、不落库。

**改动（最小正确，零表迁移）**：
- 新增 `bottleneck_hunter/watchlist/provenance.py`：
  - `prompt_hash(name) -> str`：读 `chain/prompts/{name}.md` 内容取 sha256[:12]（带 lru_cache，文件变则哈希变）；
  - `build_provenance(*, prompts, models, data_as_of, tickers, extra=None) -> dict`：产出 `{prompt_hashes:{name:hash}, models_used:[...], data_as_of, tickers, generated_at}`（`generated_at` 由调用方传入 UTC，避免脚本内 Date 限制——此处在正常运行时用 `_now_iso`）。
- 在 L1/L2/L3/L4 写库前给 `result` 塞 `result["_provenance"] = build_provenance(...)`（复用各层已有的 provider/model 局部变量、快照日、ticker 集）。**嵌进 result_json，不加列**。
- VIP advisory：给 `advice_audit_trail` 的 content 计算前把 `prompt_hash` 一并纳入 result（补齐其唯一缺口）。

**小阶段**：1) provenance.py + 单测（prompt_hash 稳定/文件变即变、build_provenance 键完整）；2) 接 L1-L4 result_json；3) 接 VIP advisory prompt_hash。

**验收标准**：任取一条 execution_plan.result_json 能读到 `_provenance`（prompt 哈希+实际 model+快照日+ticker 集）；VIP advisory 审计含 prompt_hash；prompt.md 改一字则对应哈希变。全量测试绿。

**不做**：不做 fsync 防篡改链；不加 `prev_hash` 轻链（YAGNI，可复现溯源已达标）；不给四表加列（result_json 嵌入即可，需 SQL 查询再升列）；不建统一 decision_run_id（越界，另立切片）。

---

## 大阶段三 · P0-③ FTS5 全文检索

**调研校准**：① 全库零 FTS；`get_relevant_cards`(store_research.py:242-263) 纯 scope 粗筛、不读 query 文本；② DB 建表在 `store.py:219 _init_db` 跑 CREATE_TABLES+CREATE_INDEXES+MIGRATIONS(每项单语句)+具名 `_migrate_*`；③ FTS5 已实测可用(sqlite 3.49.1)，无中文分词——用 `tokenize='trigram'`；④ 隔离靠 `_filtered` 追加 user_id+market，FTS 命中须带回隔离；⑤ 纯文本表(无需 JSON 抽取)：experience_cards(title+content)、auto_reviews(lessons_learned)、investment_theses(thesis_title+thesis_summary)；meeting_records 是 JSON(transcript_json) 抽取复杂，**本期不做**。

**改动（最小正确）**：
- `store_schema.py` MIGRATIONS 末尾追加 `experience_cards` 的 FTS5：1 个 `CREATE VIRTUAL TABLE IF NOT EXISTS experience_cards_fts USING fts5(title, content, user_id UNINDEXED, market UNINDEXED, content='experience_cards', content_rowid='rowid', tokenize='trigram')` + 3 触发器(ai/ad/au 同步)。（sqlite 无 fts5 时 `_init_db` 吞错降级，不中断——已确认。）
- `store_research.py`：新增 `search_cards(query, *, limit=10) -> list[dict]`（`... _fts MATCH ? ... JOIN 回原表`，经 `_filtered` 隔离，按 rank 排序）；`get_relevant_cards` 保留 scope 结果并 **union** FTS(以 ticker/sector 为 query)命中，去重、confidence 优先。
- 一次性回填：`_migrate_*` 加一个把存量 experience_cards 灌入 fts 的幂等迁移（`INSERT INTO _fts(rowid,title,content,user_id,market) SELECT ...`，仅当 fts 为空）。

**小阶段**：1) schema 虚表+触发器+回填迁移 + 建库自检（插卡→fts 有行）；2) search_cards + get_relevant_cards union + 隔离测试（u1 卡不串给 u2、中文关键词命中）。

**验收标准**：新插经验卡即时可 MATCH；中文关键词(trigram)命中 content；`search_cards` 不跨 user/market；`get_relevant_cards` 既含 scope 命中也含正文关键词命中。全量测试绿。

**不做**：不做 meeting_records/chat_messages 的 JSON 抽取 FTS（下一期）；不引迁移框架；不动连接工厂。

---

## 大阶段四 · P1-④ 投委会同源 + 上游失败阻断（审计加固）

**调研校准**：① 4 persona **已严格同源**(committee.py 构造一次 `context`，L701 同引用传 4 个 `_review_single`，persona 内只读不回取)——「同源」已达成；② **真缺口**：(A) 投委会**无法定人数**——LLM 故障→`abstain`，`_fallback_consensus` abstain 不计 decisive，3弃权+1approve → approve_ratio=1.0≥0.75 → verdict=`approved`(gating 只拦 rejected)；(B) **L3 不校验上游新鲜度**——`get_latest_strategic_plan/macro_strategy` 无本次运行/新鲜度断言，今日 L1/L2 失败则 L3 静默吃旧、产今日计划；空 L2 选股→L3 静默退化全观察池；(C) context 跨 plan 复用可变 dict，`build_ticker_background` 抛异常时残留上一标的估值。

**改动（最小正确，聚焦正确性）**：
- **(A) 法定人数**：`committee.py` `_fallback_consensus`/`run_committee_review` 加「有效(非 error/非 abstain)委员 < `QUORUM_MIN`(=2) → verdict=`needs_review`，理由标注『评审员多数失败，人工复核』」。堵「1人approve即approved」。
- **(C) context 每标的重建**：把跨 plan `.update()` 复用改为每 plan 基于市场级快照浅拷贝再补标的背景；`build_ticker_background` 失败时该标的**明确标注背景缺失**而非沿用上一标的。
- **(B) L3 新鲜度闸**：`run_tactical_plans` 读到 strategic/macro 后校验其 `created_at` 属今日（或带上 L2 已有的 `macro_strategy_id` 归属校验）；不新鲜→发 blocking `decision_error`「上游未刷新，跳过 L3」而非静默沿用；空 L2 选股当降级信号（如实标注「L2 未选股，L3 降级全量」而非静默）。

**小阶段**：1) 法定人数 + 单测（3 error/abstain+1 approve → needs_review）；2) context 每标的重建 + 单测（背景抛异常不串味）；3) L3 新鲜度闸 + 单测（陈旧 strategic → 阻断/标注，不产今日计划）。

**验收标准**：多数评审员失败不再输出 approved；标的背景失败不污染邻标的；L1/L2 陈旧时 L3 显式阻断/降级标注。全量测试绿。

**不做**：不引入正式加权投票/quorum 权重矩阵（YAGNI，`needs_review` 一行闸足够）；不重构编排器为总闸 DAG（先在各层加断言，编排器总闸列为后续）；不碰 chain/cross_validation/fact_check（属供应商发现管线，非投委会链）。

---

## 大阶段五 · P1-⑤ 机构 13F 个股季度环比

**调研校准**：① `institutional_holders` **已能存多期**(UNIQUE(ticker,holder_name,date)+INSERT OR REPLACE，换季新增不删旧)；② **QoQ 逻辑已存在**于 `decision_engine._positioning_signals:1923-1949`(两季共同机构净增减→added/trimmed/flat)，已注入 L1/宏观；③ 缺口在**个股级**：焦点块/`_chip_context` 只吐当期 Top 持有人，无 QoQ；④ gap_note(macro_consultation.py:509)「仅有当期持仓快照,无跨期对比」在个股级仍为真、在聚合级已假（自相矛盾，需改）；⑤ **隐患**：`get_institutional_holders`(store_market_data.py:407) 不按 date 过滤、按 pct_held 降序 → 多期累积后跨季混读/重复计同一机构。

**改动（最小正确，纯搬运+接线）**：
- 抽 `_holder_qoq(store, ticker) -> dict|None`（把 `_positioning_signals` 单 ticker 的两季净增减逻辑抽成助手：方向/幅度 + 增/减仓机构名单）。逻辑现成。
- 注入 `_chip_context`(decision_engine.py:401) → 自动流向焦点块(macro_consultation.py:436)+L3(:1016)。
- 修 gap_note(macro_consultation.py:509)：删「无跨期对比」，改为「13F 增减持方向：已提供近两季环比(见焦点资料)」。
- **修混读隐患**：`get_institutional_holders` 加「仅最新 date」或读后按 holder 去重到最新季（防跨季重复计数）。

**小阶段**：1) `_holder_qoq` 抽取 + 单测（两季 fake 数据 → 正确 added/trimmed 名单与净额）；2) 接 `_chip_context` + 修 gap_note + 修 `get_institutional_holders` 混读 + 焦点块测试。

**验收标准**：焦点块/`_chip_context` 含个股 13F 环比方向与增减仓机构；gap_note 不再自相矛盾；`get_institutional_holders` 多期下不跨季混读。全量测试绿。

**不做**：不加 `report_period` 字段（`date` 已足够区分季）；不换数据源（仍 yfinance）；不重做抓取/调度。

---

## 大阶段六 · P1-⑥ 单通道 IM 推送

**调研校准**：① 无任何面向用户对外通知（邮件仅验证码、Grafana webhook 属运维）；② **唯一挂钩点 = `web/oplog.py:70`** `record_operation` 内 `_broadcaster.publish` 之后——催化剂/regime/决策/VIP 重估/错误全部收敛于此，带 user_id/title/category/detail/market/meta；③ webhook 存 `AUTO_UPDATE_DEFAULTS`(store_budget.py:15) per-user KV，`get/set_auto_update_config` 现成；④ HTTP 用 `retry.get_http_client()`(httpx 共享池)，`asyncio.create_task` fire-and-forget；⑤ 前端 `auto-update.js:29` 加输入框，复用 `PATCH /api/settings/auto-update`；⑥ `record_operation` 是同步 def → 有事件循环时 create_task、否则静默跳过。

**改动（最小正确，不做 16 适配器框架）**：
- `AUTO_UPDATE_DEFAULTS` 加 `"push_webhook_url": ""`、`"push_channel": ""`（channel∈bark/serverchan/feishu）。
- 新增 `bottleneck_hunter/watchlist/push.py`：`build_push_payload(channel, title, body) -> (url_or_body)`（三渠道分支拼 body）；`async push_event(webhook_url, channel, title, body)`（`get_http_client().post`，异常吞掉不影响主流程）。
- `web/oplog.py`：`_broadcaster.publish` 后，按 `category in {auto_update, error}`（可推事件）取该用户 `get_auto_update_config()` 的 webhook，有则 `create_task(push_event(...))`；无 webhook/无循环→静默跳过。
- 后端 `settings_api.py`：`AutoUpdatePatch` 加 `push_webhook_url:str|None`、`push_channel:str|None`；`patch_auto_update` 加 str 分支。
- 前端 `auto-update.js`：阈值输入旁加 `<input id="push-webhook">`+渠道下拉，`change`→`saveUser({push_webhook_url, push_channel})`。

**小阶段**：1) push.py 三渠道 payload + 单测（各渠道 body 结构正确、空 url no-op）；2) AUTO_UPDATE_DEFAULTS+settings_api str 分支 + 测试；3) oplog 挂钩(过滤 category、取 webhook、fire-and-forget) + 测试；4) 前端输入框。

**验收标准**：配置 bark/serverchan/飞书 webhook 后，`record_operation(category=auto_update)` 触发一次对应结构 POST；无 webhook 时零副作用；主流程不因推送失败中断。全量测试绿（推送用 mock，不打真实网络）。

**不做**：不建多渠道适配器抽象/插件框架；不做双向 IM 控制；不接 Telegram/Slack 等墙外渠道；payload 差异用一个函数 if 分支解决。

---

## 全局收尾

- 每大阶段后：代码审查（对照本计划「改动/不做/验收标准」核偏离）+ 全量 `pytest tests/ -q`。
- 全部完成：写 `docs/P0_P1_DEVLOG_2026-08.md` 开发日志（每项实际改动/测试结果/偏差说明）+ 输出任务报告。
- **不 commit**，待用户验收授权。
