"""finnhub 数据源 — 美股日K线和基本面。

免费 tier 60次/分钟，需要在 .env 中配置 FINNHUB_API_KEY。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote

logger = logging.getLogger(__name__)


class FinnhubFetcher(BaseFetcher):
    name = "finnhub"
    priority = 2
    supported_markets = {"us_stock"}

    def __init__(self):
        super().__init__()

    def _ensure_api_key(self) -> str:
        """按「当前上下文用户」实时解析 Finnhub Key。严格隔离：不读 env、不进程缓存、不借他人。

        因 fetcher 是全局单例、跨用户复用，绝不能缓存 Key。
        """
        try:
            from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
            return resolve_data_source_key("finnhub") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _get_client(self):
        import finnhub
        return finnhub.Client(api_key=self._ensure_api_key())

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        if not self._ensure_api_key():
            return None

        def _fetch():
            client = self._get_client()
            now = int(datetime.now(timezone.utc).timestamp())
            start = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

            candles = client.stock_candles(ticker.upper(), "D", start, now)
            if not candles or candles.get("s") != "ok":
                return None

            records = []
            for i in range(len(candles["t"])):
                dt = datetime.fromtimestamp(candles["t"][i], tz=timezone.utc)
                records.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "open": candles["o"][i],
                    "high": candles["h"][i],
                    "low": candles["l"][i],
                    "close": candles["c"][i],
                    "volume": candles["v"][i],
                })
            return pd.DataFrame(records)

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        if not self._ensure_api_key():
            return None

        def _fetch():
            client = self._get_client()
            quote = client.quote(ticker.upper())
            if not quote or quote.get("c", 0) <= 0:
                return None

            price = quote["c"]
            prev_close = quote.get("pc", 0)
            change_pct = 0.0
            if prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 2)

            pe_ratio = None
            pb_ratio = None
            market_cap = None
            try:
                financials = client.company_basic_financials(ticker.upper(), "all")
                metrics = financials.get("metric", {})
                pe_ratio = metrics.get("peBasicExclExtraTTM")
                pb_ratio = metrics.get("pbQuarterly")
                market_cap = metrics.get("marketCapitalization")
                if market_cap:
                    market_cap = market_cap * 1e6  # finnhub 返回百万单位
            except Exception as e:
                logger.debug("finnhub 获取基本面失败 (%s): %s", ticker, e)

            return StandardQuote(
                ticker=ticker,
                price=price,
                change_pct=change_pct,
                volume=int(quote.get("v", 0) if quote.get("v") else 0),
                pe_ratio=pe_ratio,
                pb_ratio=pb_ratio,
                market_cap=market_cap,
                source="finnhub",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        return await asyncio.to_thread(_fetch)

    async def health_check(self) -> bool:
        if not self._ensure_api_key():
            return False
        try:
            client = self._get_client()
            q = await asyncio.to_thread(client.quote, "AAPL")
            return q is not None and q.get("c", 0) > 0
        except Exception:
            return False
