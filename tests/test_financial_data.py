"""Tests for financial_data.py — mocked, no network calls."""

import asyncio
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from bottleneck_hunter.chain.financial_data import (
    _compute_volume_metrics,
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


class TestComputeVolumeMetrics:
    def test_basic(self):
        volumes = [100.0] * 60
        closes = [10.0] * 60
        vr, c3m, c1m, consec = _compute_volume_metrics(volumes, closes)
        assert vr == 1.0
        assert c3m == 0.0
        assert c1m == 0.0
        assert consec == 0

    def test_too_few_data(self):
        vr, c3m, c1m, consec = _compute_volume_metrics([100] * 10, [10] * 10)
        assert vr is None
        assert consec == 0

    def test_outlier_filtering(self):
        volumes = [100.0] * 50 + [100.0] * 9 + [5000.0]
        closes = [10.0] * 60
        vr, _, _, _ = _compute_volume_metrics(volumes, closes)
        assert vr is not None
        assert vr < 4.0

    def test_consecutive_volume(self):
        base = [100.0] * 50
        recent = [100.0] * 4 + [200.0, 200.0, 200.0] + [100.0] * 3
        volumes = base + recent
        closes = [10.0] * 60
        _, _, _, consec = _compute_volume_metrics(volumes, closes)
        assert consec == 3

    def test_price_change(self):
        volumes = [100.0] * 60
        closes = [10.0] * 40 + [12.0] * 20
        vr, c3m, c1m, consec = _compute_volume_metrics(volumes, closes)
        assert c3m == pytest.approx(20.0, abs=0.5)
        assert c1m == 0.0

    def test_zero_avg_volume(self):
        vr, _, _, _ = _compute_volume_metrics([0.0] * 60, [10.0] * 60)
        assert vr is None


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
            "日期": ["2026-05-01"] * 10 + ["2025-08-01"] * 18,
            "机构名称": [f"券商{i % 8}" for i in range(28)],
            "机构评级": ["买入"] * 28,
        })
        mock_ak.stock_research_report_em.return_value = df_rpt

        df_hist = pd.DataFrame({
            "成交量": [100000.0] * 60,
            "收盘": [50.0] * 60,
        })
        mock_ak.stock_zh_a_hist.return_value = df_hist

        snap = _fetch_astock_financial("600519")
        assert snap.data_source == "akshare_ths"
        assert snap.revenue_yi is not None
        assert snap.analyst_report_count == 8
        assert snap.analyst_rating == "买入"
        assert snap.volume_ratio is not None

    @patch("bottleneck_hunter.chain.financial_data.ak")
    def test_empty_data(self, mock_ak):
        mock_ak.stock_financial_abstract_ths.return_value = pd.DataFrame()
        mock_ak.stock_research_report_em.return_value = None
        mock_ak.stock_zh_a_hist.return_value = None
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
            "heldPercentInstitutions": 0.72,
            "firstTradeDateEpochUtc": 917015400,
        }
        mock_hist = pd.DataFrame({
            "Volume": [1000000.0] * 60,
            "Close": [100.0] * 60,
        })
        mock_ticker.history.return_value = mock_hist
        mock_ticker.quarterly_financials = pd.DataFrame()
        mock_ticker.quarterly_income_stmt = pd.DataFrame()
        mock_yf.Ticker.return_value = mock_ticker

        snap = _fetch_us_financial("NVDA")
        assert snap.data_source == "yfinance"
        assert snap.revenue_yi is not None
        assert snap.consensus_eps == 3.2
        assert snap.analyst_rating == "buy"
        assert snap.analyst_report_count == 35
        assert snap.institution_holding_pct == 72.0
        assert snap.days_since_ipo is not None and snap.days_since_ipo > 0
        assert snap.volume_ratio is not None

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
            results, failed = await fetch_batch(suppliers)
            assert len(results) == 2
            assert "600519.SH" in results
            assert "NVDA" in results
            assert failed == []
