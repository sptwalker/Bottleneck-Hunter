# Gangtise 补充接线方案 — fetcher 路径（行情/日K）+ 候选审计

> 定位：这是 [GANGTISE_INTEGRATION_MASTER_PLAN](GANGTISE_INTEGRATION_MASTER_PLAN.md) 的**补充**。
> master plan 覆盖 **hub 路径**（`hub.fetch(cap)` → `GangtiseProvider`，已在产 5 能力：
> FINANCIALS / MACRO_EDB / RESEARCH / KB / VALUATION）。本补充只处理 master plan **未覆盖的第二条
> dispatch 路径**——`FetcherManager`（CAP_QUOTE / CAP_DAILY 行情），并对其余候选做诚实审计。

日期：2026-08-31 · 分支：main · 授权：用户「出方案即实施」同句授权，出完即开工。

---

## 0. 一句话结论

DataHub 有两条 dispatch 路径，Gangtise 只接了 hub 一条；**fetcher 一条（行情/日K）为空——这是唯一真缺口**。
本补充新建 `GangtiseFetcher`，以**严格最高优先级**插入美股链（先于 yfinance）与 A股链（先于 efinance），
让行情/日K 优先走 Gangtise（admin 共享 key、境内可达、官方口径、免费不限流），前者故障自动降级到现有免费源。
其余 5 个候选经审计**已在产或刻意缓建**，不制造无消费者的工作。

---

## 1. 架构事实（已勘察定论，勿重复调查）

| 事实 | 出处 |
|---|---|
| DataHub 两条 dispatch：**hub 路径**（`hub.py`/`providers.py`）与 **fetcher 路径**（`manager.py`） | `hub.py:49` `_MANAGER_CAPS={CAP_QUOTE,CAP_DAILY}` |
| hub 路径 Gangtise 已覆盖 5 能力 | `providers.py:743` `GangtiseProvider.capabilities()` |
| **fetcher 路径 Gangtise 为空**——`_MANAGER_CAPS` 只挂 efinance/akshare/pytdx/baostock/yfinance/akshare_us/finnhub/alphavantage | `__init__.py:_create_manager` |
| 选源顺序 `order()`：丢超额源 → `(priority, recent_load)` 升序；**priority 值小=质量更高=最先试** | `scheduler.py:126` |
| 免费源不入 `_DEFAULT_QUOTA` → 永不限流；Gangtise 不入表 → 免费不掐断 | `scheduler.py:28` |
| 熔断：`_NON_RETRIABLE=(ValueError,KeyError,TypeError)` 不计数；阈值 5 / 冷却 60s | `manager.py:19` |
| A股量能归一 `clean_ohlc`：用 `r=amount/(close·vol)` 数据反推「手/股」，**fetcher 必须透传 amount** | `cleaning.py:25` |
| 凭据 `resolve_gangtise_credentials(user_id)`：admin 双开关授权共享，缺 → 返回 None（触发 fallback，绝不 raise） | `data_source_catalog.py:306` |
| 客户端 `fetch_quote_history` 现有消费者（VIP beta）依赖 **close-only** dict 签名，OHLCV 扩展必须**加法式** | `scheduler.py:263` |
| 行情字段常量在 skill `quote.py`：`_FIELD_LIST`(OHLCV+amount) / `SNAP_FIELD_LIST`(实时快照) | `.claude/skills/gangtise-data/scripts/quote.py:36,91` |
| 实时端点 `QUOTE_REALTIME_URL = .../open-quote/quote/realtime`，payload `{"fieldList":SNAP_FIELD_LIST,"securityList":[...]}` | skill `utils.py:91` |
| 端点路由：个股（美/港/A）统一 `open-quote/kline/daily`；指数走 `index/kline/daily`（fetcher 只管个股，不涉指数） | `gangtise_client.py:63` |

---

## 2. 「更高优先级」的精确落法（严格最高优先级，非同档轮询）

`order()` 对同 priority 用 `recent_load` 轮换——若 Gangtise 与 yfinance/efinance **同为 0**，manager 首次
命中 Gangtise 后 `note_call` 抬高其 `recent_load`，**下一次就轮到 yfinance**，退化成 ~50/50 轮询，
**不是**「先于」。用户明确「置于更高优先级」= 严格最先试。

**落法（最小 diff）**：`GangtiseFetcher.priority = -1`（`order()` 升序，`-1 < 0` 恒排最前，与 `recent_load`
无关）。既有 8 源 priority **一律不动**——负值即严格最高档，无需「整体下移」的 8 处改动，既有降级链
100% 原样、零回归。Gangtise 故障（连续 5 次）熔断 60s 自动降级到 efinance/yfinance，兜底链完好。

> 为何不用 `priority=0` + 整体下移：那需改 8 个无关 fetcher 的 priority，是更大的 diff 与回归面；
> 负优先级用 1 个新类属性达成同样的「严格最高」语义，是真正的 ponytail 最小改动。`priority` 无下界
> 假设（`scheduler.order` 纯升序排序、`get_status` 仅展示排序），负值安全。

---

## 3. 候选优先级排序与实施顺序

| # | 候选 | 结论 | 工作 |
|---|---|---|---|
| **1** | **fetcher CAP_QUOTE/CAP_DAILY — 美股 + A股** | ✅ **唯一真缺口，本补充全部工作** | 客户端加 2 函数 + 新建 fetcher + 注册 + 优先级下移 + 单测 + 真实验收 |
| 2 | CAP_CALENDAR 认领（hub 路径） | ⚪ **已在产**：`job_gangtise_catalyst` 直接调 `fetch_performance_calendar`，hub 再认领无消费者 = YAGNI | 无 |
| 3 | CAP_ANNOUNCE 认领（hub 路径） | ⚪ **已在产**：同上，`job_gangtise_catalyst` 直接调 `fetch_announcements`（CN/US） | 无 |
| 4 | A股 CAP_EARNINGS 升位 | ⚪ **刻意缓建**：A股 earnings 实际值已由 akshare 供给，一致预期已并入 financials 的 consensus_eps/pe（`providers.py:735` 已注明） | 无 |
| 5 | 指数/benchmark（`is_index=True`） | ✅ **已在产**：`scheduler.py:264` VIP beta 已用 `fetch_quote_history(...,is_index=True)` | 无 |
| 6 | 港股 fetcher / private vault | ⚪ YAGNI：无第二市场需求，`_ANNOUNCE_HK_URL` 亦留待 | 无 |

> **诚实缺省**：不为凑「6 项方案」制造无消费者的 hub 认领。真正的接线缺口是 fetcher，且只有它——
> 全部实施力量投在候选 1。其余据实标注，出现真实消费者再接。

---

## 4. 候选 1 实施详案

### 4.1 客户端扩展（`gangtise_client.py`，**全加法，零签名改动**）

`fetch_quote_history` 保持 close-only 签名（VIP beta 依赖），**另加**两个函数：

```
_OHLCV_FIELDS = ["securityCode","tradeDate","open","high","low","close",
                 "preClose","change","pctChange","volume","amount"]  # = skill _FIELD_LIST

def _parse_ohlcv_body(body) -> list[dict]:
    # 严格码 000000 & status is not False；fieldList 定位列；
    # → [{date,open,high,low,close,volume,amount}...]（升序，str→float，非法行跳过）

def fetch_ohlcv_daily(ak, sk, ticker, market, days=180) -> list[dict]:
    # _resolve_gts_code（A股 6位直通 / 美股 securities/search 解析 .O/.N）
    # 窗口 start=today-ceil(days*1.5)-10 日历日；统一 kline/daily（个股）；请求 _OHLCV_FIELDS
    # → 升序 list[dict]；空/错误码 → []

def _parse_realtime_body(body) -> list[dict]:
    # 实时快照同为 data.fieldList+data.list 二维表 → 逐行 dict

def fetch_realtime_quote(ak, sk, ticker, market) -> dict | None:
    # POST QUOTE_REALTIME_URL {"fieldList":SNAP_FIELD_LIST,"securityList":[code]}
    # 取匹配 code 的行：price=latestPrice, change_pct=pctChange, volume, amount, open/high/low/preClose
    # → {price,change_pct,volume,amount,...} 或 None
```

### 4.2 新建 `data_provider/fetchers/gangtise_fetcher.py`

```
class GangtiseFetcher(BaseFetcher):
    name = "gangtise"; priority = -1   # 负值=严格最高档，恒排 efinance/yfinance 之前
    supported_markets = {"a_stock", "us_stock"}

    _infer_market(ticker): base = ticker.split('.')[0]; 6位纯数字 → a_stock，否则 us_stock
        # 与 _sec_code/_resolve_gts_code 的 market 分支一致；manager 不传 market，从 ticker 推
    _creds(): resolve_gangtise_credentials()（无 current_user/未授权 → None）；异常吞掉返 None

    async fetch_daily(ticker, days=180) -> DataFrame|None:
        creds 无 → None（触发 fallback）；有 → to_thread(fetch_ohlcv_daily)
        → DataFrame[date,open,high,low,close,volume,amount]；空 → None；tail(days)
    async fetch_realtime(ticker) -> StandardQuote|None:
        creds 无 → None；有 → to_thread(fetch_realtime_quote) → StandardQuote(source="gangtise")
```

**契约**：`fetch_daily` 返回列 `date/open/high/low/close/volume` + **`amount`**（供 `clean_ohlc` A股量能反推）；
`fetch_realtime` 返回 `StandardQuote`。缺凭据 → None（自动降级 yfinance/efinance），**绝不 raise**；
网络/接口错误 → 抛 `GangtiseError`（非 `_NON_RETRIABLE` → 计入熔断，5 次降级）。

### 4.3 注册（`data_provider/__init__.py:_create_manager`）

- 两链**最前**注册 `GangtiseFetcher()`，**无 `_installed()` 门**（requests-only，无包依赖）；
- `priority=-1` 即严格最高档，既有 8 源 priority **一律不动**（见 §2）。
- 凭据缺失时 fetcher 自己返回 None → 与「未注册」等效，但保留了「授权后即生效」的能力。

---

## 5. 横切约束（全程遵守）

- **凭据机密**：ak/sk 只用于真实连通性验证，绝不入代码/文档/日志/commit；运行时只从 gitignored
  `.claude/skills/gangtise-*/scripts/.authorization`（JSON `accessKey`/`secretKey`）或
  `resolve_gangtise_credentials()`（DB AES 解密）读取。
- **隔离铁律**：fetcher 是全局单例，**绝不缓存 Key**；每次调用实时 `resolve_gangtise_credentials()`。
- **时区**：UTC 存储 / 北京展示 / 调度 Asia-Shanghai。
- **加法式**：不改 `fetch_quote_history` 签名（VIP beta 消费者）。
- ponytail：最小 diff、YAGNI、诚实缺省；不另起无关计划。

---

## 6. 验收标准（每项：全量 pytest + 修复 + 真实数据验收 + 开发日志 + commit/push 主线）

| 层 | 验收 |
|---|---|
| 单测 | `gangtise_client._demo()` 新增 `_parse_ohlcv_body`/`_parse_realtime_body` 断言全过；新增 `tests/test_gangtise_fetcher.py`（mock 客户端，验缺凭据→None / DataFrame 列 / market 推断 / 熔断异常类型）；`pytest -m "not slow"` 全绿 |
| 真实数据 | 用活体 ak/sk 实拉：美股（AAPL 日K + 实时）、A股（600519 日K + 实时）；核对 OHLCV 非空、量价合理、amount 透传；A股经 `clean_ohlc` 后 volume 单位为「手」 |
| 集成 | `get_fetcher_manager()` 注册顺序：gangtise 在 efinance/yfinance 之前；有凭据时 `fetch_daily` 命中 gangtise，无凭据时降级 yfinance/efinance |
| 回归 | `fetch_quote_history` 签名/行为不变（VIP beta 不回归）；`test_datahub` / `test_ds_scheduler` / `test_scheduler*` 全绿 |

---

## 附：与 master plan §10 的和解

master plan §10 旧表记「US/HK 指数历史行情 ❌ 400」。已提交的 `gangtise_client.fetch_quote_history`
docstring 实测更新：个股统一 `kline/daily`、指数走 `index/kline/daily`（SPX.SPI/IXIC.O/HSI.HI/000300.SH
均取到真实收盘）。**以已提交客户端为准。** 本补充的 fetcher 只处理个股（统一 `kline/daily`），
不涉指数；指数是 VIP beta/benchmark 关注点，已在 `scheduler.py` 经 `is_index=True` 接入。
