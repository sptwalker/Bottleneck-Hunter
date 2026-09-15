"""P1-4 组合层风险预算与约束专项测试。"""

from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.portfolio_budget import (
    BudgetLimits,
    BudgetReport,
    check_portfolio_budget,
)

E = 1_000_000


def _pos(ticker, mv, **kw):
    return {"ticker": ticker, "market_value": mv, **kw}


# ---------------------------------------------------------------------------
# 通过：均衡组合全维度不超限
# ---------------------------------------------------------------------------

def test_balanced_portfolio_passes():
    r = check_portfolio_budget(
        [_pos("A", 200_000, sector="半导体", chain_node="晶圆制造", adv=5_000_000),
         _pos("B", 150_000, sector="医药", chain_node="CRO", adv=3_000_000)],
        total_equity=E, cash=300_000, factor_exposures={"market": 0.9}, portfolio_cvar=50_000)
    assert isinstance(r, BudgetReport)
    assert r.ok and r.approved
    assert r.breaches == ()


# ---------------------------------------------------------------------------
# 单票
# ---------------------------------------------------------------------------

def test_single_name_reject_and_suggested_scale():
    r = check_portfolio_budget([_pos("BIG", 400_000)], total_equity=E)
    assert r.status == "reject"
    c = r.rejected[0]
    assert c.dimension == "single_name" and c.subject == "BIG"
    assert c.usage == 40.0 and c.limit == 25.0
    # 建议缩仓到合规：25/40
    assert c.suggested_scale == round(25.0 / 40.0, 4)


def test_single_name_degrade_within_band():
    # 27% 落在 25%~28.75%(=25×1.15) → 降级而非拒绝
    r = check_portfolio_budget([_pos("MID", 270_000)], total_equity=E)
    assert r.status == "degrade"
    assert r.degraded[0].dimension == "single_name"
    assert not r.rejected


def test_single_name_exact_limit_passes():
    # 恰好等于上限不算超限
    r = check_portfolio_budget([_pos("EDGE", 250_000)], total_equity=E)
    assert r.ok


# ---------------------------------------------------------------------------
# 行业
# ---------------------------------------------------------------------------

def test_sector_concentration_reject():
    r = check_portfolio_budget(
        [_pos(t, 200_000, sector="半导体") for t in ("X", "Y", "Z")], total_equity=E)
    sec = [c for c in r.breaches if c.dimension == "sector"]
    assert sec and sec[0].status == "reject"
    assert sec[0].usage == 60.0


def test_untagged_sector_bucketed_as_unknown():
    # 无 sector 标签归「未知」桶，大未知桶本身即真实集中度
    r = check_portfolio_budget([_pos(t, 150_000) for t in ("A", "B", "C")], total_equity=E)
    assert any(c.dimension == "sector" and c.subject == "未知" for c in r.breaches)


# ---------------------------------------------------------------------------
# 链条（产业链环节聚集）
# ---------------------------------------------------------------------------

def test_chain_cluster_across_sectors_reject():
    # 跨两个行业但同属「先进封装」环节，合计 50% > 35%
    r = check_portfolio_budget(
        [_pos("P", 250_000, sector="半导体", chain_node="先进封装"),
         _pos("Q", 250_000, sector="设备", chain_node="先进封装")],
        total_equity=E)
    ch = [c for c in r.breaches if c.dimension == "chain"]
    assert ch and ch[0].status == "reject"
    assert ch[0].subject == "先进封装" and ch[0].usage == 50.0


def test_untagged_chain_not_clustered():
    # 未标注 chain_node 的标的不参与链条聚类（不凭空造簇）
    r = check_portfolio_budget(
        [_pos("A", 300_000), _pos("B", 300_000)], total_equity=E)
    assert not [c for c in r.breaches if c.dimension == "chain"]


# ---------------------------------------------------------------------------
# 因子净暴露
# ---------------------------------------------------------------------------

def test_factor_exposure_reject():
    r = check_portfolio_budget(
        [_pos("H", 100_000)], total_equity=E, factor_exposures={"market": 1.5})
    fa = [c for c in r.breaches if c.dimension == "factor"]
    assert fa and fa[0].status == "reject" and fa[0].subject == "market"


def test_factor_negative_exposure_uses_abs():
    # 负暴露按绝对值判上限（做空动量的暴露也算敞口）
    r = check_portfolio_budget(
        [_pos("H", 100_000)], total_equity=E, factor_exposures={"momentum": -1.5})
    assert any(c.dimension == "factor" and c.status == "reject" for c in r.breaches)


def test_no_factor_exposures_skips_dimension():
    r = check_portfolio_budget([_pos("H", 100_000)], total_equity=E)
    assert not [c for c in r.checks if c.dimension == "factor"]


# ---------------------------------------------------------------------------
# 流动性
# ---------------------------------------------------------------------------

def test_liquidity_days_reject():
    # 市值 100k / (ADV 50k × 参与率 0.2) = 10 天 > 5 天
    r = check_portfolio_budget([_pos("ILLIQ", 100_000, adv=50_000)], total_equity=E)
    lq = [c for c in r.breaches if c.dimension == "liquidity"]
    assert lq and lq[0].status == "reject"
    assert lq[0].usage == 10.0


def test_liquidity_coverage_gap_degrades():
    # 部分标的有 ADV、部分无 → 覆盖率缺口降级，缺口不作通过论
    r = check_portfolio_budget(
        [_pos("L", 10_000, adv=9_000_000), _pos("NOADV", 10_000)], total_equity=E)
    cov = [c for c in r.checks if c.dimension == "coverage"]
    assert cov and cov[0].status == "degrade"
    assert cov[0].usage == 1.0 and cov[0].limit == 2.0


def test_no_adv_anywhere_skips_liquidity():
    # 全无 ADV → 不体检流动性、也不谎报覆盖率
    r = check_portfolio_budget([_pos("A", 100_000), _pos("B", 100_000)], total_equity=E)
    assert not [c for c in r.checks if c.dimension in ("liquidity", "coverage")]


# ---------------------------------------------------------------------------
# CVaR 预算
# ---------------------------------------------------------------------------

def test_cvar_budget_reject():
    # 尾部日损 120k/1M = 12% > 8%
    r = check_portfolio_budget([_pos("R", 100_000)], total_equity=E, portfolio_cvar=120_000)
    cv = [c for c in r.breaches if c.dimension == "cvar"]
    assert cv and cv[0].status == "reject" and cv[0].usage == 12.0


def test_cvar_within_budget_passes():
    r = check_portfolio_budget([_pos("R", 100_000)], total_equity=E, portfolio_cvar=50_000)
    assert not [c for c in r.breaches if c.dimension == "cvar"]


# ---------------------------------------------------------------------------
# 现金下限
# ---------------------------------------------------------------------------

def test_cash_floor_reject():
    # 现金 5% < 15%×0.85=12.75% → 拒绝
    r = check_portfolio_budget([_pos("S", 100_000)], total_equity=E, cash=50_000)
    cash = [c for c in r.breaches if c.dimension == "cash"]
    assert cash and cash[0].status == "reject"
    assert cash[0].suggested_scale is None  # 现金是下限，无缩仓比例


def test_cash_floor_degrade_near_limit():
    # 现金 13% 在 12.75%~15% → 降级
    r = check_portfolio_budget([_pos("S", 100_000)], total_equity=E, cash=130_000)
    assert any(c.dimension == "cash" and c.status == "degrade" for c in r.degraded)


def test_cash_above_floor_passes():
    r = check_portfolio_budget([_pos("S", 100_000)], total_equity=E, cash=300_000)
    assert not [c for c in r.breaches if c.dimension == "cash"]


# ---------------------------------------------------------------------------
# 最坏态聚合 & 放行语义
# ---------------------------------------------------------------------------

def test_overall_status_is_worst_of_all():
    # 同时：单票拒绝 + 现金降级 → 全局取最坏「拒绝」
    r = check_portfolio_budget([_pos("BIG", 400_000)], total_equity=E, cash=130_000)
    assert r.status == "reject"
    assert not r.approved
    dims = {c.dimension for c in r.breaches}
    assert "single_name" in dims


def test_degrade_still_approved():
    # 仅降级 → approved=True（放行但须缩仓），不是拒绝
    r = check_portfolio_budget([_pos("MID", 270_000)], total_equity=E)
    assert r.approved and not r.ok


# ---------------------------------------------------------------------------
# 零预算禁止敞口
# ---------------------------------------------------------------------------

def test_zero_budget_forbids_any_exposure():
    lim = BudgetLimits(max_single_pct=0.0)
    r = check_portfolio_budget([_pos("A", 1_000)], total_equity=E, limits=lim)
    c = [x for x in r.breaches if x.dimension == "single_name"]
    assert c and c[0].status == "reject" and c[0].suggested_scale == 0.0


# ---------------------------------------------------------------------------
# 输入校验：非正权益绝不冒充通过
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("equity", [0, -1, -100_000])
def test_non_positive_equity_raises(equity):
    with pytest.raises(ValueError, match="total_equity"):
        check_portfolio_budget([_pos("A", 1)], total_equity=equity)


def test_empty_portfolio_is_ok():
    # 空组合无任何敞口 → 通过（无可体检项）
    r = check_portfolio_budget([], total_equity=E)
    assert r.ok and r.checks == ()


# ---------------------------------------------------------------------------
# from_constraints 复用既有 regime 词汇
# ---------------------------------------------------------------------------

def test_from_constraints_maps_regime_vocabulary():
    lim = BudgetLimits.from_constraints({
        "max_single_position_pct": 30.0,
        "max_sector_pct": 50.0,
        "min_cash_pct": 10.0,
        "max_portfolio_beta": 1.3,
    })
    assert lim.max_single_pct == 30.0
    assert lim.max_sector_pct == 50.0
    assert lim.min_cash_pct == 10.0
    assert lim.max_factor_exposure == 1.3  # beta 视作市场因子暴露上限
    # 未提供的链条/流动性/CVaR 走默认
    assert lim.max_chain_pct == 35.0


def test_from_constraints_overrides_win():
    lim = BudgetLimits.from_constraints({"max_single_position_pct": 30.0}, max_chain_pct=20.0)
    assert lim.max_single_pct == 30.0 and lim.max_chain_pct == 20.0


# ---------------------------------------------------------------------------
# 毛敞口口径：市值取绝对值
# ---------------------------------------------------------------------------

def test_gross_exposure_uses_abs_market_value():
    # 空头 -400k 的毛敞口仍是 40% → 触发单票上限
    r = check_portfolio_budget([_pos("SHORT", -400_000)], total_equity=E)
    assert any(c.dimension == "single_name" and c.status == "reject" for c in r.rejected)
