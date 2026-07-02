"""约束校验引擎 — 对 L4 执行计划进行硬性约束验证

在 LLM 生成执行计划后、写入数据库前进行规则校验。
违规计划自动标记为 rejected，记录违反的具体约束。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bottleneck_hunter.watchlist.slippage import SLIPPAGE_CONFIG

logger = logging.getLogger(__name__)

DEFAULT_CONSTRAINTS = {
    "max_single_position_pct": 25.0,
    "max_sector_pct": 40.0,
    "min_cash_pct": 15.0,
    "max_single_trade_usd": 50000.0,
    "max_daily_turnover_pct": 30.0,
    "max_portfolio_beta": 1.1,
}

# 账户级熔断阈值（组合层保护，独立于单股止损）。
# 触发后禁止新开/加仓（只允许减仓/清仓），防止在急跌中继续加码。
CIRCUIT_BREAKER = {
    "max_daily_loss_pct": 8.0,     # 单日权益回撤 ≥8% → 熔断
    "max_drawdown_pct": 20.0,      # 距峰值回撤 ≥20% → 熔断
}


def check_account_circuit_breaker(account: dict,
                                  today_start_equity: float | None = None,
                                  peak_equity: float | None = None,
                                  cfg: dict | None = None) -> ValidationResult:
    """账户级熔断检查：单日巨亏或深度回撤时，阻止新开/加仓。

    返回 ValidationResult：valid=True 表示未熔断；valid=False 表示已熔断，
    violations 说明触发原因。调用方（L4）在熔断时应只放行 sell/reduce 计划。
    """
    c = {**CIRCUIT_BREAKER, **(cfg or {})}
    result = ValidationResult()
    equity = account.get("total_equity") or account.get("current_capital") or 0
    if not equity:
        return result

    # 单日亏损：需要当日开盘权益基准
    if today_start_equity and today_start_equity > 0:
        day_loss_pct = (today_start_equity - equity) / today_start_equity * 100
        if day_loss_pct >= c["max_daily_loss_pct"]:
            result.valid = False
            result.violations.append(
                f"账户熔断：单日回撤 {day_loss_pct:.1f}% ≥ {c['max_daily_loss_pct']}%，暂停新开/加仓")

    # 深度回撤：距历史峰值
    peak = peak_equity or account.get("peak_equity") or account.get("initial_capital") or equity
    if peak and peak > 0:
        dd_pct = (peak - equity) / peak * 100
        if dd_pct >= c["max_drawdown_pct"]:
            result.valid = False
            result.violations.append(
                f"账户熔断：距峰值回撤 {dd_pct:.1f}% ≥ {c['max_drawdown_pct']}%，暂停新开/加仓")

    return result


# P0.6 动态约束：按 L1 宏观风险偏好调整阈值
# aggressive(进攻) / balanced(平衡) / defensive(防守)
REGIME_CONSTRAINTS = {
    "aggressive": {
        "max_single_position_pct": 30.0,
        "max_sector_pct": 50.0,
        "min_cash_pct": 10.0,
        "max_single_trade_usd": 60000.0,
        "max_daily_turnover_pct": 40.0,
        "max_portfolio_beta": 1.3,
    },
    "balanced": {
        "max_single_position_pct": 25.0,
        "max_sector_pct": 40.0,
        "min_cash_pct": 15.0,
        "max_single_trade_usd": 50000.0,
        "max_daily_turnover_pct": 30.0,
        "max_portfolio_beta": 1.1,
    },
    "defensive": {
        "max_single_position_pct": 18.0,
        "max_sector_pct": 30.0,
        "min_cash_pct": 25.0,
        "max_single_trade_usd": 40000.0,
        "max_daily_turnover_pct": 20.0,
        "max_portfolio_beta": 0.9,
    },
}


def get_constraints_for_appetite(risk_appetite: str | None) -> dict:
    """根据 L1 风险偏好返回对应约束集。未知偏好回退到 DEFAULT_CONSTRAINTS(=balanced)。"""
    if not risk_appetite:
        return dict(DEFAULT_CONSTRAINTS)
    key = str(risk_appetite).strip().lower()
    # 兼容中文/别名
    alias = {
        "进攻": "aggressive", "激进": "aggressive", "offensive": "aggressive",
        "平衡": "balanced", "中性": "balanced", "neutral": "balanced",
        "防守": "defensive", "保守": "defensive", "conservative": "defensive",
    }
    key = alias.get(key, key)
    return dict(REGIME_CONSTRAINTS.get(key, DEFAULT_CONSTRAINTS))


@dataclass
class ValidationResult:
    valid: bool = True
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_violation(self, msg: str):
        self.valid = False
        self.violations.append(msg)

    def add_warning(self, msg: str):
        """风险预算警告：不影响 valid 判定，仅作提示(如最坏滑点下的压力测试)。"""
        self.warnings.append(msg)


def validate_execution_plan(
    plan: dict,
    account: dict,
    positions: list[dict],
    constraints: dict | None = None,
) -> ValidationResult:
    """校验单个执行计划是否满足所有硬性约束。

    P0.4 双口径：
    - 校验口径(valid 判定)：用预期价(target_price)，不加最坏滑点
    - 风险预算口径(warning)：用最坏滑点价做压力测试，仅警告不否决
    """
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

    # P0.4 校验口径：预期价(不加滑点)
    trade_amount = shares * price
    # 风险预算口径：最坏滑点价
    market = result_json.get("market", "us_stock")
    cfg = SLIPPAGE_CONFIG.get(market, SLIPPAGE_CONFIG["us_stock"])
    worst_slip_pct = cfg["max_bps"] / 10000
    if action in ("buy", "add"):
        worst_amount = shares * price * (1 + worst_slip_pct)
    else:
        worst_amount = shares * price * (1 - worst_slip_pct)

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
    elif worst_amount > max_trade:
        result.add_warning(
            f"最坏滑点下单笔金额 ${worst_amount:.0f} 可能触及上限 ${max_trade:.0f}"
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
        elif (existing_value + worst_amount) / total_equity * 100 > max_pos:
            result.add_warning(
                f"最坏滑点下 {ticker} 占比可能触及上限 {max_pos:.0f}%"
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
        elif (cash - worst_amount) / total_equity * 100 < min_cash:
            result.add_warning(
                f"最坏滑点下现金比例可能跌破下限 {min_cash:.0f}%"
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


def max_compliant_shares(
    plan: dict,
    account: dict,
    positions: list[dict],
    constraints: dict | None = None,
) -> int:
    """P0.3 反推：在所有约束内，该计划最多可买/卖多少股(整数)。

    仅对 buy/add 有意义。返回 0 表示无法缩量到合规(如现金不足以买 1 手)。
    """
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    result_json = plan.get("result_json", {})
    if isinstance(result_json, str):
        import json
        try:
            result_json = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result_json = {}

    action = plan.get("action") or result_json.get("action", "")
    price = (plan.get("target_price") or result_json.get("target_price")
             or result_json.get("estimated_price", 0))
    ticker = plan.get("ticker", "")
    sector = plan.get("sector", "") or result_json.get("sector", "")
    if action not in ("buy", "add") or not price:
        return 0

    total_equity = account.get("total_equity") or account.get("current_capital", 100000)
    cash = account.get("cash_balance", 0)
    if total_equity <= 0:
        return 0

    existing_value = 0.0
    for p in positions:
        if p.get("ticker") == ticker:
            existing_value = p.get("market_value", 0)
            break

    # 各约束允许的最大金额
    limits = [
        c["max_single_trade_usd"],                                   # 单笔上限
        c["max_single_position_pct"] / 100 * total_equity - existing_value,  # 单股占比
        cash - c["min_cash_pct"] / 100 * total_equity,               # 现金下限
        c["max_daily_turnover_pct"] / 100 * total_equity,            # 日额度
    ]
    # 板块集中度上限：扣除同板块其他持仓后的剩余额度
    if sector:
        other_sector_value = sum(
            p.get("market_value", 0) for p in positions
            if _get_sector(p) == sector and p.get("ticker") != ticker
        )
        limits.append(c["max_sector_pct"] / 100 * total_equity - other_sector_value - existing_value)
    max_amount = min(limits)
    if max_amount <= 0:
        return 0
    return int(max_amount // price)


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


def validate_portfolio_beta(
    plan: dict,
    account: dict,
    positions: list[dict],
    beta_map: dict,
    constraints: dict | None = None,
) -> ValidationResult:
    """P2.1 组合级 beta 约束：买入该计划后组合加权 beta 是否超 regime 上限。

    beta_map: {ticker: beta}。缺失 beta 的标的按 beta=1.0 估算。
    若该计划标的或全组合都缺 beta，则跳过(优雅降级，仅 warning)。
    """
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    result = ValidationResult()
    max_beta = c.get("max_portfolio_beta")
    if not max_beta:
        return result

    result_json = plan.get("result_json", {})
    if isinstance(result_json, str):
        import json
        try:
            result_json = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result_json = {}

    action = plan.get("action") or result_json.get("action", "")
    if action not in ("buy", "add"):
        return result
    shares = plan.get("shares") or result_json.get("shares", 0)
    price = (plan.get("target_price") or result_json.get("target_price")
             or result_json.get("estimated_price", 0))
    ticker = plan.get("ticker", "")
    if not shares or not price or not ticker:
        return result

    if not beta_map:
        result.add_warning("缺少 beta 数据，跳过组合 beta 校验")
        return result

    trade_amount = shares * price
    # 组合现有加权 beta
    total_val = 0.0
    weighted = 0.0
    for p in positions:
        mv = p.get("market_value", 0) or 0
        b = beta_map.get(p.get("ticker"), 1.0)
        total_val += mv
        weighted += mv * b
    # 加入本计划后
    new_b = beta_map.get(ticker, 1.0)
    total_after = total_val + trade_amount
    weighted_after = weighted + trade_amount * new_b
    if total_after <= 0:
        return result
    portfolio_beta = weighted_after / total_after
    if portfolio_beta > max_beta:
        result.add_violation(
            f"买入后组合 beta {portfolio_beta:.2f} 超过 regime 上限 {max_beta:.2f}"
        )
    return result


def validate_against_regime(
    plan: dict,
    account: dict,
    positions: list[dict],
    regime_bounds: dict,
) -> ValidationResult:
    """验证执行计划是否符合当前 regime 的仓位约束。"""
    result = ValidationResult()
    if not regime_bounds:
        return result

    total_equity = account.get("total_equity") or account.get("current_capital", 100000)
    if total_equity <= 0:
        return result

    result_json = plan.get("result_json", {})
    if isinstance(result_json, str):
        import json
        try:
            result_json = json.loads(result_json)
        except (json.JSONDecodeError, TypeError):
            result_json = {}

    action = plan.get("action") or result_json.get("action", "")
    shares = plan.get("shares") or result_json.get("shares", 0)
    price = (plan.get("target_price") or result_json.get("target_price")
             or result_json.get("estimated_price", 0))
    ticker = plan.get("ticker", "")

    if action not in ("buy", "add") or not shares or not price:
        return result

    # 使用 worst-case 滑点价格，与 validate_execution_plan 保持一致
    market = result_json.get("market", "us_stock")
    worst_bps = SLIPPAGE_CONFIG.get(market, SLIPPAGE_CONFIG["us_stock"])["max_bps"]
    worst_price = price * (1 + worst_bps / 10000)
    trade_amount = shares * worst_price
    cash = account.get("cash_balance", 0)
    position_value = sum(p.get("market_value", 0) for p in positions)

    cash_after = cash - trade_amount
    equity_after = position_value + trade_amount
    equity_pct = equity_after / total_equity * 100

    max_eq = regime_bounds.get("equity_max", 100)
    if equity_pct > max_eq:
        result.add_violation(
            f"买入后权益仓位 {equity_pct:.1f}% 超过 regime 上限 {max_eq}%"
        )

    max_single = regime_bounds.get("max_single_pct", 20)
    existing_value = 0
    for p in positions:
        if p.get("ticker") == ticker:
            existing_value = p.get("market_value", 0)
            break
    new_pct = (existing_value + trade_amount) / total_equity * 100
    if new_pct > max_single:
        result.add_violation(
            f"{ticker} 仓位 {new_pct:.1f}% 超过 regime 单股上限 {max_single}%"
        )

    if not result.valid:
        logger.warning("Regime 约束校验失败 %s: %s", plan.get("id", "?"), "; ".join(result.violations))

    return result
