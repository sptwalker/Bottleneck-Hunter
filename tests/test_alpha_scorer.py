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
    volume_ratio=None,
    price_change_3m_pct=None,
    price_change_1m_pct=None,
    institution_holding_pct=None,
    consecutive_volume_days=0,
    days_since_ipo=None,
    market=MarketRegion.A_STOCK,
    with_snapshot=True,
) -> SupplierScorecard:
    supplier = SupplierInfo(
        name="测试公司", ticker="600001.SH", market=market,
        market_cap=market_cap, sector="测试", description="desc",
    )
    snapshot = None
    if with_snapshot:
        snapshot = FinancialSnapshot(
            data_source="akshare_ths",
            analyst_report_count=analyst_report_count,
            volume_ratio=volume_ratio,
            price_change_3m_pct=price_change_3m_pct,
            price_change_1m_pct=price_change_1m_pct,
            institution_holding_pct=institution_holding_pct,
            consecutive_volume_days=consecutive_volume_days,
            days_since_ipo=days_since_ipo,
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
        assert alpha.market_attention < 4.0
        assert alpha.alpha_score > 4.5

    def test_low_alpha_large_cap_high_coverage(self):
        sc = _make_scorecard(market_cap=2000, analyst_report_count=50)
        alpha = AlphaScorer.compute(sc, bottleneck_score=9.0)
        assert alpha.market_attention > 6.0
        assert alpha.alpha_score < 4.0

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
        assert 0 <= alpha.dim_cap <= 9
        assert 0 <= alpha.dim_analyst <= 9
        assert 0 <= alpha.dim_volume <= 9
        assert 0 <= alpha.dim_price <= 9
        assert alpha.dim_institution is None or 0 <= alpha.dim_institution <= 9
        assert alpha.ipo_bonus in (0, 2)
        assert alpha.vp_discount in (0.8, 1.0)

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

    def test_volume_momentum_high(self):
        sc = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=2.5, price_change_3m_pct=50.0,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        sc_low = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=0.5, price_change_3m_pct=50.0,
        )
        alpha_low = AlphaScorer.compute(sc_low, bottleneck_score=8.0)
        assert alpha.market_attention > alpha_low.market_attention

    def test_consecutive_volume_bonus(self):
        sc = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=1.5, consecutive_volume_days=3,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        sc_no_consec = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=1.5, consecutive_volume_days=0,
        )
        alpha_no = AlphaScorer.compute(sc_no_consec, bottleneck_score=8.0)
        assert alpha.market_attention > alpha_no.market_attention

    def test_ipo_bonus(self):
        sc = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            days_since_ipo=200,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        sc_old = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            days_since_ipo=500,
        )
        alpha_old = AlphaScorer.compute(sc_old, bottleneck_score=8.0)
        assert alpha.market_attention == alpha_old.market_attention + 2.0

    def test_attention_floor(self):
        sc = _make_scorecard(
            market_cap=5, analyst_report_count=0,
            volume_ratio=0.3, price_change_3m_pct=-30.0,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        assert alpha.market_attention >= 2.0

    def test_volume_price_divergence(self):
        sc = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=0.5, price_change_1m_pct=30.0,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        sc_no_div = _make_scorecard(
            market_cap=100, analyst_report_count=5,
            volume_ratio=0.5, price_change_1m_pct=10.0,
        )
        alpha_no_div = AlphaScorer.compute(sc_no_div, bottleneck_score=8.0)
        assert alpha.alpha_score < alpha_no_div.alpha_score

    def test_us_stock_institution_holding(self):
        sc = _make_scorecard(
            market_cap=50, analyst_report_count=20,
            institution_holding_pct=80.0,
            market=MarketRegion.US_STOCK,
        )
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        sc_low = _make_scorecard(
            market_cap=50, analyst_report_count=20,
            institution_holding_pct=10.0,
            market=MarketRegion.US_STOCK,
        )
        alpha_low = AlphaScorer.compute(sc_low, bottleneck_score=8.0)
        assert alpha.market_attention > alpha_low.market_attention
