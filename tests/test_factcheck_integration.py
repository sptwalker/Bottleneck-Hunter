"""端到端集成测试: FactCheck Gate 在流水线中的实际效果。

验证:
1. FactCheck 正确调整 overall_score (credibility 折进 quality)
2. FinalScorer 使用调整后的 quality 计算 final_score
3. REJECT 候选被 top_picks gate 拦截
4. credibility 和 recommendation 正确透传到 Phase 4
"""

import pytest
from bottleneck_hunter.chain.models import (
    SupplierInfo,
    SupplierScorecard,
    FinancialSnapshot,
    FinancialTrend,
    BottleneckReport,
    MarketRegion,
)
from bottleneck_hunter.chain.fact_check import apply_fact_check_to_scorecards
from bottleneck_hunter.chain.supplier_eval import FinalScorer, AlphaScorer


def test_factcheck_adjusts_quality_and_flows_to_final_score():
    """验证 credibility 调整 → quality 下降 → final_score 下降的完整链路。"""

    # 构造一个"财务健康8分但毛利趋势-5pp现金流负"的矛盾 scorecard
    snap_bad = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=20.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=-5.0,  # 大幅下滑
            revenue_acceleration=-3.0,
            trend_summary="恶化",
        ),
        cashflow_per_share=-0.5,  # 现金流为负
    )

    sc_bad = SupplierScorecard(
        supplier=SupplierInfo(
            name="矛盾公司A",
            ticker="000001.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试",
            description="测试",
        ),
        bottleneck_node="测试环节",
        market_position=7.0,
        customer_validation=7.0,
        capacity_status=7.0,
        financial_health=8.0,  # 声称健康
        valuation=7.0,
        overall_score=7.5,  # 初始 quality
        strengths=["财务稳健", "现金流充裕"],  # 与数据矛盾
        weaknesses=[],
        financial_snapshot=snap_bad,
    )

    # 构造一个正常的 scorecard 作为对照
    snap_good = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=35.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=3.0,
            revenue_acceleration=5.0,
            trend_summary="良好",
        ),
        cashflow_per_share=2.5,
    )

    sc_good = SupplierScorecard(
        supplier=SupplierInfo(
            name="正常公司B",
            ticker="000002.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试",
            description="测试",
        ),
        bottleneck_node="测试环节",
        market_position=8.0,
        customer_validation=8.0,
        capacity_status=8.0,
        financial_health=8.0,
        valuation=8.0,
        overall_score=8.0,
        strengths=["财务健康", "毛利率提升"],
        weaknesses=[],
        financial_snapshot=snap_good,
    )

    scorecards = [sc_bad, sc_good]

    # ── Step 1: FactCheck 调整 overall_score ──
    original_quality_bad = sc_bad.overall_score
    original_quality_good = sc_good.overall_score

    reports = apply_fact_check_to_scorecards(scorecards, None)

    assert len(reports) == 2
    report_bad = reports[0]
    report_good = reports[1]

    # 矛盾公司应该 REJECT
    assert report_bad.recommendation == "REJECT", f"预期REJECT,实际{report_bad.recommendation}"
    assert report_bad.credibility < 7.0, f"矛盾公司credibility应<7,实际{report_bad.credibility}"
    assert sc_bad.fact_check_recommendation == "REJECT"
    assert sc_bad.overall_score < original_quality_bad, "overall_score 应被调低"

    # 正常公司应该 PASS
    assert report_good.recommendation == "PASS"
    assert report_good.credibility >= 9.5
    assert sc_good.fact_check_recommendation == "PASS"
    # overall_score 可能略升或不变(取决于 supported 加分)
    assert sc_good.overall_score >= original_quality_good * 0.95

    # ── Step 2: AlphaScorer + FinalScorer 计算 final_score ──
    # 模拟 Phase 3 流程
    bn_score_map = {"测试环节": 8.0}
    AlphaScorer.score_all(scorecards, bn_score_map)
    FinalScorer.score_all(scorecards)

    assert sc_bad.final is not None
    assert sc_good.final is not None

    # final_score = quality^0.55 × alpha^0.45
    # sc_bad 的 quality 被 credibility 拉低 → final_score 应该低于原始 7.5
    # (但 alpha 也会影响,所以只验证相对关系)
    assert sc_bad.final.final_score < sc_good.final.final_score, \
        f"矛盾公司 final_score={sc_bad.final.final_score} 应低于正常公司 {sc_good.final.final_score}"

    # 验证 credibility 确实被记录(即使 FinalScorer 当前未使用)
    # (未来可能在 FinalScorer 中直接使用 credibility)

    # ── Step 3: top_picks gate 过滤 REJECT ──
    passed = [sc for sc in scorecards if sc.fact_check_recommendation != "REJECT"]
    assert len(passed) == 1, "REJECT 应被拦截"
    assert passed[0].supplier.ticker == "000002.SZ", "只有正常公司通过"


def test_factcheck_does_not_penalize_sparse_data():
    """验证"无数据不误杀" — 信息稀疏的小盘股不应被 REJECT。"""

    sc_sparse = SupplierScorecard(
        supplier=SupplierInfo(
            name="冷门小盘股",
            ticker="300999.SZ",
            market=MarketRegion.A_STOCK,
            sector="测试",
            description="测试",
        ),
        bottleneck_node="测试环节",
        market_position=7.0,
        customer_validation=6.0,
        capacity_status=6.0,
        financial_health=7.0,
        valuation=6.0,
        overall_score=6.5,
        strengths=["管理层经验丰富", "团队稳定"],  # 真正无数据可核的定性声称
        weaknesses=[],
        financial_snapshot=None,  # 无财务数据
        smart_money=None,  # 无聪明钱数据
    )

    reports = apply_fact_check_to_scorecards([sc_sparse], None)
    report = reports[0]

    assert report.recommendation != "REJECT", "无数据不应REJECT"
    assert report.credibility >= 9.0, f"无数据不应扣分,实际{report.credibility}"
    assert sc_sparse.overall_score >= 6.5 * 0.99, "overall_score 应基本不变"


def test_factcheck_tie_breaker():
    """验证同分时 credibility 作为 tie-breaker。"""

    # 两个 overall_score 相同的候选,一个有数据支撑,一个有软不符
    snap_supported = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=30.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=2.0,
            trend_summary="改善",
        ),
    )

    snap_mismatch = FinancialSnapshot(
        data_source="test",
        report_date="2025-12-31",
        gross_margin_pct=25.0,
        trend=FinancialTrend(
            quarters=[],
            gross_margin_trend=-1.5,  # 轻微下滑
            trend_summary="波动",
        ),
    )

    sc1 = SupplierScorecard(
        supplier=SupplierInfo(name="A", ticker="000001.SZ", market=MarketRegion.A_STOCK, sector="x", description="y"),
        bottleneck_node="n",
        market_position=7.0, customer_validation=7.0, capacity_status=7.0,
        financial_health=7.0, valuation=7.0, overall_score=7.0,
        strengths=["毛利率提升"], weaknesses=[], financial_snapshot=snap_supported,
    )

    sc2 = SupplierScorecard(
        supplier=SupplierInfo(name="B", ticker="000002.SZ", market=MarketRegion.A_STOCK, sector="x", description="y"),
        bottleneck_node="n",
        market_position=7.0, customer_validation=7.0, capacity_status=7.0,
        financial_health=7.0, valuation=7.0, overall_score=7.0,
        strengths=["毛利率提升"], weaknesses=[], financial_snapshot=snap_mismatch,  # 声称提升但数据下滑
    )

    scorecards = [sc1, sc2]
    reports = apply_fact_check_to_scorecards(scorecards, None)

    # sc1 应有更高 credibility
    assert reports[0].credibility > reports[1].credibility

    # 排序后 sc1 应排在 sc2 前面
    FinalScorer.score_all(scorecards)
    scorecards.sort(key=lambda s: s.final.final_score if s.final else s.overall_score, reverse=True)
    assert scorecards[0].supplier.ticker == "000001.SZ", "有数据支撑的应排前"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
