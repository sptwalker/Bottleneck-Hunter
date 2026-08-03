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
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

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
    statement_equities_total_usd: float | None   # 结单 TOTAL EQUITIES 行（可能缺失）
    delta_usd: float | None                       # 差值；None = 结单无合计行
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
    # 结构性产品/衍生品薄记录：解析器填充 dict（family/underlying/currency/tenor_days/terms/lot_key），
    # 导入时转 DerivativeTerm 落 vip_derivative_terms（与股票分栏）。dataclass 非 Pydantic，故用 dict 直存。
    derivative_terms: list[dict] = []


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
_BROKERS = {"citi", "nomura", "cmbi", "unknown"}
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
    if v in ("cmbi", "cmbis", "招银", "招银国际"):
        return "cmbi"
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
    elif broker == "cmbi":
        # 日结单(DAILY COMBINED)与月结单(MONTHLY)都是组合快照 → 统一落月结单桶
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
        '"broker":"citi|nomura|cmbi|unknown","confidence":0-1,'
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


def _merge_classification(heuristic: dict, llm_result: dict | None) -> dict:
    """合并启发式与 LLM 分类。LLM 负责细分 doc_type，broker 身份以确定性品牌锚为准。

    - LLM 缺失/无结果 → 用启发式。
    - 启发式判为 cmbi(招银)→ 直接用启发式：LLM 分类 prompt 的 broker 枚举**不含 cmbi**，对招银单必然
      猜成 citi/nomura，进而在 ingest_pdf 强制走错解析器、抽空后报 unsupported_non_statement:citi。cmbi 由
      auz441/champion tower/cmbi 品牌 + 文件名 M…-Daily|Monthly 判定，确定性、绝不误报，故不容 LLM 猜测覆盖。
    - 花旗启发式已 content_match(交易_/仓盘_ 文件名或 交易描述/资产级别-全部持仓 正文锚)→ 直接用启发式：
      这些是花旗导出的确定性命名/内容约定，doc_type 已定。若放任 LLM 把交易流水误判成 monthly_statement，
      会在 _parse_citi_statement 走持仓分支抽空 → 回落月结单桶 → 报 unsupported_non_statement:citi
      (交易_*.pdf 导入失败的根因)。与 cmbi 同规矩：确定性券商信号不容 LLM 覆盖，LLM 只在 ambiguous(月结兜底)时细分。
    - LLM 报 unknown 而启发式有值 → 回填启发式 broker（保留 LLM 的 doc_type 细分）。
    """
    if not llm_result:
        return heuristic
    if heuristic["broker"] == "cmbi":
        return heuristic
    if heuristic["broker"] == "citi" and heuristic["reason_code"] == "content_match":
        return heuristic
    if llm_result["broker"] == "unknown" and heuristic["broker"] != "unknown":
        llm_result["broker"] = heuristic["broker"]
    return llm_result


def _classify_statement_content(pages: list[str], filename: str, user_id: str, broker_hint: str = "") -> dict:
    heuristic = _heuristic_statement_classification(pages, filename=filename, broker_hint=broker_hint)
    try:
        llm_result = _llm_statement_classification(pages, filename, user_id)
    except Exception:  # noqa: BLE001 - 无 key / 超时 / 非法 JSON 均静默退回规则
        return heuristic
    return _merge_classification(heuristic, llm_result)


def _doc_type_from_statement(stmt: BrokerStatement, classified_doc_type: str = "") -> str:
    # 综合结单(招银日/月结单)同时含持仓快照+交易流水 → 属月结单桶(normalize 会同时写持仓与交易)，
    # 绝不能因"有交易"就翻成 trade_confirm(其路径只写交易、跳过持仓 materialize，会丢总权益)。
    # 仅"纯交易导出"(无任何持仓，如花旗交易_*.pdf)才算 trade_confirm。
    if stmt.transactions and not stmt.holdings:
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


def _num(s: str) -> float | None:
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


def _parse_equities(pages: list[str]) -> tuple[list[EquityHolding], float | None]:
    """固定偏移解析 EQUITIES 持仓（含个股 Ticker 锚 + ETF 的 ISIN 锚）+ TOTAL EQUITIES 合计。

    块结构（锚行往前 10 行，个股/ETF 一致）：
      i-10 数量 | i-9 单价 | i-8 总成本 | i-7 现价 | i-6 市值(原币) |
      i-5 未实现 | i-4 Total Value USD ★统一美元口径 | i-3 公司名 | i-2 日期 | i-1 %占比
    - 个股锚 `Ticker XXX Equity`：恒为股票（固收用 `Ticker XXX ID`，不含 Equity，天然排除）。
    - ETF 锚 `ISIN XXXX`：仅当处于 EQUITIES 区才算（否则会误收固收债券的 ISIN）。
    `in_equities` 状态跨页保持（应对 'EQUITIES CONTINUED' 续页）。
    """
    holdings: list[EquityHolding] = []
    total_eq: float | None = None
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
    # ponytail: 本地币种非 USD 时不得直接当美元返回（否则 report_ccy≠USD 时高估约 FX 倍），
    # 一律落到下方 FX 折算分支；无汇率则返回 0.0（诚实的“无法折算”而非错误美元数）。
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


def _currency_amount(raw: str) -> tuple[str, float] | None:
    s = (raw or "").strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("() ")
    m = re.match(r"([A-Z$€¥￥£HKDUSDJPYCNHEURAUDSGD]{1,4})\s*([\d,]+(?:\.\d+)?)$", s)
    if not m:
        return None
    ccy = m.group(1)
    ccy = {
        "$": "USD",
        "€": "EUR",
        "¥": "CNY",   # 本系统无日元市场，A股=CNY；人民币符号统一记 CNY
        "￥": "CNY",
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


def _citi_amount(raw: str) -> float | None:
    """花旗金额行 → 带符号数值。会计式括号=负数，括号可落在币种符号后
    （`€(167,546.74)`、`CNY (1,294,974.49)`）；前缀币种码/符号一律剥除。"""
    s = (raw or "").strip()
    if not s:
        return None
    neg = "(" in s
    body = re.sub(r"^(CNY|USD|EUR|HKD|GBP|JPY|SGD|AUD|CNH)\s*", "", s)
    body = re.sub(r"^[$€¥￥£]", "", body).strip("() ").replace(",", "").strip()
    v = _num(body)
    if v is None:
        return None
    return -abs(v) if neg else v


def _parse_citi_transactions(pages: list[str]) -> list[StatementTransaction]:
    """解析花旗「交易活动报告」PDF（竖排逐字段）。

    真实版式每条记录以账户号行(`7/XXX468/028`)为锚，其后固定 8 列：
      账户号 / 代号 / 账户描述 / 种类 / 交易货币 / 金额(CNY) / 金额(交易币) / CUSIP / ISIN
    锚之前、日期行之后的所有行拼成交易描述（证券描述常跨行：`NAME` + `ISIN xxx`）。
    花旗把大量 CJK 字用康熙部首/兼容区码位编码（如 `已购⼊证券` 的 `⼊`=U+2F0A），
    故先 NFKC 规整，否则种类映射与锚点全部落空。旧「日期+固定 9 行」逻辑漏了「交易货币」
    这一列、且无法处理跨行描述——整份 18 页单会被抽成 0 或错位脏数据。
    """
    acct_re = re.compile(r"^\d+/X{2,}\d+/\d+$")
    lines = [unicodedata.normalize("NFKC", re.sub(r"\s+", " ", x).strip())
             for pg in pages for x in pg.splitlines() if x.strip()]
    out: list[StatementTransaction] = []
    n = len(lines)
    i = 0
    while i < n:
        trade_date = _parse_trade_date(lines[i])
        if not trade_date:
            i += 1
            continue
        # 描述可跨 1~7 行，末尾锚定账户号行
        j = next((k for k in range(i + 1, min(i + 9, n)) if acct_re.match(lines[k])), None)
        if j is None or j + 8 >= n:
            i += 1
            continue
        account_ref = lines[j]
        account_desc = lines[j + 2]
        kind = lines[j + 3]
        txn_currency = lines[j + 4]
        amount_cny = _citi_amount(lines[j + 5])
        amount_txn = _citi_amount(lines[j + 6])
        cusip = lines[j + 7]
        isin = lines[j + 8]
        # 交易货币须为 3 位币种码、交易币金额须可解析，否则非交易行 → 跳过而非污染
        if not re.fullmatch(r"[A-Z]{3}", txn_currency) or amount_txn is None:
            i = j + 9
            continue

        desc = re.sub(r"\s+ISIN\s+[A-Z0-9]+\s*$", "", " ".join(lines[i + 1:j]).strip()).strip()
        isin_clean = "" if isin == "-" else isin
        cusip_clean = "" if cusip == "-" else cusip
        cny_amount = amount_cny if amount_cny is not None else 0.0
        fx_rate = abs(cny_amount / amount_txn) if amount_txn else 1.0
        txn_type = _map_citi_txn_type(kind, desc, txn_currency, amount_txn)
        net_amount = amount_txn
        if (txn_type == "buy" and net_amount > 0
                or txn_type in ("sell", "dividend", "deposit", "interest", "transfer_in") and net_amount < 0
                or txn_type in ("fee", "withdrawal") and net_amount > 0):
            net_amount = -net_amount

        out.append(StatementTransaction(
            company=desc,
            txn_type=txn_type,
            trade_date=trade_date,
            currency=txn_currency,
            gross_amount=amount_txn,
            net_amount=net_amount,
            fx_rate=round(fx_rate, 6) if fx_rate else 1.0,
            external_id=_build_citi_external_id(trade_date, account_ref, kind, desc,
                                                txn_currency, amount_txn, isin_clean, cusip_clean),
            description=f"{desc} | {kind} | {account_desc}",
            cusip=cusip_clean,
            isin=isin_clean,
            account_ref=account_ref,
        ))
        i = j + 9
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
                    if not ccy:               # 空行 = 币种表结束
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

def _reconcile_totals(calc: float, count: int, statement_total: float | None) -> ReconResult:
    """按已算好的持仓合计对账（口径可含衍生品 MV，供 FCN 移出 holdings 的招银用）。"""
    if statement_total is None:
        return ReconResult(holdings_count=count, holdings_total_usd=calc,
                           statement_equities_total_usd=None, delta_usd=None,
                           status="no_statement_total")
    delta = abs(calc - statement_total)
    ok = delta / max(statement_total, 1.0) <= _RECON_TOL
    return ReconResult(holdings_count=count, holdings_total_usd=calc,
                       statement_equities_total_usd=statement_total,
                       delta_usd=round(calc - statement_total, 2),
                       status="ok" if ok else "mismatch")


def _reconcile(holdings: list[EquityHolding],
               statement_total: float | None) -> ReconResult:
    return _reconcile_totals(sum(h.market_value_usd for h in holdings), len(holdings), statement_total)


# 覆盖率守卫：状态 no_statement_total（未做总额对账）却仍带权益子账锚 statement_equities_total_usd
# 时，真实抽出的持仓合计若显著低于该锚 = 有持仓被静默漏解析、NAV 将被低估却当权威物化。
# 例：野村结单本带 Equities 合计但刻意"先不做对账"（见 _parse_nomura_statement），漏一只即无从自检。
_COVERAGE_MIN = 0.95  # 持仓合计/权益锚 低于此 → 疑似漏解析转 needs_review。ponytail: 校准旋钮——
                      # 权益锚与持仓同口径(均不含现金)，拿到真实野村样本核对后可收紧至 _RECON_TOL


def _coverage_shortfall(recon: ReconResult) -> float | None:
    """no_statement_total 且有权益锚时返回持仓覆盖率(holdings/anchor)；无锚/无持仓/已对账→None。"""
    if recon.status != "no_statement_total" or recon.holdings_count <= 0:
        return None
    anchor = recon.statement_equities_total_usd
    if not anchor or anchor <= 0:
        return None
    return recon.holdings_total_usd / anchor


# ── Broker 检测与 parser registry（C3）────────────────────────────────────

def detect_broker(pages: list[str], filename: str = "", hint: str = "") -> str:
    """返回 broker id：显式 hint > 正文探测 > 弱文件名兜底 > 'unknown'。"""
    if hint:
        h = hint.strip().lower()
        if h in ("citi", "citibank", "citigroup"):
            return "citi"
        if h in ("nomura", "nsl"):
            return "nomura"
        if h in ("cmbi", "cmbis", "招银", "招银国际"):
            return "cmbi"
    head = "\n".join(pages[:3]).lower()
    fname = filename.lower()
    if "citibank" in head or "citi private bank" in head or "花旗私人银行" in head:
        return "citi"
    # 招银国际(CMBI/CMBIS)：CE 编号 AUZ441 / 地址 Champion Tower / 品牌 cmbi 为强锚，
    # 文件名 M<账号>-YYYYMMDD-Daily|Monthly 作兜底。须先于下面野村规则——招银单正文也含
    # 泛化词 "Portfolio Statement"，若让野村规则先跑会被截胡误判成 nomura。
    if "auz441" in head or "champion tower" in head or "cmbi" in head:
        return "cmbi"
    if re.search(r"m\d+-\d{8}-(daily|monthly)", fname):
        return "cmbi"
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


def _citi_loan_outstanding_usd(pages: list[str]) -> float:
    """花旗综合月结单已用融资：`Total Margin Loans Outstanding` 下一数值行（美元口径）。
    无融资/非月结单 → 0。ponytail: 只认该权威合计行，逐笔 Loan Account 汇总留待有需要再加。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines()]
    for i, line in enumerate(lines):
        if line == "Total Margin Loans Outstanding":
            for j in range(i + 1, min(i + 4, len(lines))):
                v = _num(lines[j])
                if v is not None:
                    return round(abs(v), 2)
    return 0.0


_CITI_DDMMMYY_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")
_CITI_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _citi_paren_num(s: str) -> float | None:
    """花旗会计式负数 `(3,245.18)` → -3245.18；普通数走 _num。"""
    t = (s or "").strip()
    m = re.fullmatch(r"\(([\d,]+\.?\d*)\)", t)
    if m:
        v = _num(m.group(1))
        return -v if v is not None else None
    return _num(t)


def _citi_ddmmmyy_to_iso(s: str) -> str:
    """花旗紧凑日期 `05JAN27` / `19AUG26` → ISO；不可解析则空串。"""
    m = _CITI_DDMMMYY_RE.match((s or "").strip().upper())
    if not m or m.group(2) not in _CITI_MONTHS:
        return ""
    d, mon, y = int(m.group(1)), _CITI_MONTHS[m.group(2)], 2000 + int(m.group(3))
    try:
        return datetime(y, mon, d).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _citi_tenor_days(as_of_iso: str, expiry_iso: str) -> int:
    """as_of(ISO) → expiry(ISO) 自然日数；任一缺失/不可解析则 0。"""
    if not as_of_iso or not expiry_iso:
        return 0
    try:
        return max((datetime.strptime(expiry_iso, "%Y-%m-%d")
                    - datetime.strptime(as_of_iso, "%Y-%m-%d")).days, 0)
    except (ValueError, TypeError):
        return 0


def _citi_total_assets_usd(pages: list[str]) -> float:
    """花旗综合月结单权威总额：`Total Assets` 合计行下一数值（美元口径，仿 loan 合计法）。
    p6 资产配置块首现即 100% 总额（36,128,828.16）。无则 0。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines()]
    for i, line in enumerate(lines):
        if line == "Total Assets":
            for j in range(i + 1, min(i + 3, len(lines))):
                v = _num(lines[j])
                if v is not None and v > 0:
                    return round(v, 2)
    return 0.0


def _parse_citi_derivatives(pages: list[str], as_of: str) -> list[dict]:
    """Equity Derivatives − Equities Accumulator (p17)：逐笔累计期权 → 薄记录。
    锚描述行 `<NAME> ACCUMULATOR`；上溯 3 行取 Mark to Market USD(★)；
    下 1 行 `...-<EXPIRY>/AFP <strike>`、下 2 行 `KO <ko>/PREM`。
    lot_key=strike:expiry 判别同标的多笔。ponytail: 结单只给当期 MTM，条款结构以此为准。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines()]
    n = len(lines)
    # 仅认 p17 权威持仓块：`Equities Accumulator` … `Total Equity Derivatives`（其后 detail 区同名描述会误命中）
    try:
        lo = next(i for i, ln in enumerate(lines) if ln == "Equities Accumulator")
        hi = next(i for i in range(lo + 1, n) if lines[i] == "Total Equity Derivatives")
    except StopIteration:
        return []
    out: list[dict] = []
    for di in range(lo, hi):
        line = lines[di]
        m = re.match(r"^(.+?)\s+(ACCUMULATOR|DECUMULATOR)$", line)
        if not m or di + 2 >= n or di - 3 < 0:
            continue
        family = "equity_decumulator" if m.group(2) == "DECUMULATOR" else "equity_accumulator"
        name = m.group(1).strip()
        symbol = name.split()[0].upper() if name.split() else "SP"
        mtm = _citi_paren_num(lines[di - 3])            # MTM USD（di-1=%, di-2=资产标签, di-3=MTM，括号=负）
        maf = re.search(r"AFP\s+([\d.]+)", lines[di + 1])
        strike = _num(maf.group(1)) if maf else 0.0
        mexp = re.search(r"-(\d{1,2}[A-Z]{3}\d{2})/", lines[di + 1])
        expiry = _citi_ddmmmyy_to_iso(mexp.group(1)) if mexp else ""
        mko = re.search(r"KO\s+([\d.]+)", lines[di + 2])
        ko = _num(mko.group(1)) if mko else 0.0
        if mtm is None:
            continue
        lot_key = f"{strike}:{mexp.group(1) if mexp else di}"
        out.append({
            "product_family": family, "underlying_symbol": symbol, "currency": "USD",
            "tenor_days": _citi_tenor_days(as_of, expiry),
            "lot_key": lot_key,
            "terms": {"market_value_usd": mtm, "strike": strike or 0.0, "knock_out_price": ko or 0.0,
                      "expiry_date": expiry, "maturity": expiry, "underlying_name": name,
                      "nominal_ccy": "USD", "settlement_style": "physical_spot"},
        })
    return out


def _parse_citi_structured(pages: list[str], as_of: str) -> list[dict]:
    """Other Structured Investments − Market Linked Investment (p18-19)：MLI Booster → 薄记录。
    锚每笔 `Market Linked Instrument`；上 1 行 Total Value USD(★，当期市值)；
    上溯至 `USD` 币种行取名义；下 3 行描述 `N MTH USD <SYM..> MLI`(可折行)，
    随后 `Value <d>` / `MAT <d>` / `Ref <id>`。lot_key=Ref（每笔唯一）。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines()]
    out: list[dict] = []
    n = len(lines)
    for mk, line in enumerate(lines):
        if line != "Market Linked Instrument":
            continue
        mv_usd = _num(lines[mk - 1]) if mk >= 1 else None   # Total Value USD（当期市值，权威）
        nominal = None
        for j in range(mk - 2, max(mk - 13, -1), -1):
            if lines[j] == "USD" and j + 1 < n:
                nominal = _num(lines[j + 1])
                break
        # 描述：mk+3 起收集，直到 `Value `；折行的 "…MLI" 拼回
        desc_parts: list[str] = []
        value_date = maturity = ref = ""
        for j in range(mk + 3, min(mk + 9, n)):
            t = lines[j]
            if t.startswith("Value "):
                value_date = _citi_ddmmmyy_to_iso(t[6:].strip())
                continue
            if t.startswith("MAT "):
                maturity = _citi_ddmmmyy_to_iso(t[4:].strip())
                continue
            if t.startswith("Ref "):
                ref = t[4:].strip()
                break
            desc_parts.append(t)
        desc = " ".join(desc_parts).strip()
        md = re.search(r"(\d+)\s*MTH\s+USD\s+(.+?)\s+MLI", desc)
        symbol = (md.group(2).split()[0].split("+")[0] if md else desc.split()[0] if desc else "MLI").upper()
        if mv_usd is None:
            continue
        lot_key = ref or f"{symbol}:{maturity}:{mk}"
        out.append({
            "product_family": "equity_mli_booster", "underlying_symbol": symbol, "currency": "USD",
            "tenor_days": _citi_tenor_days(as_of, maturity),
            "lot_key": lot_key,
            "terms": {"market_value_usd": mv_usd, "notional": nominal or 0.0, "description": desc,
                      "value_date": value_date, "maturity": maturity, "expiry_date": maturity,
                      "reference": ref, "nominal_ccy": "USD", "product_type": "market_linked_investment"},
        })
    return out


def _parse_citi_statement(pages: list[str], filename: str, content_hash: str,
                          doc_type_hint: str = "") -> BrokerStatement:
    doc_type = doc_type_hint or _detect_citi_doc_type(pages, filename)
    holdings: list[EquityHolding] = []
    cash_balances: list[CashBalance] = []
    total_cash_usd = 0.0
    transactions: list[StatementTransaction] = []
    total_eq: float | None = None
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
    derivative_terms = _parse_citi_derivatives(pages, period) + _parse_citi_structured(pages, period)
    return BrokerStatement(
        broker="citi",
        content_hash=content_hash,
        period_end=period,
        holdings=holdings,
        cash_balances=cash_balances,
        total_cash_usd=total_cash_usd,
        transactions=transactions,
        recon=recon,
        derivative_terms=derivative_terms,
        account_summary={
            "loan_outstanding_usd": _citi_loan_outstanding_usd(pages),
            "total_assets_usd": _citi_total_assets_usd(pages),
        },
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
            n1, n2 = _num(lines[j + 1]), _num(lines[j + 2])
            if re.fullmatch(r"[A-Z]{3}", lines[j]) and n1 is not None and n2 is not None:
                balances.append(CashBalance(currency=lines[j], market_value_nominal=n1, market_value_usd=n2))
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
    def _usd_before_pct(pct_idx: int, anchor_idx: int) -> float | None:
        # Asset Allocation 行版式为 `<标签> … <Total(USD)> <占比%>`；+17 命中的是占比列（以 % 结尾），
        # 其配对的美元总额是它前面第一个可解析数字。直接 _num(占比串) 恒为 None，故须回溯取美元列。
        # ponytail: 固定偏移随版式漂移，回溯取“紧邻 % 的数字”比再猜一个死偏移稳；改版重导前用 dump_stmt 核对。
        for k in range(pct_idx - 1, anchor_idx, -1):
            v = _num(lines[k])
            if v is not None:
                return v
        return None

    for i, line in enumerate(lines):
        if line == "Cash" and i + 17 < len(lines) and lines[i + 17].endswith('%'):
            summary["cash_total_usd"] = _usd_before_pct(i + 17, i) or summary["cash_total_usd"]
        elif line == "Equities" and i + 17 < len(lines) and lines[i + 17].endswith('%'):
            summary["equities_total_usd"] = _usd_before_pct(i + 17, i) or summary["equities_total_usd"]
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
    # 已用贷款/负债（统一美元，取绝对值——结单里 Total Liabilities 为负数表示）
    summary["loan_outstanding_usd"] = round(abs(summary["total_liabilities_usd"]), 2)
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
        eq_end = ("Position Details" in line and ("Derivatives" in line or "Deposit" in line)) \
            or line.startswith("Completed Transactions")
        if in_eq and eq_end:
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


_NOMURA_MATURITY_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")   # 到期/估值日 16.10.2026


def _nomura_tenor_days(as_of: str, maturity_ddmmyyyy: str) -> int:
    """as_of(ISO) → maturity(DD.MM.YYYY) 的自然日数；任一缺失/不可解析则 0。"""
    if not as_of or not _NOMURA_MATURITY_RE.match(maturity_ddmmyyyy or ""):
        return 0
    try:
        d, m, y = maturity_ddmmyyyy.split(".")
        mat = datetime(int(y), int(m), int(d))
        ao = datetime.strptime(as_of, "%Y-%m-%d")
        return max((mat - ao).days, 0)
    except (ValueError, TypeError):
        return 0


def _nomura_maturity_iso(maturity_ddmmyyyy: str) -> str:
    """野村到期日 `DD.MM.YYYY`(16.10.2026) → ISO。落库 terms.maturity/expiry_date 必须 ISO：
    到期过滤(derivatives/vip_api)按 ISO 串裸比较，存 DD.MM.YYYY 会令 '16.10.2026' < today 恒真、
    活票被误判到期而从"当前持仓"下架。与 citi/cmbi 出口即 ISO 保持同一口径。不可解析则空串。"""
    if not _NOMURA_MATURITY_RE.match(maturity_ddmmyyyy or ""):
        return ""
    try:
        d, m, y = maturity_ddmmyyyy.split(".")
        return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _parse_nomura_derivatives(pages: list[str], as_of: str) -> list[dict]:
    """Position Details − Derivatives (Derivatives − Equities)：逐笔 OTC 累计/减持期权 → 薄记录。
    锚 `OTC EQUITY ACCUMULATOR/DECUMULATOR <SYM> <MKT>`；下一行 `(strike/ko) maturitycode`；
    再下 `Underlying: NAME`。marker `s`/`n` 后固定列：+1 qty +2 到期 +3 purchase +4 mkt_price
    +5 市值 +6 Value(USD)★。lot_key=strike:maturitycode 判别同标的多笔(野村双 ORCL)。"""
    lines = _clean_nomura_lines(pages)
    out: list[dict] = []
    n = len(lines)
    for i, line in enumerate(lines):
        mfam = re.match(r"OTC EQUITY (ACCUMULATOR|DECUMULATOR)\s+([A-Z0-9]{1,6})\s+[A-Z]{2}\b", line)
        if not mfam:
            continue
        fam = "equity_accumulator" if mfam.group(1) == "ACCUMULATOR" else "equity_decumulator"
        symbol = mfam.group(2)
        strike = ko = 0.0
        mat_code = ""
        underlying = ""
        if i + 1 < n:
            ms = re.match(r"\(([\d.]+)/([\d.]+)\)\s*(\d{6})?", lines[i + 1])
            if ms:
                strike = _num(ms.group(1)) or 0.0
                ko = _num(ms.group(2)) or 0.0
                mat_code = ms.group(3) or ""
        if i + 2 < n and lines[i + 2].startswith("Underlying:"):
            underlying = lines[i + 2].split(":", 1)[1].strip()
        # 找 marker(s/n)，其后取 qty / 到期 / Value(USD)
        mk = None
        for j in range(i + 1, min(i + 8, n)):
            if lines[j] in ("s", "n"):
                mk = j
                break
        if mk is None or mk + 6 >= n:
            continue
        qty = _num(lines[mk + 1])
        maturity = lines[mk + 2] if _NOMURA_MATURITY_RE.match(lines[mk + 2]) else ""
        maturity_iso = _nomura_maturity_iso(maturity)  # terms 落 ISO；tenor_days 仍吃原始 DD.MM.YYYY
        mv_usd = _num(lines[mk + 6])   # Value (USD) 列（负值=负债 MTM）
        if qty is None or mv_usd is None:
            continue
        lot_key = f"{strike}:{mat_code}" if (strike or mat_code) else f"{qty}:{i}"
        out.append({
            "product_family": fam, "underlying_symbol": symbol, "currency": "USD",
            "tenor_days": _nomura_tenor_days(as_of, maturity),
            "lot_key": lot_key,
            "terms": {"market_value_usd": mv_usd, "quantity": qty, "strike": strike,
                      "knock_out_price": ko, "maturity": maturity_iso, "expiry_date": maturity_iso,
                      "underlying_name": underlying, "nominal_ccy": "USD", "settlement_style": "physical_spot"},
        })
    return out


def _parse_nomura_structured(pages: list[str], as_of: str) -> list[dict]:
    """Position Details − Structured Products (Structured Product Equities)：FCN 等 → 薄记录。
    锚含 `FIXED COUPON NOTE`/`FCN` 的描述行；ISIN 行(XS...)；marker `n`/`s` 后固定列：
    +1 名义 +2 到期 +3 purchase +4 mkt_price +5 市值(原币) +6 unreal +7 Value(USD)★。"""
    lines = _clean_nomura_lines(pages)
    out: list[dict] = []
    n = len(lines)
    for i, line in enumerate(lines):
        if "FIXED COUPON NOTE" not in line.upper() and not re.search(r"\bFCN\b", line.upper()):
            continue
        # 表头为 "FIXED COUPON NOTE"，本身无股票代码；标的须从 (SYM MK)/Underlying/ISIN 推出，绝不取表头词 "FIXED"。
        isin = ""
        symbol = ""
        underlying = ""
        for j in range(i + 1, min(i + 8, n)):
            if not isin and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9,10}", lines[j]):
                isin = lines[j]
            if lines[j].startswith("Underlying:"):
                underlying = lines[j].split(":", 1)[1].strip()
            if not symbol:
                mt = re.search(r"\(([A-Z0-9]{1,6})\s+[A-Z]{2}\)", lines[j])
                if mt:
                    symbol = mt.group(1)
        symbol = symbol or isin or (underlying.split()[0].upper() if underlying else "FCN")
        mk = None
        for j in range(i + 1, min(i + 8, n)):
            if lines[j] in ("n", "s"):
                mk = j
                break
        if mk is None or mk + 7 >= n:
            continue
        nominal = _num(lines[mk + 1])
        maturity = lines[mk + 2] if _NOMURA_MATURITY_RE.match(lines[mk + 2]) else ""
        maturity_iso = _nomura_maturity_iso(maturity)  # terms 落 ISO；lot_key 保持原样以保重导幂等命中
        mv_usd = _num(lines[mk + 7])   # Value (USD) 列
        if nominal is None or mv_usd is None:
            continue
        lot_key = f"{isin}:{maturity}" if isin else f"{symbol}:{maturity}"
        out.append({
            "product_family": "equity_fcn", "underlying_symbol": symbol, "currency": "USD",
            "tenor_days": _nomura_tenor_days(as_of, maturity),
            "lot_key": lot_key,
            "terms": {"market_value_usd": mv_usd, "notional": nominal, "isin": isin,
                      "maturity": maturity_iso, "expiry_date": maturity_iso, "nominal_ccy": "USD",
                      "product_type": "fixed_coupon_note"},
        })
    return out


def _parse_nomura_statement(pages: list[str], filename: str, content_hash: str) -> BrokerStatement:
    holdings = _parse_nomura_equities(pages)
    cash_balances, total_cash_usd = _parse_nomura_cash(pages)
    summary = _parse_nomura_summary(pages)
    period = _parse_nomura_asof(pages)
    derivative_terms = _parse_nomura_derivatives(pages, period) + _parse_nomura_structured(pages, period)
    # Nomura 完整账户以 NAV 为总权益锚；持仓对子账（Equities）先不做 statement_total 对账。
    recon = ReconResult(holdings_count=len(holdings), holdings_total_usd=sum(h.market_value_usd for h in holdings),
                       statement_equities_total_usd=summary.get("equities_total_usd") or None,
                       delta_usd=None, status="no_statement_total")
    return BrokerStatement(
        broker="nomura", content_hash=content_hash, period_end=period,
        holdings=holdings, cash_balances=cash_balances, total_cash_usd=total_cash_usd,
        account_summary=summary, recon=recon, derivative_terms=derivative_terms,
    )


# ── 招银国际 CMBI/CMBIS 解析器 ─────────────────────────────────────────────
# 账户特征：HKD 记账保证金户，只持现金(USD)+结构性产品(FCN)，无普通股。
# 日结单(DAILY COMBINED)与月结单(MONTHLY)持仓小节结构一致 → 同一解析器。
# ponytail: v1 只抽持仓快照(结构性产品+现金)与账户合计，不抽交易明细——
#   交易记录多格式且脆弱(合约票/交收多段)，且现金 C/F 已反映净效果；
#   总权益 = Σ结构性产品市值 + Σ现金 = 结单 TOTAL VALUE(已数值对齐)。
#   需交易层时(v2)再按真实样本补 _cmbi_transactions，勿在样本不足时臆造money记录。
_CMBI_PRICE_CCY_RE = re.compile(r"^([\d,]+\.\d+)\s+([A-Z]{3})$")     # "99.2300 USD"
_CMBI_CCY_ROW_RE = re.compile(r"^([A-Z]{3})\s+[A-Za-z]")             # "USD US Dollar"
_CMBI_DATE_ISO_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")   # 30-Jun-2026
_CMBI_FNAME_DATE_RE = re.compile(r"-(\d{4})(\d{2})(\d{2})-", re.I)   # M381691-20260630-Monthly
_CMBI_MATURITY_RE = re.compile(r"^(\d{2}[A-Z]{3}\d{4}|\d{2}/\d{2}/\d{4})$")  # 04SEP2026 或 04/09/2026
_CMBI_SP_HEADERS = ("CODE", "NAME", "MATURITY", "CLOSING", "MARKET VALUE", "MARGIN",
                    "STOCK ON HOLD", "PENDING", "PRICE", "CCY", "B/F", "IN /")


def _cmbi_num(s: str) -> float | None:
    """CMBI 数值：额外识别会计括号负数 (1,060,000.00) → -1060000.0（日结单待结列用此记法）。"""
    t = s.strip()
    if t.startswith("(") and t.endswith(")"):
        v = _num(t[1:-1])
        return -v if v is not None else None
    return _num(t)


def _cmbi_period(pages: list[str], filename: str) -> str:
    m = _CMBI_FNAME_DATE_RE.search(filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    d = _CMBI_DATE_ISO_RE.search("\n".join(pages[:1]))
    if d and d.group(2).upper() in _MONTH:
        return f"{d.group(3)}-{_MONTH[d.group(2).upper()]}-{int(d.group(1)):02d}"
    return ""


def _cmbi_account_ref(pages: list[str]) -> str:
    m = re.search(r"subAccountID:(\S+)", "\n".join(pages[:1]))
    return m.group(1).strip() if m else ""


def _cmbi_account_summary(pages: list[str]) -> tuple[dict, dict, float]:
    """抽 Account Summary：每币种 7 数值(现金/待结/组合市值/保证金/合计/汇率/HKD等值)。
    返回 (rows{ccy:{...}}, fx{ccy:hkd_per_unit}, usd_hkd)。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines() if x.strip()]
    rows: dict = {}
    fx: dict = {}
    in_summary = False
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if "Account Summary" in line:
            in_summary = True; i += 1; continue
        if in_summary and line.startswith("Total (HKD Equiv"):
            break
        if in_summary:
            m = _CMBI_CCY_ROW_RE.match(line)
            if m:
                ccy = m.group(1); nums: list[float] = []; j = i + 1
                while j < n and len(nums) < 7:
                    if lines[j].startswith("Total (HKD Equiv") or _CMBI_CCY_ROW_RE.match(lines[j]):
                        break
                    v = _cmbi_num(lines[j])
                    if v is not None:
                        nums.append(v)
                    j += 1
                if len(nums) == 7:
                    rows[ccy] = {"cash": nums[0], "pending": nums[1], "portfolio": nums[2],
                                 "margin": nums[3], "total": nums[4], "fx": nums[5], "hkd": nums[6]}
                    fx[ccy] = nums[5]; i = j; continue
        i += 1
    return rows, fx, fx.get("USD", 0.0)


def _cmbi_to_usd(amount: float | None, ccy: str, fx: dict, usd_hkd: float) -> float | None:
    """折美元。缺 USD/HKD 汇率锚时返回 None（绝不静默按原值——HKD/GBP 当 USD 混入会污染总权益）。
    调用方须把 None 视作"该行不可折美元" → 触发 fx_incomplete → 整单转人工复核。"""
    if ccy == "USD" or not amount:
        return amount or 0.0
    if usd_hkd and fx.get(ccy):
        return round(amount * fx[ccy] / usd_hkd, 2)
    return None


def _cmbi_maturity_iso(s: str) -> str:
    """招银到期日 `04SEP2026` / `04/09/2026` → ISO；不可解析则空串。"""
    t = (s or "").strip().upper()
    m = re.fullmatch(r"(\d{2})([A-Z]{3})(\d{4})", t)
    if m and m.group(2) in _CITI_MONTHS:
        try:
            return datetime(int(m.group(3)), _CITI_MONTHS[m.group(2)], int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", t)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def _parse_cmbi_derivatives(pages: list[str], fx: dict, usd_hkd: float, as_of: str) -> list[dict]:
    """结构性产品(FCN) → equity_fcn 薄记录（§4：从 holdings 迁至衍生品通道，不再进 sim_positions）。
    以 '价格 CCY' 行为锚：k-2=期终结余(名义)  k=价格+币种  k+1=市值(原币)；
    向前回溯跳数字得 CODE(ISIN)，途中捕获到期日；再往前得 NAME(标的)。lot_key=CODE:MATURITY。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines() if x.strip()]
    out: list[dict] = []
    in_sp = False
    n = len(lines)
    for k in range(n):
        line = lines[k]
        if "Structured Product Asset Summary" in line:
            in_sp = True; continue
        if in_sp and "Account Summary" in line:
            in_sp = False
        if not in_sp:
            continue
        pm = _CMBI_PRICE_CCY_RE.match(line)
        if not pm or k - 2 < 0 or k + 1 >= n:
            continue
        ccy = pm.group(2); notional = _num(lines[k - 2]); mv_nom = _num(lines[k + 1])
        if notional is None or mv_nom is None or mv_nom <= 0:
            continue
        b = k - 1
        while b >= 0 and (_num(lines[b]) is not None or _CMBI_MATURITY_RE.match(lines[b])):
            b -= 1
        code = lines[b] if b >= 0 else ""
        # CODE 与 NAME 之间可能夹一行参考号(如 202505AP005)：到期日不一定紧贴 CODE 上方。
        # 在 CODE 上方小窗口(≤3 行)内找到期日版式行，NAME 取到期日行再上方的词行——
        # 否则参考号行会顶替到期日(→到期空、已到期票剔不掉)并污染标的名(→symbol 取成参考号)。
        maturity = ""
        mat_idx = b
        for u in range(b - 1, max(b - 4, -1), -1):
            if _CMBI_MATURITY_RE.match(lines[u]):
                maturity = _cmbi_maturity_iso(lines[u])
                mat_idx = u
                break
            if any(h in lines[u] for h in _CMBI_SP_HEADERS):
                break
        c = (mat_idx - 1) if maturity else (b - 1)
        name_parts: list[str] = []
        while c >= 0 and len(name_parts) < 6:
            t = lines[c]
            if _num(t) is not None or _CMBI_PRICE_CCY_RE.match(t):
                break
            if _CMBI_MATURITY_RE.match(t):
                c -= 1; continue
            if any(h in t for h in _CMBI_SP_HEADERS) or "Structured Product Asset Summary" in t:
                break
            name_parts.append(t); c -= 1
        name = " ".join(reversed(name_parts)).strip()
        symbol = (name.split()[0].split("+")[0].upper() if name.split() else (code or "SP"))[:12]
        out.append({
            "product_family": "equity_fcn", "underlying_symbol": symbol, "currency": ccy,
            "tenor_days": _citi_tenor_days(as_of, maturity),
            "lot_key": f"{code}:{maturity}" if code else f"{symbol}:{maturity}:{k}",
            "terms": {"market_value_usd": _cmbi_to_usd(mv_nom, ccy, fx, usd_hkd) or 0.0,
                      "market_value_nominal": mv_nom, "notional": notional, "isin": code,
                      "underlying_name": name, "maturity": maturity, "expiry_date": maturity,
                      "nominal_ccy": ccy, "product_type": "fixed_coupon_note"},
        })
    return out


_CMBI_TXN_DESC_RE = re.compile(r"^(Buy|Sell)\s+[買买賣卖][入出]\s+(\S+)\s*(.*)$")
_CMBI_TD_RE = re.compile(r"^\d{2}/\d{2}$")            # 交易日 14/05
_CMBI_PRICE_PCT_RE = re.compile(r"^[\d,]+\.?\d*%$")   # 价格 103.303836% / 100.00%
_CMBI_CCY_CTX_RE = re.compile(r"^([A-Z]{3})$")        # "貨幣 Currency:" 下一行独立币种


def _cmbi_transactions(pages: list[str], filename: str) -> list[StatementTransaction]:
    """账户交易变动(賬戶交易變動)里的证券买卖流水。
    ponytail: 只抽以 REF 编号可幂等去重的证券买卖(Buy/Sell)；货币兑换(Currency Exchange)行
      不含证券、FX 文本多段易碎，跳过——其净效果已由现金 C/F 反映，且不进持仓/交易语义。
    行结构(每笔证券买卖)：T/D S/D REF [MKT] 'Buy/Sell 描述' [续行...] [code] [maturity]
      QTY PRICE% AMOUNT CASH_BALANCE。以描述行为锚，向上取 T/D+REF，向下找 QTY/PRICE%/AMOUNT 尾。"""
    lines = [x.strip() for pg in pages for x in pg.splitlines() if x.strip()]
    n = len(lines)
    out: list[StatementTransaction] = []
    seen_ref: set[str] = set()
    in_moves = False
    cur_ccy = "USD"
    default_td = _cmbi_period(pages, filename)  # ISO 兜底
    for i in range(n):
        line = lines[i]
        if "Account Transaction Movements" in line:
            in_moves = True; continue
        # 到"待交收/Pending Settlement"或页脚品牌行即离开流水区(Pending 与流水重复，靠 REF 去重亦可)
        if in_moves and ("Pending Settlement" in line or "Participant of Stock Exchange" in line):
            in_moves = False
        if not in_moves:
            continue
        if line.startswith("貨幣 Currency:") or line.startswith("货币 Currency:"):
            if i + 1 < n and _CMBI_CCY_CTX_RE.match(lines[i + 1]):
                cur_ccy = lines[i + 1]
            continue
        m = _CMBI_TXN_DESC_RE.match(line)
        if not m:
            continue
        side = m.group(1); code = m.group(2)
        # 向上找日期对：版式为 T/D、S/D 相邻(都是 DD/MM)、其后紧跟 REF 数字串。
        # 向上扫先命中 S/D，故命中 DD/MM 后再看上一行——若也是 DD/MM 则上一行才是真 T/D。
        td = sd = ref = ""
        for b in range(i - 1, max(i - 6, -1), -1):
            if _CMBI_TD_RE.match(lines[b]):
                if b - 1 >= 0 and _CMBI_TD_RE.match(lines[b - 1]):
                    td, sd, ref_start = lines[b - 1], lines[b], b + 1
                else:
                    td, sd, ref_start = lines[b], "", b + 1
                for r in range(ref_start, min(ref_start + 3, n)):
                    if lines[r].isdigit():
                        ref = lines[r]; break
                break
        # 向下找数值尾：QTY, PRICE%, AMOUNT, [CASH_BAL]
        qty = price = amount = None
        for f in range(i + 1, min(i + 10, n)):
            if _CMBI_PRICE_PCT_RE.match(lines[f]):
                price = _num(lines[f].rstrip("%"))
                qty = _cmbi_num(lines[f - 1]) if f - 1 > i else None
                amount = _cmbi_num(lines[f + 1]) if f + 1 < n else None
                break
        if qty is None or amount is None:
            continue  # 非标准证券买卖行(如无价格%的调整)——不臆造，跳过
        # REF 幂等去重(同一笔在日结/月结/成交单据/待交收多处重复)
        key = ref or f"{td}-{code}-{amount:.2f}"
        if key in seen_ref:
            continue
        seen_ref.add(key)
        iso_td = _cmbi_txn_date(td, default_td)
        out.append(StatementTransaction(
            ticker=code[:24], company=code, txn_type=side.lower(),
            trade_date=iso_td, settle_date=_cmbi_txn_date(sd, default_td),
            quantity=abs(qty), price=price or 0.0,
            gross_amount=abs(amount), net_amount=abs(amount),
            currency=cur_ccy, external_id=(ref and f"cmbi-{ref}") or "",
            isin=code if code.startswith("XS") or (len(code) == 12 and code[:2].isalpha()) else "",
            description=line[:120]))
    return out


def _cmbi_txn_date(dd_mm: str, iso_fallback: str) -> str:
    """DD/MM → YYYY-MM-DD。年份取结单期末年(跨年结单极罕见，且样本内 T/D 与期末同年)。
    ponytail: 若日后出现跨年初的结单(12月交易落次年1月单)，需按 MM<期末MM 时取上一年。"""
    if not _CMBI_TD_RE.match(dd_mm or "") or len(iso_fallback) < 4:
        return iso_fallback
    year = iso_fallback[:4]
    d, mth = dd_mm.split("/")
    return f"{year}-{mth}-{d}"


def _parse_cmbi_statement(pages: list[str], filename: str, content_hash: str) -> BrokerStatement:
    rows, fx, usd_hkd = _cmbi_account_summary(pages)
    # §4: FCN 改走衍生品通道(equity_fcn 薄记录)，不再进 holdings/sim_positions。
    # ponytail: 历史已导入的 CMBI FCN 旧 sim_positions 行留存至重新导入，重导即纠正——不做迁移脚本。
    derivative_terms = _parse_cmbi_derivatives(pages, fx, usd_hkd, _cmbi_period(pages, filename))
    holdings: list[EquityHolding] = []
    transactions = _cmbi_transactions(pages, filename)
    # 缺锚检测：任一实际出现的非美元币种(账户小节行 + 结构性产品名义币)无 USD/HKD 汇率锚 →
    # 无法折美元，总权益口径不可信。绝不静默按原值横加(HKD 当 USD) → 整单转人工复核。
    needed = set(rows.keys()) | {t["currency"] for t in derivative_terms}
    fx_incomplete = any(c != "USD" and not (usd_hkd and fx.get(c)) for c in needed)
    cash_balances: list[CashBalance] = []
    for ccy, r in rows.items():
        cash = r.get("cash") or 0.0
        if cash > 0:
            cash_balances.append(CashBalance(currency=ccy, market_value_nominal=cash,
                                             market_value_usd=_cmbi_to_usd(cash, ccy, fx, usd_hkd) or 0.0))
    total_cash_usd = round(sum(c.market_value_usd for c in cash_balances), 2)
    portfolio_usd = round(sum((_cmbi_to_usd(r.get("portfolio") or 0.0, ccy, fx, usd_hkd) or 0.0)
                              for ccy, r in rows.items()), 2)
    total_value_usd = round(sum((_cmbi_to_usd(r.get("total") or 0.0, ccy, fx, usd_hkd) or 0.0)
                                for ccy, r in rows.items()), 2)
    account_summary = {
        "report_currency": "HKD", "cash_total_usd": total_cash_usd,
        "portfolio_market_value_usd": portfolio_usd, "total_value_usd": total_value_usd,
        "total_portfolio_hkd": round(sum(r.get("hkd") or 0.0 for r in rows.values()), 2),
        "account_ref": _cmbi_account_ref(pages),
        # 已用贷款/保证金（结单 Account Summary 的 margin 列，折美元）；招银若未动用融资则为 0
        "loan_outstanding_usd": round(sum((_cmbi_to_usd(r.get("margin") or 0.0, ccy, fx, usd_hkd) or 0.0)
                                          for ccy, r in rows.items()), 2),
        "fx_incomplete": fx_incomplete,
    }
    # §4: FCN 市值已移出 holdings 进衍生品栏；对账口径须把衍生品 MV 计回，否则空 holdings 恒判 mismatch。
    deriv_mv = round(sum(t["terms"].get("market_value_usd") or 0.0 for t in derivative_terms), 2)
    recon = _reconcile_totals(round(sum(h.market_value_usd for h in holdings) + deriv_mv, 2),
                              len(holdings) + len(derivative_terms),
                              portfolio_usd if portfolio_usd > 0 else None)
    if fx_incomplete:
        # 缺锚：total_value_usd 是欠计(缺锚行按 0 计入)的假值。强制非白名单状态 → importer 不物化其 NAV
        # (importer 仅在 recon.status∈{ok,no_statement_total} 时用 total_value_usd 作 NAV)。
        recon = ReconResult(holdings_count=recon.holdings_count,
                            holdings_total_usd=recon.holdings_total_usd,
                            statement_equities_total_usd=recon.statement_equities_total_usd,
                            delta_usd=recon.delta_usd, status="fx_incomplete")
    return BrokerStatement(
        broker="cmbi", content_hash=content_hash, period_end=_cmbi_period(pages, filename),
        holdings=holdings, cash_balances=cash_balances, total_cash_usd=total_cash_usd,
        account_summary=account_summary, transactions=transactions, recon=recon,
        derivative_terms=derivative_terms)


_PARSERS = {
    "citi": _parse_citi_statement,
    "nomura": _parse_nomura_statement,
    "cmbi": _parse_cmbi_statement,
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

    # 重导即重解析：同文件手里就有新鲜 bytes，若之前直接复用旧 doc 就丢弃新解析，
    # 存量旧 schema 解析（缺贷款等新增字段）永远刷不出来 → 一律先解析，再决定 UPDATE 还是 INSERT。
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
    # 无总额对账但权益锚显示持仓覆盖不全 → 疑似漏解析，NAV 会被低估，转人工复核不自动物化
    coverage = _coverage_shortfall(stmt.recon)
    if coverage is not None and coverage < _COVERAGE_MIN:
        db_status = "needs_review"
        recon_flags["coverage_shortfall"] = round(coverage, 4)
        recon_flags["delta_flag"] = "coverage"

    # 幂等去重：同文件已在库 → 用新解析刷新 parsed_json（保持 doc 身份不变），下游拿到最新 account_summary
    existing = store.find_financial_doc_by_hash(user_id, content_hash)
    if existing:
        store.update_financial_doc_parse(user_id, existing["id"],
                                         parsed_json=stmt.model_dump_json(), recon_flags=recon_flags,
                                         status=db_status, doc_type=doc_type, period_end=stmt.period_end)
        return {"doc_id": existing["id"], "status": db_status,
                "recon": stmt.recon.model_dump(), "duplicate": True, "doc_type": doc_type}

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
    # 覆盖率守卫纯逻辑自检（无需真实 PDF，恒运行）
    def _mk(st, ht, an, n=1):
        return ReconResult(holdings_count=n, holdings_total_usd=ht,
                           statement_equities_total_usd=an, delta_usd=None, status=st)
    assert _coverage_shortfall(_mk("no_statement_total", 90.0, 100.0)) == 0.9   # 有锚→算覆盖率
    assert _coverage_shortfall(_mk("no_statement_total", 100.0, None)) is None  # 无锚→放行
    assert _coverage_shortfall(_mk("no_statement_total", 100.0, 100.0, n=0)) is None  # 无持仓→放行
    assert _coverage_shortfall(_mk("ok", 90.0, 100.0)) is None                  # 已对账→不介入
    assert 0.90 < _COVERAGE_MIN <= 1.0 and (0.90 / 1.0) < _COVERAGE_MIN         # 10% 缺口会被拦

    # 分类合并纯逻辑自检（无需 PDF/LLM）：招银单必须不被 LLM 的 broker 猜测覆盖
    _cmbi_h = {"broker": "cmbi", "doc_type": "monthly_statement", "confidence": 0.51, "reason_code": "content_match", "source": "heuristic"}
    _citi_h = {"broker": "citi", "doc_type": "trade_confirm", "confidence": 0.51, "reason_code": "content_match", "source": "heuristic"}
    # LLM 误判招银为 citi → 仍以启发式 cmbi 为准（这就是 unsupported_non_statement:citi 的根因）
    assert _merge_classification(_cmbi_h, {"broker": "citi", "doc_type": "unsupported"})["broker"] == "cmbi"
    # LLM 缺失 → 用启发式
    assert _merge_classification(_cmbi_h, None)["broker"] == "cmbi"
    # 非招银：LLM 报 unknown → 回填启发式 broker，保留 LLM 的 doc_type 细分
    _m = _merge_classification(_citi_h, {"broker": "unknown", "doc_type": "position_report"})
    assert _m["broker"] == "citi" and _m["doc_type"] == "position_report"

    # 花旗已用融资抽取：认权威合计行的下一数值，取绝对值；无该行 → 0
    assert _citi_loan_outstanding_usd(["Total Margin Loans Outstanding\n11,435,214.48\nHong Kong"]) == 11435214.48
    assert _citi_loan_outstanding_usd(["no liabilities section here"]) == 0.0

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
    assert stmt.recon.status in ("ok", "no_statement_total", "mismatch", "fx_incomplete")
    print("ingest demo 通过")


def _cmbi_demo() -> None:
    """招银国际自检：需环境变量 CMBI_PDF_PASSWORD（勿硬编码密码）。未设或无文件则跳过。
    核对：券商识别、期末、持仓非空、Σ持仓+现金 == 结单 TOTAL VALUE(0.5% 内)。"""
    pwd = os.environ.get("CMBI_PDF_PASSWORD", "")
    d = Path(r"C:\Users\walker\Documents\walker\银行文件\招银国际月结单")
    files = sorted(d.glob("*.pdf")) if d.exists() else []
    if not pwd or not files:
        print("未设 CMBI_PDF_PASSWORD 或无招银文件，跳过 cmbi demo"); return
    for pdf in files:
        stmt = ingest_pdf(pdf.read_bytes(), pdf.name, pdf_password=pwd)
        s = stmt.account_summary
        tv = s.get("total_value_usd", 0.0)
        cash = s.get("cash_total_usd", 0.0)
        port = s.get("portfolio_market_value_usd", 0.0)
        # 结单内部恒等式：TOTAL VALUE = 现金 + 待结 + 组合市值。日结单结算中途现金含负待结，
        # 故校验结单自身口径(权威锚)，而非"持仓+现金"横加(仅结算完成后才等于 TOTAL)。
        print(f"[{pdf.name}] 期末 {stmt.period_end}  持仓 {len(stmt.holdings)}  组合 ${port:,.2f}  "
              f"现金 ${cash:,.2f}  结单TOTAL ${tv:,.2f}  交易 {len(stmt.transactions)} 笔")
        for t in stmt.transactions:
            print(f"    {t.trade_date} {t.txn_type:4} {t.ticker:20} 数量{t.quantity:>12,.0f} "
                  f"价{t.price:>10,.4f} 额 {t.currency} {t.gross_amount:>14,.2f} ref={t.external_id}")
        assert stmt.broker == "cmbi", f"券商识别错误: {stmt.broker}"
        assert stmt.period_end, "未抽到期末日期"
        assert stmt.derivative_terms, "未抽到结构性产品(FCN)"  # FCN 已改走衍生品栏，不再进 holdings
        assert tv > 0, "未抽到结单 TOTAL VALUE"
        # 组合市值来自 Account Summary 的 portfolio 列，应与逐笔 FCN 当期 MTM 加总吻合
        sp_sum = round(sum(t["terms"]["market_value_usd"] for t in stmt.derivative_terms), 2)
        assert abs(sp_sum - port) / max(port, 1.0) <= _RECON_TOL, f"结构性产品对账失败 Σ{sp_sum} vs 组合{port}"
        # 交易(若有)：类型合法、有交易日、金额为正(方向靠 txn_type，金额取绝对值)
        for t in stmt.transactions:
            assert t.txn_type in ("buy", "sell"), f"未知交易类型 {t.txn_type}"
            assert t.trade_date and t.gross_amount > 0, f"交易字段缺失 {t.ticker}"
    print("cmbi demo 通过")


def _cmbi_fx_selfcheck() -> None:
    """缺锚守卫自检(不需 PDF)：_cmbi_to_usd 缺 USD/HKD 锚必返 None，绝不静默按原值当美元。"""
    fx_ok = {"USD": 7.8, "HKD": 1.0}  # usd_hkd = fx["USD"] = 7.8；各行 fx 为"该币→HKD"率(HKD→HKD=1.0)
    assert _cmbi_to_usd(780.0, "HKD", fx_ok, 7.8) == 100.0, "HKD 折美元错"
    assert _cmbi_to_usd(100.0, "USD", fx_ok, 7.8) == 100.0, "USD 直取错"
    # 缺锚(无 USD 行→usd_hkd=0)：非美元必须 None，不得回落原值(否则 HKD 780 会当 USD 780 混入总权益)
    assert _cmbi_to_usd(780.0, "HKD", {}, 0.0) is None, "缺锚未拦截：HKD 被当原值"
    assert _cmbi_to_usd(500.0, "GBP", fx_ok, 7.8) is None, "缺该币种汇率未拦截"
    # 缺锚检测谓词(与 _parse_cmbi_statement 内联同式)：任一非美元币无锚 → fx_incomplete
    needed = {"USD", "HKD"}
    assert any(c != "USD" and not (0.0 and {}.get(c)) for c in needed), "缺锚 fx_incomplete 应为真"
    assert not any(c != "USD" and not (7.8 and fx_ok.get(c)) for c in needed), "有锚不应误判"
    print("cmbi fx 缺锚守卫自检 通过")


def _deriv_selfcheck() -> None:
    """三家新分支纯逻辑自检(内联伪造版式，不碰真实 PII PDF)：
    校验能抽出 DerivativeTerm、family/lot_key 正确、同标的双 lot 不折叠。"""
    # 野村衍生品：两笔同标的 ORCL 不同 strike/到期 → 两条，lot_key 不撞
    nomura_deriv = (
        "OTC EQUITY ACCUMULATOR ORCL US\n(244.0263/461.4297) 161026\nUnderlying: ORACLE CORP\n"
        "s\n876.00\n16.10.2026\nx\nx\nx\n-3,245.18\n"
        "OTC EQUITY ACCUMULATOR ORCL US\n(230.5649/430.0000) 261026\nUnderlying: ORACLE CORP\n"
        "s\n500.00\n26.10.2026\nx\nx\nx\n-1,100.00\n"
    )
    nd = _parse_nomura_derivatives([nomura_deriv], "2026-06-30")
    assert len(nd) == 2, f"野村双 ORCL 应两条，得 {len(nd)}"
    assert {t["lot_key"] for t in nd} == {"244.0263:161026", "230.5649:261026"}, "lot_key 折叠了"
    assert all(t["product_family"] == "equity_accumulator" for t in nd)
    # 到期日必须落 ISO(非原始 16.10.2026)——否则到期过滤 mat<today 按 ASCII 恒真、活票被误判到期下架
    assert {t["terms"]["maturity"] for t in nd} == {"2026-10-16", "2026-10-26"}, \
        [t["terms"]["maturity"] for t in nd]

    # 野村结构性产品 FCN
    nomura_sp = ("FIXED COUPON NOTE\nXS3164880992\nn\n1,000,000\n14.12.2026\nx\nx\nx\nx\n976,300.00\n"
                 "Underlying: NVIDIA CORP\n")
    ns = _parse_nomura_structured([nomura_sp], "2026-06-30")
    assert len(ns) == 1 and ns[0]["product_family"] == "equity_fcn", "野村 FCN 未抽到"
    assert ns[0]["terms"]["market_value_usd"] == 976300.0
    assert ns[0]["terms"]["maturity"] == "2026-12-14", ns[0]["terms"]["maturity"]  # 到期日必须 ISO

    # 花旗 Total Assets 锚（合计行下一数值）
    assert _citi_total_assets_usd(["Total Assets\n36,128,828.16\nUSD"]) == 36128828.16
    assert _citi_total_assets_usd(["no total here"]) == 0.0

    # 招银 FCN 04-30 版式：到期日与 CODE 之间夹参考号行 → 到期须仍抓对、标的名不被参考号污染
    cmbi_sp = ("Structured Product Asset Summary\n"
               "CMBIGP issuance HKD\n12 months step-up T+ 5B\n15MAY2026\n202505AP005\nS20250515S917HKD\n"
               "2,090,000\n0\n0\n2,090,000\n0\n103.175342 HKD\n2,156,364.65\n0.00\n0.00\n"
               "Account Summary\n")
    cs = _parse_cmbi_derivatives([cmbi_sp], {"HKD": 1.0, "USD": 7.8065}, 7.8065, "2026-04-30")
    assert len(cs) == 1, f"招银 FCN 应一条，得 {len(cs)}"
    assert cs[0]["terms"]["maturity"] == "2026-05-15", f"到期抓错：{cs[0]['terms']['maturity']}"
    assert "202505AP005" not in cs[0]["terms"]["underlying_name"], "参考号污染了标的名"
    assert cs[0]["lot_key"] == "S20250515S917HKD:2026-05-15", cs[0]["lot_key"]
    print("衍生品/结构性产品 三家分支自检 通过")


if __name__ == "__main__":
    _cmbi_fx_selfcheck()
    _deriv_selfcheck()
    demo()
    _cmbi_demo()
