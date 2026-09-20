"""Phase 3 评选名单 / Phase 4 交叉验证名单 / top_picks 必须同源。

回归背景（生产 #30）：同一批入围公司在三处被三条不同代码路径各排了一次名——
Phase 3 用「勾选子集 + quality^wq × alpha^wa」，top_picks 用「scorecards[:5] + overall_score ≥ 6」，
Phase 4 用「全量 + final_score」。三者结果天然不同，用户看到的就是
「交叉验证的公司列表和上一阶段筛选出来的不一样」，误以为记录互串。
"""

from __future__ import annotations

from types import SimpleNamespace

from bottleneck_hunter.chain.picks import (
    DEFAULT_MIN_SCORE,
    DEFAULT_TOP_N,
    canonical_picks,
    passed_top,
    picks_top_n_from_config,
    sort_key,
)
from bottleneck_hunter.dataflows.store import AnalysisStore


def _card(ticker: str, overall: float, final: float | None, fc: str = "PASS") -> dict:
    return {
        "supplier": {"ticker": ticker, "name": ticker},
        "overall_score": overall,
        "fact_check_recommendation": fc,
        "final": None if final is None else {"final_score": final},
    }


def _store_with_record(tmp_path, scorecards):
    """建一条分析记录（含 supplier_scorecards），返回 (store, analysis_id)。"""
    store = AnalysisStore(str(tmp_path / "a.db")).for_user("u1")
    cfg = SimpleNamespace(sector="GPU", end_product="GPU", provider="p", model="m",
                          market="us_stock", max_depth=4, top_n=5,
                          max_market_cap_yi=200, language="zh")
    saved = store.save(cfg, {"sector": "GPU", "chain": {}, "bottleneck_reports": [],
                             "supplier_scorecards": scorecards})
    aid = saved[0] if isinstance(saved, tuple) else saved
    return store, aid


def test_sort_key_uses_final_then_falls_back_to_overall():
    assert sort_key(_card("A", 9.0, 6.0)) == 6.0
    assert sort_key(_card("B", 6.0, None)) == 6.0  # final 缺失 → overall


def test_canonical_picks_filters_reject_and_threshold():
    cards = [
        _card("TSM", 8.9, 7.51),
        _card("NVDA", 8.5, 6.40),
        _card("MU", 8.3, 6.48),          # final 高于 NVDA → 应排在 NVDA 前
        _card("BAD", 9.9, 9.9, "REJECT"),  # 硬门拦截，最高分也不出现
        _card("LOW", 4.0, 4.0),          # 低于门槛
    ]
    got = canonical_picks(cards, top_n=10)
    assert got == ["TSM", "MU", "NVDA"], got
    assert "BAD" not in got


def test_phase3_ranked_and_phase4_picks_are_the_same_company_set():
    """核心不变式：Phase 4 讨论的公司必须就是 Phase 3 选出来的那批。"""
    cards = [_card(f"T{i}", 9.0 - i * 0.1, 8.0 - i * 0.3) for i in range(20)]

    # Phase 3：入选子集（前 8 家）→ 排序取 top 5
    shortlist = cards[:8]
    phase3_ranked = passed_top(shortlist, top_n=5)
    # Phase 4：从 Phase 3 的 ranked_results 反查 ticker 再排序取 top 5
    phase3_tickers = {sc["supplier"]["ticker"] for sc in phase3_ranked}
    phase4 = passed_top([c for c in cards if c["supplier"]["ticker"] in phase3_tickers], top_n=5)

    assert [c["supplier"]["ticker"] for c in phase4] == [
        c["supplier"]["ticker"] for c in phase3_ranked
    ]
    # 落库的 top_picks 也必须是同一批
    assert canonical_picks(shortlist, top_n=5) == [c["supplier"]["ticker"] for c in phase4]


def test_top_picks_never_leaks_unselected_tickers(tmp_path):
    """用户只勾了 3 家时，落库 top_picks 不得混入未勾选的公司。

    服务端会把全量入围名单写回 supplier_scorecards（历史/仪表盘路径要用），
    所以 Phase 3 调用 update_suppliers 时必须通过 picks_pool 把评选范围收窄。
    """
    selected = [_card("AAA", 8.0, 7.0), _card("BBB", 7.0, 6.0), _card("CCC", 6.0, 5.5)]
    others = [_card("XXX", 9.9, 9.9), _card("YYY", 9.5, 9.5)]
    store, aid = _store_with_record(tmp_path, selected)

    store.update_suppliers(
        aid, selected + others,
        scoring_config={"quality_weight": 0.4, "alpha_weight": 0.6, "top_n": 5},
        picks_pool=["AAA", "BBB", "CCC"],
    )
    rec = store.get(aid)
    picks = rec["result_json"]["top_picks"]
    assert picks == ["AAA", "BBB", "CCC"], picks
    assert "XXX" not in picks and "YYY" not in picks
    # 全量评分卡仍然完整保留（历史/仪表盘要用），只是不参与评选
    assert len(rec["result_json"]["supplier_scorecards"]) == 5


def test_update_suppliers_top_picks_uses_canonical_order(tmp_path):
    """不传 picks_pool 时，top_picks 按统一口径（final_score 优先）排序。"""
    cards = [_card("NVDA", 8.5, 6.40), _card("MU", 8.3, 6.48), _card("TSM", 8.9, 7.51)]
    store, aid = _store_with_record(tmp_path, cards)

    store.update_suppliers(aid, cards, scoring_config={"top_n": 5})
    assert store.get(aid)["result_json"]["top_picks"] == ["TSM", "MU", "NVDA"]


def test_defaults_and_config_override():
    assert DEFAULT_TOP_N == 5
    assert DEFAULT_MIN_SCORE == 5.0
    assert picks_top_n_from_config({"top_n": 8}) == 8
    assert picks_top_n_from_config({"top_n": 0}) == DEFAULT_TOP_N
    assert picks_top_n_from_config(None) == DEFAULT_TOP_N
    assert picks_top_n_from_config({"top_n": "abc"}) == DEFAULT_TOP_N


def test_passed_top_keeps_order_and_does_not_threshold():
    cards = [_card("A", 3.0, 2.0), _card("B", 9.0, 8.0)]
    # passed_top 不做门槛裁剪（门槛只在 canonical_picks 里生效）
    assert [c["supplier"]["ticker"] for c in passed_top(cards, top_n=5)] == ["B", "A"]
    # canonical_picks 会裁掉 2.0
    assert canonical_picks(cards, top_n=5) == ["B"]


def test_scoring_config_records_evaluated_tickers(tmp_path):
    """勾选子集参评时，scoring_config 要记下参评名单，供历史恢复还原同一份。"""
    store, aid = _store_with_record(tmp_path, [_card("A", 8, 7), _card("B", 7, 6), _card("C", 6, 5)])

    # 只评了 A/B 两家 → 记录 evaluated_tickers
    store.update_suppliers(
        aid, [_card("A", 8, 7), _card("B", 7, 6), _card("C", 6, 5)],
        scoring_config={"quality_weight": 0.4, "alpha_weight": 0.6, "top_n": 5,
                        "evaluated_tickers": ["A", "B"]},
        picks_pool=["A", "B"],
    )
    rj = store.get(aid)["result_json"]
    assert rj["scoring_config"]["evaluated_tickers"] == ["A", "B"]
    assert rj["top_picks"] == ["A", "B"]

    # 恢复端按 evaluated_tickers 收窄后再重排 → 名单一致
    pool = ["A", "B", "C"]
    narrowed = [t for t in pool if t in set(rj["scoring_config"]["evaluated_tickers"])]
    assert narrowed == ["A", "B"]


def test_real_analysis_30_divergence_is_resolved():
    """生产 #30（半导体设备链）真实数据：旧口径下三条路径给出三份不同名单。

    quality_weight=0.4 / alpha_weight=0.6 的几何加权会重排 overall_score：
    MU  overall 8.3 → final 6.48，超过 NVDA 的 overall 8.5 → final 6.40。
    旧代码里 Phase 3 按 final 排、top_picks 按 overall≥6 取前 5，
    于是「上一阶段筛选的公司」和「最终推荐名单」对不上，被误判为记录串扰。
    """
    # (ticker, overall_score, final_score) —— 取自 #30 的计分卡
    rows = [
        ("TSM", 8.9, 7.51), ("NVDA", 8.5, 6.40), ("MU", 8.3, 6.48),
        ("AVGO", 8.0, 6.03), ("LRCX", 7.7, 5.80), ("CDNS", 7.5, 6.09),
        ("AMZN", 7.4, 6.13), ("SNPS", 7.3, 6.02), ("KLAC", 7.3, 6.02),
        ("MSFT", 7.3, 5.68), ("AMAT", 7.3, 5.68), ("TMO", 7.2, 5.79),
        ("ARM", 6.9, 5.76), ("LIN", 6.3, 5.28), ("MKSI", 6.2, 5.96),
        ("DELL", 6.0, 5.44), ("AMD", 6.0, 5.44), ("ENTG", 5.8, 5.56),
        ("RBC", 5.6, 5.42), ("ONTO", 5.6, 5.29),
    ]
    cards = [_card(t, o, f) for t, o, f in rows]

    picks = canonical_picks(cards, top_n=5)
    assert picks == ["TSM", "MU", "NVDA", "AMZN", "CDNS"], picks
    # MU 必须排在 NVDA 前面（final 6.48 > 6.40），这正是几何加权的效果
    assert picks.index("MU") < picks.index("NVDA")
    # LRCX overall 7.7 高于 CDNS 7.5，但 final 5.80 < 6.09 → 不进前五
    assert "LRCX" not in picks and "CDNS" in picks
    # Phase 4 / 圆桌拿到的也是同一批
    assert [c["supplier"]["ticker"] for c in passed_top(cards, top_n=5)] == picks


def test_empty_input_is_safe():
    assert canonical_picks([]) == []
    assert passed_top([]) == []
    assert canonical_picks(None) == []
