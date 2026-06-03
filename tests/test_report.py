"""Tests for report generator."""

from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
    IndustryNode,
    LayerType,
    ScreeningResult,
)
from bottleneck_hunter.chain.report import generate_report


def _make_result() -> ScreeningResult:
    nodes = [
        IndustryNode(name="GPU", description="GPU芯片", layer=0, layer_type=LayerType.END_PRODUCT, function="compute"),
        IndustryNode(name="光模块", description="光电信号转换", layer=1, layer_type=LayerType.COMPONENT, function="transfer"),
    ]
    chain = ChainGraph(sector="GPU/AI算力", end_product="GPU", nodes=nodes, links=[], max_depth=1)

    bottlenecks = [
        BottleneckReport(
            node_name="光模块",
            node_description="光电信号转换",
            layer=1,
            scores=[
                BottleneckScore(dimension="scarcity", score=8.0, reasoning="供应商少"),
                BottleneckScore(dimension="irreplaceability", score=9.0, reasoning="不可替代"),
                BottleneckScore(dimension="supply_demand_gap", score=7.0, reasoning="缺口大"),
                BottleneckScore(dimension="pricing_power", score=6.0, reasoning="可涨价"),
                BottleneckScore(dimension="tech_barrier", score=8.0, reasoning="壁垒高"),
            ],
            overall_score=7.7,
            rank=1,
            key_insights=["核心瓶颈"],
            risks=["竞争加剧"],
        )
    ]

    return ScreeningResult(
        sector="GPU/AI算力",
        chain=chain,
        bottleneck_reports=bottlenecks,
        supplier_scorecards=[],
        cross_validations=[],
    )


def test_generate_report_zh():
    result = _make_result()
    report = generate_report(result, language="zh")

    assert "# GPU/AI算力 产业链选股报告" in report
    assert "瓶颈环节排名" in report
    assert "光模块" in report
    assert "7.7" in report
    assert "仅供参考" in report


def test_generate_report_en():
    result = _make_result()
    report = generate_report(result, language="en")

    assert "Supply Chain Screening Report" in report
    assert "光模块" in report
    assert "not investment advice" in report
