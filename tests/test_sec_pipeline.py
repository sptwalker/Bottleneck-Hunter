"""Tests for sec_pipeline.py — SEC EDGAR 数据管道。"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bottleneck_hunter.watchlist.sec_pipeline import (
    _load_cik_map,
    _get_cik,
    _fetch_filings,
    _parse_insider_trades_from_filings,
    _fetch_one,
    fetch_sec_batch,
    _CIK_CACHE,
)


@pytest.fixture(autouse=True)
def _clear_cik_cache():
    """每个测试前清空 CIK 缓存。"""
    _CIK_CACHE.clear()
    yield
    _CIK_CACHE.clear()


class TestLoadCikMap:
    @pytest.mark.asyncio
    async def test_loads_and_caches(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "0": {"ticker": "AAPL", "cik_str": "320193"},
            "1": {"ticker": "MSFT", "cik_str": "789019"},
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client):
            result = await _load_cik_map()

        assert "AAPL" in result
        assert result["AAPL"] == "0000320193"
        assert result["MSFT"] == "0000789019"

    @pytest.mark.asyncio
    async def test_returns_cache_on_second_call(self):
        _CIK_CACHE["AAPL"] = "0000320193"
        result = await _load_cik_map()
        assert result["AAPL"] == "0000320193"

    @pytest.mark.asyncio
    async def test_http_error_returns_empty(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client):
            result = await _load_cik_map()
        assert result == {}

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        mock_client = AsyncMock()
        mock_client.get.side_effect = RuntimeError("network")

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client):
            result = await _load_cik_map()
        assert result == {}


class TestGetCik:
    @pytest.mark.asyncio
    async def test_found(self):
        _CIK_CACHE["AAPL"] = "0000320193"
        cik = await _get_cik("aapl")
        assert cik == "0000320193"

    @pytest.mark.asyncio
    async def test_not_found(self):
        _CIK_CACHE["AAPL"] = "0000320193"
        cik = await _get_cik("FAKE")
        assert cik is None


class TestFetchFilings:
    @pytest.mark.asyncio
    async def test_parses_submissions(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["4", "8-K", "10-Q"],
                    "filingDate": ["2025-03-15", "2025-03-10", "2025-02-28"],
                    "accessionNumber": ["0001-23-456789", "0001-23-456790", "0001-23-456791"],
                    "primaryDocDescription": ["Form 4", "Current Report", "Quarterly Report"],
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client), \
             patch("asyncio.sleep", return_value=None):
            result = await _fetch_filings("0000320193", ["4", "8-K", "10-Q"], limit=10)

        assert len(result) == 3
        assert result[0]["filing_type"] == "4"
        assert result[0]["is_insider_trade"] is True
        assert result[1]["filing_type"] == "8-K"
        assert result[1]["is_insider_trade"] is False

    @pytest.mark.asyncio
    async def test_filters_by_form_type(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["4", "SC 13G", "8-K"],
                    "filingDate": ["2025-03-15", "2025-03-10", "2025-03-05"],
                    "accessionNumber": ["0001-23-000001", "0001-23-000002", "0001-23-000003"],
                    "primaryDocDescription": ["Form 4", "SC 13G", "Current Report"],
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client), \
             patch("asyncio.sleep", return_value=None):
            result = await _fetch_filings("0000320193", ["4"], limit=10)

        assert len(result) == 1
        assert result[0]["filing_type"] == "4"

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "filings": {
                "recent": {
                    "form": ["4"] * 5,
                    "filingDate": [f"2025-03-{i:02d}" for i in range(1, 6)],
                    "accessionNumber": [f"0001-23-{i:06d}" for i in range(5)],
                    "primaryDocDescription": ["Form 4"] * 5,
                }
            }
        }
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client), \
             patch("asyncio.sleep", return_value=None):
            result = await _fetch_filings("0000320193", ["4"], limit=2)

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_http_error(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        with patch("bottleneck_hunter.watchlist.sec_pipeline.get_http_client", return_value=mock_client), \
             patch("asyncio.sleep", return_value=None):
            result = await _fetch_filings("0000000000", ["4"], limit=10)

        assert result == []


class TestParseInsiderTrades:
    @pytest.mark.asyncio
    async def test_xml_fail_produces_no_stub(self):
        """诚信原则（改进 0.1）：Form 4 XML 解析失败时不再落库占位空壳。"""
        filings = [
            {"id": "abc", "filing_type": "4", "filed_date": "2025-03-15",
             "title": "Form 4", "is_insider_trade": True},
            {"id": "def", "filing_type": "8-K", "filed_date": "2025-03-10",
             "title": "Current Report", "is_insider_trade": False},
        ]
        with patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_form4_xml", return_value=[]):
            trades = await _parse_insider_trades_from_filings("0000320193", "AAPL", filings)
        assert trades == []  # 无真实交易数据 → 不生成任何记录

    @pytest.mark.asyncio
    async def test_extracts_real_form4(self):
        """XML 解析成功时生成真实交易记录（含 shares/price/name）。"""
        filings = [{"id": "abc", "filing_type": "4", "filed_date": "2025-03-15",
                    "title": "Form 4", "is_insider_trade": True}]
        fake_xml = [{
            "insider_name": "Tim Cook", "insider_title": "CEO",
            "transaction_type": "Sale", "shares": 1000, "price": 150.0,
            "total_value": 150000.0, "date": "2025-03-14",
        }]
        with patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_form4_xml", return_value=fake_xml):
            trades = await _parse_insider_trades_from_filings("0000320193", "AAPL", filings)
        assert len(trades) == 1
        assert trades[0]["insider_name"] == "Tim Cook"
        assert trades[0]["shares"] == 1000
        assert trades[0]["price"] == 150.0

    @pytest.mark.asyncio
    async def test_empty_filings(self):
        assert await _parse_insider_trades_from_filings("0000320193", "AAPL", []) == []

    @pytest.mark.asyncio
    async def test_no_insider_filings(self):
        filings = [{"id": "x", "filing_type": "10-K", "filed_date": "2025-01-01",
                     "title": "Annual", "is_insider_trade": False}]
        assert await _parse_insider_trades_from_filings("0000320193", "AAPL", filings) == []

    @pytest.mark.asyncio
    async def test_real_record_id_is_deterministic(self):
        filings = [{"id": "abc", "filing_type": "4", "filed_date": "2025-03-15",
                     "title": "Form 4", "is_insider_trade": True}]
        fake_xml = [{"insider_name": "Tim Cook", "insider_title": "CEO",
                     "transaction_type": "Sale", "shares": 1000, "price": 150.0,
                     "total_value": 150000.0, "date": "2025-03-14"}]
        with patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_form4_xml", return_value=fake_xml):
            t1 = await _parse_insider_trades_from_filings("0000320193", "AAPL", filings)
            t2 = await _parse_insider_trades_from_filings("0000320193", "AAPL", filings)
        assert t1[0]["id"] == t2[0]["id"]
        expected = hashlib.md5("AAPL:insider:abc:0".encode()).hexdigest()[:12]
        assert t1[0]["id"] == expected


class TestFetchOne:
    @pytest.mark.asyncio
    async def test_no_cik(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.sec_pipeline._get_cik", return_value=None):
            result = await _fetch_one("FAKE", store)
        assert result == {"filings": 0, "trades": 0}

    @pytest.mark.asyncio
    async def test_no_filings(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.sec_pipeline._get_cik", return_value="0000320193"), \
             patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_filings", return_value=[]):
            result = await _fetch_one("AAPL", store)
        assert result == {"filings": 0, "trades": 0}

    @pytest.mark.asyncio
    async def test_filings_saved(self):
        filings = [
            {"id": "abc", "filing_type": "8-K", "filed_date": "2025-03-15",
             "title": "Current Report", "url": "", "is_insider_trade": False, "accession": "0001-23-000001"},
        ]
        store = MagicMock()
        store.save_filings.return_value = 1
        store.save_insider_trades.return_value = 0

        with patch("bottleneck_hunter.watchlist.sec_pipeline._get_cik", return_value="0000320193"), \
             patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_filings", return_value=filings):
            result = await _fetch_one("AAPL", store)

        assert result["filings"] == 1
        store.save_filings.assert_called_once()


class TestFetchSecBatch:
    @pytest.mark.asyncio
    async def test_empty(self):
        store = MagicMock()
        result = await fetch_sec_batch([], store)
        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_tickers(self):
        store = MagicMock()
        with patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_one") as mock_fetch:
            mock_fetch.return_value = {"filings": 3, "trades": 1}
            result = await fetch_sec_batch(["AAPL", "MSFT"], store)
        assert len(result) == 2
        assert result["AAPL"]["filings"] == 3

    @pytest.mark.asyncio
    async def test_error_handled(self):
        store = MagicMock()
        call_count = 0

        async def side_effect(ticker, s):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("fail")
            return {"filings": 1, "trades": 0}

        with patch("bottleneck_hunter.watchlist.sec_pipeline._fetch_one", side_effect=side_effect):
            result = await fetch_sec_batch(["BAD", "GOOD"], store)
        assert result["BAD"] == {"filings": 0, "trades": 0}
        assert result["GOOD"]["filings"] == 1
