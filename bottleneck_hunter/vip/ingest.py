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
from datetime import datetime
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
    # Phase A：成本/盈亏（结算单已含，此前未抽）。均可选——结单无该列时留 None，不猜。
    avg_cost: float | None = None           # 平均/单位成本（原币口径，同 quantity）
    cost_basis_usd: float | None = None     # 总成本基准（统一美元口径，与 market_value_usd 可直接相减）
    unrealized_pnl_usd: float | None = None # 未实现盈亏（= market_value_usd − cost_basis_usd，自算以规避负值括号解析）

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


class StatementTransaction(BaseModel):
    ticker: str = ""
    company: str = ""
    quantity: float = 0.0
    txn_type: str
    trade_date: str
    settle_date: str = ""
    price: float = 0.0
    gross_amount: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    net_amount: float = 0.0
    currency: str = "USD"
    fx_rate: float = 1.0
    external_id: str = ""
    description: str = ""
    cusip: str = ""
    isin: str = ""
    account_ref: str = ""

    @field_validator("ticker", "currency", mode="before")
    @classmethod
    def _upper(cls, v: str) -> str:
        return (v or "").strip().upper()


class BrokerStatement(BaseModel):
    broker: str = "citi"
    period_end: str = ""          # ISO 格式 YYYY-MM-DD
    content_hash: str
    holdings: list[EquityHolding] = []
    cash_balances: list[CashBalance] = []
    total_cash_usd: float = 0.0
    account_summary: dict = {}      # 可选：完整账户层摘要（如 Nomura 的 NAV/负债/衍生品合计）
    transactions: list[StatementTransaction] = []
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


_DOC_TYPES = {"monthly_statement", "trade_confirm", "position_report", "unsupported"}
_BROKERS = {"citi", "nomura", "unknown"}
_REASON_CODES = {"content_match", "insufficient_content", "ambiguous", "unsupported_format"}


def _clip_pages_for_classify(pages: list[str], max_pages: int = 4, max_chars: int = 1800) -> str:
    parts = []
    for i, page in enumerate(pages[:max_pages], start=1):
        text = re.sub(r"\s+", " ", page or "").strip()
        parts.append(f"[第{i}页]\n{text[:max_chars]}")
    return "\n\n".join(parts)


def _normalize_broker(raw: str) -> str:
    v = (raw or "").strip().lower()
    if v in ("citi", "citibank", "citigroup"):
        return "citi"
    if v in ("nomura", "nsl"):
        return "nomura"
    return "unknown"


def _normalize_doc_type(raw: str) -> str:
    v = (raw or "").strip().lower()
    return v if v in _DOC_TYPES else "unsupported"


def _heuristic_statement_classification(pages: list[str], filename: str = "", broker_hint: str = "") -> dict:
    broker = detect_broker(pages, filename=filename, hint=broker_hint)
    doc_type = "unsupported"
    reason_code = "unsupported_format"
    if broker == "citi":
        doc_type = _detect_citi_doc_type(pages, filename=filename)
        reason_code = "content_match" if doc_type != "monthly_statement" else "ambiguous"
    elif broker == "nomura":
        doc_type = "monthly_statement"
        reason_code = "content_match"
    return {
        "broker": broker,
        "doc_type": doc_type,
        "confidence": 0.51,
        "reason_code": reason_code,
        "source": "heuristic",
    }


def _llm_statement_classification(pages: list[str], filename: str, user_id: str) -> dict | None:
    from langchain_core.messages import HumanMessage, SystemMessage

    from bottleneck_hunter.chain.json_utils import extract_json_object
    from bottleneck_hunter.llm_clients.factory import get_models_for_role

    models = get_models_for_role("vip_statement_extract", user_id=user_id, with_fallback=False)
    if not models:
        return None
    llm = models[0][0]
    prompt = (
        "你是 VIP 对账单导入分类器。只做封闭枚举判断，不做自由发挥。\n"
        "必须只返回一个 JSON 对象，字段固定：\n"
        '{"doc_type":"monthly_statement|trade_confirm|position_report|unsupported",'
        '"broker":"citi|nomura|unknown","confidence":0-1,'
        '"reason_code":"content_match|insufficient_content|ambiguous|unsupported_format"}\n'
        "优先依据正文内容；文件名只作弱参考。若信息不足，宁可给 unsupported/unknown，也不要猜。"
    )
    page_text = _clip_pages_for_classify(pages)
    resp = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content=f"文件名：{filename or 'unknown'}\n\n正文摘录：\n{page_text}"),
    ])
    data = extract_json_object(getattr(resp, "content", "") or "")
    broker = _normalize_broker(data.get("broker", ""))
    doc_type = _normalize_doc_type(data.get("doc_type", ""))
    reason_code = (data.get("reason_code") or "").strip().lower()
    if reason_code not in _REASON_CODES:
        raise ValueError("invalid_reason_code")
    confidence = data.get("confidence", 0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        raise ValueError("invalid_confidence") from None
    if not 0 <= confidence <= 1:
        raise ValueError("invalid_confidence")
    return {
        "broker": broker,
        "doc_type": doc_type,
        "confidence": confidence,
        "reason_code": reason_code,
        "source": "llm",
    }


def _classify_statement_content(pages: list[str], filename: str, user_id: str, broker_hint: str = "") -> dict:
    heuristic = _heuristic_statement_classification(pages, filename=filename, broker_hint=broker_hint)
    try:
        llm_result = _llm_statement_classification(pages, filename, user_id)
    except Exception:  # noqa: BLE001 - 无 key / 超时 / 非法 JSON 均静默退回规则
        return heuristic
    if not llm_result:
        return heuristic
    if llm_result["broker"] == "unknown" and heuristic["broker"] != "unknown":
        llm_result["broker"] = heuristic["broker"]
    return llm_result


def _doc_type_from_statement(stmt: BrokerStatement, classified_doc_type: str = "") -> str:
    if stmt.transactions:
        return "trade_confirm"
    if classified_doc_type == "position_report":
        return "position_report"
    return "monthly_statement"


def _statement_is_empty(stmt: BrokerStatement) -> bool:
    """纯判定：解析结果是否为"空文档"——无持仓/现金/交易，账户层合计也无正锚点。

    调用方(ingest_and_store)仅对 monthly_statement 收窄拒收：野村结构化产品条款书(irf-*)、
    风险披露页、以及被误判券商后套错解析器的文档，都会落到月结单桶并抽成 0，需拒收。
    ponytail: 账户真为空仓且无现金属极端场景；此时宁可拒收提示，也不落一份空快照污染总览。
    """
    if stmt.holdings or stmt.cash_balances or stmt.transactions:
        return False
    summ = stmt.account_summary or {}
    anchors = ("net_asset_value_usd", "gross_asset_value_usd", "equities_total_usd", "cash_total_usd")
    return not any((summ.get(k) or 0) > 0 for k in anchors)


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
_ACCOUNT_RE = re.compile(r"^\d+/X{3}\d+/\d+$")
_REPORT_CCY_RE = re.compile(r"(?:报告货币|[^A-Z0-9]{2,})[:：]\s*([A-Z]{3})$")
_POSITION_NOISE = {
    "花旗私人银行", "参见重要信息披露", "仓盘", "描述", "账⼾号码", "帐⼾描述", "帐⼾代号",
    "当前值", "与前⼀天相⽐的变化", "与前⼀天相⽐的变化率", "名义单位", "市价", "资产级别",
    "平均或单位成本", "总成本基准", "未实现盈/(亏)", "未实现盈/(亏)%", "资产级别 - 全部持仓",
    "资产级别 -  全部持仓", "应⽤的筛选器： ⽆应⽤的筛选器",
}


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
            unit_cost = _num(lines[i - 9].strip())    # 单价/平均成本（原币）
            cost_nom = _num(lines[i - 8].strip())     # 总成本基准（原币）
            mv_nominal = _num(lines[i - 6].strip())   # 原币市值（审计）
            mv_usd = _num(lines[i - 4].strip())       # Total Value USD（统一口径）★
            company = lines[i - 3].strip()
            if qty and mv_usd and qty > 0 and mv_usd > 0:
                # 成本换算 USD：用 市值USD/市值原币 推汇率（USD 持仓比值=1）。总成本恒为正，_num 可靠。
                # 未实现盈亏一律自算(mv_usd − cost_usd)，规避结单负值括号 (x) 令 _num 返 None 的坑。
                cost_usd = None
                if cost_nom is not None and mv_nominal and mv_nominal > 0:
                    cost_usd = round(cost_nom * (mv_usd / mv_nominal), 2)
                upnl_usd = round(mv_usd - cost_usd, 2) if cost_usd is not None else None
                try:
                    holdings.append(EquityHolding(
                        ticker=symbol, company=company, quantity=qty,
                        market_value_usd=mv_usd, nominal_ccy=cur_ccy,
                        market_value_nominal=mv_nominal,
                        avg_cost=unit_cost, cost_basis_usd=cost_usd,
                        unrealized_pnl_usd=upnl_usd,
                    ))
                except Exception:  # noqa: BLE001
                    pass

    return holdings, total_eq


def _clean_position_line(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip())


def _looks_like_position_desc(line: str) -> bool:
    if not line or line == "-" or _ACCOUNT_RE.match(line):
        return False
    if line == "Ticker/ISIN" or line.startswith("ISIN "):
        return True
    if _currency_amount(line):
        return False
    if line.startswith("## ") or line.endswith("%"):
        return False
    if re.fullmatch(r"\(?-?[\d,]+(?:\.\d+)?\)?", line):
        return False
    if re.fullmatch(r"[A-Z0-9]{1,8}/[A-Z0-9]{6,}", line):
        return True
    return bool(re.search(r"[A-Za-z]", line))


def _position_usd_value(report_value: tuple[str, float] | None,
                        local_value: tuple[str, float] | None,
                        report_ccy: str,
                        report_per_usd: float | None) -> float:
    if local_value and local_value[0] == "USD":
        return local_value[1]
    if local_value and local_value[0] == report_ccy:
        return local_value[1]
    if report_value and report_value[0] == report_ccy and report_per_usd and report_per_usd > 0:
        return round(report_value[1] / report_per_usd, 2)
    return 0.0


def _parse_citi_position_report(pages: list[str], filename: str, content_hash: str) -> BrokerStatement:
    """解析花旗仓盘导出：按持仓块抽股票/基金/ETF/现金，结构化产品/负债先跳过。"""
    lines = [_clean_position_line(x) for pg in pages for x in pg.splitlines() if x.strip()]
    report_ccy = "USD"
    period = _parse_period(filename)
    for line in lines:
        m = _REPORT_CCY_RE.search(line)
        if m:
            report_ccy = m.group(1)
            break
    if not period:
        for line in lines:
            if "⻚⾯打印在" not in line:
                continue
            m = re.search(r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{4})", line)
            if not m:
                continue
            try:
                period = datetime.strptime(" ".join(m.groups()), "%d %b %Y").strftime("%Y-%m-%d")
                break
            except ValueError:
                pass

    holdings: list[EquityHolding] = []
    cash_balances: list[CashBalance] = []
    anchors = [i for i, line in enumerate(lines) if line == "Investment Advisory Portfolio"]
    last_end = 0
    report_per_usd: float | None = None

    for idx, anchor in enumerate(anchors):
        if anchor + 9 >= len(lines):
            continue
        next_anchor = anchors[idx + 1] if idx + 1 < len(anchors) else len(lines)
        data = lines[anchor:next_anchor]
        scan_start = max(last_end, anchor - 8)
        desc_block = [x for x in lines[scan_start:anchor] if _looks_like_position_desc(x)]
        last_end = anchor + 13

        report_value = _currency_amount(data[2]) if len(data) > 2 else None
        local_value = _currency_amount(data[3]) if len(data) > 3 else None
        if report_value and local_value and local_value[0] == "USD" and local_value[1] > 0:
            report_per_usd = report_value[1] / local_value[1]

        symbol = ""
        company_lines = desc_block[:]
        for pos, entry in enumerate(desc_block):
            if entry == "Ticker/ISIN" and pos + 1 < len(desc_block):
                symbol = desc_block[pos + 1].split("/", 1)[0].strip().upper()
                company_lines = desc_block[:pos]
                break
            if entry.startswith("ISIN "):
                symbol = entry.split()[-1].strip().upper()
                company_lines = desc_block[:pos]
                break
        company_lines = [x for x in company_lines if not _ACCOUNT_RE.match(x)]
        company = " ".join(company_lines).strip()
        company_upper = company.upper()

        if not company or not report_value or not local_value:
            continue
        if company_upper.startswith("LOAN ACCOUNT"):
            continue

        is_cash = "DEPOSIT" in company_upper or "CHECKING ACCOUNT" in company_upper
        if is_cash:
            usd_value = _position_usd_value(report_value, local_value, report_ccy, report_per_usd)
            cash_balances.append(CashBalance(
                currency=local_value[0],
                market_value_nominal=local_value[1],
                market_value_usd=usd_value,
            ))
            continue

        if not symbol:
            continue

        qty = _num(data[7]) if len(data) > 7 else None
        if not qty or qty <= 0:
            qty = 1.0
        usd_value = _position_usd_value(report_value, local_value, report_ccy, report_per_usd)
        holdings.append(EquityHolding(
            ticker=symbol,
            company=company,
            quantity=qty,
            nominal_ccy=local_value[0],
            market_value_nominal=local_value[1],
            market_value_usd=usd_value,
        ))

    total_cash_usd = round(sum(x.market_value_usd for x in cash_balances), 2)
    recon = ReconResult(
        holdings_count=len(holdings),
        holdings_total_usd=round(sum(h.market_value_usd for h in holdings), 2),
        statement_equities_total_usd=None,
        delta_usd=None,
        status="no_statement_total",
    )
    return BrokerStatement(
        broker="citi",
        content_hash=content_hash,
        period_end=period,
        holdings=holdings,
        cash_balances=cash_balances,
        total_cash_usd=total_cash_usd,
        account_summary={"report_currency": report_ccy},
        recon=recon,
    )


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


def _parse_trade_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _strip_cny_prefix(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("CNY"):
        s = s[3:].strip()
    return s


def _currency_amount(raw: str) -> tuple[str, float] | None:
    s = (raw or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("() ")
    m = re.match(r"([A-Z$€¥£HKDUSDJPYCNHEURAUDSGD]{1,4})\s*([\d,]+(?:\.\d+)?)$", s)
    if not m:
        return None
    ccy = m.group(1)
    ccy = {
        "$": "USD",
        "€": "EUR",
        "¥": "JPY",
        "£": "GBP",
    }.get(ccy, ccy)
    amt = _num(m.group(2))
    if amt is None:
        return None
    return ccy, -amt if neg else amt


def _safe_token(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (raw or "").upper())


def _map_citi_txn_type(kind: str, desc: str, amount_ccy: str, amount_value: float) -> str:
    k = (kind or "").strip().upper()
    d = (desc or "").strip().upper()
    if k in ("已购入证券", "BUY"):
        return "buy"
    if k in ("已售出证券", "SELL"):
        return "sell"
    if k in ("股息", "分配 - 资本收益", "DIVIDEND"):
        return "dividend"
    if k in ("分配 - 资本回报", "RETURN OF CAPITAL"):
        return "transfer_in"
    if k in ("利息收入", "贷款利息续期", "INTEREST"):
        return "interest"
    if k in ("贷款利息付款", "存仓费", "FEE", "LOAN INTEREST PAYMENT"):
        return "fee"
    if k in ("出资（超出承担额）", "CAPITAL CONTRIBUTION"):
        return "withdrawal"
    if k in ("贷款付款", "到期/赎回", "LOAN PAYMENT", "REDEMPTION"):
        return "withdrawal" if amount_value < 0 else "deposit"
    if k in ("支用贷款", "新记账", "DEPOSIT", "CASH TRANSFER", "LOAN DRAWDOWN"):
        return "deposit"
    if "LOAN" in d and amount_ccy == "EUR":
        return "withdrawal" if amount_value < 0 else "deposit"
    return "withdrawal" if amount_value < 0 else "deposit"


def _build_citi_external_id(trade_date: str, account_ref: str, kind: str, desc: str,
                            amount_ccy: str, amount_value: float, isin: str, cusip: str) -> str:
    parts = [trade_date, account_ref, kind, _safe_token(desc)[:40], amount_ccy, f"{amount_value:.2f}", isin or cusip]
    return "citi-" + "-".join(p for p in parts if p)


def _parse_citi_transactions(pages: list[str]) -> list[StatementTransaction]:
    """解析花旗交易导出 PDF（当前样本格式：日期 + 8~10 行列值）。"""
    lines = [re.sub(r"\s+", " ", x).strip() for pg in pages for x in pg.splitlines() if x.strip()]
    out: list[StatementTransaction] = []
    i = 0
    while i < len(lines):
        trade_date = _parse_trade_date(lines[i])
        if not trade_date:
            i += 1
            continue
        if i + 8 >= len(lines):
            break
        desc1 = lines[i + 1]
        account_ref = lines[i + 2]
        account_code = lines[i + 3]
        account_desc = lines[i + 4]
        kind = lines[i + 5]

        # 真实记录总是日期后紧跟账户号；多行描述记录第一版先跳过，避免错位脏解析。
        if not re.match(r"\d+/X{3}\d+/\d+", account_ref):
            i += 1
            continue
        if account_code != "-":
            i += 1
            continue

        amount_cny_raw = lines[i + 6]
        amount_txn_raw = lines[i + 7]
        cusip = lines[i + 8] if i + 8 < len(lines) else ""
        isin = lines[i + 9] if i + 9 < len(lines) else ""

        amount_cny = _currency_amount(_strip_cny_prefix(amount_cny_raw))
        amount_txn = _currency_amount(amount_txn_raw)
        if not amount_txn:
            i += 1
            continue
        txn_currency, txn_amount = amount_txn
        cny_amount = amount_cny[1] if amount_cny else 0.0
        fx_rate = abs(cny_amount / txn_amount) if txn_amount else 1.0

        isin_clean = isin if isin != "-" else ""
        cusip_clean = cusip if cusip != "-" else ""
        txn_type = _map_citi_txn_type(kind, desc1, txn_currency, txn_amount)
        net_amount = txn_amount
        if txn_type == "buy" and net_amount > 0:
            net_amount = -net_amount
        elif txn_type in ("sell", "dividend", "deposit", "interest", "transfer_in") and net_amount < 0:
            net_amount = -net_amount
        elif txn_type in ("fee", "withdrawal") and net_amount > 0:
            net_amount = -net_amount

        out.append(StatementTransaction(
            company=desc1,
            txn_type=txn_type,
            trade_date=trade_date,
            currency=txn_currency,
            gross_amount=txn_amount,
            net_amount=net_amount,
            fx_rate=round(fx_rate, 6) if fx_rate else 1.0,
            external_id=_build_citi_external_id(trade_date, account_ref, kind, desc1, txn_currency, txn_amount, isin_clean, cusip_clean),
            description=f"{desc1} | {kind} | {account_desc}",
            cusip=cusip_clean,
            isin=isin_clean,
            account_ref=account_ref,
        ))
        i += 10 if isin else 9
    return out


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
    """返回 broker id：显式 hint > 正文探测 > 弱文件名兜底 > 'unknown'。"""
    if hint:
        h = hint.strip().lower()
        if h in ("citi", "citibank", "citigroup"):
            return "citi"
        if h in ("nomura", "nsl"):
            return "nomura"
    head = "\n".join(pages[:3]).lower()
    fname = filename.lower()
    if "citibank" in head or "citi private bank" in head or "花旗私人银行" in head:
        return "citi"
    # 券商品牌锚点(花旗/野村)必须先于下面 "account number" 这条泛化规则——
    # 否则含 "Account Number" 的野村单会被泛化规则截胡误判成花旗(见 Statement_260423 事故)。
    if "nomura singapore limited" in head or "portfolio statement" in head:
        return "nomura"
    if "transaction description" in head or "account number" in head:
        return "citi"
    if "integrated statement" in fname or "交易_" in fname or "仓盘_" in fname:
        return "citi"
    return "unknown"


def _detect_citi_doc_type(pages: list[str], filename: str = "") -> str:
    flat = "\n".join(pages[:4])
    flat_lc = flat.lower()
    fname = filename.lower()
    if (
        "资产级别 - 全部持仓" in flat
        or "资产级别 -  全部持仓" in flat
        or "ticker/isin" in flat_lc
    ):
        return "position_report"
    if "交易描述" in flat:
        return "trade_confirm"
    if "交易_" in fname and "integrated statement" not in fname:
        return "trade_confirm"
    if "仓盘_" in fname and "integrated statement" not in fname:
        return "position_report"
    return "monthly_statement"


def _parse_citi_statement(pages: list[str], filename: str, content_hash: str,
                          doc_type_hint: str = "") -> BrokerStatement:
    doc_type = doc_type_hint or _detect_citi_doc_type(pages, filename)
    holdings: list[EquityHolding] = []
    cash_balances: list[CashBalance] = []
    total_cash_usd = 0.0
    transactions: list[StatementTransaction] = []
    total_eq: Optional[float] = None
    period = _parse_period(filename)

    if doc_type == "trade_confirm":
        transactions = _parse_citi_transactions(pages)
        if not transactions:
            raise ValueError("unsupported_citi_trade_export")
        period = max((t.trade_date for t in transactions), default=period)
        recon = ReconResult(
            holdings_count=0,
            holdings_total_usd=0.0,
            statement_equities_total_usd=None,
            delta_usd=None,
            status="no_statement_total",
        )
        return BrokerStatement(
            broker="citi",
            content_hash=content_hash,
            period_end=period,
            holdings=[],
            cash_balances=[],
            total_cash_usd=0.0,
            transactions=transactions,
            recon=recon,
        )
    if doc_type == "position_report":
        return _parse_citi_position_report(pages, filename, content_hash)

    holdings, total_eq = _parse_equities(pages)
    cash_balances, total_cash_usd = _parse_cash(pages)
    recon = _reconcile(holdings, total_eq)
    return BrokerStatement(
        broker="citi",
        content_hash=content_hash,
        period_end=period,
        holdings=holdings,
        cash_balances=cash_balances,
        total_cash_usd=total_cash_usd,
        transactions=transactions,
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
            avg = _num(lines[i + 6])       # 平均成本（原币）
            mv_nom = _num(lines[i + 8])
            pnl_usd = _num(lines[i + 9])   # 未实现盈亏（USD，可能为负括号→None，下方自算兜底）
            mv_usd = _num(lines[i + 10])
            symbol = _nomura_symbol(company)
            if qty is not None and mv_nom is not None and mv_usd is not None and mv_usd > 0:
                # 成本基：qty×avg（原币）换算 USD。盈亏优先自算(mv_usd−cost_usd)，pnl_usd 仅作兜底。
                cost_usd = None
                if avg is not None and qty and mv_nom and mv_nom > 0:
                    cost_usd = round(qty * avg * (mv_usd / mv_nom), 2)
                upnl_usd = round(mv_usd - cost_usd, 2) if cost_usd is not None else pnl_usd
                out.append(EquityHolding(ticker=symbol, company=company, quantity=qty,
                                         nominal_ccy=ccy, market_value_nominal=mv_nom,
                                         market_value_usd=mv_usd,
                                         avg_cost=avg, cost_basis_usd=cost_usd,
                                         unrealized_pnl_usd=upnl_usd))
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


def ingest_pdf(pdf_bytes: bytes, filename: str = "", broker_hint: str = "", doc_type_hint: str = "",
               pdf_password: str = "") -> BrokerStatement:
    """解析 PDF → BrokerStatement（含对账结果）。不写库，纯解析。"""
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    pages = _extract_pages(pdf_bytes, pdf_password=pdf_password)
    broker = detect_broker(pages, filename, broker_hint)
    parser = _PARSERS.get(broker)
    if not parser:
        raise ValueError(f"unsupported_broker:{broker or 'unknown'}")
    if broker == "citi":
        return parser(pages, filename, content_hash, doc_type_hint=doc_type_hint)
    return parser(pages, filename, content_hash)


def ingest_and_store(pdf_bytes: bytes, filename: str,
                     user_id: str, market: str = "us_stock",
                     broker: str = "citi", pdf_password: str = "") -> dict:
    """解析 + 加密落 financial_documents。返回 {doc_id, status, recon}。

    幂等：同用户同文件哈希已存在则直接返回已有 doc_id（不重复写）。
    """
    from bottleneck_hunter.auth.store import AuthStore

    pages = _extract_pages(pdf_bytes, pdf_password=pdf_password)
    classification = _classify_statement_content(pages, filename, user_id=user_id, broker_hint=broker)
    content_hash = hashlib.sha256(pdf_bytes).hexdigest()
    store = AuthStore()

    # 幂等去重
    existing = store.find_financial_doc_by_hash(user_id, content_hash)
    if existing:
        return {
            "doc_id": existing["id"],
            "status": existing["status"],
            "recon": {},
            "duplicate": True,
            "doc_type": existing.get("doc_type", ""),
        }

    stmt = ingest_pdf(
        pdf_bytes,
        filename,
        broker_hint=classification["broker"] if classification["broker"] != "unknown" else broker,
        doc_type_hint=classification["doc_type"] if classification["doc_type"] != "unsupported" else "",
        pdf_password=pdf_password,
    )
    doc_type = _doc_type_from_statement(stmt, classification.get("doc_type", ""))
    # 月结单是"兜底桶"：误判券商/非持仓文档(条款书 irf-*、披露页)都会落到这里。
    # 月结单却抽不到任何持仓/现金/账户合计 → 拒收，不落 0 持仓僵尸快照污染总览。
    # (持仓单/交易单只在命中明确内容锚点时进入，不在此收窄，避免误伤。)
    # "unsupported" 关键字被 importer._import_statement 捕获 → 返回 rejected 提示。
    if doc_type == "monthly_statement" and _statement_is_empty(stmt):
        raise ValueError(f"unsupported_non_statement:{stmt.broker or 'unknown'}")
    db_status = "parsed_ok" if stmt.recon.status in ("ok", "no_statement_total") else "needs_review"
    recon_flags = {
        "equities_recon": stmt.recon.status,
        "holdings_count": stmt.recon.holdings_count,
        "transactions_count": len(stmt.transactions),
    }
    if stmt.recon.status == "mismatch":
        recon_flags["delta_flag"] = "fail"

    doc_id = store.create_financial_doc(
        user_id,
        content_hash=stmt.content_hash,
        market=market,
        broker=stmt.broker,
        doc_type=doc_type,
        period_end=stmt.period_end,
        file_name=filename,
        parsed_json=stmt.model_dump_json(),
        recon_flags=recon_flags,
        status=db_status,
    )
    return {"doc_id": doc_id, "status": db_status,
            "recon": stmt.recon.model_dump(), "duplicate": False, "doc_type": doc_type}


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
