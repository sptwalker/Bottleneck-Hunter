"""P0-2 资金路径边界测试 · vip.projection

盯校准闭环的钱路：15% flag 阈值边界、真值缺失不除零、空账户早退、市场隔离。
需 tmp WatchlistStore。
"""
import pytest

from bottleneck_hunter.vip.projection import calibrate_projections, project_stock_mtm
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def wl(tmp_path):
    return WatchlistStore(tmp_path / "wl.db").for_user("u1").for_market("us_stock")


def test_project_stock_mtm_empty_account(wl):
    r = project_stock_mtm(wl, "A")  # 无任何持仓快照 → 早退，不崩
    assert r["n"] == 0 and r["total_mv_base"] == 0.0 and r["n_priced"] == 0


def test_calibrate_flag_threshold_boundary(wl):
    d = "2026-07-20"
    # AAPL 恰好 15%（不 flag），MSFT 16%（flag）
    wl.upsert_projection(account_ref="A", as_of_date=d, ticker="AAPL", market_value_base=115.0)
    wl.upsert_projection(account_ref="A", as_of_date=d, ticker="MSFT", market_value_base=116.0)
    r = calibrate_projections(wl, "A", [
        {"symbol": "AAPL", "market_value_base": 100.0},  # diff=+15.0% → 阈内
        {"symbol": "MSFT", "market_value_base": 100.0},  # diff=+16.0% → 超阈
    ])
    assert r["n_calibrated"] == 1 and r["n_flagged"] == 1


def test_calibrate_unmatched_no_division(wl):
    d = "2026-07-20"
    wl.upsert_projection(account_ref="A", as_of_date=d, ticker="NVDA", market_value_base=1000.0)
    # 结算单里没有 NVDA（已清仓/未披露）→ 不比对、不除零、保持 pending
    r = calibrate_projections(wl, "A", [{"symbol": "AMD", "market_value_base": 5000.0}])
    assert r["n_unmatched"] == 1 and r["n_calibrated"] == 0 and r["n_flagged"] == 0


def test_calibrate_empty_when_no_projection(wl):
    r = calibrate_projections(wl, "A", [{"symbol": "AAPL", "market_value_base": 100.0}])
    assert r == {"account_ref": "A", "n_calibrated": 0, "n_flagged": 0, "n_unmatched": 0}


def test_projection_market_isolated(wl):
    wl.upsert_projection(account_ref="A", as_of_date="2026-07-20", ticker="AAPL", market_value_base=100.0)
    cn = wl.for_market("cn_stock")
    # A股账户校准看不到美股账户的推算 → 无可校准，全 0
    r = calibrate_projections(cn, "A", [{"symbol": "AAPL", "market_value_base": 100.0}])
    assert r["n_calibrated"] == 0 and r["n_flagged"] == 0 and r["n_unmatched"] == 0
