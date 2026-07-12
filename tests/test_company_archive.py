"""企业持久化档案：评选/入围/反查过的企业按 (user_id, ticker) 存最新评分卡，供各处按 ticker 调用。"""

from __future__ import annotations

from bottleneck_hunter.dataflows.store import AnalysisStore


def _sc(ticker, score):
    return {"supplier": {"ticker": ticker, "name": ticker}, "overall_score": score}


def test_archive_roundtrip_and_overwrite(tmp_path):
    s = AnalysisStore(str(tmp_path / "a.db")).for_user("u1")
    n = s.upsert_company_archives([
        {"ticker": "AAPL", "market": "us_stock", "name": "Apple", "scorecard": _sc("AAPL", 8.5), "source": "phase2"},
        {"ticker": "NVDA", "market": "us_stock", "name": "Nvidia", "scorecard": _sc("NVDA", 9.1), "source": "phase2"},
    ])
    assert n == 2
    assert s.get_company_archive("AAPL")["scorecard"]["overall_score"] == 8.5
    assert s.get_company_archive("NVDA")["scorecard"]["overall_score"] == 9.1
    # 同 ticker 再 upsert → 覆盖为最新
    s.upsert_company_archive("AAPL", _sc("AAPL", 7.0), market="us_stock", name="Apple", source="reverse")
    a = s.get_company_archive("AAPL")
    assert a["scorecard"]["overall_score"] == 7.0 and a["source"] == "reverse"


def test_archive_user_isolation_and_missing(tmp_path):
    dbp = str(tmp_path / "a.db")
    AnalysisStore(dbp).for_user("u1").upsert_company_archive("AAPL", _sc("AAPL", 8.0))
    assert AnalysisStore(dbp).for_user("u2").get_company_archive("AAPL") is None   # 按用户隔离
    assert AnalysisStore(dbp).for_user("u1").get_company_archive("ZZZZ") is None    # 无档案
    # 空 ticker / 空 scorecard 不写
    assert AnalysisStore(dbp).for_user("u1").upsert_company_archives([{"ticker": "", "scorecard": _sc("x", 1)}]) == 0
    assert AnalysisStore(dbp).for_user("u1").upsert_company_archives([{"ticker": "X", "scorecard": None}]) == 0


def test_backfill_once_and_no_overwrite(tmp_path):
    from types import SimpleNamespace
    dbp = str(tmp_path / "a.db")
    s = AnalysisStore(dbp).for_user("u1")
    cfg = SimpleNamespace(sector="GPU", end_product="GPU", provider="p", model="m",
                          market="us_stock", max_depth=4, top_n=5, max_market_cap_yi=200, language="zh")
    s.save(cfg, {"sector": "GPU", "chain": {}, "bottleneck_reports": [],
                 "supplier_scorecards": [_sc("AAPL", 6.0), _sc("NVDA", 9.0)]})
    # 已有 AAPL 新档(评分8.5)——回填不应覆盖它
    s.upsert_company_archive("AAPL", _sc("AAPL", 8.5), source="phase2")

    n = AnalysisStore(dbp).backfill_company_archive()   # 全局回填
    assert n == 1                                        # 只补缺的 NVDA（AAPL 已有）
    assert s.get_company_archive("AAPL")["scorecard"]["overall_score"] == 8.5   # 未被覆盖
    assert s.get_company_archive("NVDA")["scorecard"]["overall_score"] == 9.0   # 补建
    # 幂等：再跑一次不再补
    assert AnalysisStore(dbp).backfill_company_archive() == 0
