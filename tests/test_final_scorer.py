"""Tests for FinalScorer — 统一最终评分。"""

import pytest

from bottleneck_hunter.chain.models import (
    AlphaScore,
    FinalScore,
    MarketRegion,
    SupplierInfo,
    SupplierScorecard,
)
from bottleneck_hunter.chain.supplier_eval import FinalScorer


def _make_scorecard(
    overall_score: float = 6.0,
    alpha_score: float = 5.0,
) -> SupplierScorecard:
    supplier = SupplierInfo(
        name="测试公司", ticker="600001.SH", market=MarketRegion.A_STOCK,
        market_cap=100, sector="测试", description="desc",
    )
    alpha = AlphaScore(
        market_attention=5.0, information_gap=5.0,
        alpha_score=alpha_score, reasoning="test",
    )
    return SupplierScorecard(
        supplier=supplier, bottleneck_node="测试环节",
        market_position=7, customer_validation=6, capacity_status=6,
        financial_health=7, valuation=5, overall_score=overall_score,
        alpha=alpha,
    )


class TestFinalScorer:
    def test_geometric_mean_balanced(self):
        """均衡分数应保持不变。"""
        sc = _make_scorecard(overall_score=7.0, alpha_score=7.0)
        result = FinalScorer.compute(sc)
        assert result.final_score == 7.0

    def test_high_quality_low_alpha(self):
        """高质量低alpha应被惩罚（市场已知的好公司）。"""
        sc = _make_scorecard(overall_score=9.0, alpha_score=1.0)
        result = FinalScorer.compute(sc)
        assert result.final_score < 3.0

    def test_low_quality_high_alpha(self):
        """低质量高alpha：隐蔽但有风险。"""
        sc = _make_scorecard(overall_score=3.0, alpha_score=9.0)
        result = FinalScorer.compute(sc)
        assert 5.0 < result.final_score < 7.0

    def test_strong_all_around(self):
        """全面优秀。"""
        sc = _make_scorecard(overall_score=8.0, alpha_score=8.0)
        result = FinalScorer.compute(sc)
        assert result.final_score == 8.0

    def test_orthogonality(self):
        """quality 和 alpha 完全正交：修改 alpha 不影响 quality_score。"""
        sc1 = _make_scorecard(overall_score=7.0, alpha_score=3.0)
        sc2 = _make_scorecard(overall_score=7.0, alpha_score=9.0)
        r1 = FinalScorer.compute(sc1)
        r2 = FinalScorer.compute(sc2)
        assert r1.quality_score == r2.quality_score == 7.0
        assert r1.alpha_score != r2.alpha_score

    def test_weight_adjustment(self):
        """调整权重改变偏好。"""
        sc = _make_scorecard(overall_score=5.0, alpha_score=9.0)
        default = FinalScorer.compute(sc, w_q=0.4, w_a=0.6)
        quality_heavy = FinalScorer.compute(sc, w_q=0.7, w_a=0.3)
        alpha_heavy = FinalScorer.compute(sc, w_q=0.2, w_a=0.8)
        assert alpha_heavy.final_score > default.final_score > quality_heavy.final_score

    def test_no_alpha(self):
        """无 alpha 数据时使用最低值。"""
        supplier = SupplierInfo(
            name="测试公司", ticker="600001.SH", market=MarketRegion.A_STOCK,
            market_cap=100, sector="测试", description="desc",
        )
        sc = SupplierScorecard(
            supplier=supplier, bottleneck_node="测试环节",
            market_position=7, customer_validation=6, capacity_status=6,
            financial_health=7, valuation=5, overall_score=8.0,
            alpha=None,
        )
        result = FinalScorer.compute(sc)
        assert result.alpha_score == 0.1
        assert result.final_score < 2.0

    def test_bounds(self):
        """结果始终在 0-10 范围内。"""
        for q in [0.0, 0.1, 5.0, 10.0]:
            for a in [0.0, 0.1, 5.0, 10.0]:
                sc = _make_scorecard(overall_score=max(0, q), alpha_score=max(0.1, a))
                result = FinalScorer.compute(sc)
                assert 0 <= result.final_score <= 10
                assert 0 <= result.quality_score <= 10
                assert 0 <= result.alpha_score <= 10

    def test_score_all_sorts_by_final(self):
        """score_all 应按 final_score 降序排列。"""
        scorecards = [
            _make_scorecard(overall_score=5.0, alpha_score=3.0),
            _make_scorecard(overall_score=8.0, alpha_score=8.0),
            _make_scorecard(overall_score=6.0, alpha_score=7.0),
        ]
        result = FinalScorer.score_all(scorecards)
        assert len(result) == 3
        assert all(sc.final is not None for sc in result)
        scores = [sc.final.final_score for sc in result]
        assert scores == sorted(scores, reverse=True)

    def test_score_all_preserves_weights(self):
        """score_all 传入的权重应体现在结果中。"""
        scorecards = [_make_scorecard(overall_score=7.0, alpha_score=7.0)]
        FinalScorer.score_all(scorecards, w_q=0.3, w_a=0.7)
        assert scorecards[0].final.quality_weight == 0.3
        assert scorecards[0].final.alpha_weight == 0.7
