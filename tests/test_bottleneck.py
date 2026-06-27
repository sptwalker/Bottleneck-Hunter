"""Tests for bottleneck.py — 瓶颈评分算法。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.chain.bottleneck import (
    DEFAULT_WEIGHTS,
    BottleneckAnalyzer,
)
from bottleneck_hunter.chain.models import (
    BottleneckDimension,
    BottleneckScore,
    ChainGraph,
    ChainLink,
    IndustryNode,
    LayerType,
)

_LAYER_TYPES = {0: LayerType.END_PRODUCT, 1: LayerType.COMPONENT, 2: LayerType.MATERIAL}


def _make_graph(nodes_data: list[tuple[str, str, int]], links: list[tuple[str, str]] | None = None) -> ChainGraph:
    """快速构建测试用 ChainGraph。"""
    nodes = [
        IndustryNode(
            name=n, description=d, layer=l,
            layer_type=_LAYER_TYPES.get(l, LayerType.COMPONENT),
            function=d,
        )
        for n, d, l in nodes_data
    ]
    chain_links = []
    if links:
        chain_links = [ChainLink(upstream=s, downstream=t, dependency=0.8, alternatives=1) for s, t in links]
    return ChainGraph(sector="测试", end_product="测试产品", nodes=nodes, links=chain_links)


def _mock_llm_response(scores: dict[str, float], key_insights: list[str] | None = None):
    """构造 LLM JSON 响应内容。"""
    import json
    dims = []
    for dim_name, score in scores.items():
        dims.append({"dimension": dim_name, "score": score, "reasoning": f"{dim_name} test"})
    resp = {
        "scores": dims,
        "key_insights": key_insights or ["insight1"],
        "risks": ["risk1"],
    }
    return json.dumps(resp, ensure_ascii=False)


class TestDefaultWeights:
    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_WEIGHTS.values())
        assert abs(total - 1.0) < 1e-6

    def test_all_dimensions_have_weights(self):
        for dim in BottleneckDimension:
            assert dim in DEFAULT_WEIGHTS

    def test_all_weights_positive(self):
        for w in DEFAULT_WEIGHTS.values():
            assert w > 0


class TestWeightedScore:
    def _make_analyzer(self):
        llm = MagicMock()
        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test prompt"):
            return BottleneckAnalyzer(llm=llm)

    def test_all_dimensions_scored(self):
        analyzer = self._make_analyzer()
        scores = [
            BottleneckScore(dimension=BottleneckDimension.SCARCITY, score=8.0, reasoning="r"),
            BottleneckScore(dimension=BottleneckDimension.IRREPLACEABILITY, score=6.0, reasoning="r"),
            BottleneckScore(dimension=BottleneckDimension.SUPPLY_DEMAND_GAP, score=7.0, reasoning="r"),
            BottleneckScore(dimension=BottleneckDimension.PRICING_POWER, score=5.0, reasoning="r"),
            BottleneckScore(dimension=BottleneckDimension.TECH_BARRIER, score=9.0, reasoning="r"),
        ]
        result = analyzer._weighted_score(scores)
        expected = (8.0 * 0.25 + 6.0 * 0.25 + 7.0 * 0.20 + 5.0 * 0.15 + 9.0 * 0.15)
        assert abs(result - expected) < 1e-6

    def test_single_dimension(self):
        analyzer = self._make_analyzer()
        scores = [BottleneckScore(dimension=BottleneckDimension.SCARCITY, score=10.0, reasoning="r")]
        result = analyzer._weighted_score(scores)
        expected = 10.0 * 0.25 / 1.0
        assert abs(result - expected) < 1e-6

    def test_empty_scores(self):
        analyzer = self._make_analyzer()
        result = analyzer._weighted_score([])
        assert result == 0

    def test_custom_weights(self):
        llm = MagicMock()
        custom = {BottleneckDimension.SCARCITY: 1.0}
        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test prompt"):
            analyzer = BottleneckAnalyzer(llm=llm, weights=custom)
        scores = [BottleneckScore(dimension=BottleneckDimension.SCARCITY, score=7.5, reasoning="r")]
        assert analyzer._weighted_score(scores) == 7.5


class TestBuildContext:
    def _make_analyzer(self):
        llm = MagicMock()
        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test prompt"):
            return BottleneckAnalyzer(llm=llm)

    def test_context_includes_node_name(self):
        analyzer = self._make_analyzer()
        graph = _make_graph(
            [("GPU", "终端产品", 0), ("光模块", "关键组件", 1)],
            [("GPU", "光模块")],
        )
        ctx = analyzer._build_context("光模块", graph)
        assert "光模块" in ctx

    def test_context_with_upstream(self):
        analyzer = self._make_analyzer()
        graph = _make_graph(
            [("GPU", "终端产品", 0), ("光模块", "组件", 1), ("磷化铟", "材料", 2)],
            [("GPU", "光模块"), ("光模块", "磷化铟")],
        )
        ctx = analyzer._build_context("光模块", graph)
        assert "GPU" in ctx or "磷化铟" in ctx

    def test_no_upstream_downstream(self):
        analyzer = self._make_analyzer()
        graph = _make_graph([("孤立节点", "无连接", 1)])
        ctx = analyzer._build_context("孤立节点", graph)
        assert "孤立节点" in ctx


class TestAnalyze:
    async def test_empty_graph_returns_empty(self):
        llm = MagicMock()
        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test prompt"):
            analyzer = BottleneckAnalyzer(llm=llm)
        graph = _make_graph([("Root", "终端", 0)])
        result = await analyzer.analyze(graph)
        assert result == []

    async def test_single_node_analyzed(self):
        mock_resp = _mock_llm_response({
            "scarcity": 8.0, "irreplaceability": 7.0,
            "supply_demand_gap": 6.0, "pricing_power": 5.0,
            "tech_barrier": 9.0,
        })
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=mock_resp))

        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test"):
            analyzer = BottleneckAnalyzer(llm=llm)

        graph = _make_graph(
            [("Root", "产品", 0), ("组件A", "关键", 1)],
            [("Root", "组件A")],
        )
        results = await analyzer.analyze(graph)
        assert len(results) == 1
        assert results[0].node_name == "组件A"
        assert results[0].rank == 1
        assert results[0].overall_score > 0

    async def test_results_sorted_by_score(self):
        def make_resp(scarcity):
            return _mock_llm_response({
                "scarcity": scarcity, "irreplaceability": 5.0,
                "supply_demand_gap": 5.0, "pricing_power": 5.0,
                "tech_barrier": 5.0,
            })

        call_count = 0

        async def mock_ainvoke(prompt):
            nonlocal call_count
            call_count += 1
            score = 10.0 if call_count == 2 else 3.0
            return MagicMock(content=make_resp(score))

        llm = MagicMock()
        llm.ainvoke = mock_ainvoke

        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test"):
            analyzer = BottleneckAnalyzer(llm=llm)

        graph = _make_graph(
            [("Root", "产品", 0), ("A", "低分", 1), ("B", "高分", 1)],
            [("Root", "A"), ("Root", "B")],
        )
        results = await analyzer.analyze(graph)
        assert len(results) == 2
        assert results[0].overall_score >= results[1].overall_score
        assert results[0].rank == 1
        assert results[1].rank == 2

    async def test_failed_node_tracked(self):
        llm = MagicMock()
        llm.ainvoke = AsyncMock(side_effect=Exception("LLM down"))

        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test"):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                analyzer = BottleneckAnalyzer(llm=llm)
                graph = _make_graph(
                    [("Root", "产品", 0), ("组件", "坏的", 1)],
                    [("Root", "组件")],
                )
                results = await analyzer.analyze(graph)
                assert results == []
                assert len(analyzer.failed_nodes) > 0

    async def test_progress_callback(self):
        mock_resp = _mock_llm_response({"scarcity": 5.0, "irreplaceability": 5.0,
                                         "supply_demand_gap": 5.0, "pricing_power": 5.0,
                                         "tech_barrier": 5.0})
        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=MagicMock(content=mock_resp))

        progress_calls = []

        async def on_progress(msg):
            progress_calls.append(msg)

        with patch("bottleneck_hunter.chain.bottleneck._load_prompt", return_value="test"):
            analyzer = BottleneckAnalyzer(llm=llm)
            graph = _make_graph(
                [("Root", "产品", 0), ("A", "组件", 1)],
                [("Root", "A")],
            )
            await analyzer.analyze(graph, on_progress=on_progress)
            assert len(progress_calls) > 0
