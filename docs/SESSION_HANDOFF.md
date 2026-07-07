# 会话交接记录 — BottleneckHunter（用于更换对话后接续）

> 新对话开始时：先读本文件。项目路径 `c:\Users\walker\Documents\walker\Vibecode\Bottleneck-Hunter`。
> **主线待办：下一阶段「数据整合精接」（尚未开始规划，见文末）。**
> 同一份记录也在 `C:\Users\walker\.claude\plans\velvety-wibbling-frog.md`。

---

## 一、当前系统状态

- **服务器**：正在 `http://127.0.0.1:8000` 运行（后台）。启动命令**必须** `bottleneck-hunter serve`（`python -m web.app` 不启动）。后台启动用全路径避免 PATH 127 错误：
  `(nohup "/c/Users/walker/AppData/Local/Programs/Python/Python312/Scripts/bottleneck-hunter" serve > server.log 2>&1 &)`
  重启前先杀 :8000：`netstat -ano | grep LISTENING | grep :8000 → taskkill //PID <pid> //F`
- **后端改动需重启**才生效；**前端/静态/prompt(.md) 改动只需浏览器硬刷新 Ctrl+Shift+R**。
- **大量改动未 git commit**（本会话新增/改了 ~30 个文件）。新对话可先 `git status` 决定是否提交；用户未要求提交前不要擅自提交。
- 测试全绿：`tests/test_datahub.py`(7) `test_data_source_config.py`(11) `test_macro_merge.py`(12) `test_macro_consult.py`(5) `test_market_news.py`(9)。

## 二、本会话已完成（按模块，均已上线验证）

1. **L1 宏观分析 5 点修复** — `watchlist/decision_engine.py`：`_merge_macro_results` 真双模型交叉验证（并集去重+分歧惩罚）；`macro_data.py`+`store_schema` 加 `change_pct` 列（不再丢变动率）；VIX 从 macro 归入 sentiment；日检重生复用已采集 `market_data`；补 `hk_stock` 市场上下文。
2. **L1 宏观咨询抽屉** — 新建 `watchlist/macro_consultation.py` + 3 个 prompt(`chain/prompts/macro_consult_*.md`) + `web/decision_api.py` 3 端点 + 前端 `decision.js`。两分析师（宏观市场/产业动向，复用 `L1_macro` 两 slot），滚动会话（`meeting_records` meeting_type=`macro_consult`），两周外折叠+滚动摘要；快照含观察池+持仓；抽屉两栏加宽 1600px；跨天日期分割线（淡红底白字 `<<< 🕒 >>>`）；新闻可展开中文摘要。
3. **L1 双分析师可配置** — `llm_clients/role_registry.py` L1_macro `multi_model=True,max_slots=2,slot_labels=["宏观市场分析师","产业动向分析师"]`；`ai_config_api.py` 返回 slot_labels；`ai-config.js` 用 slot_labels。
4. **模拟交易页签宽度统一** — `css/base/reset.css` `html{scrollbar-gutter:stable}` + `css/watchlist/watchlist.css` `.wl-container{width:100%}`（根因是 `.view` flex 收缩）。
5. **图表本地化（CDN 被墙根因）** — echarts/d3/marked 从 jsdelivr 改为本地 `static/vendor/*.min.js`（用 `registry.npmmirror.com` 下载）。`simtrading.js loadEquityChart` 加 `typeof echarts` 守卫。**记忆已存 `project_frontend_cdn_blocked.md`：前端库必须本地 vendor。**
6. **新用户必读图标** — `settings_api.py GET /guide`（读 `docs/新用户使用指南*.md` 最新版）+ 顶栏 book 图标 + `app.js initGuide()` marked 弹窗。
7. **邀请码修复** — 根因是 `data/auth.db` 的 `open_registration=1`（管理端开关开着，非核验 bug）；已置 0，注册恢复必须邀请码。
8. **产业链分析斑马纹动画** — `phases.js updateNav` 当前运行步骤加 `.running` 类 + `wizard.css` barber-pole 动画。
9. **快速赛道主分析模型移入详细设置** — 从卡片头移到 ctx-menu（`#ctx-model`），按赛道存 localStorage。
10. **付费数据源页签** — 新建 `web/data_source_api.py`(`/api/data-sources` catalog/save/delete/test) + `data_provider/data_source_catalog.py`(目录+probe+`resolve_data_source_key`) + `auth/store.py` `data_source_keys` 表(按 user 加密隔离) + `ai-config.js` loadPaidSources 页签。**finnhub_fetcher 已改为先查 DB 再 env。** FMP probe 用 `/stable/` 端点（`/api/v3` 已被 FMP 弃用）。**已删 OpenBB/Morningstar 卡片**，现存 7 源：fmp/finnhub/tushare/alphavantage/tiingo/polygon/custom。
11. **★ DataHub 数据升级（上一阶段主线，已完成）** — 见下节，是理解"下一阶段精接"的基础。

## 三、DataHub 架构（下一阶段精接的地基，务必先理解）

- **`data_provider/hub.py`**：`DataHub` 门面。`get_hub()` 单例。能力常量 `CAP_QUOTE/DAILY/FINANCIALS/EARNINGS/NEWS/SEC/INSTITUTIONAL/OPTIONS/INSIDER/NOTICE/SMARTMONEY`。
  - 全托管 `await hub.fetch(cap, ticker, market, user_id="")`：按 `capability×market` 选 priority 最高且未熔断的 provider，失败降级下一个，首个成功即返回（去重取单源），全程记账。`quote/daily` 委托现有 `FetcherManager`（复用其熔断），不建双层。
  - 半托管 `async with hub.track(source, cap, market) as sink: ...; sink["rows"]=n`：给直连管线记账+熔断态，异常 re-raise（不改原行为）。
  - 熔断复用 `manager.py` 常量（阈值5/冷却60s/`_NON_RETRIABLE`）。记账 `set_stats_store(store)`（app.py 注入）。
- **`data_provider/providers.py`**：`CapabilityProvider` 协议 + `FMPProvider`(us earnings)/`TushareProvider`(a earnings)。`build_providers()` 装配。**FMPProvider 目前只实现 `CAP_EARNINGS`——扩新能力就在这里加。**
- **`watchlist/earnings_pipeline.py`**：`fetch_earnings_batch` → `hub.fetch(CAP_EARNINGS)` → `store.save_earnings()`。已真实填 `earnings_reports` 空表（含真 eps_estimate 一致预期）。
- **用量表** `datasource_stats`（全局，`store_budget.py` `record_ds_call`/`get_ds_stats`/`get_ds_stats_by_source`）。
- **scheduler** 加 `job_earnings_update`(us/cn weekly) + `job_datasource_report`(daily 健康巡检)；5 条直连管线(news/sec/institutional/options/notice)+`chain/smart_money.py` 已各包 `hub.track` 记账。
- **报告页**：`web/data_report_api.py` `/overview`+`/probe` + `static/js/data-report.js` + 系统配置中心「数据报告」页签。
- **FMP key（用户的，已实测有效）**：`POXlWW7pzvaBMqtMjyLlVoQZCofm1uzW`。用 `/stable/` 端点，只认 `?apikey=` query（header 方式 FMP 返回 401）。`/stable/earnings` 一次返回 epsActual/epsEstimated/revenueActual/revenueEstimated。

## 四、下一阶段任务：数据整合精接（未开始，需先探查+确认范围）

**目标**：把更多真实数据接进分析链路，替换现存"假数据/占位/LLM推理"。

**已知的假数据消费点（精接靶子）**：
- `chain/financial_data.py` 约 :316-319 —— **把腾讯实时 PE 塞进 `consensus_pe` 冒充一致预期**；美股用 yfinance forwardPE。研报只数"近6月机构数量"(:277-286)。→ 可用 FMP `/stable/analyst-estimates` 或已填的 `earnings_reports.eps_estimate` 替换成真一致预期。
- `chain/financial_data.py` A股 akshare(:198)/美股 yfinance(:380) —— **财务深度只 ~8 季度**。→ FMP `/stable/income-statement` 可给 10年+。
- `chain/fin_models.py` :29-37 —— **DCF 假设（无风险利率/ERP/beta/增速）全硬编码**。
- **`earnings_reports` 已填真数据，但需确认有没有被 `financial_data`/深度分析/决策链路读取**（可能填了没人用）——这是第一个要查的点。

**下一阶段第一步（新对话执行）**：派 Explore 探子摸清——
1. `chain/financial_data.py` 的 `FinancialSnapshot` 字段结构、取数入口、consensus 真实取数行、**被谁消费**（grep 调用方）。
2. `earnings_reports`（含真 eps_estimate）现在有没有读取方？还是填了没人用。
3. FMP `/stable/` 能补哪些维度（income-statement/analyst-estimates/ratios/key-metrics）→ 映射到 financial_data 哪些字段。
4. deep-analysis(uzi)/supplier_eval 里哪些估值/财务字段是 LLM 推理或缺失、能被 FMP 填。
5. DataHub 加新能力（`CAP_FINANCIALS`/新增 `CAP_ESTIMATES`）+ FMPProvider 扩能力的最小改法。

**初步判断（性价比排序，待用户确认）**：
- P1：**真一致预期替换假 consensus**（FMP earnings 已有 eps_estimate，或 analyst-estimates；改 financial_data 的 consensus 取数）——直接消灭"最危险的假数据"，改动小。
- P2：**深度财务（10年 income-statement/ratios）**填 financial_data + DCF 输入，替换 ~8季度浅数据 + 硬编码假设。
- 均走 DataHub：FMPProvider 加 `CAP_FINANCIALS`/`CAP_ESTIMATES`，新建/扩 pipeline，`financial_data` 消费方改读真实源（保留免费源兜底）。

**需向用户确认**：先做 P1(一致预期) 还是 P1+P2 一起；是否新增表存深度财务，还是塞进现有 snapshot；A股(tushare)要不要同步。

## 五、环境/操作坑（新对话必知）

- **前端库禁用公共 CDN**（jsdelivr/unpkg/cdnjs/staticfile 国内全不可达），必须本地 vendor，用 `registry.npmmirror.com` 下载。
- **ruff**：`B008`(FastAPI `Depends()` 默认值) 和 `I001` 是全项目既有风格，新代码只剩这些可接受；不要为清既有 ruff 债扩大 diff。
- **TodoWrite 有环境 bug**：status 字段常被注入 `\r` 导致校验失败——可少用/忽略，用 plan 文件跟踪进度更稳。
- **记忆文件**：`~/.claude/projects/c--...-Bottleneck-Hunter/memory/`（MEMORY.md 索引）。已有：中文输出、A股分析师数量、serve启动命令、决策闭环、**前端CDN被墙**、9分改造、AI配置统一。
- 用户偏好**中文**输出；反感 over-promise（验收要真实数据证明，别用代码自证）；风格 lazy/精简（ponytail）。
- `docs/` 下有 `新用户使用指南V2.0.md`（被 /guide 端点读取）。
