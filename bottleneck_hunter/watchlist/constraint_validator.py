"""约束校验引擎 — 对 L4 执行计划进行硬性约束验证

在 LLM 生成执行计划后、写入数据库前进行规则校验。
违规计划自动标记为 rejected，记录违反的具体约束。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

DEFAULT_CONSTRAINTS = {
    "max_single_position_pct": 20.0,
    "max_sector_pct": 40.0,
    "min_cash_pct": 15.0,
    "max_single_trade_usd": 10000.0,
    "max_daily_turnover_pct": 30.0,
}


@dataclass
class ValidationResult:
    valid: bool = True
    violations: list[str] = field(default_factory=list)

    def add_violation(self, msg: str):
        self.valid = False
        self.violations.append(msg)


def validate_execution_plan(
    plan: dict,
    account: dict,
    positions: list[dict],
    constraints: dict | None = None,
) -> ValidationResult:
    """校验单个执行计划是否满足所有硬性约束。"""
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    result = ValidationResult()

    result_json = plan.get("result_json", {})
    if isinstance(result_json, str):
        import json
        try:
            result_json = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result_json = {}

    action = plan.get("action") or result_json.get("action", "")
    shares = plan.get("shares") or result_json.get("shares", 0)
    price = (plan.get("target_price")
             or result_json.get("target_price")
             or result_json.get("estimated_price", 0))
    ticker = plan.get("ticker", "")
    sector = plan.get("sector", "") or result_json.get("sector", "")

    if not action or not ticker or not shares or not price:
        return result

    trade_amount = shares * price
    total_equity = account.get("total_equity") or account.get("current_capital", 100000)
    cash = account.get("cash_balance", 0)

    if total_equity <= 0:
        return result

    # 1. 单笔交易金额上限
    max_trade = c["max_single_trade_usd"]
    if trade_amount > max_trade:
        result.add_violation(
            f"单笔金额 ${trade_amount:.0f} 超过上限 ${max_trade:.0f}"
        )

    if action in ("buy", "add"):
        # 2. 单股持仓占比上限
        existing_value = 0
        for p in positions:
            if p.get("ticker") == ticker:
                existing_value = p.get("market_value", 0)
                break
        new_value = existing_value + trade_amount
        position_pct = new_value / total_equity * 100
        max_pos = c["max_single_position_pct"]
        if position_pct > max_pos:
            result.add_violation(
                f"买入后 {ticker} 占比 {position_pct:.1f}% 超过上限 {max_pos:.0f}%"
            )

        # 3. 板块集中度上限
        if sector:
            sector_value = sum(
                p.get("market_value", 0) for p in positions
                if _get_sector(p) == sector and p.get("ticker") != ticker
            )
            sector_value += new_value
            sector_pct = sector_value / total_equity * 100
            max_sec = c["max_sector_pct"]
            if sector_pct > max_sec:
                result.add_violation(
                    f"板块 '{sector}' 占比 {sector_pct:.1f}% 超过上限 {max_sec:.0f}%"
                )

        # 4. 最低现金比例
        cash_after = cash - trade_amount
        cash_pct = cash_after / total_equity * 100
        min_cash = c["min_cash_pct"]
        if cash_pct < min_cash:
            result.add_violation(
                f"买入后现金比例 {cash_pct:.1f}% 低于下限 {min_cash:.0f}%"
            )

    # 5. 日交易额度（简单检查单笔 vs 总额度）
    max_turnover = c["max_daily_turnover_pct"] / 100 * total_equity
    if trade_amount > max_turnover:
        result.add_violation(
            f"单笔 ${trade_amount:.0f} 超过日交易额度 ${max_turnover:.0f}"
        )

    if not result.valid:
        logger.warning("执行计划 %s 违反约束: %s", plan.get("id", "?"), "; ".join(result.violations))

    return result


def validate_batch(
    plans: list[dict],
    account: dict,
    positions: list[dict],
    constraints: dict | None = None,
) -> dict[str, ValidationResult]:
    """批量校验多个执行计划。返回 {plan_id: ValidationResult}。"""
    results = {}
    for plan in plans:
        plan_id = plan.get("id", "")
        results[plan_id] = validate_execution_plan(plan, account, positions, constraints)
    return results


def _get_sector(position: dict) -> str:
    return position.get("sector", "") or ""
