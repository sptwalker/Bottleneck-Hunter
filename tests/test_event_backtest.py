"""P0-5 事件驱动回测核心测试：下一交易日成交、停牌、退市、现金、成本、可复现。"""

import pytest

from bottleneck_hunter.watchlist.event_backtest import (
    Bar,
    Order,
    run_event_backtest,
)

D1, D2, D3 = "2026-01-05", "2026-01-06", "2026-01-07"


def _series(*points):
    """points: (date, close, kwargs)…；返回 Bar 列表。"""
    return [Bar(date=d, close=c, **kw) for d, c, kw in points]


# —— 下一交易日成交：结构性 PIT，决策日当天绝不成交 ——

def test_order_fills_on_next_trading_day_not_decision_day():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    report = run_event_backtest(bars, [Order(D1, "AAA", "buy", 100)], initial_cash=1_000_000.0)
    assert len(report.trades) == 1
    # 决策日 D1 不成交；在 D2 以 D2 的价（含 10bps 滑点）成交
    assert report.trades[0]["date"] == D2
    assert report.trades[0]["price"] == 110.11


def test_buy_then_sell_realized_pnl_and_equity():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    orders = [Order(D1, "AAA", "buy", 100), Order(D2, "AAA", "sell", 100)]
    report = run_event_backtest(bars, orders, initial_cash=1_000_000.0)
    buy, sell = report.trades
    assert (buy["side"], sell["side"]) == ("buy", "sell")
    assert sell["date"] == D3
    # avg_cost=110.11，卖出价 119.88（-10bps），100 股，美股零佣
    assert sell["realized_pnl"] == pytest.approx(977.0)
    assert report.final_equity == pytest.approx(1_000_977.0)


def test_no_lookahead_price_is_from_fill_day():
    """成交价取成交日 bar，不回看更晚的更高价。"""
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 999.0, {}))}
    report = run_event_backtest(bars, [Order(D1, "AAA", "buy", 10)], initial_cash=1_000_000.0)
    assert report.trades[0]["date"] == D2
    assert report.trades[0]["price"] == 110.11  # 不是 D3 的 999


# —— 停牌：顺延到下一个可成交日 ——

def test_halted_day_defers_fill_to_next_open_day():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {"halted": True}), (D3, 120.0, {}))}
    report = run_event_backtest(bars, [Order(D1, "AAA", "buy", 100)], initial_cash=1_000_000.0)
    assert len(report.trades) == 1
    assert report.trades[0]["date"] == D3  # D2 停牌，顺延到 D3
    assert report.trades[0]["price"] == 120.12


# —— 退市：强制清仓，退市后订单被拒 ——

def test_delisting_forces_liquidation_and_rejects_later_orders():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 90.0, {"delisted": True}))}
    orders = [Order(D1, "AAA", "buy", 100), Order(D2, "AAA", "buy", 10)]
    report = run_event_backtest(bars, orders, initial_cash=1_000_000.0)
    liquidation = [t for t in report.trades if t["trade_type"] == "delisting_liquidation"]
    assert len(liquidation) == 1
    assert liquidation[0]["date"] == D3
    assert liquidation[0]["price"] == 90.0  # 退市清仓不加滑点
    assert liquidation[0]["realized_pnl"] == pytest.approx((90.0 - 110.11) * 100)
    # 决策于 D2 的买单在 D3 遇退市 → 拒绝
    assert any(r["reason"] == "ticker_delisted" for r in report.rejected)


# —— 现金约束：不足即拒，现金绝不为负 ——

def test_buy_exceeding_cash_is_rejected():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    report = run_event_backtest(bars, [Order(D1, "AAA", "buy", 100)], initial_cash=1_000.0)
    assert report.trades == []
    assert any(r["reason"] == "insufficient_cash" for r in report.rejected)
    assert report.final_cash == pytest.approx(1_000.0)
    assert report.final_equity == pytest.approx(1_000.0)


def test_sell_without_position_is_rejected():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    report = run_event_backtest(bars, [Order(D1, "AAA", "sell", 100)], initial_cash=1_000_000.0)
    assert report.trades == []
    assert any(r["reason"] == "no_position" for r in report.rejected)


# —— 成本：A股卖方印花税叠加佣金 ——

def test_a_stock_stamp_duty_on_sell():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    orders = [Order(D1, "AAA", "buy", 100), Order(D2, "AAA", "sell", 100)]
    report = run_event_backtest(bars, orders, initial_cash=1_000_000.0, market="a_stock")
    buy, sell = report.trades
    # 买方仅佣金万 2.5；卖方佣金万 2.5 + 印花税万 5 = 万 7.5（佣金按 4 位小数入账）
    assert buy["commission"] == pytest.approx(buy["amount"] * 2.5 / 10000, abs=1e-4)
    assert sell["commission"] == pytest.approx(sell["amount"] * 7.5 / 10000, abs=1e-4)


# —— 可复现：相同输入必得相同结果 ——

def test_deterministic_reproducibility():
    bars = {
        "AAA": _series((D1, 100.0, {"volume": 1e6}), (D2, 110.0, {"volume": 1e6}), (D3, 120.0, {"volume": 1e6})),
        "BBB": _series((D1, 50.0, {"volume": 2e6}), (D2, 55.0, {"volume": 2e6}), (D3, 60.0, {"volume": 2e6})),
    }
    orders = [
        Order(D1, "BBB", "buy", 200),
        Order(D1, "AAA", "buy", 100),
        Order(D2, "AAA", "sell", 50),
    ]
    r1 = run_event_backtest(bars, orders, initial_cash=1_000_000.0)
    r2 = run_event_backtest(bars, orders, initial_cash=1_000_000.0)
    assert r1.equity_curve == r2.equity_curve
    assert r1.trades == r2.trades
    assert r1.final_equity == r2.final_equity


# —— 指标接入 + 收盘未成交 ——

def test_metrics_and_equity_curve_length():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    orders = [Order(D1, "AAA", "buy", 100), Order(D2, "AAA", "sell", 100)]
    report = run_event_backtest(bars, orders, initial_cash=1_000_000.0)
    assert len(report.equity_curve) == 3  # 每个交易日一个点
    expected = round((report.final_equity / 1_000_000.0 - 1) * 100, 4)
    assert report.metrics.total_return_pct == pytest.approx(expected)


def test_order_on_last_day_never_fills():
    bars = {"AAA": _series((D1, 100.0, {}), (D2, 110.0, {}), (D3, 120.0, {}))}
    report = run_event_backtest(bars, [Order(D3, "AAA", "buy", 10)], initial_cash=1_000_000.0)
    assert report.trades == []
    assert any(r["reason"] == "unfilled_at_end" for r in report.rejected)
