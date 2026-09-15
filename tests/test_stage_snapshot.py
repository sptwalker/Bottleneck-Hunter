import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from bottleneck_hunter.watchlist import decision_engine as engine
from bottleneck_hunter.watchlist.research_contracts import ResearchSnapshot, StageInputCapture
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_negotiation_captures_actual_retry_prompts(monkeypatch, fallback):
    llm = Mock()
    llm.invoke.side_effect = [Mock(content="invalid"), Mock(content='{"ok":true}')]

    async def negotiate(ask, prompt, **kwargs):
        if fallback:
            raise RuntimeError("negotiation unavailable")
        return await ask(prompt), [], None

    monkeypatch.setattr(engine.ai_tools, "negotiate", negotiate)
    prompts = []
    result, _ = await engine._run_data_negotiation(
        llm, "initial", market="us_stock", layer="4", allowed_tickers=[], input_prompts=prompts)
    assert result == {"ok": True}
    assert prompts == [call.args[0] for call in llm.invoke.call_args_list]
    assert len(prompts) == 2
    assert prompts[0] == "initial" and "上一次输出无法解析" in prompts[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("circuit_breaker", [False, True])
@pytest.mark.parametrize("capture_failure", [False, True])
async def test_l4_shared_batch_and_capture_before_writes(tmp_path, monkeypatch, circuit_breaker, capture_failure):
    from bottleneck_hunter.watchlist import constraint_validator as validator

    store = WatchlistStore(db_path=tmp_path / "batch.db").for_user("u").for_market("us_stock")
    macro = store.create_macro_strategy({"regime": "sideways"}, strict=False)
    strategic = store.create_strategic_plan(macro, {}, strict=False)
    for ticker in ("AAPL", "MSFT"):
        eid = store.add({"ticker": ticker, "market": "us_stock"})
        store.create_tactical_plan(strategic, eid, ticker, engine._today(), {"action": "sell"}, strict=False)
    monkeypatch.setattr(engine, "get_llm_for_position", lambda **kw: (Mock(), "test", "test"))
    monkeypatch.setattr(engine, "_decision_allowed_tickers", lambda *a: [])
    monkeypatch.setattr(engine, "_run_data_negotiation", AsyncMock(return_value=({"execution_plans": [
        {"ticker": "AAPL", "action": "sell", "shares": 1, "target_price": 99},
        {"ticker": "MSFT", "action": "buy" if circuit_breaker else "sell", "shares": 1, "target_price": 99},
    ]}, [])))
    monkeypatch.setattr(validator, "check_account_circuit_breaker", lambda *a: Mock(
        valid=not circuit_breaker, violations=["circuit"] if circuit_breaker else []))
    monkeypatch.setattr(validator, "validate_execution_plan", lambda ep, *a: Mock(
        valid=ep["ticker"] == "AAPL", violations=[] if ep["ticker"] == "AAPL" else ["invalid MSFT"]))
    monkeypatch.setattr(validator, "validate_portfolio_beta", lambda *a: Mock(valid=True))
    monkeypatch.setattr(validator, "validate_against_regime", lambda *a: Mock(valid=True))
    monkeypatch.setattr(validator, "max_compliant_shares", lambda *a: 0)
    monkeypatch.setattr(engine, "_repair_execution_plan", lambda *a: None)
    save = Mock(side_effect=ValueError("capture failed") if capture_failure else save_stage_snapshot)
    monkeypatch.setattr(engine, "save_stage_snapshot", save)
    events = [e async for e in engine.run_execution_plans(store)]
    assert save.call_count == 1
    with store._connect() as conn:
        rows = conn.execute("SELECT snapshot_id FROM execution_plans").fetchall()
    if capture_failure:
        assert any(e["event"] == "decision_error" for e in events)
        assert rows == []
        assert store.get_rejection_patterns() == []
    else:
        assert not any(e["event"] == "decision_error" for e in events), events
        assert len(rows) == 2 and len({r[0] for r in rows}) == 1
        snapshot = store.get_research_snapshot(rows[0][0])
        repairs = json.loads(snapshot.captures[0].payload_json)["repair_inputs"]
        assert len(repairs) == (0 if circuit_breaker else 1)
        if repairs:
            assert repairs[0]["plan"]["ticker"] == "MSFT"
            assert repairs[0]["violations"] == ["invalid MSFT"]


def test_stage_capture_canonical_and_rejects_duplicates():
    c = StageInputCapture(
        stage="L1", captured_at=datetime.now(timezone.utc), run_id="r", payload_json='{"b": 2, "a": 1}')
    assert c.payload_json == '{"a":1,"b":2}'
    assert c.semantics == "current_context"
    assert c.historical_visibility == "unknown"
    with pytest.raises(ValueError):
        StageInputCapture(stage="L1", captured_at=datetime.now(timezone.utc), run_id="r", payload_json='{"a":1,"a":2}')
    with pytest.raises(ValueError):
        StageInputCapture(stage="L1", captured_at=datetime.now(timezone.utc), run_id="r", payload_json='{"a":NaN}')


def test_persistence_roundtrip_and_legacy(tmp_path):
    store = WatchlistStore(db_path=tmp_path / "stage.db").for_user("u").for_market("us_stock")
    binding = save_stage_snapshot(store, "L3", {"tickers": ["AAPL", "MSFT"], "close": 1.25})
    loaded = store.get_research_snapshot(binding["snapshot_id"])
    assert json.loads(loaded.captures[0].payload_json)["tickers"] == ["AAPL", "MSFT"]
    second = save_stage_snapshot(store, "L3", {"tickers": ["AAPL", "MSFT"]})
    assert second["snapshot_id"] != binding["snapshot_id"]
    assert second["strategy_version"] == binding["strategy_version"]
    legacy = loaded.model_dump(mode="json", exclude={"captures"})
    legacy["snapshot_id"] = "legacy"
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO research_snapshots VALUES (?,?,?,?,?,?,?)",
            ("legacy", "u", "us_stock", loaded.strategy_version,
             loaded.as_of.isoformat(), loaded.created_at.isoformat(), json.dumps(legacy)),
        )
    assert store.get_research_snapshot("legacy").captures == ()
    assert ResearchSnapshot.model_validate(legacy).captures == ()


@pytest.mark.asyncio
async def test_l3_shared_rerun_and_fail_before_delete(tmp_path, monkeypatch):
    store = WatchlistStore(db_path=tmp_path / "engine.db").for_user("u").for_market("us_stock")
    macro = store.create_macro_strategy({"regime": "sideways"}, strict=False)
    store.create_strategic_plan(macro, {"stock_selection": {}}, strict=False)
    for ticker in ("AAPL", "MSFT"):
        store.add({"ticker": ticker, "market": "us_stock"})
    monkeypatch.setattr(engine, "get_llm_for_position", lambda **kw: (Mock(), "test", "test"))
    monkeypatch.setattr(engine, "_collect_watchlist_signals", lambda *a: [])
    monkeypatch.setattr(engine, "_chip_context", lambda *a: {})
    monkeypatch.setattr(engine, "_decision_allowed_tickers", lambda *a: [])
    monkeypatch.setattr(engine, "_run_data_negotiation", AsyncMock(return_value=(
        {"tactical_plans": [{"ticker": "AAPL", "action": "buy"}, {"ticker": "MSFT", "action": "buy"}]}, [])))
    events = [event async for event in engine.run_tactical_plans(store)]
    assert not [e for e in events if e["event"] == "decision_error"], events
    first = store.get_tactical_plans_by_date(engine._today())
    assert len(first) == 2
    assert len({p["snapshot_id"] for p in first}) == 1
    events = [event async for event in engine.run_tactical_plans(store)]
    second = store.get_tactical_plans_by_date(engine._today())
    assert first[0]["snapshot_id"] != second[0]["snapshot_id"]
    monkeypatch.setattr(engine, "save_stage_snapshot", Mock(side_effect=ValueError("capture failed")))
    events = [event async for event in engine.run_tactical_plans(store)]
    assert any(e["event"] == "decision_error" for e in events)
    assert {p["id"] for p in store.get_tactical_plans_by_date(engine._today())} == {p["id"] for p in second}


@pytest.mark.asyncio
async def test_l4_archives_post_reasoning_risk_inputs(tmp_path, monkeypatch):
    store = WatchlistStore(db_path=tmp_path / "l4.db").for_user("u").for_market("us_stock")
    eid = store.add({"ticker": "AAPL", "market": "us_stock"})
    macro = store.create_macro_strategy({"regime": "sideways", "risk_appetite": "balanced"}, strict=False)
    strategic = store.create_strategic_plan(macro, {}, strict=False)
    store.create_tactical_plan(strategic, eid, "AAPL", engine._today(), {"action": "buy"}, strict=False)
    monkeypatch.setattr(engine, "get_llm_for_position", lambda **kw: (Mock(), "test", "test"))
    monkeypatch.setattr(engine, "_decision_allowed_tickers", lambda *a: [])
    async def reason(*args, **kwargs):
        store.save_snapshots([{"ticker": "AAPL", "date": engine._today(), "close": 99.0}])
        return {"execution_plans": [{"ticker": "AAPL", "action": "buy", "shares": 100,
                                     "target_price": 99.0}]}, []
    monkeypatch.setattr(engine, "_run_data_negotiation", reason)
    monkeypatch.setattr(engine, "_repair_execution_plan", lambda *args: None)
    events = [e async for e in engine.run_execution_plans(store)]
    assert not [e for e in events if e["event"] == "decision_error"], events
    with store._connect() as conn:
        rows = conn.execute("SELECT payload_json FROM research_snapshots").fetchall()
    assert rows
    payload = json.loads(json.loads(rows[-1][0])["captures"][0]["payload_json"])
    assert payload["risk_snapshots"]["AAPL"][0]["close"] == 99.0
    assert "beta_map" in payload and "constraints" in payload and "positions" in payload


@pytest.mark.asyncio
async def test_committee_independent_snapshot_and_lineage(tmp_path, monkeypatch):
    from bottleneck_hunter.chain import evidence
    from bottleneck_hunter.watchlist import committee
    store = WatchlistStore(db_path=tmp_path / "committee.db").for_user("u").for_market("us_stock")
    parent = save_stage_snapshot(store, "L4", {"input": "execution"})
    eid = store.add({"ticker": "AAPL", "market": "us_stock"})
    plan_id = store.create_execution_plan("", eid, "AAPL", {"action": "sell", "shares": 1}, **parent)
    monkeypatch.setattr(committee, "build_ticker_background", lambda *args: {"valuation_data": "test"})
    monkeypatch.setattr(evidence, "gather_evidence", AsyncMock(return_value="new broker evidence"))
    monkeypatch.setattr(engine, "_portfolio_risk_summary", lambda *args: {})
    async def review(member, *args):
        return {"role": member["role"], "provider": member["role"], "model": "test",
                "vote": "approve", "confidence": 8}
    monkeypatch.setattr(committee, "_review_single", review)
    monkeypatch.setattr(committee, "_build_consensus", AsyncMock(return_value={"final_verdict": "approved"}))
    monkeypatch.setattr(committee, "_member_weights", lambda *args: {})
    events = [e async for e in committee.run_committee_review(store, [store.get_execution_plan(plan_id)])]
    assert any(e["event"] == "committee_plan_done" for e in events)
    with store._connect() as conn:
        reviews = conn.execute("SELECT snapshot_id FROM committee_reviews").fetchall()
        meetings = conn.execute("SELECT snapshot_id FROM meeting_records").fetchall()
    assert reviews and meetings
    snapshot = store.get_research_snapshot(reviews[0][0])
    assert snapshot.snapshot_id != parent["snapshot_id"]
    payload = json.loads(snapshot.captures[0].payload_json)
    assert payload["parent_snapshot_id"] == parent["snapshot_id"]
    assert payload["context"]["research_evidence"] == "new broker evidence"
    consensus = store.get_research_snapshot(meetings[0][0])
    assert json.loads(consensus.captures[0].payload_json)["parent_snapshot_id"] == snapshot.snapshot_id

