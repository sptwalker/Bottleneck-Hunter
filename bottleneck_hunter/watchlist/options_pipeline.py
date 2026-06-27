"""Options activity pipeline — detect unusual options activity via yfinance.

Analyzes options chains for put/call ratio and unusual volume.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import yfinance as yf

from bottleneck_hunter.watchlist.retry import with_retry
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(4)
    return _SEM


@with_retry(max_retries=3, base_delay=1.0)
def _analyze_options_chain(ticker: str) -> dict | None:
    """Fetch options chain and analyze. Synchronous."""
    t = yf.Ticker(ticker)
    expiries = t.options
    if not expiries:
        return None

    nearest = expiries[0]
    chain = t.option_chain(nearest)
    calls = chain.calls
    puts = chain.puts

    total_call_vol = int(calls["volume"].sum()) if "volume" in calls.columns else 0
    total_put_vol = int(puts["volume"].sum()) if "volume" in puts.columns else 0

    pcr = total_put_vol / total_call_vol if total_call_vol > 0 else None

    unusual = (total_call_vol + total_put_vol) > 10000

    max_oi_strike = None
    max_oi_expiry = nearest
    if "openInterest" in calls.columns and not calls.empty:
        max_idx = calls["openInterest"].idxmax()
        if max_idx is not None:
            max_oi_strike = float(calls.loc[max_idx, "strike"])

    notable = []
    if "volume" in calls.columns:
        big_calls = calls[calls["volume"] > 1000]
        for _, row in big_calls.head(3).iterrows():
            notable.append({
                "type": "call",
                "strike": float(row["strike"]),
                "volume": int(row["volume"]),
                "oi": int(row.get("openInterest", 0)),
                "expiry": nearest,
            })
    if "volume" in puts.columns:
        big_puts = puts[puts["volume"] > 1000]
        for _, row in big_puts.head(3).iterrows():
            notable.append({
                "type": "put",
                "strike": float(row["strike"]),
                "volume": int(row["volume"]),
                "oi": int(row.get("openInterest", 0)),
                "expiry": nearest,
            })

    aid = hashlib.md5(f"{ticker}:opt:{datetime.now().strftime('%Y%m%d')}".encode()).hexdigest()[:12]
    return {
        "id": aid,
        "ticker": ticker,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "unusual_volume": unusual,
        "put_call_ratio": round(pcr, 3) if pcr is not None else None,
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "max_oi_strike": max_oi_strike,
        "max_oi_expiry": max_oi_expiry,
        "notable_trades": notable,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def _fetch_one(ticker: str, store: WatchlistStore) -> str:
    async with _get_sem():
        try:
            result = await asyncio.to_thread(_analyze_options_chain, ticker)
            if result:
                store.save_options([result])
                return "ok"
            return "no_data"
        except Exception as e:
            logger.error("Options pipeline error for %s: %s", ticker, e)
            return f"error: {e}"


async def fetch_options_batch(tickers: list[str], store: WatchlistStore) -> dict[str, str]:
    """Batch-analyze options for US stock tickers. Returns {ticker: status}."""
    if not tickers:
        return {}
    results = {}
    for ticker in tickers:
        results[ticker] = await _fetch_one(ticker, store)
    return results
