"""Alpha Vantage 数据源 — 美股日K线和实时行情（应急兜底层）。

定位：美股 quote/daily 的**最后一层兜底**（priority=3，排在 yfinance/新浪美股/finnhub 之后）。
只有前三层全部熔断时才轮到它，故不追求吞吐——Alpha Vantage 免费档极紧（~25 次/日，
scheduler 已配保守额度阀 per_day=20/per_min=5），is_over_quota 会在发请求前掐断，绝不打爆。

复用数据源目录里已有的 alphavantage Key（很多用户为 earnings/financials 已配置），零新 Key。
限流响应（Note/Information 字段）返回 None 而非抛异常——限流是额度问题非数据源故障，
不应误触熔断、也不该污染健康统计（与 providers._get_json_soft 同口径）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd
import requests

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote
from bottleneck_hunter.data_provider.base import safe_float as _safe_float

logger = logging.getLogger(__name__)

_BASE = "https://www.alphavantage.co/query"
_TIMEOUT = 15
_UA = {"User-Agent": "BottleneckHunter/1.0"}


class AlphaVantageFetcher(BaseFetcher):
    name = "alphavantage"
    priority = 3  # 美股行情最后一层兜底：前三层（yfinance/新浪/finnhub）全熔断才轮到
    supported_markets = {"us_stock"}

    def _ensure_api_key(self) -> str:
        """按「当前上下文用户」实时解析 Alpha Vantage Key。严格隔离：不读 env、不缓存、不借他人。

        fetcher 是全局单例、跨用户复用，绝不能缓存 Key。
        """
        try:
            from bottleneck_hunter.data_provider.data_source_catalog import resolve_data_source_key
            return resolve_data_source_key("alphavantage") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _get_json(self, params: dict) -> dict | None:
        """GET Alpha Vantage；限流(Note/Information)/空响应统一返回 None（不抛→不误触熔断）。"""
        r = requests.get(_BASE, params=params, timeout=_TIMEOUT, headers=_UA)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        # 限流 / 额度耗尽 / 无效调用：AV 用 Note/Information/Error Message 字段回传，非数据源故障
        if data.get("Note") or data.get("Information") or data.get("Error Message"):
            logger.debug("AlphaVantage 限流或无效调用: %s",
                         data.get("Note") or data.get("Information") or data.get("Error Message"))
            return None
        return data

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        if not self._ensure_api_key():
            return None

        def _fetch():
            key = self._ensure_api_key()
            if not key:
                return None
            # compact=最近100条；需要更长窗口才取 full（同一额度成本，只是 payload 更大）
            outputsize = "compact" if days <= 100 else "full"
            data = self._get_json({
                "function": "TIME_SERIES_DAILY", "symbol": ticker.upper(),
                "outputsize": outputsize, "apikey": key,
            })
            series = (data or {}).get("Time Series (Daily)")
            if not isinstance(series, dict) or not series:
                return None
            records = []
            for date_str, ohlc in series.items():
                records.append({
                    "date": date_str,
                    "open": _safe_float(ohlc.get("1. open")),
                    "high": _safe_float(ohlc.get("2. high")),
                    "low": _safe_float(ohlc.get("3. low")),
                    "close": _safe_float(ohlc.get("4. close")),
                    "volume": int(_safe_float(ohlc.get("5. volume")) or 0),
                })
            if not records:
                return None
            result = pd.DataFrame(records).sort_values("date").reset_index(drop=True)  # AV 返回降序，统一升序
            if days < len(result):
                result = result.tail(days).reset_index(drop=True)
            return result

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        if not self._ensure_api_key():
            return None

        def _fetch():
            key = self._ensure_api_key()
            if not key:
                return None
            data = self._get_json({
                "function": "GLOBAL_QUOTE", "symbol": ticker.upper(), "apikey": key,
            })
            q = (data or {}).get("Global Quote")
            if not isinstance(q, dict) or not q:
                return None
            price = _safe_float(q.get("05. price"))
            if not price:
                return None
            prev_close = _safe_float(q.get("08. previous close"))
            change_pct = 0.0
            if prev_close and prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 2)
            return StandardQuote(
                ticker=ticker,
                price=price,
                change_pct=change_pct,
                volume=int(_safe_float(q.get("06. volume")) or 0),
                source="alphavantage",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        return await asyncio.to_thread(_fetch)

    async def health_check(self) -> bool:
        return bool(self._ensure_api_key())
