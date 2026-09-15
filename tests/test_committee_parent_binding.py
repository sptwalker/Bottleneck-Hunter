import pytest
from bottleneck_hunter.watchlist import committee
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot
from bottleneck_hunter.watchlist.store import WatchlistStore

@pytest.mark.asyncio
async def test_parent_binding_checked_before_external_work(tmp_path, monkeypatch):
    s=WatchlistStore(db_path=tmp_path/'p.db').for_user('u').for_market('us_stock')
    b=save_stage_snapshot(s,'L4',{'x':1})
    good=s.create_execution_plan('','', 'AAPL', {'action':'buy'}, **b)
    bad=s.create_execution_plan('','', 'MSFT', {'action':'buy'}, strict=False)
    plans=[s.get_execution_plan(good),s.get_execution_plan(bad)]
    calls=[]
    monkeypatch.setattr(committee,'build_ticker_background',lambda *a: calls.append('bg') or {})
    async def evidence(*a): calls.append('ev'); return ''
    monkeypatch.setattr('bottleneck_hunter.chain.evidence.gather_evidence', evidence)
    async def review(*a): calls.append('llm'); return {'role':'x','provider':'x','model':'x','vote':'approve'}
    monkeypatch.setattr(committee,'_review_single',review)
    monkeypatch.setattr(committee,'_build_consensus',lambda *a: None)
    events=[e async for e in committee.run_committee_review(s,plans)]
    assert any(e['event']=='committee_error' and e['data']['plan_id']==bad for e in events)
    assert calls
