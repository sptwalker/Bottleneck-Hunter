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

    await asyncio.gather(*tasks, return_exceptions=True)

    if not results:
        cached = store.get_latest_macro_snapshots()
        for row in cached:
            results[row["indicator"]] = {
                "value": row["value"], "change_pct": row.get("change_pct", 0.0) or 0.0,
                "label": row["indicator"],
            }

    return results
