import pytest

from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore


def make_store(tmp_path):
    return WatchlistStore(db_path=tmp_path / "reject.db").for_user("alice").for_market("us_stock")


def make_plan(store, binding=None):
    return store.create_execution_plan(
        "", "", "AAPL", {"action": "sell", "shares": 1}, **(binding or {"strict": False}))


def test_reject_valid_binding_and_feedback(tmp_path):
    store = make_store(tmp_path)
    binding = save_stage_snapshot(store, "L4", {"x": 1})
    plan = make_plan(store, binding)
    assert store.reject_execution(plan, "no")
    assert store.get_execution_plan(plan)["status"] == "rejected"
    assert store.get_rejection_patterns("AAPL")[0]["snapshot_id"] == binding["snapshot_id"]


def test_reject_invalid_binding_has_no_side_effect(tmp_path):
    store = make_store(tmp_path)
    plan = make_plan(store)
    with store._connect() as conn:
        conn.execute(
            "UPDATE execution_plans SET snapshot_id = 'missing', strategy_version = 'v1' WHERE id = ?", (plan,))
        conn.commit()
    with pytest.raises(ValueError):
        store.reject_execution(plan, "bad")
    assert store.get_execution_plan(plan)["status"] == "pending"
    assert store.get_rejection_patterns("AAPL") == []


def test_reject_legacy_plan_can_retire(tmp_path):
    store = make_store(tmp_path)
    plan = make_plan(store)
    assert store.reject_execution(plan, "legacy")
    assert store.get_execution_plan(plan)["status"] == "rejected"
