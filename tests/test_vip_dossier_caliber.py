"""逐仓口径自检 —— 防「MU 现价877.57 / 成本980.43 / 浮亏-10.5%」臆造回归。

交给 AI 分析师的 build_account_dossier 逐仓字段，每股口径必须统一为「美元/股」，
否则 LLM 会把美元【总额】当每股价、或拿【原币】成本比【美元】现价：

- HIGH-1：current_price 存在且是【每股】价，绝不是被当成每股的美元总市值(总额÷股数)；
- HIGH-2：非美元持仓 avg_cost 是【美元/股】(cost_basis_usd÷股数)，与美元现价可比，
  与原币每股 avg_cost_nominal 明确区分；
- 零股：真实浮点股数(规范层 quantity)进 dossier，不被 sim.shares 的 INT 截断(MU 8.62≠8)；
- 内部自洽：current_price×shares≈market_value、avg_cost×shares≈cost_basis、
  (current_price/avg_cost−1) 与 unrealized_pnl_pct 同号同量；
- 诚实降级：无成本 → avg_cost/unrealized_pnl 留 None，绝不臆造(current_price 仍给)。
"""
import pytest

from bottleneck_hunter.vip import portfolio
from bottleneck_hunter.vip.ingest import BrokerStatement, EquityHolding, ReconResult
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path, monkeypatch):
    from bottleneck_hunter.auth import store as auth_store_mod

    monkeypatch.setattr(auth_store_mod, "_DEFAULT_DB", tmp_path / "auth.db")
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def _caliber_stmt():
    """两仓同时压 HIGH-1/HIGH-2/零股：
    - MU 美元零股 8.62（现价<成本→浮亏），压 HIGH-1 + 零股截断；
    - 700 港股（原币每股成本 ≠ 美元每股成本），压 HIGH-2。
    """
    mu_qty, mu_px, mu_cost_px = 8.62, 101.8, 113.73
    mu_mv = round(mu_px * mu_qty, 2)                          # 877.52 美元总市值
    mu_cb = round(mu_cost_px * mu_qty, 2)                     # 980.35 美元总成本基
    hk_qty, hk_fx = 1000.0, 0.1275
    hk_mv_nom, hk_cost_nom_px = 800000.0, 700.0              # 港币：总市值 / 每股成本
    hk_mv_usd = round(hk_mv_nom * hk_fx, 2)                   # 102000.0 美元总市值
    hk_cb_usd = round(hk_cost_nom_px * hk_qty * hk_fx, 2)     # 89250.0 美元总成本基
    holds = [
        EquityHolding(ticker="MU", company="Micron", quantity=mu_qty,
                      market_value_usd=mu_mv, nominal_ccy="USD", market_value_nominal=mu_mv,
                      avg_cost=mu_cost_px, cost_basis_usd=mu_cb,
                      unrealized_pnl_usd=round(mu_mv - mu_cb, 2)),
        EquityHolding(ticker="700", company="Tencent (700 HK)", quantity=hk_qty,
                      market_value_usd=hk_mv_usd, nominal_ccy="HKD", market_value_nominal=hk_mv_nom,
                      avg_cost=hk_cost_nom_px, cost_basis_usd=hk_cb_usd,
                      unrealized_pnl_usd=round(hk_mv_usd - hk_cb_usd, 2)),
    ]
    total = sum(h.market_value_usd for h in holds)
    return BrokerStatement(content_hash="caliber-1", period_end="2026-06-30", holdings=holds,
                           cash_balances=[], total_cash_usd=0.0,
                           recon=ReconResult(holdings_count=2, holdings_total_usd=total,
                                             statement_equities_total_usd=total, delta_usd=0.0, status="ok"))


def test_dossier_per_position_caliber_usd_per_share(wl):
    portfolio.normalize_statement(wl, _caliber_stmt(), account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=0.0)
    holds = {x["ticker"]: x for x in portfolio.build_account_dossier(wl, account_ref="A1")["holdings"]}
    mu, hk = holds["MU"], holds["700"]

    # 零股：真实浮点股数进 dossier，未被 sim.shares 的 INT 截断（8.62≠8）
    assert abs(mu["shares"] - 8.62) < 1e-6, mu["shares"]

    # HIGH-1：current_price 是【每股】价（101.8），不是美元总市值(877.52)当每股
    assert mu["current_price"] is not None
    assert abs(mu["current_price"] - 101.8) < 0.1, mu
    assert mu["current_price"] < mu["market_value"] / 2, mu   # 铁证：绝不是总市值本身

    # 逐仓内部自洽：现价×股数≈市值、成本×股数≈成本基（每股口径，非总额）
    for x in (mu, hk):
        assert abs(x["current_price"] * x["shares"] - x["market_value"]) < 1.0, x
        assert abs(x["avg_cost"] * x["shares"] - x["cost_basis"]) < 1.0, x

    # MU 现价<成本 → 与 unrealized_pnl_pct 同为负、量级一致（≈−10.5%）
    assert mu["current_price"] < mu["avg_cost"]
    pct_from_share = (mu["current_price"] / mu["avg_cost"] - 1) * 100
    assert abs(pct_from_share - mu["unrealized_pnl_pct"]) < 0.2, mu

    # HIGH-2：港股 avg_cost 是【美元/股】(89250/1000=89.25)，与原币每股(700 HKD)明确区分、与美元现价可比
    assert hk["currency"] == "HKD"
    assert abs(hk["avg_cost"] - 89.25) < 0.1, hk            # 美元/股
    assert abs(hk["avg_cost_nominal"] - 700.0) < 0.1, hk   # 原币/股（参考）
    assert hk["avg_cost"] != hk["avg_cost_nominal"], hk    # 二者绝不混同
    assert abs(hk["current_price"] - 102.0) < 0.1, hk      # 美元/股(102000/1000)
    assert hk["current_price"] > hk["avg_cost"] and hk["unrealized_pnl_pct"] > 0, hk  # 浮盈同号


def test_dossier_missing_cost_degrades_to_none(wl):
    """无成本(仓盘导出且无历史结转)→ avg_cost/unrealized_pnl 留 None，绝不臆造；
    current_price 仍给（有市值+股数即可算每股价）。"""
    h = EquityHolding(ticker="GOOGL", company="Alphabet", quantity=100,
                      market_value_usd=260000.0, nominal_ccy="USD", market_value_nominal=260000.0)
    stmt = BrokerStatement(content_hash="nocost-1", period_end="2026-06-30", holdings=[h],
                           cash_balances=[], total_cash_usd=0.0,
                           recon=ReconResult(holdings_count=1, holdings_total_usd=260000.0,
                                             statement_equities_total_usd=260000.0, delta_usd=0.0, status="ok"))
    portfolio.normalize_statement(wl, stmt, account_ref="A1")
    portfolio.materialize_portfolio(wl, as_of_date="2026-06-30", account_ref="A1", cash_total_usd=0.0)
    g = {x["ticker"]: x for x in portfolio.build_account_dossier(wl, account_ref="A1")["holdings"]}["GOOGL"]
    assert g["avg_cost"] is None and g["unrealized_pnl"] is None      # 无成本→不臆造
    assert abs(g["current_price"] - 2600.0) < 0.1, g                  # 260000/100 每股价仍给


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
