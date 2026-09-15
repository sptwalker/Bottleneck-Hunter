"""事件驱动回测核心 — 按交易日历逐日推进，信号在下一交易日成交，杜绝未来数据。

与 `backtest.py` 的"模拟盘复盘"明确分层：
- `backtest.py` 回放系统自身已发生的 `sim_trades`（纸面复盘，无样本外、无参数敏感性）。
- 本模块是"信号 → 订单 → 成交"的事件驱动引擎：给定历史 bar 与决策日订单，
  确定性地在下一个可交易日按当日参考价（含滑点 + 佣金）成交，处理停牌、退市、
  现金约束与交易成本；相同输入必得相同结果（无随机、迭代有序）。

不做未来数据假设（结构性 PIT）：第 T 日信息产生的订单只能在 T 之后的首个
非停牌、非退市交易日成交，成交价取该日 bar，绝不回看更晚的价格。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from bottleneck_hunter.watchlist.performance import PerformanceMetrics, compute_metrics
from bottleneck_hunter.watchlist.slippage import calc_slippage

# 佣金 + 印花税，按市场配置；这是校准旋钮而非硬编码事实，接实盘费率时在此调。
# a_stock：佣金双边约万 2.5，印花税卖方千 0.5（2023-08 起）；us_stock：近似零佣，留旋钮。
COST_CONFIG: dict[str, dict[str, float]] = {
    "us_stock": {"commission_bps": 0.0, "stamp_sell_bps": 0.0},
    "a_stock": {"commission_bps": 2.5, "stamp_sell_bps": 5.0},
}


@dataclass(frozen=True)
class Bar:
    """某 ticker 某交易日的行情。交易日历由所有 bar 的日期并集定义。"""

    date: str  # 'YYYY-MM-DD'
    close: float
    volume: float | None = None  # 用于滑点冲击估计
    halted: bool = False  # 停牌：当日不可成交，持仓按上一有效价持有
    delisted: bool = False  # 退市日：强制以当日 close 清仓，之后不可持有


@dataclass(frozen=True)
class Order:
    """一笔目标订单。decision_date 是产生该订单所依据信息的截止日（决策日）。"""

    decision_date: str
    ticker: str
    side: str  # 'buy' | 'sell'
    shares: int  # 正整数；sell 超过持仓时按当前持仓封顶


@dataclass
class BacktestReport:
    equity_curve: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)  # 与 compute_metrics 兼容的成交字典
    metrics: PerformanceMetrics = field(default_factory=PerformanceMetrics)
    rejected: list[dict] = field(default_factory=list)  # 现金不足/无价/退市后下单/未能成交
    final_cash: float = 0.0
    final_equity: float = 0.0


def _commission(amount: float, side: str, market: str, cost_config: dict[str, float]) -> float:
    """成交金额对应的佣金 + 卖方印花税。"""
    bps = cost_config["commission_bps"]
    if side == "sell":
        bps += cost_config["stamp_sell_bps"]
    return round(abs(amount) * bps / 10000.0, 4)


def run_event_backtest(
    bars: Mapping[str, Sequence[Bar]],
    orders: Sequence[Order],
    *,
    initial_cash: float = 1_000_000.0,
    market: str = "us_stock",
    benchmark: Sequence[dict] | None = None,
) -> BacktestReport:
    """事件驱动回放：按交易日历逐日推进，决策日订单在下一可交易日成交。

    bars: {ticker: [Bar, ...]}，每 ticker 的 bar 按日期给出（内部会重排）。
    orders: 决策日订单序列；同一决策日多单按 (ticker, side) 稳定排序后处理。
    返回 BacktestReport（净值曲线 + 成交 + 绩效指标 + 被拒订单）。
    """
    cost_config = COST_CONFIG.get(market, COST_CONFIG["us_stock"])
    # 建索引：by_date[ticker][date] = Bar；交易日历 = 所有日期并集（有序）。
    by_date: dict[str, dict[str, Bar]] = {}
    calendar: set[str] = set()
    for ticker, series in bars.items():
        by_date[ticker] = {b.date: b for b in series}
        calendar.update(b.date for b in series)
    days = sorted(calendar)

    report = BacktestReport(final_cash=initial_cash)
    cash = initial_cash
    positions: dict[str, dict] = {}  # ticker -> {"shares": int, "avg_cost": float}
    last_close: dict[str, float] = {}  # 停牌日按最后有效价 mark
    delisted_tickers: set[str] = set()

    # 待成交订单队列：decision_date 严格早于当前日才可成交（下一交易日规则）。
    pending = sorted(orders, key=lambda o: (o.decision_date, o.ticker, o.side))

    for day in days:
        # 1) 退市优先：当日退市的 ticker 强制清仓，之后不可再持有/下单。
        for ticker in sorted(positions):
            bar = by_date.get(ticker, {}).get(day)
            if bar is not None and bar.delisted:
                pos = positions.pop(ticker)
                price = bar.close
                amount = round(pos["shares"] * price, 4)
                commission = _commission(amount, "sell", market, cost_config)
                cash += amount - commission
                report.trades.append({
                    "date": day, "ticker": ticker, "side": "sell", "shares": pos["shares"],
                    "price": price, "amount": amount, "commission": commission,
                    "slippage_bps": 0.0, "trade_type": "delisting_liquidation",
                    "realized_pnl": round((price - pos["avg_cost"]) * pos["shares"] - commission, 4),
                })
        for ticker, bar in ((t, by_date[t].get(day)) for t in by_date):
            if bar is not None and bar.delisted:
                delisted_tickers.add(ticker)

        # 2) 成交本日可成交的待处理订单（decision_date < day）。
        still_pending: list[Order] = []
        for order in pending:
            if order.decision_date >= day:
                still_pending.append(order)
                continue
            bar = by_date.get(order.ticker, {}).get(day)
            if order.ticker in delisted_tickers:
                report.rejected.append({"order": order, "reason": "ticker_delisted"})
                continue
            if bar is None or bar.halted or bar.delisted:
                still_pending.append(order)  # 无 bar 或停牌：顺延到下一交易日
                continue
            if order.side not in ("buy", "sell") or order.shares <= 0:
                report.rejected.append({"order": order, "reason": "invalid_order"})
                continue
            price, slip_bps = calc_slippage(bar.close, order.shares, order.side, market, bar.volume)
            if order.side == "buy":
                amount = round(order.shares * price, 4)
                commission = _commission(amount, "buy", market, cost_config)
                if amount + commission > cash:
                    report.rejected.append({"order": order, "reason": "insufficient_cash"})
                    continue
                cash -= amount + commission
                pos = positions.setdefault(order.ticker, {"shares": 0, "avg_cost": 0.0})
                old_total = pos["shares"] * pos["avg_cost"]
                pos["shares"] += order.shares
                pos["avg_cost"] = round((old_total + amount) / pos["shares"], 6) if pos["shares"] else 0.0
                report.trades.append({
                    "date": day, "ticker": order.ticker, "side": "buy", "shares": order.shares,
                    "price": price, "amount": amount, "commission": commission,
                    "slippage_bps": slip_bps, "trade_type": "entry", "realized_pnl": None,
                })
            else:  # sell：按当前持仓封顶，无持仓则拒
                pos = positions.get(order.ticker)
                if not pos or pos["shares"] <= 0:
                    report.rejected.append({"order": order, "reason": "no_position"})
                    continue
                shares = min(order.shares, pos["shares"])
                amount = round(shares * price, 4)
                commission = _commission(amount, "sell", market, cost_config)
                cash += amount - commission
                realized = round((price - pos["avg_cost"]) * shares - commission, 4)
                pos["shares"] -= shares
                if pos["shares"] <= 0:
                    positions.pop(order.ticker, None)
                report.trades.append({
                    "date": day, "ticker": order.ticker, "side": "sell", "shares": shares,
                    "price": price, "amount": amount, "commission": commission,
                    "slippage_bps": slip_bps, "trade_type": "exit", "realized_pnl": realized,
                })
        pending = still_pending

        # 3) 盯市：停牌用最后有效价，其余用当日 close。
        for ticker, series in by_date.items():
            bar = series.get(day)
            if bar is not None and not bar.halted:
                last_close[ticker] = bar.close
        position_value = sum(p["shares"] * last_close.get(t, p["avg_cost"]) for t, p in positions.items())
        report.equity_curve.append({"date": day, "equity": round(cash + position_value, 2)})

    # 收盘仍未成交的订单（例如决策日在最后一天、或标的长期停牌）记为未成交。
    for order in pending:
        report.rejected.append({"order": order, "reason": "unfilled_at_end"})

    report.final_cash = round(cash, 2)
    report.final_equity = report.equity_curve[-1]["equity"] if report.equity_curve else round(cash, 2)
    report.metrics = compute_metrics(report.equity_curve, report.trades, list(benchmark or []))
    return report
