"""SEC EDGAR data pipeline — fetch Form 4 (insider trades) and 8-K filings.

Uses SEC EDGAR REST API (free, no auth). Rate limit: 10 req/s.
Only processes US_STOCK tickers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

import httpx

from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM = asyncio.Semaphore(8)
_HEADERS = {
    "User-Agent": "BottleneckHunter research@bottleneckhunter.com",
    "Accept": "application/json",
}
_CIK_CACHE: dict[str, str] = {}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

async def _load_cik_map() -> dict[str, str]:
    """Load ticker → CIK mapping from SEC."""
    if _CIK_CACHE:
        return _CIK_CACHE
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
            resp = await client.get(_TICKERS_URL)
            if resp.status_code != 200:
                logger.warning("SEC tickers endpoint returned %d", resp.status_code)
                return {}
        data = resp.json()
        for item in data.values():
            tk = str(item.get("ticker", "")).upper()
            cik = str(item.get("cik_str", ""))
            if tk and cik:
                _CIK_CACHE[tk] = cik.zfill(10)
        return _CIK_CACHE
    except Exception as e:
        logger.error("Failed to load SEC CIK map: %s", e)
        return {}


async def _get_cik(ticker: str) -> str | None:
    cik_map = await _load_cik_map()
    return cik_map.get(ticker.upper())


# ---------------------------------------------------------------------------
# Filing fetching
# ---------------------------------------------------------------------------

async def _fetch_filings(cik: str, form_types: list[str], limit: int = 10) -> list[dict]:
    """Fetch recent filings from EDGAR."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom&startdt=2025-01-01&forms={','.join(form_types)}&entities={cik}"
    # 更可靠的方式：使用 EDGAR submissions API
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        async with httpx.AsyncClient(timeout=20, headers=_HEADERS) as client:
            await asyncio.sleep(0.15)  # SEC rate limit
            resp = await client.get(submissions_url)
            if resp.status_code != 200:
                return []

        data = resp.json()
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accessions = recent.get("accessionNumber", [])
        descriptions = recent.get("primaryDocDescription", [])

        results = []
        for i in range(min(len(forms), 200)):
            if forms[i] not in form_types:
                continue
            if len(results) >= limit:
                break
            acc_clean = accessions[i].replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{acc_clean}/{accessions[i]}-index.htm"
            fid = hashlib.md5(f"{cik}:{accessions[i]}".encode()).hexdigest()[:12]
            results.append({
                "id": fid,
                "filing_type": forms[i],
                "filed_date": dates[i],
                "title": descriptions[i] if i < len(descriptions) else "",
                "url": filing_url,
                "is_insider_trade": forms[i] in ("4", "4/A"),
                "accession": accessions[i],
            })
        return results
    except Exception as e:
        logger.warning("EDGAR fetch failed for CIK %s: %s", cik, e)
        return []


def _parse_insider_trades_from_filings(ticker: str, filings: list[dict]) -> list[dict]:
    """Extract basic insider trade records from Form 4 filings."""
    trades = []
    for f in filings:
        if not f.get("is_insider_trade"):
            continue
        tid = hashlib.md5(f"{ticker}:insider:{f['id']}".encode()).hexdigest()[:12]
        trades.append({
            "id": tid,
            "ticker": ticker,
            "insider_name": f.get("title", "Unknown"),
            "insider_title": "",
            "transaction_type": "unknown",
            "shares": 0,
            "price": None,
            "total_value": None,
            "date": f["filed_date"],
            "source_filing_id": f["id"],
        })
    return trades


# ---------------------------------------------------------------------------
# Batch pipeline
# ---------------------------------------------------------------------------

async def _fetch_one(ticker: str, store: WatchlistStore) -> dict:
    """Fetch SEC data for one ticker."""
    async with _SEM:
        cik = await _get_cik(ticker)
        if not cik:
            return {"filings": 0, "trades": 0}

        filings = await _fetch_filings(cik, ["4", "4/A", "8-K", "10-Q", "10-K"], limit=15)
        if not filings:
            return {"filings": 0, "trades": 0}

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        filing_dicts = [
            {**f, "ticker": ticker, "fetched_at": now_iso}
            for f in filings
        ]
        fcount = store.save_filings(filing_dicts)

        trades = _parse_insider_trades_from_filings(ticker, filings)
        for t in trades:
            t["fetched_at"] = now_iso
        tcount = store.save_insider_trades(trades)

        return {"filings": fcount, "trades": tcount}


async def fetch_sec_batch(tickers: list[str], store: WatchlistStore) -> dict[str, dict]:
    """Batch-fetch SEC filings for US stock tickers. Returns {ticker: {filings, trades}}."""
    if not tickers:
        return {}
    results = {}
    for ticker in tickers:
        try:
            results[ticker] = await _fetch_one(ticker, store)
        except Exception as e:
            logger.error("SEC pipeline error for %s: %s", ticker, e)
            results[ticker] = {"filings": 0, "trades": 0}
    return results
