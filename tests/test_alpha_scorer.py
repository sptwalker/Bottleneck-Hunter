"""Tests for AlphaScorer."""


from bottleneck_hunter.chain.models import (
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

    def test_all_dims_missing_marks_insufficient(self):
        """全维度无数据 → alpha 置 None，**不得**退回中性分 5.0。

        旧行为下"没抓到任何数据"和"恰好中等关注度"会得出同一个 5.0，
        击穿 alpha = 瓶颈重要性 × (1 − 关注度/10) 的立意。
        """
        sc = _make_scorecard(market_cap=None, with_snapshot=False)
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        assert alpha.market_attention is None
        assert alpha.information_gap is None
        assert alpha.alpha_score is None
        assert "数据不足" in alpha.reasoning

    def test_partial_dims_still_scores(self):
        """只有市值一维有数据 → 仍能算关注度（归一化到一维），不误判为数据不足。"""
        sc = _make_scorecard(market_cap=30, with_snapshot=False)
        alpha = AlphaScorer.compute(sc, bottleneck_score=8.0)
        assert alpha.market_attention is not None
        assert alpha.alpha_score is not None
        assert alpha.dim_cap == 1  # 50 亿以下 → 最低档

    def test_unknown_bottleneck_marks_insufficient(self):
        """关注度五维齐备但瓶颈分未知 → 仍算不出 alpha，置 None。"""
        sc = _make_scorecard(market_cap=30, analyst_report_count=2)
        alpha = AlphaScorer.compute(sc, bottleneck_score=None)
        assert alpha.market_attention is not None
        assert alpha.alpha_score is None
        assert "瓶颈分未知" in alpha.reasoning

    def test_missing_dims_renormalized_not_zeroed(self):
        """缺维度 → 从加权中剔除并重归一化，不是当 0/5 分计入。

        不写死金标：直接用返回的 dim_* 与 DIM_WEIGHTS 自洽复算，
        这样维度权重调整时测试仍有效，只在"缺维度被当成有效分"时才失败。
        """
        us = _make_scorecard(
            market_cap=100, analyst_report_count=20, volume_ratio=1.0,
            price_change_3m_pct=10, institution_holding_pct=40,
            market=MarketRegion.US_STOCK,
        )
        full = AlphaScorer.compute(us, bottleneck_score=8.0)
        dims = {
            "cap": full.dim_cap, "analyst": full.dim_analyst, "vol": full.dim_volume,
            "price": full.dim_price, "inst": full.dim_institution,
        }
        assert all(v is not None for v in dims.values())
        total_w = sum(AlphaScorer.DIM_WEIGHTS[k] for k in dims)
        raw = sum(dims[k] * AlphaScorer.DIM_WEIGHTS[k] for k in dims) / total_w
        assert full.market_attention == max(2.0, min(10.0, round(raw, 1)))

    def test_a_share_drops_inst_dim(self):
        """A 股无机构持仓数据 → inst 维剔除，权重摊回四维。"""
        a = _make_scorecard(
            market_cap=100, analyst_report_count=20, volume_ratio=1.0,
            price_change_3m_pct=10, market=MarketRegion.A_STOCK,
        )
        alpha = AlphaScorer.compute(a, bottleneck_score=8.0)
        assert alpha.dim_institution is None
        # 返回的 dim_* 字段名与 DIM_WEIGHTS 键不同名（vol ↔ dim_volume），逐一手写映射
        four = {"cap": alpha.dim_cap, "analyst": alpha.dim_analyst,
                "vol": alpha.dim_volume, "price": alpha.dim_price}
        assert all(v is not None for v in four.values())
        total_w = sum(AlphaScorer.DIM_WEIGHTS[k] for k in four)
        raw = sum(four[k] * AlphaScorer.DIM_WEIGHTS[k] for k in four) / total_w
        assert alpha.market_attention == max(2.0, min(10.0, round(raw, 1)))

    def test_no_market_cap(self):
        sc = _make_scorecard(market_cap=None, analyst_report_count=5)
        alpha = AlphaScorer.compute(sc, bottleneck_score=7.0)
        assert 0 <= alpha.market_attention <= 10
        assert alpha.dim_cap is None  # 缺的维度如实置 None，不假装 5 分

    def test_bounds(self):
        sc = _make_scorecard(market_cap=10, analyst_report_count=0)
        alpha = AlphaScorer.compute(sc, bottleneck_score=10.0)
        assert 0 <= alpha.alpha_score <= 10
        assert 0 <= alpha.market_attention <= 10
        assert 0 <= alpha.information_gap <= 10
        assert 0 <= alpha.dim_cap <= 9
        assert 0 <= alpha.dim_analyst <= 9
        # 本用例未传 volume_ratio / price_change_3m_pct → 这两维无数据
        assert alpha.dim_volume is None
        assert alpha.dim_price is None
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
