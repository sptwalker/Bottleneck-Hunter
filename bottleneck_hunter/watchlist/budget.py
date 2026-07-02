"""LLM budget tracking with three-tier degradation.

Tracks daily/monthly token usage and costs, enforces configurable limits,
and provides degradation mode to avoid overspend.
"""

from __future__ import annotations

import logging

from bottleneck_hunter.watchlist.models import DegradationMode
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)

# 每百万 token 估算成本（USD）
COST_TABLE: dict[str, dict[str, float]] = {
    "deepseek": {"input": 0.14, "output": 0.28},
    "openai": {"input": 2.50, "output": 10.0},
    "anthropic": {"input": 3.00, "output": 15.0},
    "google": {"input": 1.25, "output": 5.0},
    "qwen": {"input": 0.50, "output": 1.50},
    "glm": {"input": 0.30, "output": 0.60},
    "openrouter": {"input": 2.00, "output": 8.0},
    "ollama": {"input": 0.0, "output": 0.0},
    "kimi": {"input": 0.20, "output": 0.40},
}


def estimate_cost(provider: str, input_tokens: int, output_tokens: int) -> float:
    rates = COST_TABLE.get(provider, COST_TABLE.get("openai", {"input": 2.5, "output": 10.0}))
    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return round(cost, 6)


class BudgetTracker:
    """Tracks LLM usage and enforces daily/monthly limits."""

    def __init__(self, store: WatchlistStore):
        self._store = store

    def _limits(self) -> dict[str, float]:
        return self._store.get_budget_limits()

    def can_spend(self, estimated_tokens: int = 3000, provider: str = "openai") -> bool:
        """Check if an LLM call is within budget.

        G-6 硬熔断：日累计≥100% 或 月累计≥100% 时【硬停】，不再仅靠 MINIMAL 软降级。
        （软降级只在 90% 触发且仍允许部分调用；硬上限防止失控重试/误配高价模型烧穿预算。）
        """
        limits = self._limits()
        daily_limit = limits.get("daily_limit_usd", 2.0)
        monthly_limit = limits.get("monthly_limit_usd", 30.0)
        daily_cost = self._store.get_daily_usage().get("cost", 0.0)
        monthly_cost = self._store.get_monthly_usage().get("cost", 0.0)
        if daily_limit > 0 and daily_cost >= daily_limit:
            logger.warning("LLM 预算硬熔断：日累计 $%.4f ≥ 上限 $%.2f，拒绝调用", daily_cost, daily_limit)
            return False
        if monthly_limit > 0 and monthly_cost >= monthly_limit:
            logger.warning("LLM 预算硬熔断：月累计 $%.4f ≥ 上限 $%.2f，拒绝调用", monthly_cost, monthly_limit)
            return False
        mode = self.get_degradation_mode()
        if mode == DegradationMode.MINIMAL:
            return False
        return True

    def record(self, provider: str, model: str, input_tokens: int, output_tokens: int, task_type: str = "") -> None:
        cost = estimate_cost(provider, input_tokens, output_tokens)
        self._store.record_llm_usage({
            "provider": provider,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "task_type": task_type,
        })

    def get_degradation_mode(self) -> DegradationMode:
        limits = self._limits()
        daily_limit = limits.get("daily_limit_usd", 2.0)
        daily = self._store.get_daily_usage()
        ratio = daily["cost"] / daily_limit if daily_limit > 0 else 0.0
        if ratio >= 0.9:
            return DegradationMode.MINIMAL
        if ratio >= 0.7:
            return DegradationMode.REDUCED
        return DegradationMode.FULL

    def get_status(self) -> dict:
        limits = self._limits()
        daily = self._store.get_daily_usage()
        monthly = self._store.get_monthly_usage()
        daily_limit = limits.get("daily_limit_usd", 2.0)
        monthly_limit = limits.get("monthly_limit_usd", 30.0)
        return {
            "daily_cost": daily["cost"],
            "daily_limit": daily_limit,
            "daily_pct": round(daily["cost"] / daily_limit * 100, 1) if daily_limit > 0 else 0,
            "monthly_cost": monthly["cost"],
            "monthly_limit": monthly_limit,
            "monthly_pct": round(monthly["cost"] / monthly_limit * 100, 1) if monthly_limit > 0 else 0,
            "mode": self.get_degradation_mode().value,
            "daily_input_tokens": daily["input_tokens"],
            "daily_output_tokens": daily["output_tokens"],
        }

    def set_limits(self, daily: float | None = None, monthly: float | None = None) -> None:
        if daily is not None:
            self._store.set_budget_limit("daily_limit_usd", daily)
        if monthly is not None:
            self._store.set_budget_limit("monthly_limit_usd", monthly)
