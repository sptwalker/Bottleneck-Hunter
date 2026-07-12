"""Phase1/2 从 DB 回读的回退逻辑（容器重启/TTL过期/LRU淘汰后仍可续跑）。"""

from __future__ import annotations

from bottleneck_hunter.web import phase_cache
from bottleneck_hunter.web.phase_rehydrate import load_phase1_from_db, load_phase2_from_db


def _record():
    return {
        "market": "us_stock", "max_market_cap_yi": 200, "top_n": 5,
        "sector": "GPU", "end_product": "GPU",
        "result_json": {
            "chain": {"nodes": []},
            "bottleneck_reports": [{"overall_score": 8.0}, {"overall_score": 5.0}, {"overall_score": 9.0}],
            "supplier_scorecards": [{"supplier": {"ticker": "AAA"}}, {"supplier": {"ticker": "BBB"}}],
        },
    }


class _Store:
    def get(self, analysis_id):
        return _record()


def test_phase1_rebuilds_and_caches():
    aid = "test-p1-rebuild"
    phase_cache.clear(aid)
    p1 = load_phase1_from_db(_Store(), aid)
    assert p1 is not None
    assert p1["chain"] == {"nodes": []} and len(p1["all_reports"]) == 3
    assert p1["config"]["market"] == "us_stock" and p1["config"]["max_market_cap_yi"] == 200
    assert [r["overall_score"] for r in p1["top_reports"]] == [9.0, 8.0, 5.0]  # 降序取 top_n
    assert phase_cache.get_phase(aid, 1) is not None
    phase_cache.clear(aid)


def test_phase2_rebuilds_and_caches():
    aid = "test-p2-rebuild"
    phase_cache.clear(aid)
    p2 = load_phase2_from_db(_Store(), aid)
    assert p2 is not None
    assert len(p2["scorecards"]) == 2 and p2["config"]["market"] == "us_stock"
    assert p2["stats"]["after_filter"] == 2
    assert phase_cache.get_phase(aid, 2) is not None
    phase_cache.clear(aid)


def test_missing_and_partial_return_none():
    class _Empty:
        def get(self, analysis_id):
            return None

    class _NoScorecards:  # 有记录但 result_json 缺 supplier_scorecards
        def get(self, analysis_id):
            return {"result_json": {"chain": {"nodes": []}, "bottleneck_reports": [{"overall_score": 1}]}}

    # phase1
    assert load_phase1_from_db(_Empty(), "x") is None
    assert load_phase1_from_db(None, "x") is None
    assert load_phase1_from_db(_Store(), "") is None
    # phase2
    assert load_phase2_from_db(_Empty(), "x") is None
    assert load_phase2_from_db(_NoScorecards(), "x") is None   # 缺 scorecards → None
    assert load_phase2_from_db(None, "x") is None
