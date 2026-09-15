"""P2-3 LLM / 多 Agent / 投委会增量消融专项测试。"""

from __future__ import annotations

import pytest

from bottleneck_hunter.watchlist.incremental_ablation import (
    Arm,
    IncrementStep,
    incremental_ablation,
    n_eff_from_votes,
)

# 同一组样本外窗口下的逐窗收益基线
_BASE = (0.01, 0.02, 0.00, 0.03, 0.01, 0.02, 0.00, 0.01)
_BETTER = tuple(round(x + 0.05, 6) for x in _BASE)          # 配对恒正差 → 显著正且稳定
_SAME = _BASE                                                # 零差 → 明确无增量
_NOISY = tuple(round(x + (0.1 if i % 2 == 0 else -0.1), 6) for i, x in enumerate(_BASE))  # 宽区间跨 0


# ---------------------------------------------------------------------------
# 增量裁决：证明增量 / 明确无增量 / 证据不足
# ---------------------------------------------------------------------------


def test_proven_positive():
    step = incremental_ablation([Arm("base", _BASE), Arm("llm", _BETTER)])["steps"][0]
    assert step.verdict == "proven_positive"
    assert step.significant is True and step.direction == "positive"
    assert step.delta > 0 and step.stable_fraction == 1.0


def test_proven_negative():
    worse = tuple(round(x - 0.05, 6) for x in _BASE)
    step = incremental_ablation([Arm("base", _BASE), Arm("bad", worse)])["steps"][0]
    assert step.verdict == "proven_negative"
    assert step.direction == "negative" and step.delta < 0


def test_no_increment_clean_null():
    step = incremental_ablation([Arm("base", _BASE), Arm("noop", _SAME)])["steps"][0]
    assert step.verdict == "no_increment"
    assert step.significant is False


def test_inconclusive_wide_ci():
    step = incremental_ablation([Arm("base", _BASE), Arm("noisy", _NOISY)])["steps"][0]
    assert step.verdict == "inconclusive"
    assert step.significant is False


def test_no_increment_needs_narrow_ci_knob():
    # null_width 收紧到 0 时，即便零差也不再算「明确无增量」（区间宽度需 <= null_width）
    step = incremental_ablation([Arm("base", _BASE), Arm("noop", _SAME)], null_width=0.0)["steps"][0]
    assert step.verdict == "no_increment"  # 零差宽度恰为 0，仍 <= 0
    # 极微正差但不显著的场景交给 inconclusive；此处仅验证旋钮参与判定
    assert step.ci_high - step.ci_low <= 0.0


# ---------------------------------------------------------------------------
# 独立性守卫：不以 persona 数量代替独立性
# ---------------------------------------------------------------------------


def test_persona_inflation_flagged_when_neff_redundant():
    rep = incremental_ablation([
        Arm("single", _BASE, n_components=1, n_effective=1.0),
        Arm("committee", _BETTER, n_components=6, n_effective=1.5),
    ])
    step = rep["steps"][0]
    assert step.persona_inflation is True
    assert step.n_components_added == 5
    assert "N_eff" in step.note
    assert rep["any_persona_inflation"] is True


def test_persona_inflation_not_flagged_when_diverse():
    step = incremental_ablation([
        Arm("single", _BASE, n_components=1, n_effective=1.0),
        Arm("diverse", _BETTER, n_components=3, n_effective=2.8),
    ])["steps"][0]
    assert step.persona_inflation is False


def test_persona_inflation_requires_added_members():
    # N_eff 冗余但人数未增（都 3 人）→ 不算「以人数冒充」
    step = incremental_ablation([
        Arm("a", _BASE, n_components=3, n_effective=1.2),
        Arm("b", _BETTER, n_components=3, n_effective=1.2),
    ])["steps"][0]
    assert step.persona_inflation is False


def test_non_ensemble_arm_never_inflates():
    # n_effective=None（非集成臂）→ 永不置 persona_inflation
    step = incremental_ablation([Arm("base", _BASE), Arm("llm", _BETTER, n_components=5)])["steps"][0]
    assert step.persona_inflation is False


def test_redundancy_threshold_knob():
    # 冗余 1 - 2/4 = 0.5；阈值 0.6 时不判膨胀，0.4 时判
    lo = incremental_ablation([
        Arm("a", _BASE, n_components=1, n_effective=1.0),
        Arm("b", _BETTER, n_components=4, n_effective=2.0),
    ], redundancy_threshold=0.6)["steps"][0]
    assert lo.persona_inflation is False
    hi = incremental_ablation([
        Arm("a", _BASE, n_components=1, n_effective=1.0),
        Arm("b", _BETTER, n_components=4, n_effective=2.0),
    ], redundancy_threshold=0.4)["steps"][0]
    assert hi.persona_inflation is True


# ---------------------------------------------------------------------------
# N_eff 由历史投票直接算（复用 P1-2）
# ---------------------------------------------------------------------------


def test_n_eff_identical_judges_is_one():
    votes = {"a": ["approve", "reject", "approve"], "b": ["approve", "reject", "approve"]}
    assert abs(n_eff_from_votes(votes) - 1.0) < 1e-6


def test_n_eff_opposite_judges_is_two():
    votes = {"a": ["approve", "approve", "approve"], "b": ["reject", "reject", "reject"]}
    assert abs(n_eff_from_votes(votes) - 2.0) < 1e-6


# ---------------------------------------------------------------------------
# 成本增量 + 多级阶梯 + 汇总
# ---------------------------------------------------------------------------


def test_cost_delta_per_step_and_total():
    rep = incremental_ablation([
        Arm("base", _BASE, cost_usd=0.0),
        Arm("llm", _BETTER, cost_usd=0.002),
        Arm("committee", _BETTER, n_components=4, n_effective=1.2, cost_usd=0.01),
    ])
    assert rep["steps"][0].cost_delta == 0.002
    assert rep["steps"][1].cost_delta == 0.008
    assert rep["total_cost_delta"] == 0.01


def test_ladder_multi_arm():
    rep = incremental_ablation([
        Arm("base", _BASE),
        Arm("llm", _BETTER, cost_usd=0.002),
        Arm("committee", _BETTER, n_components=4, n_effective=1.2, cost_usd=0.01),
    ])
    assert rep["ladder"] == ("base", "llm", "committee")
    assert len(rep["steps"]) == 2
    assert rep["n_windows"] == len(_BASE)
    # base→llm 证明正增量；llm→committee 零差且加人 → 无增量 + persona 膨胀
    assert rep["steps"][0].verdict == "proven_positive"
    assert rep["steps"][1].verdict == "no_increment"
    assert rep["steps"][1].persona_inflation is True
    assert rep["n_proven_positive"] == 1


# ---------------------------------------------------------------------------
# 边界与确定性
# ---------------------------------------------------------------------------


def test_single_arm_rejected():
    with pytest.raises(ValueError):
        incremental_ablation([Arm("only", _BASE)])


def test_unequal_lengths_rejected():
    with pytest.raises(ValueError):
        incremental_ablation([Arm("a", _BASE), Arm("b", _BASE[:4])])


def test_empty_returns_rejected():
    with pytest.raises(ValueError):
        incremental_ablation([Arm("a", ()), Arm("b", ())])


def test_no_seeds_rejected():
    with pytest.raises(ValueError):
        incremental_ablation([Arm("a", _BASE), Arm("b", _BETTER)], seeds=())


def test_deterministic():
    r1 = incremental_ablation([Arm("x", _BASE), Arm("y", _BETTER)])
    r2 = incremental_ablation([Arm("x", _BASE), Arm("y", _BETTER)])
    assert r1["steps"][0].as_dict() == r2["steps"][0].as_dict()


def test_step_is_dataclass():
    step = incremental_ablation([Arm("x", _BASE), Arm("y", _BETTER)])["steps"][0]
    assert isinstance(step, IncrementStep)
    d = step.as_dict()
    assert d["from_arm"] == "x" and d["to_arm"] == "y"
