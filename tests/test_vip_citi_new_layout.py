"""花旗「全部-仓盘」新版式(2026-08+)持仓 PDF 解析回归。

样本含金融 PII，不入库；靠环境变量 CITI_SAMPLE_DIR 指向本地导出目录，缺失即 skip（CI 无样本不红）。
纯离线解析层断言（不物化、不落库），验证改版后按资产级别分类别抽取正确、且旧版式不回归。"""

from __future__ import annotations

import os

import pytest

from bottleneck_hunter.vip import ingest

_DIR = os.environ.get("CITI_SAMPLE_DIR", "")
_NEW = "全部-仓盘_06_Aug_2026_10_09_15.pdf"   # `截⾄ <date>` 版式
_OLD = "全部-仓盘_24_Jul_2026_08_58_40.pdf"   # 紧凑无 `截⾄` 版式（块列更少，同为资产级别行布局）
_SEP = "全部-仓盘_18_Sep_2026_11_06_31.pdf"   # 裸日期无 `截⾄` 版式（市场价格截止日为裸日期）


def _parse(fn: str):
    path = os.path.join(_DIR, fn)
    if not _DIR or not os.path.exists(path):
        pytest.skip(f"样本缺失（设 CITI_SAMPLE_DIR 指向本地花旗导出目录）：{fn}")
    with open(path, "rb") as f:
        pages = ingest._extract_pages(f.read())
    return ingest._parse_citi_position_report(pages, fn, "test_hash")


# ── 币种金额：会计式负数（括号在币种符号内外皆可）──────────────────────
def test_currency_amount_paren_negative():
    c = ingest._currency_amount
    assert c("$(11,595.06)") == ("USD", -11595.06)      # 花旗期权负 MTM：括号在 $ 之后
    assert c("CNY (78,252.14)") == ("CNY", -78252.14)   # 括号在整体外
    assert c("$1,227,550.41") == ("USD", 1227550.41)
    assert c("-") is None and c("") is None


# ── 新版式：按资产级别分类别 ──────────────────────────────────────────
def test_citi_new_layout_categories():
    st = _parse(_NEW)
    # 股票+商品+固收 → holdings（15 股 + 1 GLD + 2 固收 = 18）
    assert len(st.holdings) == 18, [h.ticker for h in st.holdings]
    for tk in ("GOOGL", "NVDA", "TSLA", "GLD"):
        assert tk in [h.ticker for h in st.holdings]
    assert sum(h.market_value_usd for h in st.holdings) > 20_000_000  # 量级 ~2225 万美元

    # 结构性产品(MLI)+股票期权(accumulator) → derivative_terms（6 MLI + 2 期权 = 8）
    fam = [d["product_family"] for d in st.derivative_terms]
    assert fam.count("equity_mli_booster") == 6, fam
    assert fam.count("equity_accumulator") == 2, fam
    # 期权负 MTM 保留（不因 mv<0 被丢）
    acc = [d for d in st.derivative_terms if d["product_family"] == "equity_accumulator"]
    assert all(d["terms"]["market_value_usd"] < 0 for d in acc), acc
    assert {d["underlying_symbol"] for d in acc} == {"NVIDIA", "TESLA"}, acc
    # MLI 标的口径与月结单一致（split()[0].split('+')[0]）：LITE/MU/EWT/META/NVDA 各现
    mli_syms = {d["underlying_symbol"] for d in st.derivative_terms if d["product_family"] == "equity_mli_booster"}
    assert {"MU", "META", "NVDA", "EWT", "LITE"} <= mli_syms, mli_syms

    # 私募股权 → account_summary 单列（4 笔），不进 holdings/derivative
    pe = st.account_summary.get("private_equity") or []
    assert len(pe) == 4, pe

    # 负债(贷款) → loan_outstanding_usd（>0），不进 holdings
    assert (st.account_summary.get("loan_outstanding_usd") or 0) > 0

    # 现金类 → cash_balances（>0），总现金 USD 正
    assert len(st.cash_balances) >= 1 and st.total_cash_usd > 0


# ── 紧凑无 `截⾄` 版式(7/24)：块列比 8/06 少(无账户种类/当前值/应计/日期行)，仍分类别抽全 ──
# 注：此文件早前被误当「不含衍生品的旧版式」；实含 5 MLI+2 累加器+4 PE+3 贷款，此前因结构性产品/
# 期权块无 Ticker/ISIN 被旧固定偏移路径静默丢弃。以「变化率%」行为版式不变锚后与 8/06、9/18 同抽取。
def test_citi_compact_layout():
    st = _parse(_OLD)
    assert len(st.holdings) == 18, [h.ticker for h in st.holdings]
    assert sum(h.market_value_usd for h in st.holdings) > 20_000_000  # 量级 ~2231 万美元
    # 持仓无「零市值/数量=1」的错解残留（紧凑版列偏移错位的典型症状）
    assert not [h.ticker for h in st.holdings if h.market_value_usd == 0 or h.quantity == 1.0]
    # 衍生品：5 MLI + 2 累加器（此前静默丢弃，现正确落库）
    fam = [d["product_family"] for d in st.derivative_terms]
    assert fam.count("equity_mli_booster") == 5, fam
    assert fam.count("equity_accumulator") == 2, fam
    acc = [d for d in st.derivative_terms if d["product_family"] == "equity_accumulator"]
    assert all(d["terms"]["market_value_usd"] < 0 for d in acc), acc  # 累加器负 MTM 保留
    assert {d["underlying_symbol"] for d in acc} == {"NVIDIA", "TESLA"}, acc
    assert len(st.account_summary.get("private_equity") or []) == 4
    assert (st.account_summary.get("loan_outstanding_usd") or 0) > 0


# ── 裸日期无 `截⾄` 版式(9/18)：市场价格截止日为裸日期(如 `17 Sep 2026`)，同分类别抽全 ──
def test_citi_bare_date_layout():
    st = _parse(_SEP)
    assert len(st.holdings) == 19, [h.ticker for h in st.holdings]
    assert not [h.ticker for h in st.holdings if h.market_value_usd == 0 or h.quantity == 1.0]
    fam = [d["product_family"] for d in st.derivative_terms]
    assert fam.count("equity_mli_booster") == 6, fam
    assert fam.count("equity_accumulator") == 2, fam
    # 非美元持仓正确折算：700.HK 记本地币 HKD、EUR 债记 EUR，_usd 为正
    by_tk = {h.ticker: h for h in st.holdings}
    assert by_tk["700"].nominal_ccy == "HKD" and by_tk["700"].market_value_usd > 0
    assert any(h.nominal_ccy == "EUR" and h.market_value_usd > 0 for h in st.holdings)
    assert len(st.account_summary.get("private_equity") or []) == 4
    assert (st.account_summary.get("loan_outstanding_usd") or 0) > 0
    assert st.total_cash_usd > 0 and st.period_end == "2026-09-18"
