"""Tests for chain data models."""

import pytest

from bottleneck_hunter.chain.models import (
    AlphaScore,
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
    ChainLink,
    CrossValidationReport,
    FinancialSnapshot,
    IndustryNode,
    LayerType,
    ModelValidation,
    ScreeningResult,
    SupplierInfo,
    SupplierScorecard,
    ValidationResult,
    MarketRegion,
)


class TestIndustryNode:
    def test_create_node(self):
        node = IndustryNode(
            name="光模块",
            description="光电信号转换",
            layer=1,
            layer_type=LayerType.COMPONENT,
            function="数据传输",
        )
        assert node.name == "光模块"
        assert node.layer == 1
        assert node.upstream_deps == []
        assert node.downstream_deps == []


class TestChainGraph:
    def _make_graph(self) -> ChainGraph:
        nodes = [
            IndustryNode(name="GPU", description="终端", layer=0, layer_type=LayerType.END_PRODUCT, function="compute"),
            IndustryNode(name="HBM", description="内存", layer=1, layer_type=LayerType.ASSEMBLY, function="storage"),
            IndustryNode(name="光模块", description="光电转换", layer=1, layer_type=LayerType.COMPONENT, function="transfer"),
            IndustryNode(name="磷化铟衬底", description="衬底材料", layer=2, layer_type=LayerType.MATERIAL, function="substrate"),
        ]
        links = [
            ChainLink(upstream="HBM", downstream="GPU", dependency=0.9, alternatives=1),
            ChainLink(upstream="光模块", downstream="GPU", dependency=0.85, alternatives=0),
            ChainLink(upstream="磷化铟衬底", downstream="光模块", dependency=1.0, alternatives=0),
        ]
        return ChainGraph(sector="GPU", end_product="GPU", nodes=nodes, links=links, max_depth=2)

    def test_get_node(self):
        g = self._make_graph()
        assert g.get_node("GPU") is not None
        assert g.get_node("不存在") is None

    def test_get_nodes_at_layer(self):
        g = self._make_graph()
        layer0 = g.get_nodes_at_layer(0)
        assert len(layer0) == 1
        assert layer0[0].name == "GPU"
        layer1 = g.get_nodes_at_layer(1)
        assert len(layer1) == 2

    def test_get_upstream(self):
        g = self._make_graph()
        upstream = g.get_upstream("GPU")
        names = {n.name for n in upstream}
        assert names == {"HBM", "光模块"}

    def test_get_downstream(self):
        g = self._make_graph()
        downstream = g.get_downstream("磷化铟衬底")
        assert len(downstream) == 1
        assert downstream[0].name == "光模块"


class TestBottleneckReport:
    def test_create_report(self):
        scores = [
            BottleneckScore(dimension=BottleneckDimension.SCARCITY, score=9.0, reasoning="垄断"),
            BottleneckScore(dimension=BottleneckDimension.IRREPLACEABILITY, score=10.0, reasoning="物理不可替代"),
        ]
        report = BottleneckReport(
            node_name="磷化铟衬底",
            node_description="衬底材料",
            layer=3,
            scores=scores,
            overall_score=9.5,
            key_insights=["垄断95%市场"],
            risks=["产能扩张不确定"],
        )
        assert report.overall_score == 9.5
        assert len(report.scores) == 2


class TestSupplierScorecard:
    def test_create_scorecard(self):
        supplier = SupplierInfo(
            name="通美晶体",
            ticker="AGR",
            market=MarketRegion.US_STOCK,
            sector="半导体材料",
            description="磷化铟衬底供应商",
        )
        sc = SupplierScorecard(
            supplier=supplier,
            bottleneck_node="磷化铟衬底",
            market_position=10,
            customer_validation=8,
            capacity_status=7,
            financial_health=7,
            valuation=6,
            overall_score=7.6,
        )
        assert sc.overall_score == 7.6
        assert sc.supplier.name == "通美晶体"


class TestFinancialSnapshot:
    def test_create_empty(self):
        fs = FinancialSnapshot()
        assert fs.data_source == ""
        assert fs.revenue_yi is None
        assert fs.analyst_report_count is None

    def test_create_full(self):
        fs = FinancialSnapshot(
            data_source="akshare_ths",
            report_date="2025-12-31",
            revenue_yi=120.5,
            revenue_yoy_pct=15.3,
            net_profit_yi=18.2,
            net_profit_yoy_pct=22.1,
            gross_margin_pct=35.0,
            roe_pct=18.5,
            debt_ratio_pct=42.0,
            cashflow_per_share=1.25,
            analyst_report_count=28,
            analyst_rating="买入",
            consensus_eps=2.15,
            consensus_pe=25.3,
        )
        assert fs.revenue_yi == 120.5
        assert fs.analyst_report_count == 28
        d = fs.model_dump()
        assert d["data_source"] == "akshare_ths"
        assert d["consensus_pe"] == 25.3

    def test_serialization_roundtrip(self):
        fs = FinancialSnapshot(data_source="yfinance", revenue_yi=50.0)
        json_str = fs.model_dump_json()
        fs2 = FinancialSnapshot.model_validate_json(json_str)
        assert fs2.data_source == "yfinance"
        assert fs2.revenue_yi == 50.0
        assert fs2.net_profit_yi is None


class TestAlphaScore:
    def test_create_defaults(self):
        alpha = AlphaScore()
        assert alpha.market_attention == 0.0
        assert alpha.alpha_score == 0.0
        assert alpha.reasoning == ""

    def test_create_full(self):
        alpha = AlphaScore(
            market_attention=3.0,
            information_gap=8.0,
            alpha_score=7.5,
            reasoning="低关注度 + 高瓶颈得分",
        )
        assert alpha.alpha_score == 7.5

    def test_validation_bounds(self):
        with pytest.raises(Exception):
            AlphaScore(market_attention=11.0)
        with pytest.raises(Exception):
            AlphaScore(information_gap=-1.0)


class TestScorecardWithFinancialData:
    def _make_scorecard(self, with_financial=False, with_alpha=False) -> SupplierScorecard:
        supplier = SupplierInfo(
            name="中微公司", ticker="688012", market=MarketRegion.A_STOCK,
            sector="半导体设备", description="刻蚀设备龙头",
        )
        kwargs = dict(
            supplier=supplier, bottleneck_node="刻蚀设备",
            market_position=9, customer_validation=8, capacity_status=7,
            financial_health=8, valuation=6, overall_score=7.6,
        )
        if with_financial:
            kwargs["financial_snapshot"] = FinancialSnapshot(
                data_source="akshare_ths", report_date="2025-12-31",
                revenue_yi=80.5, net_profit_yi=15.2, gross_margin_pct=42.0,
            )
        if with_alpha:
            kwargs["alpha"] = AlphaScore(
                market_attention=4.0, information_gap=7.0,
                alpha_score=6.5, reasoning="中等关注度但信息差大",
            )
        return SupplierScorecard(**kwargs)

    def test_backward_compatible_no_financial(self):
        sc = self._make_scorecard()
        assert sc.financial_snapshot is None
        assert sc.alpha is None
        d = sc.model_dump()
        assert d["financial_snapshot"] is None
        assert d["alpha"] is None
        assert "dimension_scores" in d

    def test_with_financial_snapshot(self):
        sc = self._make_scorecard(with_financial=True)
        assert sc.financial_snapshot is not None
        assert sc.financial_snapshot.revenue_yi == 80.5
        d = sc.model_dump()
        assert d["financial_snapshot"]["data_source"] == "akshare_ths"

    def test_with_alpha(self):
        sc = self._make_scorecard(with_alpha=True)
        assert sc.alpha.alpha_score == 6.5
        d = sc.model_dump()
        assert d["alpha"]["reasoning"] == "中等关注度但信息差大"

    def test_full_serialization_roundtrip(self):
        sc = self._make_scorecard(with_financial=True, with_alpha=True)
        json_str = sc.model_dump_json()
        sc2 = SupplierScorecard.model_validate_json(json_str)
        assert sc2.financial_snapshot.revenue_yi == 80.5
        assert sc2.alpha.alpha_score == 6.5
        assert sc2.overall_score == 7.6


class TestCrossValidationReport:
    def test_create_validation(self):
        v = ModelValidation(
            model_name="gpt-5.5",
            score=8.0,
            reasoning="垄断地位确认",
            concerns=[],
        )
        assert v.score == 8.0

    def test_validation_score_range(self):
        v = ModelValidation(model_name="test", score=1, reasoning="")
        assert v.score == 1
        v2 = ModelValidation(model_name="test", score=10, reasoning="")
        assert v2.score == 10

    def test_cross_validation_report(self):
        cv = CrossValidationReport(
            supplier_name="测试公司",
            ticker="000001.SZ",
            validations=[
                ModelValidation(model_name="m1", score=8, reasoning="好"),
                ModelValidation(model_name="m2", score=6, reasoning="一般"),
            ],
            consensus_score=7.0,
            consensus_reasoning="观点分化",
            avg_score=7.0,
        )
        assert cv.avg_score == 7.0
        assert cv.consensus_score == 7.0
