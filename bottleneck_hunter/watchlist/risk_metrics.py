"""组合风控度量模块

计算 VaR / CVaR / Beta / HHI 集中度 / 相关性矩阵等组合级风险指标。
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252


@dataclass
class PortfolioRiskMetrics:
    portfolio_beta: float = 0.0
    var_95: float = 0.0
    cvar_95: float = 0.0
    concentration_index: float = 0.0
    max_sector_weight: float = 0.0
    max_single_weight: float = 0.0
    correlation_pairs: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def compute_portfolio_risk(
    positions: list[dict],
    price_histories: dict[str, list[float]],
    benchmark_returns: list[float] | None = None,
    total_equity: float = 100000.0,
) -> PortfolioRiskMetrics:
    """从持仓和历史价格计算组合风险指标。

    positions: [{"ticker": "AAPL", "market_value": 20000, "weight_pct": 20.0, "sector": "Technology"}, ...]
    price_histories: {"AAPL": [150.0, 151.2, ...], ...}  # 按日期正序的收盘价列表
    benchmark_returns: [0.01, -0.005, ...]  # 基准日收益率
    """
    m = PortfolioRiskMetrics()
    if not positions:
        return m

    weights = []
    for p in positions:
        w = p.get("weight_pct", 0) / 100
        if w <= 0 and total_equity > 0:
            w = p.get("market_value", 0) / total_equity
        weights.append(w)

    # HHI 集中度
    m.concentration_index = round(sum(w ** 2 for w in weights), 4)
    if m.concentration_index > 0.25:
        m.warnings.append(f"HHI={m.concentration_index:.3f} 过度集中（>0.25）")

    # 单一持仓/板块权重
    m.max_single_weight = round(max(weights) * 100, 2) if weights else 0
    sector_weights: dict[str, float] = {}
    for p, w in zip(positions, weights):
        sec = p.get("sector", "未知")
        sector_weights[sec] = sector_weights.get(sec, 0) + w
    m.max_sector_weight = round(max(sector_weights.values()) * 100, 2) if sector_weights else 0
    if m.max_sector_weight > 40:
        m.warnings.append(f"最大板块占比 {m.max_sector_weight:.1f}% 超过 40%")

    # 计算各持仓日收益率
    stock_returns: dict[str, list[float]] = {}
    for p in positions:
        ticker = p.get("ticker", "")
        prices = price_histories.get(ticker, [])
        if len(prices) >= 2:
            returns = [
                prices[i] / prices[i - 1] - 1
                for i in range(1, len(prices))
                if prices[i - 1] > 0
            ]
            stock_returns[ticker] = returns

    # 组合日收益率（加权）
    if stock_returns:
        min_len = min(len(r) for r in stock_returns.values())
        if min_len > 0:
            portfolio_returns = []
            for i in range(min_len):
                day_return = 0.0
                for p, w in zip(positions, weights):
                    ticker = p.get("ticker", "")
                    if ticker in stock_returns and i < len(stock_returns[ticker]):
                        day_return += w * stock_returns[ticker][i]
                portfolio_returns.append(day_return)

            # VaR（历史模拟法，95% 分位数）
            if portfolio_returns:
                sorted_returns = sorted(portfolio_returns)
                idx_95 = max(0, int(len(sorted_returns) * 0.05))
                m.var_95 = round(abs(sorted_returns[idx_95]) * total_equity, 2)

                # CVaR（条件 VaR）
                tail = sorted_returns[:idx_95 + 1]
                if tail:
                    m.cvar_95 = round(
                        abs(sum(tail) / len(tail)) * total_equity, 2
                    )

            # Beta
            if benchmark_returns and len(benchmark_returns) >= min_len:
                bm = benchmark_returns[:min_len]
                pr = portfolio_returns[:min_len]
                mean_bm = sum(bm) / len(bm)
                mean_pr = sum(pr) / len(pr)
                cov = sum((pr[i] - mean_pr) * (bm[i] - mean_bm) for i in range(len(pr))) / len(pr)
                var_bm = sum((b - mean_bm) ** 2 for b in bm) / len(bm)
                if var_bm > 0:
                    m.portfolio_beta = round(cov / var_bm, 4)

    # 相关性矩阵（标记 ρ > 0.7 的高相关对）
    tickers = [p.get("ticker", "") for p in positions]
    for i in range(len(tickers)):
        for j in range(i + 1, len(tickers)):
            t1, t2 = tickers[i], tickers[j]
            r1 = stock_returns.get(t1, [])
            r2 = stock_returns.get(t2, [])
            if r1 and r2:
                n = min(len(r1), len(r2))
                if n >= 20:
                    corr = _pearson(r1[:n], r2[:n])
                    if abs(corr) > 0.7:
                        m.correlation_pairs.append({
                            "ticker_a": t1, "ticker_b": t2,
                            "correlation": round(corr, 4)
                        })
                        m.warnings.append(f"{t1} 与 {t2} 高相关 ρ={corr:.3f}")

    return m


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n == 0:
        return 0.0
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n)) / n
    std_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x) / n)
    std_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y) / n)
    if std_x == 0 or std_y == 0:
        return 0.0
    return cov / (std_x * std_y)
