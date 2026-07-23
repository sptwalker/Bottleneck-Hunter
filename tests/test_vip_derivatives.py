"""C2 衍生品：BS 基准 / 两类 term sheet 抽取 / 场景收益。"""
from pathlib import Path

import pytest

from bottleneck_hunter.vip import derivatives as d

DIR = Path(r"C:\Users\walker\Documents\walker\银行文件\花旗日常文件")
ACC_MU = DIR / "EA26202070024_ECU082-62309ENG.PDF"
MLI_MU = DIR / "M0O26072205_S_1000000.PDF"


def test_bs_baseline_and_iv_roundtrip():
    p = d.bs_price(100, 100, 1, 0.05, 0.2, True)
    assert abs(p - 10.4506) < 1e-3
    iv = d.implied_vol(p, 100, 100, 1, 0.05, True)
    assert iv and abs(iv - 0.2) < 1e-3


def test_payoff_accumulator():
    t = d.DerivativeTerm("equity_accumulator", "MU", "USD", 365,
                         {"afp": 100.0, "daily_shares": 3, "step_up_daily_shares": 6})
    r = d.payoff_accumulator(t, 80.0, knock_out_happened=False, days_observed=10)
    assert r["shares_acquired"] == 60
    assert r["pnl"] < 0


def test_payoff_mli_booster():
    t = d.DerivativeTerm("equity_mli_booster", "MU", "USD", 120,
                         {"initial_price": 100.0, "participation_factor": 1.0,
                          "max_upside_pct": 0.5, "strike_pct_initial": 1.0, "knock_in_pct_initial": 0.5379})
    assert d.payoff_mli_booster(t, 130.0, knock_in_happened=False)["return_pct"] > 0
    assert d.payoff_mli_booster(t, 80.0, knock_in_happened=True)["return_pct"] < 0


@pytest.mark.skipif(not ACC_MU.exists(), reason="真实样本不存在")
def test_extract_accumulator_terms_real_sample():
    t = d.extract_accumulator_terms(str(ACC_MU))
    assert t.product_family == "equity_accumulator"
    assert t.underlying_symbol == "MU"
    assert t.currency == "USD"
    assert t.terms["daily_shares"] == 3
    assert t.terms["step_up_daily_shares"] == 6
    assert abs(t.terms["afp"] - 625.5927) < 1e-4
    assert abs(t.terms["knock_out_price"] - 910.7569) < 1e-4
    assert t.terms["max_nominal_shares"] == 1506


@pytest.mark.skipif(not MLI_MU.exists(), reason="真实样本不存在")
def test_extract_mli_terms_real_sample():
    t = d.extract_mli_terms(str(MLI_MU))
    assert t.product_family == "equity_mli_booster"
    assert t.underlying_symbol == "MU"
    assert t.currency == "USD"
    assert abs(t.terms["initial_price"] - 938.2) < 1e-6
    assert abs(t.terms["knock_in_price"] - 504.6578) < 1e-4
    assert abs(t.terms["strike_price"] - 938.2) < 1e-6
    assert abs(t.terms["knock_in_pct_initial"] - 0.5379) < 1e-6
    assert abs(t.terms["strike_pct_initial"] - 1.0) < 1e-9
    assert abs(t.terms["max_upside_pct"] - 0.5) < 1e-6


def test_classify_pdf():
    assert d.classify_pdf(str(ACC_MU)) in ("accumulator", "decumulator", "fund_report", "mli", "other")
