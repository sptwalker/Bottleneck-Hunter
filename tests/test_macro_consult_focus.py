"""聚焦个股深度资料块 `_focus_ticker_block` 自检：

- 有 profile+snapshot+chip+news+earnings → 块含 PE/目标价/新闻标题/eps惊喜关键字段
- 空库（各 getter 返回空）→ 诚实占位「暂无…建议先加入观察池」，不抛异常
- ticker 不在 list_all → 催化剂段略过、其余仍在

_focus_ticker_block 纯读库、任一子项失败降级为空，故用 fake store 覆盖即可。
"""
from bottleneck_hunter.watchlist.macro_consultation import _focus_ticker_block


class _FullStore:
    """凑齐 _focus_ticker_block + _chip_context 所需的全部读取器。"""
    def get_company_profile(self, t):
        return {"company_name": "英伟达", "sector": "半导体",
                "raw": {"trailingPE": 45.2, "forwardPE": 30.1, "priceToBook": 40.0,
                        "enterpriseToEbitda": 38.0, "enterpriseToRevenue": 25.0,
                        "returnOnEquity": 0.55, "returnOnAssets": 0.30,
                        "revenueGrowth": 1.22, "totalRevenue": 6.0e10,
                        "netIncomeToCommon": 3.0e10, "grossMargins": 0.75,
                        "operatingMargins": 0.62, "ebitdaMargins": 0.65,
                        "operatingCashflow": 2.8e10, "freeCashflow": 2.5e10,
                        "totalCash": 3.0e10, "totalDebt": 1.0e10, "debtToEquity": 25.0,
                        "currentRatio": 4.2, "beta": 1.7, "marketCap": 3.2e12}}

    def get_latest_snapshot(self, t):
        return {"close": 120.5, "market_cap": 3.2e12, "change_pct": 1.8,
                "rsi_14": 61.0, "date": "2026-08-11"}

    def get_institutional_holders(self, t, limit=50):
        return [{"holder_name": "Vanguard", "pct_held": 8.1},
                {"holder_name": "BlackRock", "pct_held": 7.3}]

    def get_analyst_ratings(self, t, limit=50):
        return [{"rating": "buy", "target_price": 160.0}, {"rating": "buy", "target_price": 150.0}]

    def get_news(self, t, limit=8):
        return [{"date": "2026-08-10", "title": "数据中心营收再超预期", "sentiment": "positive"}]

    def get_earnings(self, t):
        return [{"report_date": "2026-07-30", "eps_actual": 1.05, "eps_estimate": 0.98,
                 "eps_surprise_pct": 7.1, "revenue_actual": 3.0e10}]

    def get_options(self, t, limit=1):
        return [{"date": "2026-08-09", "put_call_ratio": 0.62,
                 "total_call_volume": 120000, "total_put_volume": 74000, "unusual_volume": True}]

    def list_all(self):
        return [{"id": "e1", "ticker": "NVDA", "market": "us_stock"}]

    def get_catalysts_for_entry(self, entry_id, active_only=True):
        return [{"catalyst_type": "earnings", "description": "Q3 财报", "expected_date": "2026-11-20"}]


class _EmptyStore:
    def get_company_profile(self, t): return None
    def get_latest_snapshot(self, t): return None
    def get_institutional_holders(self, t, limit=50): return []
    def get_analyst_ratings(self, t, limit=50): return []
    def get_news(self, t, limit=8): return []
    def get_earnings(self, t): return []
    def get_options(self, t, limit=1): return []
    def list_all(self): return []
    def get_catalysts_for_entry(self, entry_id, active_only=True): return []


def test_full_block_has_all_sections():
    block = _focus_ticker_block(_FullStore(), "NVDA")
    assert "NVDA" in block and "英伟达" in block
    assert "45.2" in block            # trailing_pe（估值倍数）
    assert "38.0" in block            # ev_to_ebitda（analyst 明确要）
    assert "损益" in block and "30000000000" in block   # net_income（netIncomeToCommon）
    assert "0.75" in block            # gross_margin
    assert "现金流与负债" in block and "debt_to_equity" in block   # 现金流/债务结构
    assert "个股期权" in block and "0.62" in block                  # per-stock PCR
    assert "160.0" in block or "160" in block   # consensus/target price
    assert "数据中心营收再超预期" in block       # 个股新闻标题
    assert "7.1" in block             # eps_surprise_pct
    assert "Q3 财报" in block          # 催化剂
    assert "2026-08-11" in block      # 快照日诚实标注
    assert "隐含波动率(IV)" in block and "未采集" in block          # 结构性缺口诚实声明


def test_empty_store_honest_placeholder():
    block = _focus_ticker_block(_EmptyStore(), "ZZZZ")
    assert "暂无系统采集的深度资料" in block
    assert "建议先加入观察池" in block


def test_ticker_not_in_watchlist_skips_catalysts_only():
    class _NoEntry(_FullStore):
        def list_all(self): return [{"id": "e1", "ticker": "AMD", "market": "us_stock"}]
    block = _focus_ticker_block(_NoEntry(), "NVDA")
    assert "Q3 财报" not in block      # 无 entry_id → 催化剂略过
    assert "45.2" in block            # 其余财务/估值仍在
    assert "数据中心营收再超预期" in block


def test_blank_ticker_returns_empty():
    assert _focus_ticker_block(_FullStore(), "") == ""
    assert _focus_ticker_block(_FullStore(), "   ") == ""


if __name__ == "__main__":
    test_full_block_has_all_sections()
    test_empty_store_honest_placeholder()
    test_ticker_not_in_watchlist_skips_catalysts_only()
    test_blank_ticker_returns_empty()
    print("OK")
