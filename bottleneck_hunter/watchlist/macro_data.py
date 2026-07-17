"""宏观市场数据采集模块 — VIX、美债收益率、DXY、北向资金等。

为 L1 宏观策略层提供真实宏观数据输入，替代空 macro 字典。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import yfinance as yf

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

from bottleneck_hunter.watchlist.retry import with_retry
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_yf_quote(symbol: str) -> dict | None:
    """从 yfinance 获取单个指标的最新价格和变动。"""
    t = yf.Ticker(symbol)
    hist = t.history(period="5d")
    if hist is None or hist.empty:
        return None
    closes = hist["Close"].tolist()
    latest = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else latest
    change = ((latest - prev) / prev * 100) if prev else 0.0
    return {"value": round(latest, 4), "change_pct": round(change, 2)}


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_northbound_flow() -> dict | None:
    """通过 akshare 获取北向资金净流入（最近交易日）。"""
    if ak is None:
        return None
    try:
        df = ak.stock_hsgt_north_net_flow_in_em()
        if df is None or df.empty:
            return None
        latest = df.iloc[-1]
        value = float(latest.get("value", latest.iloc[-1]))
        return {"value": round(value, 2), "change_pct": 0.0}
    except Exception as e:
        logger.debug("北向资金获取失败: %s", e)
        return None


@with_retry(max_retries=2, base_delay=1.0)
def _fetch_sge_gold() -> dict | None:
    """上海金 Au99.99 收盘价（人民币/克），akshare 国内可达，替代已停更的 FRED 伦敦金。

    价格锚是人民币/克而非美元/盎司，但用于「跨资产风险印证」看的是黄金**走势方向**，
    单位差异不影响判断；label 已注明单位避免误读。
    """
    if ak is None:
        return None
    try:
        df = ak.spot_hist_sge(symbol="Au99.99")
        if df is None or df.empty or len(df) < 1:
            return None
        latest = float(df.iloc[-1]["close"])
        prev = float(df.iloc[-2]["close"]) if len(df) >= 2 else latest
        change_pct = round((latest / prev - 1) * 100, 2) if prev else 0.0
        return {"value": round(latest, 2), "change_pct": change_pct}
    except Exception as e:
        logger.debug("上海金获取失败: %s", e)
        return None


# 宏观指标定义：(显示名, yfinance 代码, 市场标签)
# 全球风险因子：VIX/美债/美元指数——各市场都合理参考（人民币/资本流动/联储外溢）
_GLOBAL_INDICATORS = [
    ("vix", "^VIX", "VIX 恐慌指数"),
    ("us_10y_yield", "^TNX", "10Y 美债收益率"),
    ("dxy", "DX-Y.NYB", "美元指数"),
]
# 美股专属股指：仅美股市场纳入，避免 sp500/nasdaq 污染 A股/港股宏观口径
_US_INDICATORS = [
    ("sp500", "^GSPC", "标普500"),
    ("nasdaq", "^IXIC", "纳斯达克综指"),
]

_CN_INDICATORS = [
    ("cny_usd", "CNY=X", "人民币汇率"),
    ("sse_index", "000001.SS", "上证综指"),
    ("csi300", "000300.SS", "沪深300"),
]

_HK_INDICATORS = [
    ("hsi", "^HSI", "恒生指数"),
    ("hstech", "^HSTECH", "恒生科技指数"),
]

# 各市场用于填充"大盘指数"的真实指数键（区别于 VIX/汇率等宏观指标）
MARKET_INDEX_KEYS: dict[str, list[str]] = {
    "us_stock": ["sp500", "nasdaq"],
    "a_stock": ["sse_index", "csi300"],
    "hk_stock": ["hsi", "hstech"],
}

# 各市场「专属」宏观指标 key（_GLOBAL_INDICATORS 与 FRED 为全球共享、任何市场都可用，不在此列）。
# 用于缓存兜底时剔除「他市专属」指标，避免 sp500/北向资金 等串味进另一市场的 L1 宏观口径。
_MARKET_EXCLUSIVE_KEYS: dict[str, set[str]] = {
    "us_stock": {"sp500", "nasdaq"},
    "a_stock": {"cny_usd", "sse_index", "csi300", "northbound_flow"},
    "hk_stock": {"hsi", "hstech"},
}


def foreign_indicator_keys(markets: list[str]) -> set[str]:
    """返回不属于给定市场的『他市专属』宏观指标 key 集合（缓存兜底应剔除，防串味）。"""
    keep: set[str] = set()
    for m in markets or []:
        keep |= _MARKET_EXCLUSIVE_KEYS.get(m, set())
    all_exclusive: set[str] = set()
    for ks in _MARKET_EXCLUSIVE_KEYS.values():
        all_exclusive |= ks
    return all_exclusive - keep


# ── FRED（美联储经济数据）：真宏观经济指标，补齐 yfinance 只有行情价格的缺口 ──
# 国内服务器 yfinance(Yahoo)常被墙，而 FRED(api.stlouisfed.org)在桌面借道白名单 → 更可靠。
# 故利率/曲线/信用利差/缩表/VIX/金油全部走 FRED；VIX/10Y 与 yfinance 同 key，FRED 作兜底。
# (显示名 key, FRED series_id, 中文标签, kind)
#   kind="level"  : 取最新值 + 环比绝对变动（利率/利差/VIX/油金等价格型）
#   kind="cpi"    : 按 13 个月算同比通胀
#   kind="walcl"  : 美联储总资产（百万美元）→ 换算万亿 + 周环比%（看缩表节奏）
_FRED_INDICATORS = [
    ("fed_funds_rate", "FEDFUNDS", "联邦基金利率(%)", "level"),
    ("unemployment_rate", "UNRATE", "美国失业率(%)", "level"),
    ("cpi_yoy", "CPIAUCSL", "美国CPI同比(%)", "cpi"),
    # ── 利率与流动性「量价全景」──
    ("us_10y_yield", "DGS10", "10Y 美债收益率(%)", "level"),
    ("yield_curve_2s10s", "T10Y2Y", "2s10s 利差(%,负=倒挂)", "level"),
    ("fed_balance_sheet", "WALCL", "美联储总资产(万亿$,降=缩表QT)", "walcl"),
    # ── 风险情绪「跨资产印证」──
    ("vix", "VIXCLS", "VIX 恐慌指数", "level"),
    ("hy_oas", "BAMLH0A0HYM2", "高收益债信用利差 HY OAS(%)", "level"),
    ("wti_oil", "DCOILWTICO", "WTI 原油($/桶)", "level"),
    # 黄金：FRED 的 GOLDAMGBD228NLBM 已停更(返回400)，改用 akshare 上海金(见 _fetch_sge_gold)。
]


async def _fred_series(key: str, series_id: str, limit: int = 1) -> list[dict]:
    # 走共享 httpx 客户端(带桌面借道 transport)：api.stlouisfed.org 在借道白名单，国内服务器可经桌面取
    from bottleneck_hunter.watchlist.retry import get_http_client
    client = get_http_client()
    r = await client.get(
        "https://api.stlouisfed.org/fred/series/observations"
        f"?series_id={series_id}&api_key={key}&file_type=json&sort_order=desc&limit={limit}",
        timeout=10, headers={"User-Agent": "BottleneckHunter/1.0"})
    r.raise_for_status()
    return [o for o in (r.json().get("observations") or []) if o.get("value") not in (None, "", ".")]


async def _fetch_fred_indicators() -> dict:
    """拉取 FRED 关键宏观指标（利率/曲线/信用利差/缩表/VIX/金油等）。无 Key 则返回空。"""
    from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
    key = resolve_data_source_key("fred")
    if not key:
        return {}
    out: dict[str, dict] = {}
    for k, series_id, label, kind in _FRED_INDICATORS:
        try:
            if kind == "cpi":
                obs = await _fred_series(key, series_id, limit=13)  # 需 13 个月算同比
                if len(obs) >= 13:
                    latest, year_ago = float(obs[0]["value"]), float(obs[12]["value"])
                    yoy = round((latest / year_ago - 1) * 100, 2) if year_ago else 0.0
                    prev_yoy = None
                    if len(obs) >= 14:
                        prev_yoy = round((float(obs[1]["value"]) / float(obs[13]["value"]) - 1) * 100, 2)
                    out[k] = {"value": yoy, "change_pct": round(yoy - prev_yoy, 2) if prev_yoy is not None else 0.0,
                              "label": label, "date": obs[0].get("date", "")}
            elif kind == "walcl":
                # 美联储总资产：原始单位百万美元 → 万亿；change_pct 用周环比%（看缩表/扩表趋势）
                obs = await _fred_series(key, series_id, limit=2)
                if obs:
                    val_m = float(obs[0]["value"])
                    prev_m = float(obs[1]["value"]) if len(obs) >= 2 else val_m
                    trillions = round(val_m / 1_000_000, 3)
                    wow = round((val_m / prev_m - 1) * 100, 2) if prev_m else 0.0
                    out[k] = {"value": trillions, "change_pct": wow, "label": label, "date": obs[0].get("date", "")}
            else:  # kind == "level"：最新值 + 环比绝对变动
                obs = await _fred_series(key, series_id, limit=2)
                if obs:
                    val = float(obs[0]["value"])
                    prev = float(obs[1]["value"]) if len(obs) >= 2 else val
                    out[k] = {"value": round(val, 2), "change_pct": round(val - prev, 2),
                              "label": label, "date": obs[0].get("date", "")}
        except Exception as e:  # noqa: BLE001
            logger.warning("FRED 指标 %s 采集失败: %s", series_id, e)
    return out


async def fetch_macro_data(store: WatchlistStore, markets: list[str] | None = None) -> dict:
    """采集宏观数据并存入 macro_snapshots 表，返回整合的宏观数据字典。

    返回格式:
    {
        "vix": {"value": 18.5, "change_pct": -2.1, "label": "VIX 恐慌指数"},
        "us_10y_yield": {"value": 4.25, "change_pct": 0.5, "label": "10Y 美债收益率"},
        ...
    }
    """
    if markets is None:
        markets = ["us_stock"]

    indicators = list(_GLOBAL_INDICATORS)  # 全球风险因子各市场都取
    if "us_stock" in markets:
        indicators.extend(_US_INDICATORS)   # 美股股指仅美股纳入
    if "a_stock" in markets:
        indicators.extend(_CN_INDICATORS)
    if "hk_stock" in markets:
        indicators.extend(_HK_INDICATORS)

    results: dict[str, dict] = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    async def _fetch_one(key: str, symbol: str, label: str):
        try:
            data = await asyncio.to_thread(_fetch_yf_quote, symbol)
            if data:
                results[key] = {**data, "label": label}
                store.save_macro_snapshot(key, today, data["value"], now_iso,
                                          change_pct=data.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("宏观指标 %s 采集失败: %s", key, e)

    tasks = [_fetch_one(key, symbol, label) for key, symbol, label in indicators]

    if "a_stock" in markets:
        async def _fetch_north():
            try:
                data = await asyncio.to_thread(_fetch_northbound_flow)
                if data:
                    results["northbound_flow"] = {**data, "label": "北向资金净流入(亿)"}
                    store.save_macro_snapshot("northbound_flow", today, data["value"], now_iso,
                                              change_pct=data.get("change_pct", 0.0))
            except Exception as e:
                logger.warning("北向资金采集失败: %s", e)
        tasks.append(_fetch_north())

    # FRED 真宏观指标 —— Fed 政策对各市场都有外溢，全局纳入；无 Key 自动跳过。
    # vix/us_10y_yield 与 yfinance 同 key：FRED 作兜底(yfinance 取到就不覆盖，取不到则 FRED 补)，
    # 其余(曲线/信用利差/缩表/金油等)为 FRED 独有。故等 yfinance 任务先跑完，FRED 再 setdefault。
    async def _fetch_fred():
        try:
            fred = await _fetch_fred_indicators()  # 已改异步(走共享 httpx，可借道)，不再 to_thread
            for k, v in fred.items():
                if k in results:
                    continue  # yfinance 已取到该 key(更实时) → 不覆盖
                results[k] = v
                store.save_macro_snapshot(k, today, v["value"], now_iso,
                                          change_pct=v.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("FRED 宏观指标采集失败: %s", e)
    # 先并发跑 yfinance 指标(+北向)，全部完成后再跑 FRED 兜底，保证 setdefault 语义确定
    await asyncio.gather(*tasks, return_exceptions=True)
    await _fetch_fred()

    # 黄金（上海金，akshare 国内可达）——全局风险资产，各市场都参考
    async def _fetch_gold():
        try:
            data = await asyncio.to_thread(_fetch_sge_gold)
            if data:
                results["gold"] = {**data, "label": "上海金 Au99.99(¥/克)"}
                store.save_macro_snapshot("gold", today, data["value"], now_iso,
                                          change_pct=data.get("change_pct", 0.0))
        except Exception as e:
            logger.warning("黄金采集失败: %s", e)
    await _fetch_gold()

    if not results:
        foreign = foreign_indicator_keys(markets)  # 剔除他市专属指标，防缓存兜底串味
        cached = store.get_latest_macro_snapshots()
        for row in cached:
            if row["indicator"] in foreign:
                continue
            results[row["indicator"]] = {
                "value": row["value"], "change_pct": row.get("change_pct", 0.0) or 0.0,
                "label": row["indicator"],
            }

    return results
