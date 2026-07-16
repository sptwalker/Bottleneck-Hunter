"""efinance 数据源 — A股日K线和实时行情。

免费、无需 token，提供 PE/PB/量比/换手率/总市值。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote, safe_float as _safe_float

logger = logging.getLogger(__name__)


class EfinanceFetcher(BaseFetcher):
    name = "efinance"
    priority = 0
    supported_markets = {"a_stock"}

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        import efinance as ef

        code = self._extract_code(ticker)
        if not code:
            return None

        def _fetch():
            df = ef.stock.get_quote_history(code, klt=101)
            if df is None or df.empty:
                return None
            col_map = {}
            for col in df.columns:
                low = col.lower()
                if "日期" in col or "date" in low:
                    col_map[col] = "date"
                elif "开盘" in col or "open" in low:
                    col_map[col] = "open"
                elif "最高" in col or "high" in low:
                    col_map[col] = "high"
                elif "最低" in col or "low" in low:
                    col_map[col] = "low"
                elif "收盘" in col or "close" in low:
                    col_map[col] = "close"
                elif "成交量" in col:
                    col_map[col] = "volume"
                elif "成交额" in col:
                    col_map[col] = "amount"
                elif "涨跌幅" in col:
                    col_map[col] = "change_pct"
                elif "换手率" in col:
                    col_map[col] = "turnover_rate"

            df = df.rename(columns=col_map)
            standard_cols = ["date", "open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]
            keep = [c for c in standard_cols if c in df.columns]
            df = df[keep]
            for col in ["open", "high", "low", "close", "volume", "amount", "change_pct", "turnover_rate"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            if "date" in df.columns:
                df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

            if days < len(df):
                df = df.tail(days).reset_index(drop=True)
            return df

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        import efinance as ef

        code = self._extract_code(ticker)
        if not code:
            return None

        def _fetch():
            df = ef.stock.get_realtime_quotes()
            if df is None or df.empty:
                return None
            cols = df.columns.tolist()
            # 用列名构建索引映射（适配中文列名变化）
            col_idx = {c: i for i, c in enumerate(cols)}

            row = df[df.iloc[:, 0] == code]
            if row.empty:
                row = df[df.iloc[:, 1] == code]
            if row.empty:
                return None
            r = row.iloc[0]

            def _col_val(keywords):
                """按关键词匹配列名取值。"""
                for kw in keywords:
                    for c in cols:
                        if kw in c:
                            return _safe_float(r[c])
                return None

            price = _col_val(["最新价", "现价", "收盘"]) or 0.0
            if price <= 0:
                return None

            return StandardQuote(
                ticker=ticker,
                name=str(r.iloc[1]) if len(cols) > 1 else "",
                price=price,
                change_pct=_col_val(["涨跌幅", "涨幅"]) or 0.0,
                volume=int(_col_val(["成交量"]) or 0),
                amount=_col_val(["成交额"]) or 0.0,
                pe_ratio=_col_val(["市盈率", "动态市盈"]),
                turnover_rate=_col_val(["换手率"]),
                market_cap=_col_val(["总市值"]),
                source="efinance",
                timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )

        return await asyncio.to_thread(_fetch)

    @staticmethod
    def _extract_code(ticker: str) -> str | None:
        """从 ticker 中提取6位A股代码。全系统唯一提取器（见 store_base）。"""
        from bottleneck_hunter.watchlist.store_base import extract_astock_code
        return extract_astock_code(ticker)
