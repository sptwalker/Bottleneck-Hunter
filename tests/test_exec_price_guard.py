"""Phase 0 验收：trade_executor 诚信价格闸门 —— 无真实快照/偏离过大时拒绝成交。

对应改进方案 0.4（消除 LLM 幻觉价直通模拟成交）。
运行：pytest tests/test_exec_price_guard.py -q
"""
from bottleneck_hunter.watchlist.trade_executor import execute_trade


class _FakeStore:
    """最小可跑的假 store，只实现 execute_trade 价格闸门前需要的方法。"""
    def __init__(self, snapshot_close, planned_price=100.0, action="buy"):
        self._snap_close = snapshot_close
        self._plan = {
            "id": "p1", "market": "us_stock", "action": action,
            "ticker": "TEST", "shares": 10, "target_price": planned_price,
            "result_json": {},
        }

    def for_market(self, market):
        return self  # 单市场测试，克隆即自身

    def get_execution_plan(self, plan_id):
        return self._plan

    def get_sim_account(self):
        return {"id": "acc1", "cash_balance": 1_000_000, "initial_capital": 1_000_000}

    def get_sim_positions(self, account_id):
        return []

    def get_latest_snapshot(self, ticker):
        return {"close": self._snap_close} if self._snap_close is not None else None


def test_no_snapshot_refuses_fill():
    """无真实快照 → 拒绝成交，绝不用 LLM 规划价成交。"""
    store = _FakeStore(snapshot_close=None, planned_price=100.0)
    r = execute_trade(store, "p1")
    assert "error" in r
    assert r.get("needs") == "price_snapshot", r


def test_large_deviation_refuses_fill():
    """规划价与真实市价偏离 >30% → 暂缓成交。"""
    # 真实价 100，规划价 200（偏离 100%）
    store = _FakeStore(snapshot_close=100.0, planned_price=200.0)
    r = execute_trade(store, "p1")
    assert "error" in r
    assert "偏离" in r["error"], r


def test_normal_fill_uses_real_price():
    """快照存在且偏离在阈值内 → 用真实市价成交，不触发闸门。

    此处只验证不被价格闸门拦截（会因假 store 缺少后续买入方法而在更后面失败，
    但只要不是价格闸门的两个 error 即证明闸门放行）。
    """
    store = _FakeStore(snapshot_close=100.0, planned_price=105.0)
    try:
        r = execute_trade(store, "p1")
    except AttributeError:
        # 放行后进入 _execute_buy，假 store 未实现 create_sim_trade 等 → 属正常
        return
    # 若返回了 error，必须不是价格闸门的两类
    if "error" in r:
        assert r.get("needs") != "price_snapshot"
        assert "偏离" not in r["error"]


if __name__ == "__main__":
    test_no_snapshot_refuses_fill()
    test_large_deviation_refuses_fill()
    test_normal_fill_uses_real_price()
    print("PASS: exec price guard")
