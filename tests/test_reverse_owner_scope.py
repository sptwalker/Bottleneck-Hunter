"""验收：反向分析列表按所属正向分析记录（owner_analysis_id）独立过滤。

对应 bug 修复——原先 list_reverse_analyses 只按 user+market 过滤，
导致每条正向记录都看到同一份"公共"反向列表。现按 owner_analysis_id 隔离。
运行：pytest tests/test_reverse_owner_scope.py -q
"""
import tempfile
from pathlib import Path

from bottleneck_hunter.watchlist.store import WatchlistStore


def _store(tmp):
    return WatchlistStore(db_path=str(Path(tmp) / "rev.db"), user_id="u1").for_market("us_stock")


def test_list_scoped_by_owner():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        # 记录 A 下两条，记录 B 下一条，另有一条无 owner（历史孤儿）
        s.create_reverse_analysis(ticker="NVDA", owner_analysis_id="A")
        s.create_reverse_analysis(ticker="AMD", owner_analysis_id="A")
        s.create_reverse_analysis(ticker="TSM", owner_analysis_id="B")
        s.create_reverse_analysis(ticker="ORCL")  # owner 为空

        a = s.list_reverse_analyses(owner_analysis_id="A")
        b = s.list_reverse_analyses(owner_analysis_id="B")

        a_tickers = {r["ticker"] for r in a}
        b_tickers = {r["ticker"] for r in b}

        # 记录 A 只看到自己的两条，绝不含 B 的
        assert a_tickers == {"NVDA", "AMD"}, a_tickers
        # 记录 B 只看到自己的一条，绝不含 A 的
        assert b_tickers == {"TSM"}, b_tickers
        # 关键：两条列表互不重叠（不再是公共数据）
        assert a_tickers.isdisjoint(b_tickers)


def test_no_owner_returns_all_backcompat():
    """不传 owner_analysis_id 时回退旧行为（返回全部），保证后台/脚本/旧前端不炸。"""
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.create_reverse_analysis(ticker="NVDA", owner_analysis_id="A")
        s.create_reverse_analysis(ticker="TSM", owner_analysis_id="B")
        s.create_reverse_analysis(ticker="ORCL")

        allrecs = s.list_reverse_analyses()  # 无 owner 参数
        assert {r["ticker"] for r in allrecs} == {"NVDA", "TSM", "ORCL"}


def test_owner_field_persisted_and_returned():
    with tempfile.TemporaryDirectory() as d:
        s = _store(d)
        s.create_reverse_analysis(ticker="NVDA", owner_analysis_id="rec-123")
        rows = s.list_reverse_analyses(owner_analysis_id="rec-123")
        assert len(rows) == 1
        assert rows[0]["owner_analysis_id"] == "rec-123"


def test_cross_market_still_isolated():
    """owner 过滤叠加在 user+market 之上：不同市场即使同 owner 也不串。"""
    with tempfile.TemporaryDirectory() as d:
        base = WatchlistStore(db_path=str(Path(d) / "rev.db"), user_id="u1")
        us = base.for_market("us_stock")
        cn = base.for_market("cn_stock")
        us.create_reverse_analysis(ticker="NVDA", owner_analysis_id="A")
        cn.create_reverse_analysis(ticker="600519", owner_analysis_id="A")

        us_a = {r["ticker"] for r in us.list_reverse_analyses(owner_analysis_id="A")}
        cn_a = {r["ticker"] for r in cn.list_reverse_analyses(owner_analysis_id="A")}
        assert us_a == {"NVDA"}
        assert cn_a == {"600519"}


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
