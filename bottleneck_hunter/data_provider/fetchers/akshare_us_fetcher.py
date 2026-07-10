"""akshare 美股数据源包装 — 美股日K线（国内可访问，规避 yfinance/Yahoo 在境内数据中心被限流）。

数据源：新浪美股（ak.stock_us_daily）。免密钥、国内可达，作为 yfinance 的兜底。
只提供历史日K；实时行情仍交给 yfinance/finnhub。
"""

from __future__ import annotations

import asyncio
import logging
import re

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote, safe_float as _safe_float

logger = logging.getLogger(__name__)

# 美股代码：去掉可能的交易所后缀/前缀，保留纯字母代码（BRK.B→BRK-B 由 akshare 侧处理，这里保守取字母段）
_US_RE = re.compile(r"[A-Za-z][A-Za-z.\-]*")


def _us_symbol(ticker: str) -> str | None:
    m = _US_RE.match(ticker.strip())
    return m.group(0).upper() if m else None


class AkshareUsFetcher(BaseFetcher):
    name = "akshare_us"
    priority = 1  # yfinance(0) 被限流时兜底；finnhub(2) 需密钥，排其前
    supported_markets = {"us_stock"}

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        sym = _us_symbol(ticker)
        if not sym:
            return None

        def _fetch():
            try:
                import akshare as ak
            except ImportError:
                return None
            try:
                df = ak.stock_us_daily(symbol=sym, adjust="qfq")
            except Exception as e:  # noqa: BLE001
                logger.debug("akshare 美股 %s 获取失败: %s", sym, e)
                return None
            if df is None or df.empty:
                return None
            cols = {c.lower(): c for c in df.columns}
            need = ("date", "open", "high", "low", "close", "volume")
            if not all(k in cols for k in need):
                logger.debug("akshare 美股 %s 缺列: %s", sym, list(df.columns))
                return None
            rows = []
            for _, r in df.iterrows():
                c = _safe_float(r[cols["close"]])
                if c is None:
                    continue
                rows.append({
                    "date": str(r[cols["date"]])[:10],
                    "open": _safe_float(r[cols["open"]]),
                    "high": _safe_float(r[cols["high"]]),
                    "low": _safe_float(r[cols["low"]]),
                    "close": c,
                    "volume": int(_safe_float(r[cols["volume"]]) or 0),
                })
            if not rows:
                return None
            result = pd.DataFrame(rows)
            if days < len(result):
                result = result.tail(days).reset_index(drop=True)
            return result

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        # 仅历史日K；实时交给 yfinance/finnhub
        return None
