"""Tests for AlphaScorer."""

import pytest

from bottleneck_hunter.chain.models import (
    AlphaScore,
    FinancialSnapshot,
    MarketRegion,
    SupplierInfo,
    SupplierScorecard,
)
from bottleneck_hunter.chain.supplier_eval import AlphaScorer


def _make_scorecard(
    market_cap=None,
    analyst_report_count=None,
    with_snapshot=True,
) -> SupplierScorecard:
    supplier = SupplierInfo(
        name="测试公司", ticker="600001.SH", market=MarketRegion.A_STOCK,
        market_cap=market_cap, sector="测试", description="desc",
    )
    snapshot = None
    if with_snapshot:
        snapshot = FinancialSnapshot(
            data_source="akshare_ths",
            analyst_report_count=analyst_report_count,
        )
    return SupplierScorecard(
        supplier=supplier, bottleneck_node="测试环节",
        market_position=7, customer_validation=6, capacity_status=6,
        financial_health=7, valuation=5, overall_score=6.2,
        financial_snapshot=snapshot,
    )


class TestAlphaScorer:
    def test_high_alpha_small_cap_low_coverage(self):
        sc = _make_scorecard(market_cap=30, analyst_report_count=2)
        alpha = AlphaScorer.compute(sc, bottleneck_score=9.0)
        assert alpha.market_attention < 3.0
        assert alpha.alpha_score > 6.0

    def test_low_alpha_large_cap_high_coverage(self):
        sc = _make_scorecard(market_cap=2000, analyst_report_count=50)
        alpha = AlphaScorer.compute(sc, bottleneck_score=9.0)
        assert alpha.market_attention > 7.0
        assert alpha.alpha_score < 3.0

    def test_no_snapshot(self):
        sc = _make_scorecard(market_cap=100, with_snapshot=False)
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        assert 0 <= alpha.alpha_score <= 10
        assert alpha.reasoning

    def test_no_market_cap(self):
        sc = _make_scorecard(market_cap=None, analyst_report_count=5)
        alpha = AlphaScorer.compute(sc, bottleneck_score=7.0)
        assert 0 <= alpha.market_attention <= 10

    def test_bounds(self):
        sc = _make_scorecard(market_cap=10, analyst_report_count=0)
        alpha = AlphaScorer.compute(sc, bottleneck_score=10.0)
        assert 0 <= alpha.alpha_score <= 10
        assert 0 <= alpha.market_attention <= 10
        assert 0 <= alpha.information_gap <= 10

    def test_score_all(self):
        scorecards = [
            _make_scorecard(market_cap=30, analyst_report_count=2),
            _make_scorecard(market_cap=2000, analyst_report_count=50),
        ]
        bn_map = {"测试环节": 8.5}
        result = AlphaScorer.score_all(scorecards, bn_map)
        assert len(result) == 2
        assert result[0].alpha is not None
        assert result[1].alpha is not None
        assert result[0].alpha.alpha_score > result[1].alpha.alpha_score
