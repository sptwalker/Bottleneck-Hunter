"""会议快照独立绑定，按会议实际用户和市场校验。"""

from contextlib import closing

import pytest

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.fixture
def store(tmp_path):
    scoped = WatchlistStore(db_path=tmp_path / "meeting.db").for_user("alice").for_market("us_stock")
    time = "2026-02-03T00:00:00+00:00"
    for snapshot_id in ("execution", "committee"):
        scoped.save_research_snapshot(ResearchSnapshot(
            snapshot_id=snapshot_id, market="us_stock", strategy_version="v1", as_of=time, created_at=time,
            observations=(SourceObservation(
                observation_id="o1", metric="close", ticker="AAPL", market="us_stock", value=100,
                time=dict(period_start=time, period_end=time, effective_at=time, visible_at=time, collected_at=time),
                provenance=dict(source="test", unit="USD"),
            ),),
        ))
    return scoped


def test_meeting_independent_binding_and_legacy(store):
    plan = store.create_execution_plan(
        "tactical", "entry", "AAPL", {}, snapshot_id="execution", strategy_version="v1", strict=True,
    )
    meeting = store.create_meeting_record(
        meeting_type="committee", title="投委会", execution_plan_id=plan,
        snapshot_id="committee", strategy_version="v1", strict=True,
    )
    bound = store.get_meeting_record(meeting)
    assert (bound["snapshot_id"], bound["strategy_version"], bound["execution_plan_id"]) == ("committee", "v1", plan)
    legacy = store.create_meeting_record(meeting_type="macro", title="旧调用", strict=False)
    row = store.get_meeting_record(legacy)
    assert (row["snapshot_id"], row["strategy_version"]) == (None, None)


@pytest.mark.parametrize("binding", [
    {"strict": True},
    {"snapshot_id": "committee"},
    {"strategy_version": "v1"},
    {"snapshot_id": "", "strategy_version": "v1"},
    {"snapshot_id": "committee", "strategy_version": " "},
    {"snapshot_id": "missing", "strategy_version": "v1"},
    {"snapshot_id": "committee", "strategy_version": "v2"},
])
def test_invalid_binding_has_no_write(store, binding):
    with pytest.raises(ValueError):
        store.create_meeting_record(meeting_type="committee", title="无效绑定", **binding)
    assert store.get_meeting_records() == []


@pytest.mark.parametrize("user,scope,market", [
    ("bob", "us_stock", ""),
    ("alice", "a_stock", ""),
    ("alice", "us_stock", "a_stock"),
    ("", "us_stock", ""),
])
def test_meeting_binding_rejects_wrong_scope(store, user, scope, market):
    with pytest.raises(ValueError):
        store.for_user(user).for_market(scope).create_meeting_record(
            meeting_type="committee", title="隔离", market=market,
            snapshot_id="committee", strategy_version="v1",
        )
    assert store.get_meeting_records() == []


@pytest.mark.parametrize("scope,market", [("a_stock", "us_stock"), ("", ""), ("us_stock", "")])
def test_meeting_binding_uses_effective_market(store, scope, market):
    meeting = store.for_market(scope).create_meeting_record(
        meeting_type="committee", title="实际市场", market=market,
        snapshot_id="committee", strategy_version="v1",
    )
    row = store.get_meeting_record(meeting)
    assert (row["market"], row["snapshot_id"]) == ("us_stock", "committee")


def test_meeting_migration_preserves_legacy_rows_and_is_repeatable(store):
    legacy = store.create_meeting_record(meeting_type="macro", title="迁移前会议", strict=False)
    with closing(store._connect()) as conn:
        conn.execute("DROP INDEX idx_meeting_records_snapshot_binding")
        conn.execute("ALTER TABLE meeting_records DROP COLUMN snapshot_id")
        conn.execute("ALTER TABLE meeting_records DROP COLUMN strategy_version")
        conn.commit()
    for _ in range(2):
        migrated = WatchlistStore(db_path=store._db_path).for_user("alice").for_market("us_stock")
        row = migrated.get_meeting_record(legacy)
        assert (row["title"], row["snapshot_id"], row["strategy_version"]) == ("迁移前会议", None, None)
        with closing(migrated._connect()) as conn:
            columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(meeting_records)")}
            assert columns["snapshot_id"]["notnull"] == columns["strategy_version"]["notnull"] == 0
            index = conn.execute("PRAGMA index_info(idx_meeting_records_snapshot_binding)").fetchall()
            assert [row["name"] for row in index] == ["snapshot_id", "strategy_version"]
