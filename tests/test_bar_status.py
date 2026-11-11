"""未收盘 bar 语义（2026-11）：盘中写入的当日快照标 provisional，默认读不到。

病史：盘中触发的决策以当时价写当日快照，之后这根 bar 就成了"最新收盘价"，
被 L2 缺口、L4 定股、硬止损巡检一起消费——等于把盘中价当收盘价做决策。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from bottleneck_hunter.watchlist.store import WatchlistStore

BJ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def store(tmp_path):
    return WatchlistStore(str(tmp_path / "bar.db")).for_user("u1").for_market("a_stock")


def _bj(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=BJ)


def test_infer_bar_status_a_share():
    assert WatchlistStore.infer_bar_status("2026-11-12", "a_stock", _bj(2026, 11, 12, 10, 0)) == "provisional"
    assert WatchlistStore.infer_bar_status("2026-11-12", "a_stock", _bj(2026, 11, 12, 15, 4)) == "provisional"
    assert WatchlistStore.infer_bar_status("2026-11-12", "a_stock", _bj(2026, 11, 12, 15, 5)) == "completed"


def test_historical_bar_always_completed():
    assert WatchlistStore.infer_bar_status("2026-11-11", "a_stock", _bj(2026, 11, 12, 10, 0)) == "completed"


def test_us_bar_not_provisional():
    """美股收盘时刻在北京凌晨、窗口跨日，provisional 收益不抵复杂度——明确不做。"""
    assert WatchlistStore.infer_bar_status("2026-11-12", "us_stock", _bj(2026, 11, 12, 10, 0)) == "completed"


def test_reads_hide_provisional_by_default(store):
    store.save_snapshots([
        {"ticker": "600519.SS", "date": "2026-11-10", "close": 100.0, "bar_status": "completed"},
        {"ticker": "600519.SS", "date": "2026-11-11", "close": 101.0, "bar_status": "completed"},
    ])
    store.save_snapshots([{"ticker": "600519.SS", "date": "2026-11-12", "close": 99.0,
                           "bar_status": "provisional"}])

    assert store.get_latest_snapshot("600519.SS")["date"] == "2026-11-11"
    assert store.get_latest_snapshot("600519.SS", include_provisional=True)["date"] == "2026-11-12"
    assert len(store.get_snapshots("600519.SS")) == 2
    assert len(store.get_snapshots("600519.SS", include_provisional=True)) == 3


def test_write_infers_status_when_absent(store, monkeypatch):
    """未显式给 bar_status 时按写入时刻推断（A股 15:05 前 → provisional）。"""
    monkeypatch.setattr(WatchlistStore, "infer_bar_status",
                        classmethod(lambda cls, date, market, now_bj=None: "provisional"))
    store.save_snapshots([{"ticker": "600519.SS", "date": "2026-11-12", "close": 99.0}])
    assert store.get_latest_snapshot("600519.SS") is None
    assert store.get_latest_snapshot("600519.SS", include_provisional=True)["bar_status"] == "provisional"
