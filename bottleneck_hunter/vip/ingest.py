"""P1 摄取管道：花旗私行月结单 PDF → 结构化持仓 → 语句内对账 → 加密落 financial_documents。

支持范围（M1）：EQUITIES 子表（逐只持仓：代码/公司名/数量/市值）。
固收/期权/结构化产品留 M3。

花旗 fitz 行格式（每只持仓固定偏移，从 'Ticker X UW/UN/HK Equity' 行往前数）：
  i-10: 数量 (Quantity)
  i-6:  市值 USD (Market Value)
  i-3:  公司名 (Description)
  i-1:  as-of date (30JUN26)
  i:    Ticker 行
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator


# ── 数据模型 ──────────────────────────────────────────────────────────────

class EquityHolding(BaseModel):
    ticker: str
    company: str
    quantity: float
    market_value_usd: float

    @field_validator("ticker")
    @classmethod
    def _norm(cls, v: str) -> str:
        return v.strip().upper()


class ReconResult(BaseModel):
    holdings_count: int
    holdings_total_usd: float
    statement_equities_total_usd: Optional[float]   # 结单 TOTAL EQUITIES 行（可能缺失）
    delta_usd: Optional[float]                       # 差值；None = 结单无合计行
    status: str                                      # "ok" | "mismatch" | "no_statement_total"


class BrokerStatement(BaseModel):
    broker: str = "citi"
    period_end: str = ""          # ISO 格式 YYYY-MM-DD
    content_hash: str
    holdings: list[EquityHolding] = []
    recon: ReconResult


# ── PDF 文本抽取 ──────────────────────────────────────────────────────────

def _extract_pages(pdf_bytes: bytes) -> list[str]:
    """用 fitz 逐页抽文本，返回页文本列表。"""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    return [page.get_text() for page in doc]


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ── 持仓解析 ─────────────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"^Ticker\s+([A-Z0-9]{1,6})\s+\S+\s+Equity\s*$")
_TOTAL_EQ_RE = re.compile(r"TOTAL\s+EQUITIES")


def _parse_equities(pages: list[str]) -> tuple[list[EquityHolding], Optional[float]]:
    """从所有页文本抽 EQUITIES 持仓 + TOTAL EQUITIES 合计。"""
    holdings: list[EquityHolding] = []
    total_eq: Optional[float] = None

    for page_text in pages:
        lines = page_text.splitlines()
        for i, line in enumerate(lines):
            # TOTAL EQUITIES 合计行：下一行是数字
            if _TOTAL_EQ_RE.search(line) and i + 1 < len(lines):
                v = _num(lines[i + 1])
                if v and v > 0:
                    total_eq = v

            # 持仓行
            m = _TICKER_RE.match(line.strip())
            if not m or i < 10:
                continue
            ticker = m.group(1)
            qty = _num(lines[i - 10])
            mv = _num(lines[i - 6])
            company = lines[i - 3].strip()
            if qty and mv and qty > 0 and mv > 0:
                try:
                    holdings.append(EquityHolding(
                        ticker=ticker, company=company,
                        quantity=qty, market_value_usd=mv,
                    ))
                except Exception:  # noqa: BLE001
                    pass

    return holdings, total_eq


# ── 期末日解析 ────────────────────────────────────────────────────────────

_MONTH = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
          "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}

def _parse_period(filename: str) -> str:
    """从文件名抽期末日，如 '30_Jun_2026' → '2026-06-30'。"""
    m = re.search(r"(\d{1,2})[_\s]*(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[_\s]*(\d{4})",
                  filename.upper())
    if not m:
        return ""
    return f"{m.group(3)}-{_MONTH[m.group(2)]}-{int(m.group(1)):02d}"


# ── 语句内对账 ────────────────────────────────────────────────────────────

_RECON_TOL = 0.02   # 2% 容差（四舍五入 + 汇率微差）

def _reconcile(holdings: list[EquityHolding],
               statement_total: Optional[float]) -> ReconResult:
    calc = sum(h.market_value_usd for h in holdings)
    if statement_total is None:
        return ReconResult(holdings_count=len(holdings), holdings_total_usd=calc,
                           statement_equities_total_usd=None, delta_usd=None,
                           status="no_statement_total")
    delta = abs(calc - statement_total)
    ok = delta / max(statement_total, 1.0) <= _RECON_TOL
    return ReconResult(holdings_count=len(holdings), holdings_total_usd=calc,
                       statement_equities_total_usd=statement_total,
                       delta_usd=round(calc - statement_total, 2),
                       status="ok" if ok else "mismatch")


# ── 公开入口 ──────────────────────────────────────────────────────────────

def ingest_pdf(pdf_bytes: bytes, filename: str = "") -> BrokerStatement:
    """解析 PDF → BrokerStatement（含对账结果）。不写库，纯解析。

    调用方拿到 BrokerStatement 后：
      - recon.status == "ok" / "no_statement_total" → 可落库（status="parsed_ok"）
      - recon.status == "mismatch" → 落库 status="needs_review"，recon_flags 记 mismatch
    """
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    pages = _extract_pages(pdf_bytes)
    holdings, total_eq = _parse_equities(pages)
    recon = _reconcile(holdings, total_eq)
    period = _parse_period(filename)
    return BrokerStatement(
        content_hash=content_hash,
        period_end=period,
        holdings=holdings,
        recon=recon,
    )


def ingest_and_store(pdf_bytes: bytes, filename: str,
                     user_id: str, market: str = "us_stock",
                     broker: str = "citi") -> dict:
    """解析 + 加密落 financial_documents。返回 {doc_id, status, recon}。

    幂等：同用户同文件哈希已存在则直接返回已有 doc_id（不重复写）。
    """
    from bottleneck_hunter.auth.store import AuthStore

    stmt = ingest_pdf(pdf_bytes, filename)
    store = AuthStore()

    # 幂等去重
    existing = store.find_financial_doc_by_hash(user_id, stmt.content_hash)
    if existing:
        return {"doc_id": existing["id"], "status": existing["status"],
                "recon": stmt.recon.model_dump(), "duplicate": True}

    db_status = "parsed_ok" if stmt.recon.status in ("ok", "no_statement_total") else "needs_review"
    recon_flags = {
        "equities_recon": stmt.recon.status,
        "holdings_count": stmt.recon.holdings_count,
    }
    if stmt.recon.status == "mismatch":
        recon_flags["delta_flag"] = "fail"

    doc_id = store.create_financial_doc(
        user_id,
        content_hash=stmt.content_hash,
        market=market,
        broker=broker,
        period_end=stmt.period_end,
        file_name=filename,
        parsed_json=stmt.model_dump_json(),
        recon_flags=recon_flags,
        status=db_status,
    )
    return {"doc_id": doc_id, "status": db_status,
            "recon": stmt.recon.model_dump(), "duplicate": False}


def demo() -> None:
    """本机自检：用真实月结单跑一遍，打印结果（数字保留，账号不在此处）。"""
    import sys
    d = Path(r"C:\Users\walker\Documents\walker\银行文件\花旗月结单")
    files = sorted(d.glob("*.PDF")) if d.exists() else []
    if not files:
        print("未找到月结单，跳过 demo"); return
    pdf = next((f for f in files if "Jun 2026" in f.name), files[0])
    stmt = ingest_pdf(pdf.read_bytes(), pdf.name)
    print(f"[{pdf.name}]")
    print(f"  期末: {stmt.period_end}  sha256: {stmt.content_hash[:12]}…")
    print(f"  持仓 {stmt.recon.holdings_count} 只  合计 ${stmt.recon.holdings_total_usd:,.2f}")
    if stmt.recon.statement_equities_total_usd:
        print(f"  结单合计 ${stmt.recon.statement_equities_total_usd:,.2f}  "
              f"差值 ${stmt.recon.delta_usd:+,.2f}  对账: {stmt.recon.status}")
    for h in stmt.holdings:
        print(f"    {h.ticker:6} {h.company[:28]:28} {h.quantity:>8,.0f}股  ${h.market_value_usd:>14,.2f}")
    assert stmt.recon.holdings_count > 0, "未抽到持仓"
    assert stmt.recon.status in ("ok", "no_statement_total", "mismatch")
    print("ingest demo 通过")


if __name__ == "__main__":
    demo()
