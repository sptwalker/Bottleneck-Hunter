"""P0-2 研究快照与来源观测持久化测试。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, SourceObservation
from bottleneck_hunter.watchlist.store import WatchlistStore


def _snapshot(snapshot_id="s-1", market="us_stock", observation_id="o-1"):
    time = {
        "period_start": "2026-01-01T00:00:00Z",
        "period_end": "2026-01-31T00:00:00Z",
        "effective_at": "2026-02-01T00:00:00Z",
        "visible_at": "2026-02-02T00:00:00Z",
        "collected_at": "2026-02-03T00:00:00Z",
    }
    observation = SourceObservation(
        observation_id=observation_id, metric="close", ticker="AAPL", market=market,
        value=1.0, time=time, provenance={"source": "test", "unit": "USD"},
    )
    return ResearchSnapshot(
        snapshot_id=snapshot_id, market=market, strategy_version="v1",
        as_of=datetime(2026, 2, 2, tzinfo=timezone.utc),
        created_at=datetime(2026, 2, 3, tzinfo=timezone.utc), observations=(observation,),
    )


def test_snapshot_roundtrip_and_isolation(tmp_path):
    db = tmp_path / "research.db"
    owner = WatchlistStore(db).for_user("u1").for_market("us_stock")
    owner.save_research_snapshot(_snapshot())
    loaded = owner.get_research_snapshot("s-1")
    assert loaded.snapshot_id == "s-1"
    assert owner.get_research_observations("s-1")[0].metric == "close"
    assert WatchlistStore(db).for_user("u2").for_market("us_stock").get_research_snapshot("s-1") is None
    assert WatchlistStore(db).for_user("u1").for_market("a_stock").get_research_snapshot("s-1") is None


def test_snapshot_is_immutable_and_duplicate_rejected(tmp_path):
    db = tmp_path / "research.db"
    store = WatchlistStore(db).for_user("u1").for_market("us_stock")
    store.save_research_snapshot(_snapshot())
    with pytest.raises(ValueError, match="重复写入"):
        store.save_research_snapshot(_snapshot())
    conn = store._connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE research_snapshots SET strategy_version='v2'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM research_snapshots")
    finally:
        conn.close()


def test_unbound_store_fails_closed(tmp_path):
    store = WatchlistStore(tmp_path / "research.db")
    with pytest.raises(ValueError, match="绑定用户"):
        store.save_research_snapshot(_snapshot())
    assert store.get_research_snapshot("s-1") is None
    assert store.get_research_observations("s-1") == ()
    with pytest.raises(ValueError, match="绑定市场"):
        store.for_user("u1").save_research_snapshot(_snapshot())


def test_reusable_sources_multiticker_period_source_and_order(tmp_path):
    store = WatchlistStore(tmp_path / "research.db").for_user("u1").for_market("us_stock")
    snapshot = _snapshot()
    original = snapshot.observations[0]
    other_ticker = original.model_copy(update={"observation_id": "o-2", "ticker": "MSFT"})
    other_source = original.model_copy(update={
        "observation_id": "o-3", "provenance": original.provenance.model_copy(update={"source": "vendor2"}),
    })
    other_period = original.model_copy(update={
        "observation_id": "o-4", "time": original.time.model_copy(update={"period_start": original.time.period_end}),
    })
    snapshot = snapshot.model_copy(update={"observations": (other_source, original, other_period, other_ticker)})
    store.save_research_snapshot(snapshot)
    second = snapshot.model_copy(update={"snapshot_id": "s-2", "observations": tuple(reversed(snapshot.observations))})
    store.save_research_snapshot(second)
    assert store.get_research_snapshot("s-1") == snapshot
    assert store.get_research_observations("s-2") == second.observations
    with closing(store._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM research_observations").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM research_snapshot_observations").fetchone()[0] == 8


def test_id_conflict_and_duplicate_roll_back_new_sources(tmp_path):
    store = WatchlistStore(tmp_path / "research.db").for_user("u1").for_market("us_stock")
    snapshot = _snapshot()
    store.save_research_snapshot(snapshot)
    fresh = snapshot.observations[0].model_copy(update={"observation_id": "fresh"})
    conflict = snapshot.observations[0].model_copy(update={"value": 999.0})
    for attempted in (
        snapshot.model_copy(update={"snapshot_id": "s-2", "observations": (fresh, conflict)}),
        snapshot.model_copy(update={"observations": (fresh,)}),
    ):
        with pytest.raises(ValueError):
            store.save_research_snapshot(attempted)
        with closing(store._connect()) as conn:
            assert conn.execute("SELECT count(*) FROM research_observations").fetchone()[0] == 1
    assert store.get_research_snapshot("s-2") is None
    assert store.get_research_snapshot("s-1") == snapshot


def test_same_ids_are_independent_across_users_and_markets(tmp_path):
    base = WatchlistStore(tmp_path / "research.db")
    for user, market in (("u1", "us_stock"), ("u2", "us_stock"), ("u1", "a_stock")):
        store = base.for_user(user).for_market(market)
        snapshot = _snapshot(market=market)
        store.save_research_snapshot(snapshot)
        assert store.get_research_snapshot(snapshot.snapshot_id) == snapshot
        assert store.get_research_observations(snapshot.snapshot_id) == snapshot.observations


@pytest.mark.parametrize("foreign_keys", [0, 1])
def test_database_guards_without_global_foreign_keys(tmp_path, foreign_keys):
    store = WatchlistStore(tmp_path / "research.db").for_user("u1").for_market("us_stock")
    snapshot = _snapshot()
    store.save_research_snapshot(snapshot)
    with closing(store._connect()) as conn:
        conn.execute(f"PRAGMA foreign_keys={foreign_keys}")
        # 包括 recursive_triggers 默认关闭时 OR REPLACE 绕过 DELETE 触发器的漏洞。
        for table in ("research_snapshots", "research_observations", "research_snapshot_observations"):
            for sql in (
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
                f"DELETE FROM {table}",
                f"UPDATE {table} SET user_id='other'",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    conn.execute(sql)
        for values in (
            ("u1", "us_stock", "s-1", "o-1", 1),
            ("u2", "us_stock", "s-1", "o-1", 0),
            ("u1", "a_stock", "s-1", "o-1", 0),
            ("u1", "us_stock", "absent", "o-1", 0),
            ("u1", "us_stock", "s-1", "absent", 0),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO research_snapshot_observations VALUES (?,?,?,?,?)", values)
    assert store.get_research_snapshot("s-1") == snapshot
    assert store.get_research_observations("s-1") == snapshot.observations


def test_read_connections_close_even_on_failure(tmp_path, monkeypatch):
    store = WatchlistStore(tmp_path / "research.db").for_user("u1").for_market("us_stock")
    store.save_research_snapshot(_snapshot())
    connect = store._connect
    connections = []

    def tracked():
        connection = connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", tracked)
    store.get_research_snapshot("s-1")
    store.get_research_observations("s-1")
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_bypassed_model_validation_is_rechecked(tmp_path):
    store = WatchlistStore(tmp_path / "research.db").for_user("u1").for_market("us_stock")
    snapshot = _snapshot()
    invalid = snapshot.model_copy(update={"observations": (
        snapshot.observations[0].model_copy(update={"value": float("nan")}),
    )})
    with pytest.raises(ValueError):
        store.save_research_snapshot(invalid)
    assert store.get_research_snapshot("s-1") is None
