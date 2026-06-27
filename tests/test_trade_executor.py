"""Tests for trade_executor.py — 交易执行与自动复盘。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from bottleneck_hunter.watchlist.trade_executor import execute_trade, _auto_review_sell
from bottleneck_hunter.watchlist.constraint_validator import ValidationResult


def _pass_validation(*args, **kwargs):
    return ValidationResult()


def _mock_store(*, account=None, position=None, plan=None):
    store = MagicMock()
    store.get_sim_account.return_value = account or {
        "id": "acc1", "cash_balance": 100000, "initial_capital": 100000,
        "total_equity": 100000, "total_return_pct": 0.0,
    }
    store.get_sim_position.return_value = position
    store.get_sim_positions.return_value = []
    store.get_execution_plan.return_value = plan
    store.create_sim_trade.return_value = "trade_123"
    store.get_sim_trades.return_value = []
    return store


@patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan", _pass_validation)
class TestExecuteTrade:
    def test_missing_plan_raises(self):
        store = _mock_store(plan=None)
        with pytest.raises(ValueError, match="不存在"):
            execute_trade(store, "nonexistent")

    def test_buy_success(self):
        store = _mock_store(plan={
            "action": "buy", "ticker": "AAPL", "shares": 100,
            "target_price": 150.0, "entry_id": "e1",
            "result_json": {"reasoning": "test"},
        })
        result = execute_trade(store, "plan1")
        assert result["side"] == "buy"
        assert result["ticker"] == "AAPL"
        assert result["shares"] == 100
        store.create_sim_trade.assert_called_once()

    def test_buy_insufficient_cash(self):
        store = _mock_store(
            account={"id": "acc1", "cash_balance": 100, "initial_capital": 100000},
            plan={
                "action": "buy", "ticker": "AAPL", "shares": 100,
                "target_price": 150.0, "entry_id": "e1",
                "result_json": {"reasoning": "test"},
            },
        )
        result = execute_trade(store, "plan1")
        assert "error" in result
        assert "现金不足" in result["error"]

    def test_sell_success(self):
        store = _mock_store(
            position={"id": "pos1", "shares": 100, "avg_cost": 100.0},
            plan={
                "action": "sell", "ticker": "AAPL", "shares": 50,
                "target_price": 120.0, "entry_id": "e1",
                "result_json": {"reasoning": "take profit"},
            },
        )
        result = execute_trade(store, "plan1")
        assert result["side"] == "sell"
        assert result["realized_pnl"] > 0

    def test_sell_insufficient_shares(self):
        store = _mock_store(
            position={"id": "pos1", "shares": 10, "avg_cost": 100.0},
            plan={
                "action": "sell", "ticker": "AAPL", "shares": 50,
                "target_price": 120.0, "entry_id": "e1",
                "result_json": {"reasoning": "exit"},
            },
        )
        result = execute_trade(store, "plan1")
        assert "error" in result
        assert "持仓不足" in result["error"]

    def test_missing_fields_returns_error(self):
        store = _mock_store(plan={
            "action": "", "ticker": "", "shares": 0,
            "target_price": 0, "entry_id": None,
            "result_json": {},
        })
        result = execute_trade(store, "plan1")
        assert "error" in result

    def test_unsupported_action(self):
        store = _mock_store(plan={
            "action": "short", "ticker": "AAPL", "shares": 100,
            "target_price": 150.0, "entry_id": "e1",
            "result_json": {},
        })
        result = execute_trade(store, "plan1")
        assert "error" in result
        assert "不支持" in result["error"]


@patch("bottleneck_hunter.watchlist.constraint_validator.validate_execution_plan", _pass_validation)
class TestSellTriggersAutoReview:
    def test_sell_triggers_auto_review(self):
        """卖出成功后应触发 _auto_review_sell 的 asyncio task。"""
        store = _mock_store(
            position={"id": "pos1", "shares": 100, "avg_cost": 100.0},
            plan={
                "action": "sell", "ticker": "AAPL", "shares": 50,
                "target_price": 120.0, "entry_id": "e1",
                "result_json": {"reasoning": "take profit"},
            },
        )
        mock_loop = MagicMock()
        mock_loop.create_task = MagicMock()

        with patch("asyncio.get_event_loop", return_value=mock_loop):
            result = execute_trade(store, "plan1")

        assert result["side"] == "sell"
        mock_loop.create_task.assert_called_once()

    def test_buy_no_auto_review(self):
        """买入不触发自动复盘。"""
        store = _mock_store(plan={
            "action": "buy", "ticker": "AAPL", "shares": 100,
            "target_price": 150.0, "entry_id": "e1",
            "result_json": {"reasoning": "entry"},
        })
        mock_loop = MagicMock()
        mock_loop.create_task = MagicMock()

        with patch("asyncio.get_event_loop", return_value=mock_loop):
            result = execute_trade(store, "plan1")

        assert result["side"] == "buy"
        mock_loop.create_task.assert_not_called()

    def test_sell_error_no_auto_review(self):
        """卖出失败不触发复盘。"""
        store = _mock_store(
            position={"id": "pos1", "shares": 10, "avg_cost": 100.0},
            plan={
                "action": "sell", "ticker": "AAPL", "shares": 50,
                "target_price": 120.0, "entry_id": "e1",
                "result_json": {"reasoning": "exit"},
            },
        )
        mock_loop = MagicMock()
        mock_loop.create_task = MagicMock()

        with patch("asyncio.get_event_loop", return_value=mock_loop):
            result = execute_trade(store, "plan1")

        assert "error" in result
        mock_loop.create_task.assert_not_called()


class TestAutoReviewSell:
    @pytest.mark.asyncio
    async def test_budget_insufficient_skips(self):
        """预算不足时跳过复盘。"""
        store = MagicMock()
        store.get_budget_limits.return_value = {"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0}
        store.get_daily_usage.return_value = {"cost": 1.9, "input_tokens": 0, "output_tokens": 0}
        store.get_monthly_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}

        await _auto_review_sell(store, "trade_123")

    @pytest.mark.asyncio
    async def test_runs_trade_review(self):
        """预算充足时调用 run_trade_review。"""
        store = MagicMock()
        store.get_budget_limits.return_value = {"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0}
        store.get_daily_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
        store.get_monthly_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}

        async def mock_review(*args, **kwargs):
            yield {"event": "review_done", "data": {"event": "review_done", "message": "ok"}}

        with patch("bottleneck_hunter.watchlist.trade_reviewer.run_trade_review", side_effect=mock_review):
            await _auto_review_sell(store, "trade_123")

    @pytest.mark.asyncio
    async def test_handles_exception(self):
        """异常不传播。"""
        store = MagicMock()
        store.get_budget_limits.return_value = {"daily_limit_usd": 2.0, "monthly_limit_usd": 30.0}
        store.get_daily_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}
        store.get_monthly_usage.return_value = {"cost": 0.0, "input_tokens": 0, "output_tokens": 0}

        with patch("bottleneck_hunter.watchlist.trade_reviewer.run_trade_review", side_effect=RuntimeError("boom")):
            await _auto_review_sell(store, "trade_123")
