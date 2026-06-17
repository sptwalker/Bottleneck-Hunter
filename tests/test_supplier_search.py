"""Tests for multi-source supplier search."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.chain.models import (
    BottleneckReport,
    BottleneckScore,
    ChainGraph,
    ChainLink,
    IndustryNode,
    MarketRegion,
    SupplierInfo,
)
from bottleneck_hunter.chain.supplier_search import SupplierSearcher, _try_akshare_search


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_bottleneck(name="HBM存储器", desc="高带宽内存", insights=None):
    return BottleneckReport(
        node_name=name,
        node_description=desc,
        layer=2,
        scores=[
            BottleneckScore(dimension="scarcity", score=8.0, reasoning="x"),
            BottleneckScore(dimension="irreplaceability", score=7.0, reasoning="x"),
        ],
        overall_score=7.5,
        key_insights=insights or ["供应商高度集中", "技术壁垒极高"],
    )


def _make_chain_graph():
    """构建包含 representative_companies 的产业链图。"""
    return ChainGraph(
        sector="GPU/AI算力",
        end_product="GPU",
        nodes=[
            IndustryNode(
                name="GPU芯片",
                description="图形处理芯片",
                layer=0,
                layer_type="end_product",
                function="核心计算",
                representative_companies=[
                    {"name": "英伟达", "code": "NVDA"},
                    {"name": "AMD", "code": "AMD"},
                ],
            ),
            IndustryNode(
                name="HBM存储器",
                description="高带宽内存",
                layer=1,
                layer_type="component",
                function="高速存储",
                representative_companies=[
                    {"name": "澜起科技", "code": "688008"},
                    {"name": "兆易创新", "code": "603986"},
                    {"name": "无代码公司", "code": ""},
                ],
            ),
            IndustryNode(
                name="硅片衬底",
                description="半导体硅片",
                layer=2,
                layer_type="material",
                function="芯片基底材料",
                representative_companies=[
                    {"name": "沪硅产业", "code": "688126"},
                ],
            ),
        ],
        links=[
            ChainLink(upstream="HBM存储器", downstream="GPU芯片", dependency=0.9, alternatives=0),
            ChainLink(upstream="硅片衬底", downstream="HBM存储器", dependency=0.7, alternatives=2),
        ],
    )


def _make_llm_response(items: list[dict]) -> str:
    import json
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tests: _extract_chain_candidates
# ---------------------------------------------------------------------------

class TestExtractChainCandidates:
    def test_extracts_astock_companies(self):
        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)
        bn = _make_bottleneck()
        chain = _make_chain_graph()
        candidates = searcher._extract_chain_candidates(bn, chain)
        tickers = {c.ticker for c in candidates}
        assert "688008.SS" in tickers
        assert "603986.SS" in tickers
        assert all(c.source == "chain" for c in candidates)
        assert all(c.market == MarketRegion.A_STOCK for c in candidates)

    def test_filters_us_stock_in_astock_mode(self):
        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)
        bn = _make_bottleneck(name="GPU芯片")
        chain = _make_chain_graph()
        candidates = searcher._extract_chain_candidates(bn, chain)
        tickers = {c.ticker for c in candidates}
        assert "NVDA" not in tickers
        assert "AMD" not in tickers

    def test_includes_us_stock_in_all_mode(self):
        searcher = SupplierSearcher(market=MarketRegion.ALL)
        bn = _make_bottleneck(name="GPU芯片")
        chain = _make_chain_graph()
        candidates = searcher._extract_chain_candidates(bn, chain)
        tickers = {c.ticker for c in candidates}
        assert "NVDA" in tickers
        assert "AMD" in tickers

    def test_skips_empty_code(self):
        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)
        bn = _make_bottleneck()
        chain = _make_chain_graph()
        candidates = searcher._extract_chain_candidates(bn, chain)
        names = {c.name for c in candidates}
        assert "无代码公司" not in names

    def test_no_chain_graph(self):
        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)
        bn = _make_bottleneck()
        candidates = searcher._extract_chain_candidates(bn, ChainGraph(
            sector="test", end_product="test", nodes=[], links=[],
        ))
        assert candidates == []

    def test_dedup_same_ticker(self):
        """同一 ticker 在多个节点出现时只保留一次。"""
        chain = ChainGraph(
            sector="test",
            end_product="GPU",
            nodes=[
                IndustryNode(
                    name="HBM存储器", description="x", layer=1, layer_type="component",
                    function="x",
                    representative_companies=[{"name": "澜起科技", "code": "688008"}],
                ),
                IndustryNode(
                    name="DDR控制器", description="x", layer=2, layer_type="sub_component",
                    function="x",
                    upstream_deps=["HBM存储器"],
                    representative_companies=[{"name": "澜起科技", "code": "688008"}],
                ),
            ],
            links=[ChainLink(upstream="DDR控制器", downstream="HBM存储器", dependency=0.5, alternatives=2)],
        )
        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)
        bn = _make_bottleneck()
        candidates = searcher._extract_chain_candidates(bn, chain)
        assert len(candidates) == 1


# ---------------------------------------------------------------------------
# Tests: multi-source search merge
# ---------------------------------------------------------------------------

class TestMultiSourceSearch:
    @pytest.mark.asyncio
    async def test_three_source_merge(self):
        """三源合并：LLM + AKShare + chain，按 ticker 去重。"""
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=_make_llm_response([
                {"name": "澜起科技", "code": "688008", "sector": "半导体", "description": "x", "key_products": ["DDR5"]},
                {"name": "兆易创新", "code": "603986", "sector": "存储", "description": "x", "key_products": ["NOR Flash"]},
            ])
        )

        tencent_quotes = {
            "688008": {"name": "澜起科技", "total_mcap_yi": 150, "pe": 35},
            "603986": {"name": "兆易创新", "total_mcap_yi": 180, "pe": 40},
            "688126": {"name": "沪硅产业", "total_mcap_yi": 90, "pe": 50},
        }

        chain = _make_chain_graph()
        bn = _make_bottleneck()

        searcher = SupplierSearcher(
            market=MarketRegion.A_STOCK,
            max_market_cap_yi=500,
            llm=mock_llm,
        )

        with patch(
            "bottleneck_hunter.chain.supplier_search.fetch_tencent_quotes",
            return_value=tencent_quotes,
        ), patch(
            "bottleneck_hunter.chain.supplier_search._try_akshare_search",
            return_value=[],
        ):
            suppliers = await searcher.search(bn, chain_graph=chain)

        tickers = {s.ticker for s in suppliers}
        assert "688008.SS" in tickers
        assert "603986.SS" in tickers
        assert "688126.SS" in tickers

    @pytest.mark.asyncio
    async def test_llm_priority_over_chain(self):
        """LLM 候选的 description 和 key_products 应优先保留。"""
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=_make_llm_response([
                {"name": "澜起科技", "code": "688008", "sector": "半导体",
                 "description": "DDR5接口芯片龙头", "key_products": ["DDR5 RCD"]},
            ])
        )

        tencent_quotes = {
            "688008": {"name": "澜起科技", "total_mcap_yi": 150, "pe": 35},
        }

        chain = _make_chain_graph()
        bn = _make_bottleneck()

        searcher = SupplierSearcher(
            market=MarketRegion.A_STOCK,
            llm=mock_llm,
        )

        with patch(
            "bottleneck_hunter.chain.supplier_search.fetch_tencent_quotes",
            return_value=tencent_quotes,
        ), patch(
            "bottleneck_hunter.chain.supplier_search._try_akshare_search",
            return_value=[],
        ):
            suppliers = await searcher.search(bn, chain_graph=chain)

        found = [s for s in suppliers if "688008" in s.ticker]
        assert len(found) == 1
        assert found[0].source == "llm"
        assert "DDR5" in found[0].description or "DDR5 RCD" in found[0].key_products

    @pytest.mark.asyncio
    async def test_chain_only_no_llm(self):
        """没有 LLM 时仅使用产业链候选。"""
        chain = _make_chain_graph()
        bn = _make_bottleneck()

        tencent_quotes = {
            "688008": {"name": "澜起科技", "total_mcap_yi": 150, "pe": 35},
            "603986": {"name": "兆易创新", "total_mcap_yi": 180, "pe": 40},
        }

        searcher = SupplierSearcher(market=MarketRegion.A_STOCK)

        with patch(
            "bottleneck_hunter.chain.supplier_search._try_akshare_search",
            return_value=[],
        ):
            suppliers = await searcher.search(bn, chain_graph=chain)

        assert len(suppliers) >= 1
        assert all(s.source == "chain" for s in suppliers)

    @pytest.mark.asyncio
    async def test_source_stats_in_progress(self):
        """验证进度消息包含各来源统计。"""
        mock_llm = AsyncMock()
        mock_llm.ainvoke.return_value = MagicMock(
            content=_make_llm_response([
                {"name": "澜起科技", "code": "688008", "sector": "半导体", "description": "x", "key_products": []},
            ])
        )

        tencent_quotes = {
            "688008": {"name": "澜起科技", "total_mcap_yi": 150, "pe": 35},
            "600584": {"name": "长电科技", "total_mcap_yi": 120, "pe": 25},
        }

        chain = _make_chain_graph()
        bn = _make_bottleneck()

        messages = []

        async def capture_progress(msg):
            messages.append(msg)

        searcher = SupplierSearcher(
            market=MarketRegion.A_STOCK,
            llm=mock_llm,
        )
        searcher._on_progress = capture_progress

        with patch(
            "bottleneck_hunter.chain.supplier_search.fetch_tencent_quotes",
            return_value=tencent_quotes,
        ), patch(
            "bottleneck_hunter.chain.supplier_search._try_akshare_search",
            return_value=[],
        ):
            await searcher.search(bn, chain_graph=chain)

        combined = " ".join(messages)
        assert "LLM" in combined
        assert "去重后" in combined


# ---------------------------------------------------------------------------
# Tests: SupplierInfo source field
# ---------------------------------------------------------------------------

class TestSupplierInfoSource:
    def test_default_source(self):
        s = SupplierInfo(
            name="测试", ticker="000001.SZ", market=MarketRegion.A_STOCK,
            sector="", description="",
        )
        assert s.source == "llm"

    def test_custom_source(self):
        s = SupplierInfo(
            name="测试", ticker="000001.SZ", market=MarketRegion.A_STOCK,
            sector="", description="", source="akshare",
        )
        assert s.source == "akshare"

    def test_source_in_serialization(self):
        s = SupplierInfo(
            name="测试", ticker="000001.SZ", market=MarketRegion.A_STOCK,
            sector="", description="", source="chain",
        )
        d = s.model_dump()
        assert d["source"] == "chain"
