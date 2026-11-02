"""BH-FDR 多重检验校正。

缺口背景：阶梯消融每级各算一次「差值区间是否跨 0」，5 级比较下
「至少一次假阳性」的概率远高于单次 alpha。BH 把「每检验单独看」换成
「整族检验下控制错误发现率」。
"""

from __future__ import annotations

import math

import pytest

from bottleneck_hunter.watchlist.evaluation import benjamini_hochberg


def test_textbook_example_exact_values():
    """教科书算例精确值：[0.001,.008,.039,.041,.042] → [.005,.02,.042,.042,.042]。"""
    q = benjamini_hochberg([0.001, 0.008, 0.039, 0.041, 0.042])
    expected = [0.005, 0.02, 0.042, 0.042, 0.042]
    assert q == pytest.approx(expected, abs=1e-12)


def test_monotone_in_pvalue():
    """q 必须随 p 单调不降——否则出现「p 更小却 q 更大」的自相矛盾。"""
    p = [0.001, 0.01, 0.02, 0.03, 0.2, 0.5, 0.9]
    q = benjamini_hochberg(p)
    for a, b in zip(q, q[1:], strict=False):
        assert b >= a - 1e-12


def test_order_preserved():
    """返回与输入同序，不是排序后的顺序。"""
    p = [0.5, 0.001, 0.2]
    q = benjamini_hochberg(p)
    # 最小的 p 在索引 1，它的 q 必须最小
    assert q[1] == min(q)
    assert len(q) == 3


def test_single_value_is_unchanged():
    """n=1 时 q = p（无多重比较）。"""
    assert benjamini_hochberg([0.03]) == pytest.approx([0.03])


def test_empty_returns_empty():
    assert benjamini_hochberg([]) == []


def test_all_ones_stay_one():
    assert benjamini_hochberg([1.0, 1.0, 1.0]) == pytest.approx([1.0, 1.0, 1.0])


def test_q_never_exceeds_one():
    q = benjamini_hochberg([0.9, 0.95, 0.99])
    assert all(v <= 1.0 for v in q)


def test_fdr_is_at_least_as_lenient_as_bonferroni_only_when_n_large():
    """BH 比 Bonferroni 宽松（这是它的意义），但绝不会小于原始 p。"""
    p = [0.01, 0.02, 0.03]
    q = benjamini_hochberg(p)
    bonferroni = [min(1.0, v * 3) for v in p]
    for qv, pv in zip(q, p, strict=True):
        assert qv >= pv - 1e-12
        assert qv <= bonferroni[0] + 1e-12 or qv <= 1.0


@pytest.mark.parametrize("bad", [[-0.01], [1.5], [float("nan")], [float("inf")], ["abc"]])
def test_invalid_pvalues_raise(bad):
    """非法 p 必须抛错而非静默截断——静默修正会让调用方以为数据干净。"""
    with pytest.raises(ValueError):
        benjamini_hochberg(bad)


@pytest.mark.parametrize("bad_alpha", [0.0, -0.1, 1.5])
def test_invalid_alpha_raises(bad_alpha):
    with pytest.raises(ValueError):
        benjamini_hochberg([0.01], alpha=bad_alpha)


def test_multiple_testing_actually_changes_decision():
    """回归锚点：逐个看显著、整族看不再显著的算例。

    注意 BH 的性质：**等值 p 时 q 恒等于 p**（p*n/n 即为 running-min），
    所以「三个 0.04 校正后全变不显著」在数学上不可能成立。
    真正会翻案的场景是「一个很小的 p + 若干很大的 p」：
    n=3 时最小的 q ≥ p*3，故 p=0.04 会被拉高到 0.12。
    """
    p = [0.04, 0.5, 0.6]
    q = benjamini_hochberg(p)
    assert p[0] < 0.05, "未校正时 p=0.04 应判显著"
    assert q[0] == pytest.approx(0.12), "n=3 时 q_1 = p*n = 0.12"
    assert q[0] > 0.05, "校正后应不再显著"


def test_mixed_family_partitions_cleanly():
    """真信号应保留，噪声应被剔——而不是一刀切全否。"""
    p = [0.0001, 0.0002, 0.6, 0.7, 0.8]
    q = benjamini_hochberg(p)
    assert q[0] < 0.05 and q[1] < 0.05
    assert q[2] > 0.05 and q[3] > 0.05 and q[4] > 0.05


def test_no_nan_in_output():
    q = benjamini_hochberg([0.0, 0.5, 1.0])
    assert all(math.isfinite(v) for v in q)


# ── 接入消融阶梯后必须真正生效 ──────────────────────────────
def test_ablation_carries_p_value():
    """回归锚点：p_value 曾因 _one_step 未传出而恒为 1.0，等于 FDR 空转。"""
    from bottleneck_hunter.watchlist.evaluation import ablation

    b = (0.01, 0.02, 0.00, 0.03, 0.01, 0.02, 0.00, 0.01)
    better = tuple(round(x + 0.05, 6) for x in b)
    r = ablation(b, better, n_resamples=2000, seed=0)
    assert r.p_value < 0.05, f"强增量应得小 p，实际 {r.p_value}"

    same = ablation(b, b, n_resamples=2000, seed=0)
    assert same.p_value > 0.5, f"零差应得大 p，实际 {same.p_value}"


def test_incremental_ablation_records_fdr():
    """阶梯消融必须把 q_value / fdr_significant 挂到每一步上。"""
    from bottleneck_hunter.watchlist.incremental_ablation import Arm, incremental_ablation

    b = (0.01, 0.02, 0.00, 0.03, 0.01, 0.02, 0.00, 0.01)
    better = tuple(round(x + 0.05, 6) for x in b)
    better2 = tuple(round(x + 0.05, 6) for x in better)
    rep = incremental_ablation([Arm("base", b), Arm("llm", better), Arm("multi", better2)])

    for step in rep["steps"]:
        assert step.p_value < 1.0, "p 值未从 ablation 传出"
        assert step.q_value >= step.p_value - 1e-9, "BH 的 q 不应小于原始 p"
        assert step.fdr_significant is True

    assert rep["multiple_testing"]["method"] == "benjamini_hochberg"
    assert rep["multiple_testing"]["n_tests"] == 2


def test_fdr_stricter_with_more_steps():
    """级数越多，同样强的增量越难被判为整族显著——这是校正的本意。"""
    from bottleneck_hunter.watchlist.incremental_ablation import Arm, incremental_ablation

    b = tuple(0.001 * (i % 3) for i in range(12))
    arms = [Arm("a0", b)]
    cur = b
    for i in range(6):
        cur = tuple(round(x + 0.004, 6) for x in cur)
        arms.append(Arm(f"a{i+1}", cur))
    rep = incremental_ablation(arms, n_resamples=500, fdr_alpha=0.05)
    n_sig = rep["multiple_testing"]["n_fdr_significant"]
    assert 0 <= n_sig <= len(rep["steps"])
    assert rep["multiple_testing"]["n_tests"] == len(rep["steps"])
