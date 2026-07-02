"""SEC EDGAR data pipeline — fetch Form 4 (insider trades) and 8-K filings.

Uses SEC EDGAR REST API (free, no auth). Rate limit: 10 req/s.
Only processes US_STOCK tickers.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from bottleneck_hunter.watchlist.retry import with_retry, get_http_client
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

_SEM: asyncio.Semaphore | None = None


def _get_sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(8)
    return _SEM
_HEADERS = {
    "User-Agent": "BottleneckHunter research@bottleneckhunter.com",
    "Accept": "application/json",
}
_HEADERS_XML = {
    "User-Agent": "BottleneckHunter research@bottleneckhunter.com",
    "Accept": "application/xml, text/xml, text/html, */*",
}
_CIK_CACHE: dict[str, str] = {}
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# Transaction code mapping
_TX_CODES = {
    "P": "Purchase",
    "S": "Sale",
    "A": "Grant/Award",
    "D": "Disposition (non-open-market)",
    "F": "Tax withholding",
    "M": "Option exercise",
    "G": "Gift",
    "C": "Conversion",
    "J": "Other",
    "V": "Voluntary reporting",
    "I": "Discretionary",
    "W": "Will/inheritance",
}


# ---------------------------------------------------------------------------
# CIK resolution
# ---------------------------------------------------------------------------

async def _load_cik_map() -> dict[str, str]:
    """Load ticker → CIK mapping from SEC."""
    if _CIK_CACHE:
        return _CIK_CACHE
    try:
        client = get_http_client()
        resp = await client.get(_TICKERS_URL, headers=_HEADERS)
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

@with_retry(max_retries=3, base_delay=2.0)
async def _fetch_filings(cik: str, form_types: list[str], limit: int = 10) -> list[dict]:
    """Fetch recent filings from EDGAR."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom&startdt=2025-01-01&forms={','.join(form_types)}&entities={cik}"
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    client = get_http_client()
    await asyncio.sleep(0.15)  # SEC rate limit
    resp = await client.get(submissions_url, headers=_HEADERS)
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


# ---------------------------------------------------------------------------
# Form 4 XML parsing
# ---------------------------------------------------------------------------

def _parse_form4_xml(xml_text: str) -> list[dict]:
    """Parse Form 4 XML and extract insider transaction records.

    Returns a list of dicts with keys:
        insider_name, insider_title, transaction_type, shares, price,
        total_value, date
    Each non-derivative transaction becomes one record.
    Falls back to empty list on any parse error.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.debug("Form 4 XML parse error: %s", e)
        return []

    # --- reporting owner info ---
    owner_name = "Unknown"
    owner_title = ""
    # Try <reportingOwner> / <reportingOwnerId> / <rptOwnerName>
    for owner_el in root.iter("reportingOwner"):
        name_el = owner_el.find(".//rptOwnerName")
        if name_el is not None and name_el.text:
            owner_name = name_el.text.strip()
        # Title in <reportingOwnerRelationship> / <officerTitle>
        title_el = owner_el.find(".//officerTitle")
        if title_el is not None and title_el.text:
            owner_title = title_el.text.strip()
        break  # use first reporting owner

    # --- non-derivative transactions ---
    records = []
    for txn in root.iter("nonDerivativeTransaction"):
        record = _extract_transaction(txn, owner_name, owner_title)
        if record:
            records.append(record)

    # Also check derivativeTransaction (option exercises often have
    # an underlying transaction we care about)
    for txn in root.iter("derivativeTransaction"):
        record = _extract_transaction(txn, owner_name, owner_title)
        if record:
            records.append(record)

    return records


def _extract_transaction(txn_el: ET.Element, owner_name: str, owner_title: str) -> dict | None:
    """Extract a single transaction record from a transaction XML element."""
    # Transaction date
    date_el = txn_el.find(".//transactionDate/value")
    txn_date = date_el.text.strip() if date_el is not None and date_el.text else None

    # Transaction code (P, S, M, A, etc.)
    coding_el = txn_el.find(".//transactionCoding/transactionCode")
    txn_code = coding_el.text.strip() if coding_el is not None and coding_el.text else ""

    # Shares
    shares_el = txn_el.find(".//transactionAmounts/transactionShares/value")
    shares = 0.0
    if shares_el is not None and shares_el.text:
        try:
            shares = float(shares_el.text.strip())
        except (ValueError, TypeError):
            pass

    # Price per share
    price_el = txn_el.find(".//transactionAmounts/transactionPricePerShare/value")
    price = None
    if price_el is not None and price_el.text:
        try:
            price = float(price_el.text.strip())
        except (ValueError, TypeError):
            pass

    # Skip transactions with no meaningful data
    if shares == 0 and price is None:
        return None

    # Compute total value
    total_value = None
    if price is not None and shares > 0:
        total_value = round(price * shares, 2)

    # Build human-readable transaction type
    txn_type = _TX_CODES.get(txn_code, txn_code) if txn_code else "unknown"

    return {
        "insider_name": owner_name,
        "insider_title": owner_title,
        "transaction_type": txn_type,
        "shares": int(shares) if shares == int(shares) else shares,
        "price": price,
        "total_value": total_value,
        "date": txn_date,
        "transaction_code": txn_code,  # raw code for filtering
    }


async def _fetch_filing_documents(cik: str, accession: str) -> list[dict]:
    """Fetch the filing index JSON to find the primary XML document.

    SEC EDGAR provides a JSON index at:
      https://www.sec.gov/Archives/edgar/data/{cik}/{acc-no-dashes}/{acc}.json
    which is not always available. Alternatively, we parse the index page.

    Returns list of {name, url} for XML documents found.
    """
    cik_stripped = cik.lstrip("0")
    acc_clean = accession.replace("-", "")

    # Try the JSON index first (most reliable)
    index_json_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_clean}/{accession}-index.json"
    client = get_http_client()

    await asyncio.sleep(0.15)  # SEC rate limit
    try:
        resp = await client.get(index_json_url, headers=_HEADERS)
        if resp.status_code == 200:
            data = resp.json()
            docs = []
            for item in data.get("directory", {}).get("item", []):
                name = item.get("name", "")
                if name.endswith(".xml") and not name.startswith("R"):
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_clean}/{name}"
                    docs.append({"name": name, "url": doc_url})
            if docs:
                return docs
    except Exception as e:
        logger.debug("Filing index JSON fetch failed for %s: %s", accession, e)

    # Fallback: try common Form 4 XML filename patterns
    # Form 4 XMLs are typically named like: doc4.xml, form4.xml, {accession}.xml, primary_doc.xml
    base_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_clean}"
    candidates = [
        f"{base_url}/doc4.xml",
        f"{base_url}/form4.xml",
        f"{base_url}/primary_doc.xml",
        f"{base_url}/{accession}.xml",
    ]
    return [{"name": c.split("/")[-1], "url": c} for c in candidates]


async def _fetch_form4_xml(cik: str, filing: dict) -> list[dict]:
    """Fetch and parse Form 4 XML for a filing.

    Returns a list of transaction dicts, or empty list on failure.
    """
    accession = filing.get("accession", "")
    if not accession:
        return []

    docs = await _fetch_filing_documents(cik, accession)
    client = get_http_client()

    for doc in docs:
        await asyncio.sleep(0.15)  # SEC rate limit
        try:
            resp = await client.get(doc["url"], headers=_HEADERS_XML)
            if resp.status_code != 200:
                continue
            content = resp.text
            # Quick sanity check: is this actually Form 4 XML?
            if "<ownershipDocument" not in content:
                continue
            records = _parse_form4_xml(content)
            if records:
                logger.debug("Parsed %d transactions from %s", len(records), doc["url"])
                return records
        except Exception as e:
            logger.debug("Failed to fetch/parse %s: %s", doc["url"], e)
            continue

    return []


async def _parse_insider_trades_from_filings(cik: str, ticker: str, filings: list[dict]) -> list[dict]:
    """Extract insider trade records from Form 4 filings.

    For each Form 4 filing, attempts to fetch and parse the actual XML to get
    real transaction data (insider name, shares, price, etc.).
    Falls back to stub records if XML parsing fails.
    """
    trades = []
    for f in filings:
        if not f.get("is_insider_trade"):
            continue

        # Try to parse real Form 4 XML data
        try:
            xml_records = await _fetch_form4_xml(cik, f)
        except Exception as e:
            logger.warning("Form 4 XML fetch failed for %s/%s: %s", ticker, f.get("accession", ""), e)
            xml_records = []

        if xml_records:
            # Create a trade record for each transaction in the XML
            for i, rec in enumerate(xml_records):
                tid = hashlib.md5(
                    f"{ticker}:insider:{f['id']}:{i}".encode()
                ).hexdigest()[:12]
                trades.append({
                    "id": tid,
                    "ticker": ticker,
                    "insider_name": rec["insider_name"],
                    "insider_title": rec.get("insider_title", ""),
                    "transaction_type": rec["transaction_type"],
                    "shares": rec["shares"],
                    "price": rec["price"],
                    "total_value": rec["total_value"],
                    "date": rec.get("date") or f["filed_date"],
                    "source_filing_id": f["id"],
                })
        else:
            # 诚信原则：XML 解析失败时不落库占位空壳（shares=0/price=None 会被下游当真实信号）。
            # 原始 filing 已存 sec_filings 表可追溯；此处只跳过无法解析的内幕交易。
            logger.debug("Form 4 XML 解析失败，跳过占位记录: %s/%s", ticker, f.get("id"))

    return trades


# ---------------------------------------------------------------------------
# Batch pipeline
# ---------------------------------------------------------------------------

async def _fetch_one(ticker: str, store: WatchlistStore) -> dict:
    """Fetch SEC data for one ticker."""
    async with _get_sem():
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

        trades = await _parse_insider_trades_from_filings(cik, ticker, filings)
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
