"""Tests for report generator."""

from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
    CrossValidationReport,
    IndustryNode,
    LayerType,
    MarketRegion,
    ModelValidation,
    ScreeningResult,
    SupplierInfo,
    SupplierScorecard,
)
from bottleneck_hunter.chain.report import generate_report


def _make_supplier() -> SupplierInfo:
    return SupplierInfo(
        name="中际旭创",
        ticker="SZ300308",
        market=MarketRegion.A_STOCK,
        market_cap=1500.0,
        sector="光模块",
        description="全球领先的光模块供应商",
    )


def _make_scorecard(supplier: SupplierInfo | None = None) -> SupplierScorecard:
    if supplier is None:
        supplier = _make_supplier()
    return SupplierScorecard(
        supplier=supplier,
        bottleneck_node="光模块",
        market_position=9.0,
        customer_validation=8.5,
        capacity_status=7.0,
        financial_health=8.0,
        valuation=6.5,
        overall_score=7.8,
        strengths=["全球市占率第一", "英伟达核心供应商"],
        weaknesses=["估值偏高", "客户集中度高"],
    )


def _make_cross_validation(supplier: SupplierInfo | None = None) -> CrossValidationReport:
    if supplier is None:
        supplier = _make_supplier()
    return CrossValidationReport(
        supplier_name=supplier.name,
        ticker=supplier.ticker,
        validations=[
            ModelValidation(model_name="deepseek/deepseek-chat", score=8.0, reasoning="逻辑成立", concerns=["估值偏高"]),
            ModelValidation(model_name="qwen/qwen-plus", score=7.0, reasoning="瓶颈地位明确", concerns=[]),
        ],
        consensus_score=7.5,
        consensus_reasoning="多数模型看好",
        avg_score=7.5,
    )


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


# ---------------------------------------------------------------------------
# 供应商评分卡板块
# ---------------------------------------------------------------------------


def test_report_zh_supplier_section():
    """中文报告包含供应商评分汇总表和详细信息。"""
    result = _make_result()
    result.supplier_scorecards = [_make_scorecard()]
    report = generate_report(result, language="zh")

    assert "候选供应商评分" in report
    assert "中际旭创" in report
    assert "SZ300308" in report
    assert "7.8" in report
    assert "全球市占率第一" in report
    assert "估值偏高" in report


def test_report_en_supplier_section():
    """英文报告包含供应商评分卡。"""
    result = _make_result()
    result.supplier_scorecards = [_make_scorecard()]
    report = generate_report(result, language="en")

    assert "Supplier Scorecards" in report
    assert "中际旭创" in report


# ---------------------------------------------------------------------------
# 交叉验证板块
# ---------------------------------------------------------------------------


def test_report_zh_cross_validation():
    """中文报告包含交叉验证结果。"""
    result = _make_result()
    result.supplier_scorecards = [_make_scorecard()]
    result.cross_validations = [_make_cross_validation()]
    report = generate_report(result, language="zh")

    assert "多模型交叉验证" in report
    assert "deepseek" in report
    assert "qwen" in report
    assert "7.5" in report
    assert "共识" in report


def test_report_en_cross_validation():
    """英文报告包含交叉验证。"""
    result = _make_result()
    result.supplier_scorecards = [_make_scorecard()]
    result.cross_validations = [_make_cross_validation()]
    report = generate_report(result, language="en")

    assert "Cross-Validation" in report
    assert "Consensus" in report


# ---------------------------------------------------------------------------
# 最终推荐板块
# ---------------------------------------------------------------------------


def test_report_zh_top_picks():
    """中文报告包含最终推荐。"""
    supplier = _make_supplier()
    result = _make_result()
    result.supplier_scorecards = [_make_scorecard(supplier)]
    result.cross_validations = [_make_cross_validation(supplier)]
    result.top_picks = ["SZ300308"]
    report = generate_report(result, language="zh")

    assert "最终推荐" in report
    assert "SZ300308" in report
    assert "中际旭创" in report


def test_report_en_top_picks():
    """英文报告包含 Top Picks。"""
    result = _make_result()
    result.top_picks = ["SZ300308"]
    report = generate_report(result, language="en")

    assert "Top Picks" in report
    assert "SZ300308" in report
