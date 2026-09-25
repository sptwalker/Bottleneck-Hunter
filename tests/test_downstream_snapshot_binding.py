"""P0-3 下游快照绑定：只继承决策依据，不创建成交行情快照。"""

import asyncio
from contextlib import closing

import pytest

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    scoped = WatchlistStore(db_path=tmp_path / "downstream-binding.db").for_user("alice").for_market("us_stock")
    time = "2026-02-03T00:00:00+00:00"
    scoped.save_research_snapshot(ResearchSnapshot(
        snapshot_id="s1", market="us_stock", strategy_version="v1", as_of=time, created_at=time,
        observations=(SourceObservation(
            observation_id="o1", metric="close", ticker="AAPL", market="us_stock", value=100,
            time=dict(period_start=time, period_end=time, effective_at=time, visible_at=time, collected_at=time),
            provenance=dict(source="test", unit="USD"),
        ),),
    ))
    return scoped


def row(store, table, row_id):
    with closing(store._connect()) as conn:
        return dict(conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())


@pytest.mark.parametrize("table,method,args", [
    ("sim_trades", "create_sim_trade", ("account", "AAPL", "buy", 1, 100, 100)),
    ("auto_reviews", "create_auto_review", ("trade", "AAPL")),
])
def test_store_roundtrip_legacy_and_invalid_before_side_effect(store, table, method, args):
    create = getattr(store, method)
    legacy = create(*args, strict=False)
    bound = create(*args, snapshot_id="s1", strategy_version="v1", strict=True)
    assert (row(store, table, legacy)["snapshot_id"], row(store, table, legacy)["strategy_version"]) == (None, None)
    assert (row(store, table, bound)["snapshot_id"], row(store, table, bound)["strategy_version"]) == ("s1", "v1")

    with closing(store._connect()) as conn:
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    with pytest.raises(ValueError):
        create(*args, snapshot_id="missing", strategy_version="v1")
    with closing(store._connect()) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == before


@pytest.mark.parametrize("user,market", [("bob", "us_stock"), ("alice", "a_stock")])
def test_downstream_binding_is_scoped(store, user, market):
    other = store.for_user(user).for_market(market)
    with pytest.raises(ValueError):
        other.create_sim_trade("account", "AAPL", "buy", 1, 100, 100,
                               snapshot_id="s1", strategy_version="v1", strict=True)
    with pytest.raises(ValueError):
        other.create_auto_review("trade", "AAPL", snapshot_id="s1", strategy_version="v1", strict=True)


def test_blocked_feedback_inherits_binding(store):
    plan_id = store.create_blocked_execution(
        "tactical", "entry", "AAPL", {}, "blocked",
        snapshot_id="s1", strategy_version="v1", strict=True,
    )
    with closing(store._connect()) as conn:
        feedback = dict(conn.execute(
            "SELECT * FROM trade_feedback WHERE execution_plan_id=?", (plan_id,),
        ).fetchone())
    assert (feedback["snapshot_id"], feedback["strategy_version"]) == ("s1", "v1")


def test_reject_feedback_atomically_inherits_plan_scope_and_binding(store):
    plan_id = store.create_execution_plan(
        "tactical", "entry", "AAPL", {}, snapshot_id="s1", strategy_version="v1", strict=True,
    )
    assert store.for_market("").reject_execution(plan_id, "no")
    with closing(store._connect()) as conn:
        feedback = dict(conn.execute(
            "SELECT * FROM trade_feedback WHERE execution_plan_id=?", (plan_id,),
        ).fetchone())
    assert feedback["user_id"] == "alice"
    assert feedback["market"] == "us_stock"
    assert (feedback["snapshot_id"], feedback["strategy_version"]) == ("s1", "v1")


def test_unbound_plan_rejected_before_account_creation(store):
    from bottleneck_hunter.watchlist.trade_executor import execute_trade
    plan_id = store.create_execution_plan("tactical", "entry", "AAPL", {}, strict=False)
    assert execute_trade(store, plan_id)["code"] == "legacy_unbound"
    assert store.get_execution_plan(plan_id)["status"] == "rejected"
    with closing(store._connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sim_account").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0] == 0


@pytest.mark.parametrize("binding,code", [
    ((None, None), "legacy_unbound"),
    (("missing", "v1"), "invalid_snapshot_binding"),
    (("s1", None), "invalid_snapshot_binding"),
])
def test_confirmation_binding_error_is_business_error(store, monkeypatch, binding, code):
    from bottleneck_hunter.web import decision_api

    plan_id = store.create_execution_plan("tactical", "entry", "AAPL", {}, strict=False)
    # 模拟历史或损坏的持久化行，正常 Store 写入口禁止非法显式绑定。
    with store._write_conn() as conn:
        conn.execute("UPDATE execution_plans SET snapshot_id=?, strategy_version=? WHERE id=?",
                     (*binding, plan_id))
    monkeypatch.setattr(decision_api, "_user_store", lambda user: store)
    result = asyncio.run(decision_api.confirm_execution(plan_id, user={"sub": "alice"}))
    assert result["status"] == "error"
    assert result["trade"]["code"] == code
    assert store.get_execution_plan(plan_id)["status"] == ("rejected" if code == "legacy_unbound" else "pending")

    if code == "legacy_unbound":
        assert store.get_pending_executions() == []

    from bottleneck_hunter.watchlist.auto_execute import auto_execute_pending

    auto_plan = store.create_execution_plan("tactical", "entry", "MSFT", {}, strict=False)
    # P0-A：自动执行只看「投委会是否背书」这一条独立判据，无背书结论的计划会被直接跳过、
    # 根本走不到绑定校验。本用例要验的是绑定失败路径，故先补一条背书结论。
    store.create_committee_consensus(auto_plan, {"final_verdict": "approved"}, strict=False)
    store.create_committee_consensus(plan_id, {"final_verdict": "approved"}, strict=False)

    async def auto_execute():
        return [event async for event in auto_execute_pending(store, "us_stock")]

    events = asyncio.run(auto_execute())
    assert events[-1]["data"]["failed"] == (1 if code == "legacy_unbound" else 2)
    assert events[-1]["data"]["executed"] == 0
    assert store.get_execution_plan(auto_plan)["status"] == "rejected"
    with closing(store._connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sim_account").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0] == 0


@pytest.mark.parametrize("resting", [False, True])
def test_legacy_retirement_preserves_history_and_allows_replacement(store, resting):
    from bottleneck_hunter.watchlist.trade_executor import execute_trade

    plan_id = store.create_execution_plan("tactical", "entry", "AAPL", {}, strict=False)
    if resting:
        assert store.confirm_execution(plan_id)
        assert store.rest_execution(plan_id, "2099-01-01T00:00:00+00:00")
    assert execute_trade(store, plan_id)["code"] == "legacy_unbound"
    store.revert_to_pending(plan_id)
    historical = store.get_execution_plan(plan_id)
    assert historical["status"] == ("expired" if resting else "rejected")
    assert historical["snapshot_id"] is None
    assert historical["strategy_version"] is None
    assert store.get_pending_executions() == []
    assert store.get_resting_executions() == []
    replacement = store.create_execution_plan(
        "tactical", "entry", "AAPL", {}, snapshot_id="s1", strategy_version="v1", strict=True,
    )
    assert [plan["id"] for plan in store.get_pending_executions()] == [replacement]
    with closing(store._connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sim_account").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sim_trades").fetchone()[0] == 0


def test_reviewer_explicit_legacy_skip_and_strict_bound_write(store, monkeypatch):
    from bottleneck_hunter.watchlist import trade_reviewer
    monkeypatch.setattr(trade_reviewer, "get_llm_for_position", lambda **kwargs: (None, "", ""))

    async def review(trade_id):
        return [event async for event in trade_reviewer.run_trade_review(store.for_market(""), trade_id)]

    old = store.create_sim_trade("account", "AAPL", "sell", 1, 100, 100, strict=False)
    assert asyncio.run(review(old))[0]["event"] == "review_legacy_skipped"
    assert store.get_auto_reviews() == []
    new = store.create_sim_trade("account", "AAPL", "sell", 1, 100, 100,
                                 snapshot_id="s1", strategy_version="v1", strict=True)
    assert asyncio.run(review(new))[-1]["event"] == "review_done"
    record = store.get_auto_reviews()[0]
    assert (record["snapshot_id"], record["strategy_version"]) == ("s1", "v1")


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_executor_inherits_binding_to_trade(store, monkeypatch, side):
    from bottleneck_hunter.watchlist import trade_executor
    account = store.get_sim_account()
    if side == "sell":
        store.create_sim_position(account["id"], "AAPL", 1, 90)
    plan = store.create_execution_plan("tactical", "entry", "AAPL",
                                       {"action": side, "shares": 1, "target_price": 100},
                                       snapshot_id="s1", strategy_version="v1", strict=True)
    store.confirm_execution(plan)
    monkeypatch.setattr(type(store), "get_latest_snapshot", lambda *args: {"close": 100})
    monkeypatch.setattr(trade_executor, "_schedule_auto_review", lambda *args: None)
    result = trade_executor.execute_trade(store, plan)
    assert "error" not in result
    trade = row(store, "sim_trades", result["trade_id"])
    assert (trade["snapshot_id"], trade["strategy_version"]) == ("s1", "v1")
