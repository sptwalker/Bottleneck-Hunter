# efinance 数据源引入·升级开发计划（2026-08）

> 目标：找出本系统所有可用 efinance（东方财富）补强的数据缺口，给出可落地的分阶段开发计划。
> 结论先行：**efinance 已安装、但只接了「日K线 + 实时行情」两个能力**，东财最有价值的
> 「A股机构/股东持仓、主力资金流、板块标签、业绩预告」四类 API **一个都没接**——
> 而这四类恰好对应系统里 4 个「恒空 / 靠 LLM 编 / 已失效」的真实缺口。

---

## 0. 现状盘点（已核实）

### efinance 当前接入面
- 唯一接入点：[efinance_fetcher.py](../bottleneck_hunter/data_provider/fetchers/efinance_fetcher.py) —
  `EfinanceFetcher`（priority=0，A股首选），**只实现** `fetch_daily`（`ef.stock.get_quote_history`）
  与 `fetch_realtime`（`ef.stock.get_realtime_quotes`，取 PE/换手/市值）。
- 注册：[data_provider/__init__.py:37](../bottleneck_hunter/data_provider/__init__.py) 进 FetcherManager 的 quote/daily 链路。
- **DataHub 能力体系**（[hub.py](../bottleneck_hunter/data_provider/hub.py)）里 efinance 没有任何 CapabilityProvider——
  它只活在 quote/daily 的 FetcherManager 里，够不到 financials / institutional / smartmoney 等能力路由。

### efinance 真实可用 API（已 `dir()` 核实，全部存在）
| 函数 | 返回 | 对应缺口 |
|---|---|---|
| `ef.stock.get_top10_stock_holder_info(code, top=N)` | 十大股东（近 N 期） | §4 A股机构/股东 **全空白** |
| `ef.stock.get_latest_holder_number(date=None)` | 全市场股东人数变化 | §4 股东集中度趋势 |
| `ef.stock.get_history_bill(code)` | 个股历史主力/超大单/大单净流入 | §2 主力资金流（akshare 已失效） |
| `ef.stock.get_today_bill(code)` | 个股当日资金流 | §2 当日资金面 |
| `ef.stock.get_belong_board(code)` | 个股所属东财板块/概念（官方分类） | §3 LLM 自报 sector 无校验 |
| `ef.stock.get_all_company_performance(date)` | 全市场某季度业绩报表 | §1 催化剂事实锚点 |
| `ef.stock.get_all_report_dates()` | 财报预约披露日 | §1 催化剂 expected_date 兜底 |
| `ef.stock.get_base_info(codes)` | 所属行业/ROE/净利率/毛利率/总市值 | §3/§5 冗余增强 |
| `ef.stock.get_daily_billboard(...)` | 龙虎榜明细 | §2 备选（akshare 已有粗口径） |

---

## 1. 缺口地图（哪里能用 efinance 补，为什么）

| # | 缺口 | 现状 | 严重度 | efinance 补法 |
|---|---|---|---|---|
| **G1** | **A股机构持仓 / 十大股东 / 基金持仓** | [institutional_pipeline.py](../bottleneck_hunter/watchlist/institutional_pipeline.py) **仅美股**（yfinance 13F）；[decision_engine.py `_positioning_signals`](../bottleneck_hunter/watchlist/decision_engine.py) 对 A股「返回空」；[committee.py](../bottleneck_hunter/watchlist/committee.py) 恒回「暂无持仓集中度数据」；[models.py `dim_institution`](../bottleneck_hunter/chain/models.py) 注释「A股无数据时为None」，[supplier_eval.py](../bottleneck_hunter/chain/supplier_eval.py) 机构维度 `is_us and ...` **A股 25% 权重维度直接失效** | 🔴 最高 | `get_top10_stock_holder_info` + `get_latest_holder_number` → 新 CapabilityProvider 填 `CAP_INSTITUTIONAL`（A股） |
| **G2** | **A股主力资金流 / 北向** | [smart_money.py `_track_astock`](../bottleneck_hunter/chain/smart_money.py) 用 `ak.stock_individual_fund_flow`；北向 `ak.stock_hsgt_individual_em` **代码自注「2024-08后数据可能不可用」**；[macro_data.py](../bottleneck_hunter/watchlist/macro_data.py) 北向同样失效 | 🔴 高 | `get_history_bill`/`get_today_bill` 直接替代已失效的 akshare 主力资金口径 |
| **G3** | **A股行业/板块标签无事实校验** | [supplier_search.py](../bottleneck_hunter/chain/supplier_search.py) LLM 推荐分支 `sector=item.get("sector")` **原样落库、零校验**；下游 catalyst/cross_validation 直接消费 | 🟡 中 | `get_belong_board` 回填/校验东财官方板块标签 |
| **G4** | **催化剂无真实事件源** | [chain/catalyst.py](../bottleneck_hunter/chain/catalyst.py) **100% LLM 编** events+expected_date，urgency 反哺 alpha 加分；[watchlist/catalyst_monitor.py](../bottleneck_hunter/watchlist/catalyst_monitor.py) 生成端 LLM 套 LLM | 🟡 中 | `get_all_report_dates`/`get_all_company_performance` 给 expected_date 加事实锚点 |
| **G5** | 财务字段冗余增强 | [financial_data.py `_fetch_astock_financial`](../bottleneck_hunter/chain/financial_data.py) akshare 同花顺已较全 | ⚪ 低 | `get_base_info` 冗余兜底，非必需 |

> 说明：**§3 集中度（CR3/HHI）不是缺口** — [industry_concentration.py](../bottleneck_hunter/chain/industry_concentration.py)
> 已用 akshare 板块成分股算真实 CR3/CR5/HHI 作为事实锚点。efinance 在此只是「交叉校验」，不新建。

---

## 2. 架构落点：全部走 DataHub CapabilityProvider，零新抽象

现有 [hub.py](../bottleneck_hunter/data_provider/hub.py) 的能力-提供者模型已经具备：优先级路由、熔断、按用户 Key 隔离、计量。
**efinance 是免费源、无需 Key**，天然适合当「A股侧的默认 provider」挂在现有 US 源旁边。

统一约束（照抄现有 provider 写法）：
- 实现 `CapabilityProvider` Protocol（`name/priority/capabilities()/markets()/supports()/async fetch()`），在 [providers.py `build_providers()`](../bottleneck_hunter/data_provider/providers.py) 注册。
- efinance 是**同步阻塞** → provider 内一律 `await asyncio.to_thread(ef.stock.xxx, code)`（同 smart_money 现有写法）。
- 东财端点在国内**间歇不可达** → 每个 fetch `try/except` 返回 `None`，靠 hub 熔断降级，绝不抛穿。
- A股代码提取复用全系统唯一入口 `store_base.extract_astock_code`（efinance_fetcher 已在用）。
- `markets() = {"a_stock"}`，priority 排在现有免费源之后（不夺 quote/daily 主路）。

---

## 3. 分阶段开发计划

### Phase 1 — A股机构/股东持仓 provider（G1，价值最高）🔴

**新文件** `bottleneck_hunter/data_provider/providers_efinance.py`（或并入 providers.py 尾部）：

```python
class EfinanceInstitutionalProvider:
    name = "efinance_inst"
    priority = 50               # A股唯一源，无竞争；排在美股 provider 之外
    def capabilities(self): return {CAP_INSTITUTIONAL}
    def markets(self): return {"a_stock"}
    def supports(self, cap, market): return cap == CAP_INSTITUTIONAL and market == "a_stock"
    async def fetch(self, cap, ticker, market, user_id):
        code = extract_astock_code(ticker)
        if not code: return None
        df = await asyncio.to_thread(ef.stock.get_top10_stock_holder_info, code, 4)
        # → 规整成现有 institutional 结构：持股比例/机构名/环比
        return {...}   # 对齐 store_market_data 里美股 13F 的落库形状
```

改动点：
1. `providers_efinance.py` 新 provider（上）。
2. [providers.py `build_providers()`](../bottleneck_hunter/data_provider/providers.py) 注册它。
3. [institutional_pipeline.py](../bottleneck_hunter/watchlist/institutional_pipeline.py)：A股分支从「跳过」改为走 `hub.fetch(CAP_INSTITUTIONAL, ...)`；落库形状对齐美股 13F（[store_market_data.py](../bottleneck_hunter/watchlist/store_market_data.py)）。
4. [scheduler.py](../bottleneck_hunter/watchlist/scheduler.py) `_do_us` 机构 job 放开 A股（去掉「无美股标的跳过」硬门）。
5. [supplier_eval.py](../bottleneck_hunter/chain/supplier_eval.py) `dim_institution` 的 `is_us and ...` 门 → A股有数据时也生效，回填 [models.py](../bottleneck_hunter/chain/models.py) `institution_holding_pct`。

**验证**：`python -m ...providers_efinance`（demo：真拉一个如 `600519` 断言返回非空且含持股比例字段；网络不可达时断言优雅返回 None 不抛）；`committee.py` 对 A股标的不再恒回「暂无持仓集中度数据」。

---

### Phase 2 — A股主力资金流 provider（G2）🔴

现有 `smart_money.py` 的 akshare 主力资金/北向已知失效。新增 `CAP_MONEYFLOW` 能力（hub.py 加常量），provider：

```python
class EfinanceMoneyFlowProvider:
    name = "efinance_flow"; priority = 50
    def capabilities(self): return {CAP_MONEYFLOW}
    def markets(self): return {"a_stock"}
    async def fetch(self, cap, ticker, market, user_id):
        df = await asyncio.to_thread(ef.stock.get_history_bill, code)  # 主力/超大单/大单净流入
        return {"main_net_5d": ..., "series": ...}
```

改动点：
1. [hub.py](../bottleneck_hunter/data_provider/hub.py) 加 `CAP_MONEYFLOW = "moneyflow"`，并入 `available_capabilities()`（顺带让 AI 协商层能主动申请）。
2. provider 注册。
3. [smart_money.py `_track_astock`](../bottleneck_hunter/chain/smart_money.py) §1 主力资金流：优先 `hub.fetch(CAP_MONEYFLOW)`，失败再落回 akshare（**不删旧路，做 fallback**）。北向口径同理用 efinance 兜。
4. 字段沿用 [models.py](../bottleneck_hunter/chain/models.py) `fund_flow_net`，无需改模型。

**验证**：demo 断言 `600519` 主力净流入序列非空；smart_money_score 在 A股路径有真实资金因子（不再依赖失效 akshare）。

---

### Phase 3 — A股板块标签校验（G3）🟡

**轻量、无新表**。在 [supplier_search.py](../bottleneck_hunter/chain/supplier_search.py) LLM 推荐分支落库前，对 A股候选调 `get_belong_board` 回填/校验 `sector`：

```python
# LLM 自报 sector → 东财官方板块校验（仅 A股候选）
if market == A_STOCK and code:
    boards = await asyncio.to_thread(ef.stock.get_belong_board, code)
    if boards is not None and not boards.empty:
        sector = boards.iloc[0]["板块名称"]   # 用东财官方分类覆盖 LLM 自报
```

- 加节流/并发上限（复用 smart_money 的 `asyncio.Semaphore` 模式）。
- 失败保留 LLM 原值（增强非阻断）。

**验证**：一个已知标的（如宁德时代 300750）断言回填出「电池/锂电池」类真实东财板块。

---

### Phase 4 — 催化剂事实锚点（G4）🟡

给 [catalyst.py](../bottleneck_hunter/chain/catalyst.py) / [catalyst_monitor.py](../bottleneck_hunter/watchlist/catalyst_monitor.py) 的 LLM `expected_date` 兜真实来源：

- 拉 `get_all_report_dates()`（财报预约披露日）作为「已排定事件」注入 catalyst 输入，让 LLM 在**真实日期锚点**上生成，而非凭空编 date。
- 可选：`get_all_company_performance(date)` 业绩预告作为 event 事实源。
- **不改评分权重**，只把「事实事件」加进 prompt 上下文 + 在 event 上标 `source="efinance_report_date"` 供下游区分事实/推测。

**验证**：断言对有预约披露日的标的，catalyst events 中出现 source 标记为事实的条目。

---

### Phase 5 — 财务冗余增强（G5）⚪ 按 YAGNI 暂缓

akshare 同花顺财务已较全（[financial_data.py](../bottleneck_hunter/chain/financial_data.py)）。`get_base_info` 仅作**单点故障兜底**候选——
除非 akshare 财务在生产被证明频繁失败，否则**不建**。留此条目仅为记录，不排期。

---

## 4. 优先级与工作量

| Phase | 缺口 | 优先级 | 预估改动 | 一句话价值 |
|---|---|---|---|---|
| P1 | A股机构/股东 | 🔴 最高 | 1 新文件 + 5 处接线 | 唯一「完全空白+恒空兜底」，让 A股 25% 机构维度复活 |
| P2 | A股主力资金流 | 🔴 高 | 1 provider + hub 加能力 + 1 fallback | 替换已失效 akshare 资金口径 |
| P3 | 板块标签校验 | 🟡 中 | supplier_search 内 ~10 行 | LLM 自报行业 → 东财官方分类 |
| P4 | 催化剂锚点 | 🟡 中 | catalyst 输入注入 | 给 LLM 编的 expected_date 兜真实来源 |
| P5 | 财务冗余 | ⚪ 低 | — | 暂不建（YAGNI） |

**建议先做 P1+P2**（两个 🔴，共用同一套 provider 脚手架，一次 PR 可交付），P3/P4 视效果再排。

## 5. 统一收尾约定
- 每个 provider 留一个 `demo()`/`__main__` 自检（真拉一只 A股 + 断网优雅降级两条断言）。
- 提交 message 写一行 `📢` 白话进首页更新历史。
- 全程不加新依赖（efinance 已装）、不建新抽象（复用 DataHub provider 脚手架）、A股免费源无需 Key 隔离。
