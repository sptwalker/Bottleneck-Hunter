"""P0-1 数据契约测试。"""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from bottleneck_hunter.watchlist.data_contracts import PointInTimeObservation
from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot
from bottleneck_hunter.watchlist.store import WatchlistStore


def _observation(**overrides):
    data = {
        "ticker": "AAPL",
        "market": "us_stock",
        "value": 123.4,
        "time": {
            "period_start": "2026-01-01T00:00:00+08:00",
            "period_end": "2026-01-31T23:59:59+08:00",
            "effective_at": "2026-02-01T00:00:00+08:00",
            "visible_at": "2026-02-02T00:00:00+08:00",
            "collected_at": "2026-02-03T00:00:00+08:00",
        },
        "provenance": {"source": "vendor", "unit": "USD", "currency": "USD"},
    }
    data.update(overrides)
    return data


def _snapshot(snapshot_id="s1", market="us_stock"):
    return ResearchSnapshot.model_validate({
        "snapshot_id": snapshot_id, "market": market, "strategy_version": "v1",
        "as_of": "2026-02-03T00:00:00Z", "created_at": "2026-02-03T01:00:00Z",
        "observations": [{**_observation(), "observation_id": "o1", "metric": "close", "market": market}],
    })


def test_snapshot_store_isolated_immutable_and_idempotent(tmp_path):
    base = WatchlistStore(tmp_path / "research.db")
    a = base.for_user("a").for_market("us_stock")
    b = base.for_user("b").for_market("us_stock")
    a.save_research_snapshot(_snapshot())
    assert a.get_research_snapshot("s1") is not None
    assert b.get_research_snapshot("s1") is None
    with pytest.raises(ValueError, match="重复"):
        a.save_research_snapshot(_snapshot())
    with pytest.raises(sqlite3.IntegrityError), closing(a._connect()) as conn:
        conn.execute("UPDATE research_snapshots SET payload_json='x'")
    assert a.get_research_snapshot("s1") == _snapshot()


def test_snapshot_market_and_unbound_fail_closed(tmp_path):
    base = WatchlistStore(tmp_path / "research.db")
    with pytest.raises(ValueError):
        base.save_research_snapshot(_snapshot())
    with pytest.raises(ValueError, match="市场"):
        base.for_user("a").for_market("a_stock").save_research_snapshot(_snapshot())

    observation = PointInTimeObservation.model_validate(_observation())
    assert observation.time.visible_at.tzinfo == timezone.utc
    assert observation.time.visible_at.hour == 16
    assert observation.is_visible


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_observation_rejects_nonfinite(value):
    with pytest.raises(ValidationError):
        PointInTimeObservation.model_validate(_observation(value=value))


def test_observation_and_nested_contracts_are_frozen():
    observation = PointInTimeObservation.model_validate(_observation())
    for obj, field, value in (
        (observation, "value", 99.0),
        (observation.time, "visible_at", datetime.now(timezone.utc)),
        (observation.provenance, "source", "other"),
    ):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(obj, field, value)


def test_observation_rejects_naive_time():
    data = _observation()
    data["time"]["visible_at"] = datetime(2026, 2, 2)
    with pytest.raises(ValidationError, match="必须包含时区"):
        PointInTimeObservation.model_validate(data)


def test_observation_rejects_invalid_period_and_unknown_fields():
    data = _observation()
    data["time"]["period_end"] = "2025-12-31T00:00:00Z"
    data["unexpected"] = True
    with pytest.raises(ValidationError):
        PointInTimeObservation.model_validate(data)
