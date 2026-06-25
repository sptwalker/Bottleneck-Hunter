"""news_pipeline 单元测试。

覆盖新闻抓取（yfinance / akshare）和 LLM 摘要生成。
所有外部依赖均通过 mock 隔离。
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.watchlist.budget import BudgetTracker
from bottleneck_hunter.watchlist.models import DegradationMode
from bottleneck_hunter.watchlist.news_pipeline import (
    _fetch_astock_news,
    _fetch_yfinance_news,
    _summarize_with_llm,
)
from bottleneck_hunter.watchlist.store import WatchlistStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    """创建临时 WatchlistStore。"""
    return WatchlistStore(tmp_path / "test.db")


@pytest.fixture
def budget(store):
    """创建 BudgetTracker（依赖 store）。"""
    return BudgetTracker(store)


# ---------------------------------------------------------------------------
# TestNewsFetching — yfinance / akshare 新闻抓取
# ---------------------------------------------------------------------------

class TestNewsFetching:
    """测试原始新闻数据的拉取和解析。"""

    @patch("bottleneck_hunter.watchlist.news_pipeline.yf")
    def test_yfinance_news_parsing(self, mock_yf):
        """mock yf.Ticker().news，验证 _fetch_yfinance_news 正确解析字段。"""
        # 准备 yfinance 返回的模拟数据
        mock_news = [
            {
                "title": "Apple Hits All-Time High",
                "providerPublishTime": 1700000000,
                "link": "https://example.com/apple-high",
                "publisher": "Reuters",
            },
            {
                "title": "iPhone Sales Beat Expectations",
                "providerPublishTime": 1700100000,
                "link": "https://example.com/iphone-sales",
                "publisher": "Bloomberg",
            },
        ]
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.news = mock_news
        mock_yf.Ticker.return_value = mock_ticker_instance

        results = _fetch_yfinance_news("AAPL")

        # 验证返回 2 条新闻
        assert len(results) == 2

        # 验证第一条新闻的字段映射
        first = results[0]
        assert first["ticker"] == "AAPL"
        assert first["title"] == "Apple Hits All-Time High"
        assert first["source_url"] == "https://example.com/apple-high"
        assert first["source_name"] == "Reuters"
        assert first["id"]  # 确保有 id
        # 验证日期格式（从 Unix 时间戳转换）
        expected_date = datetime.fromtimestamp(1700000000, tz=timezone.utc).strftime("%Y-%m-%d")
        assert first["date"] == expected_date

        # 验证第二条
        second = results[1]
        assert second["title"] == "iPhone Sales Beat Expectations"
        assert second["source_name"] == "Bloomberg"

    @patch("bottleneck_hunter.watchlist.news_pipeline.ak")
    def test_astock_news_parsing(self, mock_ak):
        """mock ak.stock_news_em，验证 _fetch_astock_news 正确解析 DataFrame。"""
        # 构造 akshare 返回的 DataFrame
        df = pd.DataFrame({
            "发布时间": ["2024-01-15 09:30:00", "2024-01-15 10:00:00"],
            "新闻标题": ["贵州茅台业绩超预期", "白酒板块集体走强"],
            "新闻内容": ["贵州茅台发布年报...", "白酒板块今日集体上涨..."],
            "文章来源": ["新浪财经", "东方财富"],
            "新闻链接": ["https://example.com/maotai", "https://example.com/baijiu"],
        })
        mock_ak.stock_news_em.return_value = df

        results = _fetch_astock_news("SH600519")

        # 验证返回 2 条新闻
        assert len(results) == 2

        # 验证第一条新闻的字段
        first = results[0]
        assert first["ticker"] == "SH600519"
        assert first["title"] == "贵州茅台业绩超预期"
        assert first["date"] == "2024-01-15"
        assert first["source_name"] == "新浪财经"
        assert first["source_url"] == "https://example.com/maotai"
        assert first["id"]  # 确保有 id

        # 验证第二条
        second = results[1]
        assert second["title"] == "白酒板块集体走强"
        assert second["source_name"] == "东方财富"

    @patch("bottleneck_hunter.watchlist.news_pipeline.ak")
    def test_astock_news_invalid_ticker(self, mock_ak):
        """无效 ticker（无法提取 6 位代码）应返回空列表。"""
        # _ASTOCK_RE 无法匹配 → 直接返回 []，不会调用 akshare
        results = _fetch_astock_news("INVALID_TICKER")
        assert results == []
        mock_ak.stock_news_em.assert_not_called()


# ---------------------------------------------------------------------------
# TestLLMSummary — LLM 摘要与情感分析
# ---------------------------------------------------------------------------

class TestLLMSummary:
    """测试 _summarize_with_llm 的 LLM 调用和降级行为。"""

    async def test_summarize_with_llm(self, budget):
        """mock LLM，验证返回 summary / sentiment / sentiment_score。"""
        # 构造 LLM 返回的 JSON
        llm_response_data = {
            "summary": "苹果公司股价创新高，iPhone 销量超预期",
            "sentiment": "positive",
            "sentiment_score": 0.5,
        }
        mock_llm = AsyncMock()
        # 设置 LLM 属性（budget.record 会通过 getattr 读取）
        mock_llm._llm_type = "openai"
        mock_llm.model_name = "gpt-4o-mini"

        mock_msg = MagicMock()
        mock_msg.content = json.dumps(llm_response_data, ensure_ascii=False)
        mock_llm.ainvoke.return_value = mock_msg

        articles = [
            {"title": "Apple Hits All-Time High"},
            {"title": "iPhone Sales Beat Expectations"},
        ]

        result = await _summarize_with_llm("AAPL", articles, mock_llm, budget)

        assert result["summary"] == "苹果公司股价创新高，iPhone 销量超预期"
        assert result["sentiment"] == "positive"
        assert result["sentiment_score"] == 0.5
        # 验证 LLM 被调用了一次
        mock_llm.ainvoke.assert_called_once()

    async def test_summarize_budget_minimal(self, budget):
        """MINIMAL 降级模式下跳过 LLM，直接返回 neutral。"""
        mock_llm = AsyncMock()
        articles = [{"title": "Some headline"}]

        # mock budget.get_degradation_mode 返回 MINIMAL
        with patch.object(budget, "get_degradation_mode", return_value=DegradationMode.MINIMAL):
            result = await _summarize_with_llm("AAPL", articles, mock_llm, budget)

        # MINIMAL 模式不调用 LLM
        mock_llm.ainvoke.assert_not_called()
        assert result["sentiment"] == "neutral"
        assert result["sentiment_score"] == 0.0
        assert result["summary"] == ""
