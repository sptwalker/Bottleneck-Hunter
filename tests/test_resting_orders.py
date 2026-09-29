"""挂单交易（限价单）生命周期 —— store 状态机：rest → get_resting → mark_executed/expire。

运行：pytest tests/test_resting_orders.py -q
"""
from datetime import datetime

import pytest

from bottleneck_hunter.watchlist.market_hours import is_market_open
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "t.db")


_FAR = "2099-01-01T00:00:00+00:00"


def _mk(store, ticker="AAPL", action="buy", target=100.0):
    pid = store.create_execution_plan(
        tactical_plan_id="tp1", entry_id="e1", ticker=ticker,
        result_json={"action": action, "shares": 10, "target_price": target},
        strict=False,
    )
    assert store.confirm_execution(pid)  # pending → confirmed
    return pid


def test_rest_and_get_resting(store):
    pid = _mk(store)
    assert store.rest_execution(pid, _FAR)
    resting = store.get_resting_executions()
    assert len(resting) == 1 and resting[0]["id"] == pid
    assert resting[0]["resting_until"] == _FAR
    # 挂单不在「待确认」队列
    assert all(e["id"] != pid for e in store.get_pending_executions())


def test_rest_repeat_does_not_extend(store):
    pid = _mk(store)
    store.rest_execution(pid, _FAR)
    store.rest_execution(pid, "2100-06-06T00:00:00+00:00")  # 二次挂单不应续期
    assert store.get_resting_executions()[0]["resting_until"] == _FAR


def test_mark_executed_clears_resting(store):
    pid = _mk(store)
    store.rest_execution(pid, _FAR)
    assert store.mark_executed(pid)
    assert store.get_resting_executions() == []
    plan = store.get_execution_plan(pid)
    assert plan["status"] == "executed" and plan["executed_at"]


def test_expire_cancels_resting(store):
    pid = _mk(store)
    store.rest_execution(pid, _FAR)
    assert store.expire_execution(pid, "[用户取消]")
    assert store.get_resting_executions() == []
    plan = store.get_execution_plan(pid)
    assert plan["status"] == "expired" and "[用户取消]" in plan["rejection_reason"]


def test_clear_pending_spares_resting(store):
    pid = _mk(store)
    store.rest_execution(pid, _FAR)
    _mk_pending = store.create_execution_plan(
        tactical_plan_id="tp2", entry_id="e2", ticker="MSFT",
        result_json={"action": "buy", "shares": 5, "target_price": 50.0}, strict=False)
    store.clear_pending_executions()
    assert len(store.get_resting_executions()) == 1   # 挂单保留
    assert store.get_pending_executions() == []       # pending 被清


def test_market_hours_gate():
    def bj(h, mi):
        return datetime(2026, 7, 22, h, mi)  # 周三，naive 当北京时刻用
    # is_market_open 接受 tz-aware，这里补 tz
    from zoneinfo import ZoneInfo
    _BJ = ZoneInfo("Asia/Shanghai")
    assert is_market_open("a_stock", datetime(2026, 7, 22, 10, 0, tzinfo=_BJ))
    assert not is_market_open("a_stock", datetime(2026, 7, 22, 16, 0, tzinfo=_BJ))
    assert is_market_open("us_stock", datetime(2026, 7, 22, 22, 0, tzinfo=_BJ))
    assert not is_market_open("us_stock", datetime(2026, 7, 22, 12, 0, tzinfo=_BJ))


if __name__ == "__main__":
    import sys
    m = sys.modules[__name__]
    import pathlib
    import tempfile
    for name in [n for n in dir(m) if n.startswith("test_")]:
        fn = getattr(m, name)
        if "store" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as d:
                fn(WatchlistStore(pathlib.Path(d) / "t.db"))
        else:
            fn()
    print("挂单生命周期自检通过")


def test_rest_logs_only_first_time(store):
    """挂单轮询每小时调一次 rest_execution —— 只有首次落挂单记日志，不刷一串假到期日。"""
    pid = _mk(store)
    assert store.rest_execution(pid, _FAR)
    assert not store.rest_execution(pid, "2100-06-06T00:00:00+00:00")
    rows = [r for r in store.get_execution_status_log(pid) if "转挂单" in (r.get("reason") or "")]
    assert len(rows) == 1 and _FAR in rows[0]["reason"]


def test_stale_resting_yields_to_new_decision(store):
    """美股 9-25 起零成交的根因：挂单价远低于现价、成交不了，还占着坑挡住新计划。

    本轮又点名的票：不利侧偏离 >3% 的旧挂单作废让位；贴近现价的、以及本轮没点名的都保留。
    """
    from bottleneck_hunter.watchlist.decision_engine import _supersede_stale_resting
    far_buy = _mk(store, "TSM", "buy", 420.0)       # 现价 450 → 低 6.7%
    near_buy = _mk(store, "NVDA", "buy", 224.0)     # 现价 225 → 低 0.4%
    far_sell = _mk(store, "AVGO", "sell", 380.0)    # 现价 352 → 高 8%
    untouched = _mk(store, "MRVL", "buy", 210.0)    # 偏得远，但本轮没点名
    for pid in (far_buy, near_buy, far_sell, untouched):
        store.rest_execution(pid, _FAR)
    for tk, px in (("TSM", 450.0), ("NVDA", 225.0), ("AVGO", 352.0), ("MRVL", 260.0)):
        store.save_snapshots([{"ticker": tk, "date": "2026-09-28", "close": px}])

    out = _supersede_stale_resting(store, store.get_resting_executions(), {"TSM", "NVDA", "AVGO"})
    assert out == {"TSM", "AVGO"}
    left = {r["ticker"] for r in store.get_resting_executions()}
    assert left == {"NVDA", "MRVL"}
    assert "被新决策取代" in store.get_execution_plan(far_buy)["rejection_reason"]
