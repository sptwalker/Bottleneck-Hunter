"""P1-⑤ 个股 13F 季度环比：_holder_qoq 两季共同机构净增减 + latest_only 防跨季混读 + _chip_context 注入。

- _holder_qoq：两季 fake → 正确 direction/net + added/trimmed 名单(仅两季共同机构，进出榜的不算)。
- <2 季 或 无共同机构 → None(诚实降级，不编方向)。
- latest_only=True：多期库下只吐最新季持有人，同一机构不跨季重复；默认 False 仍多季(供 _positioning_signals)。
- _chip_context：含 institutional_qoq 且 top_institutions/institution_count 只计最新季(不被上季污染)。
"""
from bottleneck_hunter.watchlist.decision_engine import _chip_context, _holder_qoq
from bottleneck_hunter.watchlist.store import WatchlistStore

Q1 = "2025-03-31"
Q2 = "2025-06-30"


def _store(tmp_path, name="qoq.db"):
    return WatchlistStore(str(tmp_path / name)).for_user("u1").for_market("us_stock")


def _seed(store, ticker, rows):
    """rows: list[(holder, shares, pct, date)] → 经真实 save 路径落库(UNIQUE(ticker,holder,date) 多季共存)。"""
    holders = [{"holder_name": h, "shares": s, "pct_held": p, "date": d, "value": 0.0,
                "fetched_at": "2025-07-01T00:00:00+00:00"} for h, s, p, d in rows]
    store.save_institutional_holders(ticker, holders)


def _two_quarter_seed(store, ticker="NVDA"):
    # Q2 最新季 / Q1 上季；Fidelity 本季退出、StateStreet 本季新进 → 二者非共同机构，不计方向
    _seed(store, ticker, [
        ("BlackRock", 1000, 5.0, Q2), ("Vanguard", 500, 3.0, Q2), ("StateStreet", 300, 2.0, Q2),
        ("BlackRock", 800, 4.5, Q1), ("Vanguard", 600, 3.5, Q1), ("Fidelity", 200, 1.5, Q1),
    ])


def test_holder_qoq_two_quarters(tmp_path):
    s = _store(tmp_path)
    _two_quarter_seed(s)
    qoq = _holder_qoq(s, "NVDA")
    assert qoq is not None
    assert qoq["cur_quarter"] == Q2 and qoq["prev_quarter"] == Q1
    # 共同机构 = BlackRock(+200) / Vanguard(-100) → net=+100 → 净增持
    assert qoq["net_shares"] == 100
    assert qoq["direction"] == "净增持"
    assert qoq["added_holders"] == ["BlackRock"]
    assert qoq["trimmed_holders"] == ["Vanguard"]
    assert qoq["common_holders"] == 2       # StateStreet/Fidelity 非共同机构，未计入


def test_holder_qoq_single_quarter_is_none(tmp_path):
    s = _store(tmp_path)
    _seed(s, "AAA", [("BlackRock", 1000, 5.0, Q2)])
    assert _holder_qoq(s, "AAA") is None    # <2 季 → 诚实降级


def test_holder_qoq_no_common_holder_is_none(tmp_path):
    """两季但机构完全不重叠(全换手) → 无共同机构 → None，不硬编方向。"""
    s = _store(tmp_path)
    _seed(s, "BBB", [("A基金", 100, 1.0, Q2), ("B基金", 200, 2.0, Q1)])
    assert _holder_qoq(s, "BBB") is None


def test_latest_only_no_cross_quarter_mix(tmp_path):
    s = _store(tmp_path)
    _two_quarter_seed(s)
    latest = s.get_institutional_holders("NVDA", limit=50, latest_only=True)
    assert {r["date"] for r in latest} == {Q2}          # 只最新季
    assert len(latest) == 3                              # Q2 三家
    assert [r["holder_name"] for r in latest].count("BlackRock") == 1   # 同机构不跨季重复
    # 默认(多季)仍返回全部 6 行，供 _positioning_signals / _holder_qoq 用
    assert len(s.get_institutional_holders("NVDA", limit=200)) == 6


def test_chip_context_injects_qoq_and_latest_only(tmp_path):
    s = _store(tmp_path)
    _two_quarter_seed(s)
    chip = _chip_context(s, "NVDA")
    assert "institutional_qoq" in chip
    assert chip["institutional_qoq"]["direction"] == "净增持"
    # institution_count 只计最新季 3 家，不因上季 Fidelity 混读成 4+
    assert chip["institution_count"] == 3
    assert {i["name"] for i in chip["top_institutions"]} == {"BlackRock", "Vanguard", "StateStreet"}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
