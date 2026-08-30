# Gangtise 投研 OpenAPI 能力全图 + 实测证据 + 可接入范围重估

> 版本：2026-08-31 全面复核版
> 目的：纠正前期「只看了 6 个 skill 中的 1 个」的片面解读，逐域给出**活体实测证据**，
> 并对 BottleneckHunter 的可接入范围做**全面重估**（此前范围过窄）。
> 凭据：ak/sk 为机密，本文档及任何日志/提交**绝不出现**；实测均通过本机 `.authorization`（已 gitignore）完成。

---

## 0. 一句话结论

Gangtise 不是「A股财务数据源」，而是一整套**覆盖 A股/港股/美股 + 全球宏观**的投研中台，含
**6 大 skill / 域**：财务行情（含美股、EDB 宏观）、公告研报财报日历、知识库 RAG、研报级 AI 叙事、
指标选股、私有 vault。当前系统**只接入了其中 1 域的 1 个能力**（A股利润表 + 一致预期），
**可接入面被严重低估**。唯一确定的硬缺口是**美股一致预期**（接口 A股-only）。

---

## 1. 六域能力全图（逐 skill 解读）

| # | Skill / 域 | 接口域 | 核心能力 | 覆盖市场 |
|---|---|---|---|---|
| 1 | **gangtise-data** | open-quote / open-fundamental / open-alternative / open-indicator / open-reference | 实时/历史行情、财务三表、一致预期、另类数据、**EDB 宏观指标库**、证券/板块检索 | A股/港股/**美股** + **全球宏观** |
| 2 | **gangtise-file** | open-insight / open-data | **上市公司公告**、**券商研报**、**财报日历/业绩预告快报**、路演纪要 | A股/港股/美股/中概 |
| 3 | **gangtise-kb** | open-data | 知识库 RAG 检索（研报库语义片段召回） | 全库 |
| 4 | **gangtise-agent** | open-ai | 研报级 AI 叙事：一句话总结/一页通/投资逻辑/同业对比/财报点评/观点思辨/主题跟踪/研究提纲 | A股/港股（部分美股） |
| 5 | **gangtise-screener** | open-indicator | 指标选股：口语→指标/板块检索→条件表达式→选股 | A股（板块体系） |
| 6 | **gangtise-private** | open-vault | 私有数据 vault（机构自有数据回注） | 租户私有 |

---

## 2. 逐域实测证据（真实数据，非代码桩）

### 2.1 gangtise-data — 财务 / 行情 / 宏观

**A股财务（利润表累计口径）✅**
`600519.SH` 最近报告期：营收 **907.03 亿**、归母净利 **445.17 亿**、毛利率 **89.56%**。
> 字段实测为缩写：`opRev`(营业收入) / `opCost`(营业成本) / `netProfitAttrParent`(归母净利) / `basicEPS`。
> 毛利率须用「营业收入-营业成本」，`totalOpCost`(营业总成本，含税金费用) 是营业利润口径，不可用。

**A股券商一致预期 ✅**
`600519.SH` 一致预期 **EPS=69.21 / PE=19.95**（`earning-forecast`，`consensusList` 必须显式列指标，
传 `[]` 只回 forecastYear+date 无数值列）。

**美股财务 ✅**（此前误判「美股拿不到」）
`AAPL.O` 营收 **1094 亿 USD**；`NVDA.O` 营收 **1778 亿**。
> 美股码制是 `.O`(NASDAQ)/`.N`(NYSE)，**不是 `.US`**（`.US` 返回 0 行）；经 `securities/search` 返回的 `gtsCode` 解析。

**美股行情 ✅**
`quote` 用 `securityList:[...]` + `fieldList` payload（不是 `securityCode`，否则 100003）。

**美股/全球宏观 EDB ✅**（关键新发现）
美国 CPI 指标 `M00012463` = **337.13**（2026-07）。
> EDB `getData` 用 `indicatorIdList`（不是 `indicators`，否则 100003）。EDB 是完整宏观指标库，
> 可取 CPI/PPI/利率/PMI 等，直接支撑 L1 宏观层。

**美股一致预期 ❌（唯一确定硬缺口）**
`earning-forecast` 传美股码返回 `120001 SECURITY_CODE_INVALID / 请输入有效A股` —— 一致预期接口 **A股-only**。

### 2.2 gangtise-file — 公告 / 研报 / 财报日历

**A股公告 ✅** `announcement/getList` 取到 `300442.SZ` 公告列表。
**券商研报 ✅** `broker-report/getList` 可用。
**财报日历（业绩预告/快报/公告）✅**
`schedule/performance-calendar/getList` 取到 5 行，样本 `600608.SH 上海科技 2026-08-31 中期业绩公告`。
> `marketList` 值为 `aShares`/`hkStocks`/`usChinaConcept`/`usStocks`（传 `cn` 返回 100005）。
> `category` 枚举：`performanceForecast`(业绩预告)/`performanceExpress`(业绩快报)/`performanceAnnouncement`(业绩公告)。
> 字段：`performanceReportId, securityCodeList, securityName, category, publishDate, title, hasAttachment`。
> **这正是前期误判「Gangtise 无催化剂事件源」的那个源** —— 它其实一直存在。

### 2.3 gangtise-kb — 知识库 RAG ✅
`open-data/ai/search/knowledge_base` 语义检索返回 **10 个研报片段**，可作 chain 分析/投委会的证据召回层。

### 2.4 gangtise-agent — 研报级 AI 叙事 ✅
- **一句话总结** `600519.SH`：「下半年飞天供给收紧+旺季催化，批价有望上行，业绩环比改善确定性高」。
- **一页通** `600519.SH`：返回完整研报级叙事（公司近况/短期逻辑/改革三线/机构目标价瑞银1572·华创2030…）。
- 其余同 auth 路径可用：`investment-logic`(投资逻辑)/`peer-comparison`(同业对比)/`earnings-review`(财报点评，
  getId→轮询 getContent≤600s)/`viewpoint-debate`(观点思辨)/`theme-tracking`(主题跟踪)/`research-outline`(研究提纲)。

### 2.5 gangtise-screener — 指标选股 ✅
`半导体 / ROE>15 && 总市值>500亿` → **20 只**，样本：北方华创(ROE 16.05 / 市值 5061 亿)、
长川科技(ROE 33.17)、德明利(ROE 23.94)。
> 全串联：口语范围→`sector-search`→sectorId；口语指标→`indicator-search`→field code+参数元数据；
> 表达式 `F1>15 && F2>500`。裸 payload 缺条件会 100001。板块体系目前是 A股。

### 2.6 gangtise-private — 私有 vault ⚪
`open-vault` 为租户私有数据回注通道，属 admin 范围，本次未探测（诚实标注，非「不可用」）。

---

## 3. 可接入范围全面重估

### 3.1 原范围 vs 实际可接入面

| 维度 | 原计划（Part A + 已砍的 Part B） | 复核后实际可接入面 |
|---|---|---|
| 财务 | A股利润表 + 一致预期 | + **美股财务**、+ 资产负债表/现金流量表（三表全） |
| 催化剂 | Part B「无源可接」→砍掉 | **财报日历 + 公告 + 业绩预告快报**（源一直在，误判） |
| 宏观 | 无 | **EDB 全球宏观指标库**（CPI/PPI/利率/PMI…）直供 L1 |
| 研报/叙事 | 无 | **agent 一页通/投资逻辑/同业对比** + **KB RAG 召回** |
| 选股 | 无 | **指标选股**（可做瓶颈环节候选粗筛） |
| 行情 | 无 | A股/港股/美股 实时+历史 quote |

### 3.2 已落地现状（不夸大）

当前生产仅 `GangtiseProvider`（[providers.py:684](../bottleneck_hunter/data_provider/providers.py#L684)）：
- 认领 **`CAP_FINANCIALS` 单一 capability**、**仅 `a_stock`**；
- 刻意不认领 `CAP_EARNINGS`（A股实际值已由 akshare 供给，认领会以 priority0 盖掉致回归）；
- 一致预期已并入 financials 的 `consensus_eps/pe`。
即：**6 域中已接 1 域的 1 能力的 1 市场**。

### 3.3 建议接入优先级（按「填补系统真缺口 × 实测已验证」排序）

| 优先级 | 能力 | 接入点 | 依据 | 工作量 |
|---|---|---|---|---|
| **P0** | **EDB 宏观** | L1 宏观层数据门（[decision_engine.py](../bottleneck_hunter/watchlist/decision_engine.py)） | 系统当前宏观数据薄弱；EDB 实测直供、权威源 | 中（新 capability + EDB 取数） |
| **P0** | **财报日历/公告** | 观察池催化剂（复活被砍的 Part B 目标） | 误判已纠正，源实测可用；催化剂是决策触发关键 | 中 |
| **P1** | **美股财务** | 放开 `GangtiseProvider.markets` 加 `us_stock` | 实测通；仅需补 `.O/.N` 码制解析（securities/search） | 小-中 |
| **P1** | **KB RAG + agent 叙事** | chain 交叉验证 / 投委会证据层 | 研报级证据召回，提升论证质量 | 中 |
| **P2** | **指标选股** | 瓶颈环节候选粗筛（供应商检索前置） | A股板块体系，可做初筛漏斗 | 中 |
| **P3** | 港股财务 / private vault | 视需求 | 港股码制待补；vault 属私有数据，需明确场景 | 视范围 |

### 3.4 硬边界（诚实标注）

- **美股一致预期不可得**（接口 A股-only）—— 美股 `consensus_eps/pe` 须走其他源或留空，不可假装有。
- **指标选股板块体系目前是 A股** —— 美股选股不走此路。
- **private vault 未验证** —— 未探测即不宣称可用。
- 所有 amount 刻度按「原始=元」假设，量级校准点在 [`_map_gangtise_financials`](../bottleneck_hunter/data_provider/providers.py#L646) 的 `1e-8`。

---

## 4. 关键接口备忘（payload 形态差异，防再次踩坑）

| 域 | 关键 payload 键 | 易错点 |
|---|---|---|
| 财务 income | `securityCode` + `period:["latest"]` + `reportType:["consolidated"]` | 字段是缩写(opRev/opCost) |
| 一致预期 | `securityCode` + `consensusList:[显式指标]` | 传 `[]` 无数值列；A股-only |
| 行情 quote | `securityList:[...]` + `fieldList` | 用 `securityList` 不是 `securityCode`(否则100003) |
| EDB | `indicatorIdList` | 不是 `indicators`(否则100003) |
| 财报日历 | `marketList:["aShares"/"hkStocks"/"usChinaConcept"/"usStocks"]` | 传 `cn` 报 100005 |
| 美股码制 | gtsCode `AAPL.O` / `NVDA.N` | 不是 `.US`(返回0行)；经 securities/search 解析 |
| agent | URL `open-ai/agent/{subpath}` + `securityCode` | earnings-review/viewpoint 需 getId→轮询 getContent≤600s |
| screener | 需先 sector-search + indicator-search 补参 | 裸 payload 缺条件报 100001 |

---

## 5. 与既有约定的一致性

- **Key 隔离**：Gangtise 全局共享是 admin「双开关」显式授权的**唯一例外**，走 `resolve_gangtise_credentials(user_id)`；其余一切仍严格按 `current_user` 解析，缺 Key 即 `MissingUserKeyError`。
- **时区**：EDB/财报日历日期按 UTC 存、北京展示。
- **验收**：本文档所有能力均以**真实返回数据**佐证，非代码桩。
