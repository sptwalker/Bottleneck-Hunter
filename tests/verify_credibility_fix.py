"""验证 credibility 字段修复是否生效"""
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from bottleneck_hunter.chain.models import SupplierScorecard, SupplierInfo, AlphaScore, FinalScore
from bottleneck_hunter.chain.fact_check import apply_fact_check_to_scorecards
from bottleneck_hunter.chain.supplier_eval import FinalScorer


def test_credibility_preserved():
    """测试 FactCheck → FinalScorer 流程中 credibility 是否保留"""

    # 创建测试 scorecard
    sc = SupplierScorecard(
        supplier=SupplierInfo(
            ticker="TEST",
            name="测试公司",
            market="a_stock",
            sector="半导体",
            description="测试公司描述",
        ),
        bottleneck_node="测试瓶颈",
        overall_score=8.0,
        alpha=AlphaScore(alpha_score=7.0),
    )

    print("=== 测试流程 ===")
    print(f"初始 overall_score: {sc.overall_score}")
    print(f"初始 final: {sc.final}")

    # Step 1: 运行 FactCheck
    print("\n1. 运行 FactCheck...")
    reports = apply_fact_check_to_scorecards([sc], [])
    report = reports[0]

    print(f"   credibility: {report.credibility}")
    print(f"   recommendation: {report.recommendation}")
    print(f"   调整后 overall_score: {sc.overall_score}")

    if sc.final:
        print(f"   sc.final.credibility: {sc.final.credibility}")
        print(f"   sc.final.quality_adjusted: {sc.final.quality_adjusted}")
    else:
        print("   ❌ sc.final 为 None")

    # Step 2: 运行 FinalScorer
    print("\n2. 运行 FinalScorer...")
    FinalScorer.score_all([sc])

    print(f"   final_score: {sc.final.final_score}")
    print(f"   quality_score: {sc.final.quality_score}")
    print(f"   alpha_score: {sc.final.alpha_score}")
    print(f"   credibility: {sc.final.credibility}")
    print(f"   quality_adjusted: {sc.final.quality_adjusted}")

    # 验证
    print("\n=== 验证结果 ===")

    assert sc.final is not None, "❌ final 对象不存在"
    print("✓ final 对象存在")

    assert sc.final.credibility is not None, "❌ credibility 为 None"
    print(f"✓ credibility 已保留: {sc.final.credibility}")

    assert sc.final.quality_adjusted is not None, "❌ quality_adjusted 为 None"
    print(f"✓ quality_adjusted 已保留: {sc.final.quality_adjusted}")

    assert sc.final.credibility == report.credibility, "❌ credibility 值不匹配"
    print(f"✓ credibility 值正确: {sc.final.credibility} == {report.credibility}")

    print("\n✅ 所有验证通过！")


if __name__ == "__main__":
    test_credibility_preserved()
