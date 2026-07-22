状态：详细技术方案（配套 docs/VIP_ADVISOR_PLAN.md）

# VIP 私人财务 AI 顾问团队 · 详细技术方案

> 本文档把 7 个子系统设计（D1 数据模型 / D2 PDF 摄取 / D3 安全合规 / D4 衍生品 / D5 画像+检索 / D6 决策适配+报告 / D7 咨询聊天）合并为一份连贯、无矛盾、可执行的落地方案。凡子系统间冲突处，均在 §1 显式裁决并在正文统一后落定，不保留两套说法。各子系统的 DDL / 接口 / 算法 / 风险在 §6 完整保留。
>
> **本稿相对初稿的关键修订（评审逐条落实）**：H1 规范层明文的加密叙事矛盾已在 §6.3 显式裁决并升入 §10 风险表首行；H2 `recon_json` 改为**只存 flag 不存金额**；H3 数字白名单校验前移为 P0 公共件，M1 报告强制过校验；H4 画像持仓维度改读 D1 `positions`（不依赖 P5 的 `sim_positions`），三处口径统一；M4 **P3 画像移出 M1**；M1/M2/M3/M5/M6 及 L1–L7 均已落地，见各节。

---

## 0. 复用 vs 新建总表 + 依赖脊柱

### 0.1 复用（零改动或极小改动，禁止另起炉灶）

| 能力 | 复用点 | 锚点 |
|---|---|---|
| AES-256-GCM 加密 PII | `auth/crypto.py` `encrypt/decrypt/make_hint` + 单一 `BH_ENCRYPTION_KEY` | `crypto.py:44/53/62` |
| 建表/迁移/CRUD 范式 | `auth/store.py` executescript + `_migrate`；`store_schema.py` `CREATE_TABLES`+`MIGRATIONS` | `store.py:46/125/410` |
| 多用户+市场隔离三件套 | `_filtered` / `_user_insert_*` / `_market_insert_*` / `.for_user().for_market()` | `store.py:69/80/130-184` |
| LLM 角色→调度→预算→AI 配置 UI | `role_registry._INIT_ROLES` 改一处全链生效 + `get_models_for_role` | `role_registry.py:83`；`factory.py:327/517` |
| 预算门控/计费/降级 | `BudgetTracker.can_spend/record/get_degradation_mode` | `budget.py:36/45/67` |
| SSE 事件与端点 | `decision_engine._sse` + `decision_api._sse_response` + `refresh_guard.guarded` | `decision_engine.py:35`；`decision_api.py:70`；`refresh_guard.py:31` |
| 流式 token（含伪流降级） | `macro_consultation._iter_tokens` | `macro_consultation.py:163` |
| 决策引擎唯一持仓契约 | `get_sim_account()` / `get_sim_positions()`；`run_daily_decision`（只出 pending，不下单） | `store_simtrading.py:11/64`；`decision_engine.py:1526` |
| 现价 / K 线 / 年化波动率 | `FetcherManager.fetch_realtime` / `fetch_kline` / `PositionSizer.compute_stock_volatility` | `manager.py:113`；`financial_data.py:607`；`position_sizing.py:131` |
| 取数限速纪律 | `yf_gate.throttle()/observe()` | `financial_data.py:361` |
| 知识检索 | `get_relevant_cards`（P4 升级向量后自动受益） | `store_research.py:196` |
| 软约束注入 L1-L4 | `persona.py`（画像 summary 走同一通道，零新代码） | CLAUDE.md 载明 |
| 审计广播 | `oplog.record_operation` | `oplog.py:60/77` |
| 删号唯一级联点 | `admin_api.py` `store.delete_user` 之前 | `admin_api.py:213` |
| 定时任务五处接线 | scheduler `_JOB_SPECS` / `schedule_config` / `list_job_categories` / `list_job_labels` | `scheduler.py:984/1033/1057` |
| Markdown 报告 append-lines 风格 | `chain/report.py` `_report_zh` | `report.py:20` |

### 0.2 新建（仅此清单，其余一律复用）

**依赖**：`PyMuPDF (fitz)` 一个（文本层抽取 + 扫描页渲染图）。P4 后置：`fastembed` + `sqlite-vec`（M1 不引）。

**新增表（8 张 + 2 张 P4 后置）**：见 §2。（`user_profiles` 由 D5-P3 提供，**已移出 M1**，见 §7。）
**新增文件/模块**：见 §4。
**新增公共件**：`vip/number_guard.py` —— **数字白名单校验**从 D7 聊天提炼为 P0 公共件，报告（P5，M1 内）与聊天（P6）共用（裁决见 §1 C12、H3 修正）。
**新增 LLM 角色（2 个）**：`vip_statement_extract`、`vip_chat`。见 §9。

### 0.3 依赖脊柱（构建顺序即里程碑顺序）

```
P0 安全合规底座 (D3) + 数字白名单公共件
   │  financial_documents / advice_audit_trail / require_vip / compliance
   │  number_guard.verify_numbers / 删号级联
   ▼
P1 PDF 摄取管道 (D2)
   │  PyMuPDF(魔数+炸弹防护) → llm_extract → 语句内对账 → 加密落 financial_documents
   │  上传/纠错端点：返回 parsed_ok 即触发 normalize（M1 修正）
   ▼
P2 规范数据模型 (D1)         ── P2b 衍生品 (D4)  [后置 M1]
   │  instruments/positions/transactions  ← normalize_statement 桥接
   │  reconcile_positions 交叉质检          pricing.py 纯函数 + positions 加 greeks 列
   ▼
P5 决策适配 + 报告推荐 (D6)   ── P3 用户画像蒸馏 (D5-基础)  [后置 M1]
   │  materialize_portfolio → sim_*         user_profiles ← 读 D1 positions/transactions
   │  run_daily_decision → generate_vip_report（报告叙事强制过 number_guard）
   │                                        ── P4 语义检索 (D5-向量) [后置 M1]
   ▼
P6 实时咨询聊天 (D7)  [后置 M1]
      chat_sessions/chat_messages ← stream_vip_chat（复刻 macro_consultation，复用 number_guard）
```

**M1 首里程碑 = P0 + P1(单券商股票) + P2(无衍生品) + P5(一份报告)。** 灰底部分（P2b/P3/P4/P6）不进 M1。**画像 P3 移出 M1 的理由**：画像是软约束叙事，决策引擎缺 `profile.summary` 照常运行（persona 是软注入），它与 M1 两大核心风险（抽取准确率、能否喂决策）正交，不验证任何 M1 成败项（M4 修正）。

---

## 1. 子系统冲突消解裁决表（本文档的核心：消除矛盾）

| # | 冲突 | 两方主张 | **裁决（统一后）** |
|---|---|---|---|
| C1 | `financial_documents` 双 DDL | D2：`content_hash`/`period_end`/`recon_json`/status(parsed_ok…)；D3：`file_sha256`/`period`/`doc_type`/status(uploaded…) | **合并为一张表**（§2.1）：去重列统一叫 `content_hash`；期次统一 `period_end`；保留 D2 的 `recon_flags_json`（**仅 flag，无金额**，H2 修正）+ D3 的 `doc_type/parse_error/purged_at`；status 枚举合并为 `needs_review/parsed_ok/extract_failed/duplicate/normalized/purged`；`UNIQUE(user_id, content_hash)`。落 **auth.db**（PII 加密）。 |
| C2 | 规范持仓 vs sim 投影 | D1：`instruments/positions/transactions`(watchlist)；D6：物化进 `sim_account/sim_positions` | **两层不矛盾，串成管道**：D1 为规范真值层，D6 为决策投影层。`normalize_statement`（P1→P2）写 D1 表；`materialize_portfolio`（P2→P5）投影 D1 最新快照 → sim_*。见 §5。 |
| C3 | 对账重复 | D1 `reconcile_positions`(规范表交叉)；D2 `reconcile`(内存 BrokerStatement 语句内) | **分属两阶段，均保留**：D2 内存对账是 **P1 摄取门禁**（拦截错抽，决定 `parsed_ok`）；D1 `reconcile_positions` 是 **P2 规范化后**的跨期交叉质检（流水推导 vs 快照）。D2 gate 在前、D1 为规范层权威复核。 |
| C4 | 衍生品存储 | D1：`instruments`(带 option 列)+`positions`；D4：独立 `derivative_positions` 表 | **消除重复表 → 采用 D1 规范模型**。`instruments.instrument_type∈(option/future/structured)`，用 D1 已有 `underlying/option_type/strike/expiry/multiplier/extra` 列；持仓走 `positions`。**D4 的 `derivative_positions` 表废弃**；缓存 greeks/定价输入列以 **P2b `ALTER positions ADD COLUMN`** 补入（§2.3）；结构化产品条款存 `instruments.extra`。**D4 纯函数 `pricing.py` 完整保留**（§6.4）。 |
| C5 | greeks/BS 模块重复 | D4 `watchlist/derivatives/pricing.py`；D7 `watchlist/greeks.py` | **合并为一个** `watchlist/derivatives/pricing.py`（D4 更完整）。D7 聊天 `compute_greeks` 工具调用它。**D7 的 `greeks.py` 废弃**。 |
| C6 | `vip_chat` 角色双定义 | D5 与 D7 各定义一次 | **合并为一个** `vip_chat`（group=`vip`, `multi_model=False`, `min_context=_HEAVY`, `capability_weights=_DECISION_WEIGHTS`）。§9。 |
| C7 | 画像输入源命名 | D5 引用 `vip_trades`/`list_vip_trades` | **不存在该表**。统一为 D1 的 `transactions` + `list_transactions`；持仓维度读 D1 `positions`（**不读 `sim_positions`**，H4 修正）。 |
| C8 | 免责声明双实现 | D3 `vip/compliance.py`；D7 聊天内自带 | **单一真源** `vip/compliance.py`（D3）。D7 聊天注入与 SSE `disclaimer` 事件调用 `with_disclaimer`/`DISCLAIMER_*`。 |
| C9 | VIP API 文件 | D2 `vip/vip_api.py`；D7 `decision_api.py` 或新文件 | **统一** `web/vip_api.py`，承载全部 VIP 端点（上传/纠错/报告/聊天/删除），复用 `_user_store`/`_user_budget`/`_sse_response`。 |
| C10 | 删号级联多处各写 | 每个子系统各插一个 `delete_all_user_*` | **统一一段级联块**（§8），插在 `admin_api.py:213` `store.delete_user` 之前，按 auth.db / watchlist.db 两库、FK 逆序执行。 |
| C11 | 建议溯源跨库引用 | D3 `advice_audit_trail.source_doc_ids` → auth；`source_data_ref` → watchlist sim | **软引用 TEXT，无跨库 FK**。溯源链：`advice_audit_trail → financial_documents.id`（文件）；**数据锚改指向不可变的 `vip_reports.id`**（其 `payload_json` 冻结当期快照，M2 修正），不再指向会被覆盖的 sim 单例槽。 |
| C12 | 数字幻觉防护的归属 | D7 聊天自带白名单校验；报告（D6）无 | **提炼为 P0 公共件** `vip/number_guard.py`（H3 修正）。报告（P5，M1 内）与聊天（P6）**共用同一校验函数**，facts 分别取 materialize 后的 sim 快照 / 聊天 facts block。 |

---

## 2. 统一数据模型与 DDL

### 2.0 全局约定（所有新表遵守）

- 主键 `id = uuid.uuid4().hex[:12]`；时间列 `TEXT` 存 UTC ISO（`_now_iso()`），展示 `fmtBJ`；JSON 列 `TEXT DEFAULT '{}'`；枚举内联 `CHECK`。
- 隔离列 `user_id TEXT DEFAULT ''` + `market TEXT DEFAULT 'us_stock'`（auth.db 表 `user_id TEXT NOT NULL`）。
- 每张新表在 **`CREATE_TABLES`（新库建全）** 与 **`MIGRATIONS` 尾部同串 `CREATE TABLE IF NOT EXISTS`（老库补建）** 各放一份；`_init_db` try/except 吞 `already exists` 幂等。加列一律走 `MIGRATIONS` 尾 `ALTER TABLE ... ADD COLUMN ... DEFAULT`，不改基表定义。
- **PII 密文/明文边界（H1 裁决落地）**：完整资产负债 PII 的**密文**只落 auth.db（`parsed_json_encrypted`）；watchlist.db 规范表（`instruments/positions/transactions`）存**明文**（SQL 聚合刚需，见 §6.1/§6.3），其安全边界为库文件 OS 级权限保护 + 备份加密，**不出宿主机、不入日志**。此为显式取舍，已列为 §10 风险表首行；升级路径 = SQLCipher 文件级加密（不改任何 SQL）。

### 2.1 auth.db 表（PII，加密）— 落 `auth/store.py`

**统一后的 `financial_documents`（合并 D2+D3，裁决 C1；H2/L6 修正）**

```sql
CREATE TABLE IF NOT EXISTS financial_documents (
    id                    TEXT PRIMARY KEY,          -- uuid4().hex[:12]
    user_id               TEXT NOT NULL,
    market                TEXT DEFAULT 'us_stock',
    broker                TEXT DEFAULT '',
    doc_type              TEXT DEFAULT 'monthly_statement'
                          CHECK(doc_type IN ('monthly_statement','trade_confirm','position_report')),
    period_end            TEXT DEFAULT '',           -- 结单期末日 ISO，跨月去重/排序/取 prev
    file_name             TEXT DEFAULT '',           -- 仅内部匹配，非展示
    file_hint             TEXT DEFAULT '',           -- make_hint(原名)，展示用
    content_hash          TEXT NOT NULL,             -- sha256(pdf)，幂等键（即原 file_sha256）
    raw_pdf_encrypted     TEXT DEFAULT '',           -- 默认空(即焚)；短存时 encrypt(base64(pdf))
    parsed_json_encrypted TEXT DEFAULT '',           -- encrypt(BrokerStatement.model_dump_json())
    recon_flags_json      TEXT DEFAULT '{}',          -- 仅 flag/字段名/状态枚举，绝无金额数值（H2）
    status                TEXT DEFAULT 'needs_review'
                          CHECK(status IN ('needs_review','parsed_ok','extract_failed',
                                           'duplicate','normalized','purged')),
    parse_error           TEXT DEFAULT '',
    created_at            TEXT NOT NULL,
    parsed_at             TEXT DEFAULT '',
    purged_at             TEXT DEFAULT '',
    updated_at            TEXT DEFAULT '',
    UNIQUE(user_id, content_hash)                    -- 内联 UNIQUE 已生成索引，勿再单建（L6）
);
CREATE INDEX IF NOT EXISTS idx_findoc_user_period ON financial_documents(user_id, market, broker, period_end);
```

> **H2 修正**：`recon_flags_json` 只存**布尔 flag + 字段名 + 状态枚举**，例如
> `{"pos_shares":"ok","portfolio_equity":"fail","cash_flow":"needs_review","missing_fields":["dividend"]}`。
> 前端 needs_review 队列仅需 `status` 列 + flag 名即可筛选、着色，**不需要任何金额数值**；金额 PII 全部留在 `parsed_json_encrypted` 密文里。
>
> status 语义：`needs_review`(对账有 hard flag/缺字段) → `parsed_ok`(对账通过) → `normalized`(已由 normalize_statement 写入 D1 规范表) → `purged`(原始 PDF 焚毁)。`extract_failed`/`duplicate` 为终态。

**`advice_audit_trail`（D3；M2/M3 修正）**

```sql
CREATE TABLE IF NOT EXISTS advice_audit_trail (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT DEFAULT '',
    market              TEXT DEFAULT 'us_stock',
    advice_type         TEXT DEFAULT 'report'
                        CHECK(advice_type IN ('report','recommendation','chat','correction')),
    advice_ref          TEXT DEFAULT '',          -- report_id / decision_run_id / chat_msg_id / doc_id（软引用）
    source_doc_ids      TEXT DEFAULT '[]',         -- JSON: [financial_documents.id ...]（文件溯源）
    source_data_ref     TEXT DEFAULT '{}',         -- JSON（数据溯源，见下）
    model_provider      TEXT DEFAULT '',
    model_name          TEXT DEFAULT '',
    disclaimer_version  TEXT DEFAULT '',
    content_hash        TEXT DEFAULT '',           -- sha256(建议正文)，防篡改
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user_ref ON advice_audit_trail(user_id, advice_ref);
```

> **M2 修正（数据溯源锚指向不可变对象）**：`source_data_ref` 对 `advice_type∈(report,recommendation,chat)` 记
> `{"report_snapshot_id": vip_reports.id, "tickers":[...]}`；该 `vip_reports.id`（含 `kind='import_snapshot'` 或 `'periodic'`）的 `payload_json` **冻结了当期 sim 快照**，单例槽被下次导入覆盖后审计仍可复现。绝不再写"当时不存在稳定 id"的 `account_snapshot_id`。
>
> **M3 修正（纠错留痕）**：`apply_correction` 每次改动落一条 `advice_type='correction'` 行——`advice_ref=doc_id`，`source_data_ref={"field_path":"positions[3].quantity","old_hash":sha256(旧值),"new_hash":sha256(新值),"operator":user_sub}`，`content_hash=sha256(patch)`。**只记字段路径与哈希，不记明文金额**。这是财务系统人工篡改的唯一可追溯留痕，不可省。

### 2.2 watchlist.db — 规范组合模型（D1，裁决 C2/C4）— 落 `store_schema.py`

**`instruments`（工具主数据；衍生品字段就位，M1 不填）**

```sql
CREATE TABLE IF NOT EXISTS instruments (
    id              TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,                       -- normalize_ticker 后（期权用 OCC 串）
    instrument_type TEXT NOT NULL DEFAULT 'stock'
                    CHECK(instrument_type IN
                        ('stock','etf','fund','bond','cash','option','future','structured')),
    name            TEXT DEFAULT '',
    currency        TEXT NOT NULL DEFAULT 'USD',
    exchange        TEXT DEFAULT '',
    isin            TEXT DEFAULT '',                     -- 跨券商对齐锚（M1 单券商暂不用）
    underlying      TEXT DEFAULT '',                     -- 衍生品标的 symbol
    option_type     TEXT DEFAULT '' CHECK(option_type IN ('','call','put')),
    strike          REAL DEFAULT 0,
    expiry          TEXT DEFAULT '',
    multiplier      REAL DEFAULT 1,                      -- 期权美股100/A股ETF按合约解析，勿写死
    extra           TEXT DEFAULT '{}',                   -- 结构化产品条款(D4 terms_json)/类型专属 JSON
    source_doc_id   TEXT DEFAULT '',                     -- 软引用 financial_documents.id（跨库无 FK）
    source_page     INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    created_at      TEXT NOT NULL,
    updated_at      TEXT DEFAULT '',
    user_id         TEXT DEFAULT '',
    market          TEXT DEFAULT 'us_stock',
    UNIQUE(user_id, market, symbol, instrument_type, expiry, strike, option_type)
);
```

**`positions`（按结单日 append 的快照时间序列，多币种）**

```sql
CREATE TABLE IF NOT EXISTS positions (
    id                TEXT PRIMARY KEY,
    instrument_id     TEXT NOT NULL,
    account_ref       TEXT DEFAULT '',                   -- 券商账户号（脱敏）
    as_of_date        TEXT NOT NULL,                     -- 结单/快照日 ISO date
    quantity          REAL NOT NULL DEFAULT 0,           -- 空头为负
    avg_cost          REAL DEFAULT 0,
    currency          TEXT NOT NULL DEFAULT 'USD',
    market_price      REAL DEFAULT 0,
    market_value      REAL DEFAULT 0,                    -- quantity*price*multiplier（工具币种，入库算好）
    fx_rate           REAL DEFAULT 1.0,                  -- 工具币种→基币
    market_value_base REAL DEFAULT 0,                    -- 统一基币口径（组合占比必用此，勿混币加总）
    cost_basis        REAL DEFAULT 0,
    unrealized_pnl    REAL DEFAULT 0,
    weight_pct        REAL DEFAULT 0,                    -- 引擎直接读，入库算好
    source_doc_id     TEXT DEFAULT '',
    source_page       INTEGER DEFAULT 0,
    confidence        REAL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    created_at        TEXT NOT NULL,
    user_id           TEXT DEFAULT '',
    market            TEXT DEFAULT 'us_stock',
    UNIQUE(user_id, market, account_ref, instrument_id, as_of_date),
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE CASCADE
);
```

> 硬坑（decision-engine）：`market_value/unrealized_pnl/weight_pct` 引擎**读取时不重算**，入库前必须算好；多币种占比用 `market_value_base`。

**`transactions`（流水，幂等去重；M6/L3 修正）**

```sql
CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,
    instrument_id TEXT DEFAULT '',                       -- 现金类可空
    account_ref   TEXT DEFAULT '',
    txn_type      TEXT NOT NULL
                  CHECK(txn_type IN
                    ('buy','sell','dividend','interest','fee','tax',
                     'deposit','withdrawal','split','transfer_in','transfer_out',
                     'opt_exercise','opt_assign','opt_expire')),
    trade_date    TEXT NOT NULL,
    settle_date   TEXT DEFAULT '',
    quantity      REAL DEFAULT 0,                        -- 买正卖负；split 存"增量股数"(见下)
    price         REAL DEFAULT 0,
    gross_amount  REAL DEFAULT 0,
    fee           REAL DEFAULT 0,
    tax           REAL DEFAULT 0,
    net_amount    REAL DEFAULT 0,                        -- 对现金有向净影响（含费税）
    currency      TEXT NOT NULL DEFAULT 'USD',
    fx_rate       REAL DEFAULT 1.0,
    external_id   TEXT DEFAULT '',                       -- 券商流水号（去重）
    description   TEXT DEFAULT '',                       -- 结单原始行文本/拆股比例（审计+兜底去重）
    source_doc_id TEXT DEFAULT '',
    source_page   INTEGER DEFAULT 0,
    confidence    REAL DEFAULT 1.0 CHECK(confidence BETWEEN 0 AND 1),
    created_at    TEXT NOT NULL,
    user_id       TEXT DEFAULT '',
    market        TEXT DEFAULT 'us_stock',
    UNIQUE(user_id, market, account_ref, external_id),
    FOREIGN KEY(instrument_id) REFERENCES instruments(id) ON DELETE SET NULL
);
```

> **M6 修正（split 存储约定，写死并进自检）**：`txn_type='split'` 行的 `quantity` 一律存**增量股数**（正常并入 `Σquantity` 加法语义；1 拆 2 持 100 股 → 记 `quantity=+100`），拆股比例文本放 `description`（如 `"2:1 split"`）。`reconcile_positions` 的 `derived_qty=Σquantity` 因此**不需要任何"按比例"乘法分支**（§6.1 已改）。自检：`buy100 + split(+100) → derived_qty=200`。
>
> **L3 修正（空 external_id 兜底去重键）**：`external_id` 为空时改按 `(account_ref,trade_date,txn_type,instrument_id,gross_amount, description)` 探重（**加 description 区分同日同价的两笔真实成交**）。命中记为"疑似重复"——`create_transaction` 返回 `None` 但同时在 `recon_flags_json` 挂 `suspected_dup` flag 交对账环节裁，**不静默丢弃**。`# ponytail:` 启发式，券商给流水号后该分支自然退役。

**索引（进 `MIGRATIONS` 尾）**

```sql
CREATE INDEX IF NOT EXISTS idx_instruments_symbol   ON instruments(user_id, market, symbol);
CREATE INDEX IF NOT EXISTS idx_positions_asof       ON positions(user_id, market, account_ref, as_of_date);
CREATE INDEX IF NOT EXISTS idx_positions_instrument ON positions(instrument_id);
CREATE INDEX IF NOT EXISTS idx_transactions_date    ON transactions(user_id, market, account_ref, trade_date);
CREATE INDEX IF NOT EXISTS idx_transactions_instr   ON transactions(instrument_id, trade_date);
```

### 2.3 watchlist.db — P2b 衍生品扩展（裁决 C4，后置 M1）

不新建表。`instruments` 衍生品列已就位；持仓走 `positions`。P2b 只对 `positions` 追加缓存列（`ALTER`，进 `MIGRATIONS` 尾）：

```sql
-- ── P2b: positions 衍生品定价缓存（引擎不重算，job_reprice_derivatives 定时算好，见 L4 修正） ──
ALTER TABLE positions ADD COLUMN spot           REAL DEFAULT 0;   -- 定价用标的现价 S
ALTER TABLE positions ADD COLUMN iv             REAL DEFAULT 0;   -- 隐含/历史波动率 σ
ALTER TABLE positions ADD COLUMN risk_free_rate REAL DEFAULT 0;  -- 定价用 r
ALTER TABLE positions ADD COLUMN delta REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN gamma REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN vega  REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN theta REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN rho   REAL DEFAULT 0;
ALTER TABLE positions ADD COLUMN priced_at TEXT DEFAULT '';
```

> 结构化产品：`instruments.instrument_type='structured'`，条款存 `instruments.extra`（= 原 D4 `terms_json`）；`market_value` 直取月结单 mark-to-market；不做 MC 定价。
>
> **L4 修正（缓存刷新有主）**：P2b 落地时**必须同步落 `job_reprice_derivatives`**（interval，复用 `fetch_realtime` + `pricing.py` 重算这些缓存列），照 `job_profile_distill` 的 5 处接线（`_JOB_SPECS`/`schedule_config`/`list_job_categories→vip_derivatives`/`list_job_labels`）。缓存列与刷新 job 成对交付，无 job 则改为查询时算——不留 stale 无主缓存。

### 2.4 watchlist.db — 画像（D5，**P3，后置 M1**）

```sql
CREATE TABLE IF NOT EXISTS user_profiles (
    id           TEXT PRIMARY KEY,
    metrics_json TEXT NOT NULL DEFAULT '{}',   -- holding/style/risk/pnl 分面合一（L5：分面查询需要时再拆）
    summary      TEXT NOT NULL DEFAULT '',     -- 紧凑中文段落，注入 prompt（≤180 tokens）
    confidence   INTEGER NOT NULL DEFAULT 5 CHECK(confidence BETWEEN 1 AND 10),
    sample_count INTEGER NOT NULL DEFAULT 0,   -- EWMA 有效样本
    watermark_at TEXT DEFAULT '',              -- 增量水位（已消费到的最新成交时间 UTC）
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT DEFAULT '',
    user_id      TEXT DEFAULT '',
    market       TEXT DEFAULT 'us_stock',
    UNIQUE(user_id, market)
);
CREATE INDEX IF NOT EXISTS idx_user_profiles_user_market ON user_profiles(user_id, market);
```

> **L5 修正**：M1 后只有 `summary` 经 persona 注入，`holding/style/risk/pnl` 四分面合并为单列 `metrics_json`（YAGNI，真需分面查询时再拆）。`confidence/sample_count/watermark_at/version` 有增量融合用途，保留。

### 2.5 watchlist.db — 报告（D6，P5）

```sql
CREATE TABLE IF NOT EXISTS vip_reports (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL DEFAULT 'periodic'
                 CHECK(kind IN ('periodic','alert','import_snapshot')),
    period       TEXT DEFAULT '',              -- 报告期标签
    report_md    TEXT DEFAULT '',              -- 报告正文 Markdown
    payload_json TEXT DEFAULT '{}',            -- {pending, consensus, ...} 或 import 归档快照（溯源锚，M2）
    alert_key    TEXT DEFAULT '',              -- (ticker+触发类型) 去抖键
    created_at   TEXT NOT NULL,
    user_id      TEXT DEFAULT '',
    market       TEXT DEFAULT 'us_stock'
);
CREATE INDEX IF NOT EXISTS idx_vip_reports_user ON vip_reports(user_id, market, kind, created_at);
```

> `kind='import_snapshot'` 用于导入前归档旧 `sim_account+positions`（裁决 C2 的 sim 单例槽兜底 + M2 溯源锚）；`kind='alert'` 去抖靠 `alert_key`+`created_at` 查最近 24h。`payload_json` 即 M2 的不可变数据溯源锚，`advice_audit_trail.source_data_ref.report_snapshot_id` 指向本表 `id`。

### 2.6 watchlist.db — 聊天（D7，P6，后置 M1）

```sql
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY, title TEXT DEFAULT '', summary TEXT DEFAULT '',
    summarized_upto TEXT DEFAULT '', msg_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active' CHECK(status IN ('active','archived')),
    created_at TEXT NOT NULL, updated_at TEXT DEFAULT '',
    user_id TEXT DEFAULT '', market TEXT DEFAULT 'us_stock'
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user','assistant','tool')),
    content TEXT DEFAULT '', tool_calls TEXT DEFAULT '[]', tool_name TEXT DEFAULT '',
    provider TEXT DEFAULT '', model TEXT DEFAULT '',
    in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, fail_reason TEXT DEFAULT '',
    created_at TEXT NOT NULL, user_id TEXT DEFAULT '', market TEXT DEFAULT 'us_stock',
    FOREIGN KEY(session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_user_market ON chat_sessions(user_id, market, updated_at);
CREATE INDEX IF NOT EXISTS idx_chat_messages_session     ON chat_messages(session_id, created_at);
```

### 2.7 watchlist.db — 语义检索（D5，P4，后置 M1）

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vec_knowledge USING vec0(
    embedding float[512], user_id TEXT partition key, market TEXT, +chunk_id TEXT);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL CHECK(source_type IN ('trade','report','chat','card','thesis')),
    source_id TEXT NOT NULL DEFAULT '', text_snippet TEXT NOT NULL,
    embedded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    user_id TEXT DEFAULT '', market TEXT DEFAULT 'us_stock'
);
```

---

## 3. 统一数据库迁移顺序

两库分别追加，均用 `# ── VIP Phase Pn: ... ──` 起头，顺序即历史。**FK 依赖决定表内顺序**（被引用表先建）。

### 3.1 auth.db（`auth/store.py` executescript + `_migrate` PRAGMA/ALTER 幂等补列）

```
[P0]  1) CREATE financial_documents   +1 索引(period)   -- 内联 UNIQUE 自带 hash 索引，勿再单建(L6)
      2) CREATE advice_audit_trail     +1 索引
```

### 3.2 watchlist.db（`store_schema.py` `CREATE_TABLES` + `MIGRATIONS` 尾）

```
[P2]  1) CREATE instruments                    (被 2/3 引用，先建)
      2) CREATE positions        (FK instruments)
      3) CREATE transactions     (FK instruments)
      4) 5 条 CREATE INDEX
[P5]  5) CREATE vip_reports                      + 1 索引
── 以下后置 M1 ──
[P3]  6) CREATE user_profiles                   + 1 索引
[P6]  7) CREATE chat_sessions
      8) CREATE chat_messages    (FK chat_sessions)  + 2 索引
[P2b] 9) ALTER positions ADD (spot/iv/r/greeks×5/priced_at)
[P4] 10) CREATE VIRTUAL vec_knowledge；CREATE knowledge_chunks
```

> 迁移纪律：全部 `CREATE TABLE IF NOT EXISTS` / `ALTER ... ADD COLUMN`，`_init_db` 吞 `already exists`，无手写重建函数（无改 PK/UNIQUE 需求）。`sqlite-vec` 扩展加载点 `store._connect`（`store.py:187`）：`enable_load_extension(True); vec.load(conn)`，`try/except` 兜底失败降级（P4）。**M1 迁移只到第 5 步（vip_reports）**；P3 起顺延为 M1 后首个增量。

---

## 4. 新增文件 / 模块清单

| 路径 | 内容 | 阶段 |
|---|---|---|
| `bottleneck_hunter/vip/number_guard.py` | `verify_numbers(text, facts)→list[{token,status}]`：正则抽 `$数字/数字%`，逐个在 facts 子串/近似匹配，未命中标 `unverified`。**报告与聊天共用**（裁决 C12，H3）+ `demo()` 自检 | **P0** |
| `bottleneck_hunter/vip/statement_models.py` | Pydantic：`Provenance/StatementPosition/StatementTrade/CashFlow/StatementTotals/BrokerStatement`（Decimal，禁 float） | P1 |
| `bottleneck_hunter/vip/ingest.py` | `IngestStatus/IngestResult`、`ingest_statement`（编排）、`sniff_pdf`（魔数+炸弹防护）、`extract_text_layer`/`render_page_png`、`llm_extract`、`dedup_trades`、`apply_correction`（含 correction 审计） | P1 |
| `bottleneck_hunter/vip/reconcile.py` | 语句内对账 `reconcile(stmt,prev)→ReconReport`（4 勾稽，产出仅 flag 的 recon_flags）+ `__main__` 自检 | P1 |
| `bottleneck_hunter/vip/compliance.py` | `DISCLAIMER_VERSION/DISCLAIMER_ZH`、`with_disclaimer`（唯一免责真源，裁决 C8） | P0 |
| `bottleneck_hunter/vip/normalize.py` | `normalize_statement(auth_store, wl_store, doc)`：解密 parsed JSON → 写 D1 instruments/positions/transactions（P1→P2 桥接） | P2 |
| `bottleneck_hunter/watchlist/store_portfolio.py` | `_PortfolioMixin`：`upsert_instrument/find_instrument/list_instruments`、`save_position_snapshot/get_positions/list_position_dates`、`create_transaction/list_transactions`、`reconcile_positions`、`delete_all_user_portfolio`（命名不强制前缀，见 §6.1 L1 修正） | P2 |
| `bottleneck_hunter/web/vip_api.py` | 全部 VIP 端点（上传/纠错/报告/删除/**抽取质量只读统计** L7），复用 `_user_store/_user_budget/_sse_response`；上传/纠错返回 `parsed_ok` 即触发 `normalize_statement`（M1 修正，裁决 C9） | P1+ |
| `bottleneck_hunter/watchlist/vip_adapter.py` | `HoldingIn/AccountMeta`、`materialize_portfolio`（→sim_*）、`generate_vip_report`（叙事过 number_guard）、`_render_vip_report_zh` + `demo()` | P5 |
| `bottleneck_hunter/watchlist/store_memory.py` | `_MemoryMixin`：`get/upsert_user_profile`、`delete_user_profile`；（P4）`enqueue_knowledge_chunk/…/semantic_search/delete_knowledge` | **P3**/P4 |
| `bottleneck_hunter/watchlist/profile_distill.py` | `distill_profile`（读 D1 `positions`+`transactions`，确定性聚合+EWMA+衰减，LLM 仅叙事）+ `demo()` | **P3** |
| `bottleneck_hunter/watchlist/embedding.py` | fastembed 懒单例 `embed()` | P4 |
| `bottleneck_hunter/watchlist/derivatives/pricing.py` | 纯函数 BS/greeks/IV/`portfolio_greeks`/`stress_test`/`structured_cashflow_schedule` + `demo()`（裁决 C4/C5，唯一定价件） | P2b |
| `bottleneck_hunter/watchlist/store_chat.py` | `_ChatMixin`：会话/消息 CRUD + 摘要 + `delete_all_user_chat` | P6 |
| `bottleneck_hunter/watchlist/vip_chat.py` | `stream_vip_chat`（复刻 macro_consultation）+ 调 `number_guard.verify_numbers` | P6 |

**改动既有文件**：`auth/store.py`（+2 表 + `_FinancialMixin`）、`auth/dependencies.py`（+`require_vip`）、`web/admin_api.py`（`:213` 前插统一级联）、`web/oplog.py`（`_ACTION_MAP` 登记 VIP 动作）、`store_schema.py`（+表/迁移）、`store.py`（import + 基类元组挂 Mixin）、`role_registry.py`（+2 角色）、`scheduler.py`+`schedule_config.py`（P3 起画像蒸馏 job 五处接线，M1 不接）、`decision_engine.py`（P5 组合风险摘要注入衍生品段）、`options_pipeline.py:31`（P2b 扩 IV）、`pyproject.toml`（+PyMuPDF）、前端 AI 配置页（认 `vip` 组）。

---

## 5. 跨子系统接口契约（数据流脊柱）

### 5.1 端到端数据流

```
PDF bytes
  │ D2 ingest_statement(store,budget,uid,market,pdf,filename)
  │  ① sniff_pdf: %PDF 魔数 + 大小≤20MB + 声明页数/大小比 → 拒炸弹(M5)
  ▼
BrokerStatement (Pydantic, Decimal) + ReconReport(仅 flag)
  │  ├─ reconcile(stmt, prev)  → hard_ok? parsed_ok : needs_review     [P1 门禁]
  │  └─ encrypt → auth.financial_documents(parsed_json_encrypted, recon_flags_json, status)
  ▼
【触发点，M1 修正】vip_api 上传/纠错端点：status==parsed_ok ⇒ 同步(或投 job)调 normalize_statement
  ▼
D2→D1 normalize_statement(auth_store, wl_store, doc)   [仅 status=parsed_ok 触发]
  │  解密 parsed JSON → upsert_instrument / save_position_snapshot / create_transaction
  │  写 source_doc_id = financial_documents.id（跨库软引用）
  │  financial_documents.status → 'normalized'
  ▼
watchlist: instruments / positions / transactions        [P2 规范真值层，明文，OS 级保护(H1)]
  │  └─ D1 reconcile_positions(as_of_date, account_ref) → 差异列表  [P2 交叉质检]
  ▼
D1→D6 materialize_portfolio(store, holdings, account_meta)  [P2→P5 投影]
  │  materialize 前：旧 sim → vip_reports(kind='import_snapshot') 冻结（M2 溯源锚 + C2 兜底）
  │  positions 最新快照 → sim_positions；账户汇总 → sim_account
  │  隐性前置：每 ticker 建 watchlist entry + 价格快照
  ▼
sim_account / sim_positions   [决策引擎唯一契约面]
  │  run_daily_decision(store,budget,scope,market)  → L1-L4 + 投委会（只出 pending，不下单）
  ▼
D6 generate_vip_report → 读回 macro/plan/tacts/pending/committee → _render_vip_report_zh
  │  ★ 报告 LLM 叙事段(L1/L4)过 number_guard.verify_numbers(facts=sim 快照)，未命中标"⚠未核到"(H3)
  │  → vip_reports(kind='periodic') + advice_audit_trail(source_data_ref.report_snapshot_id, disclaimer_version)
  ▼
── 后置 M1 ──
D5 distill_profile(store,budget) 读 transactions + positions(非 sim_positions，H4) → user_profiles
  │  画像 summary 经 persona 通道注入 L2/L4/投委会（软约束）
D7 stream_vip_chat  [P6]：facts=get_sim_account/positions + user_profiles.summary
                    + get_relevant_cards(P4 后为向量检索) → 流式回答 + number_guard 校验
```

### 5.2 关键接口契约（签名冻结点）

| 边界 | 契约 | 生产方 | 消费方 |
|---|---|---|---|
| 摄取→存储 | `ingest_statement(...)→IngestResult{doc_id,status,statement,recon_flags}` | D2 | vip_api / normalize |
| **触发（M1 修正）** | `on_parsed_ok(doc_id)`：ingest/apply_correction 返回 `parsed_ok` ⇒ 端点调 `normalize_statement`；契约新增 `parsed_ok → normalize` 边 | vip_api | normalize |
| 存储→规范 | `normalize_statement(auth_store,wl_store,doc)→{n_instruments,n_positions,n_txns}` | D2/D1 桥 | D1 store |
| 规范→质检 | `reconcile_positions(as_of_date,account_ref)→list[{symbol,snapshot_qty,derived_qty,qty_diff,status}]` | D1 | 前端/质检 |
| 规范→画像 | `list_transactions(since=watermark)` + `get_positions()`（**不读 sim_positions**，H4） | D1 | D5 distill [P3] |
| 画像→决策/聊天 | `get_user_profile()→{summary,...}`（summary≤180tok，经 persona 注入） | D5 | D6/D7 [P3+] |
| 规范→决策 | `materialize_portfolio(...)→{account_id,n_positions,total_equity,warnings,snapshot_report_id}`（写 sim_*，先冻 import_snapshot） | D6 | 引擎 |
| 决策→报告 | `run_daily_decision(...)` 各层落库 → `get_latest_macro_strategy/strategic_plan/tactical_plans/pending_executions/committee_review` | 引擎 | D6 report |
| **叙事校验（H3）** | `verify_numbers(text, facts)→[{token,status}]`（报告与聊天共用） | number_guard | D6/D7 |
| 报告/聊天→审计 | `record_advice_audit(advice_type,advice_ref,source_doc_ids,source_data_ref,model_*,disclaimer_version,content_hash)`；`source_data_ref.report_snapshot_id=vip_reports.id`（M2） | D6/D7 | D3 audit |
| **纠错→审计（M3）** | `apply_correction(...)` 落 `advice_type='correction'` 行：`{field_path,old_hash,new_hash,operator}` | D2 | D3 audit |
| 定价（P2b） | `portfolio_greeks(legs)→{delta,gamma,vega,theta,rho,delta_notional}`、`stress_test(...)` | D4 pricing | D6 风险摘要 / D7 greeks 工具 |
| 检索→聊天 | `semantic_search(query_vec,limit,source_types)→[{snippet,source_type,score}]`（P4；M1 退化 `get_relevant_cards`） | D5 | D7 |

**字段映射（规范 positions → sim_positions，P5）**：`symbol→ticker`(已 normalize)、`quantity→shares`、`avg_cost→avg_cost`、`market_price→current_price`、`market_value_base→market_value`(统一基币)、`unrealized_pnl→unrealized_pnl`、`weight_pct→weight_pct`；`Σmarket_value_base + Σtransactions.net_amount(现金)→sim_account.total_equity/cash_balance`。写用既有 `create_sim_position`+`update_sim_position`(回填计算值)/`update_sim_account`，**不新增 sim 写方法**。

---

## 6. 各子系统详细设计（保留 DDL / 接口 / 算法 / 风险）

### 6.1 D1 · 规范数据模型（P2）

**落位与 PII 边界（H1 裁决落地）**：`instruments/positions/transactions` 进 watchlist.db（与决策消费方同库，P5 投影只是"投影方法"，避免跨库 attach）。**规范数据明文入库是显式取舍**——占比/HHI/VaR/对账全是 SQL 聚合刚需，列级加密会废掉聚合。安全边界因此不靠列加密，而靠：① watchlist.db 库文件 OS 级权限（`chmod 600`，独立数据目录）；② 备份加密；③ 该库不出宿主机、不入应用日志。原始 PDF 密文归 auth.db 加密表，`source_doc_id` 跨库软引用无 FK。升级路径 = SQLCipher 文件级加密（不改任何 SQL），留作 P-later。**此为全系统最大合规敞口，已列 §10 风险表首行。**

**Store（`_PortfolioMixin`）命名（L1 修正）**：**取消"必须带 `instrument_/position_/txn_` 前缀"这条自造规则**——既有 store 全用 `get_sim_*/get_relevant_cards` 无前缀，前缀是过度约束。方法直接叫 `upsert_instrument/get_positions/create_transaction` 等；`get_positions` 与 `get_sim_positions` 名称已足够区分（一个规范层、一个投影层）。写走 `_write_conn`+三件套；读/子查询走 `_filtered` 或显式带 `user_id=? AND market=?`。**读侧陷阱**：`get_positions` 取最新一期用 `WHERE as_of_date=(SELECT MAX(as_of_date) FROM positions WHERE user_id=? AND market=? ...)`——`_filtered` 遇多个 `SELECT` 会 raise，须传 `table=` 或手写带隔离 SQL。

**`reconcile_positions` 算法**（M1 质检核心，移动加权平均）：
```
1. 取 account_ref 下 trade_date<=as_of_date 全部 transactions，按 instrument_id 分组
2. derived_qty=Σquantity（买正卖负；split 行 quantity 已是增量股数，直接并入加法，M6）
   derived_cost=Σ(buy gross+fee)−卖出按加权均成本冲减
3. 取同 as_of_date positions 快照对齐
4. qty_diff=snapshot.quantity−derived_qty；
   status = ok(|diff|<1e-6) / qty_mismatch / missing_txn(快照有无流水) / orphan_txn(有流水无快照)
5. 抽取准确率 = ok 行数 / 总行数
```
自检：buy100/sell30 + 快照70 → assert ok；buy100 + split(+100) + 快照200 → assert ok；快照60 → assert qty_mismatch。

**Provenance**（三表统一）：`source_doc_id/source_page/confidence`，`confidence<阈值` UI 高亮待核、喂决策前可过滤。

**风险**：移动加权平均不做 FIFO/税务分批（M1 单券商股票够用，期权行权/逐日盯市/多币种成本待 P2b 扩 `txn_type` 分支，留 `# ponytail:` 标记）；空 `external_id` 兜底去重加 description 仍属启发式（§2.2 L3，命中只标 flag 不静默丢）。

### 6.2 D2 · PDF 摄取管道（P1）

**架构**：上传 → **`sniff_pdf` 信任边界硬校验** → PyMuPDF 抽文本(扫描页回退渲染图) → 复用 LLM 角色严格 Pydantic 抽取 → 语句内对账 → AES-GCM 加密落 auth.db → 低置信入人工纠错队列。唯一新依赖 `PyMuPDF`。不建解析微服务/队列；不建规范表（那是 D1，P1 存整体加密 JSON blob）。

**信任边界硬校验（`sniff_pdf`，M5 修正，不可省）**：
```
① 读前 4 字节 == b"%PDF"        （魔数，一行，客户端 content-type 不可信）
② 文件大小 ≤ 20MB
③ fitz.open 包 try/except       （防 fitz 崩溃型 DoS）
④ 打开后 doc.page_count ≤ 页数上限；且先看"声明页数/字节数比"异常 → 拒解压炸弹(object stream 膨胀)
超限/非 PDF/打开失败 → 400，绝不进 llm_extract
```

**Schema**（`statement_models.py`）：金额一律 `Decimal`（禁 float）；每数字带 `Provenance{page,raw_text}`；月结单自带合计 `StatementTotals` 独立字段（对账基准）。数字归一化（千分位/货币符/括号负/CR·DR/百分号）在 prompt + Decimal validator 双层兜。

**管道各阶段**（`ingest.py`，线性、失败降级不抛穿）：
```
① sniff_pdf（M5）→ sha256(bytes) 幂等 → 命中 duplicate
② extract_text_layer → 文本密度<阈值标 is_scanned，render_page_png 走视觉
③ llm_extract → 剥 fence→Pydantic→重试1次；预算不足/非法→extract_failed；校验失败→needs_review
④ dedup_trades(五元组指纹 (trade_date,ticker,side,shares,price)) 跨月去重
⑤ reconcile(stmt,prev) → hard_ok? parsed_ok : needs_review；产出 recon_flags(仅 flag，H2)
⑥ save_financial_doc(encrypt) UPSERT on content_hash
⑦ 若 status==parsed_ok → 端点触发 normalize_statement（M1 修正）
⑧ apply_correction(patch)→改 parsed_json→重跑⑤→更新 status→落 correction 审计(M3)
       →若变 parsed_ok 同样触发 normalize（M1 修正，纠错后不卡死在 parsed_ok）
```

**对账 4 勾稽**（`reconcile.py`，`EPS=0.01`/`REL=0.005`）：
```
1 持仓一致性(hard): 期末股数=期初+Σ买−Σ卖  [需 prev，否则 bootstrap=soft]
2 单持仓市值(soft): market_value≈shares×close_price
3 组合总权益(hard): Σ市值+期末现金=equity_end  [抽漏一个持仓必暴露]
4 现金流(hard): cash_end=cash_begin+Σ卖净−Σ买净+Σcash_flows  [费用/出入金抽错必暴露]
hard 不平→needs_review，绝不自动喂决策；soft→仍可 parsed_ok
产出 recon_flags_json：只写 {"pos_shares":"ok","portfolio_equity":"fail",...}，绝无金额(H2)
```
自检：手算样例断言 hard_ok；删一笔交易断言 pos_shares_mismatch。

**准确率量化口径**：不承诺"LLM 抽得准"，用**对账通过率作可交付置信度**——`hard_ok` 才 `parsed_ok`，其余强制人工复核。M1 验收关键是**漏报率**（对账没拦住的错才危险）。

### 6.3 D3 · 安全合规底座（P0）

**总纲**：不新建加密原语/第二套密钥/隔离机制。财务 PII 密文与审计表落 auth.db（与用户表、密钥、删除点同库）。

**加密与明文边界（H1 修正，删除自相矛盾纪律）**：复用 `crypto._get_key()` 三级回退，财务 PII 与 API Key 共用单密钥（M1 不做轮换，升级路径=`keyid`+密文版本前缀）。
- auth.db：完整 `BrokerStatement` 密文（`parsed_json_encrypted`）、原始 PDF（`raw_pdf_encrypted`，默认即焚）。**解密仅在内存，绝不写回 auth.db 明文列/日志/审计 detail。**
- **watchlist.db 规范表明文**（SQL 聚合刚需）——这是显式取舍，边界为库文件 OS 级保护 + 备份加密 + 不出机不入日志（见 §6.1）。初稿"绝不写回明文列"是与 `normalize_statement`（本就把解密后的 parsed JSON 写进 D1 明文列）直接冲突的伪纪律，**本稿删除该条**，代之以上述分库边界，并把"规范层明文"升为 §10 风险表首行。
- **即焚**：M1 默认不持久化原始 PDF，`raw_pdf_encrypted` 留空、解析后随 GC 释放；短存需求时启用 `job_purge_financial_raw`（scheduler，interval 6h，扫 TTL 过期置空，M1 留挂点不排程）。

**删除权（右被遗忘）**：见 §8 统一级联。

**审计三层**：① 动作审计走 `oplog.record_operation(category="vip_financial", detail=file_hint)`（detail 只放 hint/doc_id，绝无 PII）；② 建议溯源 `advice_audit_trail`（advice→`financial_documents.id`；advice→不可变 `vip_reports.id` 快照，M2）；③ **纠错差异审计**（M3）：`apply_correction` 落 `advice_type='correction'` 行，记 `doc_id + field_path + old_hash + new_hash + operator`，只存字段路径与哈希不存明文金额——满足人工篡改可追溯。

**数字幻觉防护公共件（H3 修正）**：`number_guard.verify_numbers` 作为 **P0 公共件**（不再是 D7 聊天专属）。M1 内的 `generate_vip_report` 对 L1/L4 的 LLM 自由文本段**强制过一遍校验**（facts=materialize 后 sim 快照），未命中标"⚠未核到"。对账只保证入库数据可信，不保证 LLM 在报告叙事里不编新数字——M1 可交付物必须自带此护栏，不后置。

**免责声明**（`compliance.py`，唯一真源，裁决 C8）：`DISCLAIMER_VERSION="2026-07-v1"` + `with_disclaimer(text)` 一行拼接。三处挂载：报告末尾、聊天 SSE `disclaimer` 终结事件、审计记 `disclaimer_version`。

**访问控制**：`require_vip`（`dependencies.py`，照 `require_admin`）——`role=='admin'` 或 `settings_json.vip==true`（回库读，热点化后移入 JWT claim）。上传信任边界见上 `sniff_pdf`（M5）。

**风险**：**规范层明文（H1，最大敞口）**、单密钥无轮换（`.encryption_key` 需备份）、sim_* 删除级联缺口（§8 P5 补 `delete_user_sim_data`）、`require_vip` 每请求查库（轻量，热点化移 JWT）——见 §10。

### 6.4 D4 · 衍生品建模（P2b，后置 M1）

**范围裁决**：M1 明确"无衍生品"。纯定价库 `pricing.py` + `positions` greeks 列（§2.3）为地基，可独立开发自检、不接线，**不进 M1 关键路径**。结构化产品 MC 定价明确不做（信任月结单 mark-to-market）。

**纯函数库**（`watchlist/derivatives/pricing.py`，`math.erf` 零依赖）：
```python
bs_price(S,K,T,r,sigma,is_call,q=0)           # T<=0→内在价值；sigma<=0→贴现内在价值
bs_greeks(...)→{price,delta,gamma,vega,theta,rho}  # vega/100, theta/365, rho/100 交易台口径
implied_vol(price,...)→float|None             # 二分[1e-4,5.0]容差1e-6，越界/无解 None（不抛）
portfolio_greeks(legs)→净greeks + delta_notional   # 按 quantity*multiplier*sign 加权
stress_test(legs,spot_shocks,vol_shocks,days_forward)→盈亏矩阵
structured_cashflow_schedule(terms)→[{date,type,amount,conditional}]  # 只展开不定价
```
BSM 含连续股息 q；边界处理（T<=0/sigma<=0/S<=0 退内在价值）。**IV 用二分非牛顿**（深度价内 vega→0 牛顿发散）。自检 4 条：Put-Call 平价、教科书基准 `bs_price(100,100,1,.05,.2,call)≈10.4506`、IV 往返、greeks 中心差分。

**市场输入复用**（零新取数）：S=`fetch_realtime`；σ=`fetch_kline`→`compute_stock_volatility`(已 `*√252`)；IV=`options_pipeline.py:31` 扩 `impliedVolatility`（有链取链、无则回退历史 σ）；**r 无数据源**→`get_risk_free_rate(market)` 常量旋钮（US 0.04/CN 0.018，env 覆盖），报告注明"基于假设 r=x%"。

**缓存刷新（L4 修正）**：§2.3 的 spot/iv/greeks 缓存列**必须配 `job_reprice_derivatives`**（interval，复用 `fetch_realtime`+`pricing.py`，5 处接线同 `job_profile_distill`）。缓存列与刷新 job 成对交付，无 job 则改查询时算，不留无主 stale 缓存。

**喂决策（P5）**：不硬塞 sim_positions（污染股票字段契约）。主路径=`_portfolio_risk_summary`（`decision_engine.py:307`）追加 `derivatives_greeks` 段（组合净 delta/vega + 压测矩阵），L1/L2 看对冲敞口。

**风险**：定价输入不重算（入库/`job_reprice_derivatives` 算好）；IV 缺失回退历史 σ、仍无则标不可靠不假装；A股期权乘数随除权变（从月结单解析，勿写死）；T 用日历日/365 与 theta 一致。

### 6.5 D5 · 画像蒸馏(P3，后置 M1) + 语义检索(P4，后置 M1)

**M1 归属（M4 修正）**：**P3 画像整体移出 M1**。画像是软约束叙事，决策引擎缺 `profile.summary` 照跑（persona 软注入），与 M1 两大核心风险正交，作 M1 后第一个增量。

**硬约束**：embedding 是金融 PII，**必须本地**（排除 API embedding）。**最关键懒惰决策：画像数值全部确定性 SQL/算术算出，LLM 只写叙事 summary 与定性标签**（烧 LLM 估可精确算的胜率是浪费且幻觉）。

**输入源统一（H4/C7 修正）**：`distill_profile` 读 D1 `transactions`（裁决 C7，非 phantom `vip_trades`）**+ D1 `positions`**（持仓维度 sector 权重/HHI/衍生品占比全部取 `positions.weight_pct`，**绝不读 `sim_positions`**——后者由 P5 才产生，P3 早于 P5，依赖倒挂会让持仓画像算不出）。三处口径（§5.1/§5.2/§6.5）已统一为 D1 `positions`。

**蒸馏算法**（`profile_distill.py`）：
```
1 确定性聚合(纯 Python/SQL，读 D1 positions+transactions)：
  holding: sector 权重(positions.weight_pct)/HHI=Σw²/持仓数/平均持有天数
  style  : 换手率=Σ|成交额|/时间加权平均权益 / 平均单笔仓位% / 加减仓比
  risk   : 实现波动率(compute_stock_volatility)/衍生品占比/峰谷回撤
  pnl    : 胜率=盈利平仓/总平仓 / 盈亏比=avg_win/avg_loss / 处置效应
2 EWMA 增量融合：w_old=0.5**(Δdays/90)，半衰期90天；eff_n_new=eff_n_old*w_old+len(trades)
3 LLM 仅叙事(预算门控，MINIMAL 走 _template_summary)
_conf(n)=min(10, 3+int(log2(1+n)))
```
**换手率分母（L2 修正）**：明确为"区间内 `positions` 快照按 `as_of_date` 的**时间加权平均权益**"；**样本<2 期时**平均权益退化为单点、量纲失真 → `confidence` 置低且 summary 注明"区间不足，换手率仅供参考"。无新交易→`_apply_decay`（confidence/eff_n 乘半衰期，<3 时 summary 追加"画像陈旧"提示）。自检：胜率/HHI 算对、eff_n 单调增、样本<2 换手率标低置信、无交易走衰减。

**Job 接线**（P3 阶段五处）：`job_profile_distill`（weekly，Asia/Shanghai，`sun 03:17` 避峰）+ `_JOB_SPECS`/`schedule_config`/`list_job_categories`(→vip_profile)/`list_job_labels`；另**导入成功即时跑一次**。M1 不接。

**注入**：画像 `summary` 复用 `persona.py` 注入点（L2 `:705`、L4 `:1218`、投委会 `committee.py:655`），零新通道；summary≤180tok 无需门控。语义检索**不进决策**（冗余烧钱），只服务聊天。

**P4 检索（后置）**：本地 `fastembed`(BAAI/bge-small-zh-v1.5, 512维, ONNX 无 torch，预烘焙进镜像) + `sqlite-vec`。**隔离必须在 KNN 内**（`user_id partition key`，不能先全局 top-k 再过滤）。`enqueue_knowledge_chunk` 只登记，嵌入在 Job 批处理。自检：跨 user 查询零泄露。

**风险**：sqlite-vec docker 加载失败→`try/except` 降级回 `get_relevant_cards`；fastembed china 下载→预烘焙进镜像；LLM 幻觉数字→数字全确定性算；删号遗留→§8 级联。

### 6.6 D6 · 决策适配 + 报告推荐（P5）

**架构裁决**：`for_market/for_user` 用 `object.__new__(WatchlistStore)` 硬编码克隆，`run_daily_decision` 首行 `store.for_market()` 会擦除任何 store 子类 override →**放弃只读代理，采用物化进 sim_***（唯一无需改引擎的路径）。`run_daily_decision` 确认**只出 `pending_executions`+投委会共识，不触发 trade_executor**→VIP 顾问全盘复用，"推荐"=读回 pending，永不接确认→下单步。

**隔离取舍**：VIP 真实组合与该用户模拟盘共用 `(user_id,market)` sim_account 单例槽（产品语义：导入真实月结单=该市场切真实顾问模式，互斥）。**导入前归档旧 account+positions 为 `vip_reports(kind='import_snapshot')` JSON**——同时充当 C2 兜底与 M2 溯源锚（不可变）。

**`materialize_portfolio`**（`vip_adapter.py`）：字段映射见 §5.2。**隐性前置**（每持仓需）：① watchlist entry（`upsert_watchlist_entry(ticker,tier='track',sector=...)` 供 sector/entry_id）；② 价格快照（`fetch_realtime`+`fetch_kline` 算 rsi/sma 存快照，取数照 `yf_gate`）。`_resolve_price`: last_price→快照 close→实时→avg_cost 兜底(warn)。权益一致性：`total_equity=cash+Σmv`，`weight_pct=mv/total_equity*100`，`peak_equity=max(prev_peak,total_equity)`。返回 `snapshot_report_id`（import_snapshot 的 vip_reports.id）供审计锚。自检：Σweight_pct≈100、pnl 符号、market_value==shares*price。

**报告**（`generate_vip_report`→跑 `run_daily_decision` 收 SSE→读回各层→`_render_vip_report_zh` append-lines）：六节=组合快照/宏观研判(L1)/组合配置诊断(L2 drift)/逐仓战术(L3)/操作建议(L4+投委会)/风险提示+免责。
- **★ 数字幻觉防护（H3，M1 成败项）**：L1/L4 的 LLM 自由文本段，渲染前过 `number_guard.verify_numbers(text, facts=sim 快照 + materialize 汇总)`，未命中的 `$数字/数字%` 就地标注"⚠未核到"。这是 M1 可交付物必备护栏。
- 落 `vip_reports(kind='periodic')` + `record_advice_audit(source_data_ref={report_snapshot_id: 本报告或 import_snapshot id, tickers})`（M2 不可变锚）+ `disclaimer_version`。

**推荐触发**：定期(周/月 job)；不定期(事件驱动，每日轻量 scope)——硬止损击穿/论点失效/催化剂落空/regime 翻转/投委会强否/L2 偏离超阈，落 `kind='alert'`，同 (ticker,类型) 24h 去抖（查 vip_reports created_at，不建 dedup 表）。

**风险**：sim 单例槽复用靠 import_snapshot 兜底；引擎不重算→依赖 materialize 预算好，facts 标注快照时间；LLM 报告叙事数字靠 number_guard 兜（H3）。

### 6.7 D7 · 实时咨询聊天（P6，后置 M1）

**定位**：复刻 `macro_consultation.py` 流式骨架为单 VIP 顾问 + 检索增强 + 工具取数。三原则：零新造基础设施；数字只准来自上下文（facts block 注入 + 回答后 `number_guard` 校验，**与报告共用同一 P0 公共件**，裁决 C12）；M1 先确定性预取，agentic tool-loop 延后。

**流程**（`stream_vip_chat`，`/vip/chat` 端点用 `refresh_guard.guarded` 并发互斥）：取 `vip_chat` 模型→预算门控→落 user 消息→组上下文(检索+持仓 facts+历史)→工具取数→`_iter_tokens` 流式→`number_guard` 校验→落库+计费+`done`。断连由 `_sse_response` 兜。

**上下文预算**（`min_context=_HEAVY`，目标 system+facts+检索+历史+问题≤12k，留 4k 回答）：分层硬上界+滚动摘要（超阈用廉价 LLM 压旧消息进 `chat_sessions.summary`，门控）。降级：FULL/REDUCED(≥0.7 跳工具二轮、检索 limit3)/MINIMAL(≥0.9 只持仓+问题、单模型直答)。

**四工具**（只读，输出结构化 JSON）：`get_holdings()`(get_sim_account/positions，每轮必注入)、`get_quote(ticker)`(fetch_realtime)、`compute_greeks(...)`(调 D4 pricing.py，裁决 C5)、`get_latest_report(kind)`(P5 报告头部)。M1 后先用意图正则+确定性预取，后续升 `bind_tools` 单轮(≤2 轮+门控)。

**三护栏**：① 免责(system 尾固定 + 开场 SSE 常驻)；② scope gate(拒非投资/敏感/代客下单，前置关键词黑名单省钱，越界 `refused`)；③ **数字白名单校验**(调 `number_guard.verify_numbers`，回答后逐个在 facts 子串/近似匹配，未命中 `warn` 标注"⚠未核到"，不阻断)。自检 `test_number_guard`：500 股 vs facts 300 股应告警。

**风险**：tool-loop 烧钱(限轮数+门控)；facts stale(标快照时间)；白名单启发式误报(仅可见性)；greeks r/IV 缺失(声明理论值)；PII 入 prompt(产品核心，审计只存 hint+加密 Key 隔离)。

---

## 7. M1 垂直切片 · 最小实现清单与验收标准

**M1 = P0 + P1(单券商股票) + P2(无衍生品) + P5(一份报告)。**（M4 修正：**P3 画像移出 M1**。）**目标：验证 PDF 抽取准确率 + 数据模型能否喂决策。**

### 7.1 最小实现清单（按依赖顺序，每项独立可验收）

| # | 阶段 | 交付 | 关键复用 |
|---|---|---|---|
| 1 | 依赖 | `pyproject.toml` +PyMuPDF | — |
| 2 | P0 | auth.db `financial_documents`(recon_flags 仅 flag)+`advice_audit_trail`(含 correction) DDL + `_migrate` 补列 + `_FinancialMixin`(encrypt/decrypt) | `crypto.py:44/53/62`；`store.py:410` |
| 3 | P0 | `require_vip` 依赖 + VIP 路由挂载 | `dependencies.py:19` |
| 4 | P0 | `vip/compliance.py`（DISCLAIMER + with_disclaimer） | `report.py` 收尾 |
| 5 | P0 | **`vip/number_guard.py`（数字白名单公共件）+ 自检**（H3/C12） | — |
| 6 | P0 | 统一删号级联接 `admin_api.py:213` 前（§8） | `admin_api.py:213` |
| 7 | P0 | 上传信任边界 `sniff_pdf`（%PDF 魔数/大小/页数/炸弹/try-except，M5）+ oplog 登记 | `oplog.py:60/77` |
| 8 | P1 | `vip/statement_models.py`(Decimal validator) | — |
| 9 | P1 | `vip/ingest.py`(sniff_pdf/extract_text_layer/render_png/llm_extract/dedup_trades/ingest_statement/apply_correction+correction 审计 M3) | PyMuPDF |
| 10 | P1 | `vip/reconcile.py`(4 勾稽，产出仅 flag 的 recon_flags + 自检) | — |
| 11 | P1 | `vip_statement_extract` 角色 + 券商 prompt 硬编码(单版式) | `role_registry.py:83`；`factory.py:327/517`；`budget.py:36` |
| 12 | P1 | `web/vip_api.py` 上传/纠错端点(multipart, `_user_store` 隔离)；**返回 parsed_ok 即触发 normalize**（M1） | `decision_api.py` helpers |
| 13 | P2 | watchlist `instruments/positions/transactions` DDL(split 存增量股数 M6) + 迁移 + `store_portfolio._PortfolioMixin`(命名无强制前缀 L1) | `store_research.py:146-163` |
| 14 | P2 | `vip/normalize.py`(parsed JSON → D1 表，status→normalized) | D1 store |
| 15 | P2 | `reconcile_positions`(移动加权平均 + split 加法自检 + 自检) | — |
| 16 | P5 | `vip_reports` DDL + `vip_adapter.materialize_portfolio`(→sim_*，先冻 import_snapshot) + 自检 | `store_simtrading.py:11/64` |
| 17 | P5 | `generate_vip_report`(**L1/L4 叙事过 number_guard** H3) + `_render_vip_report_zh` + `record_advice_audit`(report_snapshot_id 锚 M2) | `decision_engine.py:1526`；`report.py:20` |
| 18 | P0 | **抽取质量只读统计端点/job**（近 30 天各 status 计数，进 SSE/首页，L7） | 现有 SSE |

**M1 明确不做**：D5 P3 画像(user_profiles/蒸馏/EWMA/job 接线/persona 注入)、D4 衍生品(pricing.py/greeks 列/期权 txn_type/IV/reprice job)、D5 P4 向量层、D7 聊天、D6 事件告警(kind='alert' 可最小)、多券商/多币种换算/公司行动。

### 7.2 验收标准

| 维度 | 指标 | 通过线 |
|---|---|---|
| **PDF 抽取准确率** | 一批真实单券商月结单，`parsed_ok` 文档的 `parsed_json` 逐字段人工抽检正确率 | 字段级 ≥ 95% |
| **对账漏报率（最关键）** | 人工注入错误的样本中，被 hard flag 成功拦下(→needs_review)的比例 | 漏报 ≤ 5% |
| **纠错闭环（M1 修正）** | `apply_correction` 把 needs_review 改成 parsed_ok 后，**自动触发 normalize 进 D1**；且落一条 correction 审计行(字段路径+新旧哈希) | 纠正后进得了报告 + 留痕 |
| **规范化自洽** | `reconcile_positions` 对已 `normalized` 文档 status=ok 行占比（含 split 增量股数分支） | ≥ 98%（跨期首份 bootstrap 除外） |
| **喂决策贯通** | `materialize_portfolio` 后 `Σweight_pct≈100`；`run_daily_decision` 跑通产出 L1-L4+投委会 pending，无字段缺失(entry_id/sector 非空) | 端到端跑通 |
| **报告产出 + 幻觉防护（H3）** | `generate_vip_report` 产六节完整中文报告，末尾含 `DISCLAIMER_VERSION` 文案；**L1/L4 叙事经 number_guard 校验，未核到数字被标注**；`advice_audit_trail` 落一行溯源(report_snapshot_id 锚) | 报告可读 + 溯源完整 + 幻觉数字被拦 |
| **安全合规** | encrypt→save→get→decrypt 往返一致；`recon_flags_json` 无金额(H2)；删号后两库 VIP 表 0 行；非 VIP 访问 403；oplog detail 无 PII；`sniff_pdf` 拒非 PDF/超大/炸弹(M5) | 全部通过 |
| **幂等** | 同一 PDF 重传返回 `duplicate` 不翻倍；跨月重复交易被五元组指纹去重；空 external_id 同日同价两笔不误杀(L3) | 无重复且无误杀 |

**一句话验收**：真实月结单进 →（sniff 边界）抽取(对账通过率作置信度) → 规范化(交叉质检自洽) → 物化 → 一份带溯源、免责**与幻觉数字防护**的决策报告出。抽取准确率、漏报率、报告幻觉防护是 M1 的成败核心。

---

## 8. 统一删号级联（右被遗忘，裁决 C10）

单一级联块，插在 `web/admin_api.py:213` `store.delete_user(user_id)` **之前**，两库按 FK 逆序：

```python
# ── VIP 财务数据级联清理（PII，务必在 store.delete_user 之前） ──
try:
    # auth.db（PII 密文 + 审计）
    store.delete_all_user_financial_docs(user_id)      # financial_documents + advice_audit_trail
    # watchlist.db
    wl = WatchlistStore().for_user(user_id)
    wl.delete_all_user_portfolio(user_id)              # transactions → positions → instruments（FK 逆序）
    wl.delete_user_vip_reports(user_id)                # vip_reports（含 import_snapshot 溯源锚）
    wl.delete_user_sim_data(user_id)                   # sim_account + sim_positions [P5 补，修复现有缺口]
    # 后置 M1 阶段的表，随各阶段落地时解注释：
    # wl.delete_user_profile(user_id)                  # user_profiles           [P3]
    # wl.delete_all_user_chat(user_id)                 # chat_messages→chat_sessions [P6]
    # wl.delete_knowledge(user_id)                     # knowledge_chunks + vec_knowledge [P4]
except Exception as e:
    logger.warning("VIP 数据级联清理失败: %s", e)
store.delete_user(user_id)   # 原 :213
```

审计走 `oplog.record_operation`，detail 只记条数不记 PII。**现有缺口修正**：`admin_api.py:190-197` 只清 watchlist 观察池条目，不清 `sim_account/sim_positions`——P5 物化后必须补 `delete_user_sim_data`，否则残留持仓 PII。M1 各阶段方法随表落地即挂入本块（未落地阶段的行先注释，如上 P3/P6/P4）。

---

## 9. LLM 角色统一（裁决 C6）

`role_registry._INIT_ROLES`（`role_registry.py:83`）加 **2 个角色**（新 group `vip`，前端 AI 配置页据 group 分组，需认得该组），改一处全链生效（调度/预算/AI 配置 UI 自动接）：

```python
RoleDefinition("vip_statement_extract", "VIP 月结单抽取", "vip",
               capability_weights=_PIPELINE_WEIGHTS, min_context=_HEAVY),   # 16k+ 长表格；扫描页视觉由用户为该角色选视觉模型
RoleDefinition("vip_chat", "VIP 咨询顾问", "vip",
               multi_model=False, max_slots=1, min_context=_HEAVY,
               capability_weights=_DECISION_WEIGHTS),                       # 组合+检索需重上下文 [P6]
```

调用零改动：`get_models_for_role("vip_statement_extract"/"vip_chat", user_id=uid)`（`factory.py:327`）+ `budget.can_spend/record`。扫描页视觉抽取用同一 `vip_statement_extract` 角色的用户自选视觉模型（选纯文本模型则扫描件 `extract_failed` 提示），不为视觉单开第二角色。M1 只用 `vip_statement_extract`；`vip_chat` 随 P6 生效。

---

## 10. 风险汇总（合并去重）

| 风险 | 影响 | 缓解 | 阶段 |
|---|---|---|---|
| **规范层 PII 明文存 watchlist.db（H1，最大敞口）** | 完整持仓/流水明文永久留存，加密仅覆盖 auth.db | SQL 聚合刚需的显式取舍：库文件 OS 级权限 + 备份加密 + 不出机不入日志；升级路径 SQLCipher 文件级加密(不改 SQL) | P2 全程 |
| LLM 数字幻觉（入库） | 污染决策 | 对账即校验(3 hard 勾稽)，hard 不平 needs_review 不入决策 | P1 |
| **LLM 数字幻觉（报告叙事，H3）** | M1 报告编造金额/百分比 | `number_guard` P0 公共件，报告 L1/L4 叙事强制校验，未核到标注 | P0/P5 |
| recon_json 含金额 PII（H2） | 明文金额绕过加密 | `recon_flags_json` 只存 flag/字段名/状态，绝无金额数值 | P0 |
| 表格跨券商漂移 | 抽错列 | M1 锁单券商 + 硬编码 prompt 字段映射；换券商=新 prompt | P1 |
| 上传边界可绕过（M5） | 伪造 content-type/解压炸弹 DoS | `sniff_pdf`：%PDF 魔数 + 大小 + 页数 + 声明比 + fitz try/except | P0 |
| 扫描件无文本层 | 抽取空 | 密度阈值→PyMuPDF 渲染→视觉模型；无视觉模型 extract_failed | P1 |
| parsed_ok 无触发方（M1） | 纠错后卡死不进 D1 | 上传/纠错端点返回 parsed_ok 即触发 normalize | P1 |
| 溯源锚悬空（M2） | 报告不可复现 | source_data_ref 指向不可变 vip_reports.id 快照，非单例槽 | P5 |
| 纠错无差异审计（M3） | 人工篡改不可追溯 | apply_correction 落 correction 审计(字段路径+新旧哈希+操作人) | P1 |
| split 语义未定（M6） | 对账加法错 | txn_type=split 存增量股数，直接并入 Σquantity；比例放 description | P2 |
| 引擎不重算 mv/weight_pct | 占比/VaR 拿 0/stale | 入库/materialize 预算好；多币种用 market_value_base；facts 标快照时间 | P2/P5 |
| sim_account 单例槽复用 | 覆盖模拟盘 | 导入前 kind='import_snapshot' JSON 归档 | P5 |
| sim_* 删除级联缺口 | 删号残留持仓 PII | P5 补 delete_user_sim_data（§8） | P5 |
| 单密钥无轮换 | .encryption_key 丢=全部密文不可解 | 运维备份；升级=keyid+密文版本前缀 | P0 |
| 换手率分母退化（L2） | 单据下量纲失真 | 分母=时间加权平均权益，样本<2 标低置信+注明 | P3 |
| 空 external_id 误杀重复（L3） | 丢合法成交 | 兜底键加 description，命中只标 suspected_dup flag 不静默丢 | P2 |
| 衍生品缓存无刷新(L4) | greeks stale | P2b 配 job_reprice_derivatives 5 处接线，否则查询时算 | P2b |
| r 无数据源 | 期权定价偏差 | get_risk_free_rate 常量旋钮，报告注明假设 r | P2b |
| IV 缺失 | greeks 不可靠 | 回退历史 σ，仍无标不可靠不假装 | P2b |
| sqlite-vec/fastembed china 不可达 | 检索失效 | 扩展 try/except 降级回 get_relevant_cards；模型预烘焙进镜像 | P4 |
| 画像陈旧误导 | 决策偏 | 时间衰减 + confidence<3 summary 自带告警 | P3 |
| tool-loop 烧钱 | 失控取数 | 后置 M1 确定性预取；升级限轮数≤2+门控+refresh_guard | P6 |
| 抽取质量静默劣化（L7） | 版式漂移无告警 | 只读统计端点/job：近 30 天各 status 计数进 SSE/首页 | P0 |
| PII 入 prompt | 敏感数据 | 产品核心不可避免；审计 detail 只存 hint + 加密 Key 隔离 | 全程 |

---

## 附：一句话开发地图

**P0** auth.db 建 `financial_documents`(recon 仅 flag)+`advice_audit_trail`(含 correction)+`require_vip`+`compliance`+**`number_guard` 公共件**+`sniff_pdf` 边界+统一级联+抽取质量统计 → **P1** PyMuPDF 抽取 + `vip_statement_extract` 角色 + 语句内 4 勾稽对账 + 加密落库 + 纠错端点(触发 normalize + correction 审计) → **P2** watchlist 建 `instruments/positions/transactions`(split 增量股数) + `normalize_statement` 桥接 + `reconcile_positions` 交叉质检 → **P5** `materialize_portfolio`(先冻 import_snapshot)→sim_* → `run_daily_decision`→`generate_vip_report`(叙事过 number_guard)→`vip_reports`+审计。全部照抄既有隔离/加密/SSE/预算/角色范式，**M1 无画像层、无向量层、无衍生品、无聊天、无引擎改动**。

> 后置 M1：**P3 画像**(user_profiles/确定性聚合+EWMA/persona 注入，读 D1 positions 非 sim)、P2b 衍生品(`pricing.py` 纯函数 + positions greeks 列 + IV 扩展 + `job_reprice_derivatives`)、P4 语义检索(fastembed+sqlite-vec)、P6 聊天(chat 表 + `vip_chat` + 三工具 + 复用 number_guard)。schema 已留 `isin`/`extra`/衍生品列/`vec_knowledge` 锚点，加时走 `MIGRATIONS` 尾 `ALTER`/`CREATE IF NOT EXISTS`，不改基表。