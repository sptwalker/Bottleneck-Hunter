"""挂单交易（限价单）生命周期 —— store 状态机：rest → get_resting → mark_executed/expire。

运行：pytest tests/test_resting_orders.py -q
"""
import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore
from bottleneck_hunter.watchlist.market_hours import is_market_open
from datetime import datetime


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(tmp_path / "t.db")


_FAR = "2099-01-01T00:00:00+00:00"


def _mk(store, ticker="AAPL", action="buy", target=100.0):
    pid = store.create_execution_plan(
        tactical_plan_id="tp1", entry_id="e1", ticker=ticker,
        result_json={"action": action, "shares": 10, "target_price": target},
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
        result_json={"action": "buy", "shares": 5, "target_price": 50.0})
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
    import tempfile, pathlib
    for name in [n for n in dir(m) if n.startswith("test_")]:
        fn = getattr(m, name)
        if "store" in fn.__code__.co_varnames:
            with tempfile.TemporaryDirectory() as d:
                fn(WatchlistStore(pathlib.Path(d) / "t.db"))
        else:
            fn()
    print("挂单生命周期自检通过")
