"""Tests for ChainGraph 模型方法（models.py 中定义的图遍历）。"""

import pytest

from bottleneck_hunter.chain.models import (
    ChainGraph,
    ChainLink,
    IndustryNode,
    LayerType,
)


def _node(name, layer=0, lt=None):
    return IndustryNode(
        name=name, description=f"{name} desc", layer=layer,
        layer_type=lt or (LayerType.END_PRODUCT if layer == 0 else LayerType.COMPONENT),
        function=f"{name} function",
    )


def _link(upstream, downstream, dep=0.8):
    return ChainLink(upstream=upstream, downstream=downstream, dependency=dep, alternatives=1)


def _graph(nodes, links=None):
    return ChainGraph(sector="test", end_product="test", nodes=nodes, links=links or [])


class TestGetNode:
    def test_found(self):
        g = _graph([_node("A"), _node("B", 1)])
        assert g.get_node("A").name == "A"

    def test_not_found(self):
        g = _graph([_node("A")])
        assert g.get_node("X") is None

    def test_empty_graph(self):
        g = _graph([])
        assert g.get_node("A") is None


class TestGetNodesAtLayer:
    def test_single_layer(self):
        g = _graph([_node("A", 0), _node("B", 1), _node("C", 1)])
        result = g.get_nodes_at_layer(1)
        names = [n.name for n in result]
        assert sorted(names) == ["B", "C"]

    def test_no_nodes_at_layer(self):
        g = _graph([_node("A", 0)])
        assert g.get_nodes_at_layer(5) == []

    def test_layer_zero(self):
        g = _graph([_node("Root", 0), _node("X", 1)])
        result = g.get_nodes_at_layer(0)
        assert len(result) == 1
        assert result[0].name == "Root"


class TestGetUpstream:
    def test_has_upstream(self):
        g = _graph(
            [_node("GPU", 0), _node("光模块", 1)],
            [_link("GPU", "光模块")],
        )
        upstream = g.get_upstream("光模块")
        assert len(upstream) == 1
        assert upstream[0].name == "GPU"

    def test_no_upstream(self):
        g = _graph([_node("Root", 0)], [])
        assert g.get_upstream("Root") == []

    def test_multiple_upstream(self):
        g = _graph(
            [_node("A", 0), _node("B", 0), _node("C", 1)],
            [_link("A", "C"), _link("B", "C")],
        )
        upstream = g.get_upstream("C")
        names = sorted(n.name for n in upstream)
        assert names == ["A", "B"]


class TestGetDownstream:
    def test_has_downstream(self):
        g = _graph(
            [_node("A", 0), _node("B", 1), _node("C", 1)],
            [_link("A", "B"), _link("A", "C")],
        )
        downstream = g.get_downstream("A")
        names = sorted(n.name for n in downstream)
        assert names == ["B", "C"]

    def test_no_downstream(self):
        g = _graph([_node("Leaf", 2, LayerType.MATERIAL)], [])
        assert g.get_downstream("Leaf") == []

    def test_nonexistent_node(self):
        g = _graph([_node("A", 0)], [])
        assert g.get_downstream("Z") == []


class TestChainTraversal:
    def test_three_layer_chain(self):
        """GPU → 光模块 → 磷化铟"""
        g = _graph(
            [_node("GPU", 0), _node("光模块", 1), _node("磷化铟", 2, LayerType.MATERIAL)],
            [_link("GPU", "光模块"), _link("光模块", "磷化铟")],
        )
        assert g.get_node("GPU").layer == 0
        assert g.get_node("磷化铟").layer == 2
        assert g.get_upstream("磷化铟")[0].name == "光模块"
        assert g.get_downstream("GPU")[0].name == "光模块"
        assert g.get_downstream("光模块")[0].name == "磷化铟"

    def test_diamond_dependency(self):
        """A → B, A → C, B → D, C → D"""
        g = _graph(
            [_node("A", 0), _node("B", 1), _node("C", 1), _node("D", 2, LayerType.MATERIAL)],
            [_link("A", "B"), _link("A", "C"), _link("B", "D"), _link("C", "D")],
        )
        upstream_d = g.get_upstream("D")
        names = sorted(n.name for n in upstream_d)
        assert names == ["B", "C"]
        downstream_a = g.get_downstream("A")
        names = sorted(n.name for n in downstream_a)
        assert names == ["B", "C"]
