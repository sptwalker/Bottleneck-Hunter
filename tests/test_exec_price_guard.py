"""trade_executor 价格闸门 —— 无真实快照拒绝成交；未达限价则转挂单（不再用规划价直通）。

对应改进方案 0.4 + 挂单交易（限价单）。运行：pytest tests/test_exec_price_guard.py -q
"""
from bottleneck_hunter.watchlist.trade_executor import execute_trade


class _FakeStore:
    """最小可跑的假 store，只实现 execute_trade 价格/限价闸门前需要的方法。"""
    def __init__(self, snapshot_close, planned_price=100.0, action="buy", positions=None,
                 resting_until="", claim_ok=True):
        self._snap_close = snapshot_close
        self._positions = positions or []
        self.rested = None
        self.rejected = None
        self.unclaimed = None
        self._claim_ok = claim_ok
        self._plan = {
            "id": "p1", "market": "us_stock", "action": action,
            "ticker": "TEST", "shares": 10, "target_price": planned_price,
            "result_json": {}, "resting_until": resting_until,
        }

    def for_market(self, market):
        return self  # 单市场测试，克隆即自身

    def get_execution_plan(self, plan_id):
        return self._plan

    def get_sim_account(self):
        return {"id": "acc1", "cash_balance": 1_000_000, "initial_capital": 1_000_000}

    def get_sim_positions(self, account_id):
        return self._positions

    def get_latest_snapshot(self, ticker):
        return {"close": self._snap_close} if self._snap_close is not None else None

    def rest_execution(self, plan_id, resting_until):
        self.rested = (plan_id, resting_until)
        return True

    def mark_executed(self, plan_id):
        return self._claim_ok

    def unclaim_execution(self, plan_id, resting_until=""):
        self.unclaimed = (plan_id, resting_until)
        return True

    def reject_execution(self, plan_id, reason=""):
        self.rejected = (plan_id, reason)
        return True


def test_no_snapshot_refuses_fill():
    """无真实快照 → 拒绝成交，绝不用 LLM 规划价成交。"""
    store = _FakeStore(snapshot_close=None, planned_price=100.0)
    r = execute_trade(store, "p1")
    assert "error" in r
    assert r.get("needs") == "price_snapshot", r


def test_unfavorable_price_parks_as_resting():
    """买单：市价 200 > 挂单价 100 → 不利 → 自动转挂单等待，不成交、不报错。"""
    store = _FakeStore(snapshot_close=200.0, planned_price=100.0, action="buy")
    r = execute_trade(store, "p1")
    assert r.get("rested") is True, r
    assert r["limit_price"] == 100.0 and r["market_price"] == 200.0
    assert store.rested is not None  # 已落挂单


def test_sell_unfavorable_parks():
    """卖单：市价 80 < 挂单价 120 → 不利 → 挂单（需持仓，否则先被持仓校验拦）。"""
    pos = [{"ticker": "TEST", "shares": 100, "market_value": 8000, "sector": ""}]
    store = _FakeStore(snapshot_close=80.0, planned_price=120.0, action="sell", positions=pos)
    r = execute_trade(store, "p1")
    assert r.get("rested") is True, r


def test_favorable_price_fills():
    """买单：市价 100 ≤ 挂单价 105 → 有利 → 放行成交（假 store 缺后续方法→AttributeError 视为放行）。"""
    store = _FakeStore(snapshot_close=100.0, planned_price=105.0, action="buy")
    try:
        r = execute_trade(store, "p1")
    except AttributeError:
        return
    if "error" in r:
        assert r.get("needs") != "price_snapshot"
    assert not r.get("rested")


def test_atomic_claim_skip_when_status_changed():
    """并发取消/成交导致领单失败（mark_executed False）→ 跳过成交、不动钱（HIGH #1）。"""
    store = _FakeStore(snapshot_close=100.0, planned_price=105.0, action="buy", claim_ok=False)
    r = execute_trade(store, "p1")
    assert r.get("stale") is True and "error" in r
    assert store.unclaimed is None  # 未领到单，不需回滚，更不会成交


def test_resting_order_validation_fail_holds_not_reject():
    """挂单轮询时约束不满足 → 保持挂单，绝不撤单（#2）。"""
    # 卖单无持仓 → 校验失败；但它是挂单(resting_until 非空) → 应 hold 而非 reject
    store = _FakeStore(snapshot_close=80.0, planned_price=120.0, action="sell",
                       positions=[], resting_until="2099-01-01T00:00:00+00:00")
    r = execute_trade(store, "p1")
    assert r.get("resting_hold") is True
    assert store.rejected is None  # 没有被撤单


if __name__ == "__main__":
    test_no_snapshot_refuses_fill()
    test_unfavorable_price_parks_as_resting()
    test_sell_unfavorable_parks()
    test_favorable_price_fills()
    print("PASS: exec price/limit guard")

