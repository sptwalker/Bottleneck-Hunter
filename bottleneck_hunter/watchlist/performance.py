"""绩效指标计算模块

计算 Sharpe / Sortino / 最大回撤 / Calmar / 胜率 / 盈亏比等核心量化指标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


RISK_FREE_RATE = 0.04
TRADING_DAYS_PER_YEAR = 252


@dataclass
class PerformanceMetrics:
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    win_rate_pct: float = 0.0
    profit_loss_ratio: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    benchmark_return_pct: float = 0.0
    alpha_pct: float = 0.0
    max_drawdown_start: str = ""
    max_drawdown_end: str = ""
    equity_curve: list[dict] = field(default_factory=list)


def compute_metrics(
    equity_curve: list[dict],
    trades: list[dict] | None = None,
    benchmark_curve: list[dict] | None = None,
    risk_free_rate: float = RISK_FREE_RATE,
) -> PerformanceMetrics:
    """从净值曲线和交易记录计算全部绩效指标。

    equity_curve: [{"date": "2025-01-01", "equity": 100000.0}, ...]
    trades: [{"side": "sell", "realized_pnl": 123.4, "price": ..., "avg_cost": ...}, ...]
    benchmark_curve: [{"date": "...", "value": 4500.0}, ...]
    """
    m = PerformanceMetrics()
    if len(equity_curve) < 2:
        return m

    m.equity_curve = equity_curve
    equities = [p["equity"] for p in equity_curve]
    initial = equities[0]
    final = equities[-1]

    m.total_return_pct = round((final / initial - 1) * 100, 4) if initial else 0.0

    n_days = len(equities) - 1
    if n_days > 0 and initial > 0:
        total_return = final / initial
        years = n_days / TRADING_DAYS_PER_YEAR
        if years > 0:
            m.annualized_return_pct = round((total_return ** (1 / years) - 1) * 100, 4)

    daily_returns = []
    for i in range(1, len(equities)):
        if equities[i - 1] > 0:
            daily_returns.append(equities[i] / equities[i - 1] - 1)

    if daily_returns:
        daily_rf = risk_free_rate / TRADING_DAYS_PER_YEAR
        excess_returns = [r - daily_rf for r in daily_returns]

        mean_excess = sum(excess_returns) / len(excess_returns)
        variance = sum((r - mean_excess) ** 2 for r in excess_returns) / len(excess_returns)
        std = math.sqrt(variance) if variance > 0 else 0

        if std > 0:
            m.sharpe_ratio = round(mean_excess / std * math.sqrt(TRADING_DAYS_PER_YEAR), 4)

        downside_returns = [r for r in excess_returns if r < 0]
        if downside_returns:
            downside_var = sum(r ** 2 for r in downside_returns) / len(excess_returns)
            downside_std = math.sqrt(downside_var)
            if downside_std > 0:
                m.sortino_ratio = round(
                    mean_excess / downside_std * math.sqrt(TRADING_DAYS_PER_YEAR), 4
                )

    peak = equities[0]
    max_dd = 0.0
    dd_start_idx = 0
    dd_end_idx = 0
    current_start = 0
    for i, eq in enumerate(equities):
        if eq > peak:
            peak = eq
            current_start = i
        dd = (peak - eq) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            dd_start_idx = current_start
            dd_end_idx = i

    m.max_drawdown_pct = round(max_dd * 100, 4)
    if equity_curve:
        m.max_drawdown_start = equity_curve[dd_start_idx]["date"]
        m.max_drawdown_end = equity_curve[dd_end_idx]["date"]

    if m.max_drawdown_pct > 0:
        m.calmar_ratio = round(m.annualized_return_pct / m.max_drawdown_pct, 4)

    if trades:
        sell_trades = [t for t in trades if t.get("side") == "sell"]
        m.total_trades = len(sell_trades)
        winners = [t for t in sell_trades if (t.get("realized_pnl") or 0) > 0]
        losers = [t for t in sell_trades if (t.get("realized_pnl") or 0) < 0]
        m.winning_trades = len(winners)
        m.losing_trades = len(losers)

        if m.total_trades > 0:
            m.win_rate_pct = round(m.winning_trades / m.total_trades * 100, 2)

        if winners:
            m.avg_win_pct = round(
                sum(t.get("realized_pnl", 0) for t in winners) / len(winners), 2
            )
        if losers:
            m.avg_loss_pct = round(
                abs(sum(t.get("realized_pnl", 0) for t in losers) / len(losers)), 2
            )
        if m.avg_loss_pct > 0:
            m.profit_loss_ratio = round(m.avg_win_pct / m.avg_loss_pct, 4)

    if benchmark_curve and len(benchmark_curve) >= 2:
        bm_initial = benchmark_curve[0]["value"]
        bm_final = benchmark_curve[-1]["value"]
        if bm_initial > 0:
            m.benchmark_return_pct = round((bm_final / bm_initial - 1) * 100, 4)
            m.alpha_pct = round(m.total_return_pct - m.benchmark_return_pct, 4)

    return m
