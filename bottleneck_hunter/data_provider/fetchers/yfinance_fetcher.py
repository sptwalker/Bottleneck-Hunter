"""yfinance 数据源包装 — 美股日K线和实时行情。

包装现有 price_pipeline 中的 yfinance 调用为 BaseFetcher 接口。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote, safe_float as _safe_float

logger = logging.getLogger(__name__)


class YfinanceFetcher(BaseFetcher):
    name = "yfinance"
    priority = 0
    supported_markets = {"us_stock"}

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        import yfinance as yf

        def _fetch():
            t = yf.Ticker(ticker)
            period = "1y" if days > 180 else "6mo"
            df = t.history(period=period)
            if df is None or df.empty:
                return None
            records = []
            for i in range(len(df)):
                records.append({
                    "date": df.index[i].strftime("%Y-%m-%d"),
                    "open": _safe_float(df["Open"].iloc[i]),
                    "high": _safe_float(df["High"].iloc[i]),
                    "low": _safe_float(df["Low"].iloc[i]),
                    "close": _safe_float(df["Close"].iloc[i]),
                    "volume": int(df["Volume"].iloc[i]) if df["Volume"].iloc[i] else 0,
                })
            result = pd.DataFrame(records)
            if days < len(result):
                result = result.tail(days).reset_index(drop=True)
            return result

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        import yfinance as yf

        def _fetch():
            t = yf.Ticker(ticker)
            info = {}
            try:
                info = t.info or {}
            except Exception:
                pass
            price = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
            if not price:
                return None
            prev_close = _safe_float(info.get("previousClose") or info.get("regularMarketPreviousClose"))
            change_pct = 0.0
            if prev_close and prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 2)
            return StandardQuote(
                ticker=ticker,
                name=info.get("shortName", ""),
                price=price,
                change_pct=change_pct,
                volume=int(info.get("volume") or info.get("regularMarketVolume") or 0),
                pe_ratio=_safe_float(info.get("forwardPE") or info.get("trailingPE")),
                pb_ratio=_safe_float(info.get("priceToBook")),
                market_cap=_safe_float(info.get("marketCap")),
                source="yfinance",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        return await asyncio.to_thread(_fetch)
