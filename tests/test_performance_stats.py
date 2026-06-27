"""Tests for performance_stats.py — 绩效统计计算。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bottleneck_hunter.watchlist.performance_stats import PerformanceCalculator


def _mock_store(*, trades=None, reviews=None, account=None, daily=None, monthly=None, limits=None):
    store = MagicMock()
    store.get_sim_trades.return_value = trades or []
    store.get_auto_reviews.return_value = reviews or []
    store.get_sim_account.return_value = account or {"initial_capital": 100000, "total_return_pct": 0.0}
    store.get_daily_usage.return_value = daily or {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    store.get_monthly_usage.return_value = monthly or {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
    store.get_budget_limits.return_value = limits or {"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0}
    return store


class TestComputeOverview:
    def test_no_data(self):
        calc = PerformanceCalculator(_mock_store())
        result = calc.compute_overview()
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["avg_return_pct"] == 0.0

    def test_with_reviews(self):
        reviews = [
            {"ticker": "AAPL", "return_pct": 10.5, "created_at": "2025-03-01"},
            {"ticker": "MSFT", "return_pct": -3.2, "created_at": "2025-03-05"},
            {"ticker": "GOOG", "return_pct": 5.0, "created_at": "2025-03-10"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_overview()
        assert result["wins"] == 2
        assert result["losses"] == 1
        assert result["best_trade_pct"] == 10.5
        assert result["worst_trade_pct"] == -3.2

    def test_with_trades_no_reviews(self):
        trades = [
            {"side": "buy", "ticker": "AAPL", "price": 100, "amount": 10000, "created_at": "2025-01-01"},
            {"side": "sell", "ticker": "AAPL", "price": 110, "amount": 11000, "created_at": "2025-02-01"},
        ]
        calc = PerformanceCalculator(_mock_store(trades=trades))
        result = calc.compute_overview()
        assert result["total_trades"] == 1
        assert result["wins"] == 1

    def test_win_rate_calculation(self):
        reviews = [
            {"ticker": "A", "return_pct": 5.0, "created_at": "2025-01-01"},
            {"ticker": "B", "return_pct": 8.0, "created_at": "2025-01-02"},
            {"ticker": "C", "return_pct": -2.0, "created_at": "2025-01-03"},
            {"ticker": "D", "return_pct": -1.0, "created_at": "2025-01-04"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_overview()
        assert result["win_rate"] == 50.0

    def test_total_return_from_account(self):
        account = {"initial_capital": 100000, "total_return_pct": 15.5}
        calc = PerformanceCalculator(_mock_store(account=account))
        result = calc.compute_overview()
        assert result["total_return_pct"] == 15.5


class TestComputeMonthlySeries:
    def test_empty(self):
        calc = PerformanceCalculator(_mock_store())
        result = calc.compute_monthly_series()
        assert result == []

    def test_groups_by_month(self):
        reviews = [
            {"ticker": "A", "return_pct": 5.0, "created_at": "2025-01-15"},
            {"ticker": "B", "return_pct": -2.0, "created_at": "2025-01-20"},
            {"ticker": "C", "return_pct": 10.0, "created_at": "2025-02-10"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_monthly_series(months=6)
        assert len(result) == 2
        jan = next(m for m in result if m["month"] == "2025-01")
        assert jan["trades"] == 2
        assert jan["wins"] == 1

    def test_limits_months(self):
        reviews = [
            {"ticker": "A", "return_pct": 1.0, "created_at": f"2025-{m:02d}-01"}
            for m in range(1, 13)
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_monthly_series(months=3)
        assert len(result) == 3

    def test_sorted_chronologically(self):
        reviews = [
            {"ticker": "A", "return_pct": 1.0, "created_at": "2025-03-01"},
            {"ticker": "B", "return_pct": 2.0, "created_at": "2025-01-01"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_monthly_series()
        assert result[0]["month"] < result[1]["month"]


class TestComputeDrawdown:
    def test_no_trades(self):
        calc = PerformanceCalculator(_mock_store())
        result = calc.compute_drawdown()
        assert result["max_drawdown_pct"] == 0.0
        assert result["peak_date"] == ""
        assert result["trough_date"] == ""

    def test_simple_drawdown(self):
        trades = [
            {"side": "buy", "ticker": "AAPL", "amount": 50000, "created_at": "2025-01-01"},
            {"side": "sell", "ticker": "AAPL", "amount": 30000, "created_at": "2025-02-01"},
        ]
        calc = PerformanceCalculator(_mock_store(trades=trades))
        result = calc.compute_drawdown()
        assert result["max_drawdown_pct"] > 0

    def test_no_drawdown_only_gains(self):
        trades = [
            {"side": "sell", "ticker": "AAPL", "amount": 20000, "created_at": "2025-01-01"},
        ]
        calc = PerformanceCalculator(_mock_store(trades=trades))
        result = calc.compute_drawdown()
        assert result["max_drawdown_pct"] == 0.0


class TestComputeByTicker:
    def test_empty(self):
        calc = PerformanceCalculator(_mock_store())
        result = calc.compute_by_ticker()
        assert result == []

    def test_groups_tickers(self):
        reviews = [
            {"ticker": "AAPL", "return_pct": 5.0, "created_at": "2025-01-01"},
            {"ticker": "AAPL", "return_pct": -2.0, "created_at": "2025-02-01"},
            {"ticker": "MSFT", "return_pct": 8.0, "created_at": "2025-01-15"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_by_ticker()
        assert len(result) == 2
        aapl = next(r for r in result if r["ticker"] == "AAPL")
        assert aapl["trades"] == 2
        assert aapl["wins"] == 1
        assert aapl["best_pct"] == 5.0
        assert aapl["worst_pct"] == -2.0

    def test_sorted_by_trade_count(self):
        reviews = [
            {"ticker": "AAPL", "return_pct": 5.0, "created_at": "2025-01-01"},
            {"ticker": "AAPL", "return_pct": 3.0, "created_at": "2025-01-02"},
            {"ticker": "MSFT", "return_pct": 8.0, "created_at": "2025-01-15"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_by_ticker()
        assert result[0]["ticker"] == "AAPL"


class TestComputeReviewSummary:
    def test_no_reviews(self):
        calc = PerformanceCalculator(_mock_store())
        result = calc.compute_review_summary()
        assert result["total_reviews"] == 0
        assert result["avg_quality_score"] == 0.0
        assert result["common_lessons"] == []

    def test_with_reviews(self):
        reviews = [
            {"ticker": "A", "result_json": {"trade_quality_score": 8, "key_lessons": ["及时止损"]}, "created_at": "2025-01-01"},
            {"ticker": "B", "result_json": {"trade_quality_score": 6, "key_lessons": ["及时止损", "仓位控制"]}, "created_at": "2025-01-02"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_review_summary()
        assert result["total_reviews"] == 2
        assert result["avg_quality_score"] == 7.0
        assert result["common_lessons"][0]["lesson"] == "及时止损"
        assert result["common_lessons"][0]["count"] == 2

    def test_missing_result_json(self):
        reviews = [
            {"ticker": "A", "result_json": None, "created_at": "2025-01-01"},
            {"ticker": "B", "result_json": {}, "created_at": "2025-01-02"},
        ]
        calc = PerformanceCalculator(_mock_store(reviews=reviews))
        result = calc.compute_review_summary()
        assert result["avg_quality_score"] == 0.0


class TestComputeCostSummary:
    def test_returns_all_keys(self):
        calc = PerformanceCalculator(_mock_store(
            daily={"cost": 0.5, "input_tokens": 10000, "output_tokens": 5000},
            monthly={"cost": 8.0, "input_tokens": 200000, "output_tokens": 100000},
            limits={"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0},
        ))
        result = calc.compute_cost_summary()
        assert result["daily_cost"] == 0.5
        assert result["daily_limit"] == 2.0
        assert result["monthly_cost"] == 8.0
        assert result["monthly_limit"] == 30.0
        assert result["daily_tokens"] == 15000
        assert result["monthly_tokens"] == 300000


class TestCalcReturn:
    def test_positive_return(self):
        calc = PerformanceCalculator(_mock_store())
        sell = {"ticker": "AAPL", "price": 110}
        buys = [{"ticker": "AAPL", "price": 100}]
        ret = calc._calc_return(sell, buys)
        assert ret == 10.0

    def test_negative_return(self):
        calc = PerformanceCalculator(_mock_store())
        sell = {"ticker": "AAPL", "price": 90}
        buys = [{"ticker": "AAPL", "price": 100}]
        ret = calc._calc_return(sell, buys)
        assert ret == -10.0

    def test_no_matching_buy(self):
        calc = PerformanceCalculator(_mock_store())
        sell = {"ticker": "AAPL", "price": 110}
        buys = [{"ticker": "MSFT", "price": 100}]
        ret = calc._calc_return(sell, buys)
        assert ret == 0.0

    def test_zero_entry_price(self):
        calc = PerformanceCalculator(_mock_store())
        sell = {"ticker": "AAPL", "price": 110}
        buys = [{"ticker": "AAPL", "price": 0}]
        ret = calc._calc_return(sell, buys)
        assert ret == 0.0


class TestAvgHoldingDays:
    def test_basic_pair(self):
        calc = PerformanceCalculator(_mock_store())
        trades = [
            {"side": "buy", "ticker": "AAPL", "created_at": "2025-01-01T00:00:00+00:00"},
            {"side": "sell", "ticker": "AAPL", "created_at": "2025-01-11T00:00:00+00:00"},
        ]
        assert calc._avg_holding_days(trades) == 10

    def test_no_trades(self):
        calc = PerformanceCalculator(_mock_store())
        assert calc._avg_holding_days([]) == 0

    def test_malformed_dates(self):
        calc = PerformanceCalculator(_mock_store())
        trades = [
            {"side": "buy", "ticker": "AAPL", "created_at": "bad-date"},
            {"side": "sell", "ticker": "AAPL", "created_at": "also-bad"},
        ]
        assert calc._avg_holding_days(trades) == 0

    def test_z_suffix_handled(self):
        calc = PerformanceCalculator(_mock_store())
        trades = [
            {"side": "buy", "ticker": "AAPL", "created_at": "2025-01-01T00:00:00Z"},
            {"side": "sell", "ticker": "AAPL", "created_at": "2025-01-06T00:00:00Z"},
        ]
        assert calc._avg_holding_days(trades) == 5
