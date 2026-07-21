"""卖出指令持仓校验 —— 杜绝“卖无持仓/超量卖”的明显错误执行命令。"""
from bottleneck_hunter.watchlist.constraint_validator import (
    validate_execution_plan, max_compliant_shares,
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


if __name__ == "__main__":
    for fn in [test_sell_no_position_blocked, test_sell_exceeds_holding_blocked,
               test_sell_within_holding_ok, test_max_compliant_shares_sell_clamps_to_holding]:
        fn()
    print("卖出持仓校验自检通过")
