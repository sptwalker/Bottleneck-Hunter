"""P1-4（2026-10 复核）：决策账户每日盯市。

病史：只有 VIP 账户在 job_vip_project 里重估，决策中心 sim 持仓的 market_value 停在最后一次
成交那天（10-23 后 5 个交易日未更新），L2 缺口与 L4 定股都在拿旧市值算仓位。
"""
import asyncio

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    s = WatchlistStore(str(tmp_path / "remark.db")).for_user("u1").for_market("a_stock")
    acct = s.get_sim_account()
    s.update_sim_account(cash_balance=100000, total_equity=100000, current_capital=100000)
    s.create_sim_position(acct["id"], "002815.SZ", 1000, 10.0)
    return s


def _remark(store, market="a_stock"):
    """只让 _iter_users 返回本测试的 store（真实调用路径，不绕过被测函数）。"""
    import bottleneck_hunter.watchlist.scheduler as sch

    orig = sch._iter_users
    sch._iter_users = lambda *a, **k: iter([("u1", store, None)])
    try:
        return asyncio.run(sch._remark_decision_positions(market))
    finally:
        sch._iter_users = orig


def test_remark_updates_market_value_and_equity(store):
    import bottleneck_hunter.watchlist.scheduler as sch

    store.save_snapshots([{"ticker": "002815.SZ", "date": "2026-10-27", "close": 22.3,
                           "market": "a_stock", "data_quality": "ok", "quality_notes": ""}])
    pos = store.get_sim_positions() [0]
    assert pos["market_value"] == 10000.0  # 成本价建仓

    n = _remark(store)
    pos = store.get_sim_positions()[0]
    assert pos["market_value"] == 22300.0
    assert pos["current_price"] == 22.3
    assert pos["unrealized_pnl"] == 12300.0
    acct = store.get_sim_account()
    assert acct["total_equity"] == 122300.0
    assert n == 1


def test_remark_skips_when_no_new_price(store):
    import bottleneck_hunter.watchlist.scheduler as sch

    assert _remark(store) == 0
    assert store.get_sim_positions()[0]["market_value"] == 10000.0
