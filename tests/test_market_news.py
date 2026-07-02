"""市场/主题级新闻 + L1 大盘指数修复 —— 单元测试。"""

from unittest.mock import AsyncMock, patch

import pytest

from bottleneck_hunter.watchlist import news_pipeline as np


class TestFetchRssNews:
    async def test_per_ticker_id_unchanged(self):
        """参数化后，个股调用的去重 id 必须与旧实现逐字节一致（tag=ticker）。"""
        import hashlib
        title = "Apple hits new high"
        expected = hashlib.md5(f"AAPL:rss:{title}".encode()).hexdigest()[:12]

        class _Resp:
            status_code = 200
            text = ""
        fake_feed = type("F", (), {"entries": [{"title": title, "link": "u", "published_parsed": None}]})()
        with patch.object(np, "get_http_client", return_value=AsyncMock(get=AsyncMock(return_value=_Resp()))), \
             patch("feedparser.parse", return_value=fake_feed):
            out = await np._fetch_rss_news("AAPL stock", tag="AAPL")
        assert out and out[0]["id"] == expected
        assert out[0]["ticker"] == "AAPL"


class TestFetchMarketNews:
    async def _run(self, market):
        # 每个主题词返回一条带主题标签的新闻
        async def fake_rss(query, limit=5, tag=""):
            return [{"id": f"{tag}-1", "ticker": tag, "date": "2026-07-01",
                     "title": f"{tag} 相关新闻", "source_name": "Google News", "source_url": ""}]
        with patch.object(np, "_fetch_rss_news", side_effect=fake_rss):
            return await np.fetch_market_news(market)  # 不传 llm → 跳过情感

    async def test_us_market_news_nonempty_with_topics(self):
        items = await self._run("us_stock")
        assert items, "应产出非空市场新闻"
        topics = {it["topic"] for it in items}
        assert {"AI", "美联储", "大盘"} <= topics  # 覆盖主题标签
        # 均为主题级（topic 非真实个股 ticker）
        assert all("title" in it and it["topic"] for it in items)

    async def test_a_stock_market_news(self):
        items = await self._run("a_stock")
        assert items and any(it["topic"] == "AI" for it in items)

    async def test_graceful_empty_on_failure(self):
        async def boom(query, limit=5, tag=""):
            raise RuntimeError("RSS down")
        with patch.object(np, "_fetch_rss_news", side_effect=boom):
            items = await np.fetch_market_news("us_stock")
        assert items == []  # 抓取全失败 → 优雅降级为空，不抛异常


class TestMarketIndexKeys:
    def test_index_keys_present(self):
        from bottleneck_hunter.watchlist.macro_data import MARKET_INDEX_KEYS, _CN_INDICATORS, _US_INDICATORS
        us_keys = {k for k, *_ in _US_INDICATORS}
        cn_keys = {k for k, *_ in _CN_INDICATORS}
        assert {"sp500", "nasdaq"} <= us_keys
        assert {"sse_index", "csi300"} <= cn_keys
        # 大盘指数键都能在指标定义里找到
        assert set(MARKET_INDEX_KEYS["us_stock"]) <= us_keys
        assert set(MARKET_INDEX_KEYS["a_stock"]) <= cn_keys


class TestRealIndicesInContext:
    async def test_indices_carry_real_and_breadth(self, tmp_path):
        """_collect_market_context 的 indices 应含真实指数 + watchlist_breadth 子键。"""
        from bottleneck_hunter.watchlist.decision_engine import _collect_market_context
        from bottleneck_hunter.watchlist.store import WatchlistStore
        store = WatchlistStore(tmp_path / "t.db")
        eid = store.add({"ticker": "NVDA", "company_name": "NVIDIA", "tier": "focus", "market": "us_stock"})
        store.save_snapshots([{"ticker": "NVDA", "date": "2026-07-01", "close": 100.0,
                               "change_pct": 2.0, "rsi_14": 60, "sma_50": 90, "market": "us_stock"}])
        # mock 宏观返回真实指数
        fake_macro = {"sp500": {"value": 5500, "change_pct": 0.8, "label": "标普500"},
                      "nasdaq": {"value": 18000, "change_pct": 1.2, "label": "纳指"},
                      "vix": {"value": 14, "change_pct": -3, "label": "VIX"}}
        with patch("bottleneck_hunter.watchlist.macro_data.fetch_macro_data",
                   new=AsyncMock(return_value=fake_macro)):
            ctx = await _collect_market_context(store, "us_stock")
        assert "sp500" in ctx["indices"] and "nasdaq" in ctx["indices"]
        assert "watchlist_breadth" in ctx["indices"]
        assert ctx["indices"]["watchlist_breadth"]["stocks_tracked"] == 1
        # VIX 应归入 sentiment（市场情绪），并从 macro 段移除，避免两处重复
        assert ctx["sentiment"].get("vix", {}).get("value") == 14
        assert "vix" not in ctx["macro"]


class TestMarketNewsPersistence:
    """阶段二：市场新闻落库（哨兵 ticker）→ 读回，不误捞个股。"""

    async def test_refresh_and_read_back(self, tmp_path):
        from bottleneck_hunter.watchlist.store import WatchlistStore
        s = WatchlistStore(tmp_path / "t.db")
        fake = [{"topic": "AI", "title": "AI rally", "date": "2026-07-02", "source_name": "X"},
                {"topic": "美联储", "title": "Fed holds", "date": "2026-07-02", "source_name": "Y"}]
        with patch.object(np, "fetch_market_news", new=AsyncMock(return_value=fake)):
            n = await np.refresh_market_news(s, "us_stock")
        assert n == 2
        rows = s.get_news(np.market_sentinel("us_stock"), limit=15)
        assert len(rows) == 2
        assert rows[0]["llm_analysis"] in ("AI", "美联储")  # 主题标签存 llm_analysis
        # 哨兵不污染个股查询
        assert s.get_news("AAPL") == []

    async def test_empty_fetch_no_write(self, tmp_path):
        from bottleneck_hunter.watchlist.store import WatchlistStore
        s = WatchlistStore(tmp_path / "t.db")
        with patch.object(np, "fetch_market_news", new=AsyncMock(return_value=[])):
            n = await np.refresh_market_news(s, "us_stock")
        assert n == 0

    def test_sentinel_map(self):
        assert np.market_sentinel("us_stock") == "__MARKET_US__"
        assert np.market_sentinel("a_stock") == "__MARKET_CN__"
        assert np.market_sentinel("unknown") == "__MARKET_US__"
