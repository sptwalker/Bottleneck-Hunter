"""P1/C3 摄取管道：PDF → 结构化持仓 → 语句内对账 → 加密落 financial_documents。

当前已知支持：
- `citi`（花旗私行综合月结单）→ **确定性解析器**（EQUITIES + CASH，已由真实 7 期月结单验证）

C3 兼容架构：
- `detect_broker()`：优先显式 broker hint，其次由 PDF 文本/文件名探测券商
- `_PARSERS`：broker_id -> parser callable 注册表
- `ingest_pdf()`：仅负责 dispatch；未知格式后续可接 `vip_statement_extract` 角色做 LLM fallback，
  但不会在这里假装支持没见过的券商。

花旗 fitz 行格式（每只持仓固定偏移，从 'Ticker X UW/UN/HK Equity' 行往前数）：
  i-10: 数量 (Quantity)
  i-6:  市值(原币 Market Value)
  i-4:  美元总值 (Total Value USD) ← 统一口径
  i-3:  公司名 (Description)
  i:    Ticker 行 / (ETF 用 ISIN 行作锚)
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
    market_value_usd: float                 # 统一美元口径（结单 Total Value USD 列）
    nominal_ccy: str = "USD"                # 名义货币（来自币种小节标题）
    market_value_nominal: float | None = None  # 原币市值（审计留痕；USD 持仓 == market_value_usd）

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


class CashBalance(BaseModel):
    currency: str
    market_value_nominal: float
    market_value_usd: float

    @field_validator("currency")
    @classmethod
    def _c(cls, v: str) -> str:
        return v.strip().upper()


class BrokerStatement(BaseModel):
    broker: str = "citi"
    period_end: str = ""          # ISO 格式 YYYY-MM-DD
    content_hash: str
    holdings: list[EquityHolding] = []
    cash_balances: list[CashBalance] = []
    total_cash_usd: float = 0.0
    account_summary: dict = {}      # 可选：完整账户层摘要（如 Nomura 的 NAV/负债/衍生品合计）
    recon: ReconResult


# ── PDF 文本抽取 ──────────────────────────────────────────────────────────

def _extract_pages(pdf_bytes: bytes, pdf_password: str = "") -> list[str]:
    """用 fitz 逐页抽文本，返回页文本列表。加密 PDF 可传密码。"""
    import fitz  # PyMuPDF
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.needs_pass:
        if not pdf_password or not doc.authenticate(pdf_password):
            raise ValueError("pdf_password_required_or_invalid")
    return [page.get_text() for page in doc]


def _num(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ── 持仓解析 ─────────────────────────────────────────────────────────────

_TICKER_RE = re.compile(r"^Ticker\s+([A-Z0-9]{1,6})\s+\S+\s+Equity\s*$")   # 个股锚（恒为股票）
_ISIN_RE = re.compile(r"^ISIN\s+([A-Z]{2}[A-Z0-9]{9,10})\s*$")             # ETF/基金锚
_TOTAL_EQ_RE = re.compile(r"TOTAL\s+EQUITIES")
_CCY_SECTION_RE = re.compile(r"Equities\s*\((USD|HKD|TWD|EUR|JPY|GBP|CNH|CNY|SGD|AUD)\)")
# 进入/离开 EQUITIES 区（ISIN 锚需区分股票ETF vs 固收债券：仅 EQUITIES 区内的 ISIN 才算持仓）
_EQ_ENTER_RE = re.compile(r"^EQUITIES\b")
_EQ_LEAVE_RE = re.compile(r"^(FIXED INCOME|OTHER ASSETS|INVESTMENT CASH|CASH AND|TOTAL EQUITIES|"
                          r"STRUCTURED|ALTERNATIVE|COMMODIT)")


def _parse_equities(pages: list[str]) -> tuple[list[EquityHolding], Optional[float]]:
    """固定偏移解析 EQUITIES 持仓（含个股 Ticker 锚 + ETF 的 ISIN 锚）+ TOTAL EQUITIES 合计。

    块结构（锚行往前 10 行，个股/ETF 一致）：
      i-10 数量 | i-9 单价 | i-8 总成本 | i-7 现价 | i-6 市值(原币) |
      i-5 未实现 | i-4 Total Value USD ★统一美元口径 | i-3 公司名 | i-2 日期 | i-1 %占比
    - 个股锚 `Ticker XXX Equity`：恒为股票（固收用 `Ticker XXX ID`，不含 Equity，天然排除）。
    - ETF 锚 `ISIN XXXX`：仅当处于 EQUITIES 区才算（否则会误收固收债券的 ISIN）。
    `in_equities` 状态跨页保持（应对 'EQUITIES CONTINUED' 续页）。
    """
    holdings: list[EquityHolding] = []
    total_eq: Optional[float] = None
    in_equities = False
    cur_ccy = "USD"

    for page_text in pages:
        lines = page_text.splitlines()
        for i, raw in enumerate(lines):
            line = raw.strip()

            # 区间与币种状态（跨页保持）
            if _EQ_ENTER_RE.match(line):
                in_equities = True
            elif _EQ_LEAVE_RE.match(line):
                if _TOTAL_EQ_RE.search(line) and i + 1 < len(lines):
                    v = _num(lines[i + 1].strip())
                    if v and v > 0:
                        total_eq = v
                in_equities = False
            sm = _CCY_SECTION_RE.search(line)
            if sm:
                cur_ccy = sm.group(1)

            # 锚点：个股 Ticker 或（EQUITIES 区内的）ETF ISIN
            tm = _TICKER_RE.match(line)
            im = _ISIN_RE.match(line)
            if tm:
                symbol = tm.group(1)
            elif im and in_equities:
                symbol = im.group(1)          # ETF 暂用 ISIN 作标识（P2 再映射到可交易代码）
            else:
                continue
            if i < 10:
                continue

            qty = _num(lines[i - 10].strip())
            mv_nominal = _num(lines[i - 6].strip())   # 原币市值（审计）
            mv_usd = _num(lines[i - 4].strip())       # Total Value USD（统一口径）★
            company = lines[i - 3].strip()
            if qty and mv_usd and qty > 0 and mv_usd > 0:
                try:
                    holdings.append(EquityHolding(
                        ticker=symbol, company=company, quantity=qty,
                        market_value_usd=mv_usd, nominal_ccy=cur_ccy,
                        market_value_nominal=mv_nominal,
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


def _parse_nomura_asof(pages: list[str]) -> str:
    """Nomura 首页面眉 `As Of Date: 02−JUN−2026` → ISO `2026-06-02`。"""
    head = "\n".join(pages[:2]).upper()
    m = re.search(r"AS OF DATE:\s*(\d{1,2})[-−](JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[-−](\d{4})", head)
    if not m:
        return ""
    return f"{m.group(3)}-{_MONTH[m.group(2)]}-{int(m.group(1)):02d}"


def _clean_nomura_lines(pages: list[str]) -> list[str]:
    lines = [re.sub(r"\s+", " ", x).strip() for pg in pages for x in pg.splitlines() if x.strip()]
    out = []
    for s in lines:
        # fitz 对 PDF 中的 en-dash / Unicode 连字符常解成各种字符，统一规整后再匹配
        s = s.replace("−", "-").replace("–", "-").replace("—", "-").replace("��", "-")
        if s in {"BANK COPY", "Page", "Reference currency", "USD", "HKD", "Cash", "Equities", "Derivatives", "Structured Products"}:
            out.append(s)
            continue
        if s.startswith("Client Account no") or s.startswith("Portfolio no") or s.startswith("Portfolio name") \
           or s.startswith("Statement as of") or s.startswith("DD.MM.YYYY") or s.startswith("Page "):
            continue
        out.append(s)
    return out


def _parse_cash(pages: list[str]) -> tuple[list[CashBalance], float]:
    """抽 `INVESTABLE CASH BY CURRENCY` 明细 + `TOTAL CASH` 权威合计（统一美元口径）。

    p5 结构：Currency / % Total / Market Value Nominal Currency / Market Value USD
      USD / 74.08% / 719,962.81 / 719,962.81
      HKD / 25.92% / 1,975,915.99 / 251,969.03
    p12 结构：`TOTAL CASH` 下一行即统一美元总额。
    """
    balances: list[CashBalance] = []
    total_cash_usd = 0.0
    for page_text in pages:
        lines = [x.strip() for x in page_text.splitlines()]
        for i, line in enumerate(lines):
            if line == "INVESTABLE CASH BY CURRENCY":
                j = i + 1
                while j + 3 < len(lines):
                    ccy = lines[j]
                    if ccy == "EUR":          # 真实样本里 EUR 小节后为空，点到即止
                        break
                    if re.fullmatch(r"[A-Z]{3}", ccy) and re.fullmatch(r"-?\d[\d,]*(?:\.\d+)?%", lines[j + 1]):
                        nom = _num(lines[j + 2])
                        usd = _num(lines[j + 3])
                        if nom is not None and usd is not None:
                            balances.append(CashBalance(currency=ccy, market_value_nominal=nom, market_value_usd=usd))
                            j += 4
                            continue
                    j += 1
            if line == "TOTAL CASH" and i + 1 < len(lines):
                v = _num(lines[i + 1])
                if v and v > 0:
                    total_cash_usd = v
    # 若找不到 TOTAL CASH，退化为逐币种美元和
    if total_cash_usd <= 0:
        total_cash_usd = round(sum(x.market_value_usd for x in balances), 2)
    return balances, total_cash_usd


# ── 语句内对账 ────────────────────────────────────────────────────────────

_RECON_TOL = 0.005   # 0.5% 容差（口径已统一为 Total Value USD，仅留四舍五入余量）

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


# ── Broker 检测与 parser registry（C3）────────────────────────────────────

def detect_broker(pages: list[str], filename: str = "", hint: str = "") -> str:
    """返回 broker id：显式 hint > 文本/文件名探测 > 'unknown'。"""
    if hint:
        h = hint.strip().lower()
        if h in ("citi", "citibank", "citigroup"):
            return "citi"
        if h in ("nomura", "nsl"):
            return "nomura"
    head = "\n".join(pages[:3]).lower()
    fname = filename.lower()
    if "citibank" in head or "citi private bank" in head or "integrated statement" in fname:
        return "citi"
    if "nomura singapore limited" in head or "portfolio statement" in head:
        return "nomura"
    return "unknown"


def _parse_citi_statement(pages: list[str], filename: str, content_hash: str) -> BrokerStatement:
    holdings, total_eq = _parse_equities(pages)
    cash_balances, total_cash_usd = _parse_cash(pages)
    recon = _reconcile(holdings, total_eq)
    period = _parse_period(filename)
    return BrokerStatement(
        broker="citi",
        content_hash=content_hash,
        period_end=period,
        holdings=holdings,
        cash_balances=cash_balances,
        total_cash_usd=total_cash_usd,
        recon=recon,
    )


def _parse_nomura_cash(pages: list[str]) -> tuple[list[CashBalance], float]:
    lines = _clean_nomura_lines(pages)
    balances: list[CashBalance] = []
    total_cash_usd = 0.0
    for i, line in enumerate(lines):
        if "Position Details" not in line or "Money Account" not in line:
            continue
        j = i + 1
        while j + 2 < len(lines):
            if "Position Details" in lines[j] and "Equities" in lines[j]:
                break
            if re.fullmatch(r"[A-Z]{3}", lines[j]) and _num(lines[j + 1]) is not None and _num(lines[j + 2]) is not None:
                balances.append(CashBalance(currency=lines[j], market_value_nominal=_num(lines[j + 1]),
                                            market_value_usd=_num(lines[j + 2])))
                j += 3
                continue
            if lines[j] == "Total" and _num(lines[j + 1]) is not None:
                total_cash_usd = _num(lines[j + 1]) or 0.0
                break
            j += 1
    if total_cash_usd <= 0:
        total_cash_usd = round(sum(x.market_value_usd for x in balances), 2)
    return balances, total_cash_usd


def _parse_nomura_summary(pages: list[str]) -> dict:
    """抽 Nomura 账户层锚点（完整账户口径）：cash / equities / derivatives / total_liabilities / NAV。"""
    lines = _clean_nomura_lines(pages)
    summary = {
        "cash_total_usd": 0.0,
        "equities_total_usd": 0.0,
        "derivatives_total_usd": 0.0,
        "gross_asset_value_usd": 0.0,
        "total_liabilities_usd": 0.0,
        "net_asset_value_usd": 0.0,
    }
    for i, line in enumerate(lines):
        if line == "Cash" and i + 17 < len(lines) and lines[i + 17].endswith('%'):
            summary["cash_total_usd"] = _num(lines[i + 17]) or summary["cash_total_usd"]
        elif line == "Equities" and i + 17 < len(lines) and lines[i + 17].endswith('%'):
            summary["equities_total_usd"] = _num(lines[i + 17]) or summary["equities_total_usd"]
        elif line == "Derivatives" and i + 17 < len(lines):
            # 在 Asset Allocation 里，Derivatives 的 Total(USD) 列落在 +17
            v = _num(lines[i + 17])
            if v is not None:
                summary["derivatives_total_usd"] = v
        elif line == "Gross Asset Value" and i + 17 < len(lines):
            summary["gross_asset_value_usd"] = _num(lines[i + 17]) or summary["gross_asset_value_usd"]
        elif line == "Total Liabilities" and i + 17 < len(lines):
            summary["total_liabilities_usd"] = _num(lines[i + 17]) or summary["total_liabilities_usd"]
        elif line == "Net Asset Value" and i + 17 < len(lines):
            summary["net_asset_value_usd"] = _num(lines[i + 17]) or summary["net_asset_value_usd"]
    return summary


def _nomura_symbol(company_line: str) -> str:
    m = re.search(r"\(([A-Z0-9]{1,6})\s+[A-Z]{2}\)", company_line)
    return m.group(1) if m else company_line.split()[0].strip().upper()


def _parse_nomura_equities(pages: list[str]) -> list[EquityHolding]:
    lines = _clean_nomura_lines(pages)
    out: list[EquityHolding] = []
    in_eq = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if "Position Details" in line and "Equities" in line:
            in_eq = True
            i += 1
            continue
        if in_eq and (("Position Details" in line and ("Derivatives" in line or "Deposit" in line)) or line.startswith("Completed Transactions")):
            in_eq = False
        if not in_eq:
            i += 1
            continue
        # 币种行 + 公司名 + Sector + ISIN + s + qty + avg + mkt + mv_nom + pnl_usd + value_usd ...
        if re.fullmatch(r"[A-Z]{3}", line) and i + 10 < len(lines) and lines[i + 2].startswith("Sector"):
            ccy = line
            company = lines[i + 1]
            qty = _num(lines[i + 5])
            mv_nom = _num(lines[i + 8])
            mv_usd = _num(lines[i + 10])
            symbol = _nomura_symbol(company)
            if qty is not None and mv_nom is not None and mv_usd is not None and mv_usd > 0:
                out.append(EquityHolding(ticker=symbol, company=company, quantity=qty,
                                         nominal_ccy=ccy, market_value_nominal=mv_nom,
                                         market_value_usd=mv_usd))
            i += 11
            continue
        i += 1
    return out


def _parse_nomura_statement(pages: list[str], filename: str, content_hash: str) -> BrokerStatement:
    holdings = _parse_nomura_equities(pages)
    cash_balances, total_cash_usd = _parse_nomura_cash(pages)
    summary = _parse_nomura_summary(pages)
    period = _parse_nomura_asof(pages)
    # Nomura 完整账户以 NAV 为总权益锚；持仓对子账（Equities）先不做 statement_total 对账。
    recon = ReconResult(holdings_count=len(holdings), holdings_total_usd=sum(h.market_value_usd for h in holdings),
                       statement_equities_total_usd=summary.get("equities_total_usd") or None,
                       delta_usd=None, status="no_statement_total")
    return BrokerStatement(
        broker="nomura", content_hash=content_hash, period_end=period,
        holdings=holdings, cash_balances=cash_balances, total_cash_usd=total_cash_usd,
        account_summary=summary, recon=recon,
    )


_PARSERS = {
    "citi": _parse_citi_statement,
    "nomura": _parse_nomura_statement,
}


# ── 公开入口 ──────────────────────────────────────────────────────────────


def ingest_pdf(pdf_bytes: bytes, filename: str = "", broker_hint: str = "", pdf_password: str = "") -> BrokerStatement:
    """解析 PDF → BrokerStatement（含对账结果）。不写库，纯解析。

    仅做 broker dispatch；未知格式不在这里硬猜，直接抛 unsupported_broker。
    调用方若要接 LLM fallback，应在 ingest_and_store(user_id 可用处) 处理。
    """
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    pages = _extract_pages(pdf_bytes, pdf_password=pdf_password)
    broker = detect_broker(pages, filename, broker_hint)
    parser = _PARSERS.get(broker)
    if not parser:
        raise ValueError(f"unsupported_broker:{broker or 'unknown'}")
    return parser(pages, filename, content_hash)


def ingest_and_store(pdf_bytes: bytes, filename: str,
                     user_id: str, market: str = "us_stock",
                     broker: str = "citi", pdf_password: str = "") -> dict:
    """解析 + 加密落 financial_documents。返回 {doc_id, status, recon}。

    幂等：同用户同文件哈希已存在则直接返回已有 doc_id（不重复写）。
    """
    from bottleneck_hunter.auth.store import AuthStore

    stmt = ingest_pdf(pdf_bytes, filename, broker_hint=broker, pdf_password=pdf_password)
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
        broker=stmt.broker,
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
        ccy = "" if h.nominal_ccy == "USD" else f"  [{h.nominal_ccy} {h.market_value_nominal:,.0f}]"
        print(f"    {h.ticker:6} {h.company[:28]:28} {h.quantity:>8,.0f}股  ${h.market_value_usd:>14,.2f}{ccy}")
    assert stmt.recon.holdings_count > 0, "未抽到持仓"
    assert stmt.recon.status in ("ok", "no_statement_total", "mismatch")
    print("ingest demo 通过")


if __name__ == "__main__":
    demo()
