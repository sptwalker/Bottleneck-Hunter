"""C2 衍生品/结构化产品建模（M3）：Accumulator/Decumulator 与 MLI Booster 两类。

范围（先做最有价值的两族样本）：
1) Equity Accumulator / Decumulator（每日累积/减持，带 Guarantee、KO、Step-up shares）
2) Equity Market Linked Instrument / Booster（参与率 + KI Put + capped upside）

本模块两层能力：
- 术语抽取：从 term sheet / final terms PDF 文本抽关键条款，产出规范 dict。
- 场景收益：给定终值（及/或是否触发 KI/KO）算到期收益/交割股数，供报告提示风险。

说明：Accumulator/Decumulator 是路径依赖产品，不用 BS；先做静态/准静态场景引擎。
MLI Booster 是结构化票据，可抽象为 capped participation + down-and-in put 的到期收益函数。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


# ── Black-Scholes 纯函数（D4 规格，先供 MLI / 后续标准期权）──────────────

def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_price(S, K, T, r, sigma, is_call, q=0.0):
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if sigma <= 0:
        fwd = S * math.exp(-q * T) - K * math.exp(-r * T)
        return max(fwd, 0.0) if is_call else max(-fwd, 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * math.exp(-q * T) * _cdf(d1) - K * math.exp(-r * T) * _cdf(d2)
    return K * math.exp(-r * T) * _cdf(-d2) - S * math.exp(-q * T) * _cdf(-d1)


def bs_greeks(S, K, T, r, sigma, is_call, q=0.0):
    price = bs_price(S, K, T, r, sigma, is_call, q)
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        intrinsic_delta = 1.0 if (is_call and S > K) else (-1.0 if (not is_call and S < K) else 0.0)
        return {"price": price, "delta": intrinsic_delta, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    nd1 = _pdf(d1)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    delta = disc_q * _cdf(d1) if is_call else disc_q * (_cdf(d1) - 1)
    gamma = disc_q * nd1 / (S * sigma * math.sqrt(T))
    vega = S * disc_q * nd1 * math.sqrt(T) / 100.0
    if is_call:
        theta = (-S * disc_q * nd1 * sigma / (2 * math.sqrt(T))
                 - r * K * disc_r * _cdf(d2)
                 + q * S * disc_q * _cdf(d1)) / 365.0
        rho = K * T * disc_r * _cdf(d2) / 100.0
    else:
        theta = (-S * disc_q * nd1 * sigma / (2 * math.sqrt(T))
                 + r * K * disc_r * _cdf(-d2)
                 - q * S * disc_q * _cdf(-d1)) / 365.0
        rho = -K * T * disc_r * _cdf(-d2) / 100.0
    return {"price": price, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta, "rho": rho}


def implied_vol(price, S, K, T, r, is_call, q=0.0):
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    # 越界：连高波动都定不到 → 无解
    if price < bs_price(S, K, T, r, lo, is_call, q) - 1e-9:
        return None
    if price > bs_price(S, K, T, r, hi, is_call, q) + 1e-9:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        pm = bs_price(S, K, T, r, mid, is_call, q)
        if abs(pm - price) < 1e-6:
            return mid
        if pm > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# ── 结构化产品规范模型 ─────────────────────────────────────────────────────

@dataclass
class DerivativeTerm:
    product_family: str        # equity_accumulator / equity_decumulator / equity_mli_booster
    underlying_symbol: str
    currency: str
    tenor_days: int
    terms: dict
    source_file: str = ""


# ── 文本抽取 helper ───────────────────────────────────────────────────────

def _read_pdf_text(pdf_source, pages: int = 6, pdf_password: str = "") -> str:
    """读 PDF 前 N 页文本。pdf_source 可为 path(str/Path) 或 bytes；加密 PDF 可传密码。"""
    import fitz
    if isinstance(pdf_source, (bytes, bytearray)):
        doc = fitz.open(stream=pdf_source, filetype="pdf")
    else:
        doc = fitz.open(str(pdf_source))
    if doc.needs_pass:
        if not pdf_password or not doc.authenticate(pdf_password):
            raise ValueError("pdf_password_required_or_invalid")
    return "\n".join(page.get_text() for page in doc[:pages])


def _f(pat: str, text: str, group=1, flags=re.I) -> Optional[str]:
    m = re.search(pat, text, flags)
    return m.group(group).strip() if m else None


def _ff(pat: str, text: str, group=1) -> Optional[float]:
    s = _f(pat, text, group)
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _days_between(a: str, b: str) -> int:
    """兼容 Citi/野村日期格式：Jul 22, 2026 / 7 July 2026 / July 7, 2026。"""
    fmts = ("%b %d, %Y", "%d %B %Y", "%B %d, %Y", "%d %b %Y")
    def parse(s: str):
        for f in fmts:
            try:
                return datetime.strptime(s.strip(), f)
            except ValueError:
                continue
        raise ValueError(f"unsupported date format: {s}")
    da = parse(a)
    db = parse(b)
    return (db - da).days


# ── 条款抽取：Accumulator/Decumulator ────────────────────────────────────

def extract_accumulator_terms(pdf_source, pdf_password: str = "") -> DerivativeTerm:
    text = _read_pdf_text(pdf_source, pages=8, pdf_password=pdf_password)
    # 识别 product family：正文常同时出现“Equity Accumulator / Equity Decumulator”说明语，
    # 故优先看产品标题里的 Daily ... Accumulator/Decumulator。
    fam = "equity_accumulator"
    if re.search(r"Daily(?: Securities)? Decumulator", text, re.I):
        fam = "equity_decumulator"
    elif re.search(r"Daily(?: Securities)? Accumulator", text, re.I):
        fam = "equity_accumulator"

    # Citi 样本：Bloomberg Ticker / AFP / KO / DS / St-DS / Max Nominal Shares
    if "Micron Technology Inc" in text or "Marvell Technology Inc" in text or "Alibaba Group Holding" in text:
        symbol = _f(r"Bloomberg Ticker\s*:?\s*([A-Z0-9]{1,6}\s+[A-Z]{2})", text) or ""
        symbol = symbol.split()[0]
        ccy = _f(r":\s*([A-Z]{3})\s+\d+(?:\.\d+)?\s*\(.*Initial Price\)", text) or "USD"
        trade_date = _f(r"Trade Date\s*:?\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text) or ""
        termination = _f(r"Termination Date\s*:?\s*The earlier of \(a\)\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text) or ""
        tenor = _days_between(trade_date, termination) if trade_date and termination else 365
        ds = _ff(r"Daily Number of Shares \(DS\)\s*:?\s*(\d+(?:\.\d+)?)", text) or 0.0
        stds = _ff(r"Step-up Daily Number of Shares \(St-DS\)\s*:?\s*(\d+(?:\.\d+)?)", text) or 0.0
        max_nom = _ff(r"Maximum Number of Nominal Shares\s*:?\s*([\d,]+(?:\.\d+)?)", text) or 0.0
        afp = _ff(r":\s*USD\s*([\d,]+\.\d+)\s*\(\s*70\.75% of Initial Price\s*\)", text) or _ff(r"AFP\)?\s*:?\s*USD\s*([\d,]+\.\d+)", text) or 0.0
        initial = _ff(r"Initial Price\s*:?\s*USD\s*([\d,]+\.\d+)", text) or 0.0
        ko = _ff(r"Knock-out Price \(KO\)\s*:?\s*USD\s*([\d,]+\.\d+)", text) or 0.0
        gp = _f(r"Guaranteed Period End Date\s*:?\s*([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text) or ""
        return DerivativeTerm(
            product_family=fam, underlying_symbol=symbol, currency=ccy, tenor_days=tenor, source_file=str(pdf_source),
            terms={"initial_price": initial, "afp": afp, "knock_out_price": ko, "knock_out_direction": "up_and_out",
                   "daily_shares": ds, "step_up_daily_shares": stds, "max_nominal_shares": max_nom,
                   "guaranteed_period_end": gp, "settlement_style": "physical_spot", "net_premium": 0.0},
        )

    # Nomura 样本：12 Month USD Daily Accumulator/Decumulator（BE.N / PLTR.OQ）
    symbol = _f(r"Underlying Share\s*([A-Za-z0-9 .&'/-]+)?\s*\(([A-Z0-9]{1,6})\s+[A-Z]{2}\s+Equity\)", text, group=2, flags=re.I | re.S) or ""
    if not symbol:
        symbol = (_f(r"^([A-Z0-9.]{2,10}),\s*\d+(?:\.\d+)?%\s*Strike Price", text, flags=re.I | re.M) or "").split('.')[0]
    ccy = _f(r"Settlement Currency\s*([A-Z]{3})", text) or _f(r"Underlying CCY\s*([A-Z]{3})", text) or "USD"
    trade_date = _f(r"Trade Date\s*([0-9]{1,2} [A-Za-z]+ 20\d{2})", text) or ""
    end_pat = r"Final Decumulation Date\s*([0-9]{1,2} [A-Za-z]+ 20\d{2})" if fam == "equity_decumulator" else r"Final Accumulation Date\s*([0-9]{1,2} [A-Za-z]+ 20\d{2})"
    final_date = _f(end_pat, text) or ""
    tenor = _days_between(trade_date, final_date) if trade_date and final_date else 365
    initial = _ff(r"Reference Spot Price \(USD\)\s*([\d,]+\.\d+)", text) or 0.0
    afp = _ff(r"Forward Price \(USD\)\s*([\d,]+\.\d+)", text) or 0.0
    ko = _ff(r"Knock(?:-Out)? (?:Price|Level) \(USD\)\s*([\d,]+\.\d+)", text) or 0.0
    max_nom = _ff(r"Maximum Total Shares\s*([\d,]+(?:\.\d+)?)", text) or 0.0
    ds = _ff(r"Shares per Day\s*([\d,]+(?:\.\d+)?)", text) or _ff(r"Shares per day\s*([\d,]+(?:\.\d+)?)", text) or 0.0
    gear = _ff(r"Gearing Ratio\s*([\d,]+(?:\.\d+)?)", text) or 1.0
    # Nomura 日累积/减持没有显式 Step-up shares，而是 LNBD * Gearing Ratio —— 折算为 step-up daily shares = DS*GearingRatio
    stds = ds * gear
    protected_end = _f(r"Protected\s+Period\s+End\s+Date.*?([0-9]{1,2} [A-Za-z]+ 20\d{2})", text) or ""
    return DerivativeTerm(
        product_family=fam, underlying_symbol=symbol, currency=ccy, tenor_days=tenor, source_file=str(pdf_source),
        terms={"initial_price": initial, "afp": afp, "knock_out_price": ko,
               "knock_out_direction": "down_and_out" if fam == "equity_decumulator" else "up_and_out",
               "daily_shares": ds, "step_up_daily_shares": stds, "gearing_ratio": gear,
               "max_nominal_shares": max_nom, "guaranteed_period_end": protected_end,
               "settlement_style": "physical_spot", "net_premium": 0.0},
    )


# ── 条款抽取：MLI Booster / Leverage Call Spread + KI Put ───────────────

def extract_mli_terms(pdf_source, pdf_password: str = "") -> DerivativeTerm:
    text = _read_pdf_text(pdf_source, pages=8, pdf_password=pdf_password)
    symbol = _f(r"Underlying Share.*?Bloomberg.*?:\s*([A-Z0-9]{1,6}\s+[A-Z]{2})", text, flags=re.I | re.S) or ""
    symbol = symbol.split()[0]
    ccy = _f(r"([A-Z]{3})-Denominated", text) or "USD"
    # 4-month / 12-month in title
    months = _ff(r"A\s+(\d+)-month", text) or 4.0
    tenor = int(months * 30)

    # 先抓表格块：Underlying Share / Initial / KI / Strike 四列下方连续三行 USD 数值（真实样本 132/133/134）
    initial = ki_price = strike_price = 0.0
    mtab = re.search(
        r"Underlying Share \(Bloomberg Ticker\).*?Initial Price.*?Knock-in Price.*?Strike Price,? K.*?"
        r"[A-Za-z .()]+\([A-Z0-9]{1,6}\s+[A-Z]{2}\).*?USD\s*([\d,]+\.\d+).*?USD\s*([\d,]+\.\d+).*?USD\s*([\d,]+\.\d+)",
        text, re.I | re.S)
    if mtab:
        initial = float(mtab.group(1).replace(",", ""))
        ki_price = float(mtab.group(2).replace(",", ""))
        strike_price = float(mtab.group(3).replace(",", ""))
    else:
        initial = _ff(r"Initial Price\s*:?\s*USD\s*([\d,]+\.\d+)", text) or 0.0
        ki_price = _ff(r"Knock-in Price\s*:?\s*USD\s*([\d,]+\.\d+)", text) or 0.0
        strike_price = _ff(r"Strike Price.*?USD\s*([\d,]+\.\d+)", text) or 0.0

    ki_pct = _ff(r"Knock-in Price\s*\((\d+(?:\.\d+)?)% of Initial Price\)", text) or (ki_price / initial if initial else 0.0)
    strike_pct = _ff(r"Strike Price,?\s*K\s*\((\d+(?:\.\d+)?)% of Initial Price\)", text) or ((strike_price / initial) * 100 if initial else 100.0)
    max_up = _ff(r"maximum return of\s*(\d+(?:\.\d+)?)%", text) or _ff(r"Maximum Appreciation.*?(\d+(?:\.\d+)?)%", text) or 50.0
    pf = _ff(r"Participation Factor \(PF\)\s*:?\s*(\d+(?:\.\d+)?)%", text) or 100.0
    return DerivativeTerm(
        product_family="equity_mli_booster",
        underlying_symbol=symbol,
        currency=ccy,
        tenor_days=tenor,
        source_file=str(pdf_source),
        terms={
            "initial_price": initial,
            "knock_in_price": ki_price,
            "strike_price": strike_price,
            "participation_factor": pf / 100.0,
            "max_upside_pct": max_up / 100.0,
            "strike_pct_initial": strike_pct / 100.0,
            "knock_in_pct_initial": ki_pct / 100.0,
            "knock_in_direction": "down_and_in",
            "settlement_style": "physical",
            "principal_protected_if_no_ki": True,
        },
    )


# ── 场景收益引擎 ─────────────────────────────────────────────────────────

def payoff_accumulator(term: DerivativeTerm, final_price: float, *,
                       knock_out_happened: bool = False, days_observed: int | None = None) -> dict:
    """简化场景引擎：按终值相对 AFP/KO 估算累计/减持股数与盈亏（静态近似，供报告风险提示）。

    - Accumulator：终值 < AFP → step-up 累积更多股（更危险）
    - Decumulator：终值 > AFP → step-up 减持更多股（上涨踏空风险）
    """
    t = term.terms
    ds = t.get("daily_shares", 0)
    stds = t.get("step_up_daily_shares", 0)
    afp = t.get("afp", 0.0)
    ko = t.get("knock_out_price", 0.0)
    days = int(days_observed or term.tenor_days)

    if term.product_family == "equity_decumulator":
        if knock_out_happened and final_price <= ko:
            shares = ds  # 最保守：按 1 天 DS 近似
        else:
            shares = days * (ds if final_price <= afp else stds)
        proceeds = shares * afp
        market_value = shares * final_price
        return {"shares_decumulated": shares, "proceeds": proceeds, "market_value": market_value,
                "pnl": proceeds - market_value}

    # accumulator
    if knock_out_happened and final_price >= ko:
        shares = ds  # 最保守：按 1 天 DS（路径依赖,这里不假装精确）
    else:
        shares = days * (ds if final_price >= afp else stds)
    cost = shares * afp
    mtm = shares * final_price
    return {"shares_acquired": shares, "cost": cost, "market_value": mtm, "pnl": mtm - cost}


def payoff_mli_booster(term: DerivativeTerm, final_price: float, *,
                       knock_in_happened: bool = False, investment_amount: float = 1.0) -> dict:
    """MLI Booster 到期收益：3 段式。返回到期兑付金额（以 investment_amount=1 归一化）与收益率。"""
    t = term.terms
    S0 = t.get("initial_price", 0.0)
    if S0 <= 0:
        return {"redemption": investment_amount, "return_pct": 0.0}
    strike = S0 * t.get("strike_pct_initial", 1.0)
    ki = S0 * t.get("knock_in_pct_initial", 0.0)
    pf = t.get("participation_factor", 1.0)
    cap = t.get("max_upside_pct", 0.5)
    upside = max(final_price / strike - 1.0, 0.0)
    upside = min(upside * pf, cap)
    if not knock_in_happened:
        redemption = investment_amount * (1.0 + upside)
    elif final_price >= strike:
        redemption = investment_amount * (1.0 + upside)
    else:
        # down-and-in put：跌破 strike 后按标的跌幅承损
        redemption = investment_amount * max(final_price / strike, 0.0)
    return {"redemption": redemption, "return_pct": (redemption / investment_amount - 1.0) * 100,
            "knock_in_price": ki, "strike_price": strike}


def classify_pdf(pdf_source, pdf_password: str = "") -> str:
    """日常文件快速分类：fund_report / accumulator / decumulator / mli / other。"""
    text = _read_pdf_text(pdf_source, pages=2, pdf_password=pdf_password)
    if "Master Fund Highlights" in text or "Financial Statements" in text:
        return "fund_report"
    if re.search(r"Daily(?: Securities)? Accumulator", text, re.I):
        return "accumulator"
    if re.search(r"Daily(?: Securities)? Decumulator", text, re.I):
        return "decumulator"
    if "Market Linked Instrument" in text or "Leverage Call Spread" in text or "Daily Callable Fixed Coupon" in text:
        return "mli"
    return "other"


def save_derivative_term(wl_store, term: DerivativeTerm, *, source_file_name: str, source_file_hash: str,
                         broker: str, rationale_ref: str = "") -> str:
    import json, uuid
    # 幂等：重复上传同一文件保留原 id/created_at（不做 OR REPLACE 重建）
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT id FROM vip_derivative_terms WHERE source_file_hash=? AND product_family=? AND underlying_symbol=?",
            (source_file_hash, term.product_family, term.underlying_symbol))
        row = conn.execute(q, p).fetchone()
        if row:
            return row["id"]
    finally:
        conn.close()
    did = uuid.uuid4().hex[:12]
    with wl_store._write_conn() as conn:
        conn.execute(
            f"""INSERT INTO vip_derivative_terms
               (id, source_file_name, source_file_hash, broker, product_family, underlying_symbol,
                currency, terms_json, rationale_ref, created_at{wl_store._user_insert_cols()}{wl_store._market_insert_cols()})
               VALUES (?,?,?,?,?,?,?,?,?,?{wl_store._user_insert_vals()}{wl_store._market_insert_vals()})""",
            (did, source_file_name, source_file_hash, broker, term.product_family, term.underlying_symbol,
             term.currency, json.dumps(term.terms, ensure_ascii=False), rationale_ref, datetime.now().isoformat())
            + wl_store._user_insert_params() + wl_store._market_insert_params(),
        )
    return did


def list_derivative_terms(wl_store, limit: int = 50) -> list[DerivativeTerm]:
    import json
    conn = wl_store._connect()
    try:
        q, p = wl_store._filtered(
            "SELECT * FROM vip_derivative_terms ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in conn.execute(q, p).fetchall()]
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append(DerivativeTerm(product_family=r["product_family"], underlying_symbol=r["underlying_symbol"],
                                  currency=r["currency"], tenor_days=0,
                                  terms=json.loads(r["terms_json"] or "{}"), source_file=r["source_file_name"]))
    return out


def demo() -> None:
    # 教科书基准
    p = bs_price(100, 100, 1, 0.05, 0.2, True)
    assert abs(p - 10.4506) < 1e-3, p
    # IV 往返
    iv = implied_vol(p, 100, 100, 1, 0.05, True)
    assert iv and abs(iv - 0.2) < 1e-3, iv
    # Accumulator 场景
    acc = DerivativeTerm("equity_accumulator", "MU", "USD", 365, {"afp": 100.0, "daily_shares": 3, "step_up_daily_shares": 6})
    r = payoff_accumulator(acc, 80.0, knock_out_happened=False, days_observed=10)
    assert r["shares_acquired"] == 60 and r["pnl"] < 0
    # Booster 场景
    mli = DerivativeTerm("equity_mli_booster", "MU", "USD", 120,
                         {"initial_price": 100.0, "participation_factor": 1.0,
                          "max_upside_pct": 0.5, "strike_pct_initial": 1.0, "knock_in_pct_initial": 0.5379})
    assert payoff_mli_booster(mli, 130.0, knock_in_happened=False)["return_pct"] > 0
    assert payoff_mli_booster(mli, 80.0, knock_in_happened=True)["return_pct"] < 0
    print("derivatives demo 通过")


if __name__ == "__main__":
    demo()
