# Gangtise 三期接线 开发日志（行情路径最高优先层：美股 + A股 日K/实时）

> 完成日期：2026-08-31 · 依据：`GANGTISE_INTEGRATION_SUPPLEMENT_2026-08.md`（补充接线方案）
> 验收：`python -m pytest -m "not slow" -q` 全绿；新增 `tests/test_gangtise_fetcher.py` **10 passed**；US/A股 日K+实时四条链路以**真实返回数据**佐证（活体实测，非代码桩）。
> 铁律遵守：ak/sk 机密**未入任何代码/文档/日志/提交**（`.claude/` 全目录 gitignore，`.authorization` 仅运行时读取，验证用完即弃）；受控全局 key 仅走 `resolve_gangtise_credentials` 的 admin 双开关；市场隔离由 manager 以**真实 market** 调 `clean_ohlc`；**证伪即诚实留缺省**。

一期铺六域数据底座、二期止血三个活体 bug + 接两个零成本新能力后，本期补的是 DataHub 的**第二条 dispatch 路径**——`FetcherManager` 的 `CAP_QUOTE`/`CAP_DAILY` 行情。此前 Gangtise 只在 **hub 路径**（财务/宏观/研报/KB/估值）供数，行情路径（`manager.py` 的 `_MANAGER_CAPS = {CAP_QUOTE, CAP_DAILY}`）里**完全没有 Gangtise**——日K/实时仍全靠 yfinance/efinance/akshare 免费源。补充方案审计后确认：这是**唯一真实缺口**，其余 5 个候选能力（估值分位、资金流、研报、宏观、KB）经核对均已在生产供数或按 YAGNI 刻意搁置（详见 SUPPLEMENT §3 对账表）。

---

## 关键结论（决定范围）

- **唯一缺口 = 行情 fetcher**。hub 路径已覆盖的能力**不重复接**——否则制造无消费者的死代码（违 ponytail/YAGNI）。
- **免费不限流**：Gangtise 行情在免费池（0 积分/滚动 3 年），不入 `_DEFAULT_QUOTA` → 永不节流，天然适合做**最高优先层**。
- **境内可达 + 官方口径 + admin 共享 key**：相比 yfinance（境外、偶发超时）、efinance/akshare（第三方镜像），Gangtise 是更稳的境内一手源 → 值得排在**严格最高档**。

---

## 改动清单（最小 diff，零新建管线）

### 1　`gangtise_client.py` — 补两个行情函数（复用既有签名/鉴权）

- `fetch_ohlcv_daily(ak, sk, ticker, market, days=180) -> list[dict]`：解析 gtsCode（A股直通 / 美股 `securities/search`）→ 打**统一** `open-quote/kline/daily`（个股口径，指数不走这里）→ `_parse_ohlcv_body` 严格校验 `code==000000`、要求 `tradeDate`+OHLC 列、跳过非数值价行、**透传 volume/amount**、按日期升序。窗口 `span = int(days*1.6)+15` 日历日、`limit=max(days*2,500)` 覆盖停牌/节假日稀释。
- `fetch_realtime_quote(ak, sk, ticker, market) -> dict|None`：POST `open-quote/quote/realtime`，`{"fieldList":SNAP_FIELD_LIST,"securityList":[code]}` → `_parse_realtime_body` 把 2D 表（fieldList+list）还原成行 dict → 取 securityCode 匹配行，`latestPrice→price`，返回 `price/change_pct/volume/amount/open/high/low/pre_close/trade_date`。
- **加性约束遵守**：既有 `fetch_quote_history`（close-only dict，scheduler VIP beta 依赖）签名**未动**；新函数并列新增，`_demo()` 加两条离线断言，自检 `OK`。

### 2　`fetchers/gangtise_fetcher.py` — 新 fetcher（`priority=-1` 严格最高档）

```python
class GangtiseFetcher(BaseFetcher):
    name = "gangtise"
    priority = -1                       # order() 按 (priority, recent_load) 升序 → -1 恒排最前
    supported_markets = {"a_stock", "us_stock"}
```

- `_infer_market(ticker)`：manager 的 `fetch_daily(ticker, days)` **不透传 market**，故从 ticker 推断（A股恒 6 位纯数字，可带 .SH/.SZ 后缀；余为美股）——仅用于选 gtsCode 解析路径；**A股量能单位归一仍由 manager 用真实 market 调 `clean_ohlc`**，不依赖此推断。
- `_creds()`：实时解析 `resolve_gangtise_credentials()`，未授权/无上下文/异常 → `None`。**无凭据 → `fetch_daily/fetch_realtime` 返 None（触发降级），绝不 raise**；接口/网络错误抛 `GangtiseError`（非 `_NON_RETRIABLE` → 计入熔断，连续 5 次熔断 60s 自动降级到 yfinance/efinance）。
- `fetch_daily` → `asyncio.to_thread(fetch_ohlcv_daily)` → DataFrame（`date/open/high/low/close/volume/amount`）→ `tail(days)`；`fetch_realtime` → `StandardQuote(source="gangtise")`。

### 3　`data_provider/__init__.py` — 注册在链首

`_create_manager()` 最前注册 `GangtiseFetcher()`（先于 efinance/A股链与 yfinance/美股链），`try/except ImportError` 包裹，**无 `_installed()` 门**（requests-only 无包依赖）。无凭据时等效未注册（fetcher 自返 None），授权后即生效。

---

## 为什么 `priority=-1` 而非 `priority=0` 级联下移

初版方案想让 Gangtise 占 `priority=0`、把既有 8 源整体 +1。但 `order()` 对**同档** priority 按 `recent_load` 轮转——manager 每次 `note_call` 抬高 Gangtise 的 `recent_load`，下一次调度就轮到 yfinance，**得不到「严格最先」**。改用 `priority=-1`（负值独占最高档）：`(-1, *) < (0, *)` 恒成立 → 每次都先试 Gangtise，且**既有 8 源 priority 一字未改**（零回归、最小 diff）。

---

## 验收证据

### 单测（`tests/test_gangtise_fetcher.py`，全 mock 不打网络）— 10 passed

| 用例 | 覆盖 |
|---|---|
| `test_infer_market` | 6 位数字→a_stock / 带后缀 / 字母→us_stock |
| `test_fetch_daily_no_creds_returns_none`·`realtime` 同 | 缺凭据→None（触发 fallback，不 raise）|
| `test_fetch_daily_contract` | DataFrame 七列齐全 + **amount 透传** + market 推断 |
| `test_fetch_daily_empty_returns_none` | 空返回→None |
| `test_fetch_daily_tail_truncates` | `tail(days)` 只留最后 N 根 |
| `test_fetch_realtime_contract`·`_zero_price` | StandardQuote(source=gangtise) / price=0→None |
| `test_registered_as_strict_top` | `get_status()[0]=="gangtise"`、priority=-1、先于 efinance/yfinance |
| `test_clean_ohlc_normalizes_ashare_volume_via_amount` | A股「股」→「手」÷100 联动 |

回归：`test_datahub`/`test_ohlc_cleaning`/`test_ds_scheduler`/`test_scheduler*`/`test_providers_parse` 全绿；`pytest -m "not slow"` 全量全绿。

### 真实数据活体实测（2026-08-31，ak/sk 仅内存、未落任何文件/日志）

| 链路 | 结果 |
|---|---|
| **US AAPL 日K** | 33 行，`2026-08-28 close=319.7`，amount 全非空 ✓ |
| **US AAPL 实时** | `price=319.7 change_pct=+1.63%` ✓（美股实时不返 amount → StandardQuote 默认 0，符合口径）|
| **A 600519 日K** | 34 行，`2026-08-31 close=1299.52`，amount 全非空 ✓ |
| **A 600519 量能归一** | 原始 `2,324,759 股` → `clean_ohlc` 反推 r≈1 判「股」→ ÷100 → **23,247 手**（比值精确 100.0）✓ 贴合茅台真实成交量级 |
| **A 600519 实时** | `price=1299.52 change_pct=+0.16% volume+amount` 齐 ✓ |

**A股量能归一是本期最关键守卫**：Gangtise A股日K 的 volume 以「股」计，若不归一会被下游 RSI/量比/打分静默放大 100×。`clean_ohlc` 用 `r=amount/(close·vol)` 数据反推（源无关自校准）精确识别并 ÷100，实测比值 100.0 一次命中。

---

## 诚实边界

- **美股实时无 amount**：Gangtise `quote/realtime` 美股口径不返成交额，`StandardQuote.amount` 落 0（默认值），**不伪造**。日K 的 amount 正常。
- **未接的 5 个候选**：估值分位/资金流/研报/宏观/KB 均已在 hub 路径供数或按 YAGNI 搁置，本期**刻意不重复接**（SUPPLEMENT §3 对账）。
- **无凭据即透明降级**：本地/未授权环境 fetcher 自返 None，行情链无缝落到 yfinance/efinance，**无感、无报错**；授权后自动升格为最高优先源。
