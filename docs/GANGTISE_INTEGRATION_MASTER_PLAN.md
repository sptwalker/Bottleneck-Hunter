# Gangtise 全域数据接入总体方案

> 版本：2026-08-31 · 状态：待评审（未动工，未 commit）
> 依据：[GANGTISE_CAPABILITY_MAP.md](GANGTISE_CAPABILITY_MAP.md)（六域能力已逐一活体实测）
> 目标：把 Gangtise 从「A股财务单点」升级为**贯穿 chain 分析 / L1-L4 决策 / 观察池催化剂 / 投委会证据 / VIP 顾问 / 选股漏斗**的统一投研数据底座。
> 铁律：ak/sk 机密**绝不入库/日志/提交**；受控全局 key 仅走 `resolve_gangtise_credentials`；市场隔离 `for_market`；诚实边界不夸大。

---

## 1. 设计原则（先定，避免过度工程）

1. **复用 DataHub，不建第二套。** 所有取数一律做成 `CapabilityProvider`（[hub.py:41](../bottleneck_hunter/data_provider/hub.py#L41)）注册进 `build_providers()`（[providers.py:749](../bottleneck_hunter/data_provider/providers.py#L749)），自动享受 priority 排序 + 熔断 + failover。**不在业务层直连 HTTP。**
2. **一个 Gangtise 域 = 一个 capability。** 新能力用新常量（见 §3），`GangtiseProvider` 拆成按域的多 provider 或多 capability，`priority` 依质量定。
3. **受控全局 key 是唯一例外。** 其余一切按 `current_user` 解析，缺 key 即 `MissingUserKeyError`。Gangtise 走 admin 双开关授权的 `resolve_gangtise_credentials(user_id)`。
4. **薄适配 + 纯映射可单测。** 取数放 `gangtise_client.py`（无 env、token 缓存、抛异常交熔断）；`dict→规范 schema` 的映射函数纯函数化，`demo()` 自检。
5. **诚实边界硬标注。** 拿不到的（美股一致预期）留空不假装；未验证的（vault）不宣称。每个简化点 `ponytail:` 注释标上升路径。
6. **YAGNI 分期。** 按「填系统真缺口 × 实测已验证」排优先级，P0 先落地见效，P2/P3 待需求确认再动。

---

## 2. 能力 → 模块映射总表

| Gangtise 能力（实测） | 接口 | 目标系统模块 | 消费点 | 阶段 |
|---|---|---|---|---|
| **EDB 全球宏观**（CPI/PPI/利率/PMI…） | `open-alternative` EDB getData | **L1 宏观层** | `generate_macro_strategy` 数据门 / [decision_api macro](../bottleneck_hunter/web/decision_api.py#L130) | **P0** |
| **财报日历/业绩预告快报** | `open-insight/schedule/performance-calendar` | **观察池催化剂** | `get_upcoming_catalysts`（[decision_api:788](../bottleneck_hunter/web/decision_api.py#L788)） | **P0** |
| **上市公司公告**（A/H/US） | `open-insight/announcement*` | 观察池催化剂 + 个股事件 | 催化剂源 + chain 尽调 | **P0** |
| **中资券商研报**（深度/公司/行业/总量） | `open-insight/broker-report` | **chain 交叉验证 / 投委会证据层** | `chain/graph` 反面拷问、投委会评审 | **P1** |
| **外资券商研报/观点** | `open-insight/foreign-report`·`foreign-opinion` | 同上（外资视角） | 交叉验证多视角 | **P1** |
| **知识库 RAG** | `open-data/ai/search/knowledge_base` | chain 分析证据召回 | decomposer / bottleneck 佐证 | **P1** |
| **美股财务**（三表） | `open-fundamental`（`.O/.N` 码制） | **CAP_FINANCIALS 扩到 us_stock** | `GangtiseProvider.markets` | **P1** |
| **AI 研报叙事**（一页通/投资逻辑/同业对比/财报点评） | `open-ai/agent/*` | chain 报告增强 / 投委会预读 | report.py / 委员预读材料 | **P2** |
| **指标选股** | `open-indicator` | **瓶颈环节候选粗筛** | 供应商检索前置漏斗 | **P2** |
| **美股行情/历史价**（beta 用） | `open-quote`（`securityList`） | **VIP 顾问：beta/benchmark** | 补 beta恒0（见 §9） | **P1** |
| **美股公告 announcement-us** | `open-insight/announcement-us` | **VIP 顾问：持仓催化剂** | 补 催化剂恒空（见 §9） | **P0** |
| A股一致预期（已接） | `earning-forecast` | financials consensus | 现状 ✅ | 已完成 |
| 私有 vault | `open-vault` | 视场景 | 未验证 | P3 |

> **VIP 顾问系统单列 §9**——它有独立于观察池的数据缺口（beta恒0/催化剂恒空/顾问建议缺据），
> 且持仓以**美股为主**（FCN/累购累沽 on NVDA/SOXX 等），接入口径与观察池不同，必须专章说明。

---

## 3. 新增 DataHub 能力常量（[hub.py:25](../bottleneck_hunter/data_provider/hub.py#L25)）

现有：`quote/daily/financials/earnings/news/sec/institutional/options/insider/notice/smartmoney`。新增：

```python
CAP_MACRO_EDB   = "macro_edb"     # EDB 宏观指标（L1）
CAP_CALENDAR    = "calendar"      # 财报日历/业绩预告快报（催化剂）
CAP_ANNOUNCE    = "announcement"  # 上市公司公告（催化剂+尽调）
CAP_RESEARCH    = "research"      # 券商研报（中/外资，证据层）
CAP_KB          = "kb"            # 知识库 RAG 检索
CAP_NARRATIVE   = "narrative"     # AI 研报叙事（agent）
CAP_SCREEN      = "screen"        # 指标选股
```

> `CAP_MACRO_EDB`/`CAP_CALENDAR`/`CAP_ANNOUNCE`/`CAP_RESEARCH`/`CAP_KB`/`CAP_NARRATIVE`/`CAP_SCREEN` 均非「按 ticker 取一条」的经典 provider 语义（有的按指标 id、按市场、按关键词），故 fetch 签名沿用 `(capability, ticker, market, user_id)` 但允许 `ticker` 载多态参数（如 EDB 传 indicator_id、calendar 传 market 段）。**这是与现有 provider 的唯一形态差异，在各 provider docstring 标注。**

---

## 4. 分阶段接入方案

### Phase 0 — 宏观 + 催化剂（填最大真缺口，全 A/US 通用）

**动机：** L1 宏观层当前数据薄弱；观察池催化剂此前误判「无源」。两者都是决策**触发器**，价值最高。

**4.1 EDB 宏观 provider**
- `gangtise_client.py` 加 `fetch_edb(ak, sk, indicator_ids: list, start, end)` → `{indicator_id, name, points:[{date,value}]}`。payload 键 `indicatorIdList`（**不是 indicators**）。
- `providers.py` 加 `GangtiseMacroProvider`（`CAP_MACRO_EDB`，markets `{a_stock, us_stock}`，priority 0）。
- 常用指标 id 建**小映射表**（CPI=M00012463 等，实测值 337.13@2026-07），放 `data_provider/gangtise_edb_indicators.py`；未映射的走 `indicator-search` 解析。
- **接入点：** `generate_macro_strategy` 生成前，先取 EDB 关键指标注入 prompt 上下文（美国 CPI/PPI/联邦基金利率/PMI + 中国 CPI/PPI/社融）。落 `macro_snapshot`，L1 报告引用真实数据源。

**4.2 财报日历 + 公告 催化剂源**
- `gangtise_client.py` 加 `fetch_performance_calendar(ak, sk, markets, start, end, categories)` 和 `fetch_announcements(ak, sk, market, security, start, end, categories)`。
  - 日历 payload：`marketList:["aShares"/"hkStocks"/"usChinaConcept"/"usStocks"]`（传 `cn` 报 100005）；`category`：`performanceForecast/performanceExpress/performanceAnnouncement`。字段 `securityCodeList/securityName/category/publishDate/title`。
  - **时间窗有硬上限**（实测跨6月报 `110003 TIME_RANGE_EXCEEDED`）→ client 内部**按月分段**拉取再合并。
- 催化剂写入复用现有 `get_upcoming_catalysts` 背后的催化剂表（观察池内每个 ticker 命中日历/公告即生成一条 catalyst，含 `event_date/type/title/source=gangtise`）。
- **接入点：** scheduler 加系统级 job `job_gangtise_catalyst`（抄 `job_macro_update` 的 `_wl_store`/`_auth_store` 直连写法，`kind=interval`，每日一次），对观察池全量 ticker 拉未来 N 日日历 + 近 M 日公告 → upsert 催化剂（按 `securityCode+event_date+type` 幂等）。

**Phase 0 验收：**
- `python -m bottleneck_hunter.data_provider.gangtise_client` demo 自检通过（EDB/日历/公告解析 + 分段合并 + token 缓存）。
- 真实数据证明：拉美国 CPI 曲线非空；观察池某 A股 ticker 命中 performance-calendar 生成催化剂；overview 的 `upcoming_catalysts` 出现 gangtise 源事件。
- 幂等：同日重跑 job 不产生重复催化剂。

---

### Phase 1 — 研报证据层 + 美股财务

**动机：** chain 三步法的「交叉验证」和投委会评审，当前靠 LLM 内部知识**反面拷问**，缺**真实研报证据**。接入券商研报 + KB RAG，让论证有据可引。美股财务实测已通，顺带放开。

**5.1 研报 provider（中/外资 + KB）**
- `gangtise_client.py` 加 `fetch_research(ak, sk, *, securities, industries, category_list, llm_tag_list, start, end, foreign=False)`：`foreign=False` 走 `broker-report/getList`，`True` 走 `foreign-report/getList`。返回列表含摘要 + `file-id`（全文按需 `download/file`）。
  - 两条分类轴：`categoryList`（macro/strategy/industry/company/…）+ `llmTagList`（inDepth 深度/earningsReview 业绩点评/industryStrategy 行业策略）。
  - **同样有时间窗上限** → 按月分段。
- `fetch_kb(ak, sk, query, top)` → `knowledge_base` 语义片段（实测返 10 片段）。
- `providers.py` 加 `GangtiseResearchProvider`（`CAP_RESEARCH`）+ `GangtiseKBProvider`（`CAP_KB`），markets `{a_stock, us_stock}`（研报覆盖 A/H/US/中概）。
- **接入点：**
  - **chain 交叉验证**（`chain/graph`）：拷问某标的投资逻辑前，先召回该标的最新深度/点评研报摘要 + KB 片段，作为「反方论据」注入 prompt，把「凭模型记忆质疑」升级为「据研报质疑」。
  - **投委会评审**：委员预读材料附最新券商观点（含目标价/评级/评级变动），投票有据。

**5.2 美股财务放开**
- `gangtise_client._sec_code`：美股经 `open-reference/securities/search` 解析 `gtsCode`（`.O`/`.N`，**非 `.US`**），加缓存。
- `GangtiseProvider.markets()` 加 `us_stock`；`_map_gangtise_financials` 复核美股字段名/币种（USD，`revenue_yi` 语义改「亿美元」或加 `currency` 字段）。
- **硬边界：美股一致预期不可得**（`earning-forecast` A股-only，返 120001）→ 美股 `consensus_eps/pe` 留空，`ponytail:` 标注走他源的上升路径。

**Phase 1 验收：** 真实拉到茅台深度研报摘要（国海/长江已验证）注入交叉验证 prompt；AAPL.O 财务进 DataHub（1094亿USD）；美股一致预期确认留空不报错；`pytest` 全绿。

---

### Phase 2 — AI 叙事增强 + 指标选股漏斗

**动机：** 锦上添花，非填缺口，故排后。

**6.1 AI 研报叙事**
- `gangtise_client.py` 加 `fetch_narrative(ak, sk, agent_type, security, **kw)`：URL `open-ai/agent/{subpath}` + `securityCode`。`earnings-review`/`viewpoint-debate` 需 getId→轮询 getContent（≤600s，client 内部轮询封装）。
- `providers.py` 加 `GangtiseNarrativeProvider`（`CAP_NARRATIVE`）。
- **接入点：** chain `report.py` 生成个股报告时，可选附「一页通」「投资逻辑」「同业对比」作为增强段落（实测茅台一页通含瑞银1572/华创2030目标价、i茅台+274%等鲜活数据）。**默认关，管理员/高级用户开**（agent 调用较重、有 600s 轮询）。

**6.2 指标选股漏斗**
- `gangtise_client.py` 加 `screen(ak, sk, universe, expression, ...)`：需先 `sector-search`+`indicator-search` 补参（裸 payload 缺条件报 100001）。封装口语→payload 的解析（参考 screener skill 的三段式）。
- `providers.py` 加 `GangtiseScreenProvider`（`CAP_SCREEN`，A股）。
- **接入点：** 供应商检索前置——瓶颈环节确定后，用指标选股在该环节板块内粗筛（如「ROE>15 & 市值>500亿」，实测半导体返 20 只含北方华创），缩小 chain 深挖候选集。

**Phase 2 验收：** 真实生成一页通并入报告；指标选股返回真实候选（半导体案例已验证）。

---

### Phase 3 —（YAGNI，待需求）港股财务 / private vault

- 港股财务：`_sec_code` 补 `.HK` 码制。
- vault：`open-vault` 未验证，需明确场景才接。
- **不预建。**

---

## 7. 统一客户端扩展蓝图（[gangtise_client.py](../bottleneck_hunter/data_provider/gangtise_client.py)）

在现有 `_login/_headers/_sec_code/fetch_financials/fetch_earnings_forecast` 基础上，按域加平行函数，**共用 token 缓存与错误处理**：

| 新函数 | 域 | 关键 payload/坑 |
|---|---|---|
| `fetch_edb` | EDB 宏观 | `indicatorIdList` |
| `fetch_performance_calendar` | 日历 | `marketList` 枚举；**按月分段** |
| `fetch_announcements` | 公告 | A/H/US 三 URL；categoryId 树 |
| `fetch_research` | 研报 | 中/外资双 URL；两分类轴；**按月分段** |
| `fetch_kb` | 知识库 | query+top |
| `fetch_narrative` | agent | `open-ai/agent/{subpath}`；getId→轮询 |
| `screen` | 选股 | 三段式解析；缺条件 100001 |
| `_resolve_gts_code` | 码制 | 美股 `.O/.N` 经 securities/search，缓存 |

每个新函数配 `demo()` 断言（body 解析 + 分段合并 + 错误码→空）。

---

## 8. 横切约定（全阶段遵守）

- **熔断/failover：** 全走 DataHub，异常抛出交 hub 层熔断，provider 内不吞。
- **缓存：** token 进程内 TTL 缓存（已有）；日历/研报/EDB 按 (参数, 日期) 做**短 TTL 结果缓存**避免同日重复拉（催化剂 job 每日一次，缓存 6-12h）。
- **时区：** EDB/日历/公告日期 UTC 存、`fmtBJ` 北京展示；催化剂 `event_date` 归一到北京日历。
- **隔离：** 落库一律 `for_user(sub).for_market(market)`；Gangtise 取数走受控全局 key，**取数与落库归属分离**（取数用 admin 授权 key，催化剂/研报落到对应用户观察池）。
- **更新历史：** 每阶段合并的 commit 带 `📢` 行首白话行。
- **验收铁律：** 每能力以**真实返回数据**佐证，非代码桩。

---

## 9. VIP 顾问系统数据补强（专章）

VIP 顾问（[vip/advisory.py 评审](../bottleneck_hunter/vip/advice_review.py#L65) + `_VipProjectionMixin` 每日重估 + vip.js 仪表盘）
有一批**独立于观察池**的数据缺口。据 [VIP 价值评估](VIP_ADVISOR_VALUE_ASSESSMENT_2026-08.md)，多数缺口是
**「算法未接」而非「数据不可得」**——Gangtise 正好补上此前拿不到的数据侧，让这些算法能落地。
且 VIP 持仓**以美股为主**（FCN/累购累沽标的多为 NVDA/SOXX 等），接入口径偏美股。

### 9.1 缺口 → Gangtise 数据 映射

| VIP 现存缺口 | 缺什么数据 | Gangtise 补给 | 阶段 |
|---|---|---|---|
| **催化剂恒空** | 持仓标的的财报日/公告/事件 | `announcement-us` + `performance-calendar`(usStocks/usChinaConcept) | **P0** |
| **beta 恒 0** | 标的与基准的历史价序列 | `open-quote` 历史行情（标的 + 基准指数） | **P1** |
| **顾问建议缺据** | 标的基本面 + 券商观点 | 美股财务 + 研报(中/外资) + KB + 一页通叙事 | **P1** |
| **advisory pass 只读 L1 宏观** | 宏观本身偏薄 | EDB 宏观注入 L1 后，advisory 间接受益 | **P0**（随 L1） |
| **复盘缺外部对照** | 事后的研报/事件回看 | 研报 + 公告历史 | **P2** |

### 9.2 具体接入点

- **持仓催化剂（P0）**：VIP 每日重估 job 或催化剂 job 内，对 VIP 各账户持仓 ticker 拉
  `announcement-us`（近 M 日）+ `performance-calendar`（未来 N 日，`marketList` 含 `usStocks`/`usChinaConcept`）
  → 写入 VIP 催化剂（归 `for_user(vip_sub).for_market("us_stock")`），仪表盘「持仓催化剂」不再恒空。
  **注意码制**：美股经 `_resolve_gts_code` 解析 `.O/.N`；FCN 篮子多标的逐一取。
- **beta / benchmark（P1）**：`gangtise_client` 加 `fetch_quote_history(ak, sk, securities, start, end)`
  （`open-quote`，`securityList` payload），取标的 + 基准（如 NDX/SPX）历史收盘 → 现有 beta 算法接上真实序列，
  vip.js 的 benchmark 曲线（[value-series](../bottleneck_hunter/web/static/js/vip.js#L681)）用真实基准。
- **顾问建议增据（P1）**：advisory pass 生成逐仓「减/持/加」建议前，注入该标的：美股财务 + 最新券商研报摘要
  （目标价/评级/评级变动）+ KB 片段 + 一句话/一页通叙事 → 建议**有据可引**，非纯模型判断。
  复用 §5 的 `CAP_RESEARCH`/`CAP_KB`/`CAP_NARRATIVE`，仅消费方从 chain 扩到 VIP advisory。

### 9.3 VIP 专属约束（不可违反既有约定）

- **决策账户与 VIP 解耦**：决策中心 `sim_account('')` 不经 VIP 解析（[decoupled 约定](../MEMORY.md)）；
  本补强只碰 VIP 账户（`account_ref != ''`），**不动决策中心 sim_account**。
- **归属分离**：取数走 admin 授权全局 key；催化剂/建议落 `for_user(vip_sub)`，严格隔离。
- **管理员专用**：VIP 顾问本就 admin-only，接入不改变权限面。
- **压测口径不混**：beta/催化剂补强**不触碰** [VIP 压测的 pnl 口径](../MEMORY.md)（payoff 引擎 pnl 非裸市值）与 FCN 跨源去重（lot_key）。
- **美股一致预期仍不可得**：VIP 美股持仓的 `consensus_eps/pe` 同样留空（接口 A股-only），不假装。

### 9.4 VIP 验收

- 真实数据证明：某 VIP 美股持仓（如 NVDA）命中 `announcement-us`/日历 → 仪表盘「持仓催化剂」非空。
- beta 用真实历史价算出非 0 值；benchmark 曲线为真实指数。
- advisory 一条逐仓建议附真实券商研报摘要 + 目标价。
- 回归：`pytest tests/test_vip_advice_review.py tests/test_vip_portfolio.py tests/test_vip_phase2.py` 全绿；
  决策中心 overview 不受影响（解耦验证）。

---

## 10. 硬边界与不做项（诚实）

| 项 | 结论 |
|---|---|
| 美股一致预期 | ❌ 接口 A股-only（120001），留空不假装 |
| 美股/港股指标选股 | ❌ 板块体系目前 A股，选股不覆盖 |
| **US/HK 指数历史行情** | ❌ 实测 `kline/daily` 对 SPX.SPI/^GSPC/SPY.N/QQQ.O/HSI.HI 一律 400（`SECURITY_CODE_INVALID`），无 index-daily 端点。**仅 A股指数(000300.SH)可取**。故 VIP/L2 beta 与净值基准：A股走 Gangtise 兜底（yfinance 被墙时），**US/HK 仍只能靠 yfinance/akshare**，取不到则 beta 诚实为 0、不造假 |
| private vault | ⚪ 未验证，不接入直到有明确场景 |
| 研报/日历长时间窗 | ⚠️ 有硬上限，必须按月分段（治本，非重试） |
| agent 叙事 | ⚠️ 调用重 + 600s 轮询，默认关，按需开 |
| 金额刻度 | 校准点在 `_map_gangtise_financials` 的 `1e-8` + 币种字段 |

---

## 11. 里程碑与工作量

| 阶段 | 交付 | 工作量 | 依赖 |
|---|---|---|---|
| **P0** | EDB 宏观注入 L1 + 财报日历/公告催化剂源 + scheduler job | 中（2 域 + 1 job） | 无 |
| **P1** | 券商研报+KB 证据层接 chain/投委会 + 美股财务放开 | 中-大（3 provider + 码制解析） | P0 客户端骨架 |
| **P2** | AI 叙事增强报告 + 指标选股漏斗 | 中 | P1 |
| **P3** | 港股财务 / vault | 视需求 | 按需 |

**建议先批 P0**：填补宏观+催化剂两大真缺口，全市场通用、无硬边界、见效最快。

---

## 附：一句话总纲

> 一个 `gangtise_client` 按域扩函数，一批 `Gangtise*Provider` 按能力注册进 DataHub，
> 宏观喂 L1、日历公告喂催化剂、研报 KB 喂 chain 与投委会、美股放开财务、叙事与选股锦上添花——
> 六域数据经**同一个 DataHub 熔断与隔离骨架**贯通全系统，不新建第二套管线。
