# Gangtise 二期接线 开发日志（P0 止血 + P1 高价值零成本）

> 完成日期：2026-08-24 · 依据：`gangtise-phase2-wiring.md`（性价比驱动方案）
> 验收：`python -m pytest -q` → **1493 passed / 4 skipped（236s）**；五项改动均以**真实返回数据**佐证（活体实测，非代码桩）。
> 铁律遵守：ak/sk 机密**未入任何代码/文档/日志/提交**（`.claude/` 全目录 gitignore，`.authorization` 仅运行时读取，验证用完即弃）；
> 受控全局 key 仅走 `resolve_gangtise_credentials` 的 admin 双开关；市场隔离 `for_market`；**实测证伪的能力诚实留缺省，不假装接通**。

一期（[GANGTISE_INTEGRATION_DEVLOG_2026-08.md](GANGTISE_INTEGRATION_DEVLOG_2026-08.md)）把 Gangtise 铺成贯穿全系统的六域数据底座。二期基于新读的《产品概述》《接口计分标准》两份官方文档，按 **价值÷积分成本** 重排优先级：先**止血 3 个正在烧钱/报错的活体 bug**，再接 **2 个高价值零成本新能力**。全部改动零新建管线——复用既有 DataHub 熔断/隔离骨架与凭据双开关。

---

## 计价关键结论（决定优先级）

- **免费池（0 积分/滚动 3 年）**：行情（实时/日K/分钟/复权/**A股资金流**）、三大报表（**A/港/美**）、主营构成、**估值分析**、前十大股东。
- **收费池**：EDB 宏观 **30 积分/指标/次**、盈利预测 0.5/交易日指标、Agent 50/次、KB 10/次、研报下载 10~50/条、题材 500/次（最贵）。
- 试用号仅 20000 积分/月 → **结构化数据需求全压免费池**，预算只留给真正高价值的收费项。这条直接决定了 P0-1（EDB 止血）为最紧迫项。

---

## P0 — 止血 & 修断路（全部免费，修复现存 bug）

### P0-1　EDB 宏观积分止血（最紧迫）

- **病灶**：`decision_engine._inject_edb_macro` 每次 L1 运行都重取 EDB——美股 7 指标×30=**210 积分/轮**，A股 4×30=**120/轮**。宏观是月频数据，每轮重抓每轮得同值 → 纯烧分，试用号几天烧光。
- **改法**：注入前先过「重取节流窗」。新增 `_edb_cache_fresh(store, market)`——查 `macro_snapshots` 中该市场全部 EDB key 的 `fetched_at`；**全部**在 `_EDB_REFRESH_DAYS=25` 天内则返回可直接注入的缓存 dict，`_inject_edb_macro` 命中即复用、跳过计费取数；任一 key 从未落库或超窗则整组重取（保持同批 `as_of` 一致）。
- **收益**：EDB 调用从「每轮」降到「约每月一次」，**省 ~95% 积分**。改动集中在 `decision_engine.py`，不碰 provider/hub。
- **诚实边界**：缓存复用同样**覆盖** yfinance/FRED 兜底口径（EDB 官方口径优先级更高），与真打路径的注入语义完全一致。
- **验证**：单测覆盖「全新鲜→跳过 / 任一陈旧→真取 / 半桶缓存→真取补全 / fetched_at 不可解析→保守判过期」；真实连一次确认落库 `as_of` 正确。

### P0-2　修 beta=0（指数专用 K 线端点）

- **根因（活体证伪旧判断）**：`fetch_quote_history` 只打统一 `open-quote/kline/daily`；`scheduler._GTS_BENCHMARK_CODE` 硬编码只有 `{"000300.SS":"000300.SH"}`，旧注释断言「US/HK 指数一律 400，硬边界」。**实测推翻**：统一端点对**指数**返回 0 行（个股正常），指数必须走 `open-quote/index/kline/daily`。所谓「Gangtise 无 US/HK 指数」实为**端点路由缺失**，非能力缺失。
- **改法**：
  1. `gangtise_client.fetch_quote_history` 增 `is_index` 参数——`True` 走 `_QUOTE_INDEX_DAILY_URL`，`False` 走统一端点（个股）。
  2. `scheduler._GTS_BENCHMARK_CODE` 扩到四市场基准，`_gangtise_benchmark_backfill` 传 `is_index=True`。删除全部「US/HK 一律 400」旧注释，替换为实测端点说明。
- **真码活体实测（2026-08）**：`^GSPC→SPX.SPI`（标普500）、`^IXIC→IXIC.O`（纳指综合）、`000300.SS→000300.SH`（沪深300）、`^HSI→HSI.HI`（恒指）——四条经 `securities/search` + index 端点均取到真实收盘序列。
- **验证**：真实取美/A/港基准近 4y 收盘 → 落 `market_snapshots`（用 yfinance 码对齐下游查询键）→ `_portfolio_risk_summary` 可算出非零 `portfolio_beta`。

### P0-3　修 美股/港股财务路由 bug

- **根因**：`fetch_financials` 对所有市场都打 `income-statement/accumulated`（A股累计口径端点），而 `GangtiseProvider.supports(CAP_FINANCIALS)` 早已声明 `us_stock` → 美股财务实际打错端点返 0 行。**这正是此前误判「美股财务只能 FMP」的代码根因**——实为 Gangtise 有 US/HK 三表，只是路由缺失。
- **改法**：
  1. `gangtise_client._INCOME_URL_BY_MARKET` 三端点映射（A股 accumulated / 美股 us / 港股 hk），`fetch_financials` 按 `market` 选端点，未知市场保守退 A股端点。`_INCOME_URL` 保留向后兼容。
  2. `_map_gangtise_financials` 字段校准（活体实测三市场字段）：净利归母取值链 `netProfitAttrParent`（A/港）`or netProfitParent`（美）`or netProfit`；毛利率优先用接口直供 `grossProfit`（美/港有），A股无则「营收−营业成本」自算。金额刻度三市场同 `1e-8`（元→亿，实测量级成立）。
- **验证**：真实取 TSLA.O 利润表 → 映射出非空 `revenue_yi/net_profit_yi`，量级合理。

---

## P1 — 高价值零成本新能力（均免费、有真实消费者）

### P1-1　估值分位　CAP_VALUATION（免费，valuation-analysis）

- **价值**：PE/PB/PEG 的**近 3 年历史分位**（percentileRank 0~100，越低越便宜）——yfinance 只给当前 PE，给不出「贵/便宜」的历史锚。这是价值投资人 persona 估值论证的锚点、L2/L3 择时输入。
- **改法**：
  1. `gangtise_client.fetch_valuation(ak,sk,ticker,market,indicators=[peTtm,pbMrq,peg],years=3)` → 每指标 `{value, percentile, as_of}`（`_parse_valuation_body` 取末行=最新交易日，跳过尾部空值行）。
  2. `hub.py` 增 `CAP_VALUATION="valuation"`。
  3. `providers.py`：`_map_gangtise_valuation` 纯映射（可单测）→ 规范键 `pe_ttm/pb_mrq/peg + *_percentile + as_of`；`GangtiseProvider` 认领 CAP_VALUATION，`supports` **仅 a_stock**（实测美股/港股/指数 `code=120001` 无覆盖，诚实不认领）。
  4. **消费者**：`committee._gangtise_valuation_percentiles`（A股、同步 best-effort）在 `build_ticker_background` 的 `valuation_data` 段 `.update()` 分位字段，缺则不加、不破坏现有 yfinance 字段，非 A股静默跳过。
- **验证**：真实取 600519 估值分位（peTtm 分位 17.24 / pbMrq 10.49）→ 注入 committee 背景；纯映射单测（`_map_gangtise_valuation` + `_parse_valuation_body`）。

### P1-2　A股资金流向（免费，fund-flow/daily）

- **价值**：主力净流入（`mainNetInflow`）/大单/特大单——情绪与筹码信号。**生产机房 akshare 被墙**，Gangtise 是该数据的可达替代源。
- **改法**：
  1. `gangtise_client.fetch_fund_flow(ak,sk,ticker,start,end)` → `[{date, main_net, large_net, xlarge_net}]`（单位**元**、升序；`_parse_fund_flow_body` 缺列缺省 None）。
  2. 接入 `chain/smart_money._track_astock` 作 **A股 Gangtise 兜底源**——在 efinance→akshare 链之后、`total_flow is None` 时取近 5 交易日主力净流入求和，**元→万**（÷1e4）并入既有 `SmartMoneySignal.fund_flow_net`。不新增 CAP（复用聪明钱管线），akshare 可用时行为完全不变。
- **验证**：真实取 600519 资金流 16 行 → 末日主力净流入 −2806 万（元/1e4 换算正确）；`tests/test_smart_money.py` 21 项回归全绿（akshare 路径不受影响）。

---

## P2 — 明确不接（YAGNI / 低性价比，记录上升路径）

保持一期结论：主营构成 / 前十大股东 / 实时行情 / 分钟K（无紧迫消费者，暂缓）；管理层讨论 10/次、KB 扩用、Agent 异步 50/次（收费，opt-in）；题材 500/次（最贵，chain 拆解已自产，不接）。

---

## 验收纪律遵守情况

1. **ponytail**：五项均最小 diff、复用既有 provider/hub/熔断/凭据双开关，无第二套管线；非平凡逻辑均带 `_demo`/单测自检。
2. **机密**：ak/sk 全程只运行时从 gitignored `.authorization` 读，未入任何代码/文档/日志/commit；活体验证用完即弃。
3. **全量测试**：每项完成即跑相关测试文件，全部完成后跑全量 `pytest`（结果见文首）。
4. **真实数据佐证**：beta 端点、美股财务、估值分位、A股资金流均以**活体返回数据**证明，非代码自证。
5. **诚实边界**：估值分位/资金流实测仅 A股覆盖 → provider `supports` 只认领 a_stock，不假装全市场；未映射的基准码诚实缺省不臆造端点。

---

## 变更文件清单

| 文件 | 项 | 改动摘要 |
|---|---|---|
| `watchlist/decision_engine.py` | P0-1 | `_edb_cache_fresh` 节流窗 + `_inject_edb_macro` 缓存优先注入 |
| `data_provider/gangtise_client.py` | P0-2/3, P1-1/2 | `is_index` 路由 / 财务按市场端点 / `fetch_valuation` / `fetch_fund_flow` + 三组解析自检 |
| `data_provider/hub.py` | P1-1 | `CAP_VALUATION` 常量 |
| `data_provider/providers.py` | P0-3, P1-1 | 财务字段三市场校准 / `_map_gangtise_valuation` + GangtiseProvider 认领 CAP_VALUATION |
| `watchlist/scheduler.py` | P0-2 | `_GTS_BENCHMARK_CODE` 扩四市场 + `is_index=True` 兜底 |
| `watchlist/committee.py` | P1-1 | `_gangtise_valuation_percentiles` 注入投委会估值段 |
| `chain/smart_money.py` | P1-2 | A股资金流 Gangtise 兜底（元→万） |

---

## 代码审查与整改（子代理审查 → 全部修复）

五项完成后以代码审查工具全面复审，命中 3 处并全部整改（整改后全量 `pytest` 复跑 **1493 passed / 4 skipped / 223s**，与整改前基线一致，非回归）：

| 级别 | 问题 | 修复 |
|---|---|---|
| High | `providers._demo()` 断言仍为旧四能力集，漏 `CAP_VALUATION` → 触发 AssertionError 致 `_demo()` 半途中断（未入 pytest，生产无损但自检失真） | 断言补 `CAP_VALUATION`；新增 `supports(CAP_VALUATION)` 仅 A股 + `_map_gangtise_valuation` 映射自检；`_demo()` 通过 |
| Low | 投委会注入的裸 `peg` 会覆盖上游 yfinance 的 `peg`，违反"不破坏现有 yfinance 字段"约定 | Gangtise 的 `peg` 改名 `peg_gts`；`pe_ttm/pb_mrq` 与 yfinance 键不同名，安全叠加；`*_percentile` 保留 |
| Low | EDB 节流以"全指标齐备"为门 → 若某指标 Gangtise 永久无覆盖，节流永不生效、每轮重复付费（US 210/CN 120 分）却拿不到那条 | 改判"批次新鲜度"（最近一次 `fetched_at` 在窗内即复用）；缺失键交下游 yfinance/FRED 兜底；6 分支隔离测试通过（含 partial-fresh→HIT 覆盖键、混合新鲜度取最新→HIT） |

