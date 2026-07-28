"""P0-2 资金路径边界测试 · position_sizing.PositionSizer

盯「算错就下错单」的边界：负期望/满胜率、除零、封顶(25%/20%)、样本不足。
纯函数无需 DB。
"""
from bottleneck_hunter.watchlist.position_sizing import PositionSizer

S = PositionSizer


def test_kelly_guards_return_zero():
    assert S.kelly_fraction(0.6, 2, 0) == 0.0        # avg_loss<=0
    assert S.kelly_fraction(1.0, 2, 1) == 0.0        # win_rate>=1
    assert S.kelly_fraction(0.0, 2, 1) == 0.0        # win_rate<=0
    assert S.kelly_fraction(0.4, 1, 1) == 0.0        # 负期望 f<=0


def test_kelly_capped_at_quarter():
    # 极高胜率高赔率：半凯利仍应被 0.25 硬顶封住，不许 all-in
    assert S.kelly_fraction(0.9, 5, 1) == 0.25


def test_volatility_scaled_guards_and_cap():
    assert S.volatility_scaled(0.15, 0, 100000, 100) == {"amount": 0, "shares": 0, "weight_pct": 0}
    assert S.volatility_scaled(0.15, 0.30, 0, 100) == {"amount": 0, "shares": 0, "weight_pct": 0}
    r = S.volatility_scaled(0.15, 0.30, 100000, 100)  # 未封顶应 50000，硬顶到 20%
    assert r["weight_pct"] <= 20.0 and r["amount"] == 20000.0


def test_risk_parity_empty_and_zero_price():
    assert S.risk_parity([], {}, 100000) == {}
    # 价格为 0 → 股数 0，不得除零崩
    r = S.risk_parity([{"ticker": "X", "current_price": 0}], {"X": 0.3}, 100000)
    assert r["X"]["shares"] == 0 and r["X"]["weight_pct"] <= 25.0


def test_stock_vol_needs_min_samples():
    assert S.compute_stock_volatility([0.01, 0.02]) == 0.0        # <5 样本
    assert S.compute_stock_volatility([0.01, -0.02, 0.03, -0.01, 0.02]) > 0


def test_suggest_empty_when_no_inputs():
    assert S.suggest("ensemble", 100000, 100) == {}  # 无胜率/无波动率 → 无建议


if __name__ == "__main__":
    for fn in (test_kelly_guards_return_zero, test_kelly_capped_at_quarter,
               test_volatility_scaled_guards_and_cap, test_risk_parity_empty_and_zero_price,
               test_stock_vol_needs_min_samples, test_suggest_empty_when_no_inputs):
        fn()
    print("position_sizing boundary self-check OK")
