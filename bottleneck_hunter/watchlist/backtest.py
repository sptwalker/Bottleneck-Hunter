"""回测引擎 — 回放历史模拟交易，生成净值曲线和绩效指标。

使用 sim_trades + market_snapshots 数据回放指定时间段内的交易活动。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bottleneck_hunter.watchlist.performance import (
    PerformanceMetrics,
    compute_metrics,
)
from bottleneck_hunter.watchlist.store import WatchlistStore

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    run_id: str = ""
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 100000.0
    final_equity: float = 0.0
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    trade_count: int = 0
    error: str = ""


class BacktestEngine:
    """基于历史模拟交易数据的回测引擎。"""

    def __init__(self, store: WatchlistStore, initial_capital: float = 100000.0):
        self._store = store
        self._initial_capital = initial_capital

    def run(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        benchmark_ticker: str = "SPY",
    ) -> BacktestResult:
        """回放 start_date 到 end_date 之间的所有模拟交易。

        返回净值曲线和绩效指标。
        """
        result = BacktestResult(
            run_id=uuid.uuid4().hex[:12],
            initial_capital=self._initial_capital,
        )

        try:
            trades = self._store.get_sim_trades(limit=100000)
        except Exception as e:
            result.error = f"获取交易记录失败: {e}"
            return result

        if not trades:
            result.error = "无交易记录"
            return result

        trades = sorted(trades, key=lambda t: t.get("created_at", ""))

        if start_date:
            trades = [t for t in trades if t.get("created_at", "") >= start_date]
        if end_date:
            trades = [t for t in trades if t.get("created_at", "") <= end_date]

        if not trades:
            result.error = "指定时间范围内无交易"
            return result

        result.start_date = start_date or trades[0].get("created_at", "")[:10]
        result.end_date = end_date or trades[-1].get("created_at", "")[:10]
        result.trade_count = len(trades)

        equity_curve = self._build_equity_curve(trades)
        benchmark_curve = self._get_benchmark_curve(
            benchmark_ticker, result.start_date, result.end_date
        )

        result.metrics = compute_metrics(equity_curve, trades, benchmark_curve)
        result.final_equity = equity_curve[-1]["equity"] if equity_curve else self._initial_capital

        self._save_run(result)
        return result

    def _build_equity_curve(self, trades: list[dict]) -> list[dict]:
        """从交易记录构建日级净值曲线。"""
        cash = self._initial_capital
        positions: dict[str, dict] = {}
        curve = []

        date_groups: dict[str, list[dict]] = {}
        for t in trades:
            date = t.get("created_at", "")[:10]
            date_groups.setdefault(date, []).append(t)

        for date in sorted(date_groups.keys()):
            for t in date_groups[date]:
                side = t.get("side", "")
                ticker = t.get("ticker", "")
                shares = t.get("shares", 0)
                price = t.get("price", 0)
                amount = t.get("amount", 0)

                if side == "buy":
                    cash -= amount
                    if ticker in positions:
                        pos = positions[ticker]
                        old_total = pos["shares"] * pos["avg_cost"]
                        pos["shares"] += shares
                        pos["avg_cost"] = (old_total + amount) / pos["shares"] if pos["shares"] else 0
                    else:
                        positions[ticker] = {"shares": shares, "avg_cost": price}

                elif side == "sell":
                    cash += amount
                    if ticker in positions:
                        positions[ticker]["shares"] -= shares
                        if positions[ticker]["shares"] <= 0:
                            del positions[ticker]

            position_value = sum(
                p["shares"] * self._get_price_on_date(ticker, date, p["avg_cost"])
                for ticker, p in positions.items()
            )
            equity = cash + position_value
            curve.append({"date": date, "equity": round(equity, 2)})

        return curve

    def _get_price_on_date(self, ticker: str, date: str, fallback: float) -> float:
        """获取某 ticker 在指定日期的收盘价。"""
        try:
            snapshots = self._store.get_snapshots(ticker, days=500)
            for snap in snapshots:
                if snap.get("date", "")[:10] <= date:
                    return snap.get("close") or fallback
        except Exception:
            pass
        return fallback

    def _get_benchmark_curve(
        self, ticker: str, start_date: str, end_date: str
    ) -> list[dict]:
        """获取基准指数的净值曲线。"""
        try:
            snapshots = self._store.get_snapshots(ticker, days=1000)
            if not snapshots:
                return []
            points = [
                {"date": s["date"][:10], "value": s["close"]}
                for s in reversed(snapshots)
                if s.get("close") and start_date <= s.get("date", "")[:10] <= end_date
            ]
            return points
        except Exception:
            return []

    def _save_run(self, result: BacktestResult) -> None:
        """将回测结果存入数据库。"""
        try:
            self._store.save_backtest_run(
                run_id=result.run_id,
                start_date=result.start_date,
                end_date=result.end_date,
                initial_capital=result.initial_capital,
                final_equity=result.final_equity,
                total_return_pct=result.metrics.total_return_pct,
                sharpe_ratio=result.metrics.sharpe_ratio,
                sortino_ratio=result.metrics.sortino_ratio,
                max_drawdown_pct=result.metrics.max_drawdown_pct,
                calmar_ratio=result.metrics.calmar_ratio,
                win_rate_pct=result.metrics.win_rate_pct,
                trade_count=result.trade_count,
                equity_curve=result.metrics.equity_curve,
            )
        except Exception as e:
            logger.warning("保存回测记录失败: %s", e)
