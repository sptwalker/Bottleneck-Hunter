"""Tests for options_pipeline.py — 期权活动分析管道。"""

from __future__ import annotations

from collections import namedtuple
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.watchlist.options_pipeline import (
    _analyze_options_chain,
    _fetch_one,
    fetch_options_batch,
)

OptionChain = namedtuple("OptionChain", ["calls", "puts"])


def _make_chain(call_rows=None, put_rows=None):
    calls = pd.DataFrame(call_rows or [], columns=["strike", "volume", "openInterest"])
    puts = pd.DataFrame(put_rows or [], columns=["strike", "volume", "openInterest"])
    return OptionChain(calls=calls, puts=puts)


class TestAnalyzeOptionsChain:
    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_no_options(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = []
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")
        assert result is None

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_basic_analysis(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 5000, 20000], [175, 3000, 15000]],
            put_rows=[[165, 2000, 10000], [160, 1500, 8000]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        assert result is not None
        assert result["ticker"] == "AAPL"
        assert result["total_call_volume"] == 8000
        assert result["total_put_volume"] == 3500
        assert result["put_call_ratio"] == round(3500 / 8000, 3)
        assert result["unusual_volume"] is True
        assert result["max_oi_strike"] == 170.0
        assert result["max_oi_expiry"] == "2025-01-17"

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_no_volume_column(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        calls = pd.DataFrame({"strike": [170], "openInterest": [5000]})
        puts = pd.DataFrame({"strike": [165], "openInterest": [3000]})
        ticker_obj.option_chain.return_value = OptionChain(calls=calls, puts=puts)
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        assert result["total_call_volume"] == 0
        assert result["total_put_volume"] == 0
        assert result["put_call_ratio"] is None
        assert result["unusual_volume"] is False

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_zero_call_volume_pcr_none(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 0, 100]],
            put_rows=[[165, 500, 200]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        assert result["put_call_ratio"] is None

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_notable_trades_high_volume(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 5000, 20000], [175, 2000, 15000], [180, 500, 1000]],
            put_rows=[[165, 1500, 10000]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        notable = result["notable_trades"]
        call_notable = [n for n in notable if n["type"] == "call"]
        put_notable = [n for n in notable if n["type"] == "put"]
        assert len(call_notable) == 2
        assert len(put_notable) == 1

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_no_notable_trades(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 100, 500]],
            put_rows=[[165, 50, 200]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        assert result["notable_trades"] == []

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_low_volume_not_unusual(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 3000, 5000]],
            put_rows=[[165, 2000, 3000]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        assert result["unusual_volume"] is False

    @patch("bottleneck_hunter.watchlist.options_pipeline.yf")
    def test_result_has_required_keys(self, mock_yf):
        ticker_obj = MagicMock()
        ticker_obj.options = ["2025-01-17"]
        ticker_obj.option_chain.return_value = _make_chain(
            call_rows=[[170, 1000, 5000]],
            put_rows=[[165, 500, 3000]],
        )
        mock_yf.Ticker.return_value = ticker_obj
        result = _analyze_options_chain("AAPL")

        required = {"id", "ticker", "date", "unusual_volume", "put_call_ratio",
                     "total_call_volume", "total_put_volume", "max_oi_strike",
                     "max_oi_expiry", "notable_trades", "fetched_at"}
        assert required.issubset(result.keys())


class TestFetchOne:
    @pytest.mark.asyncio
    async def test_ok(self):
        store = MagicMock()
        analysis = {"id": "abc", "ticker": "AAPL"}
        with patch("bottleneck_hunter.watchlist.options_pipeline._analyze_options_chain", return_value=analysis):
            result = await _fetch_one("AAPL", store)
        assert result == "ok"
        store.save_options.assert_called_once_with([analysis])

    @pytest.mark.asyncio
    async def test_no_data(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.options_pipeline._analyze_options_chain", return_value=None):
            result = await _fetch_one("NOOPT", store)
        assert result == "no_data"
        store.save_options.assert_not_called()

    @pytest.mark.asyncio
    async def test_error(self):
        store = MagicMock()
        store._user_id = ""
        # _fetch_one 现走 DataHub（get_hub().fetch），异常时返回 "error: ..."
        from unittest.mock import AsyncMock
        with patch("bottleneck_hunter.data_provider.hub.DataHub.fetch",
                   new_callable=AsyncMock, side_effect=RuntimeError("api down")):
            result = await _fetch_one("FAIL", store)
        assert result.startswith("error:")
        store.save_options.assert_not_called()


class TestFetchOptionsBatch:
    @pytest.mark.asyncio
    async def test_empty(self):
        store = MagicMock()
        result = await fetch_options_batch([], store)
        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_tickers(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.options_pipeline._fetch_one") as mock_fetch:
            mock_fetch.side_effect = ["ok", "no_data"]
            result = await fetch_options_batch(["AAPL", "NOOPT"], store)
        assert result["AAPL"] == "ok"
        assert result["NOOPT"] == "no_data"
