"""pytdx 数据源 — A股通达信实时行情。

直连通达信行情服务器，毫秒级响应。需要尝试多个服务器地址。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote

logger = logging.getLogger(__name__)

TDX_HOSTS = [
    ("115.238.90.165", 7709),
    ("221.194.181.176", 7709),
    ("119.147.212.81", 7709),
    ("218.75.126.9", 7709),
    ("218.108.50.178", 7709),
    ("180.153.39.51", 7709),
]

MARKET_SH = 1
MARKET_SZ = 0


def _code_to_market(code: str) -> int:
    if code.startswith(("6", "9")):
        return MARKET_SH
    return MARKET_SZ


class PytdxFetcher(BaseFetcher):
    name = "pytdx"
    priority = 2
    supported_markets = {"a_stock"}

    def __init__(self):
        super().__init__()
        self._connected_host: tuple[str, int] | None = None

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        code = self._extract_code(ticker)
        if not code:
            return None

        def _fetch():
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            market = _code_to_market(code)

            if not self._connect(api):
                return None

            try:
                # category=9 是日K线
                bars = api.get_security_bars(9, market, code, 0, days)
                if not bars:
                    return None

                records = []
                for bar in bars:
                    records.append({
                        "date": bar["datetime"][:10],
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": int(bar["vol"]),
                        "amount": float(bar.get("amount", 0)),
                    })
                return pd.DataFrame(records)
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        code = self._extract_code(ticker)
        if not code:
            return None

        def _fetch():
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            market = _code_to_market(code)

            if not self._connect(api):
                return None

            try:
                data = api.get_security_quotes([(market, code)])
                if not data:
                    return None
                item = data[0]
                last_close = float(item.get("last_close", 0))
                price = float(item.get("price", 0))
                change_pct = 0.0
                if last_close > 0:
                    change_pct = round((price - last_close) / last_close * 100, 2)
                return StandardQuote(
                    ticker=ticker,
                    price=price,
                    change_pct=change_pct,
                    volume=int(item.get("vol", 0)),
                    amount=float(item.get("amount", 0)),
                    source="pytdx",
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            finally:
                try:
                    api.disconnect()
                except Exception:
                    pass

        return await asyncio.to_thread(_fetch)

    def _connect(self, api) -> bool:
        """尝试连接通达信服务器，优先使用上次成功的服务器。"""
        if self._connected_host:
            host, port = self._connected_host
            try:
                api.connect(host, port)
                return True
            except Exception:
                self._connected_host = None

        for host, port in TDX_HOSTS:
            try:
                api.connect(host, port)
                self._connected_host = (host, port)
                return True
            except Exception:
                continue
        logger.error("pytdx: 无法连接任何通达信服务器")
        return False

    @staticmethod
    def _extract_code(ticker: str) -> str | None:
        # 全系统唯一 A股代码提取器（见 store_base）；容纳 600519 / 600519.SH/.SS / SH600519 等全部形态
        from bottleneck_hunter.watchlist.store_base import extract_astock_code
        return extract_astock_code(ticker)
