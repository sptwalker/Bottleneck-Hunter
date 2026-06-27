"""Tests for notice_pipeline.py — A 股公告管道。"""

from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.watchlist.notice_pipeline import (
    _classify_notice,
    _extract_code,
    _fetch_notices_sync,
    _fetch_one,
    fetch_notice_batch,
)


class TestClassifyNotice:
    def test_earnings_preview(self):
        assert _classify_notice("关于2025年度业绩预告的公告") == "earnings_preview"

    def test_insider_sell(self):
        assert _classify_notice("关于控股股东减持计划的预披露公告") == "insider_sell"

    def test_insider_buy(self):
        assert _classify_notice("关于公司高管增持计划的公告") == "insider_buy"

    def test_dividend(self):
        assert _classify_notice("2024年度分红派息实施公告") == "dividend"

    def test_buyback(self):
        assert _classify_notice("关于回购公司股份的公告") == "buyback"

    def test_annual_report(self):
        assert _classify_notice("2024年年报") == "annual_report"

    def test_quarterly(self):
        assert _classify_notice("2024年第三季报") == "quarterly"

    def test_other_unmatched(self):
        assert _classify_notice("关于召开股东大会的通知") == "other"

    def test_empty_title(self):
        assert _classify_notice("") == "other"

    def test_first_match_wins(self):
        result = _classify_notice("年报中提到分红计划")
        assert result in ("annual_report", "dividend")


class TestExtractCode:
    def test_plain_6digit(self):
        assert _extract_code("600000") == "600000"

    def test_sh_prefix(self):
        assert _extract_code("SH600519") == "600519"

    def test_sz_prefix(self):
        assert _extract_code("SZ000001") == "000001"

    def test_lowercase_prefix(self):
        assert _extract_code("sh601398") == "601398"

    def test_with_dot_suffix(self):
        assert _extract_code("000001.SZ") == "000001"

    def test_invalid_returns_none(self):
        assert _extract_code("AAPL") is None

    def test_short_code_returns_none(self):
        assert _extract_code("123") is None


class TestFetchNoticesSync:
    def _make_df(self, rows):
        return pd.DataFrame(rows)

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_basic_fetch(self, mock_ak):
        df = self._make_df([
            {"公告标题": "关于2024年度业绩预告的公告", "公告日期": "2024-12-15", "公告链接": "http://example.com/1"},
            {"公告标题": "关于控股股东减持计划", "公告日期": "2024-12-10", "公告链接": "http://example.com/2"},
        ])
        mock_ak.stock_notice_report.return_value = df
        result = _fetch_notices_sync("SH600000")
        assert len(result) == 2
        assert result[0]["filing_type"] == "earnings_preview"
        assert result[1]["filing_type"] == "insider_sell"
        assert result[1]["is_insider_trade"] is True
        assert result[0]["is_insider_trade"] is False

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_empty_df(self, mock_ak):
        mock_ak.stock_notice_report.return_value = pd.DataFrame()
        result = _fetch_notices_sync("SH600000")
        assert result == []

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_none_df(self, mock_ak):
        mock_ak.stock_notice_report.return_value = None
        result = _fetch_notices_sync("SH600000")
        assert result == []

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak", None)
    def test_no_akshare(self):
        result = _fetch_notices_sync("SH600000")
        assert result == []

    def test_invalid_ticker(self):
        result = _fetch_notices_sync("AAPL")
        assert result == []

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_limit_respected(self, mock_ak):
        rows = [{"公告标题": f"公告{i}", "公告日期": f"2024-12-{i:02d}", "公告链接": ""} for i in range(1, 21)]
        mock_ak.stock_notice_report.return_value = self._make_df(rows)
        result = _fetch_notices_sync("600000", limit=5)
        assert len(result) == 5

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_empty_title_skipped(self, mock_ak):
        df = self._make_df([
            {"公告标题": "", "公告日期": "2024-12-15", "公告链接": ""},
            {"公告标题": "有效公告", "公告日期": "2024-12-14", "公告链接": ""},
        ])
        mock_ak.stock_notice_report.return_value = df
        result = _fetch_notices_sync("600000")
        assert len(result) == 1
        assert result[0]["title"] == "有效公告"

    @patch("bottleneck_hunter.watchlist.notice_pipeline.ak")
    def test_id_is_md5(self, mock_ak):
        df = self._make_df([{"公告标题": "测试公告", "公告日期": "2024-12-15", "公告链接": ""}])
        mock_ak.stock_notice_report.return_value = df
        result = _fetch_notices_sync("600000")
        expected_id = hashlib.md5("600000:测试公告:2024-12-15".encode()).hexdigest()[:12]
        assert result[0]["id"] == expected_id


class TestFetchOne:
    @pytest.mark.asyncio
    async def test_no_filings(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.notice_pipeline._fetch_notices_sync", return_value=[]):
            result = await _fetch_one("SH600000", store)
        assert result == {"filings": 0, "trades": 0}
        store.save_filings.assert_not_called()

    @pytest.mark.asyncio
    async def test_filings_saved(self):
        filings = [
            {"id": "abc", "ticker": "SH600000", "filing_type": "other", "filed_date": "2024-12-15",
             "title": "一般公告", "url": "", "is_insider_trade": False, "accession": "abc"},
        ]
        store = MagicMock()
        store.save_filings.return_value = 1
        store.save_insider_trades.return_value = 0
        with patch("bottleneck_hunter.watchlist.notice_pipeline._fetch_notices_sync", return_value=filings):
            result = await _fetch_one("SH600000", store)
        assert result["filings"] == 1
        assert result["trades"] == 0
        store.save_filings.assert_called_once()

    @pytest.mark.asyncio
    async def test_insider_trades_created(self):
        filings = [
            {"id": "x1", "ticker": "SH600000", "filing_type": "insider_sell", "filed_date": "2024-12-15",
             "title": "关于减持计划", "url": "", "is_insider_trade": True, "accession": "x1"},
        ]
        store = MagicMock()
        store.save_filings.return_value = 1
        store.save_insider_trades.return_value = 1
        with patch("bottleneck_hunter.watchlist.notice_pipeline._fetch_notices_sync", return_value=filings):
            result = await _fetch_one("SH600000", store)
        assert result["trades"] == 1
        store.save_insider_trades.assert_called_once()
        saved_trades = store.save_insider_trades.call_args[0][0]
        assert saved_trades[0]["transaction_type"] == "insider_sell"


class TestFetchNoticeBatch:
    @pytest.mark.asyncio
    async def test_empty_tickers(self):
        store = MagicMock()
        result = await fetch_notice_batch([], store)
        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_tickers(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.notice_pipeline._fetch_one") as mock_fetch:
            mock_fetch.return_value = {"filings": 2, "trades": 0}
            result = await fetch_notice_batch(["SH600000", "SZ000001"], store)
        assert len(result) == 2
        assert result["SH600000"]["filings"] == 2

    @pytest.mark.asyncio
    async def test_one_ticker_error(self):
        store = MagicMock()
        call_count = 0

        async def side_effect(ticker, s):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("network error")
            return {"filings": 1, "trades": 0}

        with patch("bottleneck_hunter.watchlist.notice_pipeline._fetch_one", side_effect=side_effect):
            result = await fetch_notice_batch(["BAD", "GOOD"], store)
        assert "error" in result["BAD"]
        assert result["GOOD"]["filings"] == 1
