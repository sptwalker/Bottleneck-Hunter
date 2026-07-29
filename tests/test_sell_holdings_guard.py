"""卖出指令持仓校验 —— 杜绝“卖无持仓/超量卖”的明显错误执行命令。"""
from bottleneck_hunter.watchlist.constraint_validator import (
    max_compliant_shares,
    validate_execution_plan,
)

# 宽松约束：只让持仓校验起作用，避免被金额/额度上限干扰
_LOOSE = {
    "max_single_trade_usd": 1e12, "max_single_position_pct": 100,
    "max_sector_pct": 100, "min_cash_pct": 0, "max_daily_turnover_pct": 100,
}
_ACCOUNT = {"total_equity": 100000, "cash_balance": 50000}


def _sell(ticker, shares, price=10.0):
    return {"action": "sell", "ticker": ticker, "shares": shares, "target_price": price}


def test_sell_no_position_blocked():
    r = validate_execution_plan(_sell("AAPL", 100), _ACCOUNT, positions=[], constraints=_LOOSE)
    assert not r.valid and any("无持仓" in v for v in r.violations)


def test_sell_exceeds_holding_blocked():
    pos = [{"ticker": "AAPL", "shares": 80, "market_value": 800}]
    r = validate_execution_plan(_sell("AAPL", 100), _ACCOUNT, pos, _LOOSE)
    assert not r.valid and any("超过" in v for v in r.violations)


def test_sell_within_holding_ok():
    pos = [{"ticker": "AAPL", "shares": 100, "market_value": 1000}]
    r = validate_execution_plan(_sell("AAPL", 80), _ACCOUNT, pos, _LOOSE)
    assert r.valid, r.violations


def test_max_compliant_shares_sell_clamps_to_holding():
    pos = [{"ticker": "AAPL", "shares": 80, "market_value": 800}]
    assert max_compliant_shares(_sell("AAPL", 100), _ACCOUNT, pos, _LOOSE) == 80
    assert max_compliant_shares(_sell("AAPL", 100), _ACCOUNT, [], _LOOSE) == 0


def _buy(ticker, shares, price=10.0):
    return {"action": "buy", "ticker": ticker, "shares": shares, "target_price": price}


def test_buy_exceeds_cash_blocked():
    # 现金 50000，买 100000 → 超余额，即便其它约束宽松也须拦
    acct = {"total_equity": 200000, "cash_balance": 50000}
    r = validate_execution_plan(_buy("AAPL", 1000, 100.0), acct, positions=[], constraints=_LOOSE)
    assert not r.valid and any("现金不足" in v for v in r.violations)


def test_buy_within_cash_ok():
    acct = {"total_equity": 200000, "cash_balance": 50000}
    r = validate_execution_plan(_buy("AAPL", 100, 100.0), acct, positions=[], constraints=_LOOSE)
    assert r.valid, r.violations


def test_negative_shares_blocked():
    r = validate_execution_plan(_buy("AAPL", -10, 100.0), _ACCOUNT, positions=[], constraints=_LOOSE)
    assert not r.valid and any("非法" in v for v in r.violations)


# ── 审查修复回归（F1-F4）──────────────────────────────
def test_zero_equity_still_enforces_cash_and_holdings():
    """total_equity<=0 不再 fail-open：买入超现金 / 卖无持仓 仍拦（F1）。"""
    acct = {"total_equity": 0, "cash_balance": 1000}
    rb = validate_execution_plan(_buy("AAPL", 100, 100.0), acct, positions=[], constraints=_LOOSE)
    assert not rb.valid and any("现金不足" in v for v in rb.violations)
    rs = validate_execution_plan(_sell("AAPL", 10), acct, positions=[], constraints=_LOOSE)
    assert not rs.valid and any("无持仓" in v for v in rs.violations)


def test_string_shares_do_not_crash():
    """LLM 可能传字符串股数/价格；不得崩溃（F4）。"""
    plan = {"action": "buy", "ticker": "AAPL", "shares": "100", "target_price": "100"}
    r = validate_execution_plan(plan, {"total_equity": 1e9, "cash_balance": 1e9}, [], _LOOSE)
    assert r.valid, r.violations


def test_sell_ticker_normalization_match():
    """A股 600519 vs 持仓 600519.SS 应归一化后匹配，不误判无持仓（F2）。"""
    plan = {"action": "sell", "ticker": "600519", "shares": 10, "target_price": 100.0,
            "result_json": {"market": "a_stock"}}
    pos = [{"ticker": "600519.SS", "shares": 100, "market_value": 10000}]
    r = validate_execution_plan(plan, _ACCOUNT, pos, _LOOSE)
    assert r.valid, r.violations


def test_sell_missing_price_still_checks_holdings():
    """卖出缺价也要拦「无持仓」，不能被缺价早返回绕过（F3）。"""
    plan = {"action": "sell", "ticker": "AAPL", "shares": 50, "target_price": 0}
    r = validate_execution_plan(plan, _ACCOUNT, positions=[], constraints=_LOOSE)
    assert not r.valid and any("无持仓" in v for v in r.violations)


# ── 单笔额度币种自适应（A股¥100万账户不被美元绝对值误拦）──────────────
def test_single_trade_cap_scales_by_equity_for_astock():
    """A股¥100万账户买入 ~20% 单股(¥20万)应通过：单笔上限=max($5万绝对, 50%×权益)=¥50万。

    旧逻辑单笔恒 $50000，对¥100万账户只剩 5%，正常委托整批误拦（外网 A股 L4 全失败根因）。
    """
    from bottleneck_hunter.watchlist.constraint_validator import get_constraints_for_appetite
    c = get_constraints_for_appetite("balanced")  # 默认档，含 max_single_trade_pct
    acct = {"total_equity": 1_000_000, "cash_balance": 1_000_000}
    plan = {"action": "buy", "ticker": "600519", "shares": 2000, "target_price": 100.0,
            "sector": "白酒", "result_json": {"market": "a_stock"}}  # ¥20万，占比 20%<25%
    r = validate_execution_plan(plan, acct, positions=[], constraints=c)
    assert r.valid, r.violations
    # 美股¥10万账户行为不变：单笔上限仍 $50000，买 $60000 超限
    us = {"total_equity": 100_000, "cash_balance": 100_000}
    plan_us = {"action": "buy", "ticker": "AAPL", "shares": 600, "target_price": 100.0,
               "result_json": {"market": "us_stock"}}  # $60000 > $50000
    r_us = validate_execution_plan(plan_us, us, positions=[], constraints=c)
    assert not r_us.valid and any("单笔金额" in v for v in r_us.violations)


if __name__ == "__main__":
    import sys
    mod = sys.modules[__name__]
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        getattr(mod, name)()
    print("交易约束校验自检通过")
