"""花旗「全部-仓盘」新版式(2026-08+)持仓 PDF 解析回归。

样本含金融 PII，不入库；靠环境变量 CITI_SAMPLE_DIR 指向本地导出目录，缺失即 skip（CI 无样本不红）。
纯离线解析层断言（不物化、不落库），验证改版后按资产级别分类别抽取正确、且旧版式不回归。"""

from __future__ import annotations

import os

import pytest

from bottleneck_hunter.vip import ingest

_DIR = os.environ.get("CITI_SAMPLE_DIR", "")
_NEW = "全部-仓盘_06_Aug_2026_10_09_15.pdf"
_OLD = "全部-仓盘_24_Jul_2026_08_58_40.pdf"


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


# ── 旧版式(≤7/24)不回归：仍抽 18 只 ──────────────────────────────────
def test_citi_old_layout_no_regression():
    st = _parse(_OLD)
    assert len(st.holdings) == 18, [h.ticker for h in st.holdings]
    assert len(st.derivative_terms) == 0  # 旧仓盘不含衍生品块
    assert sum(h.market_value_usd for h in st.holdings) > 20_000_000
