"""A 股公告管道 — 对标 SEC EDGAR，获取上市公司公告信息。

使用 akshare 获取 A 股个股公告，分类存储到 store.save_filings()。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.retry import with_retry
from bottleneck_hunter.watchlist.store import WatchlistStore

try:
    import akshare as ak
except ImportError:
    ak = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(4)
    return _SEM

_ASTOCK_RE = re.compile(r"^(?:SH|SZ|sh|sz)?(\d{6})")

_NOTICE_CATEGORY_MAP = {
    "业绩预告": "earnings_preview",
    "业绩快报": "earnings_flash",
    "年报": "annual_report",
    "半年报": "semi_annual",
    "季报": "quarterly",
    "定增": "private_placement",
    "增发": "private_placement",
    "配股": "rights_issue",
    "减持": "insider_sell",
    "增持": "insider_buy",
    "回购": "buyback",
    "分红": "dividend",
    "重大合同": "major_contract",
    "重组": "restructuring",
    "股权变动": "ownership_change",
}


def _classify_notice(title: str) -> str:
    """根据公告标题简单分类。"""
    for keyword, category in _NOTICE_CATEGORY_MAP.items():
        if keyword in title:
            return category
    return "other"


def _extract_code(ticker: str) -> str | None:
    code = ticker.split(".")[0].strip()
    m = _ASTOCK_RE.match(code)
    return m.group(1) if m else None


@with_retry(max_retries=3, base_delay=1.0)
def _fetch_notices_sync(ticker: str, limit: int = 15) -> list[dict]:
    """同步获取单只 A 股的公告列表。"""
    if ak is None:
        return []
    code = _extract_code(ticker)
    if not code:
        return []
    # stock_notice_report(symbol=公告类型, date) 是「全市场按日」接口，传股票代码会 KeyError；
    # 个股公告用 stock_individual_notice_report(security=代码)。
    df = ak.stock_individual_notice_report(security=code)
    if df is None or df.empty:
        return []

    results = []
    for _, row in df.head(limit).iterrows():
        title = str(row.get("公告标题", row.get("标题", ""))).strip()
        if not title:
            continue
        date_str = str(row.get("公告日期", row.get("日期", "")))[:10]
        source_url = str(row.get("网址", row.get("公告链接", row.get("链接", ""))))
        fid = hashlib.md5(f"{ticker}:{title}:{date_str}".encode()).hexdigest()[:12]
        category = _classify_notice(title)
        results.append({
            "id": fid,
            "ticker": ticker,
            "filing_type": category,
            "filed_date": date_str,
            "title": title,
            "url": source_url,
            "is_insider_trade": category in ("insider_sell", "insider_buy"),
            "accession": fid,
        })
    return results


async def _fetch_one(ticker: str, store: WatchlistStore) -> dict:
    """异步获取单只 A 股的公告并存储。"""
    async with _get_sem():
        from bottleneck_hunter.data_provider.hub import CAP_NOTICE, get_hub
        async with get_hub().track("akshare", CAP_NOTICE, "a_stock") as _sink:
            filings = await asyncio.to_thread(_fetch_notices_sync, ticker)
            if not filings:
                return {"filings": 0, "trades": 0}

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for f in filings:
                f["fetched_at"] = now_iso

            fcount = store.save_filings(filings)
            _sink["rows"] = fcount

        insider_filings = [f for f in filings if f.get("is_insider_trade")]
        trades = []
        for f in insider_filings:
            tid = hashlib.md5(f"{ticker}:insider:{f['id']}".encode()).hexdigest()[:12]
            trades.append({
                "id": tid,
                "ticker": ticker,
                "insider_name": "",
                "insider_title": "",
                "transaction_type": "insider_sell" if "减持" in f.get("title", "") else "insider_buy",
                "shares": 0,
                "price": None,
                "total_value": None,
                "date": f["filed_date"],
                "source_filing_id": f["id"],
                "fetched_at": now_iso,
            })
        tcount = store.save_insider_trades(trades) if trades else 0

        return {"filings": fcount, "trades": tcount}


async def fetch_notice_batch(tickers: list[str], store: WatchlistStore) -> dict[str, dict]:
    """批量获取 A 股公告。返回 {ticker: {filings, trades}}。"""
    if not tickers:
        return {}
    results = {}
    for ticker in tickers:
        try:
            results[ticker] = await _fetch_one(ticker, store)
        except Exception as e:
            logger.error("A 股公告管道错误 %s: %s", ticker, e)
            results[ticker] = {"filings": -1, "trades": 0, "error": str(e)}
    return results
