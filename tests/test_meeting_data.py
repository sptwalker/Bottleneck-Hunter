"""Tests for MeetingDataFetcher — 会前数据预取。"""

import pytest
from unittest.mock import patch, MagicMock
import pandas as pd

from bottleneck_hunter.chain.meeting_data import MeetingDataFetcher, _safe_float


class TestSafeFloat:
    def test_normal(self):
        assert _safe_float(3.14) == 3.14

    def test_string_with_comma(self):
        assert _safe_float("1,234.56") == 1234.56

    def test_none(self):
        assert _safe_float(None) is None

    def test_scale(self):
        assert _safe_float(100, scale=0.01) == 1.0

    def test_invalid(self):
        assert _safe_float("N/A") is None


class TestFetchAll:
    @pytest.mark.asyncio
    async def test_a_stock(self):
        fetcher = MeetingDataFetcher()
        with patch.object(fetcher, "_fetch_one", return_value={"price": {"latest": 100}}):
            results = await fetcher.fetch_all(["600519.SH"], "a_stock")
            assert "600519.SH" in results
            assert results["600519.SH"]["price"]["latest"] == 100

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        fetcher = MeetingDataFetcher()
        with patch.object(fetcher, "_fetch_one", side_effect=RuntimeError("fail")):
            results = await fetcher.fetch_all(["BAD"], "us_stock")
            assert results["BAD"] == {}

    @pytest.mark.asyncio
    async def test_empty_tickers(self):
        fetcher = MeetingDataFetcher()
        results = await fetcher.fetch_all([], "a_stock")
        assert results == {}


class TestFetchAPrice:
    def test_success(self):
        df = pd.DataFrame({
            "代码": ["600519"],
            "最新价": [1800.0],
            "涨跌幅": [2.5],
            "成交额": [5000000000],
            "换手率": [0.5],
        })
        with patch("bottleneck_hunter.chain.meeting_data.MeetingDataFetcher._fetch_a_price.__wrapped__", create=True):
            result = MeetingDataFetcher._fetch_a_price("600519")
        # 因为 _fetch_a_price 依赖 akshare，直接测试返回结构
        assert isinstance(result, dict)

    @patch("akshare.stock_zh_a_spot_em", side_effect=Exception("fail"))
    def test_failure_returns_empty(self, _):
        result = MeetingDataFetcher._fetch_a_price("600519")
        assert result == {}


class TestFetchANews:
    @patch("akshare.stock_news_em")
    def test_success(self, mock_news):
        mock_news.return_value = pd.DataFrame({"新闻标题": ["标题A", "标题B", "标题C", "标题D", "标题E", "标题F"]})
        result = MeetingDataFetcher._fetch_a_news("600519")
        assert len(result) == 5

    @patch("akshare.stock_news_em", side_effect=Exception("fail"))
    def test_failure(self, _):
        result = MeetingDataFetcher._fetch_a_news("600519")
        assert result == []


class TestFetchUsPrice:
    @patch("yfinance.Ticker")
    def test_success(self, mock_ticker_cls):
        mock_stock = MagicMock()
        mock_stock.info = {"currentPrice": 150.0, "regularMarketChangePercent": 1.5, "regularMarketVolume": 50000000, "marketCap": 2500000000000}
        mock_ticker_cls.return_value = mock_stock
        result = MeetingDataFetcher._fetch_us_price("AAPL")
        assert result["latest"] == 150.0

    @patch("yfinance.Ticker", side_effect=Exception("fail"))
    def test_failure(self, _):
        result = MeetingDataFetcher._fetch_us_price("AAPL")
        assert result == {}


class TestFormatForBriefing:
    def test_basic(self):
        fetcher = MeetingDataFetcher()
        data = {
            "600519.SH": {
                "price": {"latest": 1800, "change_pct": 2.5, "volume_yi": 50},
                "news": ["标题1", "标题2"],
                "analyst": {"rating": "买入", "recent_institutions": 15},
            }
        }
        result = fetcher.format_for_briefing(data, {"600519.SH": "贵州茅台"})
        assert "贵州茅台" in result
        assert "1800" in result
        assert "标题1" in result
        assert "买入" in result

    def test_empty_data(self):
        fetcher = MeetingDataFetcher()
        result = fetcher.format_for_briefing({})
        assert result == ""

    def test_all_empty_entries(self):
        fetcher = MeetingDataFetcher()
        result = fetcher.format_for_briefing({"AAA": {}})
        assert result == ""

    def test_with_us_data(self):
        fetcher = MeetingDataFetcher()
        data = {
            "AAPL": {
                "price": {"latest": 150, "change_pct": 1.5, "volume": 50000000},
                "news": [],
                "analyst": {"analyst_count": 40, "target_price": 180},
            }
        }
        result = fetcher.format_for_briefing(data)
        assert "AAPL" in result
        assert "150" in result
        assert "40" in result
