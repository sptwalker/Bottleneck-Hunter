"""P0-2 资金路径边界测试 · risk_metrics.compute_portfolio_risk

只盯「错了会亏钱/会崩」的边界：空组合、除零、集中度阈值、VaR 计算、脏价格。
纯函数无需 DB。
"""
from bottleneck_hunter.watchlist.risk_metrics import compute_portfolio_risk


def test_empty_positions_returns_defaults():
    m = compute_portfolio_risk([], {})
    assert m.concentration_index == 0.0 and m.var_95 == 0.0
    assert m.warnings == [] and m.correlation_pairs == []


def test_zero_equity_no_division_error():
    # weight_pct 缺失且 total_equity=0：绝不能 ZeroDivisionError，权重退化为 0
    m = compute_portfolio_risk(
        [{"ticker": "AAPL", "market_value": 20000}], {}, total_equity=0.0,
    )
    assert m.concentration_index == 0.0  # w=0 → HHI=0


def test_single_position_full_concentration_warns():
    m = compute_portfolio_risk([{"ticker": "AAPL", "weight_pct": 100.0}], {})
    assert m.concentration_index == 1.0
    assert any("集中" in w for w in m.warnings)
    assert m.max_single_weight == 100.0


def test_var_cvar_nonnegative_with_history():
    # 单票满仓 + 一个下跌日 → VaR 应为正数（损失额），且不为负
    m = compute_portfolio_risk(
        [{"ticker": "AAPL", "weight_pct": 100.0}],
        {"AAPL": [100.0, 90.0, 99.0]},  # returns: -10%, +10%
        total_equity=100000.0,
    )
    assert m.var_95 >= 0 and m.cvar_95 >= 0
    assert m.var_95 == 10000.0  # 5% 分位落在 -10% 那天 × 10万


def test_zero_price_in_history_no_crash():
    # 历史里混入 0 价（脏数据）：prices[i-1]>0 守卫应跳过，不得 ZeroDivisionError
    m = compute_portfolio_risk(
        [{"ticker": "AAPL", "weight_pct": 100.0}],
        {"AAPL": [0.0, 100.0, 110.0]},
    )
    assert m.var_95 >= 0  # 只用了 100→110 那段，未崩


if __name__ == "__main__":
    test_empty_positions_returns_defaults()
    test_zero_equity_no_division_error()
    test_single_position_full_concentration_warns()
    test_var_cvar_nonnegative_with_history()
    test_zero_price_in_history_no_crash()
    print("risk_metrics boundary self-check OK")
