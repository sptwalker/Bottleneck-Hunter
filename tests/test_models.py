"""Tests for chain data models."""

import pytest

from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
    ChainLink,
    CrossValidationReport,
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


class TestCrossValidationReport:
    def test_create_validation(self):
        v = ModelValidation(
            model_name="gpt-5.5",
            result=ValidationResult.PASS,
            reasoning="垄断地位确认",
            concerns=[],
        )
        assert v.result == "pass"

    def test_validation_result_values(self):
        assert ValidationResult.PASS == "pass"
        assert ValidationResult.CONCERN == "concern"
        assert ValidationResult.FAIL == "fail"
