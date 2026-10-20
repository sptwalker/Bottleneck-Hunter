"""P1-2（2026-10 体检）：L2 名单稳定性——新票须昨日亦在名单才算确认。

病史：名单 9→6→5→9 只日间抖动，10-16 一次性清仓 6 只，其中两只次日被重新选回。
"""
from bottleneck_hunter.watchlist.decision_engine import _stabilize_selection


def _sel(core=(), tactical=()):
    return {"core_holdings": [{"ticker": t, "target_weight_pct": 5} for t in core],
            "tactical_holdings": [{"ticker": t, "target_weight_pct": 5} for t in tactical]}


def test_new_ticker_deferred_until_second_day():
    prev = _sel(core=["AAA"])
    cur = _sel(core=["AAA", "BBB"])
    deferred = _stabilize_selection(cur, prev, is_daily=True)
    assert deferred == ["BBB"]
    assert [h["ticker"] for h in cur["core_holdings"]] == ["AAA"]  # 存量保留，新票剔除


def test_confirmed_ticker_survives_second_day():
    prev = _sel(core=["AAA", "BBB"])
    cur = _sel(core=["AAA", "BBB"])
    assert _stabilize_selection(cur, prev, is_daily=True) == []
    assert len(cur["core_holdings"]) == 2


def test_not_daily_keeps_everything():
    """昨日没有计划（断档/首日）→ 不启用，避免把整份名单清空。"""
    cur = _sel(core=["AAA", "BBB"])
    assert _stabilize_selection(cur, {}, is_daily=False) == []
    assert len(cur["core_holdings"]) == 2


def test_dropped_ticker_leaves_immediately():
    """被移出的票不受稳定性影响：不在新名单即不在新名单（清仓由 L4 的"不在 L2"规则按天生效）。"""
    prev = _sel(core=["AAA", "BBB"])
    cur = _sel(core=["AAA"])
    assert _stabilize_selection(cur, prev, is_daily=True) == []
    assert [h["ticker"] for h in cur["core_holdings"]] == ["AAA"]


def test_tactical_bucket_also_stabilized():
    cur = _sel(tactical=["CCC"])
    assert _stabilize_selection(cur, _sel(core=["AAA"]), is_daily=True) == ["CCC"]
    assert cur["tactical_holdings"] == []
