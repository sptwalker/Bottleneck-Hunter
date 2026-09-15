import sqlite3

import pytest

from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore


def test_blocked_atomic_and_bound(tmp_path):
    store = WatchlistStore(db_path=tmp_path / "blocked.db").for_user("alice").for_market("us_stock")
    binding = save_stage_snapshot(store, "L4", {"input": "execution"})
    with store._connect() as conn:
        conn.execute("CREATE TRIGGER fail_blocked_feedback BEFORE INSERT ON trade_feedback BEGIN SELECT RAISE(ABORT, 'injected'); END")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.create_blocked_execution("", "", "AAPL", {"action": "buy"}, "blocked", **binding)
    assert store.get_blocked_executions() == []

    with store._connect() as conn:
        conn.execute("DROP TRIGGER fail_blocked_feedback")
        conn.commit()
    plan_id = store.create_blocked_execution("", "", "AAPL", {"action": "buy"}, "blocked", **binding)
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT snapshot_id, strategy_version FROM execution_plans WHERE id = ? "
            "UNION ALL SELECT snapshot_id, strategy_version FROM trade_feedback WHERE execution_plan_id = ?",
            (plan_id, plan_id),
        ).fetchall()
    assert len(rows) == 2
    assert {(row[0], row[1]) for row in rows} == {(binding["snapshot_id"], binding["strategy_version"])}
