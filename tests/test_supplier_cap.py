"""市值上限过滤：阈值统一为「亿(原生货币)」，美股候选存的是 $B 需 /10 换算。"""
from bottleneck_hunter.chain.models import MarketRegion, SupplierInfo
from bottleneck_hunter.chain.supplier_search import SupplierSearcher


def _sup(mc):
    return SupplierInfo(name="x", ticker="X", market="us_stock", sector="s", description="d", market_cap=mc)


def test_us_cap_threshold_is_yi_not_billion():
    # 300亿美元 = 30 $B：25$B(=250亿) 通过，50$B(=500亿) 过滤
    us = SupplierSearcher(market=MarketRegion.US_STOCK, max_market_cap_yi=300, llm=None)
    kept = us._apply_cap_filter([_sup(25), _sup(50)], "node")
    assert [s.market_cap for s in kept] == [25]


def test_astock_cap_threshold_direct():
    a = SupplierSearcher(market=MarketRegion.A_STOCK, max_market_cap_yi=500, llm=None)
    kept = a._apply_cap_filter([_sup(300), _sup(600)], "node")
    assert [s.market_cap for s in kept] == [300]


if __name__ == "__main__":
    test_us_cap_threshold_is_yi_not_billion()
    test_astock_cap_threshold_direct()
    print("PASS")
