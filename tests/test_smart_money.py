"""Tests for smart_money — 聪明钱追踪。"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
import pandas as pd

from bottleneck_hunter.chain.smart_money import (
    _safe_float,
    _track_astock,
    _track_us_stock,
    _extract_astock_code,
    track_smart_money,
    track_batch,
)
from bottleneck_hunter.chain.models import MarketRegion, SmartMoneySignal, SupplierInfo


def _make_supplier(name="TestCo", ticker="600519.SH", market=MarketRegion.A_STOCK):
    return SupplierInfo(name=name, ticker=ticker, market=market, sector="白酒", description="desc")


class TestSafeFloat:
    def test_normal(self):
        assert _safe_float(3.14) == 3.14

    def test_string(self):
        assert _safe_float("1,234.56") == 1234.56

    def test_percentage(self):
        assert _safe_float("12.5%") == 12.5

    def test_scale(self):
        assert _safe_float(100, scale=0.01) == 1.0

    def test_none(self):
        assert _safe_float(None) is None

    def test_invalid(self):
        assert _safe_float("abc") is None


class TestExtractAstockCode:
    def test_normal(self):
        assert _extract_astock_code("600519.SH") == "600519"

    def test_no_suffix(self):
        assert _extract_astock_code("600519") == "600519"

    def test_invalid(self):
        assert _extract_astock_code("AAPL") is None

    def test_short_code(self):
        assert _extract_astock_code("123") is None


class TestTrackAstock:
    @patch("bottleneck_hunter.chain.smart_money.ak")
    def test_bullish_signal(self, mock_ak):
        flow_df = pd.DataFrame({"日期": ["2025-01-01"] * 5, "主力净流入-净额": [1000000] * 5})
        mock_ak.stock_individual_fund_flow.return_value = flow_df
        mock_ak.stock_margin_detail_sse.side_effect = Exception("skip")
        mock_ak.stock_hsgt_individual_em.side_effect = Exception("skip")
        mock_ak.stock_lhb_jgmmtj_em.side_effect = Exception("skip")

        signal = _track_astock("600519")
        assert isinstance(signal, SmartMoneySignal)
        assert signal.smart_money_score >= 5.0

    @patch("bottleneck_hunter.chain.smart_money.ak")
    def test_all_data_fail(self, mock_ak):
        mock_ak.stock_individual_fund_flow.side_effect = Exception("fail")
        mock_ak.stock_margin_detail_sse.side_effect = Exception("fail")
        mock_ak.stock_hsgt_individual_em.side_effect = Exception("fail")
        mock_ak.stock_lhb_jgmmtj_em.side_effect = Exception("fail")

        signal = _track_astock("600519")
        assert signal.smart_money_score == 5.0
        assert signal.signal_direction == "neutral"

    @patch("bottleneck_hunter.chain.smart_money.ak")
    def test_bearish_signal(self, mock_ak):
        flow_df = pd.DataFrame({"日期": ["2025-01-01"] * 5, "主力净流入-净额": [-10000000] * 5})
        mock_ak.stock_individual_fund_flow.return_value = flow_df
        mock_ak.stock_margin_detail_sse.side_effect = Exception("skip")
        mock_ak.stock_hsgt_individual_em.side_effect = Exception("skip")
        mock_ak.stock_lhb_jgmmtj_em.side_effect = Exception("skip")

        signal = _track_astock("600519")
        assert signal.smart_money_score <= 5.0


class TestTrackUsStock:
    @patch("bottleneck_hunter.chain.smart_money.yf")
    def test_bullish_with_institutions(self, mock_yf):
        mock_stock = MagicMock()
        mock_stock.info = {"shortPercentOfFloat": 0.02}
        mock_stock.institutional_holders = pd.DataFrame({"Holder": [f"Fund{i}" for i in range(10)]})
        mock_stock.insider_transactions = pd.DataFrame()
        mock_stock.recommendations = pd.DataFrame()
        mock_yf.Ticker.return_value = mock_stock

        signal = _track_us_stock("AAPL")
        assert signal.smart_money_score >= 6.0
        assert signal.institution_count == 10

    @patch("bottleneck_hunter.chain.smart_money.yf")
    def test_all_fail(self, mock_yf):
        mock_yf.Ticker.side_effect = Exception("yfinance down")
        signal = _track_us_stock("AAPL")
        assert signal.smart_money_score == 5.0


class TestTrackSmartMoney:
    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.smart_money._track_astock")
    async def test_astock(self, mock_track):
        mock_track.return_value = SmartMoneySignal(smart_money_score=7.0)
        s = _make_supplier(ticker="600519.SH", market=MarketRegion.A_STOCK)
        result = await track_smart_money(s)
        assert result is not None
        assert result.smart_money_score == 7.0

    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.smart_money._track_us_stock")
    async def test_us_stock(self, mock_track):
        mock_track.return_value = SmartMoneySignal(smart_money_score=6.0)
        s = _make_supplier(name="AAPL", ticker="AAPL", market=MarketRegion.US_STOCK)
        result = await track_smart_money(s)
        assert result is not None
        assert result.smart_money_score == 6.0

    @pytest.mark.asyncio
    async def test_invalid_market(self):
        s = _make_supplier()
        s.market = "unknown"
        result = await track_smart_money(s)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_astock_code(self):
        s = _make_supplier(ticker="INVALID")
        result = await track_smart_money(s)
        assert result is None


class TestTrackBatch:
    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.smart_money.track_smart_money")
    async def test_batch_success(self, mock_track):
        mock_track.return_value = SmartMoneySignal(smart_money_score=7.0)
        suppliers = [_make_supplier(name=f"Co{i}", ticker=f"60051{i}.SH") for i in range(3)]
        results, failed = await track_batch(suppliers)
        assert len(results) == 3
        assert len(failed) == 0

    @pytest.mark.asyncio
    @patch("bottleneck_hunter.chain.smart_money.track_smart_money")
    async def test_batch_with_retry(self, mock_track):
        call_count = 0

        async def _side_effect(s):
            nonlocal call_count
            call_count += 1
            if s.ticker == "600510.SH" and call_count <= 1:
                return None
            return SmartMoneySignal(smart_money_score=5.0)

        mock_track.side_effect = _side_effect
        suppliers = [_make_supplier(ticker="600510.SH"), _make_supplier(ticker="600520.SH")]
        results, failed = await track_batch(suppliers)
        assert "600520.SH" in results
