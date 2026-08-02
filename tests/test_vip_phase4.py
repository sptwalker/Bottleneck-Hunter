"""Phase 4 回归守卫：组合级压力测试 / 净 Greeks / 账户级瓶颈主题暴露。

红线(§8.3/§8.6)：衍生品是「呈现敞口」绝不并回头条权益；覆盖口径必须披露；止于建议层。
- P4-1：净 Greeks——对可重放衍生品 ±1% 有限差分聚合净 delta/gamma；FCN/缺价剔出并披露，绝不 BS 臆造 exotic。
- P4-2：压力测试——股票线性 delta + 衍生品 payoff 重放增量；未建模标的剔出不伪装成 0 尾部风险。
- P4-3：瓶颈主题暴露——持仓 bottleneck_node × 权重 vs 观察池候选，diff over/under-owned；无 join 诚实 available False。
"""
import pytest

from bottleneck_hunter.vip import advisory, portfolio
from bottleneck_hunter.vip.derivatives import DerivativeTerm
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.vip.stress import net_greeks, stress_test
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod
    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _acc(afp=100.0, ds=10, stds=20, ccy="USD"):
    return DerivativeTerm(product_family="equity_accumulator", underlying_symbol="AAA", currency=ccy,
                          tenor_days=365, terms={"afp": afp, "daily_shares": ds, "step_up_daily_shares": stds})


def _fcn():
    return DerivativeTerm(product_family="equity_fcn", underlying_symbol="BBB", currency="USD",
                          tenor_days=365, terms={"strike": 90.0, "afp": 90.0, "knock_out_price": 105.0})


# ── P4-2：压力测试（股票线性 delta + 衍生品 payoff 重放）─────────────────────

def test_stress_pure_stock_linear():
    """纯股票 100k：市场 -20% → 股票 P&L 精确 -20k（线性 delta≈1），无衍生品增量。"""
    out = stress_test(100000.0, [], [{"name": "跌", "market_shock": -0.20}])
    s = out["scenarios"][0]
    assert s["stock_pnl"] == -20000.0 and s["deriv_pnl"] == 0.0 and s["total_pnl"] == -20000.0
    assert out["derivative_coverage"]["total"] == 0


def test_stress_fcn_unmodeled_not_faked_as_zero_risk():
    """红线守卫：FCN 无到期 payoff 模型 → 剔出压测 P&L 并计入 unmodeled 披露，
    绝不把路径依赖 exotic 的尾部风险静默伪装成 0 增量。"""
    out = stress_test(0.0, [{"term": _fcn(), "spot": 100.0}], [{"name": "跌", "market_shock": -0.20}])
    assert out["scenarios"][0]["deriv_pnl"] == 0.0          # 未建模 → 0 增量
    cov = out["derivative_coverage"]
    assert cov["priced"] == 0 and cov["total"] == 1 and "BBB" in cov["unmodeled"]   # 但诚实披露未覆盖


def test_stress_accumulator_crash_is_loss_not_faked_gain():
    """★核心红线守卫(§8.6)：累购市场 -20% → 现价跌破 AFP「加倍套牢」，deriv_pnl 必须为**负**(尾部损失)，
    绝不因 step-up 令裸市值上升而把巨亏反转成大额收益。用经济 P&L(相对 AFP)非裸 shares×spot。"""
    out = stress_test(0.0, [{"term": _acc(), "spot": 100.0}],
                      [{"name": "涨", "market_shock": 0.10}, {"name": "跌", "market_shock": -0.20}])
    assert out["derivative_coverage"]["priced"] == 1
    down = next(s for s in out["scenarios"] if s["market_shock_pct"] == -20.0)
    # base pnl(100)=0；shocked pnl(80)=365×20×(80−100)=−146000 → deriv_pnl<0（如实呈现为损失）
    assert down["deriv_pnl"] < 0, out


def test_stress_non_usd_excluded_not_summed_as_usd():
    """§8.3 币种守卫：HKD 累购现价为原币种、无 FX → 剔入 unmodeled，绝不把港币量级(~7.8×虚高)混进 *_usd。"""
    out = stress_test(0.0, [{"term": _acc(ccy="HKD"), "spot": 400.0}], [{"name": "跌", "market_shock": -0.20}])
    assert out["scenarios"][0]["deriv_pnl"] == 0.0          # 非美元剔出，不进增量
    assert "AAA" in out["derivative_coverage"]["unmodeled"]  # 诚实披露未覆盖(非静默 ÷1 冒充美元)


# ── P4-1：净 Greeks（有限差分聚合）──────────────────────────────────────────

def test_net_greeks_directional_and_coverage():
    """累购在现价 120（远离 AFP=100，同 regime）有多头方向敞口 → net_delta_usd>0；FCN 计入 unmodeled 不污染。"""
    ng = net_greeks([{"term": _acc(), "spot": 120.0}, {"term": _fcn(), "spot": 100.0}])
    assert ng["net_delta_usd"] > 0                          # 现价涨→组合盈，方向正确
    assert ng["coverage"]["priced"] == 1 and "BBB" in ng["coverage"]["unmodeled"]


def test_net_greeks_near_afp_step_disclosed():
    """累购现价贴近 AFP(100)→ ±1% 跨 step-up 折点 → near_afp_step 披露(阶跃口径非平滑导数，凸性真实极大)。"""
    ng = net_greeks([{"term": _acc(), "spot": 100.0}])
    assert "AAA" in ng["coverage"]["near_afp_step"]
    assert "AFP" in ng["basis"]                             # basis 提示阶跃口径


def test_net_greeks_empty_when_no_derivs():
    """无衍生品 → 净 Greeks 全 0、覆盖 0/0（不臆造敞口）。"""
    ng = net_greeks([])
    assert ng["net_delta_usd"] == 0.0 and ng["coverage"]["total"] == 0


# ── P4-3：账户级瓶颈主题暴露 + 机会集缺口图 ─────────────────────────────────

def test_bottleneck_theme_over_and_under_owned():
    """持仓压在「光刻胶」主题（拥挤），观察池有高分「HBM 存储」候选账户零持仓（缺口）→ over/under 各命中。"""
    holdings = [{"ticker": "A", "bottleneck_node": "光刻胶", "weight_pct": 30.0},
                {"ticker": "B", "bottleneck_node": "光刻胶", "weight_pct": 20.0},
                {"ticker": "C", "bottleneck_node": "功率半导体", "weight_pct": 10.0}]
    candidates = [{"ticker": "X", "bottleneck_node": "HBM 存储", "composite_score": 8.5, "tier": "focus"},
                  {"ticker": "Y", "bottleneck_node": "HBM 存储", "composite_score": 7.0, "tier": "normal"},
                  {"ticker": "Z", "bottleneck_node": "光刻胶", "composite_score": 6.0, "tier": "track"}]
    out = advisory.bottleneck_theme_exposure(holdings, candidates)
    assert out["available"]
    # over-owned 按权重降序：光刻胶(50%) 居首
    assert out["over_owned"][0]["theme"] == "光刻胶" and out["over_owned"][0]["weight_pct"] == 50.0
    # under-owned：观察池有候选但账户未持有 → HBM 存储；按 top_score 降序、代表取最高分 X(8.5)
    under_themes = {u["theme"]: u for u in out["under_owned"]}
    assert "HBM 存储" in under_themes and under_themes["HBM 存储"]["top_ticker"] == "X"
    assert under_themes["HBM 存储"]["top_score"] == 8.5
    # 光刻胶已持有 → 不在 under-owned
    assert "光刻胶" not in under_themes


def test_bottleneck_theme_unavailable_when_no_join():
    """持仓全无 bottleneck_node（观察池 join 未覆盖）→ available False，不硬凑主题图。"""
    holdings = [{"ticker": "A", "bottleneck_node": "", "weight_pct": 50.0}]
    cands = [{"ticker": "X", "bottleneck_node": "HBM", "composite_score": 8}]
    out = advisory.bottleneck_theme_exposure(holdings, cands)
    assert out["available"] is False and out["over_owned"] == []


def test_bottleneck_theme_exact_norm_no_substring_merge():
    """自由文本精确归一（strip/lower）：'光刻胶' 与 '光刻胶(KrF)' 是不同主题，绝不子串误并。"""
    holdings = [{"ticker": "A", "bottleneck_node": "光刻胶", "weight_pct": 30.0},
                {"ticker": "B", "bottleneck_node": " 光刻胶 ", "weight_pct": 10.0}]   # 仅空白差异 → 归一后同主题
    out = advisory.bottleneck_theme_exposure(holdings, [])
    assert out["held_theme_count"] == 1 and out["over_owned"][0]["weight_pct"] == 40.0


def test_bottleneck_theme_under_owned_rep_is_true_top_not_rounded_tiebreak():
    """代表选取用未四舍原值比较：同主题 80.04(focus) vs 80.02(normal) 且高分先迭代 →
    代表应为真最高分 80.04/focus，不因已四舍 top_score=80.0 被 80.02 用 >= 顶替。"""
    holdings = [{"ticker": "H", "bottleneck_node": "先进封装", "weight_pct": 50.0}]  # 持有别的主题→保 available
    candidates = [{"ticker": "AAA", "bottleneck_node": "HBM", "composite_score": 80.04, "tier": "focus"},
                  {"ticker": "BBB", "bottleneck_node": "HBM", "composite_score": 80.02, "tier": "normal"}]
    out = advisory.bottleneck_theme_exposure(holdings, candidates)
    hbm = next(u for u in out["under_owned"] if u["theme"] == "HBM")
    assert hbm["top_ticker"] == "AAA" and hbm["tier"] == "focus"   # 真最高分胜出，非四舍带内低分顶替
    assert "_raw" not in hbm                                        # 内部工作字段不泄漏到输出


# ── 集成：dossier 带 P4 字段 ────────────────────────────────────────────────

def _snap(wl, account_ref, as_of, holdings, total_equity, ch):
    hs = [EquityHolding(ticker=t, company=t, quantity=q, market_value_usd=mv,
                        nominal_ccy="USD", market_value_nominal=mv) for (t, q, mv) in holdings]
    tot = sum(mv for *_, mv in holdings)
    stmt = BrokerStatement(
        content_hash=ch, period_end=as_of, holdings=hs, cash_balances=[], total_cash_usd=0.0,
        recon=ReconResult(holdings_count=len(hs), holdings_total_usd=tot,
                          statement_equities_total_usd=tot, delta_usd=0.0, status="ok"))
    portfolio.normalize_statement(wl, stmt, source_doc_id=ch, account_ref=account_ref)


def test_dossier_includes_phase4_fields(wl):
    """dossier 含 stress_test + net_greeks（纯股票账户：股票线性 delta 有值、无衍生品则 Greeks 覆盖 0）。"""
    _snap(wl, "P4", "2026-06-30", [("AAPL", 10, 1000.0), ("MSFT", 10, 1000.0)], 2000.0, "q1")
    portfolio.materialize_portfolio(wl, account_ref="P4", cash_total_usd=0.0)
    dossier = portfolio.build_account_dossier(wl, account_ref="P4")
    assert "stress_test" in dossier and "net_greeks" in dossier
    st = dossier["stress_test"]
    assert st is not None and len(st["scenarios"]) == 4          # ±10%/±20%
    # 纯股票、无衍生品：-20% 情景股票 P&L = -20% × pos_mv
    down20 = next(s for s in st["scenarios"] if s["market_shock_pct"] == -20.0)
    assert down20["stock_pnl"] < 0 and down20["deriv_pnl"] == 0.0
    assert dossier["net_greeks"]["coverage"]["total"] == 0       # 无衍生品
