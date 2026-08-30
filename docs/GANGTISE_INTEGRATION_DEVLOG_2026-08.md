# Gangtise 全域数据接入 开发日志（P0–P2）

> 完成日期：2026-08-31 · 依据：[GANGTISE_INTEGRATION_MASTER_PLAN.md](GANGTISE_INTEGRATION_MASTER_PLAN.md)
> 验收：`python -m pytest -q` → **1493 passed / 4 skipped**；六域能力均以真实返回数据佐证（非代码桩）。
> 铁律遵守：ak/sk 机密**未入任何代码/文档/日志/提交**（`.claude/` 全目录 gitignore，`.authorization` 运行时读取）；
> 受控全局 key 仅走 `resolve_gangtise_credentials` 的 admin 双开关；市场隔离 `for_market`；诚实边界不夸大。

把 Gangtise 从「A股财务单点」升级为贯穿 **chain 分析 / L1-L4 决策 / 观察池催化剂 / 投委会证据 / VIP 顾问 / 选股漏斗** 的统一投研数据底座。所有取数经**同一个 DataHub 熔断与隔离骨架**，未新建第二套管线。

---

## 架构基线（贯穿三阶段）

- **薄客户端**：`data_provider/gangtise_client.py`——无 env、token 按 (ak,sk) 进程内 30min TTL 缓存、所有失败抛 `GangtiseError`、`dict→schema` 纯函数化并配 `demo()` 自检。
- **能力即 provider**：每个 Gangtise 域 = 一个 `CAP_*` 常量（`hub.py`）+ 一个 `Gangtise*Provider`（`providers.py`），注册进 `build_providers()`，自动享受 priority/熔断/failover。业务层**零直连 HTTP**。
- **凭据受控**：`resolve_gangtise_credentials(user_id)`（`data_source_catalog.py`）——admin 双开关（`gangtise_enabled` + `gangtise_global_shared`）是**per-user 隔离的唯一显式例外**；缺 key 返回 None 静默走下个 provider，绝无全局 env key。
- **消费门面**：`chain/evidence.py` 统一格式化研报/KB/叙事文本块，任一异常/空结果**降级为空串**，绝不阻断主流程——纯增量、零回归。

---

## Phase 0 — 宏观 + 催化剂（填最大真缺口）

**EDB 宏观 → L1**
- `gangtise_client.fetch_edb`（payload 键 `indicatorIdList`）+ `gangtise_edb_indicators.py`（CPI/PPI/利率/PMI/社融 curated 指标表，`indicators_for_market` 按市场取）+ `GangtiseProvider` 增 `CAP_MACRO_EDB`。
- **接入点**：`decision_engine._inject_edb_macro`——`generate_macro_strategy` 前取 EDB 关键指标就地并入 macro 段并落 `macro_snapshot`。EDB 官方口径（中国官方 PMI/社融同比）**覆盖同 key** 的 yfinance/FRED 兜底，落库用 EDB 真实 `as_of` 日期（防日期臆造）。
- 指标值变换：identity 原值 / index100（同比=值−100）/ change_pct=最新−前值；美/中指标严格 `for_market` 隔离。

**财报日历 + 公告 → 催化剂**
- `fetch_performance_calendar`（`marketList` 枚举，**跨月按月分段**规避 `110003 TIME_RANGE_EXCEEDED`）+ `fetch_announcements`（A/H/US 三 URL）。
- **接入点**：scheduler 系统级 job `job_gangtise_catalyst`——对观察池全量 ticker 拉未来日历 + 近期公告，`_gangtise_catalyst_meta` 映射为催化剂字段，按 `securityCode+event_date+type` **幂等 upsert**（同日重跑不重复）。
- **VIP 持仓催化剂（§9 P0）**：VIP 美股持仓经 `_resolve_gts_code` 解析 `.O/.N` 拉 `announcement-us`，写入 `for_user(vip_sub).for_market("us_stock")`，仪表盘「持仓催化剂」不再恒空。

---

## Phase 1 — 研报证据层 + 美股财务

**研报（中/外资）+ KB RAG**
- `fetch_research`（`broker-report`/`foreign-report` 双 URL，`categoryList`+`llmTagList` 两分类轴，**按月分段**）+ `fetch_kb`（语义片段）+ `GangtiseProvider` 增 `CAP_RESEARCH`/`CAP_KB`。
- **接入点**：
  - chain 交叉验证（`cross_validation.py`）：`_build_financial/chain/sentiment_prompt` 增 `evidence` 形参，`gather_evidence` 召回研报摘要 + KB 片段作「反方论据」注入，把「凭模型记忆质疑」升级为「据研报质疑」。
  - 投委会（`committee.py` + 4 persona prompts）：委员预读材料附最新券商观点。

**美股财务放开（§5.2）**
- `_resolve_gts_code` 经 `securities/search` 解析美股 `.O/.N` 码并缓存；`GangtiseProvider.markets()` 加 `us_stock`；`_map_gangtise_financials` 加 `currency`（CNY/USD）字段、刻度校准点 `1e-8`。
- **硬边界**：美股一致预期不可得（`earning-forecast` A股-only，返 120001）→ `consensus_eps/pe` 留空，`ponytail:` 标注，不假装。

**VIP 顾问增据（§9 P1）**
- `advisory.gather_holdings_evidence` 复用 `chain.evidence.gather_evidence`，为逐仓「减/持/加」建议注入各持仓研报 + KB 证据，建议**有据可引**。
- beta/benchmark：A股指数（000300.SH）可经 Gangtise 兜底；**US/HK 指数历史行情实测全 400 无端点**，取不到则 beta 诚实为 0 不造假（§10 硬边界）。

---

## Phase 2 — AI 叙事增强 + 指标选股漏斗（本阶段完成）

**AI 研报叙事（一页通）**
- `gangtise_client.fetch_narrative`（`open-ai/agent/{subpath}` + `securityCode`）——**仅接 3 个同步 agent**（one-pager/investment-logic/peer-comparison）；异步 600s 轮询的 earnings-review/viewpoint-debate 按 §10「调用重、默认关」以 `ponytail:` 标注上升路径，未接。
- `GangtiseNarrativeProvider`（独立 name，`CAP_NARRATIVE`，A/US 市场）——单列不挂核心财务熔断。
- 门面 `evidence.gather_narrative` + `report.generate_report(extra_sections=)`（默认 `""`，11 处调用零改动，report.py 保持同步/无网络）+ `streaming/legacy.py` **默认关 opt-in**（`getattr(config,"include_narrative",False)`，仅首个 top pick 附一页通）。

**指标选股漏斗**
- `gangtise_client.screen`（`open-indicator/screener`，`universe`/`expression`/`indicatorList` 三者缺一短路空）+ `gangtise_sector_ids.py`（中信一级行业 curated 板块表 + 别名归一，未收录返回 None 静默降级）。
- `GangtiseScreenProvider`（`CAP_SCREEN`，A股-only）——板块内以 `pty_main_bus contains '<关键词>'` **零日期参数**粗筛（规避 tradeDate/日 K 脆弱性）。
- **接入点**：`supplier_search.search` 新增第 4 路源 `_gangtise_source`（A股 only），gts 码经 `_code_to_ticker` 归一后并入去重合并（优先级 LLM>chain>gangtise>akshare），缩小 chain 深挖候选集。

**P2 真实数据验收**：半导体板块 → 66 家真实候选（通富微电/华工科技等）；贵州茅台 600519 一页通 → 1617 字真实叙事（含机构目标价/观点）。

---

## Phase 3 —（YAGNI，按方案刻意不建）

港股财务 / private vault：无明确场景，**不预建**（方案 §10）。

---

## 横切质量

- **熔断/failover**：全走 DataHub，异常抛出交 hub，provider 内不吞。
- **时区**：EDB/日历/公告日期 UTC 存、`fmtBJ` 北京展示、催化剂 `event_date` 归一北京日历。
- **归属分离**：取数走 admin 授权 key，催化剂/研报/建议落对应用户观察池（`for_user`）。
- **自检**：每个新客户端函数 + 纯映射 + provider 能力声明均配 `demo()`/断言（`gangtise_client`/`gangtise_sector_ids`/`gangtise_edb_indicators`/`evidence`/`providers` 全绿）。
- **lint**：新增文件 ruff 全通过（仓库存量 E501 为历史遗留，未在本次触碰的行引入新错）。

---

## 边界与已知限制（诚实标注）

| 项 | 结论 |
|---|---|
| 美股一致预期 | ❌ 接口 A股-only，留空不假装 |
| 美股/港股指标选股 | ❌ 板块体系目前 A股 |
| US/HK 指数历史行情 | ❌ 实测全 400 无端点，US/HK beta 取不到诚实为 0 |
| agent 异步叙事（财报点评/观点辩论） | ⚠️ 600s 轮询、默认关，未接（`ponytail:` 标上升路径） |
| private vault | ⚪ 未验证，不接入直到有明确场景 |
