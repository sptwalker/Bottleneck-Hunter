"""P0-3 Store 绑定专项；测试输入显式封存，不代表生产采集接入。"""

import sqlite3
from contextlib import closing

import pytest

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    scoped = WatchlistStore(db_path=tmp_path / "binding.db").for_user("alice").for_market("us_stock")
    time = "2026-02-03T00:00:00+00:00"
    observation = SourceObservation(
        observation_id="o1", metric="close", ticker="AAPL", market="us_stock", value=100,
        time=dict(period_start=time, period_end=time, effective_at=time, visible_at=time, collected_at=time),
        provenance=dict(source="test", unit="USD"),
    )
    scoped.save_research_snapshot(ResearchSnapshot(
        snapshot_id="s1", market="us_stock", strategy_version="v1", as_of=time, created_at=time,
        observations=(observation,),
    ))
    return scoped


CASES = [
    ("macro_strategies", "create_macro_strategy", ({},)),
    ("strategic_plans", "create_strategic_plan", ("macro", {})),
    ("tactical_plans", "create_tactical_plan", ("strategic", "entry", "AAPL", "2026-02-03", {})),
    ("execution_plans", "create_execution_plan", ("tactical", "entry", "AAPL", {})),
    ("execution_plans", "create_blocked_execution", ("tactical", "entry", "AAPL", {}, "blocked")),
    ("committee_reviews", "create_committee_review", ("execution", "risk", "test", "test", {})),
    ("committee_consensus", "create_committee_consensus", ("execution", {})),
    ("trade_feedback", "create_trade_feedback", ("execution", "AAPL")),
]


@pytest.mark.parametrize("table,method,args", CASES)
def test_roundtrip_and_legacy(store, table, method, args):
    create = getattr(store, method)
    old = create(*args, strict=False)
    new = create(*args, snapshot_id="s1", strategy_version="v1", strict=True)
    with closing(store._connect()) as conn:
        for sid, expected in [(old, (None, None)), (new, ("s1", "v1"))]:
            row = conn.execute(f"SELECT snapshot_id,strategy_version FROM {table} WHERE id=?", (sid,)).fetchone()
            assert tuple(row) == expected
    if method == "create_blocked_execution":
        assert store.get_execution_plan(new)["status"] == "rejected"
    if method == "create_macro_strategy":
        assert store.get_macro_history()[0]["snapshot_id"] == "s1"
        assert store.get_latest_macro_strategy()["strategy_version"] == "v1"
    if method == "create_strategic_plan":
        assert store.get_strategic_history()[0]["snapshot_id"] == "s1"
        assert store.get_latest_strategic_plan()["strategy_version"] == "v1"


@pytest.mark.parametrize("table,method,args", CASES)
@pytest.mark.parametrize("binding", [
    {}, {"snapshot_id": "s1"}, {"strategy_version": "v1"},
    {"snapshot_id": " ", "strategy_version": "v1"},
    {"snapshot_id": "s1", "strategy_version": "\t"},
    {"snapshot_id": 123, "strategy_version": "v1"},
    {"snapshot_id": "s1", "strategy_version": True},
    {"snapshot_id": "missing", "strategy_version": "v1"},
    {"snapshot_id": "s1", "strategy_version": "wrong"},
])
def test_invalid_before_any_side_effect(store, table, method, args, binding):
    getattr(store, method)(*args, strict=False)
    with closing(store._connect()) as conn:
        before = conn.execute(f"SELECT * FROM {table}").fetchall()
        feedback = conn.execute("SELECT * FROM trade_feedback").fetchall()
    with pytest.raises(ValueError):
        getattr(store, method)(*args, strict=True, **binding)
    with closing(store._connect()) as conn:
        assert conn.execute(f"SELECT * FROM {table}").fetchall() == before
        assert conn.execute("SELECT * FROM trade_feedback").fetchall() == feedback


@pytest.mark.parametrize("table,method,args", CASES)
@pytest.mark.parametrize("user,market", [("bob", "us_stock"), ("alice", "a_stock"), ("", "us_stock"), ("alice", "")])
def test_binding_scope_fails_closed(store, table, method, args, user, market):
    other = store.for_user(user).for_market(market)
    with pytest.raises(ValueError):
        getattr(other, method)(*args, snapshot_id="s1", strategy_version="v1", strict=True)
    with closing(store._connect()) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize("table,method,args", CASES)
def test_explicit_binding_validated_without_strict(store, table, method, args):
    with pytest.raises(ValueError):
        getattr(store, method)(*args, snapshot_id="missing", strategy_version="v1")


def test_old_database_migration_twice(tmp_path):
    path = tmp_path / "old.db"
    tables = [case[0] for case in CASES[:4]] + [
        "committee_consensus", "committee_reviews", "trade_feedback", "sim_trades", "auto_reviews", "meeting_records",
    ]
    index_names = ["macro", "strategic", "tactical", "execution", "consensus"] + tables[5:]
    store = WatchlistStore(db_path=path)
    sid = store.create_macro_strategy({"market_summary": "old"}, strict=False)
    with closing(sqlite3.connect(path)) as conn:
        for table, index in zip(tables, index_names, strict=True):
            conn.execute(f"DROP INDEX idx_{index}_snapshot_binding")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN snapshot_id")
            conn.execute(f"ALTER TABLE {table} DROP COLUMN strategy_version")
        conn.commit()
    for _ in range(2):
        migrated = WatchlistStore(db_path=path)
        row = migrated.get_latest_macro_strategy()
        assert row["id"] == sid and row["market_summary"] == "old"
        assert row["snapshot_id"] is None and row["strategy_version"] is None
        with closing(migrated._connect()) as conn:
            for table in tables:
                columns = {r["name"]: r for r in conn.execute(f"PRAGMA table_info({table})")}
                assert columns["snapshot_id"]["notnull"] == 0
                assert columns["strategy_version"]["notnull"] == 0
            indexes = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%snapshot_binding'"
            )
            assert {row["name"] for row in indexes} == {f"idx_{name}_snapshot_binding" for name in index_names}
