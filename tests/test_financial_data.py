"""Tests for financial_data.py — mocked, no network calls."""

import asyncio
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.chain.financial_data import (
    _extract_astock_code,
    _fetch_astock_financial,
    _fetch_us_financial,
    _safe_float,
    _safe_int,
    fetch_batch,
    fetch_financial_snapshot,
)
from bottleneck_hunter.chain.models import MarketRegion, SupplierInfo


class TestHelpers:
    def test_safe_float_basic(self):
        assert _safe_float(123.45) == 123.45
        assert _safe_float("100.5") == 100.5
        assert _safe_float(None) is None
        assert _safe_float("abc") is None

    def test_safe_float_scale(self):
        assert _safe_float(1_000_000_000, 1e-8) == 10.0

    def test_safe_float_strip_comma(self):
        assert _safe_float("1,234.56") == 1234.56

    def test_safe_int(self):
        assert _safe_int(42) == 42
        assert _safe_int("3.7") == 3
        assert _safe_int(None) is None
        assert _safe_int("abc") is None

    def test_extract_astock_code(self):
        assert _extract_astock_code("600519.SH") == "600519"
        assert _extract_astock_code("688012") == "688012"
        assert _extract_astock_code("AAPL") is None
        assert _extract_astock_code("12345") is None


class TestFetchAstock:
    @patch("bottleneck_hunter.chain.financial_data.ak")
    def test_success(self, mock_ak):
        df_fin = pd.DataFrame({
            "报告期": ["2025-12-31"],
            "营业总收入": [12_050_000_000],
            "营业总收入同比增长率": [15.3],
            "归母净利润": [1_820_000_000],
            "归母净利润同比增长率": [22.1],
            "销售毛利率": [35.0],
            "净资产收益率": [18.5],
            "资产负债率": [42.0],
            "每股经营现金流": [1.25],
        })
        mock_ak.stock_financial_abstract_ths.return_value = df_fin

        df_rpt = pd.DataFrame({
            "title": [f"研报{i}" for i in range(28)],
            "机构评级": ["买入"] * 28,
        })
        mock_ak.stock_research_report_em.return_value = df_rpt

        snap = _fetch_astock_financial("600519")
        assert snap.data_source == "akshare_ths"
        assert snap.revenue_yi is not None
        assert snap.analyst_report_count == 28
        assert snap.analyst_rating == "买入"

    @patch("bottleneck_hunter.chain.financial_data.ak")
    def test_empty_data(self, mock_ak):
        mock_ak.stock_financial_abstract_ths.return_value = pd.DataFrame()
        mock_ak.stock_research_report_em.return_value = None
        snap = _fetch_astock_financial("000001")
        assert snap.data_source == "akshare_ths"
        assert snap.revenue_yi is None
        assert snap.analyst_report_count is None


class TestFetchUS:
    @patch("bottleneck_hunter.chain.financial_data.yf")
    def test_success(self, mock_yf):
        mock_ticker = MagicMock()
        mock_ticker.info = {
            "totalRevenue": 50_000_000_000,
            "revenueGrowth": 0.153,
            "netIncomeToCommon": 8_000_000_000,
            "grossMargins": 0.42,
            "returnOnEquity": 0.25,
            "debtToEquity": 65.0,
            "forwardEps": 3.2,
            "forwardPE": 28.5,
            "recommendationKey": "buy",
            "numberOfAnalystOpinions": 35,
            "mostRecentQuarter": 1735084800,  # ~2024-12-25
        }
        mock_yf.Ticker.return_value = mock_ticker

        snap = _fetch_us_financial("NVDA")
        assert snap.data_source == "yfinance"
        assert snap.revenue_yi is not None
        assert snap.consensus_eps == 3.2
        assert snap.analyst_rating == "buy"
        assert snap.analyst_report_count == 35

    @patch("bottleneck_hunter.chain.financial_data.yf")
    def test_empty_info(self, mock_yf):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_yf.Ticker.return_value = mock_ticker

        snap = _fetch_us_financial("XXXX")
        assert snap.data_source == "yfinance"
        assert snap.revenue_yi is None


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_astock_supplier(self):
        supplier = SupplierInfo(
            name="中微公司", ticker="688012.SH", market=MarketRegion.A_STOCK,
            sector="半导体设备", description="刻蚀设备",
        )
        with patch("bottleneck_hunter.chain.financial_data._fetch_astock_financial") as mock_fn:
            from bottleneck_hunter.chain.models import FinancialSnapshot
            mock_fn.return_value = FinancialSnapshot(data_source="akshare_ths", revenue_yi=80.0)
            snap = await fetch_financial_snapshot(supplier)
            assert snap is not None
            assert snap.revenue_yi == 80.0
            mock_fn.assert_called_once_with("688012")

    @pytest.mark.asyncio
    async def test_invalid_ticker(self):
        supplier = SupplierInfo(
            name="Unknown", ticker="INVALID", market=MarketRegion.A_STOCK,
            sector="test", description="test",
        )
        snap = await fetch_financial_snapshot(supplier)
        assert snap is None


class TestFetchBatch:
    @pytest.mark.asyncio
    async def test_batch(self):
        suppliers = [
            SupplierInfo(name="A公司", ticker="600519.SH", market=MarketRegion.A_STOCK, sector="白酒", description="d"),
            SupplierInfo(name="NVDA", ticker="NVDA", market=MarketRegion.US_STOCK, sector="GPU", description="d"),
        ]
        with patch("bottleneck_hunter.chain.financial_data.fetch_financial_snapshot") as mock_fn:
            from bottleneck_hunter.chain.models import FinancialSnapshot
            mock_fn.side_effect = [
                FinancialSnapshot(data_source="akshare_ths", revenue_yi=100.0),
                FinancialSnapshot(data_source="yfinance", revenue_yi=50.0),
            ]
            results = await fetch_batch(suppliers)
            assert len(results) == 2
            assert "600519.SH" in results
            assert "NVDA" in results
