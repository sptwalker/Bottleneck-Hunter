"""akshare 数据源包装 — A股日K线和基本面。

包装现有 price_pipeline 中的 akshare 调用为 BaseFetcher 接口。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from bottleneck_hunter.data_provider.base import BaseFetcher, StandardQuote, safe_float as _safe_float

logger = logging.getLogger(__name__)

def _extract_code(ticker: str) -> str | None:
    # 全系统唯一 A股代码提取器（见 store_base）；容纳 600519 / 600519.SH/.SS / SH600519 等全部形态
    from bottleneck_hunter.watchlist.store_base import extract_astock_code
    return extract_astock_code(ticker)


class AkshareFetcher(BaseFetcher):
    name = "akshare"
    priority = 1
    supported_markets = {"a_stock"}

    async def fetch_daily(self, ticker: str, days: int = 180) -> pd.DataFrame | None:
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare 未安装")
            return None

        code = _extract_code(ticker)
        if not code:
            return None

        def _fetch():
            start_date = (datetime.now() - timedelta(days=max(days, 365))).strftime("%Y%m%d")
            end_date = datetime.now().strftime("%Y%m%d")
            df = ak.stock_zh_a_hist(
                symbol=code, period="daily",
                start_date=start_date, end_date=end_date,
                adjust="qfq",
            )
            if df is None or df.empty:
                return None

            required_cols = {"日期", "开盘", "最高", "最低", "收盘", "成交量"}
            missing = required_cols - set(df.columns)
            if missing:
                logger.warning("akshare 返回缺少必要列 %s: %s", code, missing)
                return None

            records = []
            for i in range(len(df)):
                records.append({
                    "date": str(df.iloc[i]["日期"])[:10],
                    "open": float(df.iloc[i]["开盘"]),
                    "high": float(df.iloc[i]["最高"]),
                    "low": float(df.iloc[i]["最低"]),
                    "close": float(df.iloc[i]["收盘"]),
                    "volume": int(df.iloc[i]["成交量"]),
                    "amount": float(df.iloc[i].get("成交额", 0)),
                })
            result = pd.DataFrame(records)
            if days < len(result):
                result = result.tail(days).reset_index(drop=True)
            return result

        return await asyncio.to_thread(_fetch)

    async def fetch_realtime(self, ticker: str) -> StandardQuote | None:
        try:
            import akshare as ak
        except ImportError:
            return None

        code = _extract_code(ticker)
        if not code:
            return None

        def _fetch():
            try:
                df_info = ak.stock_individual_info_em(symbol=code)
                if df_info is None or df_info.empty:
                    return None
                info = dict(zip(df_info["item"], df_info["value"]))
                price = _safe_float(info.get("最新价") or info.get("收盘价"))
                if not price:
                    return None
                return StandardQuote(
                    ticker=ticker,
                    name=str(info.get("股票简称", "")),
                    price=price,
                    pe_ratio=_safe_float(info.get("市盈率(动态)")),
                    market_cap=_safe_float(info.get("总市值")),
                    source="akshare",
                    timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            except Exception as e:
                logger.debug("akshare 实时行情失败 (%s): %s", code, e)
                return None

        return await asyncio.to_thread(_fetch)
