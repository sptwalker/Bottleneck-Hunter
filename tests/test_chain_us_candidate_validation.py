# -*- coding: utf-8 -*-
"""回归：产业链图谱美股候选必须过 yfinance 行情校验，OTC 冷门/错配码在源头剔除。
真实数据活体：RNECY(瑞萨 ADR)/HAM 取不到报价必被剔，NVDA 必留。"""
import asyncio
from bottleneck_hunter.chain.models import (
    BottleneckReport, BottleneckScore, ChainGraph, IndustryNode, LayerType, MarketRegion,
)
from bottleneck_hunter.chain.supplier_search import SupplierSearcher


def _make_chain():
    node = IndustryNode(
        name="AI算力芯片", layer=1, layer_type=LayerType.COMPONENT,
        description="AI 加速芯片环节", function="AI 训练/推理加速",
        representative_companies=[
            {"name": "NVIDIA", "code": "NVDA"},        # 真实美股 → 必留
            {"name": "Renesas Electronics", "code": "RNECY"},  # 日本公司 OTC ADR → 必剔
            {"name": "Some Corp", "code": "HAM"},      # 取不到报价 → 必剔
        ],
    )
    return ChainGraph(sector="AI算力", end_product="AI服务器", nodes=[node])


def test_chain_us_candidates_validated():
    eng = SupplierSearcher(market=MarketRegion.US_STOCK)
    bn = BottleneckReport(
        node_name="AI算力芯片", node_description="AI 加速芯片", layer=1,
        scores=[], overall_score=8.0,
    )
    chain = _make_chain()
    out = asyncio.run(eng._extract_chain_candidates(bn, chain))
    tickers = {s.ticker for s in out}
    print("保留:", tickers)
    assert "NVDA" in tickers, "真实美股 NVDA 应保留"
    assert "RNECY" not in tickers, "OTC 冷门 ADR RNECY 应被行情校验剔除"
    assert "HAM" not in tickers, "取不到报价的 HAM 应被剔除"
    # NVDA 应带上 market_cap
    nvda = next(s for s in out if s.ticker == "NVDA")
    assert nvda.market_cap and nvda.market_cap > 0, "校验通过的候选应回填 market_cap"
    print("PASS: 产业链美股候选行情校验闸生效")


if __name__ == "__main__":
    test_chain_us_candidates_validated()
