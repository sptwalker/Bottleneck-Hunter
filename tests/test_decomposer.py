"""Tests for chain decomposer."""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from bottleneck_hunter.chain.decomposer import ChainDecomposer
from bottleneck_hunter.chain.models import LayerType


def _mock_llm(response_json: list[dict]):
    """Create a mock LLM that returns the given JSON."""
    llm = AsyncMock()
    msg = MagicMock()
    msg.content = json.dumps(response_json, ensure_ascii=False)
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


class TestChainDecomposer:
    @pytest.mark.asyncio
    async def test_decompose_single_layer(self):
        llm = _mock_llm([
            {
                "name": "HBM",
                "description": "高带宽内存",
                "function": "存储",
                "key_parameters": ["带宽"],
                "upstream_deps": [],
                "dependency": 0.9,
                "alternatives": 1,
                "notes": "",
            }
        ])
        decomposer = ChainDecomposer(llm=llm, max_depth=1, sector="GPU")
        graph = await decomposer.decompose("GPU")

        assert graph.end_product == "GPU"
        assert len(graph.nodes) == 2  # GPU + HBM
        assert graph.get_node("GPU") is not None
        assert graph.get_node("HBM") is not None
        assert len(graph.links) == 1

    @pytest.mark.asyncio
    async def test_decompose_preserves_root(self):
        llm = _mock_llm([])
        decomposer = ChainDecomposer(llm=llm, max_depth=1, sector="GPU")
        graph = await decomposer.decompose("GPU")

        root = graph.get_node("GPU")
        assert root is not None
        assert root.layer == 0
        assert root.layer_type == LayerType.END_PRODUCT

    @pytest.mark.asyncio
    async def test_decompose_handles_markdown_fences(self):
        llm = AsyncMock()
        msg = MagicMock()
        msg.content = '```json\n[{"name": "X", "description": "D", "function": "F", "key_parameters": [], "upstream_deps": [], "dependency": 0.5, "alternatives": 0, "notes": ""}]\n```'
        llm.ainvoke = AsyncMock(return_value=msg)

        decomposer = ChainDecomposer(llm=llm, max_depth=1, sector="Test")
        graph = await decomposer.decompose("Test")

        assert graph.get_node("X") is not None
