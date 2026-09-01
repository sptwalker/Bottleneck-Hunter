"""Tests for chain decomposer."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from bottleneck_hunter.chain.decomposer import ChainDecomposer, _market_code_allowed, _normalize_companies
from bottleneck_hunter.chain.models import LayerType, MarketRegion


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


class TestMarketCodeGate:
    """入围环节硬门槛：非主流市场代码在产业链拆解层就被拦下，杜绝 RNECY/HAM 类混入下游。"""

    def test_us_mainboard_ticker_allowed(self):
        assert _market_code_allowed("NVDA", MarketRegion.US_STOCK) is True
        assert _market_code_allowed("AAPL", MarketRegion.US_STOCK) is True

    def test_astock_mainboard_code_allowed(self):
        # 沪6/深0或3/北交所4或8
        assert _market_code_allowed("688981", MarketRegion.A_STOCK) is True
        assert _market_code_allowed("000001", MarketRegion.A_STOCK) is True
        assert _market_code_allowed("300750", MarketRegion.A_STOCK) is True
        assert _market_code_allowed("830799", MarketRegion.A_STOCK) is True

    def test_astock_rejects_us_ticker_and_adr(self):
        assert _market_code_allowed("NVDA", MarketRegion.A_STOCK) is False
        assert _market_code_allowed("MUZE.O", MarketRegion.A_STOCK) is False

    def test_us_rejects_digits_and_foreign_suffix(self):
        assert _market_code_allowed("688981", MarketRegion.US_STOCK) is False
        assert _market_code_allowed("MUZE.O", MarketRegion.US_STOCK) is False
        assert _market_code_allowed("RENN.K", MarketRegion.US_STOCK) is False

    def test_all_market_accepts_either_mainboard_form(self):
        assert _market_code_allowed("NVDA", MarketRegion.ALL) is True
        assert _market_code_allowed("688981", MarketRegion.ALL) is True

    def test_rejects_empty_otc_and_dot_suffix(self):
        assert _market_code_allowed("", MarketRegion.US_STOCK) is False
        # 粉单后缀 / 点号类股 / 带交易所后缀 ADR：形态即非主流，本层拦下
        assert _market_code_allowed("RNECY.PK", MarketRegion.US_STOCK) is False
        assert _market_code_allowed("BRK.B", MarketRegion.US_STOCK) is False
        assert _market_code_allowed("MUZE.O", MarketRegion.US_STOCK) is False

    def test_normalize_companies_blocks_dot_suffix_adr(self):
        # RNECY/HAM 是「形式合法、行情非法」——由下游数据层(exchange/quoteType)拦；
        # 形态层面只拦带后缀/粉单/OTC 等真正非主流的写法。
        raw = [
            {"name": "英伟达", "code": "NVDA"},
            {"name": "美光ADR", "code": "MUZE.O"},
            {"name": "瑞萨粉单", "code": "RNECY.PK"},
        ]
        us = _normalize_companies(raw, MarketRegion.US_STOCK)
        codes = [c["code"] for c in us]
        assert "NVDA" in codes
        assert "MUZE.O" not in codes
        assert "RNECY.PK" not in codes

    def test_normalize_companies_astock_rejects_us_ticker(self):
        raw = [
            {"name": "中芯国际", "code": "688981"},
            {"name": "英伟达", "code": "NVDA"},
        ]
        astock = _normalize_companies(raw, MarketRegion.A_STOCK)
        codes = [c["code"] for c in astock]
        assert "688981" in codes
        assert "NVDA" not in codes

    @pytest.mark.asyncio
    async def test_normalize_companies_encodes_market_into_decompose(self):
        """拆解时传入 market，带后缀 ADR 等非主流形态应在节点层被剔除，而不是进入图。"""
        llm = _mock_llm([
            {
                "name": "HBM",
                "description": "d",
                "function": "f",
                "key_parameters": [],
                "upstream_deps": [],
                "dependency": 0.5,
                "alternatives": 0,
                "notes": "",
                "representative_companies": [
                    {"name": "美光ADR", "code": "MUZE.O"},
                    {"name": "英伟达", "code": "NVDA"},
                ],
            }
        ])
        decomposer = ChainDecomposer(llm=llm, max_depth=1, sector="GPU", market="us_stock")
        graph = await decomposer.decompose("GPU")
        node = graph.get_node("HBM")
        assert node is not None
        # 只有 NVDA 通过入围门，MUZE.O 被剔除
        assert [c["code"] for c in node.representative_companies] == ["NVDA"]
