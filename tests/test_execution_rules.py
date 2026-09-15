"""P2-1 多市场交易规则与订单状态机专项测试。"""

from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.execution_rules import (
    Fill,
    MarketRules,
    Order,
    OrderStatus,
    can_fill,
    fill_price,
    limit_band,
    match_against_bar,
    next_sellable_index,
    round_to_lot,
    round_to_tick,
    rules_for,
    simulate_execution,
)

# ---------------------------------------------------------------------------
# 市场规则表
# ---------------------------------------------------------------------------


def test_a_stock_rules():
    r = rules_for("a_stock")
    assert r.price_limit_pct == 10.0
    assert r.lot_size == 100
    assert r.t_plus == 1
    assert r.allow_short is False


def test_a_stock_star_board_is_20pct():
    for board in ("科创板", "创业板", "star", "STAR_MARKET", "chinext"):
        assert rules_for("a_stock", board=board).price_limit_pct == 20.0


def test_a_stock_main_board_when_board_none_or_unknown():
    assert rules_for("a_stock").price_limit_pct == 10.0
    assert rules_for("a_stock", board="主板").price_limit_pct == 10.0


def test_us_stock_rules():
    r = rules_for("us_stock")
    assert r.price_limit_pct == 0.0
    assert r.lot_size == 1
    assert r.t_plus == 0
    assert r.allow_short is True


def test_hk_stock_rules():
    r = rules_for("hk_stock")
    assert r.lot_size == 100
    assert r.price_limit_pct == 0.0
    assert r.t_plus == 0


def test_unknown_market_falls_back_to_us():
    assert rules_for("crypto") == rules_for("us_stock")
    assert rules_for("") == rules_for("us_stock")


# ---------------------------------------------------------------------------
# tick / lot 取整
# ---------------------------------------------------------------------------


def test_round_to_tick_nearest():
    a = rules_for("a_stock")
    assert round_to_tick(10.037, a) == 10.04
    assert round_to_tick(10.032, a) == 10.03
    assert round_to_tick(10.0, a) == 10.0


def test_round_to_tick_non_positive_is_zero():
    a = rules_for("a_stock")
    assert round_to_tick(0, a) == 0.0
    assert round_to_tick(-5, a) == 0.0


def test_round_to_lot_a_stock_floors_to_100():
    a = rules_for("a_stock")
    assert round_to_lot(250, a) == 200
    assert round_to_lot(199, a) == 100
    assert round_to_lot(99, a) == 0  # 不足一手
    assert round_to_lot(0, a) == 0
    assert round_to_lot(-100, a) == 0


def test_round_to_lot_us_is_single_share():
    us = rules_for("us_stock")
    assert round_to_lot(250, us) == 250
    assert round_to_lot(1, us) == 1
    assert round_to_lot(250.9, us) == 250


# ---------------------------------------------------------------------------
# 涨跌停带
# ---------------------------------------------------------------------------


def test_limit_band_a_stock():
    a = rules_for("a_stock")
    assert limit_band(100.0, a) == (90.0, 110.0)


def test_limit_band_star_board_20pct():
    star = rules_for("a_stock", board="科创板")
    assert limit_band(100.0, star) == (80.0, 120.0)


def test_limit_band_none_without_prev_close():
    a = rules_for("a_stock")
    assert limit_band(None, a) == (None, None)
    assert limit_band(0, a) == (None, None)


def test_limit_band_none_for_no_limit_market():
    us = rules_for("us_stock")
    assert limit_band(100.0, us) == (None, None)


# ---------------------------------------------------------------------------
# 封板可成交性
# ---------------------------------------------------------------------------


def test_cannot_buy_at_limit_up():
    a = rules_for("a_stock")
    assert can_fill("buy", 110.0, 100.0, a) is False  # 封涨停买不到
    assert can_fill("add", 110.0, 100.0, a) is False


def test_cannot_sell_at_limit_down():
    a = rules_for("a_stock")
    assert can_fill("sell", 90.0, 100.0, a) is False  # 封跌停卖不掉
    assert can_fill("reduce", 90.0, 100.0, a) is False


def test_can_trade_within_band():
    a = rules_for("a_stock")
    assert can_fill("buy", 105.0, 100.0, a) is True
    assert can_fill("sell", 95.0, 100.0, a) is True


def test_can_sell_at_limit_up_and_buy_at_limit_down():
    # 涨停可卖出，跌停可买入（只是反方向封单不可成交）
    a = rules_for("a_stock")
    assert can_fill("sell", 110.0, 100.0, a) is True
    assert can_fill("buy", 90.0, 100.0, a) is True


def test_no_limit_market_always_fillable():
    us = rules_for("us_stock")
    assert can_fill("buy", 999.0, 100.0, us) is True
    assert can_fill("sell", 0.01, 100.0, us) is True


def test_unknown_side_not_fillable():
    a = rules_for("a_stock")
    assert can_fill("hold", 100.0, 100.0, a) is False


# ---------------------------------------------------------------------------
# 成交价���价差 + 涨跌停夹取 + tick
# ---------------------------------------------------------------------------


def test_fill_price_buy_adds_spread():
    a = rules_for("a_stock")
    assert fill_price(100.0, "buy", None, a) > 100.0


def test_fill_price_sell_subtracts_spread():
    a = rules_for("a_stock")
    assert fill_price(100.0, "sell", None, a) < 100.0


def test_fill_price_clamped_to_limit_up():
    a = rules_for("a_stock")
    # 参考价贴近涨停，买单价差本会顶破 → 夹到上限 110
    assert fill_price(109.99, "buy", 100.0, a) == 110.0


def test_fill_price_clamped_to_limit_down():
    a = rules_for("a_stock")
    assert fill_price(90.01, "sell", 100.0, a) == 90.0


def test_fill_price_rounded_to_tick():
    a = rules_for("a_stock")
    px = fill_price(33.333, "buy", None, a)
    assert round(px / a.tick_size) * a.tick_size == pytest.approx(px, abs=1e-6)


# ---------------------------------------------------------------------------
# T+1 / T+0
# ---------------------------------------------------------------------------


def test_next_sellable_index_t_plus_1():
    a = rules_for("a_stock")
    assert next_sellable_index(0, a) == 1
    assert next_sellable_index(5, a) == 6


def test_next_sellable_index_t_plus_0():
    us = rules_for("us_stock")
    assert next_sellable_index(0, us) == 0
    assert next_sellable_index(5, us) == 5


# ---------------------------------------------------------------------------
# 订单构造校验
# ---------------------------------------------------------------------------


def test_order_rejects_non_positive_shares():
    with pytest.raises(ValueError, match="total_shares"):
        Order("o", "T", "buy", 0)
    with pytest.raises(ValueError, match="total_shares"):
        Order("o", "T", "buy", -100)


def test_order_rejects_unknown_side():
    with pytest.raises(ValueError, match="未知交易方向"):
        Order("o", "T", "hold", 100)


def test_fresh_order_state():
    o = Order("o", "T", "buy", 1000)
    assert o.status == OrderStatus.NEW
    assert o.remaining == 1000
    assert o.filled_shares == 0
    assert o.avg_fill_price == 0.0
    assert o.is_open and not o.is_terminal


# ---------------------------------------------------------------------------
# 状态机：部分成交 → 全部成交
# ---------------------------------------------------------------------------


def test_partial_then_full_fill():
    o = Order("o", "600000.SS", "buy", 1000, market="a_stock")
    f1 = o.record_fill(300, 10.0)
    assert isinstance(f1, Fill) and f1.seq == 1
    assert o.status == OrderStatus.PARTIALLY_FILLED
    assert o.remaining == 700
    f2 = o.record_fill(700, 10.1)
    assert f2.seq == 2
    assert o.status == OrderStatus.FILLED
    assert o.remaining == 0
    assert o.is_terminal


def test_avg_fill_price_is_volume_weighted():
    o = Order("o", "T", "buy", 1000, market="a_stock")
    o.record_fill(300, 10.0)
    o.record_fill(700, 10.1)
    assert o.avg_fill_price == pytest.approx((300 * 10.0 + 700 * 10.1) / 1000)


def test_fill_audit_reconstructs_total():
    o = Order("o", "T", "buy", 1000, market="a_stock")
    o.record_fill(300, 10.0)
    o.record_fill(200, 10.0)
    o.record_fill(500, 10.0)
    assert sum(f.shares for f in o.fills) == o.filled_shares == 1000
    # 每笔审计记录都带成交后状态
    assert o.fills[0].status_after == "PARTIALLY_FILLED"
    assert o.fills[-1].status_after == "FILLED"


def test_fill_records_commission():
    o = Order("o", "T", "sell", 1000, market="a_stock")
    f = o.record_fill(1000, 10.0, commission=25.0)
    assert f.commission == 25.0
    assert f.gross == 10000.0


# ---------------------------------------------------------------------------
# 状态机守卫：单调性、超额、终态不可逆
# ---------------------------------------------------------------------------


def test_cannot_overfill():
    o = Order("o", "T", "buy", 100, market="us_stock")
    with pytest.raises(ValueError, match="超过未成交余额"):
        o.record_fill(101, 1.0)


def test_cannot_fill_non_positive():
    o = Order("o", "T", "buy", 100, market="us_stock")
    with pytest.raises(ValueError, match="成交数量必须为正"):
        o.record_fill(0, 1.0)


def test_terminal_order_cannot_fill():
    o = Order("o", "T", "buy", 100, market="us_stock")
    o.record_fill(100, 1.0)
    assert o.status == OrderStatus.FILLED
    with pytest.raises(ValueError, match="终态订单不可再成交"):
        o.record_fill(1, 1.0)


def test_filled_order_cannot_cancel():
    o = Order("o", "T", "buy", 100, market="us_stock")
    o.record_fill(100, 1.0)
    with pytest.raises(ValueError, match="仅未完结订单可撤单"):
        o.cancel()


def test_cancel_partially_filled_keeps_fills():
    o = Order("o", "T", "buy", 1000, market="a_stock")
    o.record_fill(300, 10.0)
    o.cancel("流动性不足")
    assert o.status == OrderStatus.CANCELLED
    assert o.filled_shares == 300  # 已成交部分保留
    assert o.reason == "流动性不足"
    assert o.is_terminal


def test_cancelled_order_cannot_fill_or_recancel():
    o = Order("o", "T", "buy", 1000, market="a_stock")
    o.cancel()
    with pytest.raises(ValueError):
        o.record_fill(100, 10.0)
    with pytest.raises(ValueError):
        o.cancel()


def test_reject_only_fresh_order():
    o = Order("o", "T", "buy", 100, market="us_stock")
    o.reject("风控拦截")
    assert o.status == OrderStatus.REJECTED
    assert o.reason == "风控拦截"


def test_cannot_reject_after_partial_fill():
    o = Order("o", "T", "buy", 1000, market="a_stock")
    o.record_fill(300, 10.0)
    with pytest.raises(ValueError, match="已部分成交请撤单"):
        o.reject()


def test_rejected_order_is_terminal():
    o = Order("o", "T", "buy", 100, market="us_stock")
    o.reject()
    assert o.is_terminal
    with pytest.raises(ValueError):
        o.record_fill(1, 1.0)


# ---------------------------------------------------------------------------
# 单根 bar 撮合
# ---------------------------------------------------------------------------


def test_match_full_fill_with_ample_liquidity():
    us = rules_for("us_stock")
    o = Order("o", "AAPL", "buy", 500, market="us_stock")
    f = match_against_bar(o, ref_price=100.0, prev_close=None, bar_volume=100000, rules=us)
    assert f is not None and f.shares == 500
    assert o.status == OrderStatus.FILLED


def test_match_partial_fill_bounded_by_participation():
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "buy", 10000, market="a_stock")
    # volume 3000 × participation 0.1 = 300 手上限
    f = match_against_bar(o, ref_price=10.0, prev_close=None, bar_volume=3000,
                          rules=a, participation_rate=0.1)
    assert f is not None and f.shares == 300
    assert o.status == OrderStatus.PARTIALLY_FILLED


def test_match_blocked_by_limit_up_returns_none():
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "buy", 100, market="a_stock")
    f = match_against_bar(o, ref_price=110.0, prev_close=100.0, bar_volume=999999, rules=a)
    assert f is None
    assert o.status == OrderStatus.NEW  # 未成交，留待下轮


def test_match_no_price_returns_none():
    us = rules_for("us_stock")
    o = Order("o", "T", "buy", 100, market="us_stock")
    assert match_against_bar(o, ref_price=None, prev_close=None, bar_volume=1000, rules=us) is None
    assert match_against_bar(o, ref_price=0, prev_close=None, bar_volume=1000, rules=us) is None


def test_match_zero_volume_no_fill_but_none_volume_fills():
    # bar_volume<=0 当日零成交/停牌不可成交；bar_volume=None 无量数据不设限可成交
    us = rules_for("us_stock")
    o1 = Order("o1", "T", "buy", 100, market="us_stock")
    assert match_against_bar(o1, ref_price=100.0, prev_close=None, bar_volume=0, rules=us) is None
    assert o1.status == OrderStatus.NEW
    o2 = Order("o2", "T", "buy", 100, market="us_stock")
    f = match_against_bar(o2, ref_price=100.0, prev_close=None, bar_volume=None, rules=us)
    assert f is not None and f.shares == 100


def test_match_cash_cap_limits_buy():
    us = rules_for("us_stock")
    o = Order("o", "T", "buy", 1000, market="us_stock")
    # 现金只够约 50 股（价约 100）
    f = match_against_bar(o, ref_price=100.0, prev_close=None, bar_volume=100000,
                          rules=us, cash=5000.0)
    assert f is not None and f.shares <= 50


def test_match_cash_too_low_returns_none():
    a = rules_for("a_stock")
    o = Order("o", "T", "buy", 100, market="a_stock")
    # 现金买不起一手（100 股 × 10 = 1000，现金 500）
    f = match_against_bar(o, ref_price=10.0, prev_close=None, bar_volume=100000,
                          rules=a, cash=500.0)
    assert f is None


def test_match_stamp_duty_only_on_sell():
    a = rules_for("a_stock")
    buy = Order("b", "T", "buy", 100, market="a_stock")
    fb = match_against_bar(buy, ref_price=10.0, prev_close=None, bar_volume=100000,
                           rules=a, commission_bps=2.5, stamp_sell_bps=5.0)
    sell = Order("s", "T", "sell", 100, market="a_stock")
    fs = match_against_bar(sell, ref_price=10.0, prev_close=None, bar_volume=100000,
                           rules=a, commission_bps=2.5, stamp_sell_bps=5.0)
    # 卖方多一道印花税 → 佣金更高
    assert fs.commission > fb.commission


def test_match_terminal_order_returns_none():
    us = rules_for("us_stock")
    o = Order("o", "T", "buy", 100, market="us_stock")
    o.cancel()
    assert match_against_bar(o, ref_price=100.0, prev_close=None, bar_volume=1000, rules=us) is None


# ---------------------------------------------------------------------------
# 多根 bar 驱动 + 撤单
# ---------------------------------------------------------------------------


def test_simulate_partial_across_bars_then_cancel():
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "buy", 1000, market="a_stock")
    simulate_execution(o, [("d1", 10.0, 3000), ("d2", 10.0, 3000)], a,
                       participation_rate=0.1, max_bars=2)
    # 每根 300 手 → 两根 600 < 1000 → 到期撤单
    assert o.status == OrderStatus.CANCELLED
    assert 0 < o.filled_shares < 1000
    assert o.reason == "gtc_expired"


def test_simulate_completes_with_enough_liquidity():
    us = rules_for("us_stock")
    o = Order("o", "AAPL", "buy", 500, market="us_stock")
    simulate_execution(o, [("d1", 100.0, 1000000)], us)
    assert o.status == OrderStatus.FILLED
    assert o.filled_shares == 500


def test_simulate_limit_up_blocks_all_day_cancels_with_zero_fill():
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "buy", 100, market="a_stock")
    simulate_execution(o, [("d1", 110.0, 999999)], a, prev_close=100.0)
    assert o.status == OrderStatus.CANCELLED
    assert o.filled_shares == 0


def test_simulate_no_cancel_leftover_keeps_open():
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "buy", 1000, market="a_stock")
    simulate_execution(o, [("d1", 10.0, 3000)], a, participation_rate=0.1,
                       cancel_leftover=False)
    # 未成交余额保留，订单仍未完结
    assert o.status == OrderStatus.PARTIALLY_FILLED
    assert o.is_open


def test_simulate_prev_close_chains_across_bars():
    # 昨收由上一根 bar 推得：第二根的涨跌停以第一根价为基准
    a = rules_for("a_stock")
    o = Order("o", "600000.SS", "sell", 100, market="a_stock")
    # d1 价 100（首根用传入 prev_close=100），d2 跌到 89（相对 d1 的 100 跌 11% → 封跌停卖不掉）
    simulate_execution(o, [("d1", 100.0, 0), ("d2", 89.0, 999999)], a,
                       prev_close=100.0, cancel_leftover=True)
    # d1 volume=0 无法成交，d2 封跌停无法成交 → 撤单零成交
    assert o.filled_shares == 0
    assert o.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# 确定可复现
# ---------------------------------------------------------------------------


def test_deterministic_reproducibility():
    a = rules_for("a_stock")
    bars = [("d1", 10.0, 3000), ("d2", 10.1, 4000), ("d3", 10.2, 5000)]

    def run():
        o = Order("o", "600000.SS", "buy", 2000, market="a_stock")
        simulate_execution(o, bars, a, participation_rate=0.1, max_bars=3)
        return (o.status, o.filled_shares, tuple((f.shares, f.price) for f in o.fills))

    assert run() == run()


def test_frozen_rules_and_fill_immutable():
    a = rules_for("a_stock")
    with pytest.raises((AttributeError, TypeError)):
        a.tick_size = 0.1  # type: ignore[misc]
    o = Order("o", "T", "buy", 100, market="a_stock")
    f = o.record_fill(100, 10.0)
    with pytest.raises((AttributeError, TypeError)):
        f.shares = 999  # type: ignore[misc]


def test_market_rules_constructible_directly():
    r = MarketRules(tick_size=0.05, lot_size=10, price_limit_pct=5.0,
                    half_spread_bps=1.0, t_plus=0)
    assert round_to_tick(10.03, r) == 10.05
    assert round_to_lot(25, r) == 20
    assert limit_band(100.0, r) == (95.0, 105.0)
