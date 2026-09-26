"""P1-E（N-11）：L1 置信度必须进**风险预算**，不能只进一个"建议值"。

病根：`get_allocation_bounds` 把 `regime_confidence` 只用在了 `recommended_equity` 这个
"建议值"上——LLM 完全可以无视它。而真正有牙齿的两个数是：
  - `equity_min`：缺口驱动器 `_plan_gap_fills` 拿 `equity_pct < equity_min` 授权确定性补仓；
  - `equity_max` / `max_single_pct` / `beta_limit` / `cash_max`：越界拦截线。
置信度 1 和 10 领到**同一条补仓线**，等于"我只有 1 分把握这是 sideways"照样能撬动真金白银。

本文件锁三件事：
  ① 生效下界随置信度**单调**收缩，且 high 档回到表内原值（不改变原行为）；
  ② 收缩**只动下界**，上界/单票/β/现金一律不动——降的是"该不该开始补"的门槛，不是风控预算；
  ③ 端到端：低置信度下**真的不下单**（不是"数字小了一点"），且收敛后自然停手。

③ 是这份文件存在的理由：①② 只证明"一个标量动了"，而 N-11 的病是"动了但不影响行为"。
"""
from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.constraint_validator import compute_underweight_gap
from bottleneck_hunter.watchlist.decision_engine import _plan_gap_fills
from bottleneck_hunter.watchlist.regime_mapper import REGIME_MAP, get_allocation_bounds

# 非美元市场，避免 "USD " 等前缀混进断言
MARKET = "a_stock"


def _bounds(regime: str, appetite: str, conf: float) -> dict:
    return get_allocation_bounds(regime, appetite, conf)


# ── ① 单调收缩，且 high 档不改变原行为 ────────────────────────────

@pytest.mark.parametrize("key", list(REGIME_MAP))
def test_equity_min_monotonic_in_confidence(key):
    """任一档位下，置信度越高，生效下界越不低（低置信 → 更保守）。"""
    seq = [_bounds(*key, c)["equity_min"] for c in range(1, 11)]
    assert all(seq[i] <= seq[i + 1] for i in range(9)), f"{key} 下界随置信度非单调: {seq}"


@pytest.mark.parametrize("key", list(REGIME_MAP))
def test_confidence_10_reproduces_table_floor(key):
    """置信度拉满＝表内原值。这是"不改变既有行为"的锚：日常 L1 高置信的配置一分不动。"""
    b = _bounds(*key, 10)
    assert b["equity_min"] == b["equity_min_base"] == REGIME_MAP[key]["equity_pct"][0]


@pytest.mark.parametrize("key", list(REGIME_MAP))
def test_confidence_1_falls_to_defensive_floor(key):
    """置信度最低＝退到同 regime 防御档下界："没把握就别扩张"，但**不越出该 regime**。"""
    b = _bounds(*key, 1)
    assert b["equity_min"] == REGIME_MAP[(key[0], "defensive")]["equity_pct"][0]


def test_sideways_balanced_hand_computed():
    """手算锚点（报告要求先拿数值验算公式）：sideways/balanced 表内(40,60)、defensive 下界 25。
    conf=5 → w=(5-1)/9=0.4444 → 40-(1-0.4444)*15=31.67→31.7。"""
    assert _bounds("sideways", "balanced", 1)["equity_min"] == 25.0
    assert _bounds("sideways", "balanced", 5)["equity_min"] == 31.7
    assert _bounds("sideways", "balanced", 10)["equity_min"] == 40.0


# ── ② 风控预算一分不放松 ────────────────────────────────────────

@pytest.mark.parametrize("key", list(REGIME_MAP))
def test_risk_budget_untouched_by_confidence(key):
    """除 equity_min 外，任何置信度下都不许变。低置信只收紧扩张门槛，绝不放宽风控。"""
    ref = _bounds(*key, 10)
    for c in (1, 2, 5, 8):
        b = _bounds(*key, c)
        for k in ("equity_max", "cash_min", "cash_max", "max_single_pct", "beta_limit"):
            assert b[k] == ref[k], f"{key} conf={c} 的 {k} 被置信度改动了：{b[k]} != {ref[k]}"


def test_unknown_regime_no_shift():
    """未知 regime 回退 sideways/balanced，锚点取不到 → 零偏移（不许凭空放大也不许收窄）。"""
    for r in ("unknown_regime", ""):
        for c in (1, 5, 10):
            b = _bounds(r, "balanced", c)
            assert b["equity_min"] == b["equity_min_base"] == 40


# ── ③ 端到端：低置信度**真的不下单** ──────────────────────────────

CORE = [{"ticker": "600519", "target_weight_pct": 12}, {"ticker": "000858", "target_weight_pct": 12}]


def test_low_confidence_produces_no_fills():
    """N-11 的行为面：权益 30%、现金 70%。

    sideways/balanced 表内下界 40% → 权益 30% 低于它，旧口径**必然**产补仓单。
    置信度 1 时生效下界降到 25%——但 30% 已在 25% 之上 → 不再授权补仓。
    这条断言的就是"置信度真的改变了行为"，而不只是 recommended_equity 小了几个点。
    """
    account = {"total_equity": 100000, "cash_balance": 70000}
    positions = [{"ticker": "600519", "market_value": 30000}]

    old = _plan_gap_fills(account, positions, _bounds("sideways", "balanced", 10), CORE, MARKET)
    assert old, "高置信度下权益 30% < 40% 下限 → 必须授权补仓（旧行为不能被改坏）"

    low = _bounds("sideways", "balanced", 1)
    assert compute_underweight_gap(account, positions, low)["underweight"] is False
    assert _plan_gap_fills(account, positions, low, CORE, MARKET) == []


def test_confidence_shrinks_but_does_not_zero_the_fill():
    """中间置信度：仍授权补仓，但**补得比高置信少**——不是一刀切断，是梯度收紧。"""
    account = {"total_equity": 100000, "cash_balance": 70000}
    positions = [{"ticker": "600519", "market_value": 20000}]  # 权益 20%，低于任何档下界

    hi = sum(f["amount"] for f in _plan_gap_fills(account, positions, _bounds("sideways", "balanced", 10), CORE, MARKET))
    mid = sum(f["amount"] for f in _plan_gap_fills(account, positions, _bounds("sideways", "balanced", 4), CORE, MARKET))
    lo = sum(f["amount"] for f in _plan_gap_fills(account, positions, _bounds("sideways", "balanced", 1), CORE, MARKET))

    assert hi > mid > lo > 0, f"缺口驱动的弹量未随置信度递减: hi={hi} mid={mid} lo={lo}"


if __name__ == "__main__":
    for _k in REGIME_MAP:
        test_equity_min_monotonic_in_confidence(_k)
        test_confidence_10_reproduces_table_floor(_k)
        test_confidence_1_falls_to_defensive_floor(_k)
        test_risk_budget_untouched_by_confidence(_k)
    test_sideways_balanced_hand_computed()
    test_unknown_regime_no_shift()
    test_low_confidence_produces_no_fills()
    test_confidence_shrinks_but_does_not_zero_the_fill()
    print("P1-E（N-11）自检通过：置信度进风险预算，只收紧扩张门槛、不放松风控")
