"""机构持仓 & 分析师评级数据管道 — 通过 yfinance 获取美股数据。

仅支持美股（yfinance 覆盖范围）。A 股暂不支持。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import yfinance as yf

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    """延迟创建信号量，避免在导入时绑定事件循环。"""
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(5)
    return _SEM


# ---------------------------------------------------------------------------
# 机构持仓
# ---------------------------------------------------------------------------

def _fetch_institutional_holders_sync(ticker: str) -> list[dict]:
    """同步获取机构持仓数据（在线程池中运行）。"""
    from bottleneck_hunter.data_provider import yf_gate
    yf_gate.throttle()  # 全局限速：均匀错峰打 Yahoo，避免 429
    try:
        t = yf.Ticker(ticker)
        df = t.institutional_holders
        yf_gate.observe(None)
        if df is None or df.empty:
            logger.info("无机构持仓数据: %s", ticker)
            return []
    except Exception as e:
        yf_gate.observe(e)  # 命中 429 则闸门自适应退避
        logger.warning("获取 %s 机构持仓失败: %s", ticker, e)
        return []

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    holders: list[dict] = []
    for _, row in df.iterrows():
        # yfinance 列名: Holder, Shares, Date Reported, % Out, Value
        holder_name = str(row.get("Holder", ""))
        if not holder_name:
            continue
        date_reported = ""
        raw_date = row.get("Date Reported")
        if raw_date is not None:
            try:
                date_reported = str(raw_date)[:10]
            except Exception:
                pass
        shares = 0
        raw_shares = row.get("Shares")
        if raw_shares is not None:
            try:
                shares = int(raw_shares)
            except (ValueError, TypeError):
                pass
        value = 0.0
        raw_value = row.get("Value")
        if raw_value is not None:
            try:
                value = float(raw_value)
            except (ValueError, TypeError):
                pass
        pct_held = 0.0
        raw_pct = row.get("% Out")
        if raw_pct is not None:
            try:
                pct_held = float(raw_pct) * 100  # 转为百分比
            except (ValueError, TypeError):
                pass
        holders.append({
            "holder_name": holder_name,
            "shares": shares,
            "value": value,
            "pct_held": round(pct_held, 4),
            "date": date_reported,
            "fetched_at": now_iso,
        })
    return holders


async def fetch_institutional_holders(
    ticker: str, store: WatchlistStore
) -> str:
    """异步获取单个 ticker 的机构持仓并保存到 store。

    Returns:
        "ok" / "no_data" / "error: ..."
    """
    async with _get_sem():
        try:
            from bottleneck_hunter.data_provider.hub import CAP_INSTITUTIONAL, get_hub
            async with get_hub().track("yfinance", CAP_INSTITUTIONAL, "us_stock") as _sink:
                holders = await asyncio.to_thread(
                    _fetch_institutional_holders_sync, ticker
                )
                if holders:
                    store.save_institutional_holders(ticker, holders)
                    logger.info("机构持仓保存成功: %s (%d 条)", ticker, len(holders))
                    _sink["rows"] = len(holders)
                    return "ok"
                return "no_data"
        except Exception as e:
            logger.error("机构持仓管道错误 %s: %s", ticker, e)
            return f"error: {e}"


# ---------------------------------------------------------------------------
# 分析师评级
# ---------------------------------------------------------------------------

def _fetch_analyst_ratings_sync(ticker: str) -> list[dict]:
    """同步获取分析师评级 / 推荐数据（在线程池中运行）。"""
    from bottleneck_hunter.data_provider import yf_gate
    yf_gate.throttle()  # 全局限速：均匀错峰打 Yahoo，避免 429
    try:
        t = yf.Ticker(ticker)
        df = t.recommendations
        yf_gate.observe(None)
        if df is None or df.empty:
            logger.info("无分析师评级数据: %s", ticker)
            return []
    except Exception as e:
        yf_gate.observe(e)  # 命中 429 则闸门自适应退避
        logger.warning("获取 %s 分析师评级失败: %s", ticker, e)
        return []

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ratings: list[dict] = []
    for idx, row in df.iterrows():
        # yfinance recommendations 列名: period, strongBuy, buy, hold, sell, strongSell
        # 或者较新版 yfinance: Firm, To Grade, From Grade, Action, Date
        firm = str(row.get("Firm", ""))
        rating = str(row.get("To Grade", ""))
        # 如果没有 Firm 列（汇总格式），使用 period 作为标识
        if not firm and "period" in row.index:
            firm = f"consensus_{row.get('period', '')}"
            # 汇总格式：strongBuy/buy/hold/sell/strongSell 列
            buy_count = int(row.get("strongBuy", 0) or 0) + int(row.get("buy", 0) or 0)
            hold_count = int(row.get("hold", 0) or 0)
            sell_count = int(row.get("sell", 0) or 0) + int(row.get("strongSell", 0) or 0)
            total = buy_count + hold_count + sell_count
            if total > 0:
                if buy_count > hold_count and buy_count > sell_count:
                    rating = "Buy"
                elif sell_count > hold_count:
                    rating = "Sell"
                else:
                    rating = "Hold"
            else:
                rating = "N/A"

        # 日期
        date_str = ""
        if isinstance(idx, datetime):
            date_str = idx.strftime("%Y-%m-%d")
        else:
            raw_date = row.get("Date")
            if raw_date is not None:
                try:
                    date_str = str(raw_date)[:10]
                except Exception:
                    pass

        # 目标价（如果有）
        target_price = None
        raw_tp = row.get("Target Price")
        if raw_tp is not None:
            try:
                target_price = float(raw_tp)
            except (ValueError, TypeError):
                pass

        if not firm:
            continue
        ratings.append({
            "firm": firm,
            "rating": rating,
            "target_price": target_price,
            "date": date_str,
            "fetched_at": now_iso,
        })
    return ratings


async def fetch_analyst_ratings(
    ticker: str, store: WatchlistStore
) -> str:
    """异步获取单个 ticker 的分析师评级并保存到 store。

    Returns:
        "ok" / "no_data" / "error: ..."
    """
    async with _get_sem():
        try:
            from bottleneck_hunter.data_provider.hub import CAP_INSTITUTIONAL, get_hub
            async with get_hub().track("yfinance", CAP_INSTITUTIONAL, "us_stock") as _sink:
                ratings = await asyncio.to_thread(
                    _fetch_analyst_ratings_sync, ticker
                )
                if ratings:
                    store.save_analyst_ratings(ticker, ratings)
                    logger.info("分析师评级保存成功: %s (%d 条)", ticker, len(ratings))
                    _sink["rows"] = len(ratings)
                    return "ok"
                return "no_data"
        except Exception as e:
            logger.error("分析师评级管道错误 %s: %s", ticker, e)
            return f"error: {e}"


# ---------------------------------------------------------------------------
# 批量接口
# ---------------------------------------------------------------------------

async def fetch_institutional_batch(
    tickers: list[str], store: WatchlistStore
) -> dict[str, str]:
    """批量获取机构持仓。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {
        t: asyncio.create_task(fetch_institutional_holders(t, store))
        for t in tickers
    }
    results: dict[str, str] = {}
    for ticker, task in tasks.items():
        results[ticker] = await task
    return results


async def fetch_analyst_batch(
    tickers: list[str], store: WatchlistStore
) -> dict[str, str]:
    """批量获取分析师评级。返回 {ticker: status}。"""
    if not tickers:
        return {}
    tasks = {
        t: asyncio.create_task(fetch_analyst_ratings(t, store))
        for t in tickers
    }
    results: dict[str, str] = {}
    for ticker, task in tasks.items():
        results[ticker] = await task
    return results
