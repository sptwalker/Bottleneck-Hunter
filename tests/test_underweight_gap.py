"""P0-1 扩张侧信号自检 — compute_underweight_gap 只算不拦。

对称化根因：validate_against_regime 只拦"买超上限"，本函数产出"配置不足"缺口信号。
纯确定性，无需 DB / LLM。
"""
from __future__ import annotations

from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap


def test_underweight_gap_basic():
    """权益 30%、目标下限 60% → 缺口 ≈30pct，可部署现金取缺口与现金较小者。"""
    account = {"total_equity": 100000, "cash_balance": 70000}
    positions = [{"ticker": "AAPL", "market_value": 30000}]
    bounds = {"equity_min": 60, "equity_max": 75, "cash_max": 25}

    gap = compute_underweight_gap(account, positions, bounds)

    assert gap["underweight"] is True
    assert gap["equity_pct"] == 30.0
    assert gap["gap_pct"] == 30.0
    # 缺口名义 30000，现金 70000 充足 → 可部署 = 缺口
    assert gap["deployable_cash"] == 30000.0


def test_deployable_capped_by_cash():
    """现金不足以填满缺口时，可部署被现金封顶。"""
    account = {"total_equity": 100000, "cash_balance": 12000}
    positions = [{"ticker": "AAPL", "market_value": 30000}]
    bounds = {"equity_min": 60}

    gap = compute_underweight_gap(account, positions, bounds)

    assert gap["underweight"] is True
    assert gap["gap_pct"] == 30.0
    assert gap["deployable_cash"] == 12000.0  # 被现金封顶，非 30000


def test_not_underweight_when_at_or_above_floor():
    """权益已达/超过下限 → 不触发信号，不产生任何弹药。"""
    account = {"total_equity": 100000, "cash_balance": 20000}
    positions = [{"ticker": "AAPL", "market_value": 65000}]
    bounds = {"equity_min": 60}

    gap = compute_underweight_gap(account, positions, bounds)

    assert gap["underweight"] is False
    assert gap["gap_pct"] == 0.0
    assert gap["deployable_cash"] == 0.0


def test_empty_bounds_noop():
    """无 regime_bounds → 安全返回全零信号（不误报缺口）。"""
    gap = compute_underweight_gap({"total_equity": 100000}, [], {})
    assert gap["underweight"] is False


if __name__ == "__main__":
    test_underweight_gap_basic()
    test_deployable_capped_by_cash()
    test_not_underweight_when_at_or_above_floor()
    test_empty_bounds_noop()
    print("P0-1 compute_underweight_gap 自检通过")
