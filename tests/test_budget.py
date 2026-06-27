"""Tests for budget.py — LLM 预算跟踪与三级降级。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bottleneck_hunter.watchlist.budget import (
    COST_TABLE,
    BudgetTracker,
    estimate_cost,
)
from bottleneck_hunter.watchlist.models import DegradationMode


class TestCostTable:
    def test_known_providers(self):
        expected = {"deepseek", "openai", "anthropic", "google", "qwen", "glm", "openrouter", "ollama", "kimi"}
        assert expected.issubset(set(COST_TABLE.keys()))

    def test_each_provider_has_input_output(self):
        for provider, rates in COST_TABLE.items():
            assert "input" in rates, f"{provider} missing input rate"
            assert "output" in rates, f"{provider} missing output rate"

    def test_ollama_free(self):
        assert COST_TABLE["ollama"]["input"] == 0.0
        assert COST_TABLE["ollama"]["output"] == 0.0


class TestEstimateCost:
    def test_openai_cost(self):
        cost = estimate_cost("openai", 1_000_000, 0)
        assert cost == 2.5

    def test_openai_output(self):
        cost = estimate_cost("openai", 0, 1_000_000)
        assert cost == 10.0

    def test_deepseek_cheap(self):
        cost = estimate_cost("deepseek", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.42, abs=0.01)

    def test_ollama_zero(self):
        cost = estimate_cost("ollama", 100000, 50000)
        assert cost == 0.0

    def test_unknown_provider_defaults_to_openai(self):
        cost = estimate_cost("unknown_provider", 1_000_000, 0)
        assert cost == 2.5

    def test_zero_tokens(self):
        assert estimate_cost("openai", 0, 0) == 0.0

    def test_small_cost_precision(self):
        cost = estimate_cost("openai", 100, 50)
        assert cost > 0
        assert len(str(cost).split(".")[-1]) <= 6


def _mock_store(*, daily_cost=0.0, daily_limit=2.0, monthly_cost=0.0, monthly_limit=30.0):
    store = MagicMock()
    store.get_budget_limits.return_value = {
        "daily_limit_usd": daily_limit,
        "monthly_limit_usd": monthly_limit,
    }
    store.get_daily_usage.return_value = {"cost": daily_cost, "input_tokens": 0, "output_tokens": 0}
    store.get_monthly_usage.return_value = {"cost": monthly_cost, "input_tokens": 0, "output_tokens": 0}
    return store


class TestDegradationMode:
    def test_full_when_low_usage(self):
        bt = BudgetTracker(_mock_store(daily_cost=0.5, daily_limit=2.0))
        assert bt.get_degradation_mode() == DegradationMode.FULL

    def test_reduced_at_70pct(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.4, daily_limit=2.0))
        assert bt.get_degradation_mode() == DegradationMode.REDUCED

    def test_minimal_at_90pct(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.8, daily_limit=2.0))
        assert bt.get_degradation_mode() == DegradationMode.MINIMAL

    def test_minimal_at_100pct(self):
        bt = BudgetTracker(_mock_store(daily_cost=2.0, daily_limit=2.0))
        assert bt.get_degradation_mode() == DegradationMode.MINIMAL

    def test_zero_limit_full(self):
        bt = BudgetTracker(_mock_store(daily_cost=0.0, daily_limit=0.0))
        assert bt.get_degradation_mode() == DegradationMode.FULL


class TestCanSpend:
    def test_can_spend_when_full(self):
        bt = BudgetTracker(_mock_store(daily_cost=0.0))
        assert bt.can_spend() is True

    def test_can_spend_when_reduced(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.5, daily_limit=2.0))
        assert bt.can_spend() is True

    def test_cannot_spend_when_minimal(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.9, daily_limit=2.0))
        assert bt.can_spend() is False


class TestRecord:
    def test_records_usage(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.record("openai", "gpt-4", 1000, 500, task_type="analysis")

        store.record_llm_usage.assert_called_once()
        record = store.record_llm_usage.call_args[0][0]
        assert record["provider"] == "openai"
        assert record["model"] == "gpt-4"
        assert record["input_tokens"] == 1000
        assert record["output_tokens"] == 500
        assert record["estimated_cost_usd"] > 0
        assert record["task_type"] == "analysis"

    def test_cost_matches_estimate(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.record("deepseek", "deepseek-chat", 10000, 5000)

        record = store.record_llm_usage.call_args[0][0]
        expected = estimate_cost("deepseek", 10000, 5000)
        assert record["estimated_cost_usd"] == expected


class TestGetStatus:
    def test_all_keys_present(self):
        bt = BudgetTracker(_mock_store(daily_cost=0.5, monthly_cost=8.0))
        status = bt.get_status()
        required = {"daily_cost", "daily_limit", "daily_pct", "monthly_cost",
                     "monthly_limit", "monthly_pct", "mode", "daily_input_tokens",
                     "daily_output_tokens"}
        assert required.issubset(status.keys())

    def test_pct_calculation(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.0, daily_limit=2.0, monthly_cost=15.0, monthly_limit=30.0))
        status = bt.get_status()
        assert status["daily_pct"] == 50.0
        assert status["monthly_pct"] == 50.0

    def test_zero_limit_pct_zero(self):
        bt = BudgetTracker(_mock_store(daily_limit=0.0, monthly_limit=0.0))
        status = bt.get_status()
        assert status["daily_pct"] == 0
        assert status["monthly_pct"] == 0

    def test_mode_in_status(self):
        bt = BudgetTracker(_mock_store(daily_cost=1.9, daily_limit=2.0))
        status = bt.get_status()
        assert status["mode"] == DegradationMode.MINIMAL.value


class TestSetLimits:
    def test_set_daily(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.set_limits(daily=5.0)
        store.set_budget_limit.assert_called_once_with("daily_limit_usd", 5.0)

    def test_set_monthly(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.set_limits(monthly=50.0)
        store.set_budget_limit.assert_called_once_with("monthly_limit_usd", 50.0)

    def test_set_both(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.set_limits(daily=3.0, monthly=40.0)
        assert store.set_budget_limit.call_count == 2

    def test_set_none_no_call(self):
        store = _mock_store()
        bt = BudgetTracker(store)
        bt.set_limits()
        store.set_budget_limit.assert_not_called()
