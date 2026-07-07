"""财报数据管道 — 经 DataHub 取 earnings（FMP 美股含一致预期 / Tushare A股），落 earnings_reports 表。

填补此前全空的 earnings_reports；一致预期不再用实时 PE 冒充（FMP epsEstimated 是真机构一致预期）。
"""

from __future__ import annotations

import asyncio
import logging

from bottleneck_hunter.data_provider.hub import CAP_EARNINGS, get_hub
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(3)   # 付费源额度有限，低并发
    return _SEM


async def fetch_earnings_one(ticker: str, store: WatchlistStore,
                             market: str = "us_stock", user_id: str = "") -> str:
    """取单只 earnings 并落库。返回状态 ok / no_data / error。"""
    async with _get_sem():
        try:
            rec = await get_hub().fetch(CAP_EARNINGS, ticker, market, user_id)
            if not rec or not rec.get("report_date"):
                return "no_data"
            store.save_earnings([rec])
            return "ok"
        except Exception as e:  # noqa: BLE001
            logger.debug("earnings 采集失败 %s: %s", ticker, e)
            return "error"


async def fetch_earnings_batch(tickers: list[str], store: WatchlistStore,
                               market: str = "us_stock", user_id: str = "") -> dict[str, str]:
    """批量取 earnings。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {t: asyncio.create_task(fetch_earnings_one(t, store, market, user_id)) for t in tickers}
    return {t: await task for t, task in tasks.items()}
