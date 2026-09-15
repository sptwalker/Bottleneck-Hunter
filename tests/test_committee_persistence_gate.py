"""共识落库失败不得改变计划，且不得阻断下一条计划。"""

from unittest.mock import AsyncMock, Mock

import pytest

from bottleneck_hunter.chain import evidence
from bottleneck_hunter.watchlist import committee, decision_engine
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["rejected", "approved_with_modifications"])
async def test_consensus_failure_skips_gating(tmp_path, monkeypatch, verdict):
    store = WatchlistStore(db_path=tmp_path / "committee.db").for_user("alice").for_market("us_stock")
    binding = save_stage_snapshot(store, "L4", {"input": "execution"})
    plan_ids = [
        store.create_execution_plan("", "", ticker, {"action": "sell", "shares": 10}, **binding)
        for ticker in ("AAPL", "MSFT")
    ]
    plans = [store.get_execution_plan(plan_id) for plan_id in plan_ids]
    monkeypatch.setattr(committee, "build_ticker_background", lambda *args: {})
    monkeypatch.setattr(evidence, "gather_evidence", AsyncMock(return_value=""))
    monkeypatch.setattr(decision_engine, "_portfolio_risk_summary", lambda *args: {})
    monkeypatch.setattr(committee, "_member_weights", lambda *args: {})

    async def review(member, *args):
        return {"role": member["role"], "provider": member["role"], "model": "test",
                "vote": "approve", "confidence": 8}

    monkeypatch.setattr(committee, "_review_single", review)
    monkeypatch.setattr(committee, "_build_consensus", AsyncMock(side_effect=[
        {"final_verdict": verdict, "consensus_modifications": [{"field": "shares", "modified": 1}]},
        {"final_verdict": "approved"},
    ]))
    original_save = WatchlistStore.create_committee_consensus
    attempted = []

    def save(self, execution_plan_id, result_json, **kwargs):
        snapshot = self.get_research_snapshot(kwargs["snapshot_id"])
        assert kwargs["strict"] is True
        assert snapshot.strategy_version == kwargs["strategy_version"]
        assert snapshot.snapshot_id != binding["snapshot_id"]
        attempted.append(execution_plan_id)
        if execution_plan_id == plan_ids[0]:
            raise RuntimeError("injected persistence failure")
        return original_save(self, execution_plan_id, result_json, **kwargs)

    monkeypatch.setattr(WatchlistStore, "create_committee_consensus", save)
    reject = Mock()
    modify = Mock()
    monkeypatch.setattr(WatchlistStore, "reject_execution", reject)
    monkeypatch.setattr(WatchlistStore, "apply_committee_modifications", modify)
    events = [event async for event in committee.run_committee_review(store, plans)]

    assert attempted == plan_ids
    reject.assert_not_called()
    modify.assert_not_called()
    assert store.get_execution_plan(plan_ids[0]) == plans[0]
    assert any(e["event"] == "committee_error" and e["data"]["plan_id"] == plan_ids[0] for e in events)
    assert not any(e["event"] == "committee_gating" for e in events)
    done = [e["data"]["plan_id"] for e in events if e["event"] == "committee_plan_done"]
    assert done == [plan_ids[1]]
    with store._connect() as conn:
        saved = conn.execute("SELECT execution_plan_id FROM committee_consensus").fetchall()
    assert [row[0] for row in saved] == [plan_ids[1]]
