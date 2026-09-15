import json
from bottleneck_hunter.watchlist.stage_snapshot import save_stage_snapshot

def test_redacts_nested_without_mutation(tmp_path):
    from bottleneck_hunter.watchlist.store import WatchlistStore
    s=WatchlistStore(db_path=tmp_path/'s.db').for_user('u').for_market('us_stock')
    inputs={'api_key':'TOP','nested':[{'access-token':'A','ok':1}], 'prompt':'keep token words', 'json':'{"password":"P","x":2}'}
    original=json.loads(json.dumps(inputs))
    b=save_stage_snapshot(s,'L4',inputs)
    assert inputs==original
    payload=json.loads(s.get_research_snapshot(b['snapshot_id']).captures[0].payload_json)
    assert payload['api_key']=='[REDACTED]' and payload['nested'][0]['access-token']=='[REDACTED]'
    assert payload['json'] == '{"password":"[REDACTED]","x":2}'
    assert payload['prompt']=='keep token words'
